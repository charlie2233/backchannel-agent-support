import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta, timezone

import pytest

from server.events import (
    EventStreamAdmissionController,
    lease_event_stream,
    stream_recovery_events,
)
from server.models import RecoveryEvent, RecoveryStatus

RECOVERY_ID = "recovery-a"
SESSION_KEY = "session-a"
STARTED_AT = datetime(2026, 7, 30, 12, tzinfo=UTC)


class MutableClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current


class FakePublicEventStore:
    def __init__(
        self,
        batches: list[tuple[list[RecoveryEvent], RecoveryStatus]] | None = None,
        *,
        before_read: Callable[[], None] | None = None,
    ) -> None:
        self.batches = list(batches or [])
        self.before_read = before_read
        self.read_count = 0

    def read_public_event_batch(
        self,
        recovery_id: str,
        *,
        after_seq: int,
        session_key: str,
        replay_scenarios: object,
    ) -> tuple[list[RecoveryEvent], RecoveryStatus]:
        assert recovery_id == RECOVERY_ID
        assert after_seq >= 0
        assert session_key == SESSION_KEY
        assert replay_scenarios == {}
        self.read_count += 1
        if self.before_read is not None:
            self.before_read()
        if self.batches:
            return self.batches.pop(0)
        return [], RecoveryStatus.IN_PROGRESS


def _event(seq: int) -> RecoveryEvent:
    return RecoveryEvent(
        recoveryId=RECOVERY_ID,
        seq=seq,
        type="recovery.test",
        terminal=False,
        data={"summary": f"event {seq}"},
        createdAt=STARTED_AT,
    )


async def _connected() -> bool:
    return False


def _public_stream(
    store: FakePublicEventStore,
    clock: MutableClock,
    expires_at: datetime,
    *,
    initial_batch: tuple[list[RecoveryEvent], RecoveryStatus] | None = None,
    heartbeat_seconds: float = 60,
    poll_interval_seconds: float = 0.25,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
):
    return stream_recovery_events(
        store,  # type: ignore[arg-type]
        RECOVERY_ID,
        after_seq=0,
        is_disconnected=_connected,
        heartbeat_seconds=heartbeat_seconds,
        poll_interval_seconds=poll_interval_seconds,
        initial_batch=initial_batch,
        public_session_key=SESSION_KEY,
        public_replay_scenarios={},
        public_session_expires_at=expires_at,
        session_clock=clock,
        sleep=sleep,
    )


def test_expired_session_suppresses_buffered_initial_event() -> None:
    clock = MutableClock(STARTED_AT)
    store = FakePublicEventStore()

    async def collect() -> list[str]:
        stream = _public_stream(
            store,
            clock,
            STARTED_AT,
            initial_batch=([_event(1)], RecoveryStatus.IN_PROGRESS),
        )
        return [chunk async for chunk in stream]

    assert asyncio.run(collect()) == []
    assert store.read_count == 0


def test_store_read_crossing_session_expiry_suppresses_returned_event() -> None:
    expires_at = STARTED_AT + timedelta(seconds=1)
    clock = MutableClock(STARTED_AT)
    store = FakePublicEventStore(
        [([_event(1)], RecoveryStatus.IN_PROGRESS)],
        before_read=lambda: setattr(clock, "current", expires_at),
    )

    async def collect() -> list[str]:
        return [chunk async for chunk in _public_stream(store, clock, expires_at)]

    assert asyncio.run(collect()) == []
    assert store.read_count == 1


def test_expiry_suppresses_remaining_buffer_skips_future_poll_and_releases_lease() -> None:
    expires_at = STARTED_AT + timedelta(seconds=1)
    clock = MutableClock(STARTED_AT)
    store = FakePublicEventStore([([_event(3)], RecoveryStatus.IN_PROGRESS)])
    controller = EventStreamAdmissionController(
        max_active=1,
        max_per_recovery=1,
        retry_seconds=5,
    )
    lease = controller.try_acquire(RECOVERY_ID)
    assert lease is not None

    async def exercise() -> tuple[str, type[BaseException] | None]:
        stream = lease_event_stream(
            _public_stream(
                store,
                clock,
                expires_at,
                initial_batch=(
                    [_event(1), _event(2)],
                    RecoveryStatus.IN_PROGRESS,
                ),
                heartbeat_seconds=0,
            ),
            lease,
        )
        first = await anext(stream)
        clock.current = expires_at
        try:
            await anext(stream)
        except StopAsyncIteration as error:
            return first, type(error)
        return first, None

    first, stopped_with = asyncio.run(exercise())

    assert "id: 1" in first
    assert stopped_with is StopAsyncIteration
    assert store.read_count == 0
    assert controller.active_count == 0
    reacquired = controller.try_acquire(RECOVERY_ID)
    assert reacquired is not None
    reacquired.release()


