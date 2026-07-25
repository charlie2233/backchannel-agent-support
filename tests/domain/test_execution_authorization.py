from __future__ import annotations

import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import Any
from uuid import uuid4

import pytest

from server.models import (
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    DecisionAction,
    ExecutionMode,
    RecoveryStatus,
)
from server.orchestrator import RecoveryOrchestrator
from server.providers.hotel_simulator import HotelSimulator
from server.store import (
    ApprovalDecisionClaim,
    ApprovalDecisionError,
    ExecutionConflictError,
    SQLiteStore,
)


class BeginAdvancingClockStore(SQLiteStore):
    """Move the test clock when the execution transaction acquires its write lock."""

    def __init__(self, database_path: str | Path) -> None:
        self.current_time = datetime.now(UTC)
        self.advance_on_begin_to: datetime | None = None
        super().__init__(database_path)

    def _now(self) -> datetime:
        return self.current_time

    def _connect(self) -> sqlite3.Connection:
        connection = super()._connect()
        if self.advance_on_begin_to is not None:

            def advance_clock(statement: str) -> None:
                if statement.strip().upper() == "BEGIN IMMEDIATE":
                    assert self.advance_on_begin_to is not None
                    self.current_time = self.advance_on_begin_to
                    self.advance_on_begin_to = None

            connection.set_trace_callback(advance_clock)
        return connection


@dataclass(frozen=True)
class ApprovedExecutionFixture:
    database_path: Path
    store: SQLiteStore
    recovery_id: str
    claim: ApprovalDecisionClaim
    expiry: datetime
    execution: dict[str, Any]


def _approved_execution(
    database_path: Path,
    *,
    store: SQLiteStore | None = None,
    mark_approved: bool = True,
) -> ApprovedExecutionFixture:
    active_store = store or SQLiteStore(database_path)
    orchestrator = RecoveryOrchestrator(
        store=active_store,
        hotel_provider=HotelSimulator(store=active_store),
    )
    recovery_id = str(uuid4())
    pending = asyncio.run(
        orchestrator.start(
            "hotel",
            execution_mode=ExecutionMode.SDK_STUB,
            recovery_id=recovery_id,
        )
    )
    approval = pending.recovery.pending_approval
    assert approval is not None
    claim = active_store.claim_approval_decision(
        recovery_id,
        ApprovalDecisionRequest(
            action=DecisionAction.APPROVE,
            clientDecisionId=f"decision-{recovery_id}",
            remedyId=approval.remedy_id,
            remedyDigest=approval.remedy_digest,
            toolCallId=approval.tool_call_id,
        ),
    )
    if mark_approved:
        active_store.update_pending_approval_status(
            recovery_id,
            status="approved",
        )
    return ApprovedExecutionFixture(
        database_path=database_path,
        store=active_store,
        recovery_id=recovery_id,
        claim=claim,
        expiry=active_store.get_remedy_consent(recovery_id).expiry,
        execution={
            "execution_id": f"execution-{recovery_id}",
            "recovery_id": recovery_id,
            "idempotency_key": f"idempotency-{recovery_id}",
            "request_digest": "d" * 64,
            "tool_call_id": approval.tool_call_id,
            "remedy_digest": approval.remedy_digest,
            "result_json": {
                "dispatch_id": f"dispatch-{recovery_id}",
                "status": "confirmed",
                "simulated": True,
                "provider_result": "Bound demo-provider result.",
            },
        },
    )


def _record(fixture: ApprovedExecutionFixture):
    return fixture.store.record_completed_execution(**fixture.execution)


