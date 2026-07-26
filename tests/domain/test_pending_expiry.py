from __future__ import annotations

import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest

from server.cleanup import MAX_CLEANUP_BATCH_SIZE, expire_pending_approvals
from server.models import ApprovalDecisionRequest, ExecutionMode, RecoveryStatus
from server.orchestrator import RecoveryOrchestrator
from server.providers.hotel_simulator import HotelSimulator
from server.store import ApprovalDecisionError, SQLiteStore

EXPIRATION_SUMMARY = "Consent expired without a decision; no provider dispatch was authorized."
EXPIRATION_VERIFICATION_RESULTS = [
    "Human consent requested.",
    "Consent window expired without an approval decision.",
    "No approval decision claim was recorded.",
    "Execution count is zero.",
    "Provider dispatch did not begin.",
    "Temporary permission revoked.",
    "Expiration receipt sealed.",
]


def _pending_sdk(store: SQLiteStore):
    provider = HotelSimulator(store=store)
    pending = asyncio.run(
        RecoveryOrchestrator(store=store, hotel_provider=provider).start(
            "hotel",
            execution_mode=ExecutionMode.SDK_STUB,
        )
    )
    approval = pending.recovery.pending_approval
    assert approval is not None
    return pending.recovery, approval, provider


def _as_live_fixture(
    database_path,
    *,
    recovery_id: str,
    expiry: datetime,
) -> None:
    admitted_at = expiry - timedelta(minutes=5)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            UPDATE recoveries
            SET execution_mode = 'openai_live',
                model_ids_json = ?, root_trace_id = ?, model_call = 1,
                agent_graph_version = ?
            WHERE id = ?
            """,
            (
                '["gpt-5.6-luna","gpt-5.6-terra"]',
                "trace_0123456789abcdef0123456789abcdef",
                "backchannel.hotel-agent.live.v1",
                recovery_id,
            ),
        )
        connection.execute(
            """
            UPDATE pending_approvals
            SET execution_mode = 'openai_live', model_ids_json = ?,
                root_trace_id = ?, agent_graph_version = ?
            WHERE recovery_id = ?
            """,
            (
                '["gpt-5.6-luna","gpt-5.6-terra"]',
                "trace_0123456789abcdef0123456789abcdef",
                "backchannel.hotel-agent.live.v1",
                recovery_id,
            ),
        )
        connection.execute(
            """
            UPDATE events
            SET data_json = ?
            WHERE recovery_id = ? AND seq = 1
            """,
            (
                json.dumps(
                    {
                        "scenarioId": "hotel",
                        "executionMode": "openai_live",
                        "summary": "Recovery created for the selected execution mode.",
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                recovery_id,
            ),
        )
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


@pytest.mark.parametrize(
    "execution_mode",
    [ExecutionMode.SDK_STUB, ExecutionMode.OPENAI_LIVE],
)
def test_expiry_sweep_seals_truthful_zero_dispatch_evidence_and_releases_live(
    tmp_path,
    execution_mode: ExecutionMode,
) -> None:
    database_path = tmp_path / f"expiry-{execution_mode.value}.sqlite3"
    store = SQLiteStore(database_path)
    snapshot, approval, provider = _pending_sdk(store)
    recovery_id = snapshot.recovery_id
    if execution_mode is ExecutionMode.OPENAI_LIVE:
        _as_live_fixture(
            database_path,
            recovery_id=recovery_id,
            expiry=approval.expiry,
        )

    assert (
        expire_pending_approvals(
            store,
            now=approval.expiry,
            batch_size=10,
        )
        == 1
    )

    expired = store.get_recovery(recovery_id)
    receipt = store.get_receipt(recovery_id)
    events = store.list_events(recovery_id)
    terminal_events = [event for event in events if event.terminal]
    assert expired.status is RecoveryStatus.CLOSED_WITHOUT_ACTION
    assert expired.current_step == 5
    assert expired.current_step_summary == EXPIRATION_SUMMARY
    assert expired.pending_approval is None
    assert receipt.execution_mode is execution_mode
    assert receipt.status == "closed_without_action"
    assert receipt.simulated is True
    assert receipt.provider_execution is False
    assert receipt.approved_remedy_digest is None
    assert receipt.approval_count == 0
    assert receipt.provider_result == "Provider dispatch did not begin."
    assert receipt.authorization_source == "Consent window expired before an approval decision."
    assert receipt.verification_results == EXPIRATION_VERIFICATION_RESULTS
    assert len(terminal_events) == 1
    assert terminal_events[0].type == "recovery.expired"
    assert terminal_events[0].data == {
        "approvalDecisionCount": 0,
        "executionCount": 0,
        "executionMode": execution_mode.value,
        "phase": "Verify & seal",
        "providerExecution": False,
        "recoveryId": recovery_id,
        "summary": EXPIRATION_SUMMARY,
    }
    assert store.count_decisions(recovery_id) == 0
    assert store.count_executions(recovery_id) == 0
    assert provider.dispatch_count == 0
    assert store.recovery_has_expiration_evidence(recovery_id) is True

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT status FROM pending_approvals WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone() == ("expired",)
        assert connection.execute(
            "SELECT status FROM remedies WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone() == ("expired",)
        admission = connection.execute(
            "SELECT released_at FROM live_admissions WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone()
        if execution_mode is ExecutionMode.OPENAI_LIVE:
            assert admission is not None and admission[0] is not None
            assert connection.execute(
                "SELECT category, amount FROM usage_ledger WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchall() == [("live_demo_budget_unit", 1)]
        else:
            assert admission is None


def test_expiry_sweep_is_restart_idempotent_and_does_not_rewrite_evidence(
    tmp_path,
) -> None:
    database_path = tmp_path / "expiry-restart.sqlite3"
    store = SQLiteStore(database_path)
    snapshot, approval, _provider = _pending_sdk(store)
    recovery_id = snapshot.recovery_id

    assert expire_pending_approvals(store, now=approval.expiry, batch_size=1) == 1
    first_receipt = store.get_receipt(recovery_id)
    first_events = store.list_events(recovery_id)
    with sqlite3.connect(database_path) as connection:
        first_rows = connection.execute(
            """
            SELECT recoveries.status, recoveries.updated_at,
                   pending_approvals.status, pending_approvals.updated_at,
                   remedies.status, receipts.receipt_json
            FROM recoveries
            JOIN pending_approvals ON pending_approvals.recovery_id = recoveries.id
            JOIN remedies ON remedies.recovery_id = recoveries.id
            JOIN receipts ON receipts.recovery_id = recoveries.id
            WHERE recoveries.id = ?
            """,
            (recovery_id,),
        ).fetchone()
    store.close()

    restarted = SQLiteStore(database_path)
    assert (
        expire_pending_approvals(
            restarted,
            now=approval.expiry + timedelta(days=1),
            batch_size=1,
        )
        == 0
    )
    assert restarted.get_receipt(recovery_id) == first_receipt
    assert restarted.list_events(recovery_id) == first_events
    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute(
                """
            SELECT recoveries.status, recoveries.updated_at,
                   pending_approvals.status, pending_approvals.updated_at,
                   remedies.status, receipts.receipt_json
            FROM recoveries
            JOIN pending_approvals ON pending_approvals.recovery_id = recoveries.id
            JOIN remedies ON remedies.recovery_id = recoveries.id
            JOIN receipts ON receipts.recovery_id = recoveries.id
            WHERE recoveries.id = ?
            """,
                (recovery_id,),
            ).fetchone()
            == first_rows
        )


def test_corrupt_oldest_untouched_approval_does_not_starve_valid_expiry(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "expiry-invalid-oldest.sqlite3"
    store = SQLiteStore(database_path)
    candidates = []
    for _index in range(3):
        snapshot, approval, _provider = _pending_sdk(store)
        candidates.append((approval.expiry, snapshot.recovery_id))
    candidates = sorted(
        candidates,
    )
    corrupt_ids = [candidates[0][1], candidates[1][1]]
    valid_id = candidates[2][1]
    with sqlite3.connect(database_path) as connection:
        for corrupt_id in corrupt_ids:
            connection.execute(
                """
            UPDATE events
            SET type = 'provider.executed'
            WHERE recovery_id = ? AND seq = 2
            """,
                (corrupt_id,),
            )
    monkeypatch.setattr("server.store.MAX_STORE_EXPIRY_BATCH_SIZE", 1)
    sweep_now = max(candidate[0] for candidate in candidates)

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
    assert store.get_recovery(valid_id).status is RecoveryStatus.CLOSED_WITHOUT_ACTION


def test_claim_stays_resumable_before_expiry_then_seals_at_expiry(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "claimed-wins.sqlite3")
    snapshot, approval, _provider = _pending_sdk(store)
    recovery_id = snapshot.recovery_id
    claim = store.claim_approval_decision(
        recovery_id,
        ApprovalDecisionRequest(
            action="approve",
            clientDecisionId="claim-before-expiry",
            remedyId=approval.remedy_id,
            remedyDigest=approval.remedy_digest,
            toolCallId=approval.tool_call_id,
        ),
    )

    assert claim.resume_required is True
    assert (
        expire_pending_approvals(
            store,
            now=approval.expiry - timedelta(microseconds=1),
            batch_size=1,
        )
        == 0
    )
    assert store.get_recovery(recovery_id).status is RecoveryStatus.PENDING_APPROVAL
    assert expire_pending_approvals(store, now=approval.expiry, batch_size=1) == 1
    assert store.count_decisions(recovery_id) == 1
    assert store.count_executions(recovery_id) == 0
    assert store.get_recovery(recovery_id).status is RecoveryStatus.OUTCOME_UNKNOWN
    assert store.get_receipt(recovery_id).approval_count == 1
    assert store.recovery_has_expiration_evidence(recovery_id) is True


def test_approval_claim_and_expiry_writer_race_has_exactly_one_winner(tmp_path) -> None:
    database_path = tmp_path / "expiry-claim-race.sqlite3"
    setup_store = SQLiteStore(database_path)
    snapshot, approval, _provider = _pending_sdk(setup_store)
    recovery_id = snapshot.recovery_id
    setup_store.close()
    expiry_store = SQLiteStore(database_path)
    decision_store = SQLiteStore(database_path)
    barrier = Barrier(2)
    request = ApprovalDecisionRequest(
        action="approve",
        clientDecisionId="claim-expiry-race",
        remedyId=approval.remedy_id,
        remedyDigest=approval.remedy_digest,
        toolCallId=approval.tool_call_id,
    )

    def sweep() -> int:
        barrier.wait(timeout=10)
        return expire_pending_approvals(
            expiry_store,
            now=approval.expiry,
            batch_size=1,
        )

    def claim() -> str:
        barrier.wait(timeout=10)
        try:
            decision_store.claim_approval_decision(recovery_id, request)
        except ApprovalDecisionError as error:
            return error.code
        return "claimed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        sweep_future = executor.submit(sweep)
        claim_future = executor.submit(claim)
        swept = sweep_future.result(timeout=20)
        claim_result = claim_future.result(timeout=20)

    assert swept == 1
    if claim_result == "decision_unavailable":
        assert claim_result == "decision_unavailable"
        assert expiry_store.count_decisions(recovery_id) == 0
        assert expiry_store.get_recovery(recovery_id).status is RecoveryStatus.CLOSED_WITHOUT_ACTION
    else:
        assert claim_result == "claimed"
        assert expiry_store.count_decisions(recovery_id) == 1
        assert expiry_store.get_recovery(recovery_id).status is RecoveryStatus.OUTCOME_UNKNOWN
    assert expiry_store.count_executions(recovery_id) == 0
    assert expiry_store.recovery_has_expiration_evidence(recovery_id) is True


@pytest.mark.parametrize("batch_size", [0, MAX_CLEANUP_BATCH_SIZE + 1])
def test_expiry_sweep_rejects_unbounded_batch_sizes(tmp_path, batch_size: int) -> None:
    store = SQLiteStore(tmp_path / f"expiry-bounds-{batch_size}.sqlite3")
    with pytest.raises(ValueError, match="batch_size"):
        expire_pending_approvals(store, batch_size=batch_size)


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 7, 19, 12, 0),
        datetime(2026, 7, 19, 12, 0, tzinfo=timezone(timedelta(hours=1))),
    ],
)
def test_expiry_sweep_requires_timezone_aware_utc_now(tmp_path, now: datetime) -> None:
    store = SQLiteStore(tmp_path / f"expiry-now-{now.hour}.sqlite3")
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        expire_pending_approvals(store, now=now, batch_size=1)
