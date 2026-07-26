from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from threading import Event, Thread

import pytest

from server.async_store import AsyncStoreOverloadedError
from server.models import ApprovalDecisionRequest, ExecutionMode
from server.orchestrator import RecoveryOrchestrator
from server.providers.hotel_simulator import HotelSimulator
from server.store import (
    ApprovalDecisionError,
    SQLiteStore,
    SQLiteStoreContentionError,
)


def _pending_claim(store: SQLiteStore):
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(store=store),
    )
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    approval = pending.recovery.pending_approval
    assert approval is not None
    request = ApprovalDecisionRequest(
        decision="approve",
        clientDecisionId="lease-takeover-decision",
        remedyId=approval.remedy_id,
        remedyDigest=approval.remedy_digest,
        toolCallId=approval.tool_call_id,
    )
    return store.claim_decision(pending.recovery.recovery_id, request)


def test_cancelled_blocked_lease_acquisition_cannot_orphan_ownership(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "cancelled-lease-acquisition.sqlite3")
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(store=store),
    )
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    approval = pending.recovery.pending_approval
    assert approval is not None
    request = ApprovalDecisionRequest(
        decision="approve",
        clientDecisionId="cancelled-lease-acquisition",
        remedyId=approval.remedy_id,
        remedyDigest=approval.remedy_digest,
        toolCallId=approval.tool_call_id,
    )
    claim = store.claim_decision(pending.recovery.recovery_id, request)
    entered = Event()
    release = Event()
    original_acquire = store.acquire_decision_resume
    original_release = store.release_decision_resume
    release_attempts = 0

    def blocked_acquire(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=2)
        return original_acquire(*args, **kwargs)

    def transient_release(*args, **kwargs):
        nonlocal release_attempts
        release_attempts += 1
        if release_attempts == 1:
            raise SQLiteStoreContentionError("transient on-worker contention")
        return original_release(*args, **kwargs)

    monkeypatch.setattr(store, "acquire_decision_resume", blocked_acquire)
    monkeypatch.setattr(store, "release_decision_resume", transient_release)

    async def exercise() -> None:
        acquisition = asyncio.create_task(orchestrator._acquire_resume_lease(claim))
        while not entered.is_set():
            await asyncio.sleep(0)
        acquisition.cancel()
        await asyncio.sleep(0)
        assert not acquisition.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await acquisition
        takeover = original_acquire(
            claim,
            resume_owner_id="post-cancel-owner",
            lease_duration=timedelta(seconds=30),
        )
        assert takeover.disposition == "owner"
        assert release_attempts == 2
        await orchestrator.store_io.shutdown()

    asyncio.run(exercise())


def test_cancelled_lease_fallback_retries_after_post_check_race(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "cancelled-lease-post-check.sqlite3")
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(store=store),
    )
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    approval = pending.recovery.pending_approval
    assert approval is not None
    request = ApprovalDecisionRequest(
        decision="approve",
        clientDecisionId="cancelled-lease-post-check",
        remedyId=approval.remedy_id,
        remedyDigest=approval.remedy_digest,
        toolCallId=approval.tool_call_id,
    )
    claim = store.claim_decision(pending.recovery.recovery_id, request)
    cancellation_check_completed = Event()
    allow_worker_return = Event()
    original_acquire_once = orchestrator._acquire_resume_lease_once
    original_release = store.release_decision_resume
    release_attempts = 0

    def return_after_cancellation_check(*args, **kwargs):
        result = original_acquire_once(*args, **kwargs)
        cancellation_check_completed.set()
        assert allow_worker_return.wait(timeout=2)
        return result

    def transient_release(*args, **kwargs):
        nonlocal release_attempts
        release_attempts += 1
        if release_attempts == 1:
            raise SQLiteStoreContentionError("transient fallback contention")
        return original_release(*args, **kwargs)

    monkeypatch.setattr(
        orchestrator,
        "_acquire_resume_lease_once",
        return_after_cancellation_check,
    )
    monkeypatch.setattr(store, "release_decision_resume", transient_release)

    async def exercise() -> None:
        acquisition = asyncio.create_task(orchestrator._acquire_resume_lease(claim))
        while not cancellation_check_completed.is_set():
            await asyncio.sleep(0)
        acquisition.cancel()
        allow_worker_return.set()
        with pytest.raises(asyncio.CancelledError):
            await acquisition
        takeover = store.acquire_decision_resume(
            claim,
            resume_owner_id="post-check-second-owner",
            lease_duration=timedelta(seconds=30),
        )
        assert takeover.disposition == "owner"
        assert release_attempts == 2
        await orchestrator.store_io.shutdown()

    asyncio.run(exercise())


