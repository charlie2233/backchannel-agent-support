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
    """Seal a bounded batch of claimed or untouched expired consent windows."""

    if not 1 <= batch_size <= MAX_CLEANUP_BATCH_SIZE:
        raise ValueError(
            f"batch_size must be between 1 and {MAX_CLEANUP_BATCH_SIZE}"
        )
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() != timedelta(0):
        raise ValueError("now must be timezone-aware UTC")
    if recovery_id is not None:
        claimed_count = store.expire_claimed_decisions(
            now=current,
            batch_size=batch_size,
            recovery_id=recovery_id,
        )
        remaining = batch_size - claimed_count
        if remaining == 0:
            return claimed_count
        return claimed_count + store.expire_pending_approvals(
            now=current,
            batch_size=remaining,
            recovery_id=recovery_id,
        )

    oldest_kind = store.oldest_expiry_candidate_kind(now=current)
    if oldest_kind is None:
        return 0
    other_kind = "untouched" if oldest_kind == "claimed" else "claimed"

    def expire_kind(kind: str, allowance: int) -> int:
        if allowance <= 0:
            return 0
        if kind == "claimed":
            return store.expire_claimed_decisions(
                now=current,
                batch_size=allowance,
            )
        return store.expire_pending_approvals(
            now=current,
            batch_size=allowance,
        )

    oldest_allowance = (batch_size + 1) // 2
    other_allowance = batch_size - oldest_allowance
    expired_count = expire_kind(oldest_kind, oldest_allowance)
    expired_count += expire_kind(other_kind, other_allowance)
    for kind in (oldest_kind, other_kind):
        remaining = batch_size - expired_count
        if remaining == 0:
            break
        expired_count += expire_kind(kind, remaining)
    return expired_count
