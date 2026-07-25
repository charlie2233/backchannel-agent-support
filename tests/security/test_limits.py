from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from threading import Barrier
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from server.config import RuntimeSettings
from server.controls import (
    LiveConcurrencyGate,
    LiveConcurrencyLimitError,
    PublicBoundaryMiddleware,
    PublicIdentityHasher,
    PublicLiveAdmissionError,
    resolve_client_ip,
)
from server.main import create_app
from server.store import SQLiteStore


class _SessionStoreSpy:
    def __init__(self) -> None:
        self.lookups = 0

    def is_demo_session_active(self, **_kwargs: object) -> bool:
        self.lookups += 1
        return False


def _raw_boundary_request(
    *,
    headers: list[tuple[bytes, bytes]],
    chunks: list[bytes],
    limit: int = 64,
    peer_ip: str = "127.0.0.1",
    trusted_proxy_cidrs: tuple[str, ...] = (),
    method: str = "POST",
    path: str = "/raw-boundary-probe",
) -> tuple[int, dict[str, object], _SessionStoreSpy, int, dict[str, object]]:
    session_store = _SessionStoreSpy()
    downstream_calls = 0
    observed_state: dict[str, object] = {}
    sent: list[dict[str, object]] = []

    async def downstream(scope, receive, send) -> None:
        nonlocal downstream_calls, observed_state
        downstream_calls += 1
        observed_state = dict(scope["state"])
        while True:
            message = await receive()
            if message["type"] != "http.request" or not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = PublicBoundaryMiddleware(
        downstream,
        max_body_bytes=limit,
        identity_hasher=PublicIdentityHasher(
            "test-identity-secret-that-is-at-least-32-bytes"
        ),
        session_ttl_seconds=3600,
        trusted_proxy_cidrs=trusted_proxy_cidrs,
        deployed=False,
    )
    request_messages = [
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(chunks) - 1,
        }
        for index, chunk in enumerate(chunks or [b""])
    ]

    async def receive() -> dict[str, object]:
        if request_messages:
            return request_messages.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": (peer_ip, 43123),
        "server": ("testserver", 80),
        "state": {},
    }
    asyncio.run(middleware(scope, receive, send))  # type: ignore[arg-type]
    start = next(message for message in sent if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    decoded = json.loads(body) if body else {}
    return int(start["status"]), decoded, session_store, downstream_calls, observed_state


def _live_settings(**overrides: object) -> RuntimeSettings:
    values: dict[str, object] = {
        "live_ready": False,
        "identity_hmac_secret": "test-identity-secret-that-is-at-least-32-bytes",
        "live_daily_budget": 2,
        "live_cooldown": timedelta(0),
        "cleanup_interval": timedelta(hours=1),
    }
    values.update(overrides)
    return RuntimeSettings(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("route_path", ["/health", "/readyz"])
def test_boundary_replaces_duplicate_cache_control_for_resolved_operational_routes(
    route_path: str,
) -> None:
    sent: list[dict[str, object]] = []

    async def downstream(scope, receive, send) -> None:
        del receive
        scope["route"] = SimpleNamespace(path=route_path)
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"Cache-Control", b"max-age=3600"),
                    (b"cAcHe-CoNtRoL", b"private"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b"{}"})

    middleware = PublicBoundaryMiddleware(
        downstream,
        max_body_bytes=64,
        identity_hasher=PublicIdentityHasher(
            "test-identity-secret-that-is-at-least-32-bytes"
        ),
        session_ttl_seconds=3600,
        trusted_proxy_cidrs=(),
        deployed=False,
    )
    request_messages = [{"type": "http.request", "body": b"", "more_body": False}]

    async def receive() -> dict[str, object]:
        if request_messages:
            return request_messages.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/not-the-resolved-route",
        "raw_path": b"/not-the-resolved-route",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 43123),
        "server": ("testserver", 80),
        "state": {},
    }

    asyncio.run(middleware(scope, receive, send))  # type: ignore[arg-type]

    response_start = next(
        message for message in sent if message["type"] == "http.response.start"
    )
    headers = response_start["headers"]  # type: ignore[assignment]
    cache_control = [
        (name, value)
        for name, value in headers  # type: ignore[union-attr]
        if name.lower() == b"cache-control"
    ]
    assert cache_control == [(b"cache-control", b"no-store")]


