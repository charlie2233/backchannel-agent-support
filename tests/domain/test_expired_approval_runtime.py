from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import timedelta
from threading import Event

import pytest

from server.models import (
    ApprovalDecisionRequest,
    ExecutionMode,
    PendingApprovalView,
    RecoveryStatus,
)
from server.orchestrator import RecoveryOrchestrator
from server.providers.hotel_contract import durable_hotel_dispatch_contract
from server.providers.hotel_simulator import HotelSimulator
from server.store import ApprovalDecisionError, SQLiteStore


def _pending_expiring_approval(
    store: SQLiteStore,
) -> tuple[
    RecoveryOrchestrator,
    HotelSimulator,
    str,
    ApprovalDecisionRequest,
    PendingApprovalView,
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
        clientDecisionId=f"runtime-expiry-{recovery_id}",
        remedyId=approval.remedy_id,
        remedyDigest=approval.remedy_digest,
        toolCallId=approval.tool_call_id,
    )
    return orchestrator, provider, recovery_id, request, approval


def test_real_sdk_runner_returns_terminal_expiry_when_record_guard_loses_race(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "runner-record-expiry.sqlite3")
    orchestrator, provider, recovery_id, request, approval = (
        _pending_expiring_approval(store)
    )
    clock = [approval.expiry - timedelta(microseconds=1)]
    monkeypatch.setattr(store, "_now", lambda: clock[0])
    claim = store.claim_decision(recovery_id, request)
    original_guard = store.assert_provider_dispatch_authorized
    guard_calls = 0

    def guard_then_expire(**kwargs) -> None:
        nonlocal guard_calls
        original_guard(**kwargs)
        guard_calls += 1
        clock[0] = approval.expiry

    monkeypatch.setattr(
        store,
        "assert_provider_dispatch_authorized",
        guard_then_expire,
    )

    response = asyncio.run(
        orchestrator.approve_decision(
            recovery_id,
            request,
            claimed_decision=claim,
        )
    )

    assert guard_calls == 1
    assert response.status == "closed_without_action"
    assert response.execution_started is False
    assert response.terminal_reason == "authorization_expired_before_dispatch"
    assert provider.dispatch_count == 0
    assert store.count_executions(recovery_id) == 0
    assert store.get_recovery(recovery_id).status is RecoveryStatus.CLOSED_WITHOUT_ACTION
    receipt = store.get_receipt(recovery_id)
    assert receipt.terminal_reason == "authorization_expired_before_dispatch"
    assert len([event for event in store.list_events(recovery_id) if event.terminal]) == 1

    replayed = asyncio.run(
        orchestrator.approve_decision(
            recovery_id,
            request,
            claimed_decision=claim,
        )
    )
    assert replayed == response
    assert provider.dispatch_count == 0


def test_completed_row_at_expiry_is_sealed_as_provider_outcome_unknown(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "completed-at-expiry.sqlite3")
    orchestrator, provider, recovery_id, request, approval = (
        _pending_expiring_approval(store)
    )
    before_expiry = approval.expiry - timedelta(microseconds=1)
    monkeypatch.setattr(store, "_now", lambda: before_expiry)
    store.claim_decision(recovery_id, request)
    contract = durable_hotel_dispatch_contract(
        recovery_id=recovery_id,
        remedy=store.get_remedy_consent(recovery_id).evidence.remedy,
        tool_call_id=request.tool_call_id,
        remedy_digest=request.remedy_digest,
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
                contract.execution_id,
                recovery_id,
                contract.idempotency_key,
                contract.request_digest,
                request.tool_call_id,
                request.remedy_digest,
                json.dumps(contract.result_json, separators=(",", ":"), sort_keys=True),
                approval.expiry.isoformat(),
                approval.expiry.isoformat(),
            ),
        )
    monkeypatch.setattr(
        store,
        "_now",
        lambda: approval.expiry + timedelta(microseconds=1),
    )

    assert orchestrator._reconcile_startup_once() is None

    snapshot = store.get_recovery(recovery_id)
    assert snapshot.status is RecoveryStatus.OUTCOME_UNKNOWN
    receipt = store.get_receipt(recovery_id)
    assert receipt.status == "outcome_unknown"
    assert receipt.provider_execution is True
    assert receipt.provider_dispatch_started is True
    assert receipt.execution_count == 1
    assert (
        receipt.terminal_reason
        == "authorization_expired_with_unresolved_dispatch"
    )
    assert provider.dispatch_count == 0