def test_cancelled_lease_persistent_release_failure_overrides_cancellation(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "cancelled-lease-release-failure.sqlite3")
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(store=store),
    )
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    approval = pending.recovery.pending_approval
    assert approval is not None
    request = ApprovalDecisionRequest(
        decision="approve",
        clientDecisionId="cancelled-lease-release-failure",
        remedyId=approval.remedy_id,
        remedyDigest=approval.remedy_digest,
        toolCallId=approval.tool_call_id,
    )
    claim = store.claim_decision(pending.recovery.recovery_id, request)
    cancellation_check_completed = Event()
    allow_worker_return = Event()
    original_acquire_once = orchestrator._acquire_resume_lease_once
    expected = SQLiteStoreContentionError("persistent release contention")
    release_attempts = 0
    final_release_entered = Event()
    allow_final_failure = Event()

    def return_after_cancellation_check(*args, **kwargs):
        result = original_acquire_once(*args, **kwargs)
        cancellation_check_completed.set()
        assert allow_worker_return.wait(timeout=2)
        return result

    def persistent_release(*_args, **_kwargs):
        nonlocal release_attempts
        release_attempts += 1
        if release_attempts == 2:
            final_release_entered.set()
            assert allow_final_failure.wait(timeout=2)
        raise expected

    monkeypatch.setattr(
        orchestrator,
        "_acquire_resume_lease_once",
        return_after_cancellation_check,
    )
    monkeypatch.setattr(store, "release_decision_resume", persistent_release)

    async def exercise() -> None:
        acquisition = asyncio.create_task(orchestrator._acquire_resume_lease(claim))
        while not cancellation_check_completed.is_set():
            await asyncio.sleep(0)
        acquisition.cancel()
        allow_worker_return.set()
        while not final_release_entered.is_set():
            await asyncio.sleep(0)
        acquisition.cancel()
        await asyncio.sleep(0)
        assert not acquisition.done()
        allow_final_failure.set()
        with pytest.raises(SQLiteStoreContentionError) as raised:
            await acquisition
        assert raised.value is expected
        assert isinstance(raised.value.__cause__, asyncio.CancelledError)
        assert release_attempts == 2
        await orchestrator.store_io.shutdown()

    asyncio.run(exercise())


def test_cancelled_lease_nontransient_release_failure_is_not_retried(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "cancelled-lease-programming-failure.sqlite3")
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(store=store),
    )
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    approval = pending.recovery.pending_approval
    assert approval is not None
    request = ApprovalDecisionRequest(
        decision="approve",
        clientDecisionId="cancelled-lease-programming-failure",
        remedyId=approval.remedy_id,
        remedyDigest=approval.remedy_digest,
        toolCallId=approval.tool_call_id,
    )
    claim = store.claim_decision(pending.recovery.recovery_id, request)
    cancellation_check_completed = Event()
    allow_worker_return = Event()
    original_acquire_once = orchestrator._acquire_resume_lease_once
    expected = RuntimeError("nontransient release programming failure")
    release_attempts = 0

    def return_after_cancellation_check(*args, **kwargs):
        result = original_acquire_once(*args, **kwargs)
        cancellation_check_completed.set()
        assert allow_worker_return.wait(timeout=2)
        return result

    def fail_release(*_args, **_kwargs):
        nonlocal release_attempts
        release_attempts += 1
        raise expected

    monkeypatch.setattr(
        orchestrator,
        "_acquire_resume_lease_once",
        return_after_cancellation_check,
    )
    monkeypatch.setattr(store, "release_decision_resume", fail_release)

    async def exercise() -> None:
        acquisition = asyncio.create_task(orchestrator._acquire_resume_lease(claim))
        while not cancellation_check_completed.is_set():
            await asyncio.sleep(0)
        acquisition.cancel()
        allow_worker_return.set()
        with pytest.raises(RuntimeError) as raised:
            await acquisition
        assert raised.value is expected
        assert isinstance(raised.value.__cause__, asyncio.CancelledError)
        assert release_attempts == 1
        await orchestrator.store_io.shutdown()

    asyncio.run(exercise())