def test_raw_boundary_accepts_exact_multibyte_byte_limit_without_content_length() -> None:
    body = "é".encode() * 32

    status, response, session_store, downstream_calls, _state = _raw_boundary_request(
        headers=[],
        chunks=[body[:17], body[17:]],
        limit=64,
    )

    assert status == 204
    assert response == {}
    assert downstream_calls == 1
    assert session_store.lookups == 0


def test_raw_boundary_accepts_distinct_cookies_in_one_unambiguous_header() -> None:
    status, response, session_store, downstream_calls, _state = _raw_boundary_request(
        headers=[(b"cookie", b"first=1; second=2")],
        chunks=[b""],
    )

    assert status == 204
    assert response == {}
    assert downstream_calls == 1
    assert session_store.lookups == 0


def test_scoped_create_route_issues_identity_without_persisting_a_session() -> None:
    status, response, session_store, downstream_calls, state = _raw_boundary_request(
        headers=[(b"content-type", b"application/json")],
        chunks=[b"{}"],
        path="/api/recoveries",
    )

    assert status == 204
    assert response == {}
    assert downstream_calls == 1
    identity = state["public_identity"]
    assert str(identity.session_hash).startswith("hmac-sha256:")  # type: ignore[union-attr]
    assert str(identity.ip_hash).startswith("hmac-sha256:")  # type: ignore[union-attr]
    assert session_store.lookups == 0


@pytest.mark.parametrize(
    ("headers", "chunks", "expected_status", "expected_code"),
    [
        ([], [b"x" * 64, b"y"], 413, "request_too_large"),
        ([(b"content-length", b"65")], [b"x"], 413, "request_too_large"),
        ([(b"content-length", b"1")], [b"xy"], 400, "invalid_request"),
        ([(b"content-length", b"3")], [b"xy"], 400, "invalid_request"),
        (
            [(b"content-length", b"2"), (b"content-length", b"2")],
            [b"xy"],
            400,
            "invalid_request",
        ),
        ([(b"content-length", b"invalid")], [b"x"], 400, "invalid_request"),
        ([(b"content-length", b"-1")], [b"x"], 400, "invalid_request"),
        (
            [(b"content-length", b"2"), (b"transfer-encoding", b"chunked")],
            [b"xy"],
            400,
            "invalid_request",
        ),
        (
            [(b"transfer-encoding", b"gzip")],
            [b"xy"],
            400,
            "invalid_request",
        ),
        (
            [
                (b"content-type", b"application/json"),
                (b"content-type", b"text/plain"),
            ],
            [b""],
            400,
            "invalid_request",
        ),
        (
            [(b"cookie", b"first=1"), (b"cookie", b"second=2")],
            [b""],
            400,
            "invalid_request",
        ),
        (
            [
                (
                    b"cookie",
                    b"backchannel_demo_session=first; "
                    b"backchannel_demo_session=second",
                )
            ],
            [b""],
            400,
            "invalid_request",
        ),
    ],
)
def test_raw_boundary_rejects_ambiguous_or_oversized_framing_before_side_effects(
    headers: list[tuple[bytes, bytes]],
    chunks: list[bytes],
    expected_status: int,
    expected_code: str,
) -> None:
    status, response, session_store, downstream_calls, _state = _raw_boundary_request(
        headers=headers,
        chunks=chunks,
    )

    assert status == expected_status
    assert response["error"]["code"] == expected_code  # type: ignore[index]
    assert downstream_calls == 0
    assert session_store.lookups == 0


