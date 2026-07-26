from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from server.cleanup import cleanup_terminal_recoveries, expire_pending_approvals
from server.config import RuntimeSettings
from server.main import create_app
from server.models import ApprovalDecisionRequest, DecisionAction, ExecutionMode, RecoveryStatus
from server.orchestrator import RecoveryOrchestrator
from server.providers.hotel_simulator import HotelSimulator
from server.store import ApprovalDecisionError, RecoveryNotFoundError, SQLiteStore


def _canonical_request_digest(store: SQLiteStore, recovery_id: str) -> str:
    consent = store.get_remedy_consent(recovery_id)
    serialized = json.dumps(
        {
            "recovery_id": recovery_id,
            "remedy": consent.evidence.remedy.model_dump(mode="json"),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _claimed(
    database_path: Path,
    *,
    action: DecisionAction,
    pending_status: str = "pending",
    client_decision_id: str | None = None,
) -> tuple[SQLiteStore, HotelSimulator, str, datetime, ApprovalDecisionRequest]:
    store = SQLiteStore(database_path)
    provider = HotelSimulator(store=store)
    pending = asyncio.run(
        RecoveryOrchestrator(store=store, hotel_provider=provider).start(
            "hotel",
            execution_mode=ExecutionMode.SDK_STUB,
        )
    )
    approval = pending.recovery.pending_approval
    assert approval is not None
    request = ApprovalDecisionRequest(
        action=action,
        clientDecisionId=(client_decision_id or f"claimed-expiry-{action.value}-{pending_status}"),
        remedyId=approval.remedy_id,
        remedyDigest=approval.remedy_digest,
        toolCallId=approval.tool_call_id,
    )
    store.claim_approval_decision(pending.recovery.recovery_id, request)
    if pending_status != "pending":
        if action is DecisionAction.APPROVE:
            store.update_pending_approval_status(
                pending.recovery.recovery_id,
                expected_status="pending",
                status=pending_status,
            )
        else:
            updated_at = datetime.now(UTC).isoformat()
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    """
                    UPDATE pending_approvals
                    SET status = ?, updated_at = ?
                    WHERE recovery_id = ?
                    """,
                    (pending_status, updated_at, pending.recovery.recovery_id),
                )
    return store, provider, pending.recovery.recovery_id, approval.expiry, request


@pytest.mark.parametrize(
    ("action", "pending_status", "expected_status", "provider_execution", "approval_count"),
    [
        (DecisionAction.APPROVE, "pending", "outcome_unknown", None, 1),
        (DecisionAction.APPROVE, "approved", "outcome_unknown", None, 1),
        (DecisionAction.DECLINE, "pending", "closed_without_action", False, 0),
        (DecisionAction.DECLINE, "approved", "outcome_unknown", None, 0),
        (DecisionAction.DECLINE, "outcome_unknown", "outcome_unknown", None, 0),
    ],
)
def test_expired_claim_matrix_seals_exact_terminal_evidence(
    tmp_path: Path,
    action: DecisionAction,
    pending_status: str,
    expected_status: str,
    provider_execution: bool | None,
    approval_count: int,
) -> None:
    database_path = tmp_path / f"{action.value}-{pending_status}.sqlite3"
    store, provider, recovery_id, expiry, _request = _claimed(
        database_path,
        action=action,
        pending_status=pending_status,
    )

    assert (
        expire_pending_approvals(
            store,
            now=expiry,
            batch_size=1,
            recovery_id=recovery_id,
        )
        == 1
    )

    recovery = store.get_recovery(recovery_id)
    receipt = store.get_receipt(recovery_id)
    terminal_events = [event for event in store.list_events(recovery_id) if event.terminal]
    assert recovery.status is RecoveryStatus(expected_status)
    assert recovery.pending_approval is None
    assert recovery.claimed_decision is None
    assert receipt.status == expected_status
    assert receipt.provider_execution is provider_execution
    assert receipt.approval_count == approval_count
    assert receipt.approved_remedy_digest is None
    assert len(terminal_events) == 1
    assert terminal_events[0].type == "recovery.claim_expired"
    assert terminal_events[0].data["decisionAction"] == action.value
    assert "executionCount" not in terminal_events[0].data or expected_status == (
        "closed_without_action"
    )
    assert store.count_executions(recovery_id) == 0
    assert provider.dispatch_count == 0
    assert store.recovery_has_expiration_evidence(recovery_id) is True
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT status FROM approval_decisions WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone() == ("claimed",)


def test_claimed_expiry_releases_admission_without_deleting_usage(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "claimed-expiry-admission.sqlite3"
    store, _provider, recovery_id, expiry, _request = _claimed(
        database_path,
        action=DecisionAction.APPROVE,
    )
    admitted_at = expiry - timedelta(minutes=1)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO live_admissions (
                recovery_id, ip_key, session_key, budget_units,
                admitted_at, expires_at, released_at
            ) VALUES (?, ?, ?, 1, ?, ?, NULL)
            """,
            (
                recovery_id,
                "b" * 64,
                "a" * 64,
                admitted_at.isoformat(),
                (expiry + timedelta(minutes=10)).isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO usage_ledger (
                recovery_id, category, amount, recorded_at
            ) VALUES (?, 'live_demo_budget_unit', 1, ?)
            """,
            (recovery_id, admitted_at.isoformat()),
        )

    assert (
        expire_pending_approvals(
            store,
            now=expiry,
            batch_size=1,
            recovery_id=recovery_id,
        )
        == 1
    )
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            """
            SELECT released_at IS NOT NULL FROM live_admissions
            WHERE recovery_id = ?
            """,
            (recovery_id,),
        ).fetchone() == (1,)
        assert connection.execute(
            """
            SELECT category, amount FROM usage_ledger
            WHERE recovery_id = ?
            """,
            (recovery_id,),
        ).fetchall() == [("live_demo_budget_unit", 1)]
    assert store.recovery_has_expiration_evidence(recovery_id) is True


