from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.config import RuntimeSettings
from server.main import create_app
from server.store import SQLiteStore


def _write_bundle(root: Path, *, include_script: bool = True) -> None:
    root.mkdir(parents=True)
    (root / "index.html").write_text(
        '<!doctype html><div id="root"></div><script src="/assets/app.js"></script>',
        encoding="utf-8",
    )
    if include_script:
        assets = root / "assets"
        assets.mkdir()
        (assets / "app.js").write_text("export {};", encoding="utf-8")


def test_readyz_requires_database_and_configured_complete_bundle(tmp_path: Path) -> None:
    frontend_root = tmp_path / "dist"
    _write_bundle(frontend_root)
    store = SQLiteStore(tmp_path / "ready.sqlite3")
    settings = RuntimeSettings(
        live_ready=False,
        frontend_dist_path=frontend_root,
    )

    recoveries_before = store.count_recoveries()
    with TestClient(create_app(settings, store=store)) as client:
        response = client.get(
            "/readyz",
            headers={"Origin": "http://localhost:5173"},
        )

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers
    vary_tokens = {
        token.strip().lower()
        for token in response.headers.get("vary", "").split(",")
        if token.strip()
    }
    assert "origin" in vary_tokens
    assert "cookie" not in vary_tokens
    assert store.count_recoveries() == recoveries_before


@pytest.mark.parametrize("bundle_state", ["missing_index", "missing_asset"])
def test_readyz_fails_generically_for_an_incomplete_configured_bundle(
    tmp_path: Path,
    bundle_state: str,
) -> None:
    frontend_root = tmp_path / "dist"
    if bundle_state == "missing_index":
        frontend_root.mkdir()
    else:
        _write_bundle(frontend_root, include_script=False)
    store = SQLiteStore(tmp_path / f"{bundle_state}.sqlite3")
    settings = RuntimeSettings(
        live_ready=False,
        frontend_dist_path=frontend_root,
    )

    with TestClient(create_app(settings, store=store)) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "internal_error"
    assert str(frontend_root) not in response.text
    assert "bundle" not in response.text.lower()
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize("bundle_state", ["empty_asset", "unserved_root_asset"])
def test_readyz_rejects_assets_the_static_route_cannot_use(
    tmp_path: Path,
    bundle_state: str,
) -> None:
    frontend_root = tmp_path / "dist"
    _write_bundle(frontend_root)
    if bundle_state == "empty_asset":
        (frontend_root / "assets" / "app.js").write_bytes(b"")
    else:
        (frontend_root / "index.html").write_text(
            '<!doctype html><div id="root"></div><script src="/runtime.js"></script>',
            encoding="utf-8",
        )
        (frontend_root / "runtime.js").write_text("export {};", encoding="utf-8")
    store = SQLiteStore(tmp_path / f"{bundle_state}.sqlite3")
    settings = RuntimeSettings(
        live_ready=False,
        frontend_dist_path=frontend_root,
    )

    with TestClient(create_app(settings, store=store)) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "internal_error"
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize(
    "reference",
    [
        "/assets%2Fapp.js",
        "/assets%5Capp.js",
        "/assets/app%00.js",
        "/assets%252Fapp.js",
    ],
)
def test_readyz_rejects_ambiguous_encoded_asset_references(
    tmp_path: Path,
    reference: str,
) -> None:
    frontend_root = tmp_path / "dist"
    _write_bundle(frontend_root)
    (frontend_root / "index.html").write_text(
        f'<!doctype html><div id="root"></div><script src="{reference}"></script>',
        encoding="utf-8",
    )
    store = SQLiteStore(tmp_path / "ambiguous-reference.sqlite3")
    settings = RuntimeSettings(
        live_ready=False,
        frontend_dist_path=frontend_root,
    )

    with TestClient(create_app(settings, store=store)) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "internal_error"


def test_nonseparator_percent_encoded_asset_is_ready_and_public(tmp_path: Path) -> None:
    frontend_root = tmp_path / "dist"
    _write_bundle(frontend_root)
    encoded_reference = "/assets/app%2Dencoded.js"
    (frontend_root / "index.html").write_text(
        f'<!doctype html><div id="root"></div><script src="{encoded_reference}"></script>',
        encoding="utf-8",
    )
    (frontend_root / "assets" / "app-encoded.js").write_text(
        "export const encoded = true;",
        encoding="utf-8",
    )
    store = SQLiteStore(tmp_path / "encoded-reference.sqlite3")
    settings = RuntimeSettings(
        live_ready=False,
        frontend_dist_path=frontend_root,
    )

    with TestClient(create_app(settings, store=store)) as client:
        ready = client.get("/readyz")
        asset = client.get(encoded_reference)

    assert ready.status_code == 200
    assert asset.status_code == 200
    assert asset.text == "export const encoded = true;"


def test_readyz_fails_generically_when_database_is_not_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frontend_root = tmp_path / "dist"
    _write_bundle(frontend_root)
    store = SQLiteStore(tmp_path / "database-not-ready.sqlite3")
    monkeypatch.setattr(store, "is_ready", lambda: False)
    settings = RuntimeSettings(
        live_ready=False,
        frontend_dist_path=frontend_root,
    )

    with TestClient(create_app(settings, store=store)) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "internal_error"
    assert "set-cookie" not in response.headers


def test_readyz_transition_is_never_stored_when_database_readiness_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteStore(tmp_path / "readiness-transition.sqlite3")
    readiness = {"value": True}
    monkeypatch.setattr(store, "is_ready", lambda: readiness["value"])

    with TestClient(
        create_app(RuntimeSettings(live_ready=False), store=store)
    ) as client:
        ready = client.get("/readyz")
        readiness["value"] = False
        unavailable = client.get("/readyz")

    assert ready.status_code == 200
    assert ready.headers["cache-control"] == "no-store"
    assert unavailable.status_code == 503
    assert unavailable.headers["cache-control"] == "no-store"


def test_api_only_readiness_uses_database_when_bundle_is_not_configured(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "api-only-ready.sqlite3")

    with TestClient(
        create_app(RuntimeSettings(live_ready=False), store=store)
    ) as client:
        response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    assert "set-cookie" not in response.headers


def test_deployed_readiness_fails_closed_when_bundle_is_not_configured(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "deployed-without-bundle.sqlite3")
    settings = RuntimeSettings(
        live_ready=False,
        deployed=True,
        cors_origins=("https://demo.example",),
        identity_hmac_secret="test-identity-secret-that-is-at-least-32-bytes",
    )

    with TestClient(create_app(settings, store=store)) as client:
        response = client.get(
            "/readyz",
            headers={"Origin": "https://demo.example"},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "internal_error"
    assert "set-cookie" not in response.headers


def test_frontend_bundle_path_is_loaded_from_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frontend_root = tmp_path / "configured-dist"
    monkeypatch.setenv("BACKCHANNEL_FRONTEND_DIST_PATH", str(frontend_root))

    settings = RuntimeSettings.from_environment()

    assert settings.frontend_dist_path == frontend_root
