from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from starlette.types import Message, Scope

from server.events import EventStreamAdmissionController, lease_event_stream
from server.main import _build_admitted_event_stream_response


class MutableUtcClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current


@pytest.mark.parametrize("asgi_spec_version", ["2.3", "2.4"])
def test_expired_stream_sends_only_bounded_empty_response(
    asgi_spec_version: str,
) -> None:
    expires_at = datetime(2026, 7, 30, 12, tzinfo=UTC)
    clock = MutableUtcClock(expires_at)
    controller = EventStreamAdmissionController(
        max_active=1,
        max_per_recovery=1,
        retry_seconds=5,
    )
    lease = controller.try_acquire("recovery-a")
    assert lease is not None

    never_disconnect = asyncio.Event()
    sent_messages: list[Message] = []
    final_body_lease_counts: list[int] = []

    async def source() -> AsyncIterator[str]:
        yield "id: 7\ndata: {\"summary\":\"owner evidence\"}\n\n"

    async def receive() -> Message:
        await never_disconnect.wait()
        return {"type": "http.disconnect"}

    async def cooperative_send(message: Message) -> None:
        sent_messages.append(message)
        if message["type"] == "http.response.body":
            assert message.get("body", b"") == b""
            assert message.get("more_body") is False
            final_body_lease_counts.append(controller.active_count)

    response = _build_admitted_event_stream_response(
        lease_event_stream(source(), lease),
        lease,
        session_expires_at=expires_at,
        session_clock=clock,
    )

    async def invoke() -> None:
        scope: Scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/recoveries/recovery-a/events",
            "headers": [],
            "asgi": {"spec_version": asgi_spec_version},
        }
        await asyncio.wait_for(
            response(
                scope,
                receive,
                cooperative_send,
            ),
            timeout=1,
        )

    asyncio.run(invoke())

    assert [message["type"] for message in sent_messages] == [
        "http.response.start",
        "http.response.body",
    ]
    assert final_body_lease_counts == [0]
    assert controller.active_count == 0
    reacquired = controller.try_acquire("recovery-a")
    assert reacquired is not None
    reacquired.release()


@pytest.mark.parametrize("asgi_spec_version", ["2.3", "2.4"])
def test_admitted_stream_releases_lease_without_cancelling_response_start(
    asgi_spec_version: str,
) -> None:
    expires_at = datetime(2026, 7, 30, 12, tzinfo=UTC)
    clock = MutableUtcClock(expires_at - timedelta(milliseconds=50))
    controller = EventStreamAdmissionController(
        max_active=1,
        max_per_recovery=1,
        retry_seconds=5,
    )
    lease = controller.try_acquire("recovery-a")
    assert lease is not None

    start_send_started = asyncio.Event()
    start_send_cancelled = asyncio.Event()
    release_start_send = asyncio.Event()
    never_disconnect = asyncio.Event()
    source_started = asyncio.Event()
    raw_send_attempts: list[Message] = []
    delivered_bodies: list[bytes] = []

    async def source() -> AsyncIterator[str]:
        source_started.set()
        yield "id: 7\ndata: {\"summary\":\"owner evidence\"}\n\n"

    async def receive() -> Message:
        await never_disconnect.wait()
        return {"type": "http.disconnect"}

    async def backpressured_start_send(message: Message) -> None:
        raw_send_attempts.append(message)
        if message["type"] == "http.response.start":
            start_send_started.set()
            clock.current = expires_at
            try:
                await release_start_send.wait()
            except asyncio.CancelledError:
                start_send_cancelled.set()
                raise
            return
        body = cast(bytes, message.get("body", b""))
        if body:
            delivered_bodies.append(body)

    response = _build_admitted_event_stream_response(
        lease_event_stream(source(), lease),
        lease,
        session_expires_at=expires_at,
        session_clock=clock,
    )

    async def invoke() -> None:
        scope: Scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/recoveries/recovery-a/events",
            "headers": [],
            "asgi": {"spec_version": asgi_spec_version},
        }
        response_task = asyncio.create_task(
            response(
                scope,
                receive,
                backpressured_start_send,
            )
        )
        await asyncio.wait_for(start_send_started.wait(), timeout=1)

        async def wait_for_lease_release() -> None:
            while controller.active_count:
                await asyncio.sleep(0.005)

        await asyncio.wait_for(wait_for_lease_release(), timeout=1)
        assert not response_task.done()
        assert not start_send_cancelled.is_set()
        assert not source_started.is_set()
        reacquired = controller.try_acquire("recovery-a")
        assert reacquired is not None
        reacquired.release()
        release_start_send.set()
        await asyncio.wait_for(response_task, timeout=1)

    asyncio.run(invoke())

    assert not start_send_cancelled.is_set()
    assert source_started.is_set()
    assert [message["type"] for message in raw_send_attempts] == [
        "http.response.start",
        "http.response.body",
    ]
    assert delivered_bodies == []
    assert controller.active_count == 0
    reacquired = controller.try_acquire("recovery-a")
    assert reacquired is not None
    reacquired.release()


