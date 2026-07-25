"""Bounded, idempotent retention cleanup for disposable recovery detail."""

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
    """Expire terminal detail plus immutable replay fixtures in any status."""

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


def cleanup_expired_recovery_creations(
    store: SQLiteStore,
    *,
    now: datetime | None = None,
    batch_size: int = 100,
) -> int:
    """Delete a bounded batch after its signed-session scope expires."""

    if not 1 <= batch_size <= MAX_CLEANUP_BATCH_SIZE:
        raise ValueError(
            f"batch_size must be between 1 and {MAX_CLEANUP_BATCH_SIZE}"
        )
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() != timedelta(0):
        raise ValueError("now must be timezone-aware UTC")
    return store.delete_expired_recovery_creations(
        expired_at=current,
        batch_size=batch_size,
    )


def expire_pending_approvals(
    store: SQLiteStore,
    *,
    now: datetime | None = None,
    batch_size: int = 100,
    recovery_id: str | None = None,
) -> int:
    """Seal a bounded batch of untouched, expired hotel consent windows."""

    if not 1 <= batch_size <= MAX_CLEANUP_BATCH_SIZE:
        raise ValueError(
            f"batch_size must be between 1 and {MAX_CLEANUP_BATCH_SIZE}"
        )
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() != timedelta(0):
        raise ValueError("now must be timezone-aware UTC")
    return store.expire_pending_approvals(
        now=current,
        batch_size=batch_size,
        recovery_id=recovery_id,
    )
