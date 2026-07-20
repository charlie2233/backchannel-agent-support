import asyncio
import json
import re
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from server.config import RuntimeSettings
from server.events import EventStreamAdmissionController, lease_event_stream
from server.main import _build_admitted_event_stream_response, create_app
from server.models import RecoveryStatus
from server.store import SQLiteStore


def _settings() -> RuntimeSettings:
    return RuntimeSettings(
        live_ready=False,
        max_concurrent_event_streams=1,
        max_event_streams_per_recovery=1,
        event_stream_retry_seconds=5,
    )


def _create_terminal_recovery(client: TestClient) -> str:
    response = client.post(
        "/api/recoveries",
        json={"scenarioId": "api-quota", "executionMode": "replay_fixture"},
    )
    assert response.status_code == 201
    return str(response.json()["recoveryId"])


def test_saturated_stream_returns_exact_finite_control_frame_without_polling(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SQLiteStore(tmp_path / "event-stream-capacity.sqlite3")
    app = create_app(_settings(), store=store)
    with TestClient(app) as client:
        recovery_id = _create_terminal_recovery(client)
        controller = app.state.event_stream_admission
        held = controller.try_acquire(recovery_id)
        assert held is not None

        def unexpected_poll(*_args, **_kwargs):
            raise AssertionError("a rejected stream must not start a polling loop")

        monkeypatch.setattr(store, "read_event_batch", unexpected_poll)
        response = client.get(f"/api/recoveries/{recovery_id}/events")

        request_id = response.headers["x-request-id"]
        assert re.fullmatch(r"[0-9a-f]{32}", request_id)
        expected_payload = json.dumps(
            {
                "code": "event_stream_capacity",
                "message": (
                    "Event streaming is temporarily at capacity; "
                    "retry is automatic."
                ),
                "requestId": request_id,
            },
            separators=(",", ":"),
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.text == (
            "retry: 5000\n"
            "event: stream.capacity\n"
            f"data: {expected_payload}\n\n"
        )
        assert "id:" not in response.text
        assert recovery_id not in response.text
        assert controller.active_count == 1
        held.release()


def test_authorization_cursor_and_existence_checks_precede_saturated_admission(
    tmp_path,
) -> None:
    store = SQLiteStore(tmp_path / "event-stream-order.sqlite3")
    app = create_app(_settings(), store=store)
    with TestClient(app) as owner, TestClient(app) as foreign:
        recovery_id = _create_terminal_recovery(owner)
        controller = app.state.event_stream_admission
        held = controller.try_acquire(recovery_id)
        assert held is not None

        foreign_response = foreign.get(
            f"/api/recoveries/{recovery_id}/events",
            headers={"Last-Event-ID": "not-an-integer"},
        )
        absent_response = owner.get(
            "/api/recoveries/11111111-2222-4333-8444-555555555555/events",
            headers={"Last-Event-ID": "not-an-integer"},
        )
        invalid_cursor = owner.get(
            f"/api/recoveries/{recovery_id}/events",
            headers={"Last-Event-ID": "not-an-integer"},
        )

        assert foreign_response.status_code == absent_response.status_code == 404
        assert foreign_response.content == absent_response.content == b'{"detail":"Not found"}'
        assert invalid_cursor.status_code == 400
        assert invalid_cursor.json() == {
            "detail": "Last-Event-ID must be a non-negative integer"
        }
        assert controller.active_count == 1
        held.release()


def test_owner_reacquires_after_terminal_stream_and_replay_cursor_is_unchanged(
    tmp_path,
) -> None:
    store = SQLiteStore(tmp_path / "event-stream-reacquire.sqlite3")
    app = create_app(_settings(), store=store)
    with TestClient(app) as client:
        recovery_id = _create_terminal_recovery(client)
        controller = app.state.event_stream_admission

        first = client.get(f"/api/recoveries/{recovery_id}/events")
        assert first.status_code == 200
        assert controller.active_count == 0

        second = client.get(
            f"/api/recoveries/{recovery_id}/events",
            headers={"Last-Event-ID": "2"},
        )
        assert second.status_code == 200
        assert controller.active_count == 0
        streamed_ids = [
            int(line.removeprefix("id: "))
            for line in second.text.splitlines()
            if line.startswith("id: ")
        ]
        assert streamed_ids
        assert all(sequence > 2 for sequence in streamed_ids)


def test_target_expiry_is_committed_before_saturated_admission(tmp_path) -> None:
    database_path = tmp_path / "event-stream-expiry-order.sqlite3"
    store = SQLiteStore(database_path)
    app = create_app(_settings(), store=store)
    with TestClient(app) as client:
        pending = client.post(
            "/api/recoveries",
            json={"scenarioId": "hotel", "executionMode": "sdk_stub"},
        )
        assert pending.status_code == 201
        recovery_id = str(pending.json()["recoveryId"])
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                "UPDATE remedies SET expiry = ? WHERE recovery_id = ?",
                (
                    (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                    recovery_id,
                ),
            )

        controller = app.state.event_stream_admission
        held = controller.try_acquire("other-recovery")
        assert held is not None
        response = client.get(f"/api/recoveries/{recovery_id}/events")

        assert response.status_code == 200
        assert "event: stream.capacity" in response.text
        assert store.get_recovery(recovery_id).status is RecoveryStatus.CLOSED_WITHOUT_ACTION
        assert [event.type for event in store.list_events(recovery_id) if event.terminal] == [
            "recovery.expired"
        ]
        assert controller.active_count == 1
        held.release()


def test_admitted_response_releases_lease_after_asgi_send_error() -> None:
    controller = EventStreamAdmissionController(
        max_active=1,
        max_per_recovery=1,
        retry_seconds=5,
    )
    lease = controller.try_acquire("recovery-a")
    assert lease is not None

    async def source() -> AsyncIterator[str]:
        yield "id: 1\n\n"
        await asyncio.Event().wait()

    response = _build_admitted_event_stream_response(
        lease_event_stream(source(), lease),
        lease,
    )

    async def receive() -> dict[str, str]:
        return {"type": "http.disconnect"}

    async def failing_send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.body":
            raise OSError("client send failed")

    async def invoke() -> None:
        await response(
            {
                "type": "http",
                "method": "GET",
                "path": "/events",
                "headers": [],
                "asgi": {"spec_version": "2.4"},
            },
            receive,
            failing_send,
        )

    with pytest.raises(ClientDisconnect):
        asyncio.run(invoke())
    assert controller.active_count == 0
    reacquired = controller.try_acquire("recovery-a")
    assert reacquired is not None
    reacquired.release()


def test_response_construction_failure_releases_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = EventStreamAdmissionController(
        max_active=1,
        max_per_recovery=1,
        retry_seconds=5,
    )
    lease = controller.try_acquire("recovery-a")
    assert lease is not None

    async def source() -> AsyncIterator[str]:
        yield "id: 1\n\n"

    def fail_construction(*_args, **_kwargs):
        raise RuntimeError("response construction failed")

    monkeypatch.setattr(
        "server.main._LeaseReleasingStreamingResponse",
        fail_construction,
    )
    with pytest.raises(RuntimeError, match="response construction failed"):
        _build_admitted_event_stream_response(
            lease_event_stream(source(), lease),
            lease,
        )
    assert controller.active_count == 0
