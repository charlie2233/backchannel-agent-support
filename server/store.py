"""SQLite-backed recovery snapshots, ordered events, and replay receipts."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, cast

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
    sdk_version TEXT NOT NULL,
    protocol_version TEXT NOT NULL,
    agent_graph_version TEXT NOT NULL,
    definition_digest TEXT NOT NULL,
    root_trace_id TEXT NOT NULL,
    execution_mode TEXT NOT NULL,
    remedy_digest TEXT NOT NULL,
    state_json TEXT NOT NULL,
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
    request_digest TEXT,
    tool_call_id TEXT,
    remedy_digest TEXT,
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


class ReceiptTransitionError(ValueError):
    """Raised when receipt provenance does not match its terminal transition."""


class ExecutionConflictError(ValueError):
    """Raised when a durable idempotency key is reused for different terms."""


@dataclass(frozen=True, slots=True)
class PendingApprovalEnvelope:
    """Internal-only durable envelope around one actual SDK RunState payload."""

    tool_call_id: str
    recovery_id: str
    sdk_version: str
    protocol_version: str
    agent_graph_version: str
    definition_digest: str
    root_trace_id: str
    execution_mode: ExecutionMode
    remedy_digest: str
    state_json: dict[str, Any]
    status: str = "pending"


@dataclass(frozen=True, slots=True)
class DurableExecution:
    """One durable demo-provider result keyed by exact approved action terms."""

    execution_id: str
    recovery_id: str
    idempotency_key: str
    status: str
    provider_execution: bool
    request_digest: str | None
    tool_call_id: str | None
    remedy_digest: str | None
    result_json: dict[str, Any] | None


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
            self._migrate_task3_pending_approvals(connection)
            self._migrate_task3_executions(connection)
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
    def _migrate_task3_pending_approvals(connection: sqlite3.Connection) -> None:
        """Upgrade Task 3 state rows without deserializing or discarding legacy payloads."""

        columns = {
            cast(str, row["name"])
            for row in connection.execute(
                "PRAGMA table_info(pending_approvals)"
            ).fetchall()
        }
        required = {
            "sdk_version",
            "protocol_version",
            "agent_graph_version",
            "definition_digest",
            "root_trace_id",
            "execution_mode",
            "remedy_digest",
            "state_json",
        }
        if required <= columns:
            return
        if "serialized_state" not in columns:
            raise RuntimeError("Unsupported pending approval schema")

        connection.commit()
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DROP TABLE IF EXISTS pending_approvals_task4")
            connection.execute(
                """
                CREATE TABLE pending_approvals_task4 (
                    tool_call_id TEXT PRIMARY KEY,
                    recovery_id TEXT NOT NULL REFERENCES recoveries(id) ON DELETE CASCADE,
                    sdk_version TEXT NOT NULL,
                    protocol_version TEXT NOT NULL,
                    agent_graph_version TEXT NOT NULL,
                    definition_digest TEXT NOT NULL,
                    root_trace_id TEXT NOT NULL,
                    execution_mode TEXT NOT NULL,
                    remedy_digest TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO pending_approvals_task4 (
                    tool_call_id, recovery_id, sdk_version, protocol_version,
                    agent_graph_version, definition_digest, root_trace_id,
                    execution_mode, remedy_digest, state_json, status,
                    created_at, updated_at
                )
                SELECT
                    tool_call_id,
                    recovery_id,
                    'legacy-incompatible',
                    'legacy-incompatible',
                    'legacy-incompatible',
                    'legacy-incompatible',
                    'legacy-incompatible',
                    COALESCE(
                        (SELECT execution_mode FROM recoveries
                         WHERE recoveries.id = pending_approvals.recovery_id),
                        'sdk_stub'
                    ),
                    'legacy-incompatible',
                    serialized_state,
                    status,
                    created_at,
                    updated_at
                FROM pending_approvals
                """
            )
            connection.execute("DROP TABLE pending_approvals")
            connection.execute(
                "ALTER TABLE pending_approvals_task4 RENAME TO pending_approvals"
            )
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError("Pending approval migration violated foreign keys")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _migrate_task3_executions(connection: sqlite3.Connection) -> None:
        """Add durable action identity fields while preserving existing execution rows."""

        columns = {
            cast(str, row["name"])
            for row in connection.execute("PRAGMA table_info(executions)").fetchall()
        }
        for column in ("request_digest", "tool_call_id", "remedy_digest"):
            if column not in columns:
                connection.execute(f"ALTER TABLE executions ADD COLUMN {column} TEXT")

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

    @staticmethod
    def _pending_approval_from_row(row: sqlite3.Row) -> PendingApprovalEnvelope:
        state_json = json.loads(cast(str, row["state_json"]))
        if not isinstance(state_json, dict):
            raise ValueError("Serialized SDK state must be a JSON object")
        return PendingApprovalEnvelope(
            tool_call_id=cast(str, row["tool_call_id"]),
            recovery_id=cast(str, row["recovery_id"]),
            sdk_version=cast(str, row["sdk_version"]),
            protocol_version=cast(str, row["protocol_version"]),
            agent_graph_version=cast(str, row["agent_graph_version"]),
            definition_digest=cast(str, row["definition_digest"]),
            root_trace_id=cast(str, row["root_trace_id"]),
            execution_mode=ExecutionMode(cast(str, row["execution_mode"])),
            remedy_digest=cast(str, row["remedy_digest"]),
            state_json=state_json,
            status=cast(str, row["status"]),
        )

    @staticmethod
    def _execution_from_row(row: sqlite3.Row) -> DurableExecution:
        raw_result = row["result_json"]
        result_json: dict[str, Any] | None = None
        if raw_result is not None:
            parsed = json.loads(cast(str, raw_result))
            if not isinstance(parsed, dict):
                raise ValueError("Durable execution result must be a JSON object")
            result_json = parsed
        return DurableExecution(
            execution_id=cast(str, row["id"]),
            recovery_id=cast(str, row["recovery_id"]),
            idempotency_key=cast(str, row["idempotency_key"]),
            status=cast(str, row["status"]),
            provider_execution=bool(cast(int, row["provider_execution"])),
            request_digest=cast(str | None, row["request_digest"]),
            tool_call_id=cast(str | None, row["tool_call_id"]),
            remedy_digest=cast(str | None, row["remedy_digest"]),
            result_json=result_json,
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
        pending_approval: PendingApprovalEnvelope | None = None,
    ) -> RecoverySnapshot:
        now = self._now()
        event_json = json.dumps(event_data, separators=(",", ":"), sort_keys=True)
        receipt_json = (
            receipt.model_dump_json(by_alias=True, exclude_none=True)
            if receipt is not None
            else None
        )
        pending_state_json = (
            json.dumps(
                pending_approval.state_json,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            if pending_approval is not None
            else None
        )
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT id, execution_mode FROM recoveries WHERE id = ?", (recovery_id,)
            ).fetchone()
            if existing is None:
                raise RecoveryNotFoundError("Recovery not found")
            if receipt is not None:
                if status is not RecoveryStatus.COMPLETED:
                    raise ReceiptTransitionError(
                        "Receipt requires a completed terminal transition"
                    )
                if receipt.recovery_id != recovery_id:
                    raise ReceiptTransitionError(
                        "Receipt recovery ID does not match the transition"
                    )
                existing_mode = ExecutionMode(cast(str, existing["execution_mode"]))
                if receipt.execution_mode is not existing_mode:
                    raise ReceiptTransitionError(
                        "Receipt execution mode does not match the recovery"
                    )
            if pending_approval is not None:
                if pending_approval.recovery_id != recovery_id:
                    raise ValueError("Pending approval recovery ID does not match transition")
                existing_mode = ExecutionMode(cast(str, existing["execution_mode"]))
                if pending_approval.execution_mode is not existing_mode:
                    raise ValueError("Pending approval execution mode does not match recovery")
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
            if pending_approval is not None and pending_state_json is not None:
                connection.execute(
                    """
                    INSERT INTO pending_approvals (
                        tool_call_id, recovery_id, sdk_version, protocol_version,
                        agent_graph_version, definition_digest, root_trace_id,
                        execution_mode, remedy_digest, state_json, status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        pending_approval.tool_call_id,
                        pending_approval.recovery_id,
                        pending_approval.sdk_version,
                        pending_approval.protocol_version,
                        pending_approval.agent_graph_version,
                        pending_approval.definition_digest,
                        pending_approval.root_trace_id,
                        pending_approval.execution_mode.value,
                        pending_approval.remedy_digest,
                        pending_state_json,
                        pending_approval.status,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
            if status is RecoveryStatus.COMPLETED:
                connection.execute(
                    """
                    UPDATE pending_approvals
                    SET status = 'completed', updated_at = ?
                    WHERE recovery_id = ?
                    """,
                    (now.isoformat(), recovery_id),
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

    def get_pending_approval(self, recovery_id: str) -> PendingApprovalEnvelope:
        """Load the opaque SDK state envelope without exposing it through public models."""

        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM pending_approvals
                WHERE recovery_id = ?
                ORDER BY created_at DESC
                """,
                (recovery_id,),
            ).fetchall()
        if not rows:
            raise RecoveryNotFoundError("Pending approval not found")
        if len(rows) != 1:
            raise ValueError("Recovery has an ambiguous pending approval state")
        return self._pending_approval_from_row(rows[0])

    def update_pending_approval_status(self, recovery_id: str, *, status: str) -> None:
        now = self._now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE pending_approvals
                SET status = ?, updated_at = ?
                WHERE recovery_id = ?
                """,
                (status, now.isoformat(), recovery_id),
            )
            if cursor.rowcount != 1:
                raise RecoveryNotFoundError("Pending approval not found")

    def record_completed_execution(
        self,
        *,
        execution_id: str,
        recovery_id: str,
        idempotency_key: str,
        request_digest: str,
        tool_call_id: str,
        remedy_digest: str,
        result_json: dict[str, JsonValue],
    ) -> tuple[DurableExecution, bool]:
        """Atomically persist or replay one completed idempotent provider result."""

        now = self._now()
        serialized_result = json.dumps(
            result_json,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM executions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            dispatched = False
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO executions (
                        id, recovery_id, idempotency_key, status,
                        provider_execution, request_digest, tool_call_id,
                        remedy_digest, result_json, created_at, updated_at
                    ) VALUES (?, ?, ?, 'completed', 1, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        execution_id,
                        recovery_id,
                        idempotency_key,
                        request_digest,
                        tool_call_id,
                        remedy_digest,
                        serialized_result,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                dispatched = True
            else:
                if (
                    cast(str, existing["recovery_id"]) != recovery_id
                    or cast(str | None, existing["request_digest"]) != request_digest
                    or cast(str | None, existing["tool_call_id"]) != tool_call_id
                    or cast(str | None, existing["remedy_digest"]) != remedy_digest
                ):
                    raise ExecutionConflictError(
                        "Idempotency key was already used for a different execution"
                    )
                if (
                    cast(str, existing["status"]) != "completed"
                    or existing["result_json"] is None
                ):
                    connection.execute(
                        """
                        UPDATE executions
                        SET status = 'completed', provider_execution = 1,
                            result_json = ?, updated_at = ?
                        WHERE idempotency_key = ?
                        """,
                        (serialized_result, now.isoformat(), idempotency_key),
                    )
                    dispatched = True
            row = connection.execute(
                "SELECT * FROM executions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is None:
                raise RuntimeError("Durable execution write did not persist")
            execution = self._execution_from_row(row)
        return execution, dispatched

    def count_executions(self, recovery_id: str) -> int:
        with self._lock, self._connect() as connection:
            return cast(
                int,
                connection.execute(
                    "SELECT COUNT(*) FROM executions WHERE recovery_id = ?",
                    (recovery_id,),
                ).fetchone()[0],
            )

    def list_executions_needing_finalization(self) -> list[DurableExecution]:
        """Return committed executions missing a receipt or terminal event."""

        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT executions.*
                FROM executions
                LEFT JOIN receipts
                  ON receipts.recovery_id = executions.recovery_id
                WHERE executions.status = 'completed'
                  AND executions.provider_execution = 1
                  AND (
                    receipts.recovery_id IS NULL
                    OR NOT EXISTS (
                        SELECT 1 FROM events
                        WHERE events.recovery_id = executions.recovery_id
                          AND events.terminal = 1
                    )
                  )
                ORDER BY executions.created_at ASC
                """
            ).fetchall()
        return [self._execution_from_row(row) for row in rows]

    def finalize_completed_execution(
        self,
        execution: DurableExecution,
        *,
        receipt: RecoveryReceipt,
    ) -> bool:
        """Seal one committed execution exactly once without provider redispatch."""

        if execution.result_json is None or execution.status != "completed":
            raise ValueError("Only completed executions with results can be finalized")
        if receipt.recovery_id != execution.recovery_id:
            raise ReceiptTransitionError("Execution receipt recovery ID mismatch")
        now = self._now()
        receipt_json = receipt.model_dump_json(by_alias=True, exclude_none=True)
        changed = False
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            recovery = connection.execute(
                "SELECT execution_mode FROM recoveries WHERE id = ?",
                (execution.recovery_id,),
            ).fetchone()
            if recovery is None:
                raise RecoveryNotFoundError("Recovery not found")
            if ExecutionMode(cast(str, recovery["execution_mode"])) is not receipt.execution_mode:
                raise ReceiptTransitionError("Execution receipt mode mismatch")

            existing_receipt = connection.execute(
                "SELECT 1 FROM receipts WHERE recovery_id = ?",
                (execution.recovery_id,),
            ).fetchone()
            if existing_receipt is None:
                connection.execute(
                    """
                    INSERT INTO receipts (recovery_id, receipt_json, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (execution.recovery_id, receipt_json, now.isoformat()),
                )
                changed = True

            terminal_event = connection.execute(
                """
                SELECT 1 FROM events
                WHERE recovery_id = ? AND terminal = 1
                """,
                (execution.recovery_id,),
            ).fetchone()
            if terminal_event is None:
                next_sequence = cast(
                    int,
                    connection.execute(
                        "SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE recovery_id = ?",
                        (execution.recovery_id,),
                    ).fetchone()[0],
                )
                connection.execute(
                    """
                    INSERT INTO events (
                        recovery_id, seq, type, terminal, data_json, created_at
                    ) VALUES (?, ?, 'recovery.completed', 1, ?, ?)
                    """,
                    (
                        execution.recovery_id,
                        next_sequence,
                        json.dumps(
                            {
                                "phase": "Verify & seal",
                                "providerExecution": True,
                                "summary": (
                                    "Committed demo-provider result finalized after restart."
                                ),
                            },
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        now.isoformat(),
                    ),
                )
                changed = True

            connection.execute(
                """
                UPDATE recoveries
                SET status = 'completed', current_step = 5,
                    current_step_summary = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    "Demo provider result verified and receipt sealed.",
                    now.isoformat(),
                    execution.recovery_id,
                ),
            )
            connection.execute(
                """
                UPDATE pending_approvals
                SET status = 'completed', updated_at = ?
                WHERE recovery_id = ?
                """,
                (now.isoformat(), execution.recovery_id),
            )
        return changed

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