@pytest.mark.parametrize(
    ("status", "provider_execution", "result_json"),
    [
        ("pending", 0, None),
        ("pending", 1, None),
        ("completed", 0, {"provider_result": "malformed"}),
        ("completed", 1, {"unexpected": "shape"}),
    ],
)
def test_any_noncanonical_execution_evidence_seals_unknown_without_rewrite(
    tmp_path: Path,
    status: str,
    provider_execution: int,
    result_json: dict[str, str] | None,
) -> None:
    database_path = tmp_path / f"execution-{status}-{provider_execution}.sqlite3"
    store, _provider, recovery_id, expiry, request = _claimed(
        database_path,
        action=DecisionAction.APPROVE,
        pending_status="approved",
    )
    now = datetime.now(UTC).isoformat()
    serialized = (
        json.dumps(result_json, separators=(",", ":"), sort_keys=True)
        if result_json is not None
        else None
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO executions (
                id, recovery_id, idempotency_key, status, provider_execution,
                request_digest, tool_call_id, remedy_digest, result_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"execution-{status}-{provider_execution}",
                recovery_id,
                f"idempotency-{status}-{provider_execution}",
                status,
                provider_execution,
                "d" * 64,
                request.tool_call_id,
                request.remedy_digest,
                serialized,
                now,
                now,
            ),
        )
        before = connection.execute(
            "SELECT * FROM executions WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchall()

    assert (
        expire_pending_approvals(
            store,
            now=expiry,
            batch_size=1,
            recovery_id=recovery_id,
        )
        == 1
    )
    assert store.get_recovery(recovery_id).status is RecoveryStatus.OUTCOME_UNKNOWN
    assert store.get_receipt(recovery_id).provider_execution is None
    assert (
        "executionCount"
        not in [event for event in store.list_events(recovery_id) if event.terminal][0].data
    )
    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute(
                "SELECT * FROM executions WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchall()
            == before
        )


def test_sole_canonical_completed_approve_is_left_for_completed_reconciliation(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "completed-before-expiry.sqlite3"
    store, provider, recovery_id, expiry, request = _claimed(
        database_path,
        action=DecisionAction.APPROVE,
        pending_status="approved",
    )
    execution, dispatched = store.record_completed_execution(
        execution_id="execution-completed-before-expiry",
        recovery_id=recovery_id,
        idempotency_key=(f"{recovery_id}:{request.tool_call_id}:{request.remedy_digest}"),
        request_digest=_canonical_request_digest(store, recovery_id),
        tool_call_id=request.tool_call_id,
        remedy_digest=request.remedy_digest,
        result_json={
            "dispatch_id": "dispatch-completed-before-expiry",
            "status": "confirmed",
            "simulated": True,
            "provider_result": "Bound demo-provider result.",
        },
    )
    assert dispatched is True

    assert (
        expire_pending_approvals(
            store,
            now=expiry,
            batch_size=1,
            recovery_id=recovery_id,
        )
        == 0
    )
    assert store.get_recovery(recovery_id).status is RecoveryStatus.PENDING_APPROVAL
    assert not any(event.terminal for event in store.list_events(recovery_id))

    store.close()
    restarted = SQLiteStore(database_path)
    RecoveryOrchestrator(
        store=restarted,
        hotel_provider=HotelSimulator(store=restarted),
    )
    assert restarted.get_recovery(recovery_id).status is RecoveryStatus.COMPLETED
    assert restarted.get_completed_execution(recovery_id) == execution
    assert restarted.get_receipt(recovery_id).status == "completed"
    assert provider.dispatch_count == 0


@pytest.mark.parametrize(
    "tamper",
    ["wrong_request_digest", "execution_before_claim", "execution_after_expiry"],
)
def test_wrong_request_or_execution_chronology_cannot_bypass_claim_expiry(
    tmp_path: Path,
    tamper: str,
) -> None:
    database_path = tmp_path / f"execution-binding-{tamper}.sqlite3"
    store, _provider, recovery_id, expiry, request = _claimed(
        database_path,
        action=DecisionAction.APPROVE,
        pending_status="approved",
    )
    request_digest = (
        "d" * 64
        if tamper == "wrong_request_digest"
        else _canonical_request_digest(store, recovery_id)
    )
    execution, dispatched = store.record_completed_execution(
        execution_id=f"execution-{tamper}",
        recovery_id=recovery_id,
        idempotency_key=(f"{recovery_id}:{request.tool_call_id}:{request.remedy_digest}"),
        request_digest=request_digest,
        tool_call_id=request.tool_call_id,
        remedy_digest=request.remedy_digest,
        result_json={
            "dispatch_id": f"dispatch-{tamper}",
            "status": "confirmed",
            "simulated": True,
            "provider_result": "Bound demo-provider result.",
        },
    )
    assert dispatched is True
    if tamper != "wrong_request_digest":
        with sqlite3.connect(database_path) as connection:
            claimed_at = datetime.fromisoformat(
                connection.execute(
                    """
                    SELECT claimed_at FROM approval_decisions
                    WHERE recovery_id = ?
                    """,
                    (recovery_id,),
                ).fetchone()[0]
            )
            execution_time = (
                claimed_at - timedelta(microseconds=1)
                if tamper == "execution_before_claim"
                else expiry + timedelta(microseconds=1)
            )
            connection.execute(
                """
                UPDATE executions
                SET created_at = ?, updated_at = ?
                WHERE recovery_id = ?
                """,
                (
                    execution_time.isoformat(),
                    execution_time.isoformat(),
                    recovery_id,
                ),
            )

    assert (
        expire_pending_approvals(
            store,
            now=expiry + timedelta(seconds=1),
            batch_size=1,
            recovery_id=recovery_id,
        )
        == 1
    )
    assert store.get_completed_execution(recovery_id) == execution
    assert store.get_recovery(recovery_id).status is RecoveryStatus.OUTCOME_UNKNOWN
    receipt = store.get_receipt(recovery_id)
    assert receipt.status == "outcome_unknown"
    assert receipt.provider_execution is None
    assert receipt.approved_remedy_digest is None
    assert store.recovery_has_expiration_evidence(recovery_id) is True


@pytest.mark.parametrize(
    ("execution_evidence", "expected_execution_count"),
    [
        ("wrong_request_digest", 1),
        ("multiple_completed_rows", 2),
    ],
)
def test_app_startup_defers_unsafe_claimed_execution_to_lifespan_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    execution_evidence: str,
    expected_execution_count: int,
) -> None:
    database_path = tmp_path / f"startup-{execution_evidence}.sqlite3"
    store, provider, recovery_id, expiry, request = _claimed(
        database_path,
        action=DecisionAction.APPROVE,
        pending_status="approved",
    )
    first_execution, dispatched = store.record_completed_execution(
        execution_id=f"execution-startup-{execution_evidence}",
        recovery_id=recovery_id,
        idempotency_key=(f"{recovery_id}:{request.tool_call_id}:{request.remedy_digest}"),
        request_digest=(
            "d" * 64
            if execution_evidence == "wrong_request_digest"
            else _canonical_request_digest(store, recovery_id)
        ),
        tool_call_id=request.tool_call_id,
        remedy_digest=request.remedy_digest,
        result_json={
            "dispatch_id": f"dispatch-startup-{execution_evidence}",
            "status": "confirmed",
            "simulated": True,
            "provider_result": "Bound demo-provider result.",
        },
    )
    assert dispatched is True
    if execution_evidence == "multiple_completed_rows":
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                """
                INSERT INTO executions (
                    id, recovery_id, idempotency_key, status,
                    provider_execution, request_digest, tool_call_id,
                    remedy_digest, result_json, created_at, updated_at
                )
                SELECT
                    ?, recovery_id, ?, status, provider_execution,
                    request_digest, tool_call_id, remedy_digest,
                    result_json, created_at, updated_at
                FROM executions WHERE id = ?
                """,
                (
                    "execution-startup-ambiguous-second",
                    f"{first_execution.idempotency_key}:ambiguous",
                    first_execution.execution_id,
                ),
            )
    with sqlite3.connect(database_path) as connection:
        executions_before = connection.execute(
            """
            SELECT * FROM executions
            WHERE recovery_id = ?
            ORDER BY id ASC
            """,
            (recovery_id,),
        ).fetchall()
    assert len(executions_before) == expected_execution_count

    frozen_now = expiry + timedelta(microseconds=1)

    class ExpiredCleanupClock(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return frozen_now.astimezone(tz)
            return frozen_now.replace(tzinfo=None)

    monkeypatch.setattr("server.cleanup.datetime", ExpiredCleanupClock)
    dispatch = Mock(side_effect=AssertionError("provider dispatch must not run"))
    monkeypatch.setattr(provider, "dispatch", dispatch)

    app = create_app(
        RuntimeSettings(
            live_ready=False,
            terminal_cleanup_interval_seconds=300,
        ),
        store=store,
        hotel_provider=provider,
    )

    assert store.get_recovery(recovery_id).status is RecoveryStatus.PENDING_APPROVAL
    assert store.get_pending_approval(recovery_id).status == "approved"
    assert not any(event.terminal for event in store.list_events(recovery_id))
    with pytest.raises(RecoveryNotFoundError, match="Receipt not found"):
        store.get_receipt(recovery_id)

    with TestClient(app):
        assert store.get_recovery(recovery_id).status is RecoveryStatus.OUTCOME_UNKNOWN
        assert store.get_pending_approval(recovery_id).status == "outcome_unknown"
        receipt = store.get_receipt(recovery_id)
        assert receipt.status == "outcome_unknown"
        assert receipt.provider_execution is None
        assert receipt.approval_count == 1
        assert receipt.approved_remedy_digest is None
        terminal_events = [event for event in store.list_events(recovery_id) if event.terminal]
        assert len(terminal_events) == 1
        assert terminal_events[0].type == "recovery.claim_expired"
        assert "executionCount" not in terminal_events[0].data
        assert store.recovery_has_expiration_evidence(recovery_id) is True
        with sqlite3.connect(database_path) as connection:
            executions_after = connection.execute(
                """
                SELECT * FROM executions
                WHERE recovery_id = ?
                ORDER BY id ASC
                """,
                (recovery_id,),
            ).fetchall()
            assert connection.execute(
                """
                SELECT status, result_json, completed_at
                FROM approval_decisions WHERE recovery_id = ?
                """,
                (recovery_id,),
            ).fetchone() == ("claimed", None, None)
        assert executions_after == executions_before

    dispatch.assert_not_called()
    assert provider.dispatch_count == 0


def test_claimed_expiry_is_idempotent_and_respects_one_overall_batch(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "bounded.sqlite3"
    store, _provider, first_id, expiry, _request = _claimed(
        database_path,
        action=DecisionAction.APPROVE,
    )
    _second_store, _provider, second_id, second_expiry, _request = _claimed(
        database_path,
        action=DecisionAction.DECLINE,
    )

    assert (
        expire_pending_approvals(
            store,
            now=max(expiry, second_expiry),
            batch_size=1,
        )
        == 1
    )
    terminal_after_first = {
        recovery_id
        for recovery_id in (first_id, second_id)
        if store.get_recovery(recovery_id).status.terminal
    }
    assert len(terminal_after_first) == 1
    assert (
        expire_pending_approvals(
            store,
            now=max(expiry, second_expiry),
            batch_size=1,
        )
        == 1
    )
    assert all(
        store.get_recovery(recovery_id).status.terminal for recovery_id in (first_id, second_id)
    )
    first_events = store.list_events(first_id)
    first_receipt = store.get_receipt(first_id)
    assert (
        expire_pending_approvals(
            store,
            now=max(expiry, second_expiry),
            batch_size=2,
        )
        == 0
    )
    assert store.list_events(first_id) == first_events
    assert store.get_receipt(first_id) == first_receipt


def test_claimed_expiry_write_failure_rolls_back_every_terminal_write(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "claimed-expiry-rollback.sqlite3"
    store, _provider, recovery_id, expiry, _request = _claimed(
        database_path,
        action=DecisionAction.APPROVE,
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER block_claim_expiry_pending_update
            BEFORE UPDATE ON pending_approvals
            WHEN NEW.status = 'outcome_unknown'
            BEGIN
                SELECT RAISE(ABORT, 'injected claimed expiry failure');
            END
            """
        )
        before = {
            table: connection.execute(
                f"SELECT * FROM {table} WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchall()
            for table in (
                "pending_approvals",
                "remedies",
                "approval_decisions",
                "executions",
                "events",
                "receipts",
            )
        }
        before["recoveries"] = connection.execute(
            "SELECT * FROM recoveries WHERE id = ?",
            (recovery_id,),
        ).fetchall()

    with pytest.raises(
        sqlite3.IntegrityError,
        match="injected claimed expiry failure",
    ):
        expire_pending_approvals(
            store,
            now=expiry,
            batch_size=1,
            recovery_id=recovery_id,
        )

    with sqlite3.connect(database_path) as connection:
        after = {
            table: connection.execute(
                f"SELECT * FROM {table} WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchall()
            for table in (
                "pending_approvals",
                "remedies",
                "approval_decisions",
                "executions",
                "events",
                "receipts",
            )
        }
        after["recoveries"] = connection.execute(
            "SELECT * FROM recoveries WHERE id = ?",
            (recovery_id,),
        ).fetchall()

    assert after == before
    assert store.get_recovery(recovery_id).status is RecoveryStatus.PENDING_APPROVAL
    assert not any(event.terminal for event in store.list_events(recovery_id))
    assert store.recovery_has_expiration_evidence(recovery_id) is False


def test_claimed_expiry_recognizer_fails_closed_for_each_tampered_layer(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "claimed-expiry-tamper.sqlite3"
    store, _provider, recovery_id, expiry, _request = _claimed(
        database_path,
        action=DecisionAction.APPROVE,
    )
    assert (
        expire_pending_approvals(
            store,
            now=expiry,
            batch_size=1,
            recovery_id=recovery_id,
        )
        == 1
    )
    assert store.recovery_has_expiration_evidence(recovery_id) is True

    with sqlite3.connect(database_path) as connection:
        event_json = connection.execute(
            """
            SELECT data_json FROM events
            WHERE recovery_id = ? AND terminal = 1
            """,
            (recovery_id,),
        ).fetchone()[0]
        event_data = json.loads(event_json)
        connection.execute(
            """
            UPDATE events SET data_json = ?
            WHERE recovery_id = ? AND terminal = 1
            """,
            (
                json.dumps(
                    {**event_data, "executionCount": 0},
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                recovery_id,
            ),
        )
    assert store.recovery_has_expiration_evidence(recovery_id) is False

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            UPDATE events SET data_json = ?
            WHERE recovery_id = ? AND terminal = 1
            """,
            (event_json, recovery_id),
        )
        receipt_created_at = connection.execute(
            "SELECT created_at FROM receipts WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone()[0]
        connection.execute(
            "UPDATE receipts SET created_at = ? WHERE recovery_id = ?",
            (
                (datetime.fromisoformat(receipt_created_at) + timedelta(seconds=1)).isoformat(),
                recovery_id,
            ),
        )
    assert store.recovery_has_expiration_evidence(recovery_id) is False

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE receipts SET created_at = ? WHERE recovery_id = ?",
            (receipt_created_at, recovery_id),
        )
        request_fingerprint = connection.execute(
            """
            SELECT request_fingerprint FROM approval_decisions
            WHERE recovery_id = ?
            """,
            (recovery_id,),
        ).fetchone()[0]
        replacement = "0" * 64 if request_fingerprint != "0" * 64 else "1" * 64
        connection.execute(
            """
            UPDATE approval_decisions SET request_fingerprint = ?
            WHERE recovery_id = ?
            """,
            (replacement, recovery_id),
        )
    assert store.recovery_has_expiration_evidence(recovery_id) is False

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            UPDATE approval_decisions SET request_fingerprint = ?
            WHERE recovery_id = ?
            """,
            (request_fingerprint, recovery_id),
        )
    assert store.recovery_has_expiration_evidence(recovery_id) is True


def test_corrupt_claimed_pages_do_not_starve_valid_expired_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "claimed-expiry-fairness.sqlite3"
    candidates: list[tuple[datetime, str]] = []
    store: SQLiteStore | None = None
    for index, action in enumerate(
        (
            DecisionAction.APPROVE,
            DecisionAction.APPROVE,
            DecisionAction.DECLINE,
        )
    ):
        candidate_store, _provider, recovery_id, expiry, _request = _claimed(
            database_path,
            action=action,
            client_decision_id=f"claimed-expiry-cursor-{index}",
        )
        store = candidate_store
        candidates.append((expiry, recovery_id))
    assert store is not None
    candidates.sort()
    corrupt_ids = [candidates[0][1], candidates[1][1]]
    valid_id = candidates[2][1]
    with sqlite3.connect(database_path) as connection:
        for index, corrupt_id in enumerate(corrupt_ids):
            connection.execute(
                """
                UPDATE remedies SET expiry = ?
                WHERE recovery_id = ?
                """,
                (
                    (candidates[0][0] - timedelta(minutes=2 - index)).isoformat(),
                    corrupt_id,
                ),
            )
    monkeypatch.setattr("server.store.MAX_STORE_EXPIRY_BATCH_SIZE", 1)
    sweep_now = max(expiry for expiry, _recovery_id in candidates)

    assert (
        expire_pending_approvals(
            store,
            now=sweep_now,
            batch_size=1,
        )
        == 0
    )
    assert all(
        store.get_recovery(corrupt_id).status is RecoveryStatus.PENDING_APPROVAL
        for corrupt_id in corrupt_ids
    )
    assert store.get_recovery(valid_id).status is RecoveryStatus.PENDING_APPROVAL

    assert (
        expire_pending_approvals(
            store,
            now=sweep_now,
            batch_size=1,
        )
        == 1
    )
    assert all(
        store.get_recovery(corrupt_id).status is RecoveryStatus.PENDING_APPROVAL
        for corrupt_id in corrupt_ids
    )
    assert all(
        store.recovery_has_expiration_evidence(corrupt_id) is False for corrupt_id in corrupt_ids
    )
    assert store.get_recovery(valid_id).status is RecoveryStatus.CLOSED_WITHOUT_ACTION
    assert store.recovery_has_expiration_evidence(valid_id) is True


def test_claimed_expiry_and_pending_status_cas_race_converge_on_one_seal(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "claimed-expiry-cas-race.sqlite3"
    setup_store, _provider, recovery_id, expiry, _request = _claimed(
        database_path,
        action=DecisionAction.APPROVE,
    )
    setup_store.close()
    expiry_store = SQLiteStore(database_path)
    decision_store = SQLiteStore(database_path)
    decision_store._now = lambda: expiry - timedelta(microseconds=1)  # type: ignore[method-assign]
    barrier = Barrier(2)

    def sweep() -> int:
        barrier.wait(timeout=10)
        return expire_pending_approvals(
            expiry_store,
            now=expiry,
            batch_size=1,
            recovery_id=recovery_id,
        )

    def approve_status() -> str:
        barrier.wait(timeout=10)
        try:
            decision_store.update_pending_approval_status(
                recovery_id,
                expected_status="pending",
                status="approved",
            )
        except ApprovalDecisionError as error:
            return error.code
        return "approved"

    with ThreadPoolExecutor(max_workers=2) as executor:
        sweep_future = executor.submit(sweep)
        status_future = executor.submit(approve_status)
        swept = sweep_future.result(timeout=20)
        status_result = status_future.result(timeout=20)

    assert swept == 1
    assert status_result in {"approved", "remedy_expired"}
    assert expiry_store.get_recovery(recovery_id).status is RecoveryStatus.OUTCOME_UNKNOWN
    assert expiry_store.get_receipt(recovery_id).approval_count == 1
    assert expiry_store.count_executions(recovery_id) == 0
    assert expiry_store.recovery_has_expiration_evidence(recovery_id) is True
    assert len([event for event in expiry_store.list_events(recovery_id) if event.terminal]) == 1


def test_claimed_and_untouched_expiry_share_batch_by_oldest_class(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "claimed-first-batch.sqlite3"
    store, _provider, claimed_id, claimed_expiry, _request = _claimed(
        database_path,
        action=DecisionAction.APPROVE,
    )
    untouched = asyncio.run(
        RecoveryOrchestrator(
            store=store,
            hotel_provider=HotelSimulator(store=store),
        ).start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    untouched_approval = untouched.recovery.pending_approval
    assert untouched_approval is not None
    sweep_now = max(claimed_expiry, untouched_approval.expiry)

    assert (
        expire_pending_approvals(
            store,
            now=sweep_now,
            batch_size=1,
        )
        == 1
    )
    assert store.get_recovery(claimed_id).status is RecoveryStatus.OUTCOME_UNKNOWN
    assert (
        store.get_recovery(untouched.recovery.recovery_id).status is RecoveryStatus.PENDING_APPROVAL
    )

    assert (
        expire_pending_approvals(
            store,
            now=sweep_now,
            batch_size=1,
        )
        == 1
    )
    assert (
        store.get_recovery(untouched.recovery.recovery_id).status
        is RecoveryStatus.CLOSED_WITHOUT_ACTION
    )
    assert store.count_decisions(claimed_id) == 1
    assert store.count_decisions(untouched.recovery.recovery_id) == 0


def test_claimed_expiry_terminal_detail_is_removed_by_bounded_retention(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "claimed-expiry-retention.sqlite3"
    store, _provider, recovery_id, expiry, _request = _claimed(
        database_path,
        action=DecisionAction.APPROVE,
    )
    assert (
        expire_pending_approvals(
            store,
            now=expiry,
            batch_size=1,
            recovery_id=recovery_id,
        )
        == 1
    )
    retention_now = expiry + timedelta(days=2)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            UPDATE recoveries SET updated_at = ?
            WHERE id = ?
            """,
            ((retention_now - timedelta(days=2)).isoformat(), recovery_id),
        )
        connection.execute(
            """
            INSERT INTO usage_ledger (
                recovery_id, category, amount, recorded_at
            ) VALUES (?, 'claimed_expiry_fixture', 1, ?)
            """,
            (recovery_id, expiry.isoformat()),
        )

    assert (
        cleanup_terminal_recoveries(
            store,
            terminal_ttl=timedelta(days=1),
            now=retention_now,
            batch_size=1,
        )
        == 1
    )
    with pytest.raises(RecoveryNotFoundError):
        store.get_recovery(recovery_id)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            """
            SELECT category, amount FROM usage_ledger
            WHERE recovery_id = ?
            """,
            (recovery_id,),
        ).fetchall() == [("claimed_expiry_fixture", 1)]


def test_committed_result_and_decision_response_seal_roll_back_together(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "committed-claim-atomicity.sqlite3"
    store, provider, recovery_id, _expiry, request = _claimed(
        database_path,
        action=DecisionAction.APPROVE,
        pending_status="approved",
    )
    orchestrator = RecoveryOrchestrator(store=store, hotel_provider=provider)
    claim = store.load_decision_claim_for_resume(recovery_id)
    execution, dispatched = store.record_completed_execution(
        execution_id="execution-before-atomic-finalization",
        recovery_id=recovery_id,
        idempotency_key=(f"{recovery_id}:{request.tool_call_id}:{request.remedy_digest}"),
        request_digest=_canonical_request_digest(store, recovery_id),
        tool_call_id=request.tool_call_id,
        remedy_digest=request.remedy_digest,
        result_json={
            "dispatch_id": "dispatch-before-atomic-finalization",
            "status": "confirmed",
            "simulated": True,
            "provider_result": "Bound demo-provider result.",
        },
    )
    assert dispatched is True
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER abort_committed_claim_response
            BEFORE UPDATE OF result_json ON approval_decisions
            WHEN NEW.status = 'completed'
            BEGIN
                SELECT RAISE(ABORT, 'injected committed claim response failure');
            END
            """
        )

    with pytest.raises(
        sqlite3.IntegrityError,
        match="injected committed claim response failure",
    ):
        asyncio.run(orchestrator.resume_decision(claim))

    assert store.get_completed_execution(recovery_id) == execution
    assert store.get_recovery(recovery_id).status is RecoveryStatus.PENDING_APPROVAL
    assert store.get_pending_approval(recovery_id).status == "approved"
    assert not any(event.terminal for event in store.list_events(recovery_id))
    with pytest.raises(RecoveryNotFoundError, match="Receipt not found"):
        store.get_receipt(recovery_id)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            """
            SELECT status, result_json, completed_at
            FROM approval_decisions WHERE recovery_id = ?
            """,
            (recovery_id,),
        ).fetchone() == ("claimed", None, None)
        assert connection.execute(
            "SELECT status FROM remedies WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone() == ("pending",)
        connection.execute("DROP TRIGGER abort_committed_claim_response")

    retry_claim = store.load_decision_claim_for_resume(recovery_id)
    replayed = asyncio.run(orchestrator.resume_decision(retry_claim))

    assert replayed.status == "completed"
    assert replayed.approved_remedy_digest == request.remedy_digest
    assert store.get_recovery(recovery_id).status is RecoveryStatus.COMPLETED
    assert store.count_executions(recovery_id) == 1
    assert len([event for event in store.list_events(recovery_id) if event.terminal]) == 1
    assert store.get_receipt(recovery_id).provider_execution is True


def test_legacy_finalizer_replay_preserves_atomic_decision_evidence(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "atomic-then-legacy-finalizer.sqlite3"
    store, provider, recovery_id, _expiry, request = _claimed(
        database_path,
        action=DecisionAction.APPROVE,
        pending_status="approved",
    )
    orchestrator = RecoveryOrchestrator(store=store, hotel_provider=provider)
    claim = store.load_decision_claim_for_resume(recovery_id)
    execution, dispatched = store.record_completed_execution(
        execution_id="execution-before-atomic-seal",
        recovery_id=recovery_id,
        idempotency_key=(f"{recovery_id}:{request.tool_call_id}:{request.remedy_digest}"),
        request_digest=_canonical_request_digest(store, recovery_id),
        tool_call_id=request.tool_call_id,
        remedy_digest=request.remedy_digest,
        result_json={
            "dispatch_id": "dispatch-before-atomic-seal",
            "status": "confirmed",
            "simulated": True,
            "provider_result": "Bound demo-provider result.",
        },
    )
    assert dispatched is True

    completed = asyncio.run(orchestrator.resume_decision(claim))
    receipt = store.get_receipt(recovery_id)
    snapshot_before = store.get_recovery(recovery_id)
    replay_before = store.load_decision_claim_for_resume(recovery_id)

    assert completed.status == "completed"
    assert replay_before.response == completed
    assert store.finalize_completed_execution(execution, receipt=receipt) is False

    snapshot_after = store.get_recovery(recovery_id)
    replay_after = store.load_decision_claim_for_resume(recovery_id)
    assert snapshot_after == snapshot_before
    assert replay_after.response == completed
    assert store.get_receipt(recovery_id) == receipt
    assert len([event for event in store.list_events(recovery_id) if event.terminal]) == 1
    assert provider.dispatch_count == 0
