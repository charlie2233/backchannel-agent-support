from __future__ import annotations

import asyncio
import gc
from threading import Event, Lock

import pytest

from server.async_store import (
    AsyncSQLiteStore,
    AsyncStoreClosedError,
    AsyncStoreOverloadedError,
)
from server.store import SQLiteStore


def test_blocked_sqlite_work_keeps_the_event_loop_live() -> None:
    adapter = AsyncSQLiteStore(max_pending=4)
    entered = Event()
    release = Event()

    def blocked() -> str:
        entered.set()
        assert release.wait(timeout=2)
        return "completed"

    async def exercise() -> None:
        task = asyncio.create_task(adapter.run(blocked))
        while not entered.is_set():
            await asyncio.sleep(0)
        loop_probe = asyncio.create_task(asyncio.sleep(0.01, result="responsive"))
        assert await asyncio.wait_for(loop_probe, timeout=0.1) == "responsive"
        release.set()
        assert await task == "completed"
        await adapter.shutdown()

    asyncio.run(exercise())


def test_cancellation_joins_the_exact_submitted_operation_before_propagating() -> None:
    adapter = AsyncSQLiteStore(max_pending=1, reserved_control=0)
    entered = Event()
    release = Event()
    completed = Event()
    call_count = 0
    count_lock = Lock()

    def blocked_mutation() -> None:
        nonlocal call_count
        with count_lock:
            call_count += 1
        entered.set()
        assert release.wait(timeout=2)
        completed.set()

    async def exercise() -> None:
        task = asyncio.create_task(
            adapter.mutate(
                blocked_mutation,
            )
        )
        while not entered.is_set():
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        with pytest.raises(AsyncStoreOverloadedError):
            await adapter.control(
                lambda: None,
            )
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert completed.is_set()
        assert call_count == 1
        await adapter.shutdown()

    asyncio.run(exercise())


def test_cancellation_does_not_remove_an_already_queued_mutation() -> None:
    adapter = AsyncSQLiteStore(max_pending=4, reserved_control=1)
    first_entered = Event()
    first_release = Event()
    second_completed = Event()
    second_calls = 0

    def first_mutation() -> None:
        first_entered.set()
        assert first_release.wait(timeout=2)

    def second_mutation() -> None:
        nonlocal second_calls
        second_calls += 1
        second_completed.set()

    async def exercise() -> None:
        first = asyncio.create_task(
            adapter.mutate(first_mutation)
        )
        while not first_entered.is_set():
            await asyncio.sleep(0)
        second = asyncio.create_task(
            adapter.mutate(second_mutation)
        )
        await asyncio.sleep(0)
        second.cancel()
        first_release.set()
        await first
        with pytest.raises(asyncio.CancelledError):
            await second
        assert second_completed.is_set()
        assert second_calls == 1
        await adapter.shutdown()

    asyncio.run(exercise())


def test_read_capacity_reserves_a_bounded_slot_for_control_work() -> None:
    adapter = AsyncSQLiteStore(max_pending=2, reserved_control=1)
    read_entered = Event()
    read_release = Event()
    control_completed = Event()

    def blocked_read() -> str:
        read_entered.set()
        assert read_release.wait(timeout=2)
        return "read"

    def control_mutation() -> str:
        control_completed.set()
        return "control"

    async def exercise() -> None:
        first_read = asyncio.create_task(
            adapter.read(blocked_read)
        )
        while not read_entered.is_set():
            await asyncio.sleep(0)
        with pytest.raises(AsyncStoreOverloadedError):
            await adapter.read(lambda: "second-read")
        control = asyncio.create_task(
            adapter.control(control_mutation)
        )
        await asyncio.sleep(0)
        assert not control.done()
        read_release.set()
        assert await first_read == "read"
        assert await control == "control"
        assert control_completed.is_set()
        await adapter.shutdown()

    asyncio.run(exercise())


