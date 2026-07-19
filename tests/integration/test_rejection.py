from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

import pytest
from agents import Runner

from server.agents.stub_model import (
    CLOSED_WITHOUT_ACTION_MESSAGE,
    DECLINE_MESSAGE,
    DeterministicApprovalModel,
)
from server.agents.tracing import configure_sdk_stub_tracing
from server.models import ApprovalDecisionRequest, ExecutionMode, RecoveryStatus
from server.orchestrator import RecoveryOrchestrator
from server.providers.hotel_simulator import HotelSimulator
from server.store import RecoveryNotFoundError, SQLiteStore


def _item_value(item: object, key: str) -> object:
    if isinstance(item, Mapping):
        return item.get(key)
    return getattr(item, key, None)


def _decision_request(pending, decision_id: str) -> ApprovalDecisionRequest:
    approval = pending.recovery.pending_approval
    assert approval is not None
    return ApprovalDecisionRequest(
        decision="decline",
        clientDecisionId=decision_id,
        remedyId=approval.remedy_id,
        remedyDigest=approval.remedy_digest,
        toolCallId=approval.tool_call_id,
    )


def test_actual_sdk_rejection_message_reaches_model_and_closes_without_execution(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "sdk-rejection-message.sqlite3")
    provider = HotelSimulator(store=store)
    orchestrator = RecoveryOrchestrator(store=store, hotel_provider=provider)
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    state = pending.sdk_result.to_state()
    interruptions = state.get_interruptions()
    assert len(interruptions) == 1
    interruption = interruptions[0]
    captured_inputs: list[str | list[Any]] = []
    original_get_response = DeterministicApprovalModel.get_response

    async def capture_get_response(self, *args, **kwargs):
        captured_inputs.append(kwargs.get("input", args[1] if len(args) > 1 else ""))
        return await original_get_response(self, *args, **kwargs)

    monkeypatch.setattr(
        DeterministicApprovalModel,
        "get_response",
        capture_get_response,
    )

    state.reject(interruption, rejection_message=DECLINE_MESSAGE)
    completed = asyncio.run(
        Runner.run(
            pending.original_root_agent,
            state,
            run_config=configure_sdk_stub_tracing(),
        )
    )

    matching_outputs = [
        item
        for model_input in captured_inputs
        if isinstance(model_input, list)
        for item in model_input
        if _item_value(item, "type") == "function_call_output"
        and _item_value(item, "call_id") == interruption.call_id
    ]
    assert len(matching_outputs) == 1
    assert _item_value(matching_outputs[0], "output") == DECLINE_MESSAGE
    assert completed.final_output == CLOSED_WITHOUT_ACTION_MESSAGE
    assert provider.dispatch_count == 0
    assert store.count_executions(pending.recovery.recovery_id) == 0


def test_decline_after_restart_seals_exact_cancellation_receipt(tmp_path) -> None:
    database_path = tmp_path / "decline-restart.sqlite3"
    store = SQLiteStore(database_path)
    provider = HotelSimulator(store=store)
    orchestrator = RecoveryOrchestrator(store=store, hotel_provider=provider)
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    recovery_id = pending.recovery.recovery_id
    request = _decision_request(pending, "restart-decline")
    store.close()

    restarted_store = SQLiteStore(database_path)
    restarted_provider = HotelSimulator(store=restarted_store)
    restarted = RecoveryOrchestrator(
        store=restarted_store,
        hotel_provider=restarted_provider,
    )
    response = asyncio.run(restarted.decide(recovery_id, request))

    assert response.model_dump(mode="json", by_alias=True) == {
        "clientDecisionId": "restart-decline",
        "recoveryId": recovery_id,
        "decision": "decline",
        "status": "closed_without_action",
        "decisionRemedyDigest": request.remedy_digest,
        "executionStarted": False,
    }
    assert restarted_provider.dispatch_count == 0
    assert restarted_store.count_executions(recovery_id) == 0
    assert restarted_store.get_recovery(recovery_id).status is (
        RecoveryStatus.CLOSED_WITHOUT_ACTION
    )
    receipt = restarted_store.get_receipt(recovery_id)
    assert receipt.status == "closed_without_action"
    assert receipt.provider_execution is False
    assert receipt.execution_count == 0
    assert receipt.provider_dispatch_started is False
    assert receipt.decision == "declined"
    assert receipt.decision_remedy_digest == request.remedy_digest
    assert receipt.approved_remedy_digest is None
    assert receipt.exact_interruption_rejected is True
    assert receipt.permission_revoked is True
    assert receipt.scope_closed is True
    assert receipt.model_ids == []
    assert receipt.verification_results == [
        "Human consent requested.",
        "Remedy declined by operator.",
        "Exact interruption rejected.",
        "No replacement action selected.",
        "Temporary permission revoked.",
        "Cancellation receipt sealed.",
    ]
    scope = restarted_store.get_permission_scope(recovery_id)
    assert scope.status == "revoked"
    assert scope.revoked_at is not None
    terminal = [
        event for event in restarted_store.list_events(recovery_id) if event.terminal
    ]
    assert len(terminal) == 1
    assert terminal[0].type == "recovery.closed_without_action"

    replayed = asyncio.run(restarted.decide(recovery_id, request))
    assert replayed == response
    assert restarted_provider.dispatch_count == 0
    assert restarted_store.count_decisions(recovery_id) == 1
    assert len(
        [event for event in restarted_store.list_events(recovery_id) if event.terminal]
    ) == 1