def test_trusted_forwarding_is_bounded_unambiguous_and_right_to_left() -> None:
    assert (
        resolve_client_ip(
            peer_ip="10.0.0.8",
            forwarded_for="198.51.100.99, 203.0.113.20, 10.0.0.7",
            trusted_proxy_cidrs=("10.0.0.0/8",),
        )
        == "203.0.113.20"
    )
    assert (
        resolve_client_ip(
            peer_ip="::ffff:192.0.2.9",
            forwarded_for="203.0.113.20",
            trusted_proxy_cidrs=(),
        )
        == "192.0.2.9"
    )

    for forwarded_headers in (
        [(b"x-forwarded-for", b"not-an-ip")],
        [
            (b"x-forwarded-for", b"203.0.113.20"),
            (b"x-forwarded-for", b"198.51.100.99"),
        ],
    ):
        status, response, session_store, downstream_calls, _state = _raw_boundary_request(
            headers=[(b"content-type", b"application/json"), *forwarded_headers],
            chunks=[b"{}"],
            peer_ip="10.0.0.8",
            trusted_proxy_cidrs=("10.0.0.0/8",),
            path="/api/recoveries",
        )
        assert status == 400
        assert response["error"]["code"] == "invalid_request"  # type: ignore[index]
        assert downstream_calls == 0
        assert session_store.lookups == 0


