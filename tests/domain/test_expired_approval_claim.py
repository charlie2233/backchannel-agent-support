from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from threading import Event, Thread

import pytest

from server.agents.live_models import ModelResponseMetadata
from server.models import ApprovalDecisionRequest, ExecutionMode, RecoveryStatus
from server.orchestrator import RecoveryOrchestrator
from server.providers.hotel_contract import durable_hotel_dispatch_contract
from server.providers.hotel_simulator import HotelSimulator
from server.store import (
    ApprovalDecisionError,
    ReceiptTransitionError,
    RecoveryNotFoundError,
    SQLiteStore,
)


def _pending_approval(
    store: SQLiteStore,
) -> tuple[
    RecoveryOrchestrator,
    HotelSimulator,
    str,
    ApprovalDecisionRequest,
    object,
]:
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
        clientDecisionId=f"expiring-approval-{recovery_id}",
        remedyId=approval.remedy_id,
        remedyDigest=approval.remedy_digest,
        toolCallId=approval.tool_call_id,
    )
    return orchestrator, provider, recovery_id, request, approval


def _claim_immediately_before_expiry(
    store: SQLiteStore,
    recovery_id: str,
    request: ApprovalDecisionRequest,
    approval,
    monkeypatch,
):
    before_expiry = approval.expiry - timedelta(microseconds=1)
    monkeypatch.setattr(store, "_now", lambda: before_expiry)
    claim = store.claim_decision(recovery_id, request)
    return claim, before_expiry


