from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.config import RuntimeSettings
from server.main import create_app
from server.store import SQLiteStore


def _write_frontend_bundle(root: Path) -> None:
    assets = root / "assets"
    assets.mkdir(parents=True)
    (root / "index.html").write_text(
        "<!doctype html><html><head>"
        '<link rel="stylesheet" href="/assets/app.css">'
        "</head><body><div id=\"root\">frontend-shell</div>"
        '<script type="module" src="/assets/app.js"></script>'
        "</body></html>",
        encoding="utf-8",
    )
    (assets / "app.css").write_text("body { color: #123456; }", encoding="utf-8")
    (assets / "app.js").write_text("globalThis.BACKCHANNEL_BUNDLE = true;", encoding="utf-8")


@pytest.fixture
def configured_client(tmp_path: Path) -> TestClient:
    frontend_root = tmp_path / "dist"
    _write_frontend_bundle(frontend_root)
    settings = RuntimeSettings(
        live_ready=False,
        frontend_dist_path=frontend_root,
    )
    store = SQLiteStore(tmp_path / "static.sqlite3")
    with TestClient(create_app(settings, store=store)) as client:
        yield client


def test_one_process_serves_frontend_assets_and_safe_spa_routes(
    configured_client: TestClient,
) -> None:
    recoveries_before = configured_client.app.state.recovery_store.count_recoveries()
    root = configured_client.get("/")
    client_route = configured_client.get("/recoveries/recent")
    asset = configured_client.get("/assets/app.js")
    head = configured_client.head("/recoveries/recent")

    assert root.status_code == 200
    assert root.headers["content-type"].startswith("text/html")
    assert root.headers["cache-control"] == "no-cache"
    assert "frontend-shell" in root.text
    assert client_route.status_code == 200
    assert client_route.text == root.text
    assert asset.status_code == 200
    assert asset.headers["content-type"].startswith("text/javascript")
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert asset.text == "globalThis.BACKCHANNEL_BUNDLE = true;"
    assert head.status_code == 200
    assert head.content == b""
    assert "set-cookie" not in root.headers
    assert configured_client.app.state.recovery_store.count_recoveries() == recoveries_before


def test_spa_fallback_is_limited_to_get_head_and_html_capable_clients(
    configured_client: TestClient,
) -> None:
    json_get = configured_client.get(
        "/recoveries/recent",
        headers={"Accept": "application/json"},
    )
    post = configured_client.post("/recoveries/recent", json={})

    assert json_get.status_code == 404
    assert json_get.headers["content-type"].startswith("application/json")
    assert post.status_code == 405
    assert post.headers["content-type"].startswith("application/json")
    assert "frontend-shell" not in json_get.text
    assert "frontend-shell" not in post.text


@pytest.mark.parametrize(
    "accept",
    [
        "text/html;q=0, application/json",
        "text/html;q=0, */*;q=1",
        "*/*;q=0",
        "text/html;q",
        "text/html;q=invalid",
        "text/html;q=1.1",
        "text/html;q=-0.1",
    ],
)
def test_spa_fallback_rejects_nonpositive_or_invalid_html_quality(
    configured_client: TestClient,
    accept: str,
) -> None:
    response = configured_client.get(
        "/recoveries/recent",
        headers={"Accept": accept},
    )

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert "frontend-shell" not in response.text


@pytest.mark.parametrize("accept", ["text/html;q=0.5", "application/json, */*;q=0.2"])
def test_spa_fallback_accepts_positive_html_quality(
    configured_client: TestClient,
    accept: str,
) -> None:
    response = configured_client.get(
        "/recoveries/recent",
        headers={"Accept": accept},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_static_fallback_does_not_shadow_api_or_event_streams(
    configured_client: TestClient,
) -> None:
    health = configured_client.get("/health")
    ready = configured_client.get("/readyz")
    scenarios = configured_client.get("/api/scenarios")
    created = configured_client.post(
        "/api/recoveries",
        json={"scenarioId": "api-quota", "executionMode": "replay_fixture"},
    )
    events = configured_client.get(
        f"/api/recoveries/{created.json()['recoveryId']}/events"
    )
    missing_api = configured_client.get("/api/does-not-exist")

    assert health.status_code == 200
    assert health.headers["content-type"].startswith("application/json")
    assert ready.status_code == 200
    assert ready.headers["content-type"].startswith("application/json")
    assert scenarios.status_code == 200
    assert scenarios.headers["content-type"].startswith("application/json")
    assert created.status_code == 201
    assert events.status_code == 200
    assert events.headers["content-type"].startswith("text/event-stream")
    assert '"type":"recovery.completed"' in events.text
    assert missing_api.status_code == 404
    assert missing_api.headers["content-type"].startswith("application/json")
    assert missing_api.json()["error"]["code"] == "not_found"
    assert "frontend-shell" not in missing_api.text


@pytest.mark.parametrize(
    "path",
    [
        "/assets/missing.js",
        "/missing.css",
        "/favicon.ico",
        "/.env",
        "/assets/%2e%2e/index.html",
        "/assets%2fapp.js",
        "/assets%5capp.js",
        "/bad%00path",
        "/literal%252fseparator",
        "/health/details",
        "/readyz/details",
    ],
)
def test_missing_dotted_and_ambiguous_paths_never_receive_spa_html(
    configured_client: TestClient,
    path: str,
) -> None:
    response = configured_client.get(path)

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "not_found"
    assert "frontend-shell" not in response.text


def test_even_existing_hidden_files_and_outside_symlinks_are_not_public(
    tmp_path: Path,
) -> None:
    frontend_root = tmp_path / "dist"
    _write_frontend_bundle(frontend_root)
    (frontend_root / ".env").write_text("DO_NOT_SERVE=true", encoding="utf-8")
    outside = tmp_path / "outside.js"
    outside.write_text("DO_NOT_SERVE", encoding="utf-8")
    (frontend_root / "assets" / "outside.js").symlink_to(outside)
    settings = RuntimeSettings(
        live_ready=False,
        frontend_dist_path=frontend_root,
    )
    store = SQLiteStore(tmp_path / "static-secrets.sqlite3")

    with TestClient(create_app(settings, store=store)) as client:
        hidden = client.get("/.env")
        escaped = client.get("/assets/outside.js")

    assert hidden.status_code == 404
    assert escaped.status_code == 404
    assert "DO_NOT_SERVE" not in hidden.text
    assert "DO_NOT_SERVE" not in escaped.text


def test_api_only_development_remains_available_without_a_frontend_bundle(
    tmp_path: Path,
) -> None:
    settings = RuntimeSettings(live_ready=False)
    store = SQLiteStore(tmp_path / "api-only.sqlite3")

    with TestClient(create_app(settings, store=store)) as client:
        root = client.get("/")
        scenarios = client.get("/api/scenarios")

    assert root.status_code == 404
    assert root.headers["content-type"].startswith("application/json")
    assert scenarios.status_code == 200
