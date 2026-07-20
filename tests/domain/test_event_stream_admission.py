import asyncio
import threading
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor

import pytest

from server.events import (
    EventStreamAdmissionController,
    lease_event_stream,
)


def test_event_stream_admission_is_atomic_across_threads_and_limits_each_recovery() -> None:
    controller = EventStreamAdmissionController(
        max_active=16,
        max_per_recovery=4,
        retry_seconds=5,
    )
    worker_count = 48
    start = threading.Barrier(worker_count)
    release_winners = threading.Event()
    recorded = threading.Condition()
    results: list[tuple[str, object | None]] = []

    def attempt(index: int) -> None:
        recovery_id = f"recovery-{index % 6}"
        start.wait()
        lease = controller.try_acquire(recovery_id)
        with recorded:
            results.append((recovery_id, lease))
            recorded.notify_all()
        if lease is not None:
            release_winners.wait(timeout=5)
            lease.release()

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(attempt, index) for index in range(worker_count)]
        with recorded:
            assert recorded.wait_for(lambda: len(results) == worker_count, timeout=5)

        admitted = [item for item in results if item[1] is not None]
        assert len(admitted) == 16
        assert controller.active_count == 16
        assert controller.active_recovery_count <= 6
        for recovery_id in {item[0] for item in admitted}:
            assert 1 <= controller.active_for(recovery_id) <= 4

        release_winners.set()
        for future in futures:
            future.result(timeout=5)

    assert controller.active_count == 0
    assert controller.active_recovery_count == 0


def test_event_stream_lease_release_is_idempotent_and_removes_zero_entries() -> None:
    controller = EventStreamAdmissionController(
        max_active=1,
        max_per_recovery=1,
        retry_seconds=5,
    )
    lease = controller.try_acquire("recovery-a")

    assert lease is not None
    assert controller.try_acquire("recovery-a") is None
    assert controller.active_count == 1
    assert controller.active_recovery_count == 1

    lease.release()
    lease.release()

    assert controller.active_count == 0
    assert controller.active_for("recovery-a") == 0
    assert controller.active_recovery_count == 0
    reacquired = controller.try_acquire("recovery-a")
    assert reacquired is not None
    reacquired.release()


@pytest.mark.parametrize("completion", ["terminal", "disconnect"])
def test_leased_event_stream_releases_after_normal_completion(completion: str) -> None:
    controller = EventStreamAdmissionController(
        max_active=1,
        max_per_recovery=1,
        retry_seconds=5,
    )
    lease = controller.try_acquire("recovery-a")
    assert lease is not None

    async def source() -> AsyncIterator[str]:
        if completion == "terminal":
            yield "id: 1\n\n"
        return

    async def collect() -> list[str]:
        return [chunk async for chunk in lease_event_stream(source(), lease)]

    asyncio.run(collect())
    assert controller.active_count == 0
    reacquired = controller.try_acquire("recovery-a")
    assert reacquired is not None
    reacquired.release()


def test_leased_event_stream_releases_after_iterator_error() -> None:
    controller = EventStreamAdmissionController(
        max_active=1,
        max_per_recovery=1,
        retry_seconds=5,
    )
    lease = controller.try_acquire("recovery-a")
    assert lease is not None

    async def broken_source() -> AsyncIterator[str]:
        yield "id: 1\n\n"
        raise RuntimeError("store or encoding failure")

    async def collect() -> None:
        async for _chunk in lease_event_stream(broken_source(), lease):
            pass

    with pytest.raises(RuntimeError, match="store or encoding failure"):
        asyncio.run(collect())
    assert controller.active_count == 0
    reacquired = controller.try_acquire("recovery-a")
    assert reacquired is not None
    reacquired.release()


def test_leased_event_stream_releases_when_iteration_is_cancelled() -> None:
    controller = EventStreamAdmissionController(
        max_active=1,
        max_per_recovery=1,
        retry_seconds=5,
    )
    lease = controller.try_acquire("recovery-a")
    assert lease is not None

    async def run() -> None:
        entered = asyncio.Event()

        async def waiting_source() -> AsyncIterator[str]:
            entered.set()
            await asyncio.Event().wait()
            yield "unreachable"

        stream = lease_event_stream(waiting_source(), lease)
        task = asyncio.create_task(anext(stream))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert controller.active_count == 0
    reacquired = controller.try_acquire("recovery-a")
    assert reacquired is not None
    reacquired.release()
