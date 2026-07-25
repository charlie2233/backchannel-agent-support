from __future__ import annotations

import asyncio
import sqlite3

import pytest

from server.models import ApprovalDecisionRequest, ExecutionMode, RecoveryStatus
from server.orchestrator import RecoveryOrchestrator
from server.providers.hotel_simulator import HotelSimulator
from server.store import ApprovalDecisionError, RecoveryNotFoundError, SQLiteStore


def test_decline_terminal_evidence_rolls_back_as_one_transaction(tmp_path) -> None:
    database_path = tmp_path / "decline-atomicity.sqlite3"
    store = SQLiteStore(database_path)
    provider = HotelSimulator(store=store)
    orchestrator = RecoveryOrchestrator(store=store, hotel_provider=provider)
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    approval = pending.recovery.pending_approval
    assert approval is not None
    request = ApprovalDecisionRequest(
        action="decline",
        clientDecisionId="atomic-decline",
        remedyId=approval.remedy_id,
        remedyDigest=approval.remedy_digest,
        toolCallId=approval.tool_call_id,
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER abort_decline_seal
            BEFORE UPDATE OF result_json ON approval_decisions
            BEGIN
                SELECT RAISE(ABORT, 'simulated decline seal failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="simulated decline seal failure"):
        asyncio.run(
            orchestrator.approve_decision(pending.recovery.recovery_id, request)
        )

    recovery_id = pending.recovery.recovery_id
    assert store.get_recovery(recovery_id).status is RecoveryStatus.PENDING_APPROVAL
    assert store.count_decisions(recovery_id) == 1
    assert store.count_executions(recovery_id) == 0
    assert provider.dispatch_count == 0
    assert not any(event.terminal for event in store.list_events(recovery_id))
    with pytest.raises(RecoveryNotFoundError, match="Receipt not found"):
        store.get_receipt(recovery_id)
    assert store.get_pending_approval(recovery_id).status == "pending"

    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TRIGGER abort_decline_seal")

    replayed = asyncio.run(orchestrator.approve_decision(recovery_id, request))

    assert replayed.status == "closed_without_action"
    assert store.get_recovery(recovery_id).status is RecoveryStatus.CLOSED_WITHOUT_ACTION
    assert store.count_executions(recovery_id) == 0
    assert len([event for event in store.list_events(recovery_id) if event.terminal]) == 1
    assert store.get_receipt(recovery_id).provider_execution is False


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE approval_decisions SET status = 'completed' WHERE recovery_id = ?",
        "UPDATE approval_decisions SET result_json = '{}' WHERE recovery_id = ?",
        (
            "UPDATE approval_decisions "
            "SET completed_at = '2026-07-25T00:00:00+00:00' "
            "WHERE recovery_id = ?"
        ),
    ],
)
def test_decline_seal_rejects_raw_decision_tuple_tamper(
    tmp_path,
    mutation: str,
) -> None:
    database_path = tmp_path / "decline-raw-tuple-tamper.sqlite3"
    store = SQLiteStore(database_path)
    provider = HotelSimulator(store=store)
    orchestrator = RecoveryOrchestrator(store=store, hotel_provider=provider)
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    approval = pending.recovery.pending_approval
    assert approval is not None
    recovery_id = pending.recovery.recovery_id
    request = ApprovalDecisionRequest(
        action="decline",
        clientDecisionId="tampered-decline",
        remedyId=approval.remedy_id,
        remedyDigest=approval.remedy_digest,
        toolCallId=approval.tool_call_id,
    )
    claim = store.claim_approval_decision(recovery_id, request)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(mutation, (recovery_id,))
        connection.execute("PRAGMA ignore_check_constraints = OFF")

    with pytest.raises(ApprovalDecisionError) as incompatible:
        store.complete_decline_decision(claim)

    assert incompatible.value.code == "resume_incompatible"
    assert store.get_recovery(recovery_id).status is RecoveryStatus.PENDING_APPROVAL
    assert store.count_decisions(recovery_id) == 1
    assert store.count_executions(recovery_id) == 0
    assert provider.dispatch_count == 0
    assert not any(event.terminal for event in store.list_events(recovery_id))
    with pytest.raises(RecoveryNotFoundError, match="Receipt not found"):
        store.get_receipt(recovery_id)
