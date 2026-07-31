from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import UUID, uuid4

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
_STATIC_MANIFEST_NAME = ".backchannel-static-manifest.json"


def _write_static_manifest(static_dir: Path) -> None:
    artifact_paths = [
        static_dir / "index.html",
        *sorted(path for path in (static_dir / "assets").rglob("*") if path.is_file()),
    ]
    files = []
    for path in sorted(artifact_paths):
        content = path.read_bytes()
        files.append(
            {
                "path": path.relative_to(static_dir).as_posix(),
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    (static_dir / _STATIC_MANIFEST_NAME).write_text(
        json.dumps({"version": 1, "files": files}, indent=2) + "\n",
        encoding="utf-8",
    )


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
    _write_static_manifest(static_dir)
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
        json={
            "scenarioId": "api-quota",
            "executionMode": "replay_fixture",
            "clientRequestId": uuid4().hex,
        },
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
        f"/{_STATIC_MANIFEST_NAME}",
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


def test_external_index_symlink_is_never_ready_or_served(
    tmp_path: Path,
    static_dist: Path,
) -> None:
    outside_index = tmp_path / "outside-index.html"
    outside_index.write_text(
        "<!doctype html><title>outside-static-root-canary</title>",
        encoding="utf-8",
    )
    index_path = static_dist / "index.html"
    index_path.unlink()
    index_path.symlink_to(outside_index)
    store = SQLiteStore(tmp_path / "index-symlink.sqlite3")

    with TestClient(
        create_app(
            RuntimeSettings(live_ready=False),
            store=store,
            static_dir=static_dist,
        )
    ) as client:
        readiness = client.get("/readyz")
        root = client.get("/")

    assert readiness.status_code == 503
    assert readiness.json() == {"status": "not_ready"}
    assert root.status_code == 404
    assert "outside-static-root-canary" not in root.text
    store.close()


def test_static_root_symlink_is_never_ready_or_served(
    tmp_path: Path,
    static_dist: Path,
) -> None:
    static_link = tmp_path / "linked-dist"
    static_link.symlink_to(static_dist, target_is_directory=True)
    store = SQLiteStore(tmp_path / "root-symlink.sqlite3")

    with TestClient(
        create_app(
            RuntimeSettings(live_ready=False),
            store=store,
            static_dir=static_link,
        )
    ) as client:
        readiness = client.get("/readyz")
        root = client.get("/")
        asset = client.get("/assets/index-deadbeef.js")

    assert readiness.status_code == 503
    assert readiness.json() == {"status": "not_ready"}
    assert root.status_code == 404
    assert asset.status_code == 404
    store.close()