def test_cancelled_lease_transient_retry_yields_before_second_admission(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "cancelled-lease-yield-retry.sqlite3")
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(store=store),
        decision_wait_interval=0.001,
    )
    claim = _pending_claim(store)
    owner_id = "yield-retry-owner"
    lease = store.acquire_decision_resume(
        claim,
        resume_owner_id=owner_id,
        lease_duration=timedelta(seconds=30),
    )
    assert lease.disposition == "owner"
    original_control = orchestrator.store_io.control
    admission_cleared = False
    control_calls = 0

    def clear_admission() -> None:
        nonlocal admission_cleared
        admission_cleared = True

    async def transient_control(operation, /, *args, **kwargs):
        nonlocal control_calls
        control_calls += 1
        if control_calls == 1:
            asyncio.get_running_loop().call_soon(clear_admission)
            raise AsyncStoreOverloadedError("test-only transient overload")
        assert admission_cleared
        return await original_control(operation, *args, **kwargs)

    monkeypatch.setattr(orchestrator.store_io, "control", transient_control)

    async def exercise() -> None:
        await orchestrator._release_cancelled_resume_lease(
            lease,
            resume_owner_id=owner_id,
            attempts=2,
        )
        assert control_calls == 2
        takeover = store.acquire_decision_resume(
            claim,
            resume_owner_id="yield-retry-second-owner",
            lease_duration=timedelta(seconds=30),
        )
        assert takeover.disposition == "owner"
        await orchestrator.store_io.shutdown()

    asyncio.run(exercise())


@pytest.mark.parametrize("decision", ["approve", "decline"])
def test_failed_heartbeat_still_releases_approval_and_decline_lease(
    tmp_path,
    monkeypatch,
    decision,
) -> None:
    store = SQLiteStore(tmp_path / f"failed-heartbeat-{decision}.sqlite3")
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(store=store),
    )
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    approval = pending.recovery.pending_approval
    assert approval is not None
    request = ApprovalDecisionRequest(
        decision=decision,
        clientDecisionId=f"failed-heartbeat-{decision}",
        remedyId=approval.remedy_id,
        remedyDigest=approval.remedy_digest,
        toolCallId=approval.tool_call_id,
    )
    heartbeat_failed = asyncio.Event()
    expected = RuntimeError(f"{decision}-heartbeat-failed")

    async def fail_heartbeat(*_args, **_kwargs) -> None:
        heartbeat_failed.set()
        raise expected

    async def fail_resume(*_args, **_kwargs):
        await heartbeat_failed.wait()
        raise RuntimeError(f"{decision}-body-failed")

    monkeypatch.setattr(orchestrator, "_heartbeat_resume_lease", fail_heartbeat)
    monkeypatch.setattr(
        orchestrator,
        (
            "_resume_claimed_approval"
            if decision == "approve"
            else "_resume_claimed_decline"
        ),
        fail_resume,
    )

    async def exercise() -> None:
        decide = (
            orchestrator.approve_decision
            if decision == "approve"
            else orchestrator.decline_decision
        )
        with pytest.raises(RuntimeError) as raised:
            await decide(pending.recovery.recovery_id, request)
        assert raised.value is expected
        claim = store.claim_decision(pending.recovery.recovery_id, request)
        takeover = store.acquire_decision_resume(
            claim,
            resume_owner_id=f"second-owner-{decision}",
            lease_duration=timedelta(seconds=30),
        )
        assert takeover.disposition == "owner"
        await orchestrator.store_io.shutdown()

    asyncio.run(exercise())