def test_request_body_cap_uses_actual_bytes_when_content_length_lies(tmp_path) -> None:
    settings = RuntimeSettings(
        live_ready=False,
        max_request_body_bytes=64,
        identity_hmac_secret="test-identity-secret-that-is-at-least-32-bytes",
    )
    store = SQLiteStore(tmp_path / "body-limit.sqlite3")

    with TestClient(create_app(settings, store=store)) as client:
        response = client.request(
            "POST",
            "/api/recoveries",
            content=b'{"scenarioId":"hotel","executionMode":"replay_fixture","pad":"'
            + b"x" * 256
            + b'"}',
            headers={
                "Content-Type": "application/json",
                "Content-Length": "1",
            },
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"
    assert response.headers["x-request-id"].startswith("req_")


def test_request_body_cap_rejects_streamed_body_without_content_length(tmp_path) -> None:
    settings = RuntimeSettings(
        live_ready=False,
        max_request_body_bytes=64,
        identity_hmac_secret="test-identity-secret-that-is-at-least-32-bytes",
    )
    store = SQLiteStore(tmp_path / "chunked-limit.sqlite3")

    def chunks() -> list[bytes]:
        return [b"{" + b"x" * 40, b"y" * 40 + b"}"]

    with TestClient(create_app(settings, store=store)) as client:
        response = client.request(
            "POST",
            "/api/recoveries",
            content=iter(chunks()),
            headers={
                "Content-Type": "application/json",
                "Transfer-Encoding": "chunked",
            },
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"


def test_strict_body_media_type_and_security_headers(tmp_path) -> None:
    settings = RuntimeSettings(
        live_ready=False,
        identity_hmac_secret="test-identity-secret-that-is-at-least-32-bytes",
    )
    store = SQLiteStore(tmp_path / "strict-boundary.sqlite3")
    with TestClient(create_app(settings, store=store)) as client:
        extra = client.post(
            "/api/recoveries",
            json={
                "scenarioId": "hotel",
                "executionMode": "replay_fixture",
                "unexpected": "must-fail",
            },
        )
        media = client.post(
            "/api/recoveries",
            content='{"scenarioId":"hotel","executionMode":"replay_fixture"}',
            headers={"Content-Type": "text/plain"},
        )
        healthy = client.get("/health")

    assert extra.status_code == 422
    assert extra.json()["error"]["code"] == "invalid_request"
    assert media.status_code == 415
    assert media.json()["error"]["code"] == "unsupported_media_type"
    assert healthy.headers["x-content-type-options"] == "nosniff"
    assert healthy.headers["x-frame-options"] == "DENY"
    assert healthy.headers["referrer-policy"] == "no-referrer"
    assert "camera=()" in healthy.headers["permissions-policy"]


def test_boundary_replaces_inner_security_headers_and_closes_failed_stream(
    caplog,
) -> None:
    sent: list[dict[str, object]] = []

    async def failing_event_stream(scope, receive, send) -> None:
        del scope, receive
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"text/event-stream"),
                    (b"x-request-id", b"attacker-request-id"),
                    (b"x-frame-options", b"ALLOWALL"),
                    (b"content-security-policy", b"default-src * 'unsafe-inline'"),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b"event: update\ndata: safe-partial\n\n",
                "more_body": True,
            }
        )
        raise RuntimeError("stream-prompt-canary tool-payload-canary")

    middleware = PublicBoundaryMiddleware(
        failing_event_stream,
        max_body_bytes=64,
        identity_hasher=PublicIdentityHasher(
            "test-identity-secret-that-is-at-least-32-bytes"
        ),
        session_ttl_seconds=3600,
        trusted_proxy_cidrs=(),
        deployed=False,
    )
    request_messages = [
        {"type": "http.request", "body": b"", "more_body": False}
    ]

    async def receive() -> dict[str, object]:
        if request_messages:
            return request_messages.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/recoveries/11111111-2222-4333-8444-555555555555/events",
        "raw_path": b"/api/recoveries/11111111-2222-4333-8444-555555555555/events",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 43123),
        "server": ("testserver", 80),
        "state": {},
    }

    with caplog.at_level(logging.INFO, logger="server.public"):
        asyncio.run(middleware(scope, receive, send))  # type: ignore[arg-type]

    starts = [message for message in sent if message["type"] == "http.response.start"]
    assert len(starts) == 1
    start_headers = starts[0]["headers"]
    assert isinstance(start_headers, list)
    assert [value for key, value in start_headers if key.lower() == b"x-frame-options"] == [
        b"DENY"
    ]
    assert [
        value
        for key, value in start_headers
        if key.lower() == b"content-security-policy"
    ] == [
        b"default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
        b"object-src 'none'; form-action 'self'"
    ]
    request_ids = [
        value for key, value in start_headers if key.lower() == b"x-request-id"
    ]
    assert len(request_ids) == 1
    assert request_ids[0].startswith(b"req_")
    assert request_ids[0] != b"attacker-request-id"

    bodies = [message for message in sent if message["type"] == "http.response.body"]
    assert bodies[-1] == {
        "type": "http.response.body",
        "body": b"",
        "more_body": False,
    }
    public_logs = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name == "server.public"
    )
    assert "event=response_failed" in public_logs
    assert "code=internal_error" in public_logs
    assert request_ids[0].decode("ascii") in public_logs
    assert "stream-prompt-canary" not in public_logs
    assert "tool-payload-canary" not in public_logs
    assert "RuntimeError" not in public_logs


def test_cookie_is_server_issued_and_hardened_in_deployed_mode(tmp_path) -> None:
    settings = _live_settings(
        deployed=True,
        cors_origins=("https://demo.example",),
    )
    store = SQLiteStore(tmp_path / "cookie.sqlite3")

    with TestClient(create_app(settings, store=store)) as client:
        response = client.post(
            "/api/recoveries",
            json={"scenarioId": "hotel", "executionMode": "replay_fixture"},
            headers={"Origin": "https://demo.example"},
        )

    cookie = response.headers["set-cookie"]
    assert cookie.startswith("backchannel_demo_session=")
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=lax" in cookie
    assert "max-age=" in cookie.lower()


def test_deployed_cors_allows_only_the_exact_configured_origin(tmp_path) -> None:
    settings = _live_settings(
        deployed=True,
        cors_origins=("https://demo.example",),
    )
    store = SQLiteStore(tmp_path / "cors.sqlite3")
    preflight_headers = {
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type",
    }

    with TestClient(create_app(settings, store=store)) as client:
        allowed = client.options(
            "/api/recoveries",
            headers={"Origin": "https://demo.example", **preflight_headers},
        )
        denied = client.options(
            "/api/recoveries",
            headers={"Origin": "https://demo.example.evil.test", **preflight_headers},
        )

    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "https://demo.example"
    assert allowed.headers["access-control-allow-credentials"] == "true"
    assert allowed.headers["strict-transport-security"].startswith("max-age=")
    assert denied.status_code == 400
    assert "access-control-allow-origin" not in denied.headers