def test_execution_write_resamples_expiry_at_final_insert_boundary(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "final-write-boundary-expiry.sqlite3")
    _orchestrator, _provider, recovery_id, request, approval = (
        _pending_expiring_approval(store)
    )
    before_expiry = approval.expiry - timedelta(microseconds=1)
    monkeypatch.setattr(store, "_now", lambda: before_expiry)
    claim = store.claim_decision(recovery_id, request)
    lease = store.acquire_decision_resume(
        claim,
        resume_owner_id="final-write-boundary-owner",
        lease_duration=timedelta(minutes=1),
        now=before_expiry,
    )
    contract = durable_hotel_dispatch_contract(
        recovery_id=recovery_id,
        remedy=store.get_remedy_consent(recovery_id).evidence.remedy,
        tool_call_id=request.tool_call_id,
        remedy_digest=request.remedy_digest,
    )
    clock_calls = 0

    def cross_expiry_during_validation():
        nonlocal clock_calls
        clock_calls += 1
        return before_expiry if clock_calls == 1 else approval.expiry

    monkeypatch.setattr(store, "_now", cross_expiry_during_validation)

    with pytest.raises(ApprovalDecisionError) as rejected:
        store.record_completed_execution(
            execution_id=contract.execution_id,
            recovery_id=recovery_id,
            idempotency_key=contract.idempotency_key,
            request_digest=contract.request_digest,
            tool_call_id=request.tool_call_id,
            remedy_digest=request.remedy_digest,
            action_digest=store.get_pending_approval(recovery_id).action_digest,
            resume_owner_id="final-write-boundary-owner",
            resume_generation=lease.resume_generation,
            result_json=contract.result_json,
        )

    assert rejected.value.code == "remedy_expired"
    assert clock_calls == 2
    assert store.count_executions(recovery_id) == 0


def test_cancelled_claim_cannot_strand_expiry_reconciliation(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "cancelled-claim-expiry.sqlite3")
    orchestrator, provider, recovery_id, request, approval = (
        _pending_expiring_approval(store)
    )
    current = [approval.expiry - timedelta(seconds=5)]
    monkeypatch.setattr(store, "_now", lambda: current[0])
    orchestrator._reconciliation_clock = lambda: current[0]
    sleep_delays: list[float] = []
    original_claim = store.claim_decision
    claim_committed = Event()
    release_claim_worker = Event()

    def commit_then_block(*args, **kwargs):
        claim = original_claim(*args, **kwargs)
        claim_committed.set()
        assert release_claim_worker.wait(timeout=2)
        return claim

    monkeypatch.setattr(store, "claim_decision", commit_then_block)

    async def advance_to_expiry(delay: float) -> None:
        sleep_delays.append(delay)
        current[0] = approval.expiry
        await asyncio.sleep(0)

    orchestrator._reconciliation_sleep = advance_to_expiry

    async def exercise() -> None:
        owner = object()
        await orchestrator.startup(owner)
        await orchestrator.wait_for_startup_reconciliation()
        approval_task = asyncio.create_task(
            orchestrator.approve_decision(recovery_id, request)
        )
        committed = await asyncio.to_thread(claim_committed.wait, 2)
        assert committed
        approval_task.cancel()
        release_claim_worker.set()
        with pytest.raises(asyncio.CancelledError):
            await approval_task
        await asyncio.wait_for(
            orchestrator.wait_for_startup_reconciliation(),
            timeout=1,
        )
        await orchestrator.shutdown(owner)
        await orchestrator.store_io.shutdown()

    asyncio.run(exercise())

    assert sleep_delays == [pytest.approx(5.0)]
    assert provider.dispatch_count == 0
    assert store.count_executions(recovery_id) == 0
    assert store.get_recovery(recovery_id).status is RecoveryStatus.CLOSED_WITHOUT_ACTION
    assert (
        store.get_receipt(recovery_id).terminal_reason
        == "authorization_expired_before_dispatch"
    )
