import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from server import main as server_main
from server.config import RuntimeSettings
from server.events import EventStreamAdmissionController, lease_event_stream
from server.main import _build_admitted_event_stream_response, create_app
from server.models import RecoveryStatus
from server.store import SQLiteStore

_INVALID_EVENT_CURSOR_DETAIL = (
    "Last-Event-ID must contain only ASCII digits and be between 0 and 9223372036854775807"
)


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
        json={
            "scenarioId": "api-quota",
            "executionMode": "replay_fixture",
            "clientRequestId": uuid4().hex,
        },
    )
    assert response.status_code == 201
    return str(response.json()["recoveryId"])


def _stable_security_headers(response) -> dict[str, str]:
    return {
        name: response.headers[name]
        for name in (
            "cache-control",
            "content-security-policy",
            "content-type",
            "permissions-policy",
            "referrer-policy",
            "x-content-type-options",
            "x-frame-options",
        )
    }


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
                "message": ("Event streaming is temporarily at capacity; retry is automatic."),
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
            f"retry: 5000\nevent: stream.capacity\ndata: {expected_payload}\n\n"
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
            headers={"Last-Event-ID": str(2**63)},
        )
        absent_response = owner.get(
            "/api/recoveries/11111111-2222-4333-8444-555555555555/events",
            headers={"Last-Event-ID": str(2**63)},
        )
        duplicate_headers = [
            ("Last-Event-ID", "0"),
            ("Last-Event-ID", str(2**63)),
        ]
        foreign_duplicate = foreign.get(
            f"/api/recoveries/{recovery_id}/events",
            headers=duplicate_headers,
        )
        absent_duplicate = owner.get(
            "/api/recoveries/11111111-2222-4333-8444-555555555555/events",
            headers=duplicate_headers,
        )
        invalid_cursor = owner.get(
            f"/api/recoveries/{recovery_id}/events",
            headers={"Last-Event-ID": str(2**63)},
        )

        assert foreign_response.status_code == absent_response.status_code == 404
        assert foreign_response.content == absent_response.content == b'{"detail":"Not found"}'
        assert _stable_security_headers(foreign_response) == _stable_security_headers(
            absent_response
        )
        assert foreign_duplicate.status_code == absent_duplicate.status_code == 404
        assert foreign_duplicate.content == absent_duplicate.content == b'{"detail":"Not found"}'
        assert _stable_security_headers(foreign_duplicate) == _stable_security_headers(
            absent_duplicate
        )
        assert invalid_cursor.status_code == 400
        assert invalid_cursor.json() == {"detail": _INVALID_EVENT_CURSOR_DETAIL}
        assert controller.active_count == 1
        held.release()


@pytest.mark.parametrize(
    "last_event_id",
    [
        "",
        " ",
        "+1",
        "-0",
        "-1",
        "1_0",
        "true",
        "not-an-integer",
        str(2**63),
    ],
)
def test_invalid_cursor_is_rejected_before_stream_admission_or_event_batch_read(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    last_event_id: str,
) -> None:
    store = SQLiteStore(tmp_path / "event-stream-invalid-cursor.sqlite3")
    app = create_app(_settings(), store=store)
    with TestClient(app) as client:
        recovery_id = _create_terminal_recovery(client)
        controller = app.state.event_stream_admission
        acquire = Mock(side_effect=AssertionError("invalid cursor must not acquire a lease"))
        read_batch = Mock(side_effect=AssertionError("invalid cursor must not stream events"))
        monkeypatch.setattr(controller, "try_acquire", acquire)
        monkeypatch.setattr(store, "read_event_batch", read_batch)

        response = client.get(
            f"/api/recoveries/{recovery_id}/events",
            headers={"Last-Event-ID": last_event_id},
        )

        assert response.status_code == 400
        assert response.json() == {"detail": _INVALID_EVENT_CURSOR_DETAIL}
        assert response.headers["content-type"] == "application/json"
        assert response.headers["cache-control"] == "no-store"
        assert "x-accel-buffering" not in response.headers
        assert acquire.call_count == 0
        assert read_batch.call_count == 0
        assert controller.active_count == 0


def test_cursor_parser_accepts_leading_zeroes_but_rejects_unicode_digits() -> None:
    assert server_main._parse_event_cursor("0001") == 1
    assert server_main._parse_event_cursor("0" * 5_000 + str(2**63 - 1)) == 2**63 - 1
    with pytest.raises(ValueError):
        server_main._parse_event_cursor("9" * 5_000)
    for unicode_digits in ("\u0661", "\uff11"):
        with pytest.raises(ValueError):
            server_main._parse_event_cursor(unicode_digits)


def test_oversized_cursor_does_not_start_a_stream_or_log_an_internal_error(
    tmp_path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR)
    store = SQLiteStore(tmp_path / "event-stream-oversized-cursor.sqlite3")
    app = create_app(_settings(), store=store)
    with TestClient(app, raise_server_exceptions=False) as client:
        recovery_id = _create_terminal_recovery(client)
        caplog.clear()

        response = client.get(
            f"/api/recoveries/{recovery_id}/events",
            headers={"Last-Event-ID": str(2**63)},
        )

        assert response.status_code == 400
        assert response.json() == {"detail": _INVALID_EVENT_CURSOR_DETAIL}
        assert response.headers["content-type"] == "application/json"
        assert response.headers["cache-control"] == "no-store"
        assert "x-accel-buffering" not in response.headers
        assert app.state.event_stream_admission.active_count == 0
        assert not any(
            record.getMessage().startswith("request_failed") for record in caplog.records
        )