def _insert_execution(
    fixture: ApprovedExecutionFixture,
    *,
    status: str,
    provider_execution: int,
    has_result: bool,
    execution_id: str | None = None,
    idempotency_key: str | None = None,
) -> None:
    timestamp = datetime.now(UTC).isoformat()
    serialized_result = (
        json.dumps(
            fixture.execution["result_json"],
            separators=(",", ":"),
            sort_keys=True,
        )
        if has_result
        else None
    )
    with sqlite3.connect(fixture.database_path) as connection:
        connection.execute(
            """
            INSERT INTO executions (
                id, recovery_id, idempotency_key, status,
                provider_execution, request_digest, tool_call_id,
                remedy_digest, result_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                execution_id or fixture.execution["execution_id"],
                fixture.recovery_id,
                idempotency_key or fixture.execution["idempotency_key"],
                status,
                provider_execution,
                fixture.execution["request_digest"],
                fixture.execution["tool_call_id"],
                fixture.execution["remedy_digest"],
                serialized_result,
                timestamp,
                timestamp,
            ),
        )


def _execution_row(
    fixture: ApprovedExecutionFixture,
    *,
    idempotency_key: str | None = None,
) -> tuple[object, ...] | None:
    with sqlite3.connect(fixture.database_path) as connection:
        return connection.execute(
            """
            SELECT id, recovery_id, idempotency_key, status,
                   provider_execution, request_digest, tool_call_id,
                   remedy_digest, result_json, created_at, updated_at
            FROM executions
            WHERE idempotency_key = ?
            """,
            (idempotency_key or fixture.execution["idempotency_key"],),
        ).fetchone()


def _completed_response(
    fixture: ApprovedExecutionFixture,
) -> ApprovalDecisionResponse:
    return ApprovalDecisionResponse(
        action=DecisionAction.APPROVE,
        clientDecisionId=fixture.claim.request.client_decision_id,
        recoveryId=fixture.recovery_id,
        status="completed",
        approvedRemedyDigest=fixture.claim.request.remedy_digest,
        executionStarted=True,
    )


def test_execution_samples_expiry_after_acquiring_the_write_transaction(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "execution-expiry-fence.sqlite3"
    store = BeginAdvancingClockStore(database_path)
    fixture = _approved_execution(database_path, store=store)
    store.current_time = fixture.expiry - timedelta(microseconds=1)

    store.assert_provider_dispatch_authorized(
        recovery_id=fixture.recovery_id,
        tool_call_id=fixture.claim.request.tool_call_id,
        remedy_digest=fixture.claim.request.remedy_digest,
    )
    store.advance_on_begin_to = fixture.expiry

    with pytest.raises(ApprovalDecisionError) as rejected:
        _record(fixture)

    assert rejected.value.code == "remedy_expired"
    assert store.count_executions(fixture.recovery_id) == 0


def test_new_execution_requires_the_sdk_approved_pending_marker(
    tmp_path: Path,
) -> None:
    fixture = _approved_execution(
        tmp_path / "execution-pending-marker.sqlite3",
        mark_approved=False,
    )

    with pytest.raises(ApprovalDecisionError) as rejected:
        _record(fixture)

    assert rejected.value.code == "decision_unavailable"
    assert fixture.store.count_executions(fixture.recovery_id) == 0


def test_new_execution_rejects_a_completed_decision_without_an_execution(
    tmp_path: Path,
) -> None:
    fixture = _approved_execution(tmp_path / "execution-completed-decision.sqlite3")
    fixture.store.complete_approval_decision(
        fixture.claim,
        _completed_response(fixture),
    )

    with pytest.raises(ApprovalDecisionError) as rejected:
        _record(fixture)

    assert rejected.value.code == "decision_unavailable"
    assert fixture.store.count_executions(fixture.recovery_id) == 0


def test_fingerprint_valid_decline_cannot_create_an_execution(
    tmp_path: Path,
) -> None:
    fixture = _approved_execution(tmp_path / "execution-decline.sqlite3")
    decline = fixture.claim.request.model_copy(
        update={"action": DecisionAction.DECLINE}
    )
    fingerprint = fixture.store._decision_fingerprint(
        fixture.recovery_id,
        decline,
    )
    with sqlite3.connect(fixture.database_path) as connection:
        connection.execute(
            """
            UPDATE approval_decisions
            SET action = 'decline', request_fingerprint = ?
            WHERE recovery_id = ?
            """,
            (fingerprint, fixture.recovery_id),
        )

    with pytest.raises(ApprovalDecisionError) as rejected:
        _record(fixture)

    assert rejected.value.code == "decision_id_conflict"
    assert fixture.store.count_executions(fixture.recovery_id) == 0


@pytest.mark.parametrize("argument", ["tool_call_id", "remedy_digest"])
def test_direct_execution_arguments_must_match_the_approved_claim(
    tmp_path: Path,
    argument: str,
) -> None:
    fixture = _approved_execution(tmp_path / f"execution-direct-{argument}.sqlite3")
    changed = {
        **fixture.execution,
        argument: (
            "different-tool-call"
            if argument == "tool_call_id"
            else f"sha256:{'e' * 64}"
        ),
    }

    with pytest.raises(ApprovalDecisionError) as rejected:
        fixture.store.record_completed_execution(**changed)

    assert rejected.value.code == "decision_id_conflict"
    assert fixture.store.count_executions(fixture.recovery_id) == 0


@pytest.mark.parametrize(
    ("tamper", "expected_code"),
    [
        ("decision-fingerprint", "resume_incompatible"),
        ("decision-tool-binding", "decision_id_conflict"),
        ("decision-remedy-binding", "decision_id_conflict"),
        ("pending-action-digest", "resume_incompatible"),
        ("consent", "remedy_digest_mismatch"),
    ],
)
def test_execution_rechecks_every_authorization_binding(
    tmp_path: Path,
    tamper: str,
    expected_code: str,
) -> None:
    fixture = _approved_execution(tmp_path / f"execution-{tamper}.sqlite3")
    fixture.store.assert_provider_dispatch_authorized(
        recovery_id=fixture.recovery_id,
        tool_call_id=fixture.claim.request.tool_call_id,
        remedy_digest=fixture.claim.request.remedy_digest,
    )
    with sqlite3.connect(fixture.database_path) as connection:
        if tamper == "decision-fingerprint":
            connection.execute(
                """
                UPDATE approval_decisions
                SET request_fingerprint = ?
                WHERE recovery_id = ?
                """,
                ("f" * 64, fixture.recovery_id),
            )
        elif tamper in {"decision-tool-binding", "decision-remedy-binding"}:
            changed_request = (
                fixture.claim.request.model_copy(
                    update={"tool_call_id": "changed-tool-call"}
                )
                if tamper == "decision-tool-binding"
                else fixture.claim.request.model_copy(
                    update={"remedy_digest": f"sha256:{'b' * 64}"}
                )
            )
            changed_fingerprint = fixture.store._decision_fingerprint(
                fixture.recovery_id,
                changed_request,
            )
            connection.execute(
                """
                UPDATE approval_decisions
                SET tool_call_id = ?, remedy_digest = ?, request_fingerprint = ?
                WHERE recovery_id = ?
                """,
                (
                    changed_request.tool_call_id,
                    changed_request.remedy_digest,
                    changed_fingerprint,
                    fixture.recovery_id,
                ),
            )
        elif tamper == "pending-action-digest":
            stored_digest = connection.execute(
                """
                SELECT action_digest
                FROM pending_approvals
                WHERE recovery_id = ?
                """,
                (fixture.recovery_id,),
            ).fetchone()
            assert stored_digest is not None
            changed_digest = (
                "1" * 64 if stored_digest[0] == "0" * 64 else "0" * 64
            )
            connection.execute(
                """
                UPDATE pending_approvals
                SET action_digest = ?
                WHERE recovery_id = ?
                """,
                (changed_digest, fixture.recovery_id),
            )
        else:
            assert tamper == "consent"
            connection.execute(
                """
                UPDATE remedies
                SET cost_delta_minor = cost_delta_minor + 1
                WHERE recovery_id = ?
                """,
                (fixture.recovery_id,),
            )

    with pytest.raises(ApprovalDecisionError) as rejected:
        _record(fixture)

    assert rejected.value.code == expected_code
    assert fixture.store.count_executions(fixture.recovery_id) == 0


@pytest.mark.parametrize("terminal_evidence", ["receipt", "event"])
def test_new_execution_rejects_existing_terminal_evidence(
    tmp_path: Path,
    terminal_evidence: str,
) -> None:
    fixture = _approved_execution(
        tmp_path / f"execution-terminal-{terminal_evidence}.sqlite3"
    )
    timestamp = datetime.now(UTC).isoformat()
    with sqlite3.connect(fixture.database_path) as connection:
        if terminal_evidence == "receipt":
            connection.execute(
                """
                INSERT INTO receipts (recovery_id, receipt_json, created_at)
                VALUES (?, '{}', ?)
                """,
                (fixture.recovery_id, timestamp),
            )
        else:
            connection.execute(
                """
                INSERT INTO events (
                    recovery_id, seq, type, terminal, data_json, created_at
                ) VALUES (
                    ?,
                    (SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE recovery_id = ?),
                    'recovery.outcome_unknown',
                    1,
                    '{}',
                    ?
                )
                """,
                (fixture.recovery_id, fixture.recovery_id, timestamp),
            )

    with pytest.raises(ApprovalDecisionError) as rejected:
        _record(fixture)

    assert rejected.value.code == "decision_unavailable"
    assert fixture.store.count_executions(fixture.recovery_id) == 0


def test_different_execution_key_for_one_recovery_is_rejected(
    tmp_path: Path,
) -> None:
    fixture = _approved_execution(tmp_path / "execution-second-key.sqlite3")
    first, first_dispatched = _record(fixture)
    changed = {
        **fixture.execution,
        "execution_id": f"{fixture.execution['execution_id']}-other",
        "idempotency_key": f"{fixture.execution['idempotency_key']}-other",
    }

    with pytest.raises(ExecutionConflictError):
        fixture.store.record_completed_execution(**changed)

    assert first_dispatched is True
    assert fixture.store.count_executions(fixture.recovery_id) == 1
    assert fixture.store.get_completed_execution(fixture.recovery_id) == first


@pytest.mark.parametrize(
    ("status", "provider_execution", "has_result"),
    [
        ("completed", 0, True),
        ("completed", 1, False),
        ("completed", 0, False),
        ("pending", 1, False),
        ("pending", 0, True),
        ("pending", 1, True),
        ("failed", 0, False),
        ("failed", 1, True),
    ],
)
def test_same_key_mixed_execution_states_fail_closed_without_rewrite(
    tmp_path: Path,
    status: str,
    provider_execution: int,
    has_result: bool,
) -> None:
    fixture = _approved_execution(
        tmp_path
        / f"execution-mixed-{status}-{provider_execution}-{int(has_result)}.sqlite3"
    )
    _insert_execution(
        fixture,
        status=status,
        provider_execution=provider_execution,
        has_result=has_result,
    )
    before = _execution_row(fixture)

    with pytest.raises(ExecutionConflictError):
        _record(fixture)

    assert _execution_row(fixture) == before
    assert fixture.store.count_executions(fixture.recovery_id) == 1


def test_two_completed_rows_make_even_exact_replay_ambiguous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _approved_execution(tmp_path / "execution-ambiguous-replay.sqlite3")
    _insert_execution(
        fixture,
        status="completed",
        provider_execution=1,
        has_result=True,
    )
    other_key = f"{fixture.execution['idempotency_key']}-other"
    _insert_execution(
        fixture,
        status="completed",
        provider_execution=1,
        has_result=True,
        execution_id=f"{fixture.execution['execution_id']}-other",
        idempotency_key=other_key,
    )
    fixture.store.complete_approval_decision(
        fixture.claim,
        _completed_response(fixture),
    )
    fixture.store.update_pending_approval_status(
        fixture.recovery_id,
        status="completed",
    )
    monkeypatch.setattr(fixture.store, "_now", lambda: fixture.expiry)
    before = (
        _execution_row(fixture),
        _execution_row(fixture, idempotency_key=other_key),
    )

    with pytest.raises(ExecutionConflictError):
        _record(fixture)

    assert (
        _execution_row(fixture),
        _execution_row(fixture, idempotency_key=other_key),
    ) == before
    assert fixture.store.count_executions(fixture.recovery_id) == 2


def test_same_key_two_store_race_commits_once_and_replays_once(
    tmp_path: Path,
) -> None:
    fixture = _approved_execution(tmp_path / "execution-same-key-race.sqlite3")
    second_store = SQLiteStore(fixture.database_path)
    barrier = Barrier(2)

    def record(store: SQLiteStore):
        barrier.wait(timeout=10)
        return store.record_completed_execution(**fixture.execution)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(record, (fixture.store, second_store)))

    assert sorted(dispatched for _execution, dispatched in results) == [False, True]
    assert results[0][0] == results[1][0]
    assert fixture.store.count_executions(fixture.recovery_id) == 1


def test_different_key_two_store_race_has_one_owner_and_one_conflict(
    tmp_path: Path,
) -> None:
    fixture = _approved_execution(tmp_path / "execution-different-key-race.sqlite3")
    second_store = SQLiteStore(fixture.database_path)
    barrier = Barrier(2)
    second_execution = {
        **fixture.execution,
        "execution_id": f"{fixture.execution['execution_id']}-other",
        "idempotency_key": f"{fixture.execution['idempotency_key']}-other",
    }

    def record(
        store: SQLiteStore,
        execution: dict[str, Any],
    ) -> str:
        barrier.wait(timeout=10)
        try:
            _stored, dispatched = store.record_completed_execution(**execution)
        except ExecutionConflictError:
            return "conflict"
        return "owner" if dispatched else "replay"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                lambda pair: record(*pair),
                (
                    (fixture.store, fixture.execution),
                    (second_store, second_execution),
                ),
            )
        )

    assert sorted(outcomes) == ["conflict", "owner"]
    assert fixture.store.count_executions(fixture.recovery_id) == 1


def test_incomplete_same_key_completes_only_with_current_authorization(
    tmp_path: Path,
) -> None:
    fixture = _approved_execution(tmp_path / "execution-incomplete.sqlite3")
    _insert_execution(
        fixture,
        status="pending",
        provider_execution=0,
        has_result=False,
    )

    execution, dispatched = _record(fixture)

    assert dispatched is True
    assert execution.status == "completed"
    assert execution.provider_execution is True
    assert execution.result_json == fixture.execution["result_json"]
    assert fixture.store.count_executions(fixture.recovery_id) == 1


def test_expired_incomplete_same_key_remains_unfinished(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _approved_execution(tmp_path / "execution-incomplete-expired.sqlite3")
    _insert_execution(
        fixture,
        status="pending",
        provider_execution=0,
        has_result=False,
    )
    monkeypatch.setattr(fixture.store, "_now", lambda: fixture.expiry)

    with pytest.raises(ApprovalDecisionError) as rejected:
        _record(fixture)

    assert rejected.value.code == "remedy_expired"
    with sqlite3.connect(fixture.database_path) as connection:
        row = connection.execute(
            """
            SELECT status, provider_execution, result_json
            FROM executions
            WHERE recovery_id = ?
            """,
            (fixture.recovery_id,),
        ).fetchone()
    assert row == ("pending", 0, None)


@pytest.mark.parametrize("operation", ["insert", "update"])
def test_execution_write_failure_rolls_back_without_rewriting_evidence(
    tmp_path: Path,
    operation: str,
) -> None:
    fixture = _approved_execution(tmp_path / f"execution-{operation}-rollback.sqlite3")
    if operation == "update":
        _insert_execution(
            fixture,
            status="pending",
            provider_execution=0,
            has_result=False,
        )
    with sqlite3.connect(fixture.database_path) as connection:
        connection.execute(
            f"""
            CREATE TRIGGER block_execution_{operation}
            BEFORE {operation.upper()} ON executions
            BEGIN
                SELECT RAISE(ABORT, 'injected execution {operation} failure');
            END
            """
        )
    execution_before = _execution_row(fixture)
    with sqlite3.connect(fixture.database_path) as connection:
        evidence_before = connection.execute(
            """
            SELECT approval_decisions.status,
                   approval_decisions.result_json,
                   approval_decisions.completed_at,
                   pending_approvals.status
            FROM approval_decisions
            JOIN pending_approvals USING (recovery_id)
            WHERE recovery_id = ?
            """,
            (fixture.recovery_id,),
        ).fetchone()

    with pytest.raises(
        sqlite3.IntegrityError,
        match=f"injected execution {operation} failure",
    ):
        _record(fixture)

    with sqlite3.connect(fixture.database_path) as connection:
        evidence_after = connection.execute(
            """
            SELECT approval_decisions.status,
                   approval_decisions.result_json,
                   approval_decisions.completed_at,
                   pending_approvals.status
            FROM approval_decisions
            JOIN pending_approvals USING (recovery_id)
            WHERE recovery_id = ?
            """,
            (fixture.recovery_id,),
        ).fetchone()
    assert _execution_row(fixture) == execution_before
    assert evidence_after == evidence_before


def test_exact_completed_execution_replays_after_terminalization_and_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _approved_execution(tmp_path / "execution-terminal-replay.sqlite3")
    execution, dispatched = _record(fixture)
    fixture.store.finalize_completed_execution(
        execution,
        receipt=fixture.store.completed_receipt_for_execution(execution),
    )
    fixture.store.complete_approval_decision(
        fixture.claim,
        _completed_response(fixture),
    )
    monkeypatch.setattr(fixture.store, "_now", lambda: fixture.expiry)
    changed_result = {
        **fixture.execution,
        "execution_id": f"{fixture.execution['execution_id']}-retry-candidate",
        "result_json": {
            **fixture.execution["result_json"],
            "provider_result": "A retry must not replace the stored result.",
        },
    }

    replayed, replay_dispatched = fixture.store.record_completed_execution(
        **changed_result
    )

    assert dispatched is True
    assert replay_dispatched is False
    assert replayed == execution
    assert fixture.store.get_recovery(fixture.recovery_id).status is RecoveryStatus.COMPLETED
    assert fixture.store.get_pending_approval(fixture.recovery_id).status == "completed"
    assert fixture.store.count_executions(fixture.recovery_id) == 1