def test_heartbeat_join_preserves_caller_cancellation_until_task_settles() -> None:
    cleanup_entered = Event()
    cleanup_release = Event()

    async def heartbeat() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleanup_entered.set()
            await asyncio.to_thread(cleanup_release.wait)
            raise

    async def exercise() -> None:
        heartbeat_task = asyncio.create_task(heartbeat())
        await asyncio.sleep(0)
        join = asyncio.create_task(
            RecoveryOrchestrator._cancel_heartbeat(heartbeat_task)
        )
        while not cleanup_entered.is_set():
            await asyncio.sleep(0)
        join.cancel()
        await asyncio.sleep(0)
        assert not join.done()
        cleanup_release.set()
        with pytest.raises(asyncio.CancelledError):
            await join
        assert heartbeat_task.cancelled()

    asyncio.run(exercise())


def test_cancelled_heartbeat_release_waits_for_one_failing_worker_call(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "heartbeat-release-cancel-failure.sqlite3")
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(store=store),
    )
    claim = _pending_claim(store)
    owner_id = "heartbeat-release-failure-owner"
    lease = store.acquire_decision_resume(
        claim,
        resume_owner_id=owner_id,
        lease_duration=timedelta(seconds=30),
    )
    assert lease.disposition == "owner"
    entered = Event()
    release = Event()
    expected = RuntimeError("heartbeat-release-worker-failed")
    release_calls = 0

    def blocked_failing_release(*_args, **_kwargs) -> None:
        nonlocal release_calls
        release_calls += 1
        entered.set()
        assert release.wait(timeout=2)
        raise expected

    monkeypatch.setattr(
        store,
        "release_decision_resume",
        blocked_failing_release,
    )

    async def heartbeat() -> None:
        await asyncio.Event().wait()

    async def exercise() -> None:
        heartbeat_task = asyncio.create_task(heartbeat())
        await asyncio.sleep(0)
        stop = asyncio.create_task(
            orchestrator._stop_heartbeat_and_release(
                heartbeat_task,
                claim,
                resume_owner_id=owner_id,
                resume_generation=lease.resume_generation,
            )
        )
        while not entered.is_set():
            await asyncio.sleep(0)
        stop.cancel("first-caller-cancel")
        await asyncio.sleep(0)
        assert not stop.done()
        stop.cancel("second-caller-cancel")
        await asyncio.sleep(0)
        assert not stop.done()
        release.set()
        with pytest.raises(RuntimeError) as raised:
            await stop
        assert raised.value is expected
        assert isinstance(raised.value.__cause__, asyncio.CancelledError)
        assert raised.value.__cause__.args == ("first-caller-cancel",)
        assert release_calls == 1
        await orchestrator.store_io.shutdown()

    asyncio.run(exercise())


def test_cancelled_heartbeat_release_restores_cancellation_after_one_worker_call(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "heartbeat-release-cancel-success.sqlite3")
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(store=store),
    )
    claim = _pending_claim(store)
    owner_id = "heartbeat-release-success-owner"
    lease = store.acquire_decision_resume(
        claim,
        resume_owner_id=owner_id,
        lease_duration=timedelta(seconds=30),
    )
    assert lease.disposition == "owner"
    entered = Event()
    release = Event()
    original_release = store.release_decision_resume
    release_calls = 0

    def blocked_successful_release(*args, **kwargs) -> None:
        nonlocal release_calls
        release_calls += 1
        entered.set()
        assert release.wait(timeout=2)
        original_release(*args, **kwargs)

    monkeypatch.setattr(
        store,
        "release_decision_resume",
        blocked_successful_release,
    )

    async def heartbeat() -> None:
        await asyncio.Event().wait()

    async def exercise() -> None:
        heartbeat_task = asyncio.create_task(heartbeat())
        await asyncio.sleep(0)
        stop = asyncio.create_task(
            orchestrator._stop_heartbeat_and_release(
                heartbeat_task,
                claim,
                resume_owner_id=owner_id,
                resume_generation=lease.resume_generation,
            )
        )
        while not entered.is_set():
            await asyncio.sleep(0)
        stop.cancel("first-caller-cancel")
        await asyncio.sleep(0)
        assert not stop.done()
        stop.cancel("second-caller-cancel")
        await asyncio.sleep(0)
        assert not stop.done()
        release.set()
        with pytest.raises(asyncio.CancelledError) as raised:
            await stop
        assert raised.value.args == ("first-caller-cancel",)
        assert release_calls == 1
        takeover = store.acquire_decision_resume(
            claim,
            resume_owner_id="heartbeat-release-success-takeover",
            lease_duration=timedelta(seconds=30),
        )
        assert takeover.disposition == "owner"
        await orchestrator.store_io.shutdown()

    asyncio.run(exercise())


