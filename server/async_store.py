"""Bounded, cancellation-safe async access to the synchronous SQLite store."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import IntEnum
from functools import partial
from heapq import heapify, heappop, heappush
from itertools import count
from threading import BoundedSemaphore, Lock, RLock
from typing import ParamSpec, TypeVar, cast
from weakref import ReferenceType, WeakKeyDictionary, ref

P = ParamSpec("P")
T = TypeVar("T")

_STORE_ADAPTERS: WeakKeyDictionary[object, AsyncSQLiteStore] = WeakKeyDictionary()
_STORE_ADAPTERS_LOCK = RLock()


class RetryableStoreAccessError(RuntimeError):
    """Base for app-owned store access failures that callers may retry safely."""


class AsyncStoreOverloadedError(RetryableStoreAccessError):
    """Raised before submission when the bounded SQLite lane is full."""


class AsyncStoreClosedError(RetryableStoreAccessError):
    """Raised when work is submitted outside an active adapter lifecycle."""


class AsyncStorePriority(IntEnum):
    """Classify work so ordinary traffic cannot consume reserved control capacity."""

    CONTROL = 0
    MUTATION = 1
    READ = 2
    STREAM = 3
    MAINTENANCE = 4


@dataclass(order=True, slots=True)
class _QueuedOperation:
    priority: int
    sequence: int
    operation: Callable[[], object] = field(compare=False)
    completion: Future[object] = field(compare=False)
    non_control_slot: bool = field(compare=False)


class AsyncSQLiteStore:
    """Run synchronous SQLite calls on one bounded, lifecycle-owned worker.

    The adapter deliberately uses only thread synchronization for admission and
    lifecycle state, so the same object remains usable across distinct event
    loops in tests. Once submitted, an operation is shielded and joined even if
    its caller is cancelled; this prevents detached claims, writes, and leases.
    """

    def __init__(
        self,
        *,
        store: object | None = None,
        max_pending: int = 64,
        reserved_control: int | None = None,
        thread_name_prefix: str = "backchannel-sqlite",
    ) -> None:
        if max_pending < 1:
            raise ValueError("Async SQLite pending capacity must be positive")
        if reserved_control is None:
            reserved_control = min(16, max_pending - 1)
        if reserved_control < 0 or reserved_control >= max_pending:
            raise ValueError(
                "Async SQLite reserved control capacity must be smaller than capacity"
            )
        self._max_pending = max_pending
        self._reserved_control = reserved_control
        self._thread_name_prefix = thread_name_prefix
        self._store_ref: ReferenceType[object] | None = (
            ref(store) if store is not None else None
        )
        self._capacity = BoundedSemaphore(max_pending)
        self._non_control_capacity = BoundedSemaphore(max_pending - reserved_control)
        self._lock = Lock()
        self._executor: ThreadPoolExecutor | None = self._new_executor()
        self._sequence = count()
        self._queue: list[_QueuedOperation] = []
        self._worker_active = False
        self._running: _QueuedOperation | None = None
        self._lifecycle_owners: set[object] = set()
        self._default_owner = object()
        self._closing = False
        self._closed = False
        self._shutdown_completion: Future[None] | None = None
        if store is not None:
            with _STORE_ADAPTERS_LOCK:
                existing = _STORE_ADAPTERS.get(store)
                if existing is not None:
                    self._executor.shutdown(wait=False, cancel_futures=True)
                    self._executor = None
                    raise ValueError(
                        "SQLiteStore already has a canonical async adapter"
                    )
                _STORE_ADAPTERS[store] = self

    @classmethod
    def for_store(cls, store: object) -> AsyncSQLiteStore:
        """Return the one canonical adapter bound to a SQLiteStore instance."""

        with _STORE_ADAPTERS_LOCK:
            existing = _STORE_ADAPTERS.get(store)
            if existing is not None:
                return existing
            return cls(store=store)

    def is_bound_to(self, store: object) -> bool:
        """Return whether this adapter is the canonical lane for ``store``."""

        bound = self._store_ref() if self._store_ref is not None else None
        if bound is not store:
            return False
        with _STORE_ADAPTERS_LOCK:
            return _STORE_ADAPTERS.get(store) is self

    def _new_executor(self) -> ThreadPoolExecutor:
        return ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=self._thread_name_prefix,
        )

    @staticmethod
    async def _join(future: Future[T]) -> T:
        """Join one submitted operation without allowing caller cancellation to detach it."""

        wrapped = asyncio.wrap_future(future)
        cancellation: asyncio.CancelledError | None = None
        while not wrapped.done():
            try:
                await asyncio.shield(wrapped)
            except asyncio.CancelledError as error:
                task = asyncio.current_task()
                if task is not None and task.cancelling() > 0:
                    if cancellation is None:
                        cancellation = error
                    continue
                break
            except BaseException:
                break
        try:
            result = wrapped.result()
        except BaseException:
            if cancellation is not None:
                raise cancellation from None
            raise
        if cancellation is not None:
            raise cancellation
        return result

    def _release_capacity(self, *, non_control_slot: bool) -> None:
        self._capacity.release()
        if non_control_slot:
            self._non_control_capacity.release()

    def _run_worker(self) -> None:
        """Drain the priority heap on the single dedicated executor worker."""

        while True:
            with self._lock:
                if not self._queue:
                    self._running = None
                    self._worker_active = False
                    return
                queued = heappop(self._queue)
                self._running = queued
            try:
                result = queued.operation()
            except BaseException as error:
                self._release_capacity(non_control_slot=queued.non_control_slot)
                queued.completion.set_exception(error)
            else:
                self._release_capacity(non_control_slot=queued.non_control_slot)
                queued.completion.set_result(result)
            finally:
                with self._lock:
                    if self._running is queued:
                        self._running = None

    async def _run_bound(
        self,
        operation: Callable[[], T],
        *,
        priority: AsyncStorePriority,
    ) -> T:
        """Submit one exact call, or fail before submission when capacity is exhausted."""

        non_control_slot = priority != AsyncStorePriority.CONTROL
        if non_control_slot and not self._non_control_capacity.acquire(blocking=False):
            raise AsyncStoreOverloadedError(
                "Async SQLite non-control capacity is exhausted"
            )
        if not self._capacity.acquire(blocking=False):
            if non_control_slot:
                self._non_control_capacity.release()
            raise AsyncStoreOverloadedError("Async SQLite capacity is exhausted")

        completion: Future[object] = Future()
        queued = _QueuedOperation(
            priority=int(priority),
            sequence=next(self._sequence),
            operation=operation,
            completion=completion,
            non_control_slot=non_control_slot,
        )
        with self._lock:
            executor = self._executor
            if self._closing or self._closed or executor is None:
                self._release_capacity(non_control_slot=non_control_slot)
                raise AsyncStoreClosedError("Async SQLite adapter is closed")
            heappush(self._queue, queued)
            if not self._worker_active:
                self._worker_active = True
                try:
                    executor.submit(self._run_worker)
                except BaseException:
                    self._worker_active = False
                    self._queue.remove(queued)
                    heapify(self._queue)
                    self._release_capacity(non_control_slot=non_control_slot)
                    raise
        result = await self._join(completion)
        return cast(T, result)

    async def run(
        self,
        operation: Callable[P, T],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> T:
        """Run ordinary mutation work on the bounded SQLite lane."""

        return await self._run_bound(
            partial(operation, *args, **kwargs),
            priority=AsyncStorePriority.MUTATION,
        )

    async def control(
        self,
        operation: Callable[P, T],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> T:
        """Run lease, finalization, or reconciliation work ahead of read traffic."""

        return await self._run_bound(
            partial(operation, *args, **kwargs),
            priority=AsyncStorePriority.CONTROL,
        )

    async def mutate(
        self,
        operation: Callable[P, T],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> T:
        """Run an ordinary durable mutation."""

        return await self.run(operation, *args, **kwargs)

    async def read(
        self,
        operation: Callable[P, T],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> T:
        """Run a bounded read while preserving control capacity."""

        return await self._run_bound(
            partial(operation, *args, **kwargs),
            priority=AsyncStorePriority.READ,
        )

    async def stream(
        self,
        operation: Callable[P, T],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> T:
        """Run low-priority SSE polling work."""

        return await self._run_bound(
            partial(operation, *args, **kwargs),
            priority=AsyncStorePriority.STREAM,
        )

    async def maintenance(
        self,
        operation: Callable[P, T],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> T:
        """Run periodic cleanup only after request and stream work."""

        return await self._run_bound(
            partial(operation, *args, **kwargs),
            priority=AsyncStorePriority.MAINTENANCE,
        )

    async def startup(self, owner: object | None = None) -> None:
        """Enter an application lifespan, reopening after a prior clean shutdown."""

        lifecycle_owner = self._default_owner if owner is None else owner
        while True:
            with self._lock:
                shutdown_completion = (
                    self._shutdown_completion if self._closing else None
                )
                if shutdown_completion is None:
                    if lifecycle_owner in self._lifecycle_owners:
                        raise RuntimeError("Async SQLite lifecycle owner already started")
                    if self._closed:
                        self._executor = self._new_executor()
                        self._closed = False
                    self._lifecycle_owners.add(lifecycle_owner)
                    return
            await self._join(shutdown_completion)

    async def shutdown(self, owner: object | None = None) -> None:
        """Leave a lifespan and drain the final one without abandoning submitted work."""

        lifecycle_owner = self._default_owner if owner is None else owner
        with self._lock:
            if self._lifecycle_owners:
                if lifecycle_owner not in self._lifecycle_owners:
                    raise RuntimeError("Async SQLite lifecycle owner is not active")
                self._lifecycle_owners.remove(lifecycle_owner)
                if self._lifecycle_owners:
                    return
            if self._closed:
                return
            if self._closing:
                leader = False
                shutdown_completion = self._shutdown_completion
                queued: tuple[_QueuedOperation, ...] = ()
                running = None
                executor = None
            else:
                leader = True
                self._closing = True
                shutdown_completion = Future()
                self._shutdown_completion = shutdown_completion
                queued = tuple(self._queue)
                self._queue.clear()
                running = self._running
                executor = self._executor
        if shutdown_completion is None:
            raise RuntimeError("Async SQLite shutdown lost its completion fence")
        if not leader:
            await self._join(shutdown_completion)
            return

        for item in queued:
            self._release_capacity(non_control_slot=item.non_control_slot)
            item.completion.set_exception(
                AsyncStoreClosedError(
                    "Async SQLite operation did not start before shutdown"
                )
            )
        cancellation: asyncio.CancelledError | None = None
        if running is not None:
            try:
                await self._join(running.completion)
            except asyncio.CancelledError as error:
                cancellation = error
            except BaseException:
                # The submitting caller owns the operation result. Shutdown only
                # proves that the worker settled before the executor is closed.
                pass
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=False)
        with self._lock:
            self._executor = None
            self._closed = True
            self._closing = False
            self._shutdown_completion = None
        shutdown_completion.set_result(None)
        if cancellation is not None:
            raise cancellation