def test_expired_exact_remedy_can_be_declined_without_execution(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "expired-decline.sqlite3"
    store = SQLiteStore(database_path)
    provider = HotelSimulator(store=store)
    orchestrator = RecoveryOrchestrator(store=store, hotel_provider=provider)
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    recovery_id = pending.recovery.recovery_id
    request = _decision_request(pending, "expired-decline")
    approval = pending.recovery.pending_approval
    assert approval is not None
    monkeypatch.setattr(
        store,
        "_now",
        lambda: approval.expiry + timedelta(minutes=1),
    )

    response = asyncio.run(orchestrator.decide(recovery_id, request))

    assert response.status == "closed_without_action"
    assert provider.dispatch_count == 0
    assert store.count_executions(recovery_id) == 0


def test_execution_evidence_forces_decline_to_outcome_unknown(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "decline-outcome-unknown.sqlite3")
    provider = HotelSimulator(store=store)
    orchestrator = RecoveryOrchestrator(store=store, hotel_provider=provider)
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    recovery_id = pending.recovery.recovery_id
    request = _decision_request(pending, "unknown-decline")
    store.record_completed_execution(
        execution_id="execution-preexisting-evidence",
        recovery_id=recovery_id,
        idempotency_key="preexisting-dispatch-evidence",
        request_digest="request-digest-evidence",
        tool_call_id=request.tool_call_id,
        remedy_digest=request.remedy_digest,
        result_json={
            "dispatch_id": "dispatch-evidence",
            "status": "confirmed",
            "simulated": True,
            "provider_result": "Provider dispatch evidence exists.",
        },
    )

    response = asyncio.run(orchestrator.decide(recovery_id, request))

    assert response.status == "outcome_unknown"
    snapshot = store.get_recovery(recovery_id)
    assert snapshot.status is RecoveryStatus.OUTCOME_UNKNOWN
    receipt = store.get_receipt(recovery_id)
    assert receipt.status == "outcome_unknown"
    assert receipt.execution_count == 1
    assert receipt.provider_dispatch_started is True
    assert receipt.provider_execution is True
    assert receipt.exact_interruption_rejected is True
    assert receipt.permission_revoked is True
    assert [event.type for event in store.list_events(recovery_id) if event.terminal] == [
        "recovery.outcome_unknown"
    ]


def test_decline_retries_after_crash_between_claim_and_sdk_restore(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "decline-crash-after-claim.sqlite3"
    store = SQLiteStore(database_path)
    provider = HotelSimulator(store=store)
    orchestrator = RecoveryOrchestrator(store=store, hotel_provider=provider)
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    recovery_id = pending.recovery.recovery_id
    request = _decision_request(pending, "decline-crash-after-claim")

    class SimulatedProcessCrash(RuntimeError):
        pass

    async def crash_before_restore(_claim):
        raise SimulatedProcessCrash("crash after durable decline claim")

    monkeypatch.setattr(orchestrator, "_resume_claimed_decline", crash_before_restore)
    with pytest.raises(SimulatedProcessCrash, match="durable decline claim"):
        asyncio.run(orchestrator.decide(recovery_id, request))

    assert store.count_decisions(recovery_id) == 1
    assert store.get_pending_approval(recovery_id).status == "declined"
    assert store.get_permission_scope(recovery_id).status == "pending"
    assert store.count_executions(recovery_id) == 0
    with pytest.raises(RecoveryNotFoundError):
        store.get_receipt(recovery_id)
    store.close()

    restarted_store = SQLiteStore(database_path)
    restarted_provider = HotelSimulator(store=restarted_store)
    restarted = RecoveryOrchestrator(
        store=restarted_store,
        hotel_provider=restarted_provider,
    )
    response = asyncio.run(restarted.decide(recovery_id, request))
    assert response.status == "closed_without_action"
    assert restarted_provider.dispatch_count == 0
    assert restarted_store.count_executions(recovery_id) == 0
    assert restarted_store.get_permission_scope(recovery_id).status == "revoked"


def test_decline_retries_after_runner_before_terminal_finalize(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "decline-crash-after-runner.sqlite3"
    store = SQLiteStore(database_path)
    provider = HotelSimulator(store=store)
    orchestrator = RecoveryOrchestrator(store=store, hotel_provider=provider)
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    recovery_id = pending.recovery.recovery_id
    request = _decision_request(pending, "decline-crash-after-runner")

    class SimulatedProcessCrash(RuntimeError):
        pass

    def crash_before_finalize(*_args, **_kwargs):
        raise SimulatedProcessCrash("crash after rejected SDK run")

    monkeypatch.setattr(store, "finalize_declined_decision", crash_before_finalize)
    with pytest.raises(SimulatedProcessCrash, match="rejected SDK run"):
        asyncio.run(orchestrator.decide(recovery_id, request))

    assert store.get_pending_approval(recovery_id).status == "rejected"
    assert store.get_permission_scope(recovery_id).status == "pending"
    with pytest.raises(RecoveryNotFoundError):
        store.get_receipt(recovery_id)
    store.close()

    restarted_store = SQLiteStore(database_path)
    restarted_provider = HotelSimulator(store=restarted_store)
    restarted = RecoveryOrchestrator(
        store=restarted_store,
        hotel_provider=restarted_provider,
    )
    assert restarted_store.get_recovery(recovery_id).status is (
        RecoveryStatus.CLOSED_WITHOUT_ACTION
    )
    response = asyncio.run(restarted.decide(recovery_id, request))
    assert response.status == "closed_without_action"
    assert restarted_provider.dispatch_count == 0
    assert restarted_store.count_decisions(recovery_id) == 1
    assert len(
        [event for event in restarted_store.list_events(recovery_id) if event.terminal]
    ) == 1


def test_rejected_decline_with_dispatch_evidence_reopens_as_outcome_unknown(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "decline-reopen-outcome-unknown.sqlite3"
    store = SQLiteStore(database_path)
    provider = HotelSimulator(store=store)
    orchestrator = RecoveryOrchestrator(store=store, hotel_provider=provider)
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    recovery_id = pending.recovery.recovery_id
    request = _decision_request(pending, "rejected-with-execution")
    store.record_completed_execution(
        execution_id="execution-before-decline-reopen",
        recovery_id=recovery_id,
        idempotency_key="dispatch-before-decline-reopen",
        request_digest="dispatch-evidence-digest",
        tool_call_id=request.tool_call_id,
        remedy_digest=request.remedy_digest,
        result_json={
            "dispatch_id": "dispatch-before-decline-reopen",
            "status": "confirmed",
            "simulated": True,
            "provider_result": "Preexisting dispatch evidence.",
        },
    )

    class SimulatedProcessCrash(RuntimeError):
        pass

    def crash_before_finalize(*_args, **_kwargs):
        raise SimulatedProcessCrash("reopen before outcome finalization")

    monkeypatch.setattr(store, "finalize_declined_decision", crash_before_finalize)
    with pytest.raises(SimulatedProcessCrash, match="outcome finalization"):
        asyncio.run(orchestrator.decide(recovery_id, request))
    assert store.get_pending_approval(recovery_id).status == "rejected"
    store.close()

    reopened_store = SQLiteStore(database_path)
    reopened_provider = HotelSimulator(store=reopened_store)
    RecoveryOrchestrator(store=reopened_store, hotel_provider=reopened_provider)

    assert reopened_provider.dispatch_count == 0
    assert reopened_store.get_recovery(recovery_id).status is (
        RecoveryStatus.OUTCOME_UNKNOWN
    )
    receipt = reopened_store.get_receipt(recovery_id)
    assert receipt.status == "outcome_unknown"
    assert receipt.execution_count == 1
    assert receipt.provider_dispatch_started is True
    assert receipt.exact_interruption_rejected is True
    assert len(
        [event for event in reopened_store.list_events(recovery_id) if event.terminal]
    ) == 1

    reopened_store.close()
    second_store = SQLiteStore(database_path)
    second_provider = HotelSimulator(store=second_store)
    RecoveryOrchestrator(store=second_store, hotel_provider=second_provider)
    assert second_provider.dispatch_count == 0
    assert second_store.get_receipt(recovery_id) == receipt
    assert len(
        [event for event in second_store.list_events(recovery_id) if event.terminal]
    ) == 1
