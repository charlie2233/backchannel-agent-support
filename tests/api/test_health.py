import json

from fastapi.testclient import TestClient

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