@pytest.mark.parametrize(
    "frame",
    [
        "id: 7\ndata: {\"summary\":\"owner evidence\"}\n\n",
        ": heartbeat\n\n",
    ],
    ids=["event", "heartbeat"],
)
@pytest.mark.parametrize("asgi_spec_version", ["2.3", "2.4"])
def test_admitted_stream_cancels_body_send_that_crosses_session_expiry(
    frame: str,
    asgi_spec_version: str,
) -> None:
    expires_at = datetime(2026, 7, 30, 12, tzinfo=UTC)
    clock = MutableUtcClock(expires_at - timedelta(milliseconds=50))
    controller = EventStreamAdmissionController(
        max_active=1,
        max_per_recovery=1,
        retry_seconds=5,
    )
    lease = controller.try_acquire("recovery-a")
    assert lease is not None

    body_send_started = asyncio.Event()
    body_send_cancelled = asyncio.Event()
    never_release_body_send = asyncio.Event()
    never_disconnect = asyncio.Event()
    delivered_bodies: list[bytes] = []
    sent_messages: list[Message] = []
    final_body_lease_counts: list[int] = []

    async def source() -> AsyncIterator[str]:
        yield frame

    async def receive() -> Message:
        await never_disconnect.wait()
        return {"type": "http.disconnect"}

    async def backpressured_send(message: Message) -> None:
        sent_messages.append(message)
        body = cast(bytes, message.get("body", b""))
        if message["type"] != "http.response.body":
            return
        if not body:
            if message.get("more_body") is False:
                final_body_lease_counts.append(controller.active_count)
            return
        body_send_started.set()
        clock.current = expires_at
        try:
            await never_release_body_send.wait()
        except asyncio.CancelledError:
            body_send_cancelled.set()
            raise
        delivered_bodies.append(body)

    response = _build_admitted_event_stream_response(
        lease_event_stream(source(), lease),
        lease,
        session_expires_at=expires_at,
        session_clock=clock,
    )

    async def invoke() -> None:
        scope: Scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/recoveries/recovery-a/events",
            "headers": [],
            "asgi": {"spec_version": asgi_spec_version},
        }
        response_task = asyncio.create_task(
            response(
                scope,
                receive,
                backpressured_send,
            )
        )
        await asyncio.wait_for(body_send_started.wait(), timeout=1)
        await asyncio.wait_for(response_task, timeout=1)

    asyncio.run(invoke())

    assert body_send_cancelled.is_set()
    assert delivered_bodies == []
    assert sent_messages[0]["type"] == "http.response.start"
    assert sent_messages[-1] == {
        "type": "http.response.body",
        "body": b"",
        "more_body": False,
    }
    assert final_body_lease_counts == [0]
    assert controller.active_count == 0
    reacquired = controller.try_acquire("recovery-a")
    assert reacquired is not None
    reacquired.release()


