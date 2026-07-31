"""Reconnect-safe server-sent events backed by the persisted event ledger."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from threading import Lock
from time import monotonic

from server.models import (
    RecoveryEvent,
    RecoveryStatus,
    ReplayScenarioDefinition,
    ScenarioId,
)
from server.store import PublicRecoveryAccessRevokedError, SQLiteStore

HEARTBEAT_SECONDS = 15.0
POLL_INTERVAL_SECONDS = 0.25
STREAM_CAPACITY_CODE = "event_stream_capacity"
STREAM_CAPACITY_MESSAGE = (
    "Event streaming is temporarily at capacity; retry is automatic."
)


class EventStreamLease:
    """One idempotently releasable process-local stream admission."""

    __slots__ = ("_controller", "_recovery_id", "_released")

    def __init__(
        self,
        controller: EventStreamAdmissionController,
        recovery_id: str,
    ) -> None:
        self._controller = controller
        self._recovery_id = recovery_id
        self._released = False

    def release(self) -> None:
        self._controller._release(self)


class EventStreamAdmissionController:
    """Fail fast at bounded SSE capacity without queueing connections."""

    def __init__(
        self,
        *,
        max_active: int,
        max_per_recovery: int,
        retry_seconds: int,
    ) -> None:
        if not 1 <= max_per_recovery <= max_active <= 1_024:
            raise ValueError(
                "event stream limits must satisfy 1 <= per recovery <= process <= 1024"
            )
        if not 1 <= retry_seconds <= 300:
            raise ValueError("event stream retry seconds must be between 1 and 300")
        self._max_active = max_active
        self._max_per_recovery = max_per_recovery
        self._retry_seconds = retry_seconds
        self._lock = Lock()
        self._active = 0
        self._active_by_recovery: dict[str, int] = {}

    @property
    def retry_seconds(self) -> int:
        return self._retry_seconds

    @property
    def active_count(self) -> int:
        with self._lock:
            return self._active

    @property
    def active_recovery_count(self) -> int:
        with self._lock:
            return len(self._active_by_recovery)

    def active_for(self, recovery_id: str) -> int:
        with self._lock:
            return self._active_by_recovery.get(recovery_id, 0)

    def try_acquire(self, recovery_id: str) -> EventStreamLease | None:
        """Atomically acquire now or reject; this method never waits."""

        with self._lock:
            active_for_recovery = self._active_by_recovery.get(recovery_id, 0)
            if (
                self._active >= self._max_active
                or active_for_recovery >= self._max_per_recovery
            ):
                return None
            self._active += 1
            self._active_by_recovery[recovery_id] = active_for_recovery + 1
            return EventStreamLease(self, recovery_id)

    def _release(self, lease: EventStreamLease) -> None:
        with self._lock:
            if lease._released:
                return
            lease._released = True
            active_for_recovery = self._active_by_recovery[lease._recovery_id]
            self._active -= 1
            if active_for_recovery == 1:
                del self._active_by_recovery[lease._recovery_id]
            else:
                self._active_by_recovery[lease._recovery_id] = (
                    active_for_recovery - 1
                )


def encode_stream_capacity_event(*, request_id: str, retry_seconds: int) -> str:
    """Return the complete finite SSE control frame for native retry."""

    payload = json.dumps(
        {
            "code": STREAM_CAPACITY_CODE,
            "message": STREAM_CAPACITY_MESSAGE,
            "requestId": request_id,
        },
        separators=(",", ":"),
    )
    return (
        f"retry: {retry_seconds * 1_000}\n"
        "event: stream.capacity\n"
        f"data: {payload}\n\n"
    )


async def lease_event_stream(
    stream: AsyncIterator[str],
    lease: EventStreamLease,
) -> AsyncIterator[str]:
    """Release admission on completion, cancellation, or iterator failure."""

    try:
        async for chunk in stream:
            yield chunk
    finally:
        lease.release()


def encode_sse_event(event: RecoveryEvent) -> str:
    payload = json.dumps(
        event.model_dump(mode="json", by_alias=True),
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"id: {event.seq}\ndata: {payload}\n\n"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _seconds_until_session_expiry(
    expires_at: datetime,
    session_clock: Callable[[], datetime],
) -> float:
    current = session_clock()
    if current.tzinfo is None or current.utcoffset() != timedelta(0):
        raise ValueError("Public event session clock must return a UTC timestamp")
    return (expires_at - current).total_seconds()


async def stream_recovery_events(
    store: SQLiteStore,
    recovery_id: str,
    *,
    after_seq: int,
    is_disconnected: Callable[[], Awaitable[bool]],
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
    poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
    initial_batch: tuple[list[RecoveryEvent], RecoveryStatus] | None = None,
    public_session_key: str | None = None,
    public_replay_scenarios: (
        Mapping[ScenarioId, ReplayScenarioDefinition] | None
    ) = None,
    public_session_expires_at: datetime | None = None,
    session_clock: Callable[[], datetime] = _utc_now,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> AsyncIterator[str]:
    """Replay durable events first, then poll for newly committed events."""

    public_options = (
        public_session_key is not None,
        public_replay_scenarios is not None,
        public_session_expires_at is not None,
    )
    if any(public_options) and not all(public_options):
        raise ValueError(
            "Public event validation requires session, replay definitions, and expiry"
        )
    if (
        public_session_expires_at is not None
        and (
            public_session_expires_at.tzinfo is None
            or public_session_expires_at.utcoffset() != timedelta(0)
        )
    ):
        raise ValueError("Public event session expiry must be a UTC timestamp")

    def session_is_active() -> bool:
        if public_session_expires_at is None:
            return True
        return (
            _seconds_until_session_expiry(
                public_session_expires_at,
                session_clock,
            )
            > 0
        )

    cursor = after_seq
    last_emission = monotonic()
    pending_initial_batch = initial_batch
    while True:
        if not session_is_active():
            return
        if pending_initial_batch is not None:
            persisted, recovery_status = pending_initial_batch
            pending_initial_batch = None
        elif (
            public_session_key is not None
            and public_replay_scenarios is not None
        ):
            try:
                persisted, recovery_status = store.read_public_event_batch(
                    recovery_id,
                    after_seq=cursor,
                    session_key=public_session_key,
                    replay_scenarios=public_replay_scenarios,
                )
            except PublicRecoveryAccessRevokedError:
                return
        else:
            persisted, recovery_status = store.read_event_batch(
                recovery_id,
                after_seq=cursor,
            )
        if not session_is_active():
            return
        for event in persisted:
            encoded_event = encode_sse_event(event)
            if not session_is_active():
                return
            yield encoded_event
            cursor = event.seq
            last_emission = monotonic()

        if recovery_status.terminal:
            return
        if not session_is_active():
            return
        if await is_disconnected():
            return
        if not session_is_active():
            return

        elapsed = monotonic() - last_emission
        if elapsed >= heartbeat_seconds:
            if not session_is_active():
                return
            yield ": heartbeat\n\n"
            last_emission = monotonic()
            continue
        sleep_seconds = min(
            poll_interval_seconds,
            heartbeat_seconds - elapsed,
        )
        if public_session_expires_at is not None:
            remaining_session_seconds = _seconds_until_session_expiry(
                public_session_expires_at,
                session_clock,
            )
            if remaining_session_seconds <= 0:
                return
            sleep_seconds = min(sleep_seconds, remaining_session_seconds)
        await sleep(sleep_seconds)
