"""Bounded, idempotent retention cleanup for terminal recovery detail."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from server.store import SQLiteStore

MAX_CLEANUP_BATCH_SIZE = 1_000


def cleanup_terminal_recoveries(
    store: SQLiteStore,
    *,
    terminal_ttl: timedelta,
    now: datetime | None = None,
    batch_size: int = 100,
) -> int:
    if terminal_ttl <= timedelta(0):
        raise ValueError("terminal_ttl must be positive")
    if not 1 <= batch_size <= MAX_CLEANUP_BATCH_SIZE:
        raise ValueError(
            f"batch_size must be between 1 and {MAX_CLEANUP_BATCH_SIZE}"
        )
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() != timedelta(0):
        raise ValueError("now must be timezone-aware UTC")
    return store.delete_terminal_recoveries(
        updated_before=current - terminal_ttl,
        batch_size=batch_size,
    )