def test_deployed_reset_rejects_cross_origin_and_form_style_posts(tmp_path) -> None:
    settings = _live_settings(
        deployed=True,
        cors_origins=("https://demo.example",),
        demo_reset_enabled=True,
    )
    store = SQLiteStore(tmp_path / "reset-origin.sqlite3")

    with TestClient(create_app(settings, store=store)) as client:
        created = client.post(
            "/api/recoveries",
            json={"scenarioId": "hotel", "executionMode": "replay_fixture"},
            headers={"Origin": "https://demo.example"},
        )
        assert created.status_code == 201
        assert store.count_recoveries() == 1
        session_cookie = _cookie_value(created.headers["set-cookie"])
        cookie_header = f"backchannel_demo_session={session_cookie}"
        cross_origin = client.post(
            "/api/demo/reset",
            content=b"",
            headers={
                "Origin": "https://evil.example",
                "Content-Type": "application/x-www-form-urlencoded",
                "Cookie": cookie_header,
            },
        )
        assert store.count_recoveries() == 1
        no_origin_form = client.post(
            "/api/demo/reset",
            content=b"",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Cookie": cookie_header,
            },
        )
        assert store.count_recoveries() == 1
        same_origin_json = client.post(
            "/api/demo/reset",
            content=b"{}",
            headers={
                "Origin": "https://demo.example",
                "Content-Type": "application/json",
                "Cookie": cookie_header,
            },
        )

    assert cross_origin.status_code == 403
    assert cross_origin.json()["error"]["code"] == "invalid_request"
    assert no_origin_form.status_code == 415
    assert no_origin_form.json()["error"]["code"] == "unsupported_media_type"
    assert same_origin_json.status_code == 200
    assert store.count_recoveries() == 0


def test_local_cookie_keeps_http_only_and_same_site_without_secure(tmp_path) -> None:
    settings = RuntimeSettings(
        live_ready=False,
        identity_hmac_secret="test-identity-secret-that-is-at-least-32-bytes",
    )
    store = SQLiteStore(tmp_path / "local-cookie.sqlite3")

    with TestClient(create_app(settings, store=store)) as client:
        response = client.post(
            "/api/recoveries",
            json={"scenarioId": "hotel", "executionMode": "replay_fixture"},
        )

    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Secure" not in cookie


def _cookie_value(set_cookie: str) -> str:
    parsed = SimpleCookie()
    parsed.load(set_cookie)
    return parsed["backchannel_demo_session"].value


def test_unknown_and_expired_session_cookies_fail_closed_without_rotation(tmp_path) -> None:
    settings = RuntimeSettings(
        live_ready=False,
        identity_hmac_secret="test-identity-secret-that-is-at-least-32-bytes",
    )
    store = SQLiteStore(tmp_path / "cookie-rejection.sqlite3")
    hasher = PublicIdentityHasher(settings.identity_hmac_secret)
    codec = hasher.demo_session_cookie_codec(
        lifetime_seconds=int(settings.demo_session_ttl.total_seconds())
    )
    expired, _credential = codec.mint(
        now=datetime.now(UTC) - settings.demo_session_ttl - timedelta(seconds=1)
    )

    with TestClient(create_app(settings, store=store)) as client:
        unknown = "A" * 43
        client.cookies.set("backchannel_demo_session", unknown)
        unknown_response = client.post(
            "/api/recoveries",
            json={"scenarioId": "hotel", "executionMode": "replay_fixture"},
        )
        client.cookies.clear()
        client.cookies.set("backchannel_demo_session", expired)
        expired_response = client.post(
            "/api/recoveries",
            json={"scenarioId": "hotel", "executionMode": "replay_fixture"},
        )

    for response in (unknown_response, expired_response):
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"
        assert "set-cookie" not in response.headers
    assert store.count_recoveries() == 0