def test_blocked_startup_reconciliation_keeps_loop_live_and_shutdown_joins(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "blocked-startup-reconciliation.sqlite3")
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(store=store),
    )
    entered = Event()
    release = Event()
    attempts = 0

    def blocked_reconciliation():
        nonlocal attempts
        attempts += 1
        entered.set()
        assert release.wait(timeout=2)
        return None

    monkeypatch.setattr(
        orchestrator,
        "_reconcile_startup_once",
        blocked_reconciliation,
    )

    async def exercise() -> None:
        await orchestrator.startup()
        while not entered.is_set():
            await asyncio.sleep(0)
        assert await asyncio.wait_for(
            asyncio.sleep(0.01, result="responsive"),
            timeout=0.2,
        ) == "responsive"
        shutdown = asyncio.create_task(orchestrator.shutdown())
        await asyncio.sleep(0)
        assert not shutdown.done()
        release.set()
        await shutdown
        assert attempts == 1
        await orchestrator.store_io.shutdown()

    asyncio.run(exercise())


def test_orchestrator_nested_same_loop_lifecycles_stop_only_for_final_owner(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "orchestrator-nested-lifecycle.sqlite3")
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(store=store),
    )

    async def exercise() -> None:
        blocked = asyncio.Event()

        async def blocked_reconciliation() -> None:
            await blocked.wait()

        monkeypatch.setattr(
            orchestrator,
            "_run_startup_reconciliation",
            blocked_reconciliation,
        )
        first_owner = object()
        second_owner = object()
        await orchestrator.startup(first_owner)
        reconciliation = orchestrator._reconciliation_task
        assert reconciliation is not None
        await orchestrator.startup(second_owner)
        await orchestrator.shutdown(first_owner)
        assert not reconciliation.done()
        await orchestrator.shutdown(second_owner)
        assert reconciliation.done()
        assert orchestrator._lifecycle_loop is None
        await orchestrator.store_io.shutdown()

    asyncio.run(exercise())


def test_orchestrator_rejects_concurrent_cross_loop_reuse_and_reopens_later(
    tmp_path,
) -> None:
    store = SQLiteStore(tmp_path / "orchestrator-cross-loop.sqlite3")
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(store=store),
    )
    first_owner = object()
    second_owner = object()
    third_owner = object()
    started = Event()
    release = Event()
    thread_errors: list[BaseException] = []

    def active_loop() -> None:
        async def exercise() -> None:
            await orchestrator.startup(first_owner)
            started.set()
            await asyncio.to_thread(release.wait)
            await orchestrator.shutdown(first_owner)

        try:
            asyncio.run(exercise())
        except BaseException as error:
            thread_errors.append(error)

    active = Thread(target=active_loop, name="orchestrator-active-loop")
    active.start()
    assert started.wait(timeout=2)
    try:
        with pytest.raises(RuntimeError, match="concurrent event loops"):
            asyncio.run(orchestrator.startup(second_owner))
    finally:
        release.set()
        active.join(timeout=3)
    assert not active.is_alive()
    assert thread_errors == []

    async def reopened() -> None:
        await orchestrator.startup(third_owner)
        await orchestrator.shutdown(third_owner)
        await orchestrator.store_io.shutdown()

    asyncio.run(reopened())


def test_orchestrator_startup_rolls_back_owner_when_scheduling_fails(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "orchestrator-startup-rollback.sqlite3")
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(store=store),
    )
    original_schedule = orchestrator._schedule_startup_reconciliation

    def fail_schedule() -> None:
        raise RuntimeError("schedule-failed")

    async def exercise() -> None:
        monkeypatch.setattr(
            orchestrator,
            "_schedule_startup_reconciliation",
            fail_schedule,
        )
        with pytest.raises(RuntimeError, match="schedule-failed"):
            await orchestrator.startup(object())
        assert orchestrator._lifecycle_loop is None
        assert orchestrator._lifecycle_owners == set()

        monkeypatch.setattr(
            orchestrator,
            "_schedule_startup_reconciliation",
            original_schedule,
        )
        owner = object()
        await orchestrator.startup(owner)
        await orchestrator.shutdown(owner)
        await orchestrator.store_io.shutdown()

    asyncio.run(exercise())


