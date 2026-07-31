from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.types import Message, Receive, Scope, Send

from server.config import RuntimeSettings
from server.controls import ClientIdentity, PublicBoundaryMiddleware
from server.main import create_app
from server.store import SQLiteStore

_DEPLOYMENT_SECRET = "deployment-identity-secret-that-is-long-enough"


def _deployed_settings(
    *,
    allowed_hosts: tuple[str, ...] = ("demo.example",),
) -> RuntimeSettings:
    return RuntimeSettings(
        live_ready=False,
        deployed_mode=True,
        deployed_cors_origins=("https://demo.example",),
        deployed_allowed_hosts=allowed_hosts,
        identity_hash_secret=_DEPLOYMENT_SECRET,
    )


def test_deployed_mode_requires_an_explicit_host_allowlist() -> None:
    with pytest.raises(ValueError, match="allowed host"):
        RuntimeSettings(
            live_ready=False,
            deployed_mode=True,
            deployed_cors_origins=("https://demo.example",),
            identity_hash_secret=_DEPLOYMENT_SECRET,
        )


def test_environment_loads_the_deployed_host_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BACKCHANNEL_DEPLOYED_MODE", "true")
    monkeypatch.setenv("BACKCHANNEL_CORS_ORIGINS", "https://demo.example")
    monkeypatch.setenv("BACKCHANNEL_ALLOWED_HOSTS", "demo.example,api.example")
    monkeypatch.setenv("BACKCHANNEL_IDENTITY_HASH_SECRET", _DEPLOYMENT_SECRET)

    settings = RuntimeSettings.from_environment()

    assert settings.allowed_hosts == ("demo.example", "api.example")


@pytest.mark.parametrize(
    "allowed_hosts",
    [
        ("*",),
        ("*.example",),
        ("https://demo.example",),
        ("demo.example:443",),
        ("demo.example/path",),
        ("[::1]",),
        ("demo..example",),
        ("-demo.example",),
        ("café.example",),
        ("127.0.0.01",),
        ("demo.example", "DEMO.EXAMPLE"),
    ],
)
def test_host_allowlist_rejects_noncanonical_or_duplicate_names(
    allowed_hosts: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError, match="allowed host"):
        _deployed_settings(allowed_hosts=allowed_hosts)


def test_untrusted_host_is_rejected_before_cookie_or_creation(tmp_path) -> None:
    database_path = tmp_path / "untrusted-host.sqlite3"
    app = create_app(
        _deployed_settings(),
        store=SQLiteStore(database_path),
    )

    with TestClient(app, base_url="https://demo.example") as client:
        response = client.post(
            "/api/recoveries",
            headers={
                "Host": "evil.example",
                "Origin": "https://evil.example",
            },
            json={
                "scenarioId": "api-quota",
                "executionMode": "replay_fixture",
                "clientRequestId": uuid4().hex,
            },
        )

    assert response.status_code == 400
    assert response.json() == {
        "code": "invalid_host",
        "message": "The request host is not allowed.",
        "requestId": response.headers["x-request-id"],
    }
    assert "set-cookie" not in response.headers
    assert "access-control-allow-origin" not in response.headers
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["strict-transport-security"].startswith("max-age=")

    with sqlite3.connect(database_path) as connection:
        for table in (
            "recoveries",
            "recovery_creations",
            "usage_ledger",
            "live_admissions",
        ):
            count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert count == 0


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": ""},
        {"Host": " demo.example"},
        {"Host": "demo.example "},
        {"Host": "bad host"},
        {"Host": "demo.example/path"},
        {"Host": "demo.example\\path"},
        {"Host": "demo.example@evil.example"},
        {"Host": "demo.example."},
        {"Host": "demo.example:0"},
        {"Host": "demo.example:65536"},
        {"Host": f"demo.example:{'9' * 10_000}"},
        {"Host": "demo.example:not-a-port"},
        {"Host": "::1"},
        {"Host": "[::1"},
        {"Host": "[::1]suffix"},
        {"Host": "[fe80::1%eth0]"},
        {"Host": "127.0.0.01"},
        [("Host", "demo.example"), ("Host", "evil.example")],
    ],
)
def test_invalid_host_framing_is_rejected_without_a_cookie(
    tmp_path,
    headers: dict[str, str] | list[tuple[str, str]],
) -> None:
    app = create_app(
        _deployed_settings(),
        store=SQLiteStore(tmp_path / f"invalid-host-{uuid4().hex}.sqlite3"),
    )

    with TestClient(app, base_url="https://demo.example") as client:
        response = client.get("/health", headers=headers)

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_host"
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize(
    "raw_headers",
    [
        [],
        [(b"host", b"demo.example"), (b"host", b"evil.example")],
        [(b"host", b"evil.example"), (b"host", b"demo.example")],
        [(b"host", b"\xff.example")],
        [(b"host", b"demo.example,evil.example")],
    ],
)
def test_invalid_raw_host_is_rejected_before_identity_body_or_downstream(
    raw_headers: list[tuple[bytes, bytes]],
) -> None:
    calls = {"identity": 0, "receive": 0, "downstream": 0}
    sent: list[Message] = []

    class SentinelControls:
        def resolve_client_identity(self, request: Request) -> ClientIdentity:
            calls["identity"] += 1
            raise AssertionError("identity resolution must not run")

        def session_cookie_header(self, value: str) -> str:
            raise AssertionError("cookie issuance must not run")

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        calls["downstream"] += 1
        raise AssertionError("downstream routing must not run")

    async def receive() -> Message:
        calls["receive"] += 1
        raise AssertionError("the request body must not be read")

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "server": ("127.0.0.1", 8000),
        "client": ("127.0.0.1", 5000),
        "scheme": "http",
        "method": "POST",
        "root_path": "",
        "path": "/api/recoveries",
        "raw_path": b"/api/recoveries",
        "query_string": b"",
        "headers": raw_headers,
    }
    middleware = PublicBoundaryMiddleware(
        downstream,
        controls=SentinelControls(),  # type: ignore[arg-type]
        settings=RuntimeSettings(live_ready=False),
    )

    asyncio.run(middleware(scope, receive, send))

    assert calls == {"identity": 0, "receive": 0, "downstream": 0}
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 400
    assert all(name.lower() != b"set-cookie" for name, _ in sent[0]["headers"])
    assert sent[1]["type"] == "http.response.body"
    assert b'"code":"invalid_host"' in sent[1]["body"]