def test_irrelevant_routes_preflight_and_replays_do_not_allocate_durable_sessions(
    tmp_path,
) -> None:
    settings = RuntimeSettings(
        live_ready=False,
        identity_hmac_secret="test-identity-secret-that-is-at-least-32-bytes",
    )
    store = SQLiteStore(tmp_path / "no-session-allocation.sqlite3")

    with TestClient(create_app(settings, store=store)) as client:
        responses = [
            client.get("/health"),
            client.get("/readyz"),
            client.get("/api/scenarios"),
            client.get("/missing"),
            client.options(
                "/api/recoveries",
                headers={
                    "Origin": "http://testserver",
                    "Access-Control-Request-Method": "POST",
                },
            ),
        ]
        replay_responses = [
            client.post(
                "/api/recoveries",
                json={"scenarioId": "hotel", "executionMode": "replay_fixture"},
            )
            for _ in range(3)
        ]

    assert all("set-cookie" not in response.headers for response in responses)
    assert all(response.status_code == 201 for response in replay_responses)
    assert store.count_demo_sessions() == 0
    assert store.count_public_live_usage_rows() == 0


def test_non_live_creation_requests_do_not_refresh_an_admitted_session(
    tmp_path,
) -> None:
    settings = _live_settings(
        deployed=True,
        cors_origins=("https://demo.example",),
    )
    database_path = tmp_path / "admission-owned-session.sqlite3"
    store = SQLiteStore(database_path)
    hasher = PublicIdentityHasher(settings.identity_hmac_secret)
    codec = hasher.demo_session_cookie_codec(
        lifetime_seconds=int(settings.demo_session_ttl.total_seconds())
    )
    signed_session, credential = codec.mint(now=datetime.now(UTC))
    session_hash = hasher.session(credential.nonce)
    admitted_at = datetime.now(UTC) - timedelta(minutes=5)
    store.claim_public_live_admission(
        session_hash=session_hash,
        ip_hash=hasher.ip("127.0.0.1"),
        now=admitted_at,
        cooldown=timedelta(0),
        daily_budget=10,
        session_expires_at=admitted_at + timedelta(days=1),
    )

    def identity_rows() -> tuple[list[tuple[object, ...]], ...]:
        with sqlite3.connect(database_path) as connection:
            return (
                connection.execute(
                    """
                    SELECT session_hash, created_at, last_seen_at, expires_at
                    FROM demo_sessions ORDER BY session_hash
                    """
                ).fetchall(),
                connection.execute(
                    """
                    SELECT identity_kind, identity_hash, usage_day, amount,
                           last_admitted_at, updated_at
                    FROM usage_ledger
                    ORDER BY identity_kind, identity_hash, usage_day
                    """
                ).fetchall(),
                connection.execute(
                    """
                    SELECT identity_kind, identity_hash, last_admitted_at, updated_at
                    FROM public_live_cooldowns
                    ORDER BY identity_kind, identity_hash
                    """
                ).fetchall(),
            )

    before = identity_rows()
    headers = {"Origin": "https://demo.example"}
    with TestClient(create_app(settings, store=store)) as client:
        client.cookies.set("backchannel_demo_session", signed_session)
        replay = client.post(
            "/api/recoveries",
            json={"scenarioId": "hotel", "executionMode": "replay_fixture"},
            headers=headers,
        )
        invalid = client.post(
            "/api/recoveries",
            json={"scenarioId": "unknown", "executionMode": "replay_fixture"},
            headers=headers,
        )
        unavailable_sdk = client.post(
            "/api/recoveries",
            json={"scenarioId": "hotel", "executionMode": "sdk_stub"},
            headers=headers,
        )
        unavailable_live = client.post(
            "/api/recoveries",
            json={"scenarioId": "hotel", "executionMode": "openai_live"},
            headers=headers,
        )

    assert replay.status_code == 201
    assert invalid.status_code == 422
    assert unavailable_sdk.status_code == 422
    assert unavailable_live.status_code == 503
    assert all(
        "set-cookie" not in response.headers
        for response in (replay, invalid, unavailable_sdk, unavailable_live)
    )
    assert identity_rows() == before


