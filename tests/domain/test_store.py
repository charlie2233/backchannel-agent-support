import sqlite3
from uuid import UUID

from server.models import ExecutionMode
from server.replay.engine import ReplayEngine
from server.replay.loader import ScenarioLoader
from server.store import SQLiteStore

REQUIRED_TABLES = {
    "demo_sessions",
    "events",
    "executions",
    "pending_approvals",
    "receipts",
    "recoveries",
    "remedies",
    "usage_ledger",
}


def test_hotel_recovery_and_ordered_events_survive_reopen(tmp_path) -> None:
    database_path = tmp_path / "backchannel.sqlite3"
    store = SQLiteStore(database_path)
    recovery = ReplayEngine(store, ScenarioLoader()).start(
        "hotel", execution_mode=ExecutionMode.REPLAY_FIXTURE
    )

    UUID(recovery.recovery_id)
    events = store.list_events(recovery.recovery_id)
    sequences = [event.seq for event in events]
    assert sequences == sorted(sequences)
    assert sequences == list(range(1, len(sequences) + 1))

    store.close()
    reopened = SQLiteStore(database_path)
    assert reopened.get_recovery(recovery.recovery_id) == recovery
    assert reopened.list_events(recovery.recovery_id) == events

    with sqlite3.connect(database_path) as connection:
        table_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert REQUIRED_TABLES <= table_names
    reopened.close()
