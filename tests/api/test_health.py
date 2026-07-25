import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.config import RuntimeSettings
from server.main import create_app
from server.store import SQLiteStore


def test_health_without_key_reports_truthful_stub_boundary(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = TestClient(create_app())

    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["backend"] == "stub"
    assert body["liveReady"] is False
    assert body["sdkStubReady"] is True
    assert body["providerBoundary"] == "demo_adapter_only"
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers
    assert response.headers.get("vary", "").lower() != "cookie"
    assert "key" not in json.dumps(body).lower()


def test_deployed_health_disables_sdk_stub_and_route_rejects_it(tmp_path) -> None:
    settings = RuntimeSettings(
        live_ready=False,
        deployed=True,
        cors_origins=("https://demo.example",),
        identity_hmac_secret="test-identity-secret-that-is-at-least-32-bytes",
    )
    store = SQLiteStore(tmp_path / "deployed-sdk-stub.sqlite3")

    with TestClient(create_app(settings, store=store)) as client:
        health = client.get("/health")
        rejected = client.post(
            "/api/recoveries",
            json={"scenarioId": "hotel", "executionMode": "sdk_stub"},
            headers={"Origin": "https://demo.example"},
        )

    assert health.status_code == 200
    assert health.json()["sdkStubReady"] is False
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "invalid_request"
    assert store.count_recoveries() == 0


def test_demo_reset_requires_explicit_true_environment_value(monkeypatch) -> None:
    monkeypatch.delenv("BACKCHANNEL_DEMO_RESET_ENABLED", raising=False)
    assert RuntimeSettings.from_environment().demo_reset_enabled is False

    monkeypatch.setenv("BACKCHANNEL_DEMO_RESET_ENABLED", "true")
    assert RuntimeSettings.from_environment().demo_reset_enabled is True

    monkeypatch.setenv("BACKCHANNEL_DEMO_RESET_ENABLED", "1")
    assert RuntimeSettings.from_environment().demo_reset_enabled is False


def test_live_environment_requires_a_stable_unexposed_identity_secret(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder-only")
    monkeypatch.delenv("BACKCHANNEL_IDENTITY_HMAC_SECRET", raising=False)

    with pytest.raises(ValueError, match="explicit identity HMAC secret"):
        RuntimeSettings.from_environment()

    secret = "test-stable-identity-secret-that-is-at-least-32-bytes"
    monkeypatch.setenv("BACKCHANNEL_IDENTITY_HMAC_SECRET", secret)
    settings = RuntimeSettings.from_environment()
    assert settings.live_ready is True
    assert secret not in repr(settings)


def test_public_server_example_disables_unsafe_multiworker_proxy_and_access_logs() -> None:
    example = Path(".env.example").read_text(encoding="utf-8")

    assert "--workers 1" in example
    assert "--no-proxy-headers" in example
    assert "--no-access-log" in example
