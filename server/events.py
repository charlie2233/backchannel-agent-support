"""Reconnect-safe server-sent events backed by the persisted event ledger."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from time import monotonic

from server.models import RecoveryEvent, RecoveryStatus
from server.store import RecoveryNotFoundError, SQLiteStore

HEARTBEAT_SECONDS = 15.0
POLL_INTERVAL_SECONDS = 0.25
TERMINAL_RECOVERY_STATUSES = frozenset(
    {
        RecoveryStatus.COMPLETED,
        RecoveryStatus.CLOSED_WITHOUT_ACTION,
        RecoveryStatus.OUTCOME_UNKNOWN,
    }
)


def encode_sse_event(event: RecoveryEvent) -> str:
    payload = json.dumps(
        event.model_dump(mode="json", by_alias=True),
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"id: {event.seq}\ndata: {payload}\n\n"


async def stream_recovery_events(
    store: SQLiteStore,
    recovery_id: str,
    *,
    after_seq: int,
    is_disconnected: Callable[[], Awaitable[bool]],
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
    poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
    session_hash: str | None = None,
    session_expires_at: datetime | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> AsyncIterator[str]:
    """Replay durable events first, then poll for newly committed events."""

    cursor = after_seq
    last_emission = monotonic()
    while True:
        if session_expires_at is not None and now() >= session_expires_at:
            return
        try:
            if session_hash is None:
                persisted, recovery_status = store.read_event_batch(
                    recovery_id, after_seq=cursor
                )
            else:
                persisted, recovery_status = store.read_event_batch_for_session(
                    recovery_id,
                    session_hash=session_hash,
                    after_seq=cursor,
                )
        except RecoveryNotFoundError:
            if session_hash is not None:
                return
            raise
        for event in persisted:
            yield encode_sse_event(event)
            cursor = event.seq
            last_emission = monotonic()

        if recovery_status in TERMINAL_RECOVERY_STATUSES:
            return
        if await is_disconnected():
            return

        elapsed = monotonic() - last_emission
        if elapsed >= heartbeat_seconds:
            yield ": heartbeat\n\n"
            last_emission = monotonic()
            continue
        await asyncio.sleep(min(poll_interval_seconds, heartbeat_seconds - elapsed))
