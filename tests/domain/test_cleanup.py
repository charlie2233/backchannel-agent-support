from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from server.cleanup import cleanup_terminal_recoveries
from server.config import RuntimeSettings
from server.main import create_app
from server.models import ExecutionMode, RecoveryStatus, ScenarioId
from server.store import RecoveryNotFoundError, SQLiteStore


def _create_recovery(store: SQLiteStore, recovery_id: str, *, terminal: bool) -> None:
    store.create_recovery(
        recovery_id=recovery_id,
        scenario_id=ScenarioId.HOTEL,
        execution_mode=ExecutionMode.REPLAY_FIXTURE,
        current_step=0,
        current_step_summary="Created for cleanup test.",
    )
    if terminal:
        store.record_transition(
            recovery_id,
            status=RecoveryStatus.COMPLETED,
            current_step=5,
            current_step_summary="Terminal cleanup fixture.",
            event_type="recovery.completed",
            event_data={"summary": "Terminal cleanup fixture."},
        )


def test_cleanup_deletes_only_expired_terminal_rows_and_preserves_usage(tmp_path) -> None:
    database_path = tmp_path / "cleanup.sqlite3"
    store = SQLiteStore(database_path)
    _create_recovery(store, "expired-terminal", terminal=True)
    _create_recovery(store, "recent-terminal", terminal=True)
    _create_recovery(store, "old-in-progress", terminal=False)
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    old = (now - timedelta(days=2)).isoformat()
    recent = (now - timedelta(minutes=5)).isoformat()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE recoveries SET updated_at = ? WHERE id IN (?, ?)",
            (old, "expired-terminal", "old-in-progress"),
        )
        connection.execute(
            "UPDATE recoveries SET updated_at = ? WHERE id = ?",
            (recent, "recent-terminal"),
        )
        connection.execute(
            "INSERT INTO usage_ledger (recovery_id, category, amount, recorded_at) "
            "VALUES (?, ?, ?, ?)",
            ("expired-terminal", "live_demo_budget_unit", 1, old),
        )

    deleted = cleanup_terminal_recoveries(
        store,
        terminal_ttl=timedelta(days=1),
        now=now,
        batch_size=10,
    )

    assert deleted == 1
    with pytest.raises(RecoveryNotFoundError):
        store.get_recovery("expired-terminal")
    assert store.get_recovery("recent-terminal").status is RecoveryStatus.COMPLETED
    assert store.get_recovery("old-in-progress").status is RecoveryStatus.IN_PROGRESS
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT category, amount FROM usage_ledger WHERE recovery_id = ?",
            ("expired-terminal",),
        ).fetchall() == [("live_demo_budget_unit", 1)]

    assert (
        cleanup_terminal_recoveries(
            store,
            terminal_ttl=timedelta(days=1),
            now=now,
            batch_size=10,
        )
        == 0
    )


def test_cleanup_rejects_unbounded_or_nonpositive_inputs(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "cleanup-bounds.sqlite3")

    with pytest.raises(ValueError, match="terminal_ttl"):
        cleanup_terminal_recoveries(store, terminal_ttl=timedelta(0), batch_size=10)
    with pytest.raises(ValueError, match="batch_size"):
        cleanup_terminal_recoveries(store, terminal_ttl=timedelta(days=1), batch_size=0)


def test_lifespan_periodically_removes_idle_expired_terminal_detail(tmp_path) -> None:
    database_path = tmp_path / "periodic-cleanup.sqlite3"
    store = SQLiteStore(database_path)
    _create_recovery(store, "idle-terminal", terminal=True)
    recorded_at = datetime.now(UTC).isoformat()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO usage_ledger (recovery_id, category, amount, recorded_at) "
            "VALUES (?, ?, ?, ?)",
            ("idle-terminal", "live_demo_budget_unit", 1, recorded_at),
        )
    settings = RuntimeSettings(
        live_ready=False,
        terminal_recovery_ttl_seconds=1,
        terminal_cleanup_interval_seconds=1,
    )

    with TestClient(create_app(settings, store=store)):
        expired = (datetime.now(UTC) - timedelta(seconds=2)).isoformat()
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                "UPDATE recoveries SET updated_at = ? WHERE id = ?",
                (expired, "idle-terminal"),
            )
        deadline = time.monotonic() + 3
        remaining = 1
        while remaining and time.monotonic() < deadline:
            time.sleep(0.05)
            with sqlite3.connect(database_path) as connection:
                remaining = connection.execute(
                    "SELECT COUNT(*) FROM recoveries WHERE id = ?",
                    ("idle-terminal",),
                ).fetchone()[0]

    assert remaining == 0
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT category, amount FROM usage_ledger WHERE recovery_id = ?",
            ("idle-terminal",),
        ).fetchall() == [("live_demo_budget_unit", 1)]