@pytest.mark.parametrize(
    "host",
    [
        "demo.example",
        "DEMO.EXAMPLE",
        "demo.example:443",
        "DEMO.EXAMPLE:8443",
    ],
)
def test_allowed_dns_host_accepts_case_and_a_valid_port(tmp_path, host: str) -> None:
    app = create_app(
        _deployed_settings(),
        store=SQLiteStore(tmp_path / f"allowed-host-{uuid4().hex}.sqlite3"),
    )

    with TestClient(app, base_url="https://demo.example") as client:
        response = client.get("/health", headers={"Host": host})

    assert response.status_code == 200


def test_bracketed_ipv6_host_is_normalized_deliberately(tmp_path) -> None:
    app = create_app(
        _deployed_settings(allowed_hosts=("::1",)),
        store=SQLiteStore(tmp_path / "ipv6-host.sqlite3"),
    )

    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/health", headers={"Host": "[0:0:0:0:0:0:0:1]:8000"})

    assert response.status_code == 200


@pytest.mark.parametrize(
    "host",
    [
        "testserver",
        "localhost:8000",
        "127.0.0.1:8000",
        "[::1]:8000",
    ],
)
def test_development_mode_allows_only_local_and_test_hosts(tmp_path, host: str) -> None:
    app = create_app(
        RuntimeSettings(live_ready=False),
        store=SQLiteStore(tmp_path / f"development-host-{uuid4().hex}.sqlite3"),
    )

    with TestClient(app) as client:
        response = client.get("/health", headers={"Host": host})

    assert response.status_code == 200


def test_development_mode_rejects_a_rebinding_host(tmp_path) -> None:
    app = create_app(
        RuntimeSettings(live_ready=False),
        store=SQLiteStore(tmp_path / "development-rebinding.sqlite3"),
    )

    with TestClient(app) as client:
        response = client.get("/health", headers={"Host": "attacker.example"})

    assert response.status_code == 400
    assert "set-cookie" not in response.headers


def test_env_example_declares_the_required_deployed_host_allowlist() -> None:
    example = (
        Path(__file__).resolve().parents[2] / ".env.example"
    ).read_text(encoding="utf-8")

    assert "BACKCHANNEL_ALLOWED_HOSTS=\n" in example
    assert "Include 127.0.0.1 when using the packaged image healthcheck." in example
