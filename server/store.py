"""SQLite-backed recovery snapshots, ordered events, and replay receipts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Literal, cast

from pydantic import JsonValue

from server.agents.schemas import CommitRemedyArguments
from server.digest import remedy_consent_digest
from server.models import (
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    DecisionAction,
    ExecutionMode,
    HotelRemedyTerms,
    PendingApprovalView,
    RecoveryEvent,
    RecoveryReceipt,
    RecoverySnapshot,
    RecoveryStatus,
    ScenarioId,
)
from server.policy import (
    DETERMINISTIC_HOTEL_AUTHORITY,
    evaluate_hotel_policy,
    exact_hotel_terms,
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
        status IN (
            'in_progress', 'pending_approval', 'completed',
            'closed_without_action', 'outcome_unknown'
        )
    ),
    current_step INTEGER NOT NULL CHECK (current_step BETWEEN 0 AND 5),
    current_step_summary TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS remedies (
    id TEXT NOT NULL,
    recovery_id TEXT NOT NULL REFERENCES recoveries(id) ON DELETE CASCADE,
    terms_json TEXT NOT NULL,
    digest TEXT,
    cost_delta_minor INTEGER,
    changed_fields_json TEXT,
    provider_commitments_json TEXT,
    expiry TEXT,
    hard_constraint_satisfied INTEGER CHECK (
        hard_constraint_satisfied IN (0, 1)
    ),
    delegated_authority_satisfied INTEGER CHECK (
        delegated_authority_satisfied IN (0, 1)
    ),
    evidence_json TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (recovery_id, id)
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
    action_digest TEXT NOT NULL,
    remedy_id TEXT NOT NULL,
    consent_digest TEXT NOT NULL,
    state_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approval_decisions (
    recovery_id TEXT PRIMARY KEY REFERENCES recoveries(id) ON DELETE CASCADE,
    client_decision_id TEXT NOT NULL UNIQUE,
    action TEXT NOT NULL CHECK (action IN ('approve', 'decline')),
    remedy_id TEXT NOT NULL,
    remedy_digest TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('claimed', 'completed')),
    result_json TEXT,
    claimed_at TEXT NOT NULL,
    completed_at TEXT,
    CHECK (
        (status = 'claimed' AND result_json IS NULL AND completed_at IS NULL)
        OR
        (status = 'completed' AND result_json IS NOT NULL AND completed_at IS NOT NULL)
    )
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


class ApprovalDecisionError(ValueError):
    """Stable public decision failure without consent or SDK payload details."""

    def __init__(self, code: str, recovery_id: str, *, status_code: int) -> None:
        self.code = code
        self.recovery_id = recovery_id
        self.status_code = status_code
        super().__init__(code)

    @property
    def public_detail(self) -> dict[str, str]:
        return {"code": self.code, "recoveryId": self.recovery_id}


@dataclass(frozen=True, slots=True)
class ApprovalDecisionClaim:
    recovery_id: str
    request: ApprovalDecisionRequest
    request_fingerprint: str
    response: ApprovalDecisionResponse | None = None

    @property
    def resume_required(self) -> bool:
        return self.response is None


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
    action_digest: str
    remedy_id: str
    consent_digest: str
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


@dataclass(frozen=True, slots=True)
class RemedyConsentRecord:
    """Authoritative persisted consent and policy evidence for one remedy."""

    remedy_id: str
    recovery_id: str
    terms: HotelRemedyTerms
    cost_delta_minor: int
    changed_fields: tuple[str, ...]
    provider_commitments: tuple[str, ...]
    expiry: datetime
    consent_digest: str
    hard_constraint_satisfied: bool
    delegated_authority_satisfied: bool
    evidence: CommitRemedyArguments

    def public_view(self, *, tool_call_id: str) -> PendingApprovalView:
        return PendingApprovalView(
            remedyId=self.remedy_id,
            remedyDigest=self.consent_digest,
            terms=self.terms,
            costDeltaMinor=self.cost_delta_minor,
            changedFields=list(self.changed_fields),
            providerCommitments=list(self.provider_commitments),
            expiry=self.expiry,
            hardConstraintSatisfied=self.hard_constraint_satisfied,
            delegatedAuthoritySatisfied=self.delegated_authority_satisfied,
            toolCallId=tool_call_id,
            executionStarted=False,
        )


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
            self._migrate_task4_remedies(connection)
            self._migrate_task3_pending_approvals(connection)
            self._migrate_task3_executions(connection)
            self._migrate_task6_approval_decisions(connection)
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
                    SELECT id FROM recoveries
                    WHERE status IN (
                        'completed', 'closed_without_action', 'outcome_unknown'
                    )
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
        """Rebuild every legacy envelope table to the exact Task 5 contract."""

        columns = {
            cast(str, row["name"])
            for row in connection.execute(
                "PRAGMA table_info(pending_approvals)"
            ).fetchall()
        }
        task5_columns = {
            "tool_call_id",
            "recovery_id",
            "sdk_version",
            "protocol_version",
            "agent_graph_version",
            "definition_digest",
            "root_trace_id",
            "execution_mode",
            "action_digest",
            "remedy_id",
            "consent_digest",
            "state_json",
            "status",
            "created_at",
            "updated_at",
        }
        if columns == task5_columns:
            return

        required_legacy_columns = {
            "tool_call_id",
            "recovery_id",
            "status",
            "created_at",
            "updated_at",
        }
        if not required_legacy_columns <= columns:
            raise RuntimeError("Unsupported pending approval schema")
        if "state_json" in columns:
            state_source = "state_json"
        elif "serialized_state" in columns:
            state_source = "serialized_state"
        else:
            raise RuntimeError("Unsupported pending approval schema")

        def legacy_value(column: str, fallback: str = "'legacy-incompatible'") -> str:
            if column not in columns:
                return fallback
            return f"COALESCE({column}, {fallback})"

        if "action_digest" in columns:
            action_source = legacy_value("action_digest")
        elif "remedy_digest" in columns:
            action_source = legacy_value("remedy_digest")
        else:
            action_source = "'legacy-incompatible'"
        execution_mode_source = legacy_value(
            "execution_mode",
            "COALESCE((SELECT execution_mode FROM recoveries "
            "WHERE recoveries.id = pending_approvals.recovery_id), 'sdk_stub')",
        )

        connection.commit()
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DROP TABLE IF EXISTS pending_approvals_task5")
            connection.execute(
                """
                CREATE TABLE pending_approvals_task5 (
                    tool_call_id TEXT PRIMARY KEY,
                    recovery_id TEXT NOT NULL REFERENCES recoveries(id) ON DELETE CASCADE,
                    sdk_version TEXT NOT NULL,
                    protocol_version TEXT NOT NULL,
                    agent_graph_version TEXT NOT NULL,
                    definition_digest TEXT NOT NULL,
                    root_trace_id TEXT NOT NULL,
                    execution_mode TEXT NOT NULL,
                    action_digest TEXT NOT NULL,
                    remedy_id TEXT NOT NULL,
                    consent_digest TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                f"""
                INSERT INTO pending_approvals_task5 (
                    tool_call_id, recovery_id, sdk_version, protocol_version,
                    agent_graph_version, definition_digest, root_trace_id,
                    execution_mode, action_digest, remedy_id, consent_digest,
                    state_json, status,
                    created_at, updated_at
                )
                SELECT
                    tool_call_id,
                    recovery_id,
                    'legacy-incompatible',
                    {legacy_value("protocol_version")},
                    {legacy_value("agent_graph_version")},
                    {legacy_value("definition_digest")},
                    {legacy_value("root_trace_id")},
                    {execution_mode_source},
                    {action_source},
                    {legacy_value("remedy_id")},
                    {legacy_value("consent_digest")},
                    {state_source},
                    status,
                    created_at,
                    updated_at
                FROM pending_approvals
                """
            )
            connection.execute("DROP TABLE pending_approvals")
            connection.execute(
                "ALTER TABLE pending_approvals_task5 RENAME TO pending_approvals"
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
    def _migrate_task4_remedies(connection: sqlite3.Connection) -> None:
        """Add consent evidence columns without rewriting or dropping legacy remedies."""

        columns = {
            cast(str, row["name"])
            for row in connection.execute("PRAGMA table_info(remedies)").fetchall()
        }
        additions = {
            "cost_delta_minor": "INTEGER",
            "changed_fields_json": "TEXT",
            "provider_commitments_json": "TEXT",
            "expiry": "TEXT",
            "hard_constraint_satisfied": "INTEGER",
            "delegated_authority_satisfied": "INTEGER",
            "evidence_json": "TEXT",
        }
        for column, column_type in additions.items():
            if column not in columns:
                connection.execute(
                    f"ALTER TABLE remedies ADD COLUMN {column} {column_type}"
                )

        primary_key = {
            cast(str, row["name"]): cast(int, row["pk"])
            for row in connection.execute("PRAGMA table_info(remedies)").fetchall()
            if cast(int, row["pk"]) > 0
        }
        if primary_key == {"recovery_id": 1, "id": 2}:
            return
        if primary_key != {"id": 1}:
            raise RuntimeError("Unsupported remedies primary key schema")

        connection.commit()
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DROP TABLE IF EXISTS remedies_task5")
            connection.execute(
                """
                CREATE TABLE remedies_task5 (
                    id TEXT NOT NULL,
                    recovery_id TEXT NOT NULL REFERENCES recoveries(id) ON DELETE CASCADE,
                    terms_json TEXT NOT NULL,
                    digest TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    cost_delta_minor INTEGER,
                    changed_fields_json TEXT,
                    provider_commitments_json TEXT,
                    expiry TEXT,
                    hard_constraint_satisfied INTEGER,
                    delegated_authority_satisfied INTEGER,
                    evidence_json TEXT,
                    PRIMARY KEY (recovery_id, id)
                )
                """
            )
            connection.execute(
                """
                INSERT INTO remedies_task5 (
                    id, recovery_id, terms_json, digest, status, created_at,
                    cost_delta_minor, changed_fields_json,
                    provider_commitments_json, expiry,
                    hard_constraint_satisfied, delegated_authority_satisfied,
                    evidence_json
                )
                SELECT
                    id, recovery_id, terms_json, digest, status, created_at,
                    cost_delta_minor, changed_fields_json,
                    provider_commitments_json, expiry,
                    hard_constraint_satisfied, delegated_authority_satisfied,
                    evidence_json
                FROM remedies
                """
            )
            connection.execute("DROP TABLE remedies")
            connection.execute("ALTER TABLE remedies_task5 RENAME TO remedies")
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError("Remedy migration violated foreign keys")
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
    def _migrate_task6_approval_decisions(connection: sqlite3.Connection) -> None:
        """Add explicit actions while preserving every legacy approval claim."""

        columns = {
            cast(str, row["name"])
            for row in connection.execute(
                "PRAGMA table_info(approval_decisions)"
            ).fetchall()
        }
        if "action" in columns:
            return

        connection.commit()
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
            locked_columns = {
                cast(str, row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(approval_decisions)"
                ).fetchall()
            }
            if "action" in locked_columns:
                connection.commit()
                return
            legacy_rows = connection.execute(
                "SELECT * FROM approval_decisions ORDER BY claimed_at ASC"
            ).fetchall()
            connection.execute("DROP TABLE IF EXISTS approval_decisions_task6")
            connection.execute(
                """
                CREATE TABLE approval_decisions_task6 (
                    recovery_id TEXT PRIMARY KEY
                        REFERENCES recoveries(id) ON DELETE CASCADE,
                    client_decision_id TEXT NOT NULL UNIQUE,
                    action TEXT NOT NULL CHECK (action IN ('approve', 'decline')),
                    remedy_id TEXT NOT NULL,
                    remedy_digest TEXT NOT NULL,
                    tool_call_id TEXT NOT NULL,
                    request_fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('claimed', 'completed')),
                    result_json TEXT,
                    claimed_at TEXT NOT NULL,
                    completed_at TEXT,
                    CHECK (
                        (status = 'claimed' AND result_json IS NULL
                            AND completed_at IS NULL)
                        OR
                        (status = 'completed' AND result_json IS NOT NULL
                            AND completed_at IS NOT NULL)
                    )
                )
                """
            )
            for row in legacy_rows:
                recovery_id = cast(str, row["recovery_id"])
                request = ApprovalDecisionRequest(
                    action=DecisionAction.APPROVE,
                    clientDecisionId=cast(str, row["client_decision_id"]),
                    remedyId=cast(str, row["remedy_id"]),
                    remedyDigest=cast(str, row["remedy_digest"]),
                    toolCallId=cast(str, row["tool_call_id"]),
                )
                migrated_result: str | None = None
                if row["result_json"] is not None:
                    result_payload = json.loads(cast(str, row["result_json"]))
                    if not isinstance(result_payload, dict):
                        raise RuntimeError("Legacy decision result must be a JSON object")
                    result_payload["action"] = DecisionAction.APPROVE.value
                    migrated_result = ApprovalDecisionResponse.model_validate(
                        result_payload
                    ).model_dump_json(by_alias=True)
                connection.execute(
                    """
                    INSERT INTO approval_decisions_task6 (
                        recovery_id, client_decision_id, action, remedy_id,
                        remedy_digest, tool_call_id, request_fingerprint, status,
                        result_json, claimed_at, completed_at
                    ) VALUES (?, ?, 'approve', ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        recovery_id,
                        request.client_decision_id,
                        request.remedy_id,
                        request.remedy_digest,
                        request.tool_call_id,
                        SQLiteStore._decision_fingerprint(recovery_id, request),
                        cast(str, row["status"]),
                        migrated_result,
                        cast(str, row["claimed_at"]),
                        cast(str | None, row["completed_at"]),
                    ),
                )
            connection.execute("DROP TABLE approval_decisions")
            connection.execute(
                "ALTER TABLE approval_decisions_task6 RENAME TO approval_decisions"
            )
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError("Approval decision migration violated foreign keys")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _migrate_task2_recovery_constraints(connection: sqlite3.Connection) -> None:
        """Expand Task 2 CHECK constraints while preserving rows and foreign keys."""

        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'recoveries'"
        ).fetchone()
        if row is None:
            raise RuntimeError("recoveries table was not created")
        table_sql = cast(str, row["sql"])
        if (
            "'sdk_stub'" in table_sql
            and "'pending_approval'" in table_sql
            and "'closed_without_action'" in table_sql
            and "'outcome_unknown'" in table_sql
        ):
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
                        status IN (
                            'in_progress', 'pending_approval', 'completed',
                            'closed_without_action', 'outcome_unknown'
                        )
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
    def _recovery_from_row(
        row: sqlite3.Row,
        *,
        pending_approval: PendingApprovalView | None = None,
    ) -> RecoverySnapshot:
        return RecoverySnapshot(
            recoveryId=cast(str, row["id"]),
            scenarioId=ScenarioId(cast(str, row["scenario_id"])),
            executionMode=ExecutionMode(cast(str, row["execution_mode"])),
            status=RecoveryStatus(cast(str, row["status"])),
            currentStep=cast(int, row["current_step"]),
            currentStepSummary=cast(str, row["current_step_summary"]),
            createdAt=datetime.fromisoformat(cast(str, row["created_at"])),
            updatedAt=datetime.fromisoformat(cast(str, row["updated_at"])),
            pendingApproval=pending_approval,
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
            action_digest=cast(str, row["action_digest"]),
            remedy_id=cast(str, row["remedy_id"]),
            consent_digest=cast(str, row["consent_digest"]),
            state_json=state_json,
            status=cast(str, row["status"]),
        )

    @staticmethod
    def _remedy_consent_from_row(row: sqlite3.Row) -> RemedyConsentRecord:
        required = (
            "digest",
            "cost_delta_minor",
            "changed_fields_json",
            "provider_commitments_json",
            "expiry",
            "hard_constraint_satisfied",
            "delegated_authority_satisfied",
            "evidence_json",
        )
        if any(row[field] is None for field in required):
            raise ValueError("Legacy remedy does not contain a consent record")
        changed_fields = json.loads(cast(str, row["changed_fields_json"]))
        provider_commitments = json.loads(
            cast(str, row["provider_commitments_json"])
        )
        if not isinstance(changed_fields, list) or not all(
            isinstance(item, str) for item in changed_fields
        ):
            raise ValueError("Stored changed fields must be a string array")
        if not isinstance(provider_commitments, list) or not all(
            isinstance(item, str) for item in provider_commitments
        ):
            raise ValueError("Stored provider commitments must be a string array")
        return RemedyConsentRecord(
            remedy_id=cast(str, row["id"]),
            recovery_id=cast(str, row["recovery_id"]),
            terms=HotelRemedyTerms.model_validate_json(cast(str, row["terms_json"])),
            cost_delta_minor=cast(int, row["cost_delta_minor"]),
            changed_fields=tuple(changed_fields),
            provider_commitments=tuple(provider_commitments),
            expiry=datetime.fromisoformat(cast(str, row["expiry"])),
            consent_digest=cast(str, row["digest"]),
            hard_constraint_satisfied=bool(
                cast(int, row["hard_constraint_satisfied"])
            ),
            delegated_authority_satisfied=bool(
                cast(int, row["delegated_authority_satisfied"])
            ),
            evidence=CommitRemedyArguments.model_validate_json(
                cast(str, row["evidence_json"])
            ),
        )

    @staticmethod
    def _decision_fingerprint(
        recovery_id: str,
        request: ApprovalDecisionRequest,
    ) -> str:
        serialized = json.dumps(
            {
                "recoveryId": recovery_id,
                **request.model_dump(mode="json", by_alias=True),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(serialized).hexdigest()

    @staticmethod
    def _decision_request_from_row(row: sqlite3.Row) -> ApprovalDecisionRequest:
        return ApprovalDecisionRequest(
            action=DecisionAction(cast(str, row["action"])),
            clientDecisionId=cast(str, row["client_decision_id"]),
            remedyId=cast(str, row["remedy_id"]),
            remedyDigest=cast(str, row["remedy_digest"]),
            toolCallId=cast(str, row["tool_call_id"]),
        )

    @classmethod
    def _decision_claim_from_row(cls, row: sqlite3.Row) -> ApprovalDecisionClaim:
        response: ApprovalDecisionResponse | None = None
        if row["result_json"] is not None:
            response = ApprovalDecisionResponse.model_validate_json(
                cast(str, row["result_json"])
            )
        return ApprovalDecisionClaim(
            recovery_id=cast(str, row["recovery_id"]),
            request=cls._decision_request_from_row(row),
            request_fingerprint=cast(str, row["request_fingerprint"]),
            response=response,
        )

    def _validate_consent_for_decision(
        self,
        connection: sqlite3.Connection,
        recovery_id: str,
        request: ApprovalDecisionRequest,
        *,
        now: datetime,
    ) -> tuple[PendingApprovalEnvelope, RemedyConsentRecord]:
        recovery = connection.execute(
            "SELECT * FROM recoveries WHERE id = ?",
            (recovery_id,),
        ).fetchone()
        if recovery is None:
            raise ApprovalDecisionError(
                "decision_unavailable", recovery_id, status_code=404
            )
        if (
            cast(str, recovery["status"]) != RecoveryStatus.PENDING_APPROVAL.value
            or cast(str, recovery["scenario_id"]) != ScenarioId.HOTEL.value
            or cast(str, recovery["execution_mode"]) != ExecutionMode.SDK_STUB.value
        ):
            raise ApprovalDecisionError(
                "decision_unavailable", recovery_id, status_code=409
            )

        pending_row = connection.execute(
            "SELECT * FROM pending_approvals WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone()
        if pending_row is None:
            raise ApprovalDecisionError(
                "decision_unavailable", recovery_id, status_code=409
            )
        try:
            pending = self._pending_approval_from_row(pending_row)
        except (TypeError, ValueError):
            raise ApprovalDecisionError(
                "resume_incompatible", recovery_id, status_code=409
            ) from None
        if pending.status not in {"pending", "approved"}:
            raise ApprovalDecisionError(
                "decision_unavailable", recovery_id, status_code=409
            )
        if request.remedy_id != pending.remedy_id:
            raise ApprovalDecisionError(
                "remedy_mismatch", recovery_id, status_code=422
            )
        if request.tool_call_id != pending.tool_call_id:
            raise ApprovalDecisionError(
                "tool_call_mismatch", recovery_id, status_code=422
            )
        if request.remedy_digest != pending.consent_digest:
            raise ApprovalDecisionError(
                "remedy_digest_mismatch", recovery_id, status_code=422
            )

        remedy_row = connection.execute(
            "SELECT * FROM remedies WHERE recovery_id = ? AND id = ?",
            (recovery_id, pending.remedy_id),
        ).fetchone()
        if remedy_row is None:
            raise ApprovalDecisionError(
                "decision_unavailable", recovery_id, status_code=409
            )
        try:
            consent = self._remedy_consent_from_row(remedy_row)
            recomputed_digest = remedy_consent_digest(
                {
                    "recoveryId": recovery_id,
                    "remedyId": consent.remedy_id,
                    "terms": consent.terms.model_dump(mode="json", by_alias=True),
                    "costDeltaMinor": consent.cost_delta_minor,
                    "changedFields": list(consent.changed_fields),
                    "providerCommitments": list(consent.provider_commitments),
                    "expiry": consent.expiry,
                }
            )
        except (TypeError, ValueError):
            raise ApprovalDecisionError(
                "remedy_digest_mismatch", recovery_id, status_code=422
            ) from None
        if (
            consent.consent_digest != pending.consent_digest
            or recomputed_digest != pending.consent_digest
        ):
            raise ApprovalDecisionError(
                "remedy_digest_mismatch", recovery_id, status_code=422
            )
        if consent.expiry <= now:
            raise ApprovalDecisionError("remedy_expired", recovery_id, status_code=422)
        if exact_hotel_terms(consent.evidence) != consent.terms:
            raise ApprovalDecisionError(
                "constraint_denied", recovery_id, status_code=422
            )
        policy = evaluate_hotel_policy(
            consent.evidence,
            DETERMINISTIC_HOTEL_AUTHORITY,
        )
        if (
            not policy.hard_constraint_satisfied
            or consent.hard_constraint_satisfied
            != policy.hard_constraint_satisfied
        ):
            raise ApprovalDecisionError(
                "constraint_denied", recovery_id, status_code=422
            )
        if (
            not policy.delegated_authority_satisfied
            or consent.delegated_authority_satisfied
            != policy.delegated_authority_satisfied
        ):
            raise ApprovalDecisionError(
                "authority_denied", recovery_id, status_code=422
            )
        return pending, consent

    def _public_pending_view(
        self,
        connection: sqlite3.Connection,
        recovery_id: str,
    ) -> PendingApprovalView | None:
        pending = connection.execute(
            """
            SELECT tool_call_id, remedy_id, consent_digest
            FROM pending_approvals
            WHERE recovery_id = ?
              AND status = 'pending'
              AND NOT EXISTS (
                  SELECT 1 FROM approval_decisions
                  WHERE approval_decisions.recovery_id = pending_approvals.recovery_id
              )
            ORDER BY created_at DESC
            """,
            (recovery_id,),
        ).fetchone()
        if pending is None or cast(str, pending["consent_digest"]) == "legacy-incompatible":
            return None
        remedy = connection.execute(
            """
            SELECT * FROM remedies
            WHERE recovery_id = ? AND id = ?
            """,
            (recovery_id, cast(str, pending["remedy_id"])),
        ).fetchone()
        if remedy is None:
            raise ValueError("Pending approval consent linkage is invalid")
        consent = self._remedy_consent_from_row(remedy)
        if consent.consent_digest != cast(str, pending["consent_digest"]):
            raise ValueError("Pending approval consent digest linkage is invalid")
        return consent.public_view(tool_call_id=cast(str, pending["tool_call_id"]))

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
        remedy_consent: RemedyConsentRecord | None = None,
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
        if (pending_approval is None) is not (remedy_consent is None):
            raise ValueError(
                "Pending SDK envelope and authoritative consent must persist together"
            )
        if pending_approval is not None and remedy_consent is not None:
            if remedy_consent.recovery_id != recovery_id:
                raise ValueError("Remedy consent recovery ID does not match transition")
            if pending_approval.remedy_id != remedy_consent.remedy_id:
                raise ValueError("Pending approval remedy linkage does not match consent")
            if pending_approval.consent_digest != remedy_consent.consent_digest:
                raise ValueError("Pending approval digest linkage does not match consent")
            remedy_consent.public_view(tool_call_id=pending_approval.tool_call_id)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT id, execution_mode FROM recoveries WHERE id = ?", (recovery_id,)
            ).fetchone()
            if existing is None:
                raise RecoveryNotFoundError("Recovery not found")
            if receipt is not None:
                if not status.terminal:
                    raise ReceiptTransitionError(
                        "Receipt requires a terminal transition"
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
                    int(status.terminal),
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
            if remedy_consent is not None:
                connection.execute(
                    """
                    INSERT INTO remedies (
                        id, recovery_id, terms_json, digest, cost_delta_minor,
                        changed_fields_json, provider_commitments_json, expiry,
                        hard_constraint_satisfied, delegated_authority_satisfied,
                        evidence_json, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        remedy_consent.remedy_id,
                        remedy_consent.recovery_id,
                        remedy_consent.terms.model_dump_json(by_alias=True),
                        remedy_consent.consent_digest,
                        remedy_consent.cost_delta_minor,
                        json.dumps(
                            remedy_consent.changed_fields,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        json.dumps(
                            remedy_consent.provider_commitments,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        remedy_consent.expiry.isoformat(),
                        int(remedy_consent.hard_constraint_satisfied),
                        int(remedy_consent.delegated_authority_satisfied),
                        remedy_consent.evidence.model_dump_json(),
                        now.isoformat(),
                    ),
                )
            if pending_approval is not None and pending_state_json is not None:
                connection.execute(
                    """
                    INSERT INTO pending_approvals (
                        tool_call_id, recovery_id, sdk_version, protocol_version,
                        agent_graph_version, definition_digest, root_trace_id,
                        execution_mode, action_digest, remedy_id, consent_digest,
                        state_json, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        pending_approval.action_digest,
                        pending_approval.remedy_id,
                        pending_approval.consent_digest,
                        pending_state_json,
                        pending_approval.status,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
            if status.terminal:
                terminal_approval_status = {
                    RecoveryStatus.COMPLETED: "completed",
                    RecoveryStatus.CLOSED_WITHOUT_ACTION: "rejected",
                    RecoveryStatus.OUTCOME_UNKNOWN: "outcome_unknown",
                }[status]
                connection.execute(
                    """
                    UPDATE pending_approvals
                    SET status = ?, updated_at = ?
                    WHERE recovery_id = ?
                    """,
                    (terminal_approval_status, now.isoformat(), recovery_id),
                )
        return self.get_recovery(recovery_id)

    def get_recovery(self, recovery_id: str) -> RecoverySnapshot:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM recoveries WHERE id = ?", (recovery_id,)
            ).fetchone()
            pending_view = (
                self._public_pending_view(connection, recovery_id)
                if row is not None
                and cast(str, row["status"])
                == RecoveryStatus.PENDING_APPROVAL.value
                else None
            )
        if row is None:
            raise RecoveryNotFoundError("Recovery not found")
        return self._recovery_from_row(row, pending_approval=pending_view)

    def get_remedy_consent(self, recovery_id: str) -> RemedyConsentRecord:
        """Load authoritative consent and evidence without serializing it publicly."""

        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT remedies.*
                FROM remedies
                JOIN pending_approvals
                  ON pending_approvals.remedy_id = remedies.id
                 AND pending_approvals.recovery_id = remedies.recovery_id
                WHERE remedies.recovery_id = ?
                ORDER BY remedies.created_at DESC
                """,
                (recovery_id,),
            ).fetchall()
        if not rows:
            raise RecoveryNotFoundError("Remedy consent not found")
        if len(rows) != 1:
            raise ValueError("Recovery has ambiguous remedy consent")
        return self._remedy_consent_from_row(rows[0])

    def claim_approval_decision(
        self,
        recovery_id: str,
        request: ApprovalDecisionRequest,
    ) -> ApprovalDecisionClaim:
        """Claim one approval winner while rechecking authoritative consent."""

        now = self._now()
        fingerprint = self._decision_fingerprint(recovery_id, request)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            reused_id = connection.execute(
                "SELECT * FROM approval_decisions WHERE client_decision_id = ?",
                (request.client_decision_id,),
            ).fetchone()
            if reused_id is not None:
                if (
                    cast(str, reused_id["recovery_id"]) != recovery_id
                    or cast(str, reused_id["request_fingerprint"]) != fingerprint
                ):
                    raise ApprovalDecisionError(
                        "decision_id_conflict", recovery_id, status_code=409
                    )
                return self._decision_claim_from_row(reused_id)

            existing = connection.execute(
                "SELECT * FROM approval_decisions WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone()
            if existing is not None:
                raise ApprovalDecisionError(
                    "already_decided", recovery_id, status_code=409
                )

            self._validate_consent_for_decision(
                connection,
                recovery_id,
                request,
                now=now,
            )
            connection.execute(
                """
                INSERT INTO approval_decisions (
                    recovery_id, client_decision_id, action, remedy_id,
                    remedy_digest, tool_call_id, request_fingerprint, status, result_json,
                    claimed_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'claimed', NULL, ?, NULL)
                """,
                (
                    recovery_id,
                    request.client_decision_id,
                    request.action.value,
                    request.remedy_id,
                    request.remedy_digest,
                    request.tool_call_id,
                    fingerprint,
                    now.isoformat(),
                ),
            )
            connection.execute(
                """
                UPDATE recoveries
                SET current_step_summary = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    f"Exact {request.action.value} claimed; outcome pending.",
                    now.isoformat(),
                    recovery_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM approval_decisions WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("Approval decision claim did not persist")
            return self._decision_claim_from_row(row)

    def validate_claimed_decision(
        self,
        claim: ApprovalDecisionClaim,
    ) -> ApprovalDecisionClaim:
        """Repeat every binding check immediately before SDK approval."""

        now = self._now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM approval_decisions WHERE recovery_id = ?",
                (claim.recovery_id,),
            ).fetchone()
            if row is None:
                raise ApprovalDecisionError(
                    "decision_unavailable", claim.recovery_id, status_code=409
                )
            durable_claim = self._decision_claim_from_row(row)
            if (
                durable_claim.request.client_decision_id
                != claim.request.client_decision_id
                or durable_claim.request_fingerprint != claim.request_fingerprint
            ):
                raise ApprovalDecisionError(
                    "decision_id_conflict", claim.recovery_id, status_code=409
                )
            if durable_claim.response is not None:
                return durable_claim
            self._validate_consent_for_decision(
                connection,
                claim.recovery_id,
                claim.request,
                now=now,
            )
            return durable_claim

    def assert_provider_dispatch_authorized(
        self,
        *,
        recovery_id: str,
        tool_call_id: str,
        remedy_digest: str,
    ) -> None:
        """Block direct tool invocation unless the exact durable claim won."""

        now = self._now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM approval_decisions WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone()
            if row is None:
                raise ApprovalDecisionError(
                    "decision_unavailable", recovery_id, status_code=409
                )
            claim = self._decision_claim_from_row(row)
            if (
                claim.request.action is not DecisionAction.APPROVE
                or claim.request.tool_call_id != tool_call_id
                or claim.request.remedy_digest != remedy_digest
            ):
                raise ApprovalDecisionError(
                    "decision_id_conflict", recovery_id, status_code=409
                )
            completed = connection.execute(
                """
                SELECT 1 FROM executions
                WHERE recovery_id = ? AND status = 'completed'
                """,
                (recovery_id,),
            ).fetchone()
            if completed is not None:
                return
            self._validate_consent_for_decision(
                connection,
                recovery_id,
                claim.request,
                now=now,
            )

    def complete_approval_decision(
        self,
        claim: ApprovalDecisionClaim,
        response: ApprovalDecisionResponse,
    ) -> ApprovalDecisionResponse:
        """Store and replay the one safe public response for the winning claim."""

        now = self._now()
        serialized = response.model_dump_json(by_alias=True)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM approval_decisions WHERE recovery_id = ?",
                (claim.recovery_id,),
            ).fetchone()
            if row is None:
                raise ApprovalDecisionError(
                    "decision_unavailable", claim.recovery_id, status_code=409
                )
            durable_claim = self._decision_claim_from_row(row)
            if (
                durable_claim.request.client_decision_id
                != claim.request.client_decision_id
                or durable_claim.request_fingerprint != claim.request_fingerprint
            ):
                raise ApprovalDecisionError(
                    "decision_id_conflict", claim.recovery_id, status_code=409
                )
            if durable_claim.response is not None:
                return durable_claim.response
            connection.execute(
                """
                UPDATE approval_decisions
                SET status = 'completed', result_json = ?, completed_at = ?
                WHERE recovery_id = ? AND status = 'claimed'
                """,
                (serialized, now.isoformat(), claim.recovery_id),
            )
        return response

    def complete_decline_decision(
        self,
        claim: ApprovalDecisionClaim,
    ) -> ApprovalDecisionResponse:
        """Atomically seal an exact rejection without claiming a false cancellation."""

        if claim.request.action is not DecisionAction.DECLINE:
            raise ValueError("Only decline decisions can use the rejection seal")
        now = self._now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM approval_decisions WHERE recovery_id = ?",
                (claim.recovery_id,),
            ).fetchone()
            if row is None:
                raise ApprovalDecisionError(
                    "decision_unavailable", claim.recovery_id, status_code=409
                )
            durable_claim = self._decision_claim_from_row(row)
            if (
                durable_claim.request.client_decision_id
                != claim.request.client_decision_id
                or durable_claim.request_fingerprint != claim.request_fingerprint
                or durable_claim.request.action is not DecisionAction.DECLINE
            ):
                raise ApprovalDecisionError(
                    "decision_id_conflict", claim.recovery_id, status_code=409
                )
            if durable_claim.response is not None:
                return durable_claim.response

            recovery = connection.execute(
                "SELECT status, execution_mode FROM recoveries WHERE id = ?",
                (claim.recovery_id,),
            ).fetchone()
            pending = connection.execute(
                "SELECT status FROM pending_approvals WHERE recovery_id = ?",
                (claim.recovery_id,),
            ).fetchone()
            if recovery is None or pending is None:
                raise ApprovalDecisionError(
                    "decision_unavailable", claim.recovery_id, status_code=409
                )
            execution_rows = connection.execute(
                "SELECT status, provider_execution FROM executions WHERE recovery_id = ?",
                (claim.recovery_id,),
            ).fetchall()
            may_have_begun = (
                bool(execution_rows)
                or cast(str, pending["status"]) != "pending"
                or cast(str, recovery["status"])
                != RecoveryStatus.PENDING_APPROVAL.value
            )
            decision_status: Literal[
                "closed_without_action", "outcome_unknown"
            ] = (
                "outcome_unknown"
                if may_have_begun
                else "closed_without_action"
            )
            terminal_status = RecoveryStatus(decision_status)
            response = ApprovalDecisionResponse(
                action=DecisionAction.DECLINE,
                clientDecisionId=claim.request.client_decision_id,
                recoveryId=claim.recovery_id,
                status=decision_status,
                executionStarted=None if may_have_begun else False,
            )
            if terminal_status is RecoveryStatus.CLOSED_WITHOUT_ACTION:
                provider_result = "Provider dispatch did not begin."
                authorization_source = (
                    "User declined the exact Agents SDK commit_remedy interruption."
                )
                verification_results = [
                    "Exact interruption rejected.",
                    "No replacement remedy selected.",
                    "Execution count is zero.",
                    "Provider dispatch did not begin.",
                    "Temporary permission revoked.",
                    "Cancellation receipt sealed.",
                ]
                summary = "Exact remedy declined before provider dispatch."
            else:
                provider_result = "Provider dispatch may have begun; its outcome is unknown."
                authorization_source = (
                    "Decline arrived after execution or dispatch may have begun; "
                    "cancellation was not claimed."
                )
                verification_results = [
                    "Exact interruption rejected.",
                    "No replacement remedy selected.",
                    "Prior execution or dispatch evidence detected.",
                    "Outcome marked unknown instead of cancelled.",
                    "Temporary permission revoked.",
                    "Uncertain-outcome receipt sealed.",
                ]
                summary = "Decline recorded; prior provider outcome remains unknown."

            receipt = RecoveryReceipt(
                recoveryId=claim.recovery_id,
                executionMode=ExecutionMode(cast(str, recovery["execution_mode"])),
                status=decision_status,
                simulated=True,
                providerExecution=None if may_have_begun else False,
                modelIds=[],
                boundary=(
                    "Deterministic Agents SDK model and demo hotel adapter only; "
                    "no OpenAI model call, real booking, or payment change."
                ),
                providerResult=provider_result,
                authorizationSource=authorization_source,
                verificationResults=verification_results,
            )
            existing_receipt = connection.execute(
                "SELECT 1 FROM receipts WHERE recovery_id = ?",
                (claim.recovery_id,),
            ).fetchone()
            terminal_events = connection.execute(
                "SELECT 1 FROM events WHERE recovery_id = ? AND terminal = 1",
                (claim.recovery_id,),
            ).fetchall()
            if existing_receipt is not None or terminal_events:
                raise ReceiptTransitionError("Existing terminal evidence mismatch")

            response_json = response.model_dump_json(by_alias=True)
            receipt_json = receipt.model_dump_json(by_alias=True)
            terminal_data: dict[str, JsonValue] = {
                "recoveryId": claim.recovery_id,
                "executionMode": receipt.execution_mode.value,
                "executionCount": len(execution_rows),
                "providerExecution": receipt.provider_execution,
                "phase": "Verify & seal",
                "summary": summary,
            }
            next_sequence = cast(
                int,
                connection.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE recovery_id = ?",
                    (claim.recovery_id,),
                ).fetchone()[0],
            )
            connection.execute(
                """
                INSERT INTO receipts (recovery_id, receipt_json, created_at)
                VALUES (?, ?, ?)
                """,
                (claim.recovery_id, receipt_json, now.isoformat()),
            )
            connection.execute(
                """
                INSERT INTO events (
                    recovery_id, seq, type, terminal, data_json, created_at
                ) VALUES (?, ?, ?, 1, ?, ?)
                """,
                (
                    claim.recovery_id,
                    next_sequence,
                    f"recovery.{terminal_status.value}",
                    json.dumps(terminal_data, separators=(",", ":"), sort_keys=True),
                    now.isoformat(),
                ),
            )
            connection.execute(
                """
                UPDATE recoveries
                SET status = ?, current_step = 5,
                    current_step_summary = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    terminal_status.value,
                    summary,
                    now.isoformat(),
                    claim.recovery_id,
                ),
            )
            connection.execute(
                """
                UPDATE pending_approvals
                SET status = ?, updated_at = ?
                WHERE recovery_id = ?
                """,
                (
                    "rejected" if not may_have_begun else "outcome_unknown",
                    now.isoformat(),
                    claim.recovery_id,
                ),
            )
            connection.execute(
                "UPDATE remedies SET status = ? WHERE recovery_id = ?",
                (
                    "declined" if not may_have_begun else "outcome_unknown",
                    claim.recovery_id,
                ),
            )
            cursor = connection.execute(
                """
                UPDATE approval_decisions
                SET status = 'completed', result_json = ?, completed_at = ?
                WHERE recovery_id = ? AND status = 'claimed'
                """,
                (response_json, now.isoformat(), claim.recovery_id),
            )
            if cursor.rowcount != 1:
                raise ApprovalDecisionError(
                    "decision_unavailable", claim.recovery_id, status_code=409
                )
        return response

    def list_claimed_decisions(self) -> list[ApprovalDecisionClaim]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM approval_decisions
                WHERE status = 'claimed'
                ORDER BY claimed_at ASC
                """
            ).fetchall()
        return [self._decision_claim_from_row(row) for row in rows]

    def count_decisions(self, recovery_id: str) -> int:
        with self._lock, self._connect() as connection:
            return cast(
                int,
                connection.execute(
                    "SELECT COUNT(*) FROM approval_decisions WHERE recovery_id = ?",
                    (recovery_id,),
                ).fetchone()[0],
            )

    def count_recoveries(self) -> int:
        with self._lock, self._connect() as connection:
            return cast(
                int,
                connection.execute("SELECT COUNT(*) FROM recoveries").fetchone()[0],
            )

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

    def get_completed_execution(self, recovery_id: str) -> DurableExecution | None:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM executions
                WHERE recovery_id = ? AND status = 'completed'
                ORDER BY created_at ASC
                """,
                (recovery_id,),
            ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise ValueError("Recovery has ambiguous completed executions")
        return self._execution_from_row(rows[0])

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
        if (
            execution.remedy_digest is None
            or receipt.approved_remedy_digest != execution.remedy_digest
        ):
            raise ReceiptTransitionError("Execution receipt approved digest mismatch")
        if not execution.provider_execution or not receipt.provider_execution:
            raise ReceiptTransitionError("Execution receipt provider evidence mismatch")
        provider_result = execution.result_json.get("provider_result")
        if not isinstance(provider_result, str) or receipt.provider_result != provider_result:
            raise ReceiptTransitionError("Execution receipt provider result mismatch")
        now = self._now()
        receipt_json = receipt.model_dump_json(by_alias=True, exclude_none=True)
        terminal_data: dict[str, JsonValue] = {
            "recoveryId": execution.recovery_id,
            "executionMode": receipt.execution_mode.value,
            "providerExecution": execution.provider_execution,
            "approvedRemedyDigest": execution.remedy_digest,
            "phase": "Verify & seal",
            "summary": "Committed demo-provider result finalized after restart.",
        }
        terminal_json = json.dumps(
            terminal_data,
            separators=(",", ":"),
            sort_keys=True,
        )
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
                "SELECT receipt_json FROM receipts WHERE recovery_id = ?",
                (execution.recovery_id,),
            ).fetchone()
            if existing_receipt is not None:
                try:
                    stored_receipt = RecoveryReceipt.model_validate_json(
                        cast(str, existing_receipt["receipt_json"])
                    )
                except (TypeError, ValueError):
                    raise ReceiptTransitionError(
                        "Existing receipt evidence mismatch"
                    ) from None
                if stored_receipt != receipt:
                    raise ReceiptTransitionError("Existing receipt evidence mismatch")

            terminal_events = connection.execute(
                """
                SELECT type, data_json FROM events
                WHERE recovery_id = ? AND terminal = 1
                ORDER BY seq ASC
                """,
                (execution.recovery_id,),
            ).fetchall()
            if len(terminal_events) > 1:
                raise ReceiptTransitionError("Existing terminal evidence mismatch")
            if terminal_events:
                terminal_event = terminal_events[0]
                try:
                    stored_terminal_data = json.loads(
                        cast(str, terminal_event["data_json"])
                    )
                except (TypeError, ValueError):
                    raise ReceiptTransitionError(
                        "Existing terminal evidence mismatch"
                    ) from None
                if (
                    cast(str, terminal_event["type"]) != "recovery.completed"
                    or stored_terminal_data != terminal_data
                ):
                    raise ReceiptTransitionError(
                        "Existing terminal evidence mismatch"
                    )

            if existing_receipt is None:
                connection.execute(
                    """
                    INSERT INTO receipts (recovery_id, receipt_json, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (execution.recovery_id, receipt_json, now.isoformat()),
                )
                changed = True

            if not terminal_events:
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
                        terminal_json,
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
            connection.execute(
                """
                UPDATE remedies
                SET status = 'approved'
                WHERE recovery_id = ?
                """,
                (execution.recovery_id,),
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