def test_expiry_during_disconnect_probe_suppresses_due_heartbeat() -> None:
    expires_at = STARTED_AT + timedelta(seconds=1)
    clock = MutableClock(STARTED_AT)
    store = FakePublicEventStore()

    async def crosses_deadline() -> bool:
        clock.current = expires_at
        return False

    async def collect() -> list[str]:
        return [
            chunk
            async for chunk in stream_recovery_events(
                store,  # type: ignore[arg-type]
                RECOVERY_ID,
                after_seq=0,
                is_disconnected=crosses_deadline,
                heartbeat_seconds=0,
                initial_batch=([], RecoveryStatus.IN_PROGRESS),
                public_session_key=SESSION_KEY,
                public_replay_scenarios={},
                public_session_expires_at=expires_at,
                session_clock=clock,
            )
        ]

    assert asyncio.run(collect()) == []
    assert store.read_count == 0


def test_idle_sleep_is_bounded_by_remaining_session_lifetime() -> None:
    expires_at = STARTED_AT + timedelta(milliseconds=200)
    clock = MutableClock(STARTED_AT)
    store = FakePublicEventStore()
    sleep_calls: list[float] = []

    async def advance_clock(delay: float) -> None:
        sleep_calls.append(delay)
        clock.current += timedelta(seconds=delay)

    async def collect() -> list[str]:
        stream = _public_stream(
            store,
            clock,
            expires_at,
            initial_batch=([], RecoveryStatus.IN_PROGRESS),
            heartbeat_seconds=60,
            poll_interval_seconds=5,
            sleep=advance_clock,
        )
        return [chunk async for chunk in stream]

    assert asyncio.run(collect()) == []
    assert sleep_calls == [pytest.approx(0.2)]
    assert store.read_count == 0


@pytest.mark.parametrize(
    ("session_key", "replay_scenarios", "expires_at"),
    [
        (SESSION_KEY, None, None),
        (None, {}, None),
        (None, None, STARTED_AT + timedelta(seconds=1)),
        (SESSION_KEY, {}, None),
        (SESSION_KEY, None, STARTED_AT + timedelta(seconds=1)),
        (None, {}, STARTED_AT + timedelta(seconds=1)),
    ],
)
def test_public_session_options_are_strictly_all_or_none(
    session_key: str | None,
    replay_scenarios: dict[object, object] | None,
    expires_at: datetime | None,
) -> None:
    store = FakePublicEventStore()

    async def collect() -> list[str]:
        return [
            chunk
            async for chunk in stream_recovery_events(
                store,  # type: ignore[arg-type]
                RECOVERY_ID,
                after_seq=0,
                is_disconnected=_connected,
                initial_batch=([], RecoveryStatus.IN_PROGRESS),
                public_session_key=session_key,
                public_replay_scenarios=replay_scenarios,  # type: ignore[arg-type]
                public_session_expires_at=expires_at,
            )
        ]

    with pytest.raises(
        ValueError,
        match="requires session, replay definitions, and expiry",
    ):
        asyncio.run(collect())
    assert store.read_count == 0


@pytest.mark.parametrize(
    "expires_at",
    [
        STARTED_AT.replace(tzinfo=None),
        STARTED_AT.astimezone(timezone(timedelta(hours=1))),
    ],
)
def test_public_session_expiry_must_be_utc(expires_at: datetime) -> None:
    store = FakePublicEventStore()

    async def collect() -> list[str]:
        return [
            chunk
            async for chunk in _public_stream(
                store,
                MutableClock(STARTED_AT),
                expires_at,
                initial_batch=([], RecoveryStatus.IN_PROGRESS),
            )
        ]

    with pytest.raises(ValueError, match="expiry must be a UTC timestamp"):
        asyncio.run(collect())
    assert store.read_count == 0


def test_public_session_clock_must_return_utc() -> None:
    store = FakePublicEventStore()

    async def collect() -> list[str]:
        return [
            chunk
            async for chunk in stream_recovery_events(
                store,  # type: ignore[arg-type]
                RECOVERY_ID,
                after_seq=0,
                is_disconnected=_connected,
                initial_batch=([], RecoveryStatus.IN_PROGRESS),
                public_session_key=SESSION_KEY,
                public_replay_scenarios={},
                public_session_expires_at=STARTED_AT + timedelta(seconds=1),
                session_clock=lambda: STARTED_AT.replace(tzinfo=None),
            )
        ]

    with pytest.raises(
        ValueError,
        match="session clock must return a UTC timestamp",
    ):
        asyncio.run(collect())
    assert store.read_count == 0