def test_proxy_headers_are_off_by_default_and_only_trusted_by_cidr() -> None:
    assert (
        resolve_client_ip(
            peer_ip="127.0.0.1",
            forwarded_for="203.0.113.9",
            trusted_proxy_cidrs=(),
        )
        == "127.0.0.1"
    )
    assert (
        resolve_client_ip(
            peer_ip="10.1.2.3",
            forwarded_for="203.0.113.9, 10.2.3.4",
            trusted_proxy_cidrs=("10.0.0.0/8",),
        )
        == "203.0.113.9"
    )
    assert (
        resolve_client_ip(
            peer_ip="192.0.2.40",
            forwarded_for="203.0.113.9",
            trusted_proxy_cidrs=("10.0.0.0/8",),
        )
        == "192.0.2.40"
    )


def test_identity_hashes_are_domain_separated_and_do_not_persist_raw_values(tmp_path) -> None:
    raw_session = "66928ea8-e058-4e4c-bba2-56311673beef"
    raw_ip = "203.0.113.77"
    hasher = PublicIdentityHasher(
        "test-identity-secret-that-is-at-least-32-bytes"
    )
    session_hash = hasher.session(raw_session)
    ip_hash = hasher.ip(raw_ip)
    assert session_hash != ip_hash
    assert raw_session not in session_hash
    assert raw_ip not in ip_hash

    database_path = tmp_path / "hashed-identity.sqlite3"
    store = SQLiteStore(database_path)
    now = datetime(2026, 7, 19, 8, tzinfo=UTC)
    store.claim_public_live_admission(
        session_hash=session_hash,
        ip_hash=ip_hash,
        now=now,
        cooldown=timedelta(0),
        daily_budget=2,
        session_expires_at=now + timedelta(days=1),
    )
    store.close()

    with sqlite3.connect(database_path) as connection:
        persisted = "\n".join(connection.iterdump())
    assert raw_session not in persisted
    assert raw_ip not in persisted
    assert session_hash in persisted
    assert ip_hash in persisted


def test_two_store_admission_race_has_one_atomic_daily_budget_winner(tmp_path) -> None:
    database_path = tmp_path / "admission-race.sqlite3"
    first_store = SQLiteStore(database_path)
    second_store = SQLiteStore(database_path)
    barrier = Barrier(2)
    now = datetime(2026, 7, 19, 9, tzinfo=UTC)

    def claim(store: SQLiteStore) -> str:
        barrier.wait(timeout=5)
        try:
            store.claim_public_live_admission(
                session_hash="hmac-sha256:" + "a" * 64,
                ip_hash="hmac-sha256:" + "b" * 64,
                now=now,
                cooldown=timedelta(0),
                daily_budget=1,
                session_expires_at=now + timedelta(days=1),
            )
        except PublicLiveAdmissionError as error:
            return error.code
        return "admitted"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim, (first_store, second_store)))

    assert sorted(outcomes) == ["admitted", "live_daily_budget_exceeded"]
    assert first_store.public_live_usage(
        identity_kind="session",
        identity_hash="hmac-sha256:" + "a" * 64,
        usage_day="2026-07-19",
    ) == 1
    assert first_store.public_live_usage(
        identity_kind="global",
        identity_hash="public-live-global",
        usage_day="2026-07-19",
    ) == 1