def test_mutation_flood_cannot_consume_reserved_control_capacity() -> None:
    adapter = AsyncSQLiteStore(max_pending=3, reserved_control=1)
    first_entered = Event()
    first_release = Event()
    order: list[str] = []

    def first_mutation() -> str:
        order.append("first")
        first_entered.set()
        assert first_release.wait(timeout=2)
        return "first"

    def second_mutation() -> str:
        order.append("second")
        return "second"

    def control_mutation() -> str:
        order.append("control")
        return "control"

    async def exercise() -> None:
        first = asyncio.create_task(adapter.mutate(first_mutation))
        while not first_entered.is_set():
            await asyncio.sleep(0)
        second = asyncio.create_task(adapter.mutate(second_mutation))
        await asyncio.sleep(0)
        with pytest.raises(AsyncStoreOverloadedError):
            await adapter.mutate(lambda: "third")
        control = asyncio.create_task(adapter.control(control_mutation))
        await asyncio.sleep(0)
        first_release.set()
        assert await first == "first"
        assert await control == "control"
        assert await second == "second"
        assert order == ["first", "control", "second"]
        await adapter.shutdown()

    asyncio.run(exercise())


def test_control_work_overtakes_queued_stream_reads() -> None:
    adapter = AsyncSQLiteStore(max_pending=3, reserved_control=1)
    first_entered = Event()
    first_release = Event()
    order: list[str] = []

    def first_read() -> str:
        order.append("first")
        first_entered.set()
        assert first_release.wait(timeout=2)
        return "first"

    def second_read() -> str:
        order.append("second")
        return "second"

    def control() -> str:
        order.append("control")
        return "control"

    async def exercise() -> None:
        first = asyncio.create_task(
            adapter.stream(first_read)
        )
        while not first_entered.is_set():
            await asyncio.sleep(0)
        second = asyncio.create_task(
            adapter.stream(second_read)
        )
        await asyncio.sleep(0)
        control_task = asyncio.create_task(
            adapter.control(control)
        )
        await asyncio.sleep(0)
        first_release.set()
        assert await first == "first"
        assert await control_task == "control"
        assert await second == "second"
        assert order == ["first", "control", "second"]
        await adapter.shutdown()

    asyncio.run(exercise())


def test_adapter_can_be_used_from_distinct_event_loops() -> None:
    adapter = AsyncSQLiteStore(max_pending=2)

    assert asyncio.run(adapter.run(lambda: 1)) == 1
    assert asyncio.run(adapter.run(lambda: 2)) == 2
    asyncio.run(adapter.shutdown())


def test_fast_operations_cannot_deadlock_callback_registration() -> None:
    adapter = AsyncSQLiteStore(max_pending=2)

    async def exercise() -> None:
        for expected in range(100):
            assert await adapter.run(lambda value=expected: value) == expected
        await adapter.shutdown()

    asyncio.run(asyncio.wait_for(exercise(), timeout=5))


def test_operation_exception_identity_is_preserved() -> None:
    adapter = AsyncSQLiteStore(max_pending=2)
    expected = ValueError("exact-operation-failure")

    def fail() -> None:
        raise expected

    async def exercise() -> None:
        with pytest.raises(ValueError) as raised:
            await adapter.run(fail)
        assert raised.value is expected
        await adapter.shutdown()

    asyncio.run(exercise())


def test_worker_originated_cancelled_error_is_not_mistaken_for_caller_cancellation() -> None:
    adapter = AsyncSQLiteStore(max_pending=2)
    expected = asyncio.CancelledError("worker-originated")

    def fail() -> None:
        raise expected

    async def exercise() -> None:
        with pytest.raises(asyncio.CancelledError) as raised:
            await adapter.run(fail)
        assert raised.value is expected
        assert await adapter.run(lambda: "still-usable") == "still-usable"
        await adapter.shutdown()

    asyncio.run(exercise())