def test_response_clock_failure_releases_lease_and_allows_reacquisition() -> None:
    expires_at = datetime(2026, 7, 30, 12, tzinfo=UTC)

    def naive_clock() -> datetime:
        return expires_at.replace(tzinfo=None)

    def failing_clock() -> datetime:
        raise RuntimeError("clock unavailable")

    for session_clock, expected_error, match in (
        (naive_clock, ValueError, "clock must return a UTC timestamp"),
        (failing_clock, RuntimeError, "clock unavailable"),
    ):
        controller = EventStreamAdmissionController(
            max_active=1,
            max_per_recovery=1,
            retry_seconds=5,
        )
        lease = controller.try_acquire("recovery-a")
        assert lease is not None

        async def source() -> AsyncIterator[str]:
            yield "id: 1\n\n"

        async def receive() -> Message:
            return {"type": "http.disconnect"}

        async def send(_message: Message) -> None:
            return None

        response = _build_admitted_event_stream_response(
            lease_event_stream(source(), lease),
            lease,
            session_expires_at=expires_at,
            session_clock=session_clock,
        )

        with pytest.raises(expected_error, match=match):
            asyncio.run(
                response(
                    {
                        "type": "http",
                        "method": "GET",
                        "path": "/api/recoveries/recovery-a/events",
                        "headers": [],
                        "asgi": {"spec_version": "2.4"},
                    },
                    receive,
                    send,
                )
            )

        assert controller.active_count == 0
        reacquired = controller.try_acquire("recovery-a")
        assert reacquired is not None
        reacquired.release()


@pytest.mark.parametrize("asgi_spec_version", ["2.3", "2.4"])
def test_expired_stream_bounds_a_backpressured_final_body(
    asgi_spec_version: str,
) -> None:
    expires_at = datetime(2026, 7, 30, 12, tzinfo=UTC)
    clock = MutableUtcClock(expires_at - timedelta(milliseconds=50))
    controller = EventStreamAdmissionController(
        max_active=1,
        max_per_recovery=1,
        retry_seconds=5,
    )
    lease = controller.try_acquire("recovery-a")
    assert lease is not None

    nonempty_send_started = asyncio.Event()
    nonempty_send_cancelled = asyncio.Event()
    final_send_started = asyncio.Event()
    final_send_cancelled = asyncio.Event()
    never_release_send = asyncio.Event()
    never_disconnect = asyncio.Event()

    async def source() -> AsyncIterator[str]:
        yield "id: 7\ndata: {\"summary\":\"owner evidence\"}\n\n"

    async def receive() -> Message:
        await never_disconnect.wait()
        return {"type": "http.disconnect"}

    async def block_every_body_send(message: Message) -> None:
        if message["type"] != "http.response.body":
            return
        body = cast(bytes, message.get("body", b""))
        started = nonempty_send_started if body else final_send_started
        cancelled = nonempty_send_cancelled if body else final_send_cancelled
        if body:
            clock.current = expires_at
        else:
            assert controller.active_count == 0
        started.set()
        try:
            await never_release_send.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    response = _build_admitted_event_stream_response(
        lease_event_stream(source(), lease),
        lease,
        session_expires_at=expires_at,
        session_clock=clock,
    )

    async def invoke() -> None:
        scope: Scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/recoveries/recovery-a/events",
            "headers": [],
            "asgi": {"spec_version": asgi_spec_version},
        }
        response_task = asyncio.create_task(
            response(
                scope,
                receive,
                block_every_body_send,
            )
        )
        await asyncio.wait_for(nonempty_send_started.wait(), timeout=1)
        await asyncio.wait_for(final_send_started.wait(), timeout=1)
        await asyncio.wait_for(response_task, timeout=1)

    asyncio.run(invoke())

    assert nonempty_send_cancelled.is_set()
    assert final_send_cancelled.is_set()
    assert controller.active_count == 0
    reacquired = controller.try_acquire("recovery-a")
    assert reacquired is not None
    reacquired.release()
