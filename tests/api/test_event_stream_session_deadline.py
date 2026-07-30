from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from server.config import RuntimeSettings
from server.main import create_app
from server.store import SQLiteStore


def _stable_not_found_headers(response) -> dict[str, str]:
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


def test_event_stream_receives_exact_verified_session_expiry_and_releases_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteStore(tmp_path / "event-stream-session-deadline.sqlite3")
    settings = RuntimeSettings(
        live_ready=False,
        max_concurrent_event_streams=1,
        max_event_streams_per_recovery=1,
    )
    app = create_app(settings, store=store)
    captured_options: dict[str, object] = {}

    async def finite_stream(
        *_args: object,
        **options: object,
    ) -> AsyncIterator[str]:
        captured_options.update(options)
        yield ""

    monkeypatch.setattr("server.main.stream_recovery_events", finite_stream)

    try:
        with TestClient(app) as client:
            created = client.post(
                "/api/recoveries",
                json={
                    "scenarioId": "api-quota",
                    "executionMode": "replay_fixture",
                    "clientRequestId": uuid4().hex,
                },
            )
            assert created.status_code == 201
            recovery_id = str(created.json()["recoveryId"])

            raw_cookie = client.cookies.get(settings.demo_session_cookie_name)
            assert raw_cookie is not None
            verified_session = app.state.public_demo_controls._verified_session_nonce(
                raw_cookie,
                current=datetime.now(UTC),
            )
            assert verified_session is not None
            _nonce, expected_session_expiry = verified_session

            response = client.get(f"/api/recoveries/{recovery_id}/events")

            assert response.status_code == 200
            assert response.text == ""
            assert app.state.event_stream_admission.active_count == 0
            assert captured_options.get("public_session_expires_at") == expected_session_expiry
    finally:
        store.close()


def test_expired_stream_session_reconnect_gets_new_identity_and_generic_404(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issued_at = datetime.now(UTC).replace(microsecond=0)

    class FrozenDateTime(datetime):
        current = issued_at

        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return cls.current.replace(tzinfo=None)
            return cls.current.astimezone(tz)

    monkeypatch.setattr("server.controls.datetime", FrozenDateTime)
    store = SQLiteStore(tmp_path / "expired-stream-session.sqlite3")
    settings = RuntimeSettings(
        live_ready=False,
        identity_hash_secret="event-stream-deadline-secret-0123456789abcdef",
        demo_session_lifetime_seconds=60,
    )
    app = create_app(settings, store=store)

    try:
        with TestClient(app) as client:
            created = client.post(
                "/api/recoveries",
                json={
                    "scenarioId": "api-quota",
                    "executionMode": "replay_fixture",
                    "clientRequestId": uuid4().hex,
                },
            )
            assert created.status_code == 201
            recovery_id = str(created.json()["recoveryId"])
            original_cookie = client.cookies.get(settings.demo_session_cookie_name)
            assert original_cookie is not None

            FrozenDateTime.current = issued_at + timedelta(seconds=61)
            expired_owner = client.get(f"/api/recoveries/{recovery_id}/events")
            replacement_cookie = expired_owner.cookies.get(settings.demo_session_cookie_name)
            absent = client.get(
                "/api/recoveries/11111111-2222-4333-8444-555555555555/events"
            )

            assert replacement_cookie is not None
            assert replacement_cookie != original_cookie
            assert expired_owner.status_code == absent.status_code == 404
            assert expired_owner.content == absent.content == b'{"detail":"Not found"}'
            assert _stable_not_found_headers(expired_owner) == _stable_not_found_headers(absent)
            assert "x-accel-buffering" not in expired_owner.headers
            assert app.state.event_stream_admission.active_count == 0
    finally:
        store.close()