def test_caller_cancellation_consumes_a_later_worker_exception() -> None:
    adapter = AsyncSQLiteStore(max_pending=2)
    entered = Event()
    release = Event()
    expected = RuntimeError("worker-failed-after-caller-cancelled")

    def fail_after_release() -> None:
        entered.set()
        assert release.wait(timeout=2)
        raise expected

    async def exercise() -> None:
        loop = asyncio.get_running_loop()
        unhandled_contexts: list[dict[str, object]] = []
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(
            lambda _loop, context: unhandled_contexts.append(context)
        )
        try:
            operation = asyncio.create_task(adapter.run(fail_after_release))
            while not entered.is_set():
                await asyncio.sleep(0)
            operation.cancel("caller-cancelled")
            release.set()
            with pytest.raises(asyncio.CancelledError) as raised:
                await operation
            assert raised.value.args == ("caller-cancelled",)
            await adapter.shutdown()
            await asyncio.sleep(0)
            gc.collect()
            await asyncio.sleep(0)
            assert [
                context
                for context in unhandled_contexts
                if context.get("message") == "Future exception was never retrieved"
            ] == []
        finally:
            loop.set_exception_handler(previous_handler)

    asyncio.run(exercise())


def test_sqlite_store_has_exactly_one_canonical_async_adapter(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "canonical-adapter.sqlite3")
    canonical = AsyncSQLiteStore.for_store(store)

    assert AsyncSQLiteStore.for_store(store) is canonical
    assert canonical.is_bound_to(store)
    with pytest.raises(
        ValueError,
        match="already has a canonical async adapter",
    ):
        AsyncSQLiteStore(store=store)
    asyncio.run(canonical.shutdown())


def test_lifespan_shutdown_drains_and_allows_a_later_lifespan() -> None:
    adapter = AsyncSQLiteStore(max_pending=2)

    async def first_lifespan() -> None:
        await adapter.startup()
        assert await adapter.run(lambda: "first") == "first"
        await adapter.shutdown()
        with pytest.raises(AsyncStoreClosedError):
            await adapter.run(lambda: "closed")

    async def second_lifespan() -> None:
        await adapter.startup()
        assert await adapter.run(lambda: "second") == "second"
        await adapter.shutdown()

    asyncio.run(first_lifespan())
    asyncio.run(second_lifespan())


def test_nested_lifespans_close_only_after_the_final_owner_exits() -> None:
    adapter = AsyncSQLiteStore(max_pending=2)
    first_owner = object()
    second_owner = object()

    async def exercise() -> None:
        await adapter.startup(first_owner)
        await adapter.startup(second_owner)
        await adapter.shutdown(first_owner)
        assert await adapter.run(lambda: "still-open") == "still-open"
        await adapter.shutdown(second_owner)
        with pytest.raises(AsyncStoreClosedError):
            await adapter.run(lambda: "closed")

    asyncio.run(exercise())


def test_cancelled_shutdown_still_drains_before_it_propagates() -> None:
    adapter = AsyncSQLiteStore(max_pending=2)
    entered = Event()
    release = Event()
    completed = Event()

    def blocked() -> None:
        entered.set()
        assert release.wait(timeout=2)
        completed.set()

    async def exercise() -> None:
        await adapter.startup()
        work = asyncio.create_task(adapter.run(blocked))
        while not entered.is_set():
            await asyncio.sleep(0)
        shutdown = asyncio.create_task(adapter.shutdown())
        await asyncio.sleep(0)
        shutdown.cancel()
        release.set()
        await work
        with pytest.raises(asyncio.CancelledError):
            await shutdown
        assert completed.is_set()
        with pytest.raises(AsyncStoreClosedError):
            await adapter.run(lambda: None)

    asyncio.run(exercise())


def test_shutdown_rejects_queued_work_and_joins_only_the_active_operation() -> None:
    adapter = AsyncSQLiteStore(max_pending=2, reserved_control=0)
    entered = Event()
    release = Event()
    queued_calls = 0

    def active() -> None:
        entered.set()
        assert release.wait(timeout=2)

    def queued() -> None:
        nonlocal queued_calls
        queued_calls += 1

    async def exercise() -> None:
        await adapter.startup()
        active_task = asyncio.create_task(adapter.run(active))
        while not entered.is_set():
            await asyncio.sleep(0)
        queued_task = asyncio.create_task(adapter.run(queued))
        await asyncio.sleep(0)
        shutdown = asyncio.create_task(adapter.shutdown())
        await asyncio.sleep(0)
        with pytest.raises(AsyncStoreClosedError):
            await queued_task
        assert queued_calls == 0
        assert not shutdown.done()
        release.set()
        await active_task
        await shutdown

    asyncio.run(exercise())
