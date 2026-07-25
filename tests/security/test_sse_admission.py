from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.types import Message, Scope

import server.main as server_main
from server.config import RuntimeSettings
from server.controls import DEMO_SESSION_COOKIE
from server.main import create_app
from server.sse_admission import (
    LeasedStreamingResponse,
    SSEAdmissionGate,
    SSEStreamLease,
    StreamCapacityReachedError,
)
from server.store import SQLiteStore


def _session(label: str) -> str:
    return f"test-session-{label}"


def test_sse_admission_enforces_recovery_session_and_global_caps_separately() -> None:
    recovery_gate = SSEAdmissionGate(
        max_concurrent=4,
        max_per_session=3,
        max_per_recovery=1,
    )
    recovery_lease = recovery_gate.acquire(
        session_key=_session("recovery"),
        recovery_id="recovery-a",
    )
    with pytest.raises(StreamCapacityReachedError):
        recovery_gate.acquire(
            session_key=_session("recovery"),
            recovery_id="recovery-a",
        )
    recovery_lease.release()

    session_gate = SSEAdmissionGate(
        max_concurrent=4,
        max_per_session=2,
        max_per_recovery=2,
    )
    session_leases = [
        session_gate.acquire(
            session_key=_session("bounded"),
            recovery_id=f"recovery-{index}",
        )
        for index in range(2)
    ]
    with pytest.raises(StreamCapacityReachedError):
        session_gate.acquire(
            session_key=_session("bounded"),
            recovery_id="recovery-overflow",
        )
    for lease in session_leases:
        lease.release()

    global_gate = SSEAdmissionGate(
        max_concurrent=2,
        max_per_session=2,
        max_per_recovery=1,
    )
    global_leases = [
        global_gate.acquire(
            session_key=_session(str(index)),
            recovery_id=f"recovery-{index}",
        )
        for index in range(2)
    ]
    with pytest.raises(StreamCapacityReachedError):
        global_gate.acquire(
            session_key=_session("overflow"),
            recovery_id="recovery-overflow",
        )
    assert global_gate.snapshot().active == 2
    for lease in global_leases:
        lease.release()
    assert global_gate.snapshot().active == 0


def test_sse_admission_is_race_safe_and_a_lease_releases_exactly_once() -> None:
    gate = SSEAdmissionGate(
        max_concurrent=1,
        max_per_session=1,
        max_per_recovery=1,
    )
    barrier = Barrier(2)

    def compete(index: int) -> object:
        barrier.wait(timeout=5)
        try:
            return gate.acquire(
                session_key=_session(str(index)),
                recovery_id=f"recovery-{index}",
            )
        except StreamCapacityReachedError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(compete, range(2)))

    leases = [outcome for outcome in outcomes if isinstance(outcome, SSEStreamLease)]
    assert len(leases) == 1
    assert outcomes.count("stream_capacity_reached") == 1
    assert gate.snapshot().active == 1

    lease = leases[0]
    lease.release()
    lease.release()
    assert gate.snapshot().active == 0

    replacement = gate.acquire(
        session_key=_session("replacement"),
        recovery_id="replacement",
    )
    assert gate.snapshot().active == 1
    replacement.release()


def test_leased_stream_releases_after_normal_exception_and_cancellation_paths() -> None:
    gate = SSEAdmissionGate(
        max_concurrent=1,
        max_per_session=1,
        max_per_recovery=1,
    )
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/events",
        "raw_path": b"/events",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
        "state": {},
    }

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message: Message) -> None:
        return None

    async def normal() -> AsyncIterator[str]:
        yield "data: complete\n\n"

    async def failing() -> AsyncIterator[str]:
        yield "data: started\n\n"
        raise RuntimeError("test stream failure")

    async def exercise() -> None:
        normal_lease = gate.acquire(
            session_key=_session("normal"),
            recovery_id="normal",
        )
        await LeasedStreamingResponse(normal(), lease=normal_lease)(
            scope,
            receive,
            send,
        )
        assert gate.snapshot().active == 0

        failing_lease = gate.acquire(
            session_key=_session("failing"),
            recovery_id="failing",
        )
        with pytest.raises(RuntimeError, match="test stream failure"):
            await LeasedStreamingResponse(failing(), lease=failing_lease)(
                scope,
                receive,
                send,
            )
        assert gate.snapshot().active == 0

        started = asyncio.Event()

        async def blocked() -> AsyncIterator[str]:
            started.set()
            yield "data: waiting\n\n"
            await asyncio.Event().wait()

        cancelled_lease = gate.acquire(
            session_key=_session("cancelled"),
            recovery_id="cancelled",
        )
        response = LeasedStreamingResponse(blocked(), lease=cancelled_lease)
        task = asyncio.create_task(response(scope, receive, send))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert gate.snapshot().active == 0
        replacement = gate.acquire(
            session_key=_session("after-cancel"),
            recovery_id="after-cancel",
        )
        replacement.release()

    asyncio.run(exercise())