def test_session_and_ip_cooldowns_each_block_without_partial_usage(tmp_path) -> None:
    database_path = tmp_path / "dual-cooldown.sqlite3"
    store = SQLiteStore(database_path)
    session_a = "hmac-sha256:" + "1" * 64
    session_b = "hmac-sha256:" + "2" * 64
    ip_a = "hmac-sha256:" + "3" * 64
    ip_b = "hmac-sha256:" + "4" * 64
    now = datetime(2026, 7, 19, 23, 59, 59, tzinfo=UTC)
    expiry = now + timedelta(days=1)
    store.claim_public_live_admission(
        session_hash=session_a,
        ip_hash=ip_a,
        now=now,
        cooldown=timedelta(minutes=1),
        daily_budget=10,
        session_expires_at=expiry,
    )

    with pytest.raises(PublicLiveAdmissionError, match="live_cooldown"):
        store.claim_public_live_admission(
            session_hash=session_a,
            ip_hash=ip_b,
            now=now + timedelta(seconds=2),
            cooldown=timedelta(minutes=1),
            daily_budget=10,
            session_expires_at=expiry,
        )
    with pytest.raises(PublicLiveAdmissionError, match="live_cooldown"):
        store.claim_public_live_admission(
            session_hash=session_b,
            ip_hash=ip_a,
            now=now + timedelta(seconds=2),
            cooldown=timedelta(minutes=1),
            daily_budget=10,
            session_expires_at=expiry,
        )

    assert store.public_live_usage(
        identity_kind="session",
        identity_hash=session_b,
        usage_day="2026-07-20",
    ) == 0
    assert store.public_live_usage(
        identity_kind="ip",
        identity_hash=ip_b,
        usage_day="2026-07-20",
    ) == 0
    store.reset()
    store.close()

    reopened = SQLiteStore(database_path)
    with pytest.raises(PublicLiveAdmissionError, match="live_cooldown"):
        reopened.claim_public_live_admission(
            session_hash=session_a,
            ip_hash=ip_b,
            now=now + timedelta(seconds=3),
            cooldown=timedelta(minutes=1),
            daily_budget=10,
            session_expires_at=expiry,
        )


def test_two_store_cooldown_race_has_one_winner(tmp_path) -> None:
    database_path = tmp_path / "cooldown-race.sqlite3"
    stores = (SQLiteStore(database_path), SQLiteStore(database_path))
    barrier = Barrier(2)
    now = datetime(2026, 7, 19, 9, tzinfo=UTC)

    def claim(store: SQLiteStore) -> str:
        barrier.wait(timeout=5)
        try:
            store.claim_public_live_admission(
                session_hash="hmac-sha256:" + "5" * 64,
                ip_hash="hmac-sha256:" + "6" * 64,
                now=now,
                cooldown=timedelta(minutes=1),
                daily_budget=10,
                session_expires_at=now + timedelta(days=1),
            )
        except PublicLiveAdmissionError as error:
            return error.code
        return "admitted"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim, stores))

    assert sorted(outcomes) == ["admitted", "live_cooldown"]


def test_live_concurrency_gate_fails_fast_and_releases_after_exit() -> None:
    gate = LiveConcurrencyGate(max_concurrent=1)

    async def exercise() -> None:
        async with gate.slot():
            assert gate.active == 1
            with pytest.raises(LiveConcurrencyLimitError):
                async with gate.slot():
                    raise AssertionError("capacity failure must occur before entry")
        assert gate.active == 0
        async with gate.slot():
            assert gate.active == 1

    asyncio.run(exercise())


def test_replay_bypasses_zero_live_budget(tmp_path) -> None:
    settings = _live_settings(live_daily_budget=0)
    store = SQLiteStore(tmp_path / "replay-bypass.sqlite3")

    with TestClient(create_app(settings, store=store)) as client:
        response = client.post(
            "/api/recoveries",
            json={"scenarioId": "hotel", "executionMode": "replay_fixture"},
        )

    assert response.status_code == 201
    assert response.json()["executionMode"] == "replay_fixture"
    assert store.count_public_live_usage_rows() == 0
