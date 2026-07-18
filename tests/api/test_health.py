import json

from fastapi.testclient import TestClient

from server.config import RuntimeSettings
from server.main import create_app


def test_health_without_key_reports_truthful_stub_boundary(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = TestClient(create_app())

    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["backend"] == "stub"
    assert body["liveReady"] is False
    assert body["providerBoundary"] == "demo_adapter_only"
    assert "key" not in json.dumps(body).lower()


def test_demo_reset_requires_explicit_true_environment_value(monkeypatch) -> None:
    monkeypatch.delenv("BACKCHANNEL_DEMO_RESET_ENABLED", raising=False)
    assert RuntimeSettings.from_environment().demo_reset_enabled is False

    monkeypatch.setenv("BACKCHANNEL_DEMO_RESET_ENABLED", "true")
    assert RuntimeSettings.from_environment().demo_reset_enabled is True

    monkeypatch.setenv("BACKCHANNEL_DEMO_RESET_ENABLED", "1")
    assert RuntimeSettings.from_environment().demo_reset_enabled is False