def test_sse_response_construction_error_releases_capacity(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "sse-construction.sqlite3")
    application = create_app(
        RuntimeSettings(
            live_ready=False,
            sse_max_concurrent=1,
            sse_max_per_session=1,
            sse_max_per_recovery=1,
        ),
        store=store,
    )

    def fail_construction(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("test response construction failure")

    with TestClient(application, raise_server_exceptions=False) as client:
        created = client.post(
            "/api/recoveries",
            json={"scenarioId": "api-quota", "executionMode": "replay_fixture"},
        )
        recovery_id = created.json()["recoveryId"]
        monkeypatch.setattr(server_main, "LeasedStreamingResponse", fail_construction)

        failed = client.get(f"/api/recoveries/{recovery_id}/events")

        assert failed.status_code == 500
        assert failed.json()["error"]["code"] == "internal_error"
        gate = client.app.state.sse_gate
        assert gate.snapshot().active == 0
        replacement = gate.acquire(
            session_key=_session("after-construction"),
            recovery_id="after-construction",
        )
        replacement.release()


def test_sse_capacity_is_checked_after_ownership_without_polling_or_leaking_counts(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "sse-capacity.sqlite3")
    settings = RuntimeSettings(
        live_ready=False,
        sse_max_concurrent=1,
        sse_max_per_session=1,
        sse_max_per_recovery=1,
    )
    with TestClient(create_app(settings, store=store)) as client:
        created = client.post(
            "/api/recoveries",
            json={"scenarioId": "api-quota", "executionMode": "replay_fixture"},
        )
        recovery_id = created.json()["recoveryId"]
        owner_cookie = client.cookies.get(DEMO_SESSION_COOKIE)
        assert owner_cookie is not None
        client.cookies.clear()
        foreign_created = client.post(
            "/api/recoveries",
            json={"scenarioId": "api-quota", "executionMode": "replay_fixture"},
        )
        assert foreign_created.status_code == 201
        foreign_cookie = client.cookies.get(DEMO_SESSION_COOKIE)
        assert foreign_cookie is not None
        assert foreign_cookie != owner_cookie
        client.cookies.clear()
        gate = client.app.state.sse_gate
        occupied = gate.acquire(
            session_key=_session("occupied"),
            recovery_id="occupied",
        )
        polls = 0
        original_read = store.read_event_batch_for_session

        def count_reads(*args: object, **kwargs: object) -> object:
            nonlocal polls
            polls += 1
            return original_read(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(store, "read_event_batch_for_session", count_reads)
        rejected = client.get(
            f"/api/recoveries/{recovery_id}/events",
            headers={"Cookie": f"{DEMO_SESSION_COOKIE}={owner_cookie}"},
        )
        missing = client.get(
            f"/api/recoveries/{uuid4()}/events",
            headers={"Cookie": f"{DEMO_SESSION_COOKIE}={owner_cookie}"},
        )
        foreign = client.get(
            f"/api/recoveries/{recovery_id}/events",
            headers={"Cookie": f"{DEMO_SESSION_COOKIE}={foreign_cookie}"},
        )

        assert rejected.status_code == 429
        assert rejected.headers["retry-after"] == "1"
        assert rejected.json()["error"] == {
            "code": "stream_capacity_reached",
            "message": "The event stream is currently at capacity.",
            "requestId": rejected.json()["error"]["requestId"],
            "recoveryId": recovery_id,
            "retryAfterSeconds": 1,
            "fallback": None,
        }
        assert "session" not in rejected.text.lower()
        assert "counter" not in rejected.text.lower()
        assert missing.status_code == 404
        assert foreign.status_code == 404
        assert polls == 0
        assert gate.snapshot().active == 1
        occupied.release()
        assert gate.snapshot().active == 0


def test_sse_configuration_defaults_environment_and_ordering(monkeypatch) -> None:
    defaults = RuntimeSettings(live_ready=False)
    assert (
        defaults.sse_max_concurrent,
        defaults.sse_max_per_session,
        defaults.sse_max_per_recovery,
    ) == (32, 4, 2)

    monkeypatch.setenv("BACKCHANNEL_SSE_MAX_CONCURRENT", "9")
    monkeypatch.setenv("BACKCHANNEL_SSE_MAX_PER_SESSION", "5")
    monkeypatch.setenv("BACKCHANNEL_SSE_MAX_PER_RECOVERY", "3")
    configured = RuntimeSettings.from_environment()
    assert (
        configured.sse_max_concurrent,
        configured.sse_max_per_session,
        configured.sse_max_per_recovery,
    ) == (9, 5, 3)

    with pytest.raises(ValueError, match="SSE.*positive"):
        RuntimeSettings(live_ready=False, sse_max_concurrent=0)
    with pytest.raises(ValueError, match="per recovery.*per session.*global"):
        RuntimeSettings(
            live_ready=False,
            sse_max_concurrent=2,
            sse_max_per_session=3,
            sse_max_per_recovery=1,
        )
    monkeypatch.setenv("BACKCHANNEL_SSE_MAX_PER_RECOVERY", "6")
    with pytest.raises(ValueError, match="per recovery.*per session.*global"):
        RuntimeSettings.from_environment()
    monkeypatch.setenv("BACKCHANNEL_SSE_MAX_PER_RECOVERY", "0")
    with pytest.raises(ValueError, match="BACKCHANNEL_SSE_MAX_PER_RECOVERY"):
        RuntimeSettings.from_environment()


def test_sse_openapi_declares_the_generic_capacity_response(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "sse-openapi.sqlite3")
    with TestClient(create_app(RuntimeSettings(live_ready=False), store=store)) as client:
        responses = client.app.openapi()["paths"][
            "/api/recoveries/{recovery_id}/events"
        ]["get"]["responses"]

    assert responses["429"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/PublicErrorResponse"
    }
    assert responses["429"]["headers"]["Retry-After"] == {
        "description": "Retry delay in seconds for a stream-capacity response.",
        "schema": {
            "maximum": 300.0,
            "minimum": 1.0,
            "type": "integer",
        },
    }
