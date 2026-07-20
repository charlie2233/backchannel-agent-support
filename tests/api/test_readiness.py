from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scripts.start import bind_host
from server.config import RuntimeSettings
from server.main import create_app
from server.store import SQLiteStore


def _static_dir(tmp_path: Path) -> Path:
    static_dir = tmp_path / "dist"
    static_dir.mkdir()
    (static_dir / "index.html").write_text(
        "<!doctype html><title>Backchannel</title>",
        encoding="utf-8",
    )
    return static_dir


def test_readiness_verifies_sqlite_schema_and_configured_static_index(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "ready.sqlite3"
    store = SQLiteStore(database_path)
    static_dir = _static_dir(tmp_path)
    with TestClient(
        create_app(
            RuntimeSettings(live_ready=False),
            store=store,
            static_dir=static_dir,
        )
    ) as client:
        response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    store.close()


def test_readiness_is_generic_503_when_configured_static_index_is_missing(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "missing-static.sqlite3")
    missing_static_dir = tmp_path / "not-built"
    with TestClient(
        create_app(
            RuntimeSettings(live_ready=False),
            store=store,
            static_dir=missing_static_dir,
        )
    ) as client:
        readiness = client.get("/readyz")
        health = client.get("/health")

    assert readiness.status_code == 503
    assert readiness.json() == {"status": "not_ready"}
    assert str(missing_static_dir) not in readiness.text
    assert "index.html" not in readiness.text
    assert health.status_code == 200
    assert health.json()["backend"] == "stub"
    store.close()


@pytest.mark.parametrize(
    "failure",
    [
        "closed",
        "missing_schema",
        "malformed_schema",
        "missing_access",
        "malformed_access",
        "access_foreign_key_violation",
    ],
)
def test_readiness_is_generic_503_for_sqlite_or_schema_failure(
    tmp_path: Path,
    failure: str,
) -> None:
    database_path = tmp_path / f"{failure}.sqlite3"
    store = SQLiteStore(database_path)
    static_dir = _static_dir(tmp_path)
    with TestClient(
        create_app(
            RuntimeSettings(live_ready=False),
            store=store,
            static_dir=static_dir,
        )
    ) as client:
        if failure == "closed":
            store.close()
        else:
            with sqlite3.connect(database_path) as connection:
                if failure == "missing_schema":
                    connection.execute("DROP TABLE receipts")
                elif failure == "malformed_schema":
                    connection.execute(
                        "ALTER TABLE receipts "
                        "RENAME COLUMN receipt_json TO broken_receipt"
                    )
                elif failure == "missing_access":
                    connection.execute("DROP TABLE recovery_access")
                elif failure == "malformed_access":
                    connection.execute(
                        "ALTER TABLE recovery_access "
                        "RENAME COLUMN session_key TO broken_session_key"
                    )
                else:
                    connection.execute(
                        "INSERT INTO recovery_access (recovery_id, session_key) "
                        "VALUES ('missing-recovery', ?)",
                        ("f" * 64,),
                    )
        response = client.get("/readyz")
        health = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}
    public_body = json.dumps(response.json()).lower()
    assert str(database_path).lower() not in public_body
    assert "sqlite" not in public_body
    assert "receipt" not in public_body
    assert "recovery_access" not in public_body
    assert "session" not in public_body
    assert health.status_code == 200


def test_api_only_test_app_requires_database_but_not_a_static_index(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "api-only-ready.sqlite3")
    with TestClient(
        create_app(RuntimeSettings(live_ready=False), store=store)
    ) as client:
        response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    store.close()


def test_start_bind_is_loopback_locally_and_all_interfaces_only_when_deployed() -> None:
    assert bind_host({}) == "127.0.0.1"
    assert bind_host({"BACKCHANNEL_DEPLOYED_MODE": "false"}) == "127.0.0.1"
    assert bind_host({"BACKCHANNEL_DEPLOYED_MODE": "true"}) == "0.0.0.0"