def test_sqlite_max_cursor_is_accepted_and_releases_its_stream_lease(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "event-stream-max-cursor.sqlite3")
    app = create_app(_settings(), store=store)
    with TestClient(app) as client:
        recovery_id = _create_terminal_recovery(client)

        response = client.get(
            f"/api/recoveries/{recovery_id}/events",
            headers={"Last-Event-ID": str(2**63 - 1)},
        )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"
        assert response.text == ""
        assert app.state.event_stream_admission.active_count == 0


def test_invalid_authorized_cursor_precedes_targeted_expiry_side_effect(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "event-stream-cursor-before-expiry.sqlite3"
    store = SQLiteStore(database_path)
    app = create_app(_settings(), store=store)
    with TestClient(app) as client:
        pending = client.post(
            "/api/recoveries",
            json={
                "scenarioId": "hotel",
                "executionMode": "sdk_stub",
                "clientRequestId": uuid4().hex,
            },
        )
        assert pending.status_code == 201
        recovery_id = str(pending.json()["recoveryId"])
        event_types_before = [event.type for event in store.list_events(recovery_id)]
        expiry = datetime.fromisoformat(str(pending.json()["pendingApproval"]["expiry"]))
        monkeypatch.setattr(
            store,
            "_now",
            lambda: expiry + timedelta(seconds=1),
        )

        response = client.get(
            f"/api/recoveries/{recovery_id}/events",
            headers={"Last-Event-ID": "+1"},
        )

        assert response.status_code == 400
        assert response.json() == {"detail": _INVALID_EVENT_CURSOR_DETAIL}
        assert store.get_recovery(recovery_id).status is RecoveryStatus.PENDING_APPROVAL
        assert [event.type for event in store.list_events(recovery_id)] == event_types_before
        assert app.state.event_stream_admission.active_count == 0


@pytest.mark.parametrize(
    "cursor_values",
    [
        ("0", str(2**63)),
        (str(2**63), "0"),
        ("1", "2"),
    ],
)
def test_duplicate_cursor_is_rejected_before_expiry_admission_read_or_log(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    cursor_values: tuple[str, str],
) -> None:
    caplog.set_level(logging.ERROR)
    database_path = tmp_path / "event-stream-duplicate-cursor.sqlite3"
    store = SQLiteStore(database_path)
    app = create_app(_settings(), store=store)
    with TestClient(app) as client:
        pending = client.post(
            "/api/recoveries",
            json={
                "scenarioId": "hotel",
                "executionMode": "sdk_stub",
                "clientRequestId": uuid4().hex,
            },
        )
        assert pending.status_code == 201
        recovery_id = str(pending.json()["recoveryId"])
        event_types_before = [event.type for event in store.list_events(recovery_id)]
        expiry = datetime.fromisoformat(str(pending.json()["pendingApproval"]["expiry"]))
        monkeypatch.setattr(
            store,
            "_now",
            lambda: expiry + timedelta(seconds=1),
        )
        controller = app.state.event_stream_admission
        acquire = Mock(side_effect=AssertionError("duplicate cursor must not acquire a lease"))
        read_batch = Mock(side_effect=AssertionError("duplicate cursor must not stream events"))
        original_read_batch = store.read_event_batch
        monkeypatch.setattr(controller, "try_acquire", acquire)
        monkeypatch.setattr(store, "read_event_batch", read_batch)
        caplog.clear()

        response = client.get(
            f"/api/recoveries/{recovery_id}/events",
            headers=[
                ("Last-Event-ID", cursor_values[0]),
                ("Last-Event-ID", cursor_values[1]),
            ],
        )

        assert response.status_code == 400
        assert response.json() == {"detail": _INVALID_EVENT_CURSOR_DETAIL}
        assert response.headers["content-type"] == "application/json"
        assert response.headers["cache-control"] == "no-store"
        assert "x-accel-buffering" not in response.headers
        assert store.get_recovery(recovery_id).status is RecoveryStatus.PENDING_APPROVAL
        assert acquire.call_count == 0
        assert read_batch.call_count == 0
        assert controller.active_count == 0
        persisted_events, _status = original_read_batch(recovery_id)
        assert [event.type for event in persisted_events] == event_types_before
        assert not any(
            record.getMessage().startswith("request_failed") for record in caplog.records
        )


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


def test_target_expiry_is_committed_before_saturated_admission(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "event-stream-expiry-order.sqlite3"
    store = SQLiteStore(database_path)
    app = create_app(_settings(), store=store)
    with TestClient(app) as client:
        pending = client.post(
            "/api/recoveries",
            json={
                "scenarioId": "hotel",
                "executionMode": "sdk_stub",
                "clientRequestId": uuid4().hex,
            },
        )
        assert pending.status_code == 201
        recovery_id = str(pending.json()["recoveryId"])
        expiry = datetime.fromisoformat(str(pending.json()["pendingApproval"]["expiry"]))
        monkeypatch.setattr(
            store,
            "_now",
            lambda: expiry + timedelta(seconds=1),
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
