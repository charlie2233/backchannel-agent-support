"""Lifecycle-owned, bounded cleanup for terminal recovery evidence."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from server.store import SQLiteStore

logger = logging.getLogger(__name__)
CleanupClock = Callable[[], datetime]


class RecoveryCleanupService:
    """Own exactly one periodic task and join it during application shutdown."""

    def __init__(
        self,
        *,
        store: SQLiteStore,
        ttl: timedelta,
        interval: timedelta,
        batch_size: int,
        creation_usage_retention: timedelta = timedelta(days=8),
        clock: CleanupClock | None = None,
    ) -> None:
        if ttl <= timedelta(0):
            raise ValueError("Cleanup TTL must be positive")
        if interval <= timedelta(0):
            raise ValueError("Cleanup interval must be positive")
        if batch_size < 1:
            raise ValueError("Cleanup batch size must be positive")
        if creation_usage_retention < timedelta(days=1):
            raise ValueError("Creation usage retention must be at least one day")
        self._store = store
        self._ttl = ttl
        self._interval = interval
        self._batch_size = batch_size
        self._creation_usage_retention = creation_usage_retention
        self._clock = clock or (lambda: datetime.now(UTC))
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def task(self) -> asyncio.Task[None] | None:
        return self._task

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def run_once(self) -> int:
        now = self._clock()
        terminal_count = self._store.cleanup_terminal_recoveries(
            cutoff=now - self._ttl,
            batch_size=self._batch_size,
        )
        session_count = self._store.cleanup_expired_demo_sessions(
            cutoff=now,
            batch_size=self._batch_size,
        )
        creation_usage_count = self._store.cleanup_public_creation_usage(
            cutoff_day=(now - self._creation_usage_retention).date().isoformat(),
            batch_size=self._batch_size,
        )
        return terminal_count + session_count + creation_usage_count

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                deleted = self.run_once()
                if deleted:
                    logger.info("Durable cleanup completed count=%d", deleted)
            except Exception:
                # Never attach traceback or exception text: database/provider payloads are not
                # part of the allowlisted operational log contract.
                logger.error("Durable cleanup failed error_code=cleanup_failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._interval.total_seconds(),
                )
            except TimeoutError:
                continue

    async def startup(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._task = asyncio.create_task(
            self._run(),
            name="backchannel-terminal-cleanup",
        )

    async def shutdown(self) -> None:
        task = self._task
        if task is None:
            return
        self._stop.set()
        try:
            await task
        finally:
            self._task = None
