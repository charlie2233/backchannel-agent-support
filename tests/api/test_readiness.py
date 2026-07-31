from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock

import pytest
from fastapi.testclient import TestClient

from scripts import docker_restart_smoke, start
from scripts.start import bind_host
from scripts.start import main as start_main
from server.config import RuntimeSettings
from server.main import create_app
from server.store import SQLiteStore

_READINESS_CACHE_SECONDS = 5.0
_STATIC_MANIFEST_NAME = ".backchannel-static-manifest.json"


class _ManualClock:
    def __init__(self) -> None:
        self._value = 0.0

    def __call__(self) -> float:
        return self._value

    def advance_past_cache(self) -> None:
        self._value += _READINESS_CACHE_SECONDS + 0.01

    def advance(self, seconds: float) -> None:
        self._value += seconds


def _static_dir(tmp_path: Path) -> Path:
    static_dir = tmp_path / "dist"
    assets_dir = static_dir / "assets"
    assets_dir.mkdir(parents=True)
    (static_dir / "index.html").write_text(
        "<!doctype html><html><head><title>Backchannel</title>"
        '<script type="module" src="/assets/index-deadbeef.js"></script>'
        '<link rel="stylesheet" href="/assets/index-feedface.css">'
        "</head><body></body></html>",
        encoding="utf-8",
    )
    (assets_dir / "index-deadbeef.js").write_text(
        'document.body.dataset.runtime = "ready";',
        encoding="utf-8",
    )
    (assets_dir / "index-feedface.css").write_text(
        ":root { color-scheme: light; }",
        encoding="utf-8",
    )
    _write_static_manifest(static_dir)
    return static_dir


def _write_static_manifest(static_dir: Path) -> None:
    artifact_paths = [
        static_dir / "index.html",
        *sorted(
            path
            for path in (static_dir / "assets").rglob("*")
            if path.is_file() and not path.is_symlink()
        ),
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


def _readiness_row(database_path: Path) -> tuple[int, int]:
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT id, generation FROM readiness_probe ORDER BY id"
        ).fetchall()
    assert len(rows) == 1
    return int(rows[0][0]), int(rows[0][1])