def _assert_expired_without_dispatch(
    store: SQLiteStore,
    *,
    recovery_id: str,
    request: ApprovalDecisionRequest,
) -> None:
    snapshot = store.get_recovery(recovery_id)
    assert snapshot.status is RecoveryStatus.CLOSED_WITHOUT_ACTION
    assert snapshot.current_step == 5
    assert snapshot.pending_approval is None
    assert snapshot.current_step_summary == (
        "Authorization expired before provider dispatch; closed without action."
    )
    assert store.get_pending_approval(recovery_id).status == "expired"
    assert store.get_permission_scope(recovery_id).status == "revoked"
    assert store.count_executions(recovery_id) == 0

    receipt = store.get_receipt(recovery_id)
    assert receipt.status == "closed_without_action"
    assert receipt.decision == "approved"
    assert receipt.decision_remedy_digest == request.remedy_digest
    assert receipt.approved_remedy_digest is None
    assert receipt.execution_count == 0
    assert receipt.provider_dispatch_started is False
    assert receipt.provider_execution is False
    assert receipt.exact_interruption_rejected is False
    assert receipt.permission_revoked is True
    assert receipt.scope_closed is True
    assert receipt.terminal_reason == "authorization_expired_before_dispatch"

    terminal = [event for event in store.list_events(recovery_id) if event.terminal]
    assert len(terminal) == 1
    assert terminal[0].type == "recovery.closed_without_action"
    assert terminal[0].data["decision"] == "approved"
    assert terminal[0].data["terminalReason"] == (
        "authorization_expired_before_dispatch"
    )

    with sqlite3.connect(store._database_path) as connection:
        connection.row_factory = sqlite3.Row
        decision = connection.execute(
            "SELECT * FROM approval_decisions WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone()
        assert decision is not None
        assert decision["status"] == "completed"
        assert decision["completed_at"] is not None
        assert decision["resume_owner_id"] is None
        assert decision["resume_lease_expires_at"] is None
        assert decision["resume_heartbeat_at"] is None
        assert json.loads(decision["result_json"])["terminalReason"] == (
            "authorization_expired_before_dispatch"
        )
        remedy_status = connection.execute(
            "SELECT status FROM remedies WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone()
        assert remedy_status is not None
        assert remedy_status["status"] == "expired"


def test_claimed_approval_expiring_before_dispatch_replays_truthful_terminal_result(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "expired-claimed-approval.sqlite3")
    orchestrator, provider, recovery_id, request, approval = _pending_approval(store)
    claim, _before_expiry = _claim_immediately_before_expiry(
        store,
        recovery_id,
        request,
        approval,
        monkeypatch,
    )
    monkeypatch.setattr(
        store,
        "_now",
        lambda: approval.expiry + timedelta(microseconds=1),
    )

    response = asyncio.run(
        orchestrator.approve_decision(
            recovery_id,
            request,
            claimed_decision=claim,
        )
    )

    assert response.model_dump(mode="json", by_alias=True) == {
        "clientDecisionId": request.client_decision_id,
        "recoveryId": recovery_id,
        "decision": "approve",
        "status": "closed_without_action",
        "decisionRemedyDigest": request.remedy_digest,
        "executionStarted": False,
        "terminalReason": "authorization_expired_before_dispatch",
    }
    assert provider.dispatch_count == 0
    _assert_expired_without_dispatch(
        store,
        recovery_id=recovery_id,
        request=request,
    )

    replayed = asyncio.run(orchestrator.approve_decision(recovery_id, request))
    assert replayed == response
    assert provider.dispatch_count == 0
    assert len([event for event in store.list_events(recovery_id) if event.terminal]) == 1


def test_startup_schedules_and_terminalizes_claim_at_exact_consent_expiry(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "startup-expired-approval.sqlite3")
    orchestrator, provider, recovery_id, request, approval = _pending_approval(store)
    claim, before_expiry = _claim_immediately_before_expiry(
        store,
        recovery_id,
        request,
        approval,
        monkeypatch,
    )

    assert orchestrator._reconcile_startup_once() == approval.expiry
    with pytest.raises(RecoveryNotFoundError, match="Receipt not found"):
        store.get_receipt(recovery_id)

    monkeypatch.setattr(store, "_now", lambda: approval.expiry)
    assert orchestrator._reconcile_startup_once() is None
    _assert_expired_without_dispatch(
        store,
        recovery_id=recovery_id,
        request=request,
    )
    replayed = asyncio.run(
        orchestrator.approve_decision(
            recovery_id,
            request,
            claimed_decision=claim,
        )
    )
    assert replayed.status == "closed_without_action"
    assert provider.dispatch_count == 0

    monkeypatch.setattr(store, "_now", lambda: before_expiry)
    assert orchestrator._reconcile_startup_once() is None
    assert len([event for event in store.list_events(recovery_id) if event.terminal]) == 1


def test_exact_completed_execution_committed_before_expiry_wins_reconciliation(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "completed-before-expiry.sqlite3")
    orchestrator, provider, recovery_id, request, approval = _pending_approval(store)
    claim, before_expiry = _claim_immediately_before_expiry(
        store,
        recovery_id,
        request,
        approval,
        monkeypatch,
    )
    lease = store.acquire_decision_resume(
        claim,
        resume_owner_id="completed-before-expiry-owner",
        lease_duration=timedelta(minutes=1),
        now=before_expiry,
    )
    contract = durable_hotel_dispatch_contract(
        recovery_id=recovery_id,
        remedy=store.get_remedy_consent(recovery_id).evidence.remedy,
        tool_call_id=request.tool_call_id,
        remedy_digest=request.remedy_digest,
    )
    execution, dispatched = store.record_completed_execution(
        execution_id=contract.execution_id,
        recovery_id=recovery_id,
        idempotency_key=contract.idempotency_key,
        request_digest=contract.request_digest,
        tool_call_id=request.tool_call_id,
        remedy_digest=request.remedy_digest,
        action_digest=store.get_pending_approval(recovery_id).action_digest,
        resume_owner_id="completed-before-expiry-owner",
        resume_generation=lease.resume_generation,
        result_json=contract.result_json,
    )
    assert dispatched is True
    assert execution.status == "completed"
    store.release_decision_resume(
        claim,
        resume_owner_id="completed-before-expiry-owner",
        resume_generation=lease.resume_generation,
    )
    monkeypatch.setattr(
        store,
        "_now",
        lambda: approval.expiry + timedelta(microseconds=1),
    )

    assert orchestrator._reconcile_startup_once() is None

    snapshot = store.get_recovery(recovery_id)
    assert snapshot.status is RecoveryStatus.COMPLETED
    assert snapshot.current_step == 5
    receipt = store.get_receipt(recovery_id)
    assert receipt.status == "completed"
    assert receipt.approved_remedy_digest == request.remedy_digest
    assert receipt.terminal_reason is None
    assert provider.dispatch_count == 0


def test_expired_claim_with_exact_dispatch_start_evidence_becomes_outcome_unknown(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "expired-dispatch-start.sqlite3")
    orchestrator, provider, recovery_id, request, approval = _pending_approval(store)
    claim, before_expiry = _claim_immediately_before_expiry(
        store,
        recovery_id,
        request,
        approval,
        monkeypatch,
    )
    with sqlite3.connect(store._database_path) as connection:
        contract = durable_hotel_dispatch_contract(
            recovery_id=recovery_id,
            remedy=store.get_remedy_consent(recovery_id).evidence.remedy,
            tool_call_id=request.tool_call_id,
            remedy_digest=request.remedy_digest,
        )
        connection.execute(
            """
            INSERT INTO executions (
                id, recovery_id, idempotency_key, status, provider_execution,
                request_digest, tool_call_id, remedy_digest, result_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, 'dispatch_started', 0, ?, ?, ?, NULL, ?, ?)
            """,
            (
                contract.execution_id,
                recovery_id,
                contract.idempotency_key,
                contract.request_digest,
                request.tool_call_id,
                request.remedy_digest,
                before_expiry.isoformat(),
                before_expiry.isoformat(),
            ),
        )
    monkeypatch.setattr(
        store,
        "_now",
        lambda: approval.expiry + timedelta(microseconds=1),
    )

    assert orchestrator._reconcile_startup_once() is None

    response = asyncio.run(
        orchestrator.approve_decision(
            recovery_id,
            request,
            claimed_decision=claim,
        )
    )
    assert response.model_dump(mode="json", by_alias=True) == {
        "clientDecisionId": request.client_decision_id,
        "recoveryId": recovery_id,
        "decision": "approve",
        "status": "outcome_unknown",
        "decisionRemedyDigest": request.remedy_digest,
        "executionStarted": True,
        "terminalReason": "authorization_expired_with_unresolved_dispatch",
    }
    snapshot = store.get_recovery(recovery_id)
    assert snapshot.status is RecoveryStatus.OUTCOME_UNKNOWN
    assert snapshot.current_step == 5
    assert store.get_pending_approval(recovery_id).status == "expired"
    assert store.get_permission_scope(recovery_id).status == "revoked"
    receipt = store.get_receipt(recovery_id)
    assert receipt.status == "outcome_unknown"
    assert receipt.decision == "approved"
    assert receipt.execution_count == 1
    assert receipt.provider_dispatch_started is True
    assert receipt.provider_execution is False
    assert receipt.exact_interruption_rejected is False
    assert receipt.terminal_reason == (
        "authorization_expired_with_unresolved_dispatch"
    )
    assert provider.dispatch_count == 0
    terminal = [event for event in store.list_events(recovery_id) if event.terminal]
    assert len(terminal) == 1
    assert terminal[0].type == "recovery.outcome_unknown"


@pytest.mark.parametrize(
    "forged_field",
    [
        "request_digest",
        "idempotency_key",
        "execution_id",
        "dispatch_id",
        "provider_result",
    ],
)
def test_pre_expiry_reconciliation_rejects_forged_completed_execution_contract(
    tmp_path,
    monkeypatch,
    forged_field: str,
) -> None:
    store = SQLiteStore(tmp_path / "forged-pre-expiry-completed.sqlite3")
    orchestrator, _provider, recovery_id, request, approval = _pending_approval(store)
    _claim, before_expiry = _claim_immediately_before_expiry(
        store,
        recovery_id,
        request,
        approval,
        monkeypatch,
    )
    contract = durable_hotel_dispatch_contract(
        recovery_id=recovery_id,
        remedy=store.get_remedy_consent(recovery_id).evidence.remedy,
        tool_call_id=request.tool_call_id,
        remedy_digest=request.remedy_digest,
    )
    execution_id = contract.execution_id
    idempotency_key = contract.idempotency_key
    request_digest = contract.request_digest
    result_json = dict(contract.result_json)
    if forged_field == "execution_id":
        execution_id = "forged-execution"
    elif forged_field == "idempotency_key":
        idempotency_key = "forged-idempotency-key"
    elif forged_field == "request_digest":
        request_digest = "0" * 64
    elif forged_field == "dispatch_id":
        result_json["dispatch_id"] = "forged-dispatch"
    else:
        result_json["provider_result"] = "Forged result must never be sealed."
    with sqlite3.connect(store._database_path) as connection:
        connection.execute(
            """
            INSERT INTO executions (
                id, recovery_id, idempotency_key, status, provider_execution,
                request_digest, tool_call_id, remedy_digest, result_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, 'completed', 1, ?, ?, ?, ?, ?, ?)
            """,
            (
                execution_id,
                recovery_id,
                idempotency_key,
                request_digest,
                request.tool_call_id,
                request.remedy_digest,
                json.dumps(
                    result_json,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                before_expiry.isoformat(),
                before_expiry.isoformat(),
            ),
        )

    with pytest.raises(ReceiptTransitionError, match="dispatch evidence mismatch"):
        orchestrator._reconcile_startup_once()

    assert store.get_recovery(recovery_id).status is RecoveryStatus.PENDING_APPROVAL
    assert store.get_pending_approval(recovery_id).status == "approved"
    assert store.get_permission_scope(recovery_id).status == "active"
    with pytest.raises(RecoveryNotFoundError, match="Receipt not found"):
        store.get_receipt(recovery_id)
    assert not [event for event in store.list_events(recovery_id) if event.terminal]


@pytest.mark.parametrize(
    ("execution_status", "provider_execution"),
    [
        ("completed", 1),
        ("dispatch_started", 0),
    ],
)
def test_pre_expiry_reconciliation_rejects_execution_created_before_authorization(
    tmp_path,
    monkeypatch,
    execution_status: str,
    provider_execution: int,
) -> None:
    store = SQLiteStore(tmp_path / f"pre-authorization-{execution_status}.sqlite3")
    orchestrator, _provider, recovery_id, request, approval = _pending_approval(store)
    _claim, before_expiry = _claim_immediately_before_expiry(
        store,
        recovery_id,
        request,
        approval,
        monkeypatch,
    )
    contract = durable_hotel_dispatch_contract(
        recovery_id=recovery_id,
        remedy=store.get_remedy_consent(recovery_id).evidence.remedy,
        tool_call_id=request.tool_call_id,
        remedy_digest=request.remedy_digest,
    )
    evidence_at = before_expiry - timedelta(microseconds=1)
    result_json = (
        json.dumps(
            contract.result_json,
            separators=(",", ":"),
            sort_keys=True,
        )
        if execution_status == "completed"
        else None
    )
    with sqlite3.connect(store._database_path) as connection:
        connection.execute(
            """
            INSERT INTO executions (
                id, recovery_id, idempotency_key, status, provider_execution,
                request_digest, tool_call_id, remedy_digest, result_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                contract.execution_id,
                recovery_id,
                contract.idempotency_key,
                execution_status,
                provider_execution,
                contract.request_digest,
                request.tool_call_id,
                request.remedy_digest,
                result_json,
                evidence_at.isoformat(),
                evidence_at.isoformat(),
            ),
        )

    with pytest.raises(ReceiptTransitionError, match="chronology mismatch"):
        orchestrator._reconcile_startup_once()

    assert store.get_recovery(recovery_id).status is RecoveryStatus.PENDING_APPROVAL
    assert store.get_pending_approval(recovery_id).status == "approved"
    assert store.get_permission_scope(recovery_id).status == "active"
    with pytest.raises(RecoveryNotFoundError, match="Receipt not found"):
        store.get_receipt(recovery_id)
    assert not [event for event in store.list_events(recovery_id) if event.terminal]


def test_openai_live_metadata_update_after_claim_preserves_expiry_reconciliation(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "live-metadata-before-expiry.sqlite3")
    orchestrator, provider, recovery_id, request, approval = _pending_approval(store)
    with sqlite3.connect(store._database_path) as connection:
        connection.execute(
            "UPDATE recoveries SET execution_mode = 'openai_live' WHERE id = ?",
            (recovery_id,),
        )
        connection.execute(
            """
            UPDATE pending_approvals
            SET execution_mode = 'openai_live',
                root_trace_id = 'trace_expiry_metadata_test'
            WHERE recovery_id = ?
            """,
            (recovery_id,),
        )
    claimed_at = approval.expiry - timedelta(microseconds=2)
    monkeypatch.setattr(store, "_now", lambda: claimed_at)
    claim = store.claim_decision(recovery_id, request)
    lease = store.acquire_decision_resume(
        claim,
        resume_owner_id="live-metadata-owner",
        lease_duration=timedelta(minutes=1),
        now=claimed_at,
    )
    metadata_at = claimed_at + timedelta(microseconds=1)
    monkeypatch.setattr(store, "_now", lambda: metadata_at)
    store.update_pending_model_metadata(
        recovery_id,
        (
            ModelResponseMetadata(
                requested_model="gpt-5.6-terra",
                returned_model="gpt-5.6-terra-2026-07-15-resume",
                response_id="resp_expiry_resume",
                request_id="req_expiry_resume",
            ),
        ),
        resume_owner_id="live-metadata-owner",
        resume_generation=lease.resume_generation,
    )

    assert orchestrator._reconcile_startup_once() == approval.expiry

    monkeypatch.setattr(store, "_now", lambda: approval.expiry)
    assert orchestrator._reconcile_startup_once() is None
    assert provider.dispatch_count == 0
    _assert_expired_without_dispatch(
        store,
        recovery_id=recovery_id,
        request=request,
    )


def test_expired_claim_rejects_mismatched_dispatch_evidence_without_rewrite(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "expired-mismatched-dispatch.sqlite3")
    orchestrator, _provider, recovery_id, request, approval = _pending_approval(store)
    _claim, before_expiry = _claim_immediately_before_expiry(
        store,
        recovery_id,
        request,
        approval,
        monkeypatch,
    )
    with sqlite3.connect(store._database_path) as connection:
        connection.execute(
            """
            INSERT INTO executions (
                id, recovery_id, idempotency_key, status, provider_execution,
                request_digest, tool_call_id, remedy_digest, result_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, 'dispatch_started', 0, ?, ?, ?, NULL, ?, ?)
            """,
            (
                "mismatched-dispatch",
                recovery_id,
                "mismatched-dispatch",
                "c" * 64,
                request.tool_call_id,
                f"sha256:{'0' * 64}",
                before_expiry.isoformat(),
                before_expiry.isoformat(),
            ),
        )
    monkeypatch.setattr(store, "_now", lambda: approval.expiry)

    with pytest.raises(ReceiptTransitionError, match="dispatch evidence mismatch"):
        orchestrator._reconcile_startup_once()

    assert store.get_recovery(recovery_id).status is RecoveryStatus.PENDING_APPROVAL
    assert store.get_pending_approval(recovery_id).status == "approved"
    assert store.get_permission_scope(recovery_id).status == "active"
    with pytest.raises(RecoveryNotFoundError, match="Receipt not found"):
        store.get_receipt(recovery_id)
    assert not [event for event in store.list_events(recovery_id) if event.terminal]


def test_execution_write_rechecks_expiry_inside_its_atomic_transaction(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "late-execution-write.sqlite3")
    _orchestrator, _provider, recovery_id, request, approval = _pending_approval(store)
    claim, before_expiry = _claim_immediately_before_expiry(
        store,
        recovery_id,
        request,
        approval,
        monkeypatch,
    )
    lease = store.acquire_decision_resume(
        claim,
        resume_owner_id="late-write-owner",
        lease_duration=timedelta(minutes=1),
        now=before_expiry,
    )
    monkeypatch.setattr(store, "_now", lambda: approval.expiry)

    with pytest.raises(ApprovalDecisionError, match="remedy_expired"):
        store.record_completed_execution(
            execution_id="late-execution",
            recovery_id=recovery_id,
            idempotency_key="late-execution",
            request_digest="d" * 64,
            tool_call_id=request.tool_call_id,
            remedy_digest=request.remedy_digest,
            action_digest=store.get_pending_approval(recovery_id).action_digest,
            resume_owner_id="late-write-owner",
            resume_generation=lease.resume_generation,
            result_json={
                "dispatch_id": "late-execution",
                "status": "confirmed",
                "simulated": True,
                "provider_result": "This result must not commit after expiry.",
            },
        )

    assert store.count_executions(recovery_id) == 0
    assert lease.resume_generation == 1


def test_resume_preserves_typed_approval_decision_error(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "typed-decision-error.sqlite3")
    orchestrator, _provider, recovery_id, request, approval = _pending_approval(store)
    claim, before_expiry = _claim_immediately_before_expiry(
        store,
        recovery_id,
        request,
        approval,
        monkeypatch,
    )
    lease = store.acquire_decision_resume(
        claim,
        resume_owner_id="typed-error-owner",
        lease_duration=timedelta(minutes=1),
        now=before_expiry,
    )

    def fail_typed(*_args, **_kwargs):
        raise ApprovalDecisionError(
            "decision_unavailable",
            recovery_id,
            status_code=409,
        )

    monkeypatch.setattr(store, "validate_claimed_decision", fail_typed)

    with pytest.raises(ApprovalDecisionError) as caught:
        asyncio.run(
            orchestrator._resume_claimed_approval(
                claim,
                resume_owner_id="typed-error-owner",
                resume_generation=lease.resume_generation,
            )
        )

    assert caught.value.code == "decision_unavailable"


def test_expired_owner_takeover_fences_stale_generation_before_execution_write(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "expired-approval-stale-owner.sqlite3")
    _orchestrator, _provider, recovery_id, request, approval = _pending_approval(store)
    claimed_at = approval.expiry - timedelta(minutes=1)
    monkeypatch.setattr(store, "_now", lambda: claimed_at)
    claim = store.claim_decision(recovery_id, request)
    first = store.acquire_decision_resume(
        claim,
        resume_owner_id="stale-owner-a",
        lease_duration=timedelta(seconds=10),
        now=claimed_at,
    )
    takeover_at = claimed_at + timedelta(seconds=11)
    second = store.acquire_decision_resume(
        claim,
        resume_owner_id="current-owner-b",
        lease_duration=timedelta(seconds=10),
        now=takeover_at,
    )
    assert second.disposition == "owner"
    assert second.resume_generation == first.resume_generation + 1
    monkeypatch.setattr(store, "_now", lambda: takeover_at)
    action_digest = store.get_pending_approval(recovery_id).action_digest
    result = {
        "dispatch_id": "stale-owner-dispatch",
        "status": "confirmed",
        "simulated": True,
        "provider_result": "Only the current generation may commit this result.",
    }

    with pytest.raises(ApprovalDecisionError) as stale:
        store.record_completed_execution(
            execution_id="stale-owner-execution",
            recovery_id=recovery_id,
            idempotency_key="stale-owner-execution",
            request_digest="e" * 64,
            tool_call_id=request.tool_call_id,
            remedy_digest=request.remedy_digest,
            action_digest=action_digest,
            resume_owner_id="stale-owner-a",
            resume_generation=first.resume_generation,
            result_json=result,
        )

    assert stale.value.code == "resume_owner_lost"
    assert store.count_executions(recovery_id) == 0
    contract = durable_hotel_dispatch_contract(
        recovery_id=recovery_id,
        remedy=store.get_remedy_consent(recovery_id).evidence.remedy,
        tool_call_id=request.tool_call_id,
        remedy_digest=request.remedy_digest,
    )
    execution, dispatched = store.record_completed_execution(
        execution_id=contract.execution_id,
        recovery_id=recovery_id,
        idempotency_key=contract.idempotency_key,
        request_digest=contract.request_digest,
        tool_call_id=request.tool_call_id,
        remedy_digest=request.remedy_digest,
        action_digest=action_digest,
        resume_owner_id="current-owner-b",
        resume_generation=second.resume_generation,
        result_json=contract.result_json,
    )
    assert dispatched is True
    assert execution.status == "completed"


def test_execution_write_samples_expiry_clock_only_after_begin_immediate(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "expired-approval-write-clock.sqlite3")
    _orchestrator, _provider, recovery_id, request, approval = _pending_approval(store)
    claim, before_expiry = _claim_immediately_before_expiry(
        store,
        recovery_id,
        request,
        approval,
        monkeypatch,
    )
    lease = store.acquire_decision_resume(
        claim,
        resume_owner_id="atomic-clock-owner",
        lease_duration=timedelta(minutes=1),
        now=before_expiry,
    )
    action_digest = store.get_pending_approval(recovery_id).action_digest
    begin_attempted = Event()
    allow_begin = Event()
    clock_sampled = Event()
    original_connect = store._connect

    class BlockingConnection:
        def __init__(self, connection) -> None:
            self._connection = connection

        def __enter__(self):
            self._connection.__enter__()
            return self

        def __exit__(self, *args):
            return self._connection.__exit__(*args)

        def execute(self, statement, parameters=()):
            if statement == "BEGIN IMMEDIATE":
                begin_attempted.set()
                assert allow_begin.wait(timeout=2)
            return self._connection.execute(statement, parameters)

    monkeypatch.setattr(
        store,
        "_connect",
        lambda: BlockingConnection(original_connect()),
    )

    def expired_now() -> datetime:
        clock_sampled.set()
        return approval.expiry

    monkeypatch.setattr(store, "_now", expired_now)
    errors: list[BaseException] = []

    def write_execution() -> None:
        try:
            store.record_completed_execution(
                execution_id="atomic-clock-execution",
                recovery_id=recovery_id,
                idempotency_key="atomic-clock-execution",
                request_digest="1" * 64,
                tool_call_id=request.tool_call_id,
                remedy_digest=request.remedy_digest,
                action_digest=action_digest,
                resume_owner_id="atomic-clock-owner",
                resume_generation=lease.resume_generation,
                result_json={
                    "dispatch_id": "atomic-clock-dispatch",
                    "status": "confirmed",
                    "simulated": True,
                    "provider_result": "This result is rejected at exact expiry.",
                },
            )
        except BaseException as error:
            errors.append(error)

    worker = Thread(target=write_execution, name="expired-approval-write-clock")
    worker.start()
    assert begin_attempted.wait(timeout=2)
    assert not clock_sampled.is_set()
    allow_begin.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert clock_sampled.is_set()
    assert len(errors) == 1
    assert isinstance(errors[0], ApprovalDecisionError)
    assert errors[0].code == "remedy_expired"
    assert store.count_executions(recovery_id) == 0


def test_expiry_finalizer_wins_before_a_stale_execution_writer(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "expired-approval-finalizer-wins.sqlite3")
    orchestrator, _provider, recovery_id, request, approval = _pending_approval(store)
    claim, before_expiry = _claim_immediately_before_expiry(
        store,
        recovery_id,
        request,
        approval,
        monkeypatch,
    )
    lease = store.acquire_decision_resume(
        claim,
        resume_owner_id="finalizer-race-owner",
        lease_duration=timedelta(minutes=1),
        now=before_expiry,
    )
    action_digest = store.get_pending_approval(recovery_id).action_digest
    monkeypatch.setattr(store, "_now", lambda: approval.expiry)
    assert orchestrator._reconcile_startup_once() is None

    monkeypatch.setattr(store, "_now", lambda: before_expiry)
    with pytest.raises(ApprovalDecisionError) as rejected:
        store.record_completed_execution(
            execution_id="post-finalizer-execution",
            recovery_id=recovery_id,
            idempotency_key="post-finalizer-execution",
            request_digest="2" * 64,
            tool_call_id=request.tool_call_id,
            remedy_digest=request.remedy_digest,
            action_digest=action_digest,
            resume_owner_id="finalizer-race-owner",
            resume_generation=lease.resume_generation,
            result_json={
                "dispatch_id": "post-finalizer-dispatch",
                "status": "confirmed",
                "simulated": True,
                "provider_result": "This result cannot overwrite the expiry seal.",
            },
        )

    assert rejected.value.code == "resume_owner_lost"
    assert store.count_executions(recovery_id) == 0
    _assert_expired_without_dispatch(
        store,
        recovery_id=recovery_id,
        request=request,
    )


def test_expired_terminal_decision_preserves_exact_and_conflicting_id_semantics(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "expired-approval-decision-ids.sqlite3")
    orchestrator, _provider, recovery_id, request, approval = _pending_approval(store)
    claim, _before_expiry = _claim_immediately_before_expiry(
        store,
        recovery_id,
        request,
        approval,
        monkeypatch,
    )
    monkeypatch.setattr(store, "_now", lambda: approval.expiry)
    completed_claim, retry_at = store.reconcile_expired_approval_claim(claim)
    assert retry_at is None
    assert completed_claim.response is not None
    assert store.claim_decision(recovery_id, request).response == completed_claim.response

    opposite = request.model_copy(update={"decision": "decline"})
    with pytest.raises(ApprovalDecisionError) as same_id_conflict:
        store.claim_decision(recovery_id, opposite)
    assert same_id_conflict.value.code == "decision_id_conflict"

    new_id = request.model_copy(update={"client_decision_id": "expired-new-id"})
    with pytest.raises(ApprovalDecisionError) as already_decided:
        store.claim_decision(recovery_id, new_id)
    assert already_decided.value.code == "already_decided"
    assert orchestrator._reconcile_startup_once() is None
    assert len([event for event in store.list_events(recovery_id) if event.terminal]) == 1


def test_expired_claim_rejects_multiple_dispatch_rows_without_rewrite(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "expired-approval-multiple-dispatch.sqlite3")
    orchestrator, _provider, recovery_id, request, approval = _pending_approval(store)
    _claim, before_expiry = _claim_immediately_before_expiry(
        store,
        recovery_id,
        request,
        approval,
        monkeypatch,
    )
    with sqlite3.connect(store._database_path) as connection:
        for index in range(2):
            connection.execute(
                """
                INSERT INTO executions (
                    id, recovery_id, idempotency_key, status, provider_execution,
                    request_digest, tool_call_id, remedy_digest, result_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'dispatch_started', 0, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    f"ambiguous-dispatch-{index}",
                    recovery_id,
                    f"ambiguous-dispatch-{index}",
                    str(index + 3) * 64,
                    request.tool_call_id,
                    request.remedy_digest,
                    before_expiry.isoformat(),
                    before_expiry.isoformat(),
                ),
            )
    monkeypatch.setattr(store, "_now", lambda: approval.expiry)

    with pytest.raises(ReceiptTransitionError, match="evidence is ambiguous"):
        orchestrator._reconcile_startup_once()

    assert store.get_recovery(recovery_id).status is RecoveryStatus.PENDING_APPROVAL
    assert store.get_permission_scope(recovery_id).status == "active"
    with pytest.raises(RecoveryNotFoundError, match="Receipt not found"):
        store.get_receipt(recovery_id)
    assert not [event for event in store.list_events(recovery_id) if event.terminal]


def test_expired_claim_rejects_malformed_completed_result_without_rewrite(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "expired-approval-malformed-result.sqlite3")
    orchestrator, _provider, recovery_id, request, approval = _pending_approval(store)
    _claim, before_expiry = _claim_immediately_before_expiry(
        store,
        recovery_id,
        request,
        approval,
        monkeypatch,
    )
    with sqlite3.connect(store._database_path) as connection:
        connection.execute(
            """
            INSERT INTO executions (
                id, recovery_id, idempotency_key, status, provider_execution,
                request_digest, tool_call_id, remedy_digest, result_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, 'completed', 1, ?, ?, ?, ?, ?, ?)
            """,
            (
                "malformed-completed-dispatch",
                recovery_id,
                "malformed-completed-dispatch",
                "5" * 64,
                request.tool_call_id,
                request.remedy_digest,
                json.dumps(
                    {
                        "dispatch_id": "malformed-completed-dispatch",
                        "simulated": True,
                        "status": "confirmed",
                    }
                ),
                before_expiry.isoformat(),
                before_expiry.isoformat(),
            ),
        )
    monkeypatch.setattr(store, "_now", lambda: approval.expiry)

    with pytest.raises(ReceiptTransitionError, match="dispatch evidence mismatch"):
        orchestrator._reconcile_startup_once()

    assert store.get_recovery(recovery_id).status is RecoveryStatus.PENDING_APPROVAL
    assert store.get_permission_scope(recovery_id).status == "active"
    with pytest.raises(RecoveryNotFoundError, match="Receipt not found"):
        store.get_receipt(recovery_id)
    assert not [event for event in store.list_events(recovery_id) if event.terminal]


def test_active_lifecycle_wakes_for_new_claim_and_seals_at_immutable_expiry(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "expired-approval-active-lifecycle.sqlite3")
    orchestrator, provider, recovery_id, request, approval = _pending_approval(store)
    current = [approval.expiry - timedelta(seconds=5)]
    sleeps: list[float] = []
    monkeypatch.setattr(store, "_now", lambda: current[0])
    orchestrator._reconciliation_clock = lambda: current[0]

    async def advance_to_expiry(delay: float) -> None:
        sleeps.append(delay)
        current[0] = approval.expiry
        await asyncio.sleep(0)

    orchestrator._reconciliation_sleep = advance_to_expiry

    async def exercise() -> None:
        owner = object()
        await orchestrator.startup(owner)
        await orchestrator.wait_for_startup_reconciliation()
        claim = await orchestrator.store_io.control(
            store.claim_decision,
            recovery_id,
            request,
        )
        assert claim.response is None
        orchestrator.notify_decision_claimed()
        await orchestrator.wait_for_startup_reconciliation()
        await orchestrator.shutdown(owner)
        await orchestrator.store_io.shutdown()

    asyncio.run(exercise())

    assert sleeps == [pytest.approx(5.0)]
    assert provider.dispatch_count == 0
    _assert_expired_without_dispatch(
        store,
        recovery_id=recovery_id,
        request=request,
    )


def test_new_claim_wakes_later_sleep_and_reschedules_earlier_without_overlap(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "expired-approval-deadline-wake.sqlite3")
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(store=store),
    )
    base = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    later = base + timedelta(seconds=30)
    earlier = base + timedelta(seconds=10)
    current = [base]
    outcomes = iter([later, earlier, None])
    reconcile_calls = 0
    active_sleeps = 0
    max_active_sleeps = 0
    cancelled_sleeps = 0
    delays: list[float] = []

    def reconcile_once():
        nonlocal reconcile_calls
        reconcile_calls += 1
        return next(outcomes)

    monkeypatch.setattr(orchestrator, "_reconcile_startup_once", reconcile_once)
    orchestrator._reconciliation_clock = lambda: current[0]

    async def exercise() -> None:
        nonlocal active_sleeps, max_active_sleeps, cancelled_sleeps
        first_sleep_started = asyncio.Event()
        block_first_sleep = asyncio.Event()

        async def controlled_sleep(delay: float) -> None:
            nonlocal active_sleeps, max_active_sleeps, cancelled_sleeps
            delays.append(delay)
            active_sleeps += 1
            max_active_sleeps = max(max_active_sleeps, active_sleeps)
            try:
                if len(delays) == 1:
                    first_sleep_started.set()
                    await block_first_sleep.wait()
                else:
                    current[0] = earlier
                    await asyncio.sleep(0)
            except asyncio.CancelledError:
                cancelled_sleeps += 1
                raise
            finally:
                active_sleeps -= 1

        orchestrator._reconciliation_sleep = controlled_sleep
        owner = object()
        await orchestrator.startup(owner)
        await asyncio.wait_for(first_sleep_started.wait(), timeout=1)
        orchestrator.notify_decision_claimed()
        await asyncio.wait_for(
            orchestrator.wait_for_startup_reconciliation(),
            timeout=1,
        )
        await orchestrator.shutdown(owner)
        await orchestrator.store_io.shutdown()

    asyncio.run(exercise())

    assert reconcile_calls == 3
    assert delays == [pytest.approx(30.0), pytest.approx(10.0)]
    assert cancelled_sleeps == 1
    assert max_active_sleeps == 1
