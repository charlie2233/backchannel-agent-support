import json
import sqlite3
from uuid import UUID

from server.models import ExecutionMode, RecoveryStatus, ScenarioId
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


def test_task2_recovery_constraints_migrate_without_losing_replay_rows(
    tmp_path,
) -> None:
    database_path = tmp_path / "task2-schema.sqlite3"
    legacy_time = "2026-07-18T12:00:00+00:00"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE recoveries (
                id TEXT PRIMARY KEY,
                scenario_id TEXT NOT NULL CHECK (scenario_id IN ('hotel', 'api-quota')),
                execution_mode TEXT NOT NULL CHECK (execution_mode = 'replay_fixture'),
                status TEXT NOT NULL CHECK (status IN ('in_progress', 'completed')),
                current_step INTEGER NOT NULL CHECK (current_step BETWEEN 0 AND 5),
                current_step_summary TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recovery_id TEXT NOT NULL REFERENCES recoveries(id) ON DELETE CASCADE,
                seq INTEGER NOT NULL CHECK (seq >= 1),
                type TEXT NOT NULL,
                terminal INTEGER NOT NULL DEFAULT 0 CHECK (terminal IN (0, 1)),
                data_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (recovery_id, seq)
            );

            CREATE TABLE receipts (
                recovery_id TEXT PRIMARY KEY REFERENCES recoveries(id) ON DELETE CASCADE,
                receipt_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            """
            INSERT INTO recoveries (
                id, scenario_id, execution_mode, status, current_step,
                current_step_summary, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-replay",
                "api-quota",
                "replay_fixture",
                "completed",
                5,
                "Preserved Task 2 replay row.",
                legacy_time,
                legacy_time,
            ),
        )
        connection.execute(
            """
            INSERT INTO events (
                recovery_id, seq, type, terminal, data_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-replay",
                1,
                "recovery.completed",
                1,
                json.dumps({"summary": "Preserved Task 2 event."}),
                legacy_time,
            ),
        )
        connection.execute(
            """
            INSERT INTO receipts (recovery_id, receipt_json, created_at)
            VALUES (?, ?, ?)
            """,
            (
                "legacy-replay",
                json.dumps(
                    {
                        "recoveryId": "legacy-replay",
                        "executionMode": "replay_fixture",
                        "status": "simulated_completed",
                        "simulated": True,
                        "providerExecution": False,
                        "modelIds": [],
                        "boundary": "Bundled replay only.",
                        "providerResult": "Recorded replay result.",
                        "authorizationSource": "Recorded fixture.",
                        "verificationResults": ["Recorded fixture verified."],
                    }
                ),
                legacy_time,
            ),
        )

    store = SQLiteStore(database_path)

    legacy = store.get_recovery("legacy-replay")
    assert legacy.execution_mode is ExecutionMode.REPLAY_FIXTURE
    assert legacy.status is RecoveryStatus.COMPLETED
    assert [event.type for event in store.list_events("legacy-replay")] == [
        "recovery.completed"
    ]
    assert store.get_receipt("legacy-replay").provider_execution is False
    sdk_recovery = store.create_recovery(
        recovery_id="sdk-stub-recovery",
        scenario_id=ScenarioId.HOTEL,
        execution_mode=ExecutionMode.SDK_STUB,
        current_step=0,
        current_step_summary="SDK stub recovery created after migration.",
    )
    pending = store.record_transition(
        sdk_recovery.recovery_id,
        status=RecoveryStatus.PENDING_APPROVAL,
        current_step=3,
        current_step_summary="SDK interruption is pending.",
        event_type="approval.requested",
        event_data={"providerExecution": False},
    )
    assert pending.execution_mode is ExecutionMode.SDK_STUB
    assert pending.status is RecoveryStatus.PENDING_APPROVAL
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
