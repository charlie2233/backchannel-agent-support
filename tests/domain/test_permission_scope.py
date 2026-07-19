from __future__ import annotations

import asyncio
import sqlite3

import pytest
from agents.exceptions import UserError

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
            action_digest=store.get_pending_approval(recovery_id).action_digest,
        )

    assert store.count_executions(recovery_id) == 0


def test_provider_guard_receives_the_matching_action_digest(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "provider-matching-action-digest.sqlite3"
    store = SQLiteStore(database_path)
    provider = HotelSimulator(store=store)
    orchestrator = RecoveryOrchestrator(store=store, hotel_provider=provider)
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    recovery_id = pending.recovery.recovery_id
    approval = pending.recovery.pending_approval
    assert approval is not None
    expected_action_digest = store.get_pending_approval(recovery_id).action_digest
    request = ApprovalDecisionRequest(
        decision="approve",
        clientDecisionId=f"matching-action-{recovery_id}",
        remedyId=approval.remedy_id,
        remedyDigest=approval.remedy_digest,
        toolCallId=approval.tool_call_id,
    )
    observed_action_digests: list[str | None] = []
    original_guard = store.assert_provider_dispatch_authorized

    def capture_action_digest(**kwargs) -> None:
        observed_action_digests.append(kwargs.get("action_digest"))
        original_guard(**kwargs)

    monkeypatch.setattr(
        store,
        "assert_provider_dispatch_authorized",
        capture_action_digest,
    )

    response = asyncio.run(orchestrator.approve_decision(recovery_id, request))

    assert response.status == "completed"
    assert observed_action_digests == [expected_action_digest]
    assert provider.dispatch_count == 1


@pytest.mark.parametrize("tampered_table", ["pending_approvals", "permission_scopes"])
def test_provider_guard_rejects_action_digest_tampered_after_claim_validation(
    tmp_path,
    monkeypatch,
    tampered_table: str,
) -> None:
    database_path = tmp_path / f"provider-tampered-{tampered_table}.sqlite3"
    store = SQLiteStore(database_path)
    provider = HotelSimulator(store=store)
    orchestrator = RecoveryOrchestrator(store=store, hotel_provider=provider)
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    recovery_id = pending.recovery.recovery_id
    approval = pending.recovery.pending_approval
    assert approval is not None
    request = ApprovalDecisionRequest(
        decision="approve",
        clientDecisionId=f"tampered-{tampered_table}-{recovery_id}",
        remedyId=approval.remedy_id,
        remedyDigest=approval.remedy_digest,
        toolCallId=approval.tool_call_id,
    )
    original_validate = store.validate_claimed_decision
    tampered = False

    def validate_then_tamper(claim):
        nonlocal tampered
        validated = original_validate(claim)
        if not tampered:
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    f"UPDATE {tampered_table} SET action_digest = ? "
                    "WHERE recovery_id = ?",
                    ("0" * 64, recovery_id),
                )
            tampered = True
        return validated

    monkeypatch.setattr(store, "validate_claimed_decision", validate_then_tamper)

    with pytest.raises(UserError, match="decision_unavailable"):
        asyncio.run(orchestrator.approve_decision(recovery_id, request))

    assert provider.dispatch_count == 0
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