def _application_table_counts(database_path: Path) -> dict[str, int]:
    with sqlite3.connect(database_path) as connection:
        table_names = [
            str(row[0])
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                  AND name NOT LIKE 'sqlite_%'
                  AND name <> 'readiness_probe'
                ORDER BY name
                """
            ).fetchall()
        ]
        return {
            table_name: int(
                connection.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]
            )
            for table_name in table_names
        }


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


def test_readiness_commits_one_singleton_toggle_per_cache_interval(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "cached-write-ready.sqlite3"
    store = SQLiteStore(database_path)
    clock = _ManualClock()
    counts_before = _application_table_counts(database_path)
    assert _readiness_row(database_path) == (1, 0)

    with TestClient(
        create_app(
            RuntimeSettings(live_ready=False),
            store=store,
            readiness_clock=clock,
        )
    ) as client:
        first = client.get("/readyz")
        first_generation = _readiness_row(database_path)
        cached = client.get("/readyz")
        cached_generation = _readiness_row(database_path)
        clock.advance_past_cache()
        refreshed = client.get("/readyz")

    assert first.status_code == 200
    assert cached.status_code == 200
    assert refreshed.status_code == 200
    assert first.headers["cache-control"] == "no-store"
    assert cached.headers["cache-control"] == "no-store"
    assert refreshed.headers["cache-control"] == "no-store"
    assert first_generation == (1, 1)
    assert cached_generation == first_generation
    assert _readiness_row(database_path) == (1, 0)
    assert _application_table_counts(database_path) == counts_before
    store.close()


def test_readiness_cache_starts_after_probe_and_refreshes_at_exact_boundary(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "cache-boundary.sqlite3"
    store = SQLiteStore(database_path)
    clock = _ManualClock()
    commit_calls = 0

    def advancing_commit(connection: sqlite3.Connection) -> None:
        nonlocal commit_calls
        commit_calls += 1
        connection.commit()
        clock.advance(10)

    with TestClient(
        create_app(
            RuntimeSettings(live_ready=False),
            store=store,
            readiness_clock=clock,
            readiness_commit=advancing_commit,
        )
    ) as client:
        first = client.get("/readyz")
        cached_after_long_probe = client.get("/readyz")
        clock.advance(_READINESS_CACHE_SECONDS)
        exact_boundary = client.get("/readyz")

    assert first.status_code == 200
    assert cached_after_long_probe.status_code == 200
    assert exact_boundary.status_code == 200
    assert commit_calls == 2
    assert _readiness_row(database_path) == (1, 0)
    store.close()


def test_readiness_rejects_query_only_connection_but_health_remains_live(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "query-only.sqlite3"
    store = SQLiteStore(database_path)

    def query_only_connection() -> sqlite3.Connection:
        connection = sqlite3.connect(database_path, timeout=0.35)
        connection.execute("PRAGMA query_only = ON")
        return connection

    with TestClient(
        create_app(
            RuntimeSettings(live_ready=False),
            store=store,
            readiness_connection_factory=query_only_connection,
        )
    ) as client:
        readiness = client.get("/readyz")
        health = client.get("/health")

    assert readiness.status_code == 503
    assert readiness.json() == {"status": "not_ready"}
    assert health.status_code == 200
    assert _readiness_row(database_path) == (1, 0)
    store.close()


def test_readiness_bounds_writer_contention_and_recovers_after_failure_cache(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "writer-contention.sqlite3"
    store = SQLiteStore(database_path)
    clock = _ManualClock()
    with TestClient(
        create_app(
            RuntimeSettings(live_ready=False),
            store=store,
            readiness_clock=clock,
        )
    ) as client:
        writer = sqlite3.connect(database_path, timeout=0)
        try:
            writer.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            blocked = client.get("/readyz")
            elapsed = time.monotonic() - started
            writer.rollback()

            cached_failure = client.get("/readyz")
            clock.advance_past_cache()
            recovered = client.get("/readyz")
        finally:
            writer.close()

    assert blocked.status_code == 503
    assert elapsed < 1.5
    assert cached_failure.status_code == 503
    assert recovered.status_code == 200
    assert _readiness_row(database_path) == (1, 1)
    store.close()


@pytest.mark.parametrize(
    "failure",
    [
        "missing_table",
        "missing_row",
        "extra_column",
        "wrong_primary_key",
        "extra_row",
        "invalid_generation",
        "fractional_generation",
        "generated_extra_column",
        "destructive_trigger",
        "case_variant_trigger",
    ],
)
def test_readiness_rejects_invalid_probe_schema_or_singleton(
    tmp_path: Path,
    failure: str,
) -> None:
    database_path = tmp_path / f"{failure}.sqlite3"
    store = SQLiteStore(database_path)
    with sqlite3.connect(database_path) as connection:
        if failure == "missing_table":
            connection.execute("DROP TABLE readiness_probe")
        elif failure == "missing_row":
            connection.execute("DELETE FROM readiness_probe")
        elif failure == "extra_column":
            connection.execute("DROP TABLE readiness_probe")
            connection.execute(
                """
                CREATE TABLE readiness_probe (
                    id INTEGER PRIMARY KEY,
                    generation INTEGER NOT NULL,
                    extra TEXT
                )
                """
            )
            connection.execute("INSERT INTO readiness_probe (id, generation) VALUES (1, 0)")
        elif failure == "wrong_primary_key":
            connection.execute("DROP TABLE readiness_probe")
            connection.execute(
                """
                CREATE TABLE readiness_probe (
                    id INTEGER NOT NULL,
                    generation INTEGER PRIMARY KEY
                )
                """
            )
            connection.execute("INSERT INTO readiness_probe (id, generation) VALUES (1, 0)")
        elif failure == "extra_row":
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute("INSERT INTO readiness_probe (id, generation) VALUES (2, 1)")
        elif failure == "invalid_generation":
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute("UPDATE readiness_probe SET generation = 2 WHERE id = 1")
        elif failure == "fractional_generation":
            connection.execute("DROP TABLE readiness_probe")
            connection.execute(
                """
                CREATE TABLE readiness_probe (
                    id INTEGER PRIMARY KEY,
                    generation INTEGER NOT NULL
                )
                """
            )
            connection.execute("INSERT INTO readiness_probe (id, generation) VALUES (1, 1.5)")
        elif failure == "generated_extra_column":
            connection.execute("DROP TABLE readiness_probe")
            connection.execute(
                """
                CREATE TABLE readiness_probe (
                    id INTEGER PRIMARY KEY,
                    generation INTEGER NOT NULL,
                    extra INTEGER GENERATED ALWAYS AS (generation) VIRTUAL
                )
                """
            )
            connection.execute("INSERT INTO readiness_probe (id, generation) VALUES (1, 0)")
        else:
            trigger_target = (
                "readiness_probe" if failure == "destructive_trigger" else "ReAdInEsS_PrObE"
            )
            connection.execute(
                """
                INSERT INTO usage_ledger (
                    recovery_id, category, amount, recorded_at
                )
                VALUES ('trigger-canary', 'live_attempt', 1, '2026-07-24T00:00:00+00:00')
                """
            )
            connection.execute(
                f"""
                CREATE TRIGGER {failure}
                AFTER UPDATE ON {trigger_target}
                BEGIN
                    DELETE FROM usage_ledger;
                END
                """
            )

    with TestClient(create_app(RuntimeSettings(live_ready=False), store=store)) as client:
        readiness = client.get("/readyz")
        health = client.get("/health")

    assert readiness.status_code == 503
    assert readiness.json() == {"status": "not_ready"}
    assert health.status_code == 200
    if failure in {"destructive_trigger", "case_variant_trigger"}:
        with sqlite3.connect(database_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM usage_ledger").fetchone()[0] == 1
    elif failure in {"missing_table", "missing_row"}:
        with sqlite3.connect(database_path) as connection:
            table_exists = (
                connection.execute(
                    """
                    SELECT 1
                    FROM sqlite_master
                    WHERE type = 'table' AND name = 'readiness_probe'
                    """
                ).fetchone()
                is not None
            )
            if failure == "missing_table":
                assert table_exists is False
            else:
                assert table_exists is True
                assert connection.execute("SELECT COUNT(*) FROM readiness_probe").fetchone()[0] == 0
    store.close()


def test_readiness_does_not_reuse_cached_success_after_store_closes(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "closed-after-ready.sqlite3")
    clock = _ManualClock()
    with TestClient(
        create_app(
            RuntimeSettings(live_ready=False),
            store=store,
            readiness_clock=clock,
        )
    ) as client:
        first = client.get("/readyz")
        store.close()
        closed = client.get("/readyz")
        health = client.get("/health")

    assert first.status_code == 200
    assert closed.status_code == 503
    assert closed.json() == {"status": "not_ready"}
    assert health.status_code == 200


def test_store_initialization_migrates_and_seeds_legacy_database(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy-without-readiness.sqlite3"
    original_store = SQLiteStore(database_path)
    original_store.close()
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE readiness_probe")

    migrated_store = SQLiteStore(database_path)

    assert _readiness_row(database_path) == (1, 0)
    with TestClient(create_app(RuntimeSettings(live_ready=False), store=migrated_store)) as client:
        response = client.get("/readyz")
    assert response.status_code == 200
    assert _readiness_row(database_path) == (1, 1)
    migrated_store.close()


def test_readiness_rolls_back_commit_failure_and_recovers_after_cache_expiry(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "commit-failure.sqlite3"
    store = SQLiteStore(database_path)
    clock = _ManualClock()
    commit_calls = 0

    def fail_first_commit(connection: sqlite3.Connection) -> None:
        nonlocal commit_calls
        commit_calls += 1
        if commit_calls == 1:
            raise sqlite3.OperationalError("simulated commit failure")
        connection.commit()

    with TestClient(
        create_app(
            RuntimeSettings(live_ready=False),
            store=store,
            readiness_clock=clock,
            readiness_commit=fail_first_commit,
        )
    ) as client:
        failed = client.get("/readyz")
        rolled_back_generation = _readiness_row(database_path)
        cached_failure = client.get("/readyz")
        clock.advance_past_cache()
        recovered = client.get("/readyz")

    assert failed.status_code == 503
    assert rolled_back_generation == (1, 0)
    assert cached_failure.status_code == 503
    assert recovered.status_code == 200
    assert commit_calls == 2
    assert _readiness_row(database_path) == (1, 1)
    store.close()


def test_concurrent_readiness_requests_coalesce_to_one_committed_probe(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "coalesced.sqlite3"
    store = SQLiteStore(database_path)
    commit_entered = Event()
    allow_commit = Event()
    count_lock = Lock()
    commit_calls = 0

    def controlled_commit(connection: sqlite3.Connection) -> None:
        nonlocal commit_calls
        with count_lock:
            commit_calls += 1
        commit_entered.set()
        assert allow_commit.wait(timeout=2)
        connection.commit()

    with TestClient(
        create_app(
            RuntimeSettings(live_ready=False),
            store=store,
            readiness_commit=controlled_commit,
        )
    ) as client:

        def request() -> int:
            return client.get("/readyz").status_code

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(request) for _ in range(8)]
            assert commit_entered.wait(timeout=2)
            allow_commit.set()
            statuses = [future.result(timeout=2) for future in futures]

    assert statuses == [200] * 8
    assert commit_calls == 1
    assert _readiness_row(database_path) == (1, 1)
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
    "mutation",
    [
        "missing_manifest",
        "malformed_manifest",
        "deep_manifest",
        "duplicate_manifest_key",
        "noncanonical_manifest",
        "reordered_manifest_keys",
        "oversized_manifest",
        "unsafe_manifest_path",
        "duplicate_manifest_path",
        "boolean_manifest_size",
        "uppercase_manifest_digest",
        "missing_asset",
        "empty_asset",
        "modified_asset",
        "asset_directory",
        "internal_asset_symlink",
        "external_asset_symlink",
        "extra_asset",
        "extra_empty_asset_directory",
        "unsafe_asset_directory",
        "too_many_asset_directories",
        "too_many_asset_files",
        "empty_index",
        "modified_index",
        "missing_index_reference",
        "relative_index_reference",
    ],
)
def test_readiness_rejects_incomplete_or_changed_static_artifact(
    tmp_path: Path,
    mutation: str,
) -> None:
    store = SQLiteStore(tmp_path / f"static-{mutation}.sqlite3")
    static_dir = _static_dir(tmp_path)
    manifest_path = static_dir / _STATIC_MANIFEST_NAME
    asset_path = static_dir / "assets" / "index-deadbeef.js"
    index_path = static_dir / "index.html"
    if mutation == "missing_manifest":
        manifest_path.unlink()
    elif mutation == "malformed_manifest":
        manifest_path.write_text("{", encoding="utf-8")
    elif mutation == "deep_manifest":
        manifest_path.write_text(
            ("[" * 20_000) + "0" + ("]" * 20_000),
            encoding="utf-8",
        )
    elif mutation == "duplicate_manifest_key":
        manifest_path.write_text(
            manifest_path.read_text(encoding="utf-8").replace(
                '"version": 1,',
                '"version": 1,\n  "version": 1,',
                1,
            ),
            encoding="utf-8",
        )
    elif mutation == "noncanonical_manifest":
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    elif mutation == "reordered_manifest_keys":
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_path.write_text(
            json.dumps(
                {
                    "files": manifest["files"],
                    "version": manifest["version"],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    elif mutation == "oversized_manifest":
        manifest_path.write_bytes(b" " * (64 * 1024 + 1))
    elif mutation in {
        "unsafe_manifest_path",
        "duplicate_manifest_path",
        "boolean_manifest_size",
        "uppercase_manifest_digest",
    }:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if mutation == "unsafe_manifest_path":
            manifest["files"][0]["path"] = "../outside.js"
        elif mutation == "duplicate_manifest_path":
            manifest["files"].append(dict(manifest["files"][0]))
        elif mutation == "boolean_manifest_size":
            manifest["files"][0]["size"] = True
        else:
            manifest["files"][0]["sha256"] = manifest["files"][0]["sha256"].upper()
        manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n",
            encoding="utf-8",
        )
    elif mutation == "missing_asset":
        asset_path.unlink()
    elif mutation == "empty_asset":
        asset_path.write_bytes(b"")
    elif mutation == "modified_asset":
        original = asset_path.read_bytes()
        asset_path.write_bytes(original[::-1])
    elif mutation == "asset_directory":
        asset_path.unlink()
        asset_path.mkdir()
    elif mutation in {"internal_asset_symlink", "external_asset_symlink"}:
        asset_path.unlink()
        target = (
            static_dir / "assets" / "index-feedface.css"
            if mutation == "internal_asset_symlink"
            else tmp_path / "outside.js"
        )
        if mutation == "external_asset_symlink":
            target.write_text("outside static root", encoding="utf-8")
        asset_path.symlink_to(target)
    elif mutation == "extra_asset":
        (static_dir / "assets" / "unmanifested.js").write_text(
            "unmanifested",
            encoding="utf-8",
        )
    elif mutation == "extra_empty_asset_directory":
        (static_dir / "assets" / "empty").mkdir()
    elif mutation == "unsafe_asset_directory":
        (static_dir / "assets" / "_unsafe").mkdir()
    elif mutation == "too_many_asset_directories":
        for branch in ("a", "b", "c"):
            branch_path = static_dir / "assets" / branch
            for _ in range(86):
                branch_path /= "d"
            branch_path.mkdir(parents=True)
            (branch_path / "leaf.js").write_text("x", encoding="utf-8")
    elif mutation == "too_many_asset_files":
        for index in range(254):
            (static_dir / "assets" / f"extra-{index:03d}.js").write_text(
                "x",
                encoding="utf-8",
            )
    elif mutation == "empty_index":
        index_path.write_bytes(b"")
    elif mutation == "modified_index":
        index_path.write_bytes(index_path.read_bytes() + b"<!-- changed -->")
    elif mutation == "missing_index_reference":
        index_path.write_text(
            index_path.read_text(encoding="utf-8").replace(
                "/assets/index-deadbeef.js",
                "/assets/missing-entry.js",
            ),
            encoding="utf-8",
        )
        _write_static_manifest(static_dir)
    else:
        index_path.write_text(
            index_path.read_text(encoding="utf-8").replace(
                "/assets/index-deadbeef.js",
                "assets/index-deadbeef.js",
            ),
            encoding="utf-8",
        )
        _write_static_manifest(static_dir)

    with TestClient(
        create_app(
            RuntimeSettings(live_ready=False),
            store=store,
            static_dir=static_dir,
        )
    ) as client:
        readiness = client.get("/readyz")
        health = client.get("/health")
        root = client.get("/")
        static_asset = client.get("/assets/index-feedface.css")

    assert readiness.status_code == 503
    assert readiness.json() == {"status": "not_ready"}
    assert str(static_dir) not in readiness.text
    assert mutation not in readiness.text
    assert health.status_code == 200
    assert root.status_code == 404
    assert static_asset.status_code == 404
    store.close()


def test_static_readiness_cache_detects_mutation_then_recovers(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "static-cache.sqlite3")
    static_dir = _static_dir(tmp_path)
    asset_path = static_dir / "assets" / "index-deadbeef.js"
    original = asset_path.read_bytes()
    clock = _ManualClock()

    with TestClient(
        create_app(
            RuntimeSettings(live_ready=False),
            store=store,
            static_dir=static_dir,
            readiness_clock=clock,
        )
    ) as client:
        first = client.get("/readyz")
        asset_path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
        cached_success = client.get("/readyz")
        clock.advance_past_cache()
        changed = client.get("/readyz")
        asset_path.write_bytes(original)
        cached_failure = client.get("/readyz")
        clock.advance_past_cache()
        recovered = client.get("/readyz")

    assert first.status_code == 200
    assert cached_success.status_code == 200
    assert changed.status_code == 503
    assert cached_failure.status_code == 503
    assert recovered.status_code == 200
    store.close()


def test_static_assets_missing_at_start_can_recover_without_restart(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "static-mount-recovery.sqlite3")
    static_dir = _static_dir(tmp_path)
    assets_dir = static_dir / "assets"
    held_assets_dir = tmp_path / "held-assets"
    assets_dir.rename(held_assets_dir)
    clock = _ManualClock()

    with TestClient(
        create_app(
            RuntimeSettings(live_ready=False),
            store=store,
            static_dir=static_dir,
            readiness_clock=clock,
        )
    ) as client:
        missing = client.get("/readyz")
        held_assets_dir.rename(assets_dir)
        clock.advance_past_cache()
        recovered = client.get("/readyz")
        javascript = client.get("/assets/index-deadbeef.js")

    assert missing.status_code == 503
    assert recovered.status_code == 200
    assert javascript.status_code == 200
    assert javascript.text == 'document.body.dataset.runtime = "ready";'
    store.close()


@pytest.mark.parametrize(
    "failure",
    [
        "closed",
        "missing_schema",
        "malformed_schema",
        "malformed_creation_ip",
        "malformed_creation_ip_value",
        "missing_quota_execution_contract",
        "missing_quota_legacy_provenance",
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
                        "ALTER TABLE receipts RENAME COLUMN receipt_json TO broken_receipt"
                    )
                elif failure == "malformed_creation_ip":
                    connection.execute(
                        "ALTER TABLE recovery_creations RENAME COLUMN ip_key TO broken_ip_key"
                    )
                elif failure == "malformed_creation_ip_value":
                    connection.execute(
                        """
                        INSERT INTO recovery_creations (
                            request_key, request_fingerprint, session_key,
                            ip_key, scenario_id, execution_mode, recovery_id,
                            status, created_at, updated_at, expires_at
                        ) VALUES (?, ?, ?, ?, 'hotel', 'sdk_stub', ?,
                                  'reserved', ?, ?, ?)
                        """,
                        (
                            "a" * 64,
                            "b" * 64,
                            "c" * 64,
                            "z" * 64,
                            "11111111-2222-4333-8444-555555555555",
                            "2026-07-24T12:00:00+00:00",
                            "2026-07-24T12:00:00+00:00",
                            "2026-07-25T12:00:00+00:00",
                        ),
                    )
                elif failure == "missing_quota_execution_contract":
                    connection.execute(
                        "ALTER TABLE recoveries RENAME COLUMN "
                        "quota_execution_contract TO "
                        "broken_quota_execution_contract"
                    )
                elif failure == "missing_quota_legacy_provenance":
                    connection.execute("DROP TABLE quota_legacy_completions")
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
    assert "ip_key" not in public_body
    assert "correlation" not in public_body
    assert health.status_code == 200


def test_api_only_test_app_requires_database_but_not_a_static_index(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "api-only-ready.sqlite3")
    with TestClient(create_app(RuntimeSettings(live_ready=False), store=store)) as client:
        response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    store.close()


def test_start_bind_is_loopback_locally_and_all_interfaces_only_when_deployed() -> None:
    assert bind_host({}) == "127.0.0.1"
    assert bind_host({"BACKCHANNEL_DEPLOYED_MODE": "false"}) == "127.0.0.1"
    assert bind_host({"BACKCHANNEL_DEPLOYED_MODE": "true"}) == "0.0.0.0"


def test_production_launcher_uses_one_bounded_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(app: str, **kwargs: object) -> None:
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr("scripts.start.uvicorn.run", fake_run)
    monkeypatch.delenv("BACKCHANNEL_DEPLOYED_MODE", raising=False)
    monkeypatch.delenv("PORT", raising=False)

    start_main()

    assert captured["app"] == "server.main:app"
    assert captured["workers"] == 1
    assert captured["limit_concurrency"] == 64
    assert captured["backlog"] == 128
    assert captured["timeout_keep_alive"] == 5
    assert start.HTTP_CONCURRENCY_LIMIT == 64
    assert start.HTTP_LISTEN_BACKLOG == 128
    assert start.HTTP_KEEP_ALIVE_TIMEOUT_SECONDS == 5
    assert captured["timeout_graceful_shutdown"] == 3
    assert start.GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS == 3
    assert (
        docker_restart_smoke.CONTAINER_STOP_TIMEOUT_SECONDS
        - start.GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS
        >= 2
    )