def test_expired_owner_takeover_fences_stale_provider_guard_and_heartbeat(
    tmp_path,
) -> None:
    database_path = tmp_path / "decision-resume-lease.sqlite3"
    first_store = SQLiteStore(database_path)
    claim = _pending_claim(first_store)
    second_store = SQLiteStore(database_path)
    started_at = datetime(2026, 7, 19, 8, 0, tzinfo=UTC)
    lease_duration = timedelta(seconds=30)

    first = first_store.acquire_decision_resume(
        claim,
        resume_owner_id="owner-a",
        lease_duration=lease_duration,
        now=started_at,
    )
    assert first.disposition == "owner"
    assert first.resume_generation == 1
    assert second_store.acquire_decision_resume(
        claim,
        resume_owner_id="owner-b",
        lease_duration=lease_duration,
        now=started_at + timedelta(seconds=20),
    ).disposition == "wait"

    assert first_store.renew_decision_resume(
        claim,
        resume_owner_id="owner-a",
        resume_generation=first.resume_generation,
        lease_duration=lease_duration,
        now=started_at + timedelta(seconds=20),
    )
    assert second_store.acquire_decision_resume(
        claim,
        resume_owner_id="owner-b",
        lease_duration=lease_duration,
        now=started_at + timedelta(seconds=35),
    ).disposition == "wait"

    takeover = second_store.acquire_decision_resume(
        claim,
        resume_owner_id="owner-b",
        lease_duration=lease_duration,
        now=started_at + timedelta(seconds=51),
    )
    assert takeover.disposition == "owner"
    assert takeover.resume_generation == 2
    envelope = second_store.get_pending_approval(claim.recovery_id)

    with pytest.raises(ApprovalDecisionError, match="resume_owner_lost"):
        first_store.assert_provider_dispatch_authorized(
            recovery_id=claim.recovery_id,
            tool_call_id=claim.request.tool_call_id,
            remedy_digest=claim.request.remedy_digest,
            action_digest=envelope.action_digest,
            resume_owner_id="owner-a",
            resume_generation=first.resume_generation,
            now=started_at + timedelta(seconds=52),
        )
    second_store.assert_provider_dispatch_authorized(
        recovery_id=claim.recovery_id,
        tool_call_id=claim.request.tool_call_id,
        remedy_digest=claim.request.remedy_digest,
        action_digest=envelope.action_digest,
        resume_owner_id="owner-b",
        resume_generation=takeover.resume_generation,
        now=started_at + timedelta(seconds=52),
    )


def test_provider_guard_samples_clock_after_write_transaction_begins(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "decision-provider-guard-clock.sqlite3"
    store = SQLiteStore(database_path)
    claim = _pending_claim(store)
    started_at = datetime(2026, 7, 19, 8, 0, tzinfo=UTC)
    lease = store.acquire_decision_resume(
        claim,
        resume_owner_id="clock-owner",
        lease_duration=timedelta(seconds=30),
        now=started_at,
    )
    envelope = store.get_pending_approval(claim.recovery_id)
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
        return started_at + timedelta(seconds=31)

    monkeypatch.setattr(store, "_now", expired_now)
    errors: list[BaseException] = []

    def run_guard() -> None:
        try:
            store.assert_provider_dispatch_authorized(
                recovery_id=claim.recovery_id,
                tool_call_id=claim.request.tool_call_id,
                remedy_digest=claim.request.remedy_digest,
                action_digest=envelope.action_digest,
                resume_owner_id="clock-owner",
                resume_generation=lease.resume_generation,
            )
        except BaseException as error:
            errors.append(error)

    worker = Thread(target=run_guard)
    worker.start()
    assert begin_attempted.wait(timeout=2)
    assert not clock_sampled.is_set()
    allow_begin.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert clock_sampled.is_set()
    assert len(errors) == 1
    assert isinstance(errors[0], ApprovalDecisionError)
    assert errors[0].code == "resume_owner_lost"
