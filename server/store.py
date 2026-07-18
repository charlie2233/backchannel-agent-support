"""SQLite-backed recovery snapshots, ordered events, and replay receipts."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import cast

from pydantic import JsonValue

from server.models import (
    ExecutionMode,
    RecoveryEvent,
    RecoveryReceipt,
    RecoverySnapshot,
    RecoveryStatus,
    ScenarioId,
)

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS recoveries (
    id TEXT PRIMARY KEY,
    scenario_id TEXT NOT NULL CHECK (scenario_id IN ('hotel', 'api-quota')),
    execution_mode TEXT NOT NULL CHECK (
        execution_mode IN ('openai_live', 'sdk_stub', 'replay_fixture')
    ),
    status TEXT NOT NULL CHECK (
        status IN ('in_progress', 'pending_approval', 'completed')
    ),
    current_step INTEGER NOT NULL CHECK (current_step BETWEEN 0 AND 5),
    current_step_summary TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS remedies (
    id TEXT PRIMARY KEY,
    recovery_id TEXT NOT NULL REFERENCES recoveries(id) ON DELETE CASCADE,
    terms_json TEXT NOT NULL,
    digest TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_approvals (
    tool_call_id TEXT PRIMARY KEY,
    recovery_id TEXT NOT NULL REFERENCES recoveries(id) ON DELETE CASCADE,
    serialized_state TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS executions (
    id TEXT PRIMARY KEY,
    recovery_id TEXT NOT NULL REFERENCES recoveries(id) ON DELETE CASCADE,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    provider_execution INTEGER NOT NULL DEFAULT 0 CHECK (provider_execution IN (0, 1)),
    result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recovery_id TEXT NOT NULL REFERENCES recoveries(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL CHECK (seq >= 1),
    type TEXT NOT NULL,
    terminal INTEGER NOT NULL DEFAULT 0 CHECK (terminal IN (0, 1)),
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (recovery_id, seq)
);

CREATE INDEX IF NOT EXISTS events_recovery_seq_idx ON events(recovery_id, seq);

CREATE TABLE IF NOT EXISTS receipts (
    recovery_id TEXT PRIMARY KEY REFERENCES recoveries(id) ON DELETE CASCADE,
    receipt_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recovery_id TEXT NOT NULL REFERENCES recoveries(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    amount INTEGER NOT NULL DEFAULT 0,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS demo_sessions (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""


class RecoveryNotFoundError(LookupError):
    """Raised when a durable recovery or receipt does not exist."""


class SQLiteStore:
    """Small connection-per-transaction store safe for FastAPI worker threads."""

    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)
        self._lock = RLock()
        self._closed = False
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            self._migrate_task2_recovery_constraints(connection)
            event_columns = {
                cast(str, row["name"])
                for row in connection.execute("PRAGMA table_info(events)").fetchall()
            }
            if "terminal" not in event_columns:
                connection.execute(
                    "ALTER TABLE events ADD COLUMN terminal INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute(
                """
                UPDATE events
                SET terminal = 1
                WHERE recovery_id IN (
                    SELECT id FROM recoveries WHERE status = 'completed'
                )
                AND seq = (
                    SELECT MAX(final_event.seq)
                    FROM events AS final_event
                    WHERE final_event.recovery_id = events.recovery_id
                )
                """
            )

    @staticmethod
    def _migrate_task2_recovery_constraints(connection: sqlite3.Connection) -> None:
        """Expand Task 2 CHECK constraints while preserving rows and foreign keys."""

        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'recoveries'"
        ).fetchone()
        if row is None:
            raise RuntimeError("recoveries table was not created")
        table_sql = cast(str, row["sql"])
        if "'sdk_stub'" in table_sql and "'pending_approval'" in table_sql:
            return

        connection.commit()
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DROP TABLE IF EXISTS recoveries_task3")
            connection.execute(
                """
                CREATE TABLE recoveries_task3 (
                    id TEXT PRIMARY KEY,
                    scenario_id TEXT NOT NULL CHECK (
                        scenario_id IN ('hotel', 'api-quota')
                    ),
                    execution_mode TEXT NOT NULL CHECK (
                        execution_mode IN ('openai_live', 'sdk_stub', 'replay_fixture')
                    ),
                    status TEXT NOT NULL CHECK (
                        status IN ('in_progress', 'pending_approval', 'completed')
                    ),
                    current_step INTEGER NOT NULL CHECK (current_step BETWEEN 0 AND 5),
                    current_step_summary TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO recoveries_task3 (
                    id, scenario_id, execution_mode, status, current_step,
                    current_step_summary, created_at, updated_at
                )
                SELECT
                    id, scenario_id, execution_mode, status, current_step,
                    current_step_summary, created_at, updated_at
                FROM recoveries
                """
            )
            connection.execute("DROP TABLE recoveries")
            connection.execute("ALTER TABLE recoveries_task3 RENAME TO recoveries")
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError("Recovery schema migration violated foreign keys")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

    def _connect(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError("SQLiteStore is closed")
        connection = sqlite3.connect(self._database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _recovery_from_row(row: sqlite3.Row) -> RecoverySnapshot:
        return RecoverySnapshot(
            recoveryId=cast(str, row["id"]),
            scenarioId=ScenarioId(cast(str, row["scenario_id"])),
            executionMode=ExecutionMode(cast(str, row["execution_mode"])),
            status=RecoveryStatus(cast(str, row["status"])),
            currentStep=cast(int, row["current_step"]),
            currentStepSummary=cast(str, row["current_step_summary"]),
            createdAt=datetime.fromisoformat(cast(str, row["created_at"])),
            updatedAt=datetime.fromisoformat(cast(str, row["updated_at"])),
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> RecoveryEvent:
        data = cast(dict[str, JsonValue], json.loads(cast(str, row["data_json"])))
        return RecoveryEvent(
            recoveryId=cast(str, row["recovery_id"]),
            seq=cast(int, row["seq"]),
            type=cast(str, row["type"]),
            terminal=bool(cast(int, row["terminal"])),
            data=data,
            createdAt=datetime.fromisoformat(cast(str, row["created_at"])),
        )

    def create_recovery(
        self,
        *,
        recovery_id: str,
        scenario_id: ScenarioId,
        execution_mode: ExecutionMode,
        current_step: int,
        current_step_summary: str,
    ) -> RecoverySnapshot:
        now = self._now()
        created_data = json.dumps(
            {
                "scenarioId": scenario_id.value,
                "executionMode": execution_mode.value,
                "summary": "Recovery created for the selected execution mode.",
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO recoveries (
                    id, scenario_id, execution_mode, status, current_step,
                    current_step_summary, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    recovery_id,
                    scenario_id.value,
                    execution_mode.value,
                    RecoveryStatus.IN_PROGRESS.value,
                    current_step,
                    current_step_summary,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            connection.execute(
                """
                INSERT INTO events (
                    recovery_id, seq, type, terminal, data_json, created_at
                ) VALUES (?, 1, 'recovery.created', 0, ?, ?)
                """,
                (recovery_id, created_data, now.isoformat()),
            )
        return self.get_recovery(recovery_id)

    def record_transition(
        self,
        recovery_id: str,
        *,
        status: RecoveryStatus,
        current_step: int,
        current_step_summary: str,
        event_type: str,
        event_data: dict[str, JsonValue],
        receipt: RecoveryReceipt | None = None,
    ) -> RecoverySnapshot:
        now = self._now()
        event_json = json.dumps(event_data, separators=(",", ":"), sort_keys=True)
        receipt_json = (
            receipt.model_dump_json(by_alias=True, exclude_none=True)
            if receipt is not None
            else None
        )
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT id FROM recoveries WHERE id = ?", (recovery_id,)
            ).fetchone()
            if existing is None:
                raise RecoveryNotFoundError("Recovery not found")
            next_sequence = cast(
                int,
                connection.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE recovery_id = ?",
                    (recovery_id,),
                ).fetchone()[0],
            )
            connection.execute(
                """
                UPDATE recoveries
                SET status = ?, current_step = ?, current_step_summary = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    status.value,
                    current_step,
                    current_step_summary,
                    now.isoformat(),
                    recovery_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO events (
                    recovery_id, seq, type, terminal, data_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    recovery_id,
                    next_sequence,
                    event_type,
                    int(status is RecoveryStatus.COMPLETED),
                    event_json,
                    now.isoformat(),
                ),
            )
            if receipt_json is not None:
                connection.execute(
                    """
                    INSERT INTO receipts (recovery_id, receipt_json, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (recovery_id, receipt_json, now.isoformat()),
                )
        return self.get_recovery(recovery_id)

    def get_recovery(self, recovery_id: str) -> RecoverySnapshot:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM recoveries WHERE id = ?", (recovery_id,)
            ).fetchone()
        if row is None:
            raise RecoveryNotFoundError("Recovery not found")
        return self._recovery_from_row(row)

    def list_events(self, recovery_id: str, *, after_seq: int = 0) -> list[RecoveryEvent]:
        events, _ = self.read_event_batch(recovery_id, after_seq=after_seq)
        return events

    def read_event_batch(
        self, recovery_id: str, *, after_seq: int = 0
    ) -> tuple[list[RecoveryEvent], RecoveryStatus]:
        """Read events and status from one explicit SQLite snapshot."""

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN")
            rows = connection.execute(
                """
                SELECT recovery_id, seq, type, terminal, data_json, created_at
                FROM events
                WHERE recovery_id = ? AND seq > ?
                ORDER BY seq ASC
                """,
                (recovery_id, after_seq),
            ).fetchall()
            recovery = connection.execute(
                "SELECT status FROM recoveries WHERE id = ?", (recovery_id,)
            ).fetchone()
            if recovery is None:
                raise RecoveryNotFoundError("Recovery not found")
            recovery_status = RecoveryStatus(cast(str, recovery["status"]))
            connection.commit()
        return [self._event_from_row(row) for row in rows], recovery_status

    def get_receipt(self, recovery_id: str) -> RecoveryReceipt:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT receipt_json FROM receipts WHERE recovery_id = ?", (recovery_id,)
            ).fetchone()
        if row is None:
            raise RecoveryNotFoundError("Receipt not found")
        return RecoveryReceipt.model_validate_json(cast(str, row["receipt_json"]))

    def reset(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM demo_sessions")
            connection.execute("DELETE FROM recoveries")

    def close(self) -> None:
        with self._lock:
            self._closed = True
