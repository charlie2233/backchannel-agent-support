import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from agents.exceptions import UserError

from server.config import RuntimeSettings
from server.main import create_app
from server.models import ApprovalDecisionRequest, ExecutionMode, RecoveryStatus
from server.orchestrator import RecoveryOrchestrator
from server.providers.hotel_simulator import HotelSimulator
from server.store import RecoveryNotFoundError, SQLiteStore


def approval_request(pending, decision_id: str) -> ApprovalDecisionRequest:
    approval = pending.recovery.pending_approval
    assert approval is not None
    return ApprovalDecisionRequest(
        decision="approve",
        clientDecisionId=decision_id,
        remedyId=approval.remedy_id,
        remedyDigest=approval.remedy_digest,
        toolCallId=approval.tool_call_id,
    )


async def run_startup_reconciliation(orchestrator: RecoveryOrchestrator) -> None:
    await orchestrator.startup()
    await orchestrator.wait_for_startup_reconciliation()
    await orchestrator.shutdown()


def test_pending_sdk_approval_resumes_after_every_runtime_object_is_recreated(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "restart-resume.sqlite3"
    store = SQLiteStore(database_path)
    provider = HotelSimulator(store=store)
    orchestrator = RecoveryOrchestrator(store=store, hotel_provider=provider)

    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    recovery_id = pending.recovery.recovery_id
    request = approval_request(pending, "restart-approval")
    interruption = pending.sdk_result.interruptions[0]
    envelope = store.get_pending_approval(recovery_id)

    assert envelope.tool_call_id == interruption.call_id
    assert envelope.execution_mode is ExecutionMode.SDK_STUB
    assert envelope.sdk_version == "0.18.3"
    assert envelope.protocol_version
    assert envelope.agent_graph_version
    assert len(envelope.definition_digest) == 64
    assert envelope.root_trace_id.startswith("qa_trace_")
    assert len(envelope.action_digest) == 64
    assert envelope.remedy_id == "remedy-king-room"
    assert envelope.consent_digest.startswith("sha256:")
    assert envelope.state_json
    assert provider.dispatch_count == 0
    public_event_json = json.dumps(
        store.list_events(recovery_id)[-1].data,
        sort_keys=True,
    )
    assert interruption.call_id not in public_event_json
    assert "commit_remedy" not in public_event_json

    store.close()
    del orchestrator, provider, store, pending

    reopened_store = SQLiteStore(database_path)
    fresh_provider = HotelSimulator(store=reopened_store)
    fresh_orchestrator = RecoveryOrchestrator(
        store=reopened_store,
        hotel_provider=fresh_provider,
    )
    observed_action_digests: list[str | None] = []
    original_guard = reopened_store.assert_provider_dispatch_authorized

    def capture_action_digest(**kwargs) -> None:
        observed_action_digests.append(kwargs.get("action_digest"))
        original_guard(**kwargs)

    monkeypatch.setattr(
        reopened_store,
        "assert_provider_dispatch_authorized",
        capture_action_digest,
    )

    completed = asyncio.run(fresh_orchestrator.approve_decision(recovery_id, request))

    assert completed.status == "completed"
    assert observed_action_digests == [envelope.action_digest]
    assert fresh_provider.dispatch_count == 1
    assert reopened_store.get_recovery(recovery_id).status is RecoveryStatus.COMPLETED
    assert reopened_store.get_receipt(recovery_id).provider_execution is True
    assert reopened_store.count_executions(recovery_id) == 1


def test_startup_reconciles_a_committed_execution_without_redispatch(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "restart-reconcile.sqlite3"
    store = SQLiteStore(database_path)
    provider = HotelSimulator(store=store)
    orchestrator = RecoveryOrchestrator(store=store, hotel_provider=provider)
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    recovery_id = pending.recovery.recovery_id
    request = approval_request(pending, "crash-reconcile")
    original_finalize = store.finalize_completed_execution

    class SimulatedProcessCrash(RuntimeError):
        pass

    def crash_after_execution(*args, **kwargs):
        if store.count_executions(recovery_id) == 1:
            raise SimulatedProcessCrash("crash after provider commit")
        return original_finalize(*args, **kwargs)

    monkeypatch.setattr(store, "finalize_completed_execution", crash_after_execution)

    with pytest.raises(UserError, match="crash after provider commit"):
        asyncio.run(orchestrator.approve_decision(recovery_id, request))

    assert provider.dispatch_count == 1
    assert store.count_executions(recovery_id) == 1
    with pytest.raises(RecoveryNotFoundError, match="Receipt not found"):
        store.get_receipt(recovery_id)
    store.close()

    fresh_store = SQLiteStore(database_path)
    fresh_provider = HotelSimulator(store=fresh_store)
    fresh_orchestrator = RecoveryOrchestrator(
        store=fresh_store,
        hotel_provider=fresh_provider,
    )

    assert fresh_provider.dispatch_count == 0
    assert fresh_store.get_recovery(recovery_id).status is (
        RecoveryStatus.PENDING_APPROVAL
    )
    with pytest.raises(RecoveryNotFoundError, match="Receipt not found"):
        fresh_store.get_receipt(recovery_id)

    asyncio.run(run_startup_reconciliation(fresh_orchestrator))

    assert fresh_store.get_recovery(recovery_id).status is RecoveryStatus.COMPLETED
    assert fresh_store.get_receipt(recovery_id).provider_execution is True
    assert fresh_store.count_executions(recovery_id) == 1
    assert fresh_store.count_decisions(recovery_id) == 1
    terminal_events = [
        event for event in fresh_store.list_events(recovery_id) if event.terminal
    ]
    assert len(terminal_events) == 1
    fresh_store.close()

    second_store = SQLiteStore(database_path)
    second_provider = HotelSimulator(store=second_store)
    second_orchestrator = RecoveryOrchestrator(
        store=second_store,
        hotel_provider=second_provider,
    )
    asyncio.run(run_startup_reconciliation(second_orchestrator))
    assert second_provider.dispatch_count == 0
    second_terminal_events = [
        event for event in second_store.list_events(recovery_id) if event.terminal
    ]
    assert len(second_terminal_events) == 1
    replayed = asyncio.run(second_orchestrator.approve_decision(recovery_id, request))
    assert replayed.status == "completed"
    assert second_provider.dispatch_count == 0


def test_startup_retries_committed_execution_after_foreign_lease_expires(
    tmp_path,
    monkeypatch,
) -> None:
    async def exercise() -> None:
        class FakeClock:
            def __init__(self) -> None:
                self.current = datetime.now(UTC)
                self.sleep_delays: list[float] = []
                self._wake_reconcilers = asyncio.Event()

            def now(self) -> datetime:
                return self.current

            async def sleep(self, delay: float) -> None:
                self.sleep_delays.append(delay)
                if len(self.sleep_delays) == 2:
                    self.current += timedelta(seconds=max(self.sleep_delays))
                    self._wake_reconcilers.set()
                await self._wake_reconcilers.wait()

        clock = FakeClock()
        database_path = tmp_path / "restart-unexpired-lease.sqlite3"
        initial_store = SQLiteStore(database_path)
        initial_orchestrator = RecoveryOrchestrator(
            store=initial_store,
            hotel_provider=HotelSimulator(store=initial_store),
        )
        pending = await initial_orchestrator.start(
            "hotel",
            execution_mode=ExecutionMode.SDK_STUB,
        )
        recovery_id = pending.recovery.recovery_id
        request = approval_request(pending, "crash-with-unexpired-lease")
        claim = initial_store.claim_decision(recovery_id, request)
        lease_duration = timedelta(milliseconds=500)
        lease = initial_store.acquire_decision_resume(
            claim,
            resume_owner_id="crashed-process-owner",
            lease_duration=lease_duration,
            now=clock.now(),
        )
        assert lease.disposition == "owner"
        execution, dispatched = initial_store.record_completed_execution(
            execution_id="execution-before-unexpired-crash",
            recovery_id=recovery_id,
            idempotency_key="dispatch-before-unexpired-crash",
            request_digest="request-before-unexpired-crash",
            tool_call_id=request.tool_call_id,
            remedy_digest=request.remedy_digest,
            result_json={
                "dispatch_id": "dispatch-before-unexpired-crash",
                "status": "confirmed",
                "simulated": True,
                "provider_result": "Durable result committed before process crash.",
            },
        )
        assert dispatched is True
        assert execution.provider_execution is True
        await initial_orchestrator.shutdown()
        initial_store.close()

        restarted_store = SQLiteStore(database_path)
        monkeypatch.setattr(restarted_store, "_now", clock.now)
        restarted_provider = HotelSimulator(store=restarted_store)
        restarted_orchestrator = RecoveryOrchestrator(
            store=restarted_store,
            hotel_provider=restarted_provider,
            decision_lease_duration=lease_duration,
            decision_wait_interval=0.01,
            reconciliation_clock=clock.now,
            reconciliation_sleep=clock.sleep,
        )
        competing_store = SQLiteStore(database_path)
        monkeypatch.setattr(competing_store, "_now", clock.now)
        competing_provider = HotelSimulator(store=competing_store)
        competing_orchestrator = RecoveryOrchestrator(
            store=competing_store,
            hotel_provider=competing_provider,
            decision_lease_duration=lease_duration,
            decision_wait_interval=0.01,
            reconciliation_clock=clock.now,
            reconciliation_sleep=clock.sleep,
        )
        assert restarted_orchestrator._reconciliation_task is None
        assert competing_orchestrator._reconciliation_task is None
        assert restarted_store.get_recovery(recovery_id).status is (
            RecoveryStatus.PENDING_APPROVAL
        )
        await restarted_orchestrator.startup()
        await competing_orchestrator.startup()

        await asyncio.wait_for(
            asyncio.gather(
                restarted_orchestrator.wait_for_startup_reconciliation(),
                competing_orchestrator.wait_for_startup_reconciliation(),
            ),
            timeout=0.5,
        )

        assert len(clock.sleep_delays) == 2
        assert all(delay == pytest.approx(0.5, abs=0.05) for delay in clock.sleep_delays)
        assert restarted_provider.dispatch_count == 0
        assert competing_provider.dispatch_count == 0
        assert restarted_store.count_executions(recovery_id) == 1
        assert restarted_store.get_recovery(recovery_id).status is (
            RecoveryStatus.COMPLETED
        )
        assert restarted_store.get_receipt(recovery_id).provider_execution is True
        assert competing_store.get_receipt(recovery_id).provider_execution is True
        assert len(
            [
                event
                for event in restarted_store.list_events(recovery_id)
                if event.terminal
            ]
        ) == 1
        await restarted_orchestrator.shutdown()
        await competing_orchestrator.shutdown()
        restarted_store.close()
        competing_store.close()

    asyncio.run(asyncio.wait_for(exercise(), timeout=1))


def test_startup_releases_owned_lease_after_partial_approval_completion_failure(
    tmp_path,
    monkeypatch,
) -> None:
    async def exercise() -> None:
        class FakeClock:
            def __init__(self) -> None:
                self.current = datetime.now(UTC)
                self.sleep_delays: list[float] = []

            def now(self) -> datetime:
                return self.current

            async def sleep(self, delay: float) -> None:
                self.sleep_delays.append(delay)
                self.current += timedelta(seconds=delay)
                await asyncio.sleep(0)

        clock = FakeClock()
        database_path = tmp_path / "restart-release-approval-lease.sqlite3"
        initial_store = SQLiteStore(database_path)
        initial_orchestrator = RecoveryOrchestrator(
            store=initial_store,
            hotel_provider=HotelSimulator(store=initial_store),
        )
        pending = await initial_orchestrator.start(
            "hotel",
            execution_mode=ExecutionMode.SDK_STUB,
        )
        recovery_id = pending.recovery.recovery_id
        request = approval_request(pending, "release-partial-approval-lease")
        initial_store.claim_decision(recovery_id, request)
        _, dispatched = initial_store.record_completed_execution(
            execution_id="execution-before-partial-approval-failure",
            recovery_id=recovery_id,
            idempotency_key="dispatch-before-partial-approval-failure",
            request_digest="request-before-partial-approval-failure",
            tool_call_id=request.tool_call_id,
            remedy_digest=request.remedy_digest,
            result_json={
                "dispatch_id": "dispatch-before-partial-approval-failure",
                "status": "confirmed",
                "simulated": True,
                "provider_result": "Durable result before partial finalization failure.",
            },
        )
        assert dispatched is True
        await initial_orchestrator.shutdown()
        initial_store.close()

        restarted_store = SQLiteStore(database_path)
        monkeypatch.setattr(restarted_store, "_now", clock.now)
        restarted_provider = HotelSimulator(store=restarted_store)
        original_complete = restarted_store.complete_approval_decision
        completion_attempts = 0

        def fail_completion_once(*args, **kwargs):
            nonlocal completion_attempts
            completion_attempts += 1
            if completion_attempts == 1:
                raise RuntimeError("injected approval completion failure")
            return original_complete(*args, **kwargs)

        release_calls: list[tuple[str, int, bool]] = []
        original_release = restarted_store.release_decision_resume

        def capture_release(
            claim,
            *,
            resume_owner_id: str,
            resume_generation: int,
        ) -> bool:
            released = original_release(
                claim,
                resume_owner_id=resume_owner_id,
                resume_generation=resume_generation,
            )
            release_calls.append(
                (resume_owner_id, resume_generation, released)
            )
            return released

        monkeypatch.setattr(
            restarted_store,
            "complete_approval_decision",
            fail_completion_once,
        )
        monkeypatch.setattr(
            restarted_store,
            "release_decision_resume",
            capture_release,
        )
        orchestrator = RecoveryOrchestrator(
            store=restarted_store,
            hotel_provider=restarted_provider,
            decision_wait_interval=0.01,
            reconciliation_clock=clock.now,
            reconciliation_sleep=clock.sleep,
        )

        await orchestrator.startup()
        await asyncio.wait_for(
            orchestrator.wait_for_startup_reconciliation(),
            timeout=0.5,
        )

        assert completion_attempts == 2
        assert clock.sleep_delays == [pytest.approx(0.1, abs=0.01)]
        assert len(release_calls) == 1
        released_owner, released_generation, released = release_calls[0]
        assert released_owner.startswith("reconcile-")
        assert released_generation > 0
        assert released is True
        assert restarted_provider.dispatch_count == 0
        assert restarted_store.count_executions(recovery_id) == 1
        assert restarted_store.get_recovery(recovery_id).status is (
            RecoveryStatus.COMPLETED
        )
        assert restarted_store.get_receipt(recovery_id).provider_execution is True
        assert len(
            [
                event
                for event in restarted_store.list_events(recovery_id)
                if event.terminal
            ]
        ) == 1
        await orchestrator.shutdown()
        restarted_store.close()

    asyncio.run(asyncio.wait_for(exercise(), timeout=1))


def test_startup_releases_owned_lease_after_decline_finalization_failure(
    tmp_path,
    monkeypatch,
) -> None:
    async def exercise() -> None:
        class FakeClock:
            def __init__(self) -> None:
                self.current = datetime.now(UTC)
                self.sleep_delays: list[float] = []

            def now(self) -> datetime:
                return self.current

            async def sleep(self, delay: float) -> None:
                self.sleep_delays.append(delay)
                self.current += timedelta(seconds=delay)
                await asyncio.sleep(0)

        clock = FakeClock()
        database_path = tmp_path / "restart-release-decline-lease.sqlite3"
        initial_store = SQLiteStore(database_path)
        initial_orchestrator = RecoveryOrchestrator(
            store=initial_store,
            hotel_provider=HotelSimulator(store=initial_store),
        )
        pending = await initial_orchestrator.start(
            "hotel",
            execution_mode=ExecutionMode.SDK_STUB,
        )
        recovery_id = pending.recovery.recovery_id
        approval = pending.recovery.pending_approval
        assert approval is not None
        request = ApprovalDecisionRequest(
            decision="decline",
            clientDecisionId="release-decline-lease",
            remedyId=approval.remedy_id,
            remedyDigest=approval.remedy_digest,
            toolCallId=approval.tool_call_id,
        )
        claim = initial_store.claim_decision(recovery_id, request)
        initial_lease = initial_store.acquire_decision_resume(
            claim,
            resume_owner_id="decline-crashed-owner",
            lease_duration=timedelta(seconds=30),
            now=clock.now(),
        )
        assert initial_lease.disposition == "owner"
        initial_store.record_exact_interruption_rejected(
            claim,
            resume_owner_id="decline-crashed-owner",
            resume_generation=initial_lease.resume_generation,
        )
        assert initial_store.release_decision_resume(
            claim,
            resume_owner_id="decline-crashed-owner",
            resume_generation=initial_lease.resume_generation,
        )
        await initial_orchestrator.shutdown()
        initial_store.close()

        restarted_store = SQLiteStore(database_path)
        monkeypatch.setattr(restarted_store, "_now", clock.now)
        restarted_provider = HotelSimulator(store=restarted_store)
        original_finalize = restarted_store.finalize_declined_decision
        finalization_attempts = 0

        def fail_finalization_once(*args, **kwargs):
            nonlocal finalization_attempts
            finalization_attempts += 1
            if finalization_attempts == 1:
                raise RuntimeError("injected decline finalization failure")
            return original_finalize(*args, **kwargs)

        release_calls: list[tuple[str, int, bool]] = []
        original_release = restarted_store.release_decision_resume

        def capture_release(
            durable_claim,
            *,
            resume_owner_id: str,
            resume_generation: int,
        ) -> bool:
            released = original_release(
                durable_claim,
                resume_owner_id=resume_owner_id,
                resume_generation=resume_generation,
            )
            release_calls.append(
                (resume_owner_id, resume_generation, released)
            )
            return released

        monkeypatch.setattr(
            restarted_store,
            "finalize_declined_decision",
            fail_finalization_once,
        )
        monkeypatch.setattr(
            restarted_store,
            "release_decision_resume",
            capture_release,
        )
        orchestrator = RecoveryOrchestrator(
            store=restarted_store,
            hotel_provider=restarted_provider,
            decision_wait_interval=0.01,
            reconciliation_clock=clock.now,
            reconciliation_sleep=clock.sleep,
        )

        await orchestrator.startup()
        await asyncio.wait_for(
            orchestrator.wait_for_startup_reconciliation(),
            timeout=0.5,
        )

        assert finalization_attempts == 2
        assert clock.sleep_delays == [pytest.approx(0.1, abs=0.01)]
        assert len(release_calls) == 1
        released_owner, released_generation, released = release_calls[0]
        assert released_owner.startswith("reconcile-")
        assert released_generation > initial_lease.resume_generation
        assert released is True
        assert restarted_provider.dispatch_count == 0
        assert restarted_store.count_executions(recovery_id) == 0
        assert restarted_store.get_recovery(recovery_id).status is (
            RecoveryStatus.CLOSED_WITHOUT_ACTION
        )
        receipt = restarted_store.get_receipt(recovery_id)
        assert receipt.exact_interruption_rejected is True
        assert receipt.provider_execution is False
        assert len(
            [
                event
                for event in restarted_store.list_events(recovery_id)
                if event.terminal
            ]
        ) == 1
        await orchestrator.shutdown()
        restarted_store.close()

    asyncio.run(asyncio.wait_for(exercise(), timeout=1))


def test_shutdown_cancels_permanently_failing_startup_reconciliation(
    tmp_path,
    monkeypatch,
) -> None:
    async def exercise() -> None:
        class FakeClock:
            def __init__(self) -> None:
                self.current = datetime.now(UTC)

            def now(self) -> datetime:
                return self.current

        clock = FakeClock()
        lease_duration = timedelta(milliseconds=500)
        database_path = tmp_path / "restart-reconciliation-shutdown.sqlite3"
        initial_store = SQLiteStore(database_path)
        initial_orchestrator = RecoveryOrchestrator(
            store=initial_store,
            hotel_provider=HotelSimulator(store=initial_store),
        )
        pending = await initial_orchestrator.start(
            "hotel",
            execution_mode=ExecutionMode.SDK_STUB,
        )
        recovery_id = pending.recovery.recovery_id
        request = approval_request(pending, "shutdown-unexpired-lease")
        claim = initial_store.claim_decision(recovery_id, request)
        lease = initial_store.acquire_decision_resume(
            claim,
            resume_owner_id="still-running-owner",
            lease_duration=lease_duration,
            now=clock.now(),
        )
        assert lease.disposition == "owner"
        _, dispatched = initial_store.record_completed_execution(
            execution_id="execution-before-shutdown-test",
            recovery_id=recovery_id,
            idempotency_key="dispatch-before-shutdown-test",
            request_digest="request-before-shutdown-test",
            tool_call_id=request.tool_call_id,
            remedy_digest=request.remedy_digest,
            result_json={
                "dispatch_id": "dispatch-before-shutdown-test",
                "status": "confirmed",
                "simulated": True,
                "provider_result": "Durable result awaiting lease expiry.",
            },
        )
        assert dispatched is True
        await initial_orchestrator.shutdown()
        initial_store.close()

        backoff_sleep_started = asyncio.Event()
        sleep_cancelled = asyncio.Event()
        never_release = asyncio.Event()
        sleep_delays: list[float] = []

        async def controlled_sleep(delay: float) -> None:
            sleep_delays.append(delay)
            if len(sleep_delays) == 1:
                clock.current += timedelta(seconds=delay)
                await asyncio.sleep(0)
                return
            backoff_sleep_started.set()
            try:
                await never_release.wait()
            except asyncio.CancelledError:
                sleep_cancelled.set()
                raise

        restarted_store = SQLiteStore(database_path)
        monkeypatch.setattr(restarted_store, "_now", clock.now)
        restarted_provider = HotelSimulator(store=restarted_store)
        orchestrator = RecoveryOrchestrator(
            store=restarted_store,
            hotel_provider=restarted_provider,
            decision_lease_duration=lease_duration,
            decision_wait_interval=0.01,
            reconciliation_clock=clock.now,
            reconciliation_sleep=controlled_sleep,
        )
        reconciliation_attempts = 0

        def permanently_fail_reconciliation() -> None:
            nonlocal reconciliation_attempts
            reconciliation_attempts += 1
            raise RuntimeError("injected permanent reconciliation failure")

        monkeypatch.setattr(
            orchestrator,
            "_reconcile_startup_once",
            permanently_fail_reconciliation,
        )
        assert orchestrator._reconciliation_task is None
        await orchestrator.startup()
        await asyncio.wait_for(backoff_sleep_started.wait(), timeout=0.2)
        await asyncio.wait_for(orchestrator.shutdown(), timeout=0.2)
        await orchestrator.shutdown()

        assert reconciliation_attempts == 2
        assert len(sleep_delays) == 2
        assert sleep_delays[0] == pytest.approx(0.1, abs=0.01)
        assert sleep_delays[1] == pytest.approx(0.2, abs=0.01)
        assert sleep_cancelled.is_set()
        assert orchestrator._reconciliation_task is None
        assert restarted_provider.dispatch_count == 0
        assert restarted_store.get_recovery(recovery_id).status is (
            RecoveryStatus.PENDING_APPROVAL
        )
        assert restarted_store.count_executions(recovery_id) == 1
        restarted_store.close()

    asyncio.run(asyncio.wait_for(exercise(), timeout=1))


def test_app_lifespan_retries_store_failure_for_orchestrator_created_outside_loop(
    tmp_path,
    monkeypatch,
) -> None:
    class FakeClock:
        def __init__(self) -> None:
            self.current = datetime.now(UTC)
            self.sleep_delays: list[float] = []

        def now(self) -> datetime:
            return self.current

        async def sleep(self, delay: float) -> None:
            self.sleep_delays.append(delay)
            self.current += timedelta(seconds=delay)
            await asyncio.sleep(0)

    clock = FakeClock()
    lease_duration = timedelta(milliseconds=500)
    database_path = tmp_path / "restart-lifespan-construction-order.sqlite3"
    initial_store = SQLiteStore(database_path)
    initial_orchestrator = RecoveryOrchestrator(
        store=initial_store,
        hotel_provider=HotelSimulator(store=initial_store),
    )
    pending = asyncio.run(
        initial_orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    recovery_id = pending.recovery.recovery_id
    request = approval_request(pending, "lifespan-unexpired-lease")
    claim = initial_store.claim_decision(recovery_id, request)
    lease = initial_store.acquire_decision_resume(
        claim,
        resume_owner_id="crashed-before-lifespan-owner",
        lease_duration=lease_duration,
        now=clock.now(),
    )
    assert lease.disposition == "owner"
    _, dispatched = initial_store.record_completed_execution(
        execution_id="execution-before-lifespan-start",
        recovery_id=recovery_id,
        idempotency_key="dispatch-before-lifespan-start",
        request_digest="request-before-lifespan-start",
        tool_call_id=request.tool_call_id,
        remedy_digest=request.remedy_digest,
        result_json={
            "dispatch_id": "dispatch-before-lifespan-start",
            "status": "confirmed",
            "simulated": True,
            "provider_result": "Durable result awaiting application lifespan.",
        },
    )
    assert dispatched is True
    asyncio.run(initial_orchestrator.shutdown())
    initial_store.close()

    restarted_store = SQLiteStore(database_path)
    monkeypatch.setattr(restarted_store, "_now", clock.now)
    original_list_executions = restarted_store.list_executions_needing_finalization
    reconciliation_store_attempts = 0

    def fail_reconciliation_store_once():
        nonlocal reconciliation_store_attempts
        reconciliation_store_attempts += 1
        if reconciliation_store_attempts == 1:
            raise RuntimeError("injected transient reconciliation store failure")
        return original_list_executions()

    monkeypatch.setattr(
        restarted_store,
        "list_executions_needing_finalization",
        fail_reconciliation_store_once,
    )
    restarted_provider = HotelSimulator(store=restarted_store)
    restarted_orchestrator = RecoveryOrchestrator(
        store=restarted_store,
        hotel_provider=restarted_provider,
        decision_lease_duration=lease_duration,
        decision_wait_interval=0.01,
        reconciliation_clock=clock.now,
        reconciliation_sleep=clock.sleep,
    )
    application = create_app(
        RuntimeSettings(live_ready=False, database_path=database_path),
        store=restarted_store,
        hotel_provider=restarted_provider,
        orchestrator=restarted_orchestrator,
    )

    assert reconciliation_store_attempts == 0
    assert restarted_orchestrator._reconciliation_task is None
    assert restarted_store.get_recovery(recovery_id).status is (
        RecoveryStatus.PENDING_APPROVAL
    )

    async def exercise_lifespan() -> None:
        async with application.router.lifespan_context(application):
            await asyncio.wait_for(
                restarted_orchestrator.wait_for_startup_reconciliation(),
                timeout=0.5,
            )
            assert reconciliation_store_attempts == 3
            assert len(clock.sleep_delays) == 2
            assert clock.sleep_delays[0] == pytest.approx(0.1, abs=0.01)
            assert clock.sleep_delays[1] == pytest.approx(0.4, abs=0.05)
            assert restarted_provider.dispatch_count == 0
            assert restarted_store.count_executions(recovery_id) == 1
            assert restarted_store.get_recovery(recovery_id).status is (
                RecoveryStatus.COMPLETED
            )
            assert restarted_store.get_receipt(recovery_id).provider_execution is True
            assert len(
                [
                    event
                    for event in restarted_store.list_events(recovery_id)
                    if event.terminal
                ]
            ) == 1

        assert restarted_orchestrator._reconciliation_task is None
        await restarted_orchestrator.shutdown()

    asyncio.run(asyncio.wait_for(exercise_lifespan(), timeout=1))
    restarted_store.close()


def test_task3_schema_migration_preserves_parent_child_rows_and_foreign_keys(
    tmp_path,
) -> None:
    database_path = tmp_path / "task3-schema.sqlite3"
    timestamp = "2026-07-18T20:00:00+00:00"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE recoveries (
                id TEXT PRIMARY KEY,
                scenario_id TEXT NOT NULL CHECK (scenario_id IN ('hotel', 'api-quota')),
                execution_mode TEXT NOT NULL CHECK (
                    execution_mode IN ('openai_live', 'sdk_stub', 'replay_fixture')
                ),
                status TEXT NOT NULL CHECK (
                    status IN ('in_progress', 'pending_approval', 'completed')
                ),
                current_step INTEGER NOT NULL CHECK (current_step BETWEEN 0 AND 5),
                current_step_summary TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE remedies (
                id TEXT PRIMARY KEY,
                recovery_id TEXT NOT NULL REFERENCES recoveries(id) ON DELETE CASCADE,
                terms_json TEXT NOT NULL,
                digest TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE pending_approvals (
                tool_call_id TEXT PRIMARY KEY,
                recovery_id TEXT NOT NULL REFERENCES recoveries(id) ON DELETE CASCADE,
                serialized_state TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE executions (
                id TEXT PRIMARY KEY,
                recovery_id TEXT NOT NULL REFERENCES recoveries(id) ON DELETE CASCADE,
                idempotency_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                provider_execution INTEGER NOT NULL DEFAULT 0,
                result_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recovery_id TEXT NOT NULL REFERENCES recoveries(id) ON DELETE CASCADE,
                seq INTEGER NOT NULL,
                type TEXT NOT NULL,
                terminal INTEGER NOT NULL DEFAULT 0,
                data_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (recovery_id, seq)
            );
            """
        )
        connection.execute(
            "INSERT INTO recoveries VALUES (?, 'hotel', 'sdk_stub', "
            "'pending_approval', 3, 'Legacy pending state.', ?, ?)",
            ("legacy-recovery", timestamp, timestamp),
        )
        connection.execute(
            "INSERT INTO remedies VALUES "
            "('legacy-remedy', 'legacy-recovery', '{}', NULL, 'pending', ?)",
            (timestamp,),
        )
        connection.execute(
            "INSERT INTO pending_approvals VALUES "
            "('legacy-call', 'legacy-recovery', '{\"legacy\":true}', "
            "'pending', ?, ?)",
            (timestamp, timestamp),
        )
        connection.execute(
            "INSERT INTO executions VALUES "
            "('legacy-execution', 'legacy-recovery', 'legacy-key', 'pending', "
            "0, NULL, ?, ?)",
            (timestamp, timestamp),
        )
        connection.execute(
            "INSERT INTO events (recovery_id, seq, type, terminal, data_json, created_at) "
            "VALUES ('legacy-recovery', 1, 'approval.requested', 0, '{}', ?)",
            (timestamp,),
        )

    migrated = SQLiteStore(database_path)
    envelope = migrated.get_pending_approval("legacy-recovery")

    assert envelope.tool_call_id == "legacy-call"
    assert envelope.sdk_version == "legacy-incompatible"
    assert envelope.state_json == {"legacy": True}
    assert migrated.count_executions("legacy-recovery") == 1
    assert len(migrated.list_events("legacy-recovery")) == 1
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM remedies").fetchone() == (1,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
