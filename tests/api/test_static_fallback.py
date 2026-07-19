from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from server.config import RuntimeSettings
from server.main import create_app
from server.store import SQLiteStore

HTML_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'none'"
)
API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"


@pytest.fixture
def static_dist(tmp_path: Path) -> Path:
    static_dir = tmp_path / "dist"
    assets = static_dir / "assets"
    assets.mkdir(parents=True)
    (static_dir / "index.html").write_text(
        "<!doctype html><html><head><title>Backchannel production</title></head>"
        '<body><div id="root"></div><script type="module" '
        'src="/assets/index-deadbeef.js"></script></body></html>',
        encoding="utf-8",
    )
    (assets / "index-deadbeef.js").write_text(
        'document.querySelector("#root").textContent = "ready";',
        encoding="utf-8",
    )
    (assets / "index-feedface.css").write_text(
        ":root { color-scheme: light; }",
        encoding="utf-8",
    )
    return static_dir


@pytest.fixture
def production_client(tmp_path: Path, static_dist: Path) -> TestClient:
    store = SQLiteStore(tmp_path / "production.sqlite3")
    with TestClient(
        create_app(
            RuntimeSettings(live_ready=False),
            store=store,
            static_dir=static_dist,
        )
    ) as client:
        yield client
    store.close()


def test_static_app_serves_index_real_assets_and_route_like_spa_paths(
    production_client: TestClient,
) -> None:
    for path in ("/", "/recoveries/current", "/scenario/hotel"):
        response = production_client.get(path)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert response.headers["content-security-policy"] == HTML_CSP
        assert "Backchannel production" in response.text

    javascript = production_client.get("/assets/index-deadbeef.js")
    assert javascript.status_code == 200
    assert javascript.text == 'document.querySelector("#root").textContent = "ready";'
    assert "javascript" in javascript.headers["content-type"]
    assert javascript.headers["content-security-policy"] == API_CSP

    stylesheet = production_client.get("/assets/index-feedface.css")
    assert stylesheet.status_code == 200
    assert stylesheet.text == ":root { color-scheme: light; }"
    assert stylesheet.headers["content-type"].startswith("text/css")


def test_api_health_readiness_and_sse_routes_keep_precedence_over_spa(
    production_client: TestClient,
) -> None:
    health = production_client.get("/health")
    assert health.status_code == 200
    assert health.headers["content-type"].startswith("application/json")
    assert health.headers["content-security-policy"] == API_CSP
    assert health.json()["providerBoundary"] == "demo_adapter_only"

    readiness = production_client.get("/readyz")
    assert readiness.status_code == 200
    assert readiness.json() == {"status": "ready"}
    assert readiness.headers["content-security-policy"] == API_CSP

    created = production_client.post(
        "/api/recoveries",
        json={"scenarioId": "api-quota", "executionMode": "replay_fixture"},
    )
    assert created.status_code == 201
    recovery_id = str(UUID(created.json()["recoveryId"]))
    stream = production_client.get(
        f"/api/recoveries/{recovery_id}/events",
        headers={"Last-Event-ID": "2"},
    )
    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert stream.headers["content-security-policy"] == API_CSP
    assert all(
        int(line.removeprefix("id: ")) > 2
        for line in stream.text.splitlines()
        if line.startswith("id: ")
    )


@pytest.mark.parametrize(
    "path",
    [
        "/api/not-a-route",
        "/health/not-a-route",
        "/readyz/not-a-route",
        "/assets/missing.js",
        "/favicon.ico",
        "/manifest.json",
        "/release/v0.3",
        "/%2e%2e/private",
        "/safe/%2e%2e/private",
    ],
)
def test_missing_asset_file_like_traversal_and_reserved_paths_never_spa_fallback(
    production_client: TestClient,
    path: str,
) -> None:
    response = production_client.get(path)

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["content-security-policy"] == API_CSP
    assert "Backchannel production" not in response.text


def test_create_app_remains_api_only_unless_static_directory_is_configured(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "api-only.sqlite3")
    with TestClient(
        create_app(RuntimeSettings(live_ready=False), store=store)
    ) as client:
        response = client.get("/")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["content-security-policy"] == API_CSP
    store.close()
