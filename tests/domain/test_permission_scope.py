from __future__ import annotations

import asyncio
import sqlite3

import pytest

from server.models import ApprovalDecisionRequest, ExecutionMode, RecoveryReceipt
from server.orchestrator import RecoveryOrchestrator
from server.providers.hotel_simulator import HotelSimulator
from server.store import ApprovalDecisionError, RecoveryNotFoundError, SQLiteStore


def _start_and_claim_approval(store: SQLiteStore):
    provider = HotelSimulator(store=store)
    orchestrator = RecoveryOrchestrator(store=store, hotel_provider=provider)
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    approval = pending.recovery.pending_approval
    assert approval is not None
    request = ApprovalDecisionRequest(
        decision="approve",
        clientDecisionId=f"permission-{pending.recovery.recovery_id}",
        remedyId=approval.remedy_id,
        remedyDigest=approval.remedy_digest,
        toolCallId=approval.tool_call_id,
    )
    store.claim_decision(pending.recovery.recovery_id, request)
    return pending.recovery.recovery_id, request


def test_provider_guard_requires_the_exact_active_permission_scope(tmp_path) -> None:
    database_path = tmp_path / "provider-active-scope.sqlite3"
    store = SQLiteStore(database_path)
    recovery_id, request = _start_and_claim_approval(store)
    assert store.get_permission_scope(recovery_id).status == "active"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE permission_scopes SET status = 'revoked', revoked_at = updated_at "
            "WHERE recovery_id = ?",
            (recovery_id,),
        )

    with pytest.raises(ApprovalDecisionError, match="decision_unavailable"):
        store.assert_provider_dispatch_authorized(
            recovery_id=recovery_id,
            tool_call_id=request.tool_call_id,
            remedy_digest=request.remedy_digest,
        )

    assert store.count_executions(recovery_id) == 0


def test_approved_receipt_and_scope_revocation_commit_atomically(tmp_path) -> None:
    database_path = tmp_path / "approved-scope-atomic.sqlite3"
    store = SQLiteStore(database_path)
    recovery_id, request = _start_and_claim_approval(store)
    provider_result = "Durable provider evidence for scope atomicity."
    execution, _dispatched = store.record_completed_execution(
        execution_id="execution-scope-atomic",
        recovery_id=recovery_id,
        idempotency_key="idempotency-scope-atomic",
        request_digest="request-scope-atomic",
        tool_call_id=request.tool_call_id,
        remedy_digest=request.remedy_digest,
        result_json={
            "dispatch_id": "dispatch-scope-atomic",
            "status": "confirmed",
            "simulated": True,
            "provider_result": provider_result,
        },
    )
    receipt = RecoveryReceipt(
        recoveryId=recovery_id,
        executionMode=ExecutionMode.SDK_STUB,
        status="completed",
        simulated=True,
        providerExecution=True,
        modelIds=[],
        boundary="Deterministic test adapter only.",
        providerResult=provider_result,
        authorizationSource="Exact durable approval decision.",
        verificationResults=[
            "Provider result stored.",
            "Temporary permission revoked after terminal completion.",
        ],
        decision="approved",
        decisionRemedyDigest=request.remedy_digest,
        executionCount=1,
        providerDispatchStarted=True,
        exactInterruptionRejected=False,
        permissionRevoked=True,
        scopeClosed=True,
        approvedRemedyDigest=request.remedy_digest,
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_scope_receipt
            BEFORE INSERT ON receipts
            BEGIN
                SELECT RAISE(ABORT, 'receipt write blocked');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="receipt write blocked"):
        store.finalize_completed_execution(execution, receipt=receipt)

    assert store.get_permission_scope(recovery_id).status == "active"
    with pytest.raises(RecoveryNotFoundError):
        store.get_receipt(recovery_id)
    assert not any(event.terminal for event in store.list_events(recovery_id))

    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TRIGGER reject_scope_receipt")
    assert store.finalize_completed_execution(execution, receipt=receipt) is True
    scope = store.get_permission_scope(recovery_id)
    assert scope.status == "revoked"
    assert scope.revoked_at is not None
    sealed = store.get_receipt(recovery_id)
    assert sealed.permission_revoked is True
    assert sealed.scope_closed is True
    assert sealed.decision == "approved"
