"""SQLite-backed recovery snapshots, ordered events, and replay receipts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, RLock
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from pydantic import JsonValue

from server.agents.schemas import CommitRemedyArguments
from server.agents.versioning import remedy_action_digest
from server.config import (
    DEFAULT_MAX_RECOVERY_CREATIONS_GLOBAL,
    DEFAULT_MAX_RECOVERY_CREATIONS_PER_IP,
    DEFAULT_MAX_RECOVERY_CREATIONS_PER_SESSION,
    MAX_RECOVERY_CREATIONS_GLOBAL,
    MAX_RECOVERY_CREATIONS_PER_IP,
    MAX_RECOVERY_CREATIONS_PER_SESSION,
)
from server.digest import remedy_consent_digest
from server.models import (
    OPENAI_LIVE_BOUNDARY,
    QUOTA_SDK_AUTHORIZATION_SOURCE,
    QUOTA_SDK_INITIAL_SUMMARY,
    QUOTA_SDK_PROVIDER_RESULT,
    QUOTA_SDK_STUB_BOUNDARY,
    QUOTA_SDK_UNKNOWN_AUTHORIZATION_SOURCE,
    QUOTA_SDK_UNKNOWN_PROVIDER_RESULT,
    QUOTA_SDK_UNKNOWN_SUMMARY,
    QUOTA_SDK_UNKNOWN_VERIFICATION_RESULTS,
    QUOTA_SDK_VERIFICATION_RESULTS,
    SDK_STUB_BOUNDARY,
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    ClaimedDecisionView,
    DecisionAction,
    ExecutionMode,
    HotelRemedyTerms,
    PendingApprovalView,
    RecoveryEvent,
    RecoveryReceipt,
    RecoverySnapshot,
    RecoveryStatus,
    ReplayScenarioDefinition,
    ScenarioId,
)
from server.policy import (
    DETERMINISTIC_HOTEL_AUTHORITY,
    evaluate_hotel_policy,
    exact_hotel_terms,
)
from server.providers.quota_simulator import (
    DETERMINISTIC_QUOTA_REQUEST,
    DETERMINISTIC_QUOTA_RESULT,
    QUOTA_TOOL_CALL_ID,
    QuotaRecoveryResult,
    quota_request_digest,
)
from server.trace_ids import is_valid_live_trace_id, is_valid_qa_trace_id

MAX_STORE_EXPIRY_BATCH_SIZE = 1_000
RECOVERY_CREATION_STALE_AFTER = timedelta(minutes=5)
EXPIRATION_SUMMARY = "Consent expired without a decision; no provider dispatch was authorized."
EXPIRATION_VERIFICATION_RESULTS = (
    "Human consent requested.",
    "Consent window expired without an approval decision.",
    "No approval decision claim was recorded.",
    "Execution count is zero.",
    "Provider dispatch did not begin.",
    "Temporary permission revoked.",
    "Expiration receipt sealed.",
)
CLAIM_EXPIRY_CLOSED_SUMMARY = "Claimed decline expired before provider dispatch."
CLAIM_EXPIRY_UNKNOWN_SUMMARY = "Claimed decision expired; provider outcome remains unknown."
CLAIM_EXPIRY_CLOSED_VERIFICATION_RESULTS = (
    "Human consent requested.",
    "Exact decline decision claimed before consent expiry.",
    "Execution count is zero.",
    "Provider dispatch did not begin.",
    "Temporary permission revoked.",
    "Claim-expiry receipt sealed.",
)
CLAIM_EXPIRY_UNKNOWN_VERIFICATION_RESULTS = (
    "Human consent requested.",
    "An exact decision was claimed before consent expiry.",
    "Durable evidence cannot prove that provider dispatch did not begin.",
    "Cancellation and zero execution are not claimed.",
    "Temporary permission revoked.",
    "Uncertain claim-expiry receipt sealed.",
)
CLAIM_EXPIRY_CLOSED_PROVIDER_RESULT = "Provider dispatch did not begin."
CLAIM_EXPIRY_UNKNOWN_PROVIDER_RESULT = CLAIM_EXPIRY_UNKNOWN_SUMMARY
CLAIM_EXPIRY_CLOSED_AUTHORIZATION_SOURCE = "Exact decline claim expired before provider dispatch."
CLAIM_EXPIRY_UNKNOWN_AUTHORIZATION_SOURCE = (
    "A durable exact decision was claimed before consent expiry; no provider outcome is asserted."
)
QUOTA_EXECUTION_RESULT_RECORDED = "result_recorded"
QUOTA_EXECUTION_INVARIANT_FAILED = "sdk_invariant_failed"
LEGACY_QUOTA_COMPLETION_DOMAIN = "backchannel:legacy-quota-completion:v1"

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
    model_ids_json TEXT NOT NULL DEFAULT '[]',
    root_trace_id TEXT,
    model_call INTEGER NOT NULL DEFAULT 0 CHECK (model_call IN (0, 1)),
    sdk_version TEXT,
    protocol_version TEXT,
    agent_graph_version TEXT,
    definition_digest TEXT,
    quota_execution_contract INTEGER NOT NULL DEFAULT 0 CHECK (
        quota_execution_contract IN (0, 1)
    ),
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
    model_ids_json TEXT NOT NULL,
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
    recovery_id TEXT NOT NULL,
    category TEXT NOT NULL,
    amount INTEGER NOT NULL DEFAULT 0,
    recorded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS usage_ledger_category_time_idx
ON usage_ledger(category, recorded_at);

CREATE TABLE IF NOT EXISTS demo_sessions (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recovery_access (
    recovery_id TEXT NOT NULL REFERENCES recoveries(id) ON DELETE CASCADE,
    session_key TEXT NOT NULL,
    PRIMARY KEY (recovery_id, session_key)
);

CREATE INDEX IF NOT EXISTS recovery_access_session_idx
ON recovery_access(session_key);

CREATE TABLE IF NOT EXISTS live_admissions (
    recovery_id TEXT PRIMARY KEY,
    ip_key TEXT NOT NULL,
    session_key TEXT NOT NULL,
    budget_units INTEGER NOT NULL CHECK (budget_units > 0),
    admitted_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    released_at TEXT
);

CREATE INDEX IF NOT EXISTS live_admissions_ip_time_idx
ON live_admissions(ip_key, admitted_at);

CREATE INDEX IF NOT EXISTS live_admissions_session_time_idx
ON live_admissions(session_key, admitted_at);

CREATE TABLE IF NOT EXISTS recovery_creations (
    request_key TEXT PRIMARY KEY,
    request_fingerprint TEXT NOT NULL,
    session_key TEXT NOT NULL CHECK (length(session_key) = 64),
    ip_key TEXT NOT NULL CHECK (length(ip_key) = 64),
    scenario_id TEXT NOT NULL CHECK (scenario_id IN ('hotel', 'api-quota')),
    execution_mode TEXT NOT NULL CHECK (
        execution_mode IN ('openai_live', 'sdk_stub', 'replay_fixture')
    ),
    recovery_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('reserved', 'started', 'ready', 'unknown')
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS recovery_creations_status_updated_idx
ON recovery_creations(status, updated_at);

CREATE TABLE IF NOT EXISTS expiry_scan_state (
    candidate_kind TEXT PRIMARY KEY CHECK (
        candidate_kind IN ('oldest', 'claimed', 'untouched')
    ),
    last_expiry TEXT NOT NULL,
    last_recovery_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS readiness_probe (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    generation INTEGER NOT NULL CHECK (generation IN (0, 1))
);

INSERT OR IGNORE INTO readiness_probe (id, generation) VALUES (1, 0);
"""


class RecoveryNotFoundError(LookupError):
    """Raised when a durable recovery or receipt does not exist."""


class PublicRecoveryAccessRevokedError(RecoveryNotFoundError):
    """Raised when an admitted public reader no longer has recovery access."""


class ReceiptTransitionError(ValueError):
    """Raised when receipt provenance does not match its terminal transition."""


class ReplayIntegrityError(RuntimeError):
    """Raised when a canonical replay row no longer matches its definition."""


class PublicEvidenceIntegrityError(RuntimeError):
    """Raised when public snapshot, event, and receipt evidence disagree."""


class ExecutionConflictError(ValueError):
    """Raised when a durable idempotency key is reused for different terms."""


class ResetCreationPendingError(RuntimeError):
    """Raised when reset would invalidate this session's unresolved start owner."""


RecoveryCreationDisposition = Literal[
    "owner",
    "ready",
    "pending",
    "unknown",
    "conflict",
    "capacity",
]


@dataclass(frozen=True, slots=True)
class RecoveryCreationClaim:
    """One session-scoped durable ownership decision for recovery creation."""

    disposition: RecoveryCreationDisposition
    recovery_id: str
    scenario_id: ScenarioId
    execution_mode: ExecutionMode


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
    model_ids: tuple[str, ...]
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
class RecoveryProvenance:
    """Durable mode and SDK markers used by snapshots and crash reconciliation."""

    recovery_id: str
    execution_mode: ExecutionMode
    model_ids: tuple[str, ...]
    root_trace_id: str | None
    model_call: bool
    sdk_version: str | None
    protocol_version: str | None
    agent_graph_version: str | None
    definition_digest: str | None


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
        self.require_policy_eligible()
        return PendingApprovalView(
            remedyId=self.remedy_id,
            remedyDigest=self.consent_digest,
            terms=self.terms,
            costDeltaMinor=self.cost_delta_minor,
            changedFields=list(self.changed_fields),
            providerCommitments=list(self.provider_commitments),
            expiry=self.expiry,
            hardConstraintSatisfied=True,
            delegatedAuthoritySatisfied=True,
            toolCallId=tool_call_id,
            executionStarted=False,
        )

    def require_policy_eligible(self) -> None:
        """Reject consent unless its complete binding and current policy allow it."""

        policy = evaluate_hotel_policy(
            self.evidence,
            DETERMINISTIC_HOTEL_AUTHORITY,
        )
        recomputed_digest = remedy_consent_digest(
            {
                "recoveryId": self.recovery_id,
                "remedyId": self.remedy_id,
                "terms": self.terms.model_dump(mode="json", by_alias=True),
                "costDeltaMinor": self.cost_delta_minor,
                "changedFields": list(self.changed_fields),
                "providerCommitments": list(self.provider_commitments),
                "expiry": self.expiry,
            }
        )
        if (
            self.evidence.remedy.remedy_id != self.remedy_id
            or exact_hotel_terms(self.evidence) != self.terms
            or self.evidence.remedy.cost_delta_minor != self.cost_delta_minor
            or tuple(sorted(self.evidence.remedy.changed_fields)) != self.changed_fields
            or tuple(sorted(self.evidence.remedy.provider_commitments)) != self.provider_commitments
            or recomputed_digest != self.consent_digest
            or self.hard_constraint_satisfied is not True
            or self.delegated_authority_satisfied is not True
            or policy.hard_constraint_satisfied is not True
            or policy.delegated_authority_satisfied is not True
        ):
            raise ValueError("Remedy consent is not policy eligible")


@dataclass(frozen=True, slots=True)
class _HotelApprovalEvidence:
    """Canonical immutable foundation shared by active and expired hotel reads."""

    pending: PendingApprovalEnvelope
    consent: RemedyConsentRecord
    pending_row: sqlite3.Row
    remedy_row: sqlite3.Row
    claim: ApprovalDecisionClaim | None
    decision_row: sqlite3.Row | None
    execution_rows: tuple[sqlite3.Row, ...]
    approval_requested_at: datetime
    pending_updated_at: datetime
    claimed_at: datetime | None


class SQLiteStore:
    """Small connection-per-transaction store safe for FastAPI worker threads."""

    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)
        self._lock = RLock()
        self._closed = False
        self._closed_event = Event()
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            self._migrate_task2_recovery_constraints(connection)
            self._migrate_task4_remedies(connection)
            self._migrate_task3_pending_approvals(connection)
            self._migrate_task7_recovery_provenance(connection)
            self._migrate_quota_execution_contract(connection)
            self._migrate_task7_receipts(connection)
            self._migrate_task3_executions(connection)
            self._migrate_task6_approval_decisions(connection)
            self._migrate_task8_usage_ledger(connection)
            self._migrate_stateless_demo_sessions(connection)
            self._migrate_recovery_access(connection)
            self._migrate_recovery_creations(connection)
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
    def _migrate_stateless_demo_sessions(connection: sqlite3.Connection) -> None:
        """Clear legacy server-side sessions now replaced by signed cookies."""

        connection.execute("DELETE FROM demo_sessions")

    @staticmethod
    def _migrate_recovery_creations(connection: sqlite3.Connection) -> None:
        """Transactionally upgrade creation claims to the canonical IP schema."""

        savepoint = "recovery_creations_schema_migration"
        connection.execute(f"SAVEPOINT {savepoint}")
        try:
            columns = {
                cast(str, row["name"])
                for row in connection.execute("PRAGMA table_info(recovery_creations)").fetchall()
            }
            legacy_expiry = (datetime.now(UTC) + timedelta(days=7)).isoformat()
            if "session_key" not in columns:
                connection.execute("ALTER TABLE recovery_creations ADD COLUMN session_key TEXT")
                connection.execute(
                    """
                    UPDATE recovery_creations
                    SET session_key = request_key, status = 'unknown'
                    WHERE session_key IS NULL
                    """
                )
            if "expires_at" not in columns:
                connection.execute("ALTER TABLE recovery_creations ADD COLUMN expires_at TEXT")
                connection.execute(
                    """
                    UPDATE recovery_creations
                    SET expires_at = ?, status = 'unknown'
                    WHERE expires_at IS NULL
                    """,
                    (legacy_expiry,),
                )

            ip_column = next(
                (
                    row
                    for row in connection.execute(
                        "PRAGMA table_xinfo(recovery_creations)"
                    ).fetchall()
                    if cast(str, row["name"]) == "ip_key"
                ),
                None,
            )
            schema_row = connection.execute(
                """
                SELECT sql
                FROM sqlite_master
                WHERE type = 'table' AND name = 'recovery_creations'
                """
            ).fetchone()
            normalized_schema = (
                "".join(cast(str, schema_row["sql"]).lower().split())
                if schema_row is not None and schema_row["sql"] is not None
                else ""
            )
            canonical_ip_schema = (
                ip_column is not None
                and cast(str, ip_column["type"]).upper() == "TEXT"
                and cast(int, ip_column["notnull"]) == 1
                and cast(int, ip_column["hidden"]) == 0
                and ("ip_keytextnotnullcheck(length(ip_key)=64)" in normalized_schema)
            )
            if not canonical_ip_schema:
                invalid_session = connection.execute(
                    """
                    SELECT 1
                    FROM recovery_creations
                    WHERE typeof(session_key) <> 'text'
                       OR length(session_key) <> 64
                       OR session_key GLOB '*[^0-9a-f]*'
                    LIMIT 1
                    """
                ).fetchone()
                if invalid_session is not None:
                    raise RuntimeError("Unsupported recovery creation session correlation")
                connection.execute("DROP TABLE IF EXISTS recovery_creations_ip_migration")
                connection.execute(
                    """
                    CREATE TABLE recovery_creations_ip_migration (
                        request_key TEXT PRIMARY KEY,
                        request_fingerprint TEXT NOT NULL,
                        session_key TEXT NOT NULL CHECK (length(session_key) = 64),
                        ip_key TEXT NOT NULL CHECK (length(ip_key) = 64),
                        scenario_id TEXT NOT NULL CHECK (
                            scenario_id IN ('hotel', 'api-quota')
                        ),
                        execution_mode TEXT NOT NULL CHECK (
                            execution_mode IN (
                                'openai_live', 'sdk_stub', 'replay_fixture'
                            )
                        ),
                        recovery_id TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (
                            status IN ('reserved', 'started', 'ready', 'unknown')
                        ),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL
                    )
                    """
                )
                ip_key_expression = (
                    """
                    CASE
                        WHEN typeof(ip_key) = 'text'
                         AND length(ip_key) = 64
                         AND ip_key NOT GLOB '*[^0-9a-f]*'
                        THEN ip_key
                        ELSE session_key
                    END
                    """
                    if ip_column is not None
                    else "session_key"
                )
                connection.execute(
                    f"""
                    INSERT INTO recovery_creations_ip_migration (
                        request_key, request_fingerprint, session_key, ip_key,
                        scenario_id, execution_mode, recovery_id, status,
                        created_at, updated_at, expires_at
                    )
                    SELECT
                        request_key, request_fingerprint, session_key,
                        {ip_key_expression},
                        scenario_id, execution_mode, recovery_id, status,
                        created_at, updated_at, expires_at
                    FROM recovery_creations
                    """
                )
                connection.execute("DROP TABLE recovery_creations")
                connection.execute(
                    "ALTER TABLE recovery_creations_ip_migration RENAME TO recovery_creations"
                )

            connection.execute(
                "CREATE INDEX IF NOT EXISTS "
                "recovery_creations_status_updated_idx "
                "ON recovery_creations(status, updated_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS recovery_creations_session_idx "
                "ON recovery_creations(session_key)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS "
                "recovery_creations_ip_expiry_idx "
                "ON recovery_creations(ip_key, expires_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS recovery_creations_expiry_idx "
                "ON recovery_creations(expires_at)"
            )
        except BaseException:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        else:
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")

    @staticmethod
    def _migrate_recovery_access(connection: sqlite3.Connection) -> None:
        """Require the exact opaque, fail-closed recovery ownership schema."""

        columns = connection.execute("PRAGMA table_info(recovery_access)").fetchall()
        expected = [("recovery_id", 1), ("session_key", 2)]
        actual = [(cast(str, row["name"]), cast(int, row["pk"])) for row in columns]
        if actual != expected:
            raise RuntimeError("Unsupported recovery access schema")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS recovery_access_session_idx ON recovery_access(session_key)"
        )
        foreign_keys = connection.execute("PRAGMA foreign_key_list(recovery_access)").fetchall()
        if len(foreign_keys) != 1:
            raise RuntimeError("Unsupported recovery access foreign key")
        foreign_key = foreign_keys[0]
        if (
            cast(str, foreign_key["table"]),
            cast(str, foreign_key["from"]),
            cast(str, foreign_key["to"]),
            cast(str, foreign_key["on_delete"]).upper(),
        ) != ("recoveries", "recovery_id", "id", "CASCADE"):
            raise RuntimeError("Unsupported recovery access foreign key")
        session_index = connection.execute(
            "PRAGMA index_info(recovery_access_session_idx)"
        ).fetchall()
        if [cast(str, row["name"]) for row in session_index] != ["session_key"]:
            raise RuntimeError("Unsupported recovery access session index")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("Recovery access migration violated foreign keys")

    @staticmethod
    def _migrate_task8_usage_ledger(connection: sqlite3.Connection) -> None:
        """Detach aggregate demo usage from recovery-detail retention."""

        foreign_keys = connection.execute("PRAGMA foreign_key_list(usage_ledger)").fetchall()
        if not foreign_keys:
            return

        connection.commit()
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DROP TABLE IF EXISTS usage_ledger_task8")
            connection.execute(
                """
                CREATE TABLE usage_ledger_task8 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recovery_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    amount INTEGER NOT NULL DEFAULT 0,
                    recorded_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO usage_ledger_task8 (
                    id, recovery_id, category, amount, recorded_at
                )
                SELECT id, recovery_id, category, amount, recorded_at
                FROM usage_ledger
                """
            )
            connection.execute("DROP TABLE usage_ledger")
            connection.execute("ALTER TABLE usage_ledger_task8 RENAME TO usage_ledger")
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS usage_ledger_category_time_idx
                ON usage_ledger(category, recorded_at)
                """
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _migrate_task7_recovery_provenance(connection: sqlite3.Connection) -> None:
        """Add public mode-bound provenance without losing earlier recoveries."""

        columns = {
            cast(str, row["name"])
            for row in connection.execute("PRAGMA table_info(recoveries)").fetchall()
        }
        if "model_ids_json" not in columns:
            connection.execute(
                "ALTER TABLE recoveries ADD COLUMN model_ids_json TEXT NOT NULL DEFAULT '[]'"
            )
        if "root_trace_id" not in columns:
            connection.execute("ALTER TABLE recoveries ADD COLUMN root_trace_id TEXT")
        additions = {
            "model_call": "INTEGER NOT NULL DEFAULT 0",
            "sdk_version": "TEXT",
            "protocol_version": "TEXT",
            "agent_graph_version": "TEXT",
            "definition_digest": "TEXT",
        }
        for column, column_type in additions.items():
            if column not in columns:
                connection.execute(f"ALTER TABLE recoveries ADD COLUMN {column} {column_type}")
        connection.execute(
            """
            UPDATE recoveries
            SET root_trace_id = (
                SELECT pending_approvals.root_trace_id
                FROM pending_approvals
                WHERE pending_approvals.recovery_id = recoveries.id
            )
            WHERE root_trace_id IS NULL
              AND execution_mode = 'sdk_stub'
              AND EXISTS (
                  SELECT 1 FROM pending_approvals
                  WHERE pending_approvals.recovery_id = recoveries.id
              )
            """
        )
        connection.execute(
            """
            UPDATE recoveries
            SET sdk_version = (
                    SELECT pending_approvals.sdk_version FROM pending_approvals
                    WHERE pending_approvals.recovery_id = recoveries.id
                ),
                protocol_version = (
                    SELECT pending_approvals.protocol_version FROM pending_approvals
                    WHERE pending_approvals.recovery_id = recoveries.id
                ),
                agent_graph_version = (
                    SELECT pending_approvals.agent_graph_version FROM pending_approvals
                    WHERE pending_approvals.recovery_id = recoveries.id
                ),
                definition_digest = (
                    SELECT pending_approvals.definition_digest FROM pending_approvals
                    WHERE pending_approvals.recovery_id = recoveries.id
                )
            WHERE EXISTS (
                SELECT 1 FROM pending_approvals
                WHERE pending_approvals.recovery_id = recoveries.id
            )
            """
        )
        connection.execute(
            """
            UPDATE recoveries
            SET root_trace_id = 'qa_trace_' || lower(hex(randomblob(16)))
            WHERE root_trace_id IS NULL AND execution_mode = 'sdk_stub'
            """
        )

    @staticmethod
    def _legacy_quota_recovery_fingerprint(row: sqlite3.Row) -> str:
        """Bind legacy acceptance to the exact row observed during schema migration."""

        fields = (
            "id",
            "scenario_id",
            "execution_mode",
            "status",
            "current_step",
            "current_step_summary",
            "model_ids_json",
            "root_trace_id",
            "model_call",
            "sdk_version",
            "protocol_version",
            "agent_graph_version",
            "definition_digest",
            "created_at",
            "updated_at",
        )
        payload = {
            "domain": LEGACY_QUOTA_COMPLETION_DOMAIN,
            "recovery": {field: row[field] for field in fields},
        }
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
        ).hexdigest()

    @classmethod
    def _migrate_quota_execution_contract(
        cls,
        connection: sqlite3.Connection,
    ) -> None:
        """Mark rows created under the restart-durable quota contract."""

        columns = {
            cast(str, row["name"])
            for row in connection.execute("PRAGMA table_info(recoveries)").fetchall()
        }
        provenance_exists = (
            connection.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type = 'table' AND name = 'quota_legacy_completions'
                """
            ).fetchone()
            is not None
        )
        register_legacy_completions = (
            "quota_execution_contract" not in columns and not provenance_exists
        )
        savepoint = "quota_execution_contract_migration"
        connection.execute(f"SAVEPOINT {savepoint}")
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS quota_legacy_completions (
                    recovery_id TEXT NOT NULL PRIMARY KEY
                        REFERENCES recoveries(id) ON DELETE CASCADE,
                    recovery_fingerprint TEXT NOT NULL CHECK (
                        length(recovery_fingerprint) = 64
                        AND recovery_fingerprint NOT GLOB '*[^0-9a-f]*'
                    ),
                    recorded_at TEXT NOT NULL
                )
                """
            )
            if register_legacy_completions:
                legacy_rows = connection.execute(
                    """
                    SELECT recoveries.*
                    FROM recoveries
                    WHERE scenario_id = 'api-quota'
                      AND execution_mode = 'sdk_stub'
                      AND status = 'completed'
                      AND NOT EXISTS (
                        SELECT 1
                        FROM executions
                        WHERE executions.recovery_id = recoveries.id
                      )
                    ORDER BY id ASC
                    """
                ).fetchall()
                recorded_at = datetime.now(UTC).isoformat()
                for row in legacy_rows:
                    connection.execute(
                        """
                        INSERT INTO quota_legacy_completions (
                            recovery_id, recovery_fingerprint, recorded_at
                        ) VALUES (?, ?, ?)
                        """,
                        (
                            cast(str, row["id"]),
                            cls._legacy_quota_recovery_fingerprint(row),
                            recorded_at,
                        ),
                    )
            if "quota_execution_contract" not in columns:
                connection.execute(
                    """
                    ALTER TABLE recoveries
                    ADD COLUMN quota_execution_contract INTEGER NOT NULL
                        DEFAULT 0 CHECK (quota_execution_contract IN (0, 1))
                    """,
                )
        except BaseException:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        else:
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")

    @staticmethod
    def _migrate_task7_receipts(connection: sqlite3.Connection) -> None:
        """Enrich legacy receipts only from durable, mode-compatible provenance."""

        rows = connection.execute(
            """
            SELECT
                receipts.recovery_id AS recovery_id,
                receipts.receipt_json AS receipt_json,
                recoveries.scenario_id AS scenario_id,
                recoveries.execution_mode AS execution_mode,
                recoveries.model_ids_json AS model_ids_json,
                recoveries.root_trace_id AS root_trace_id,
                recoveries.model_call AS model_call,
                recoveries.sdk_version AS sdk_version,
                recoveries.protocol_version AS protocol_version,
                recoveries.agent_graph_version AS agent_graph_version,
                recoveries.definition_digest AS definition_digest
            FROM receipts
            JOIN recoveries ON recoveries.id = receipts.recovery_id
            """
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(cast(str, row["receipt_json"]))
                mode = ExecutionMode(cast(str, row["execution_mode"]))
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict) or payload.get("executionMode") != mode.value:
                continue
            task7_fields = {
                "modelCall",
                "rootTraceId",
                "sdkVersion",
                "protocolVersion",
                "agentGraphVersion",
                "definitionDigest",
            }
            present_task7_fields = task7_fields.intersection(payload)
            if present_task7_fields == task7_fields:
                # Current receipts are immutable evidence, not migration input.
                continue
            if present_task7_fields:
                # A partial provenance shape is ambiguous and must fail closed.
                continue

            if mode is ExecutionMode.REPLAY_FIXTURE:
                payload.update(
                    {
                        "modelCall": False,
                        "modelIds": [],
                        "rootTraceId": None,
                        "sdkVersion": None,
                        "protocolVersion": None,
                        "agentGraphVersion": None,
                        "definitionDigest": None,
                    }
                )
            else:
                try:
                    model_ids = json.loads(cast(str, row["model_ids_json"]))
                except (TypeError, ValueError):
                    continue
                if not isinstance(model_ids, list) or not all(
                    isinstance(model_id, str) for model_id in model_ids
                ):
                    continue
                root_trace_id = cast(str | None, row["root_trace_id"])
                markers = (
                    cast(str | None, row["sdk_version"]),
                    cast(str | None, row["protocol_version"]),
                    cast(str | None, row["agent_graph_version"]),
                    cast(str | None, row["definition_digest"]),
                )
                if any(marker is None for marker in markers):
                    continue
                model_call = bool(cast(int, row["model_call"]))
                if mode is ExecutionMode.SDK_STUB:
                    if (
                        model_call
                        or model_ids
                        or not (
                            isinstance(root_trace_id, str) and is_valid_qa_trace_id(root_trace_id)
                        )
                    ):
                        continue
                    boundary = (
                        QUOTA_SDK_STUB_BOUNDARY
                        if cast(str, row["scenario_id"]) == ScenarioId.API_QUOTA.value
                        else SDK_STUB_BOUNDARY
                    )
                else:
                    if (
                        not model_call
                        or model_ids != ["gpt-5.6-luna", "gpt-5.6-terra"]
                        or not (
                            isinstance(root_trace_id, str) and is_valid_live_trace_id(root_trace_id)
                        )
                    ):
                        # Never invent live model-call or trace evidence.
                        continue
                    boundary = OPENAI_LIVE_BOUNDARY
                payload.update(
                    {
                        "modelCall": model_call,
                        "modelIds": model_ids,
                        "rootTraceId": root_trace_id,
                        "sdkVersion": markers[0],
                        "protocolVersion": markers[1],
                        "agentGraphVersion": markers[2],
                        "definitionDigest": markers[3],
                        "boundary": boundary,
                    }
                )
            try:
                receipt = RecoveryReceipt.model_validate(payload)
            except (TypeError, ValueError):
                continue
            connection.execute(
                "UPDATE receipts SET receipt_json = ? WHERE recovery_id = ?",
                (
                    receipt.model_dump_json(by_alias=True),
                    cast(str, row["recovery_id"]),
                ),
            )

    @staticmethod
    def _migrate_task3_pending_approvals(connection: sqlite3.Connection) -> None:
        """Rebuild every legacy envelope table to the exact Task 5 contract."""

        columns = {
            cast(str, row["name"])
            for row in connection.execute("PRAGMA table_info(pending_approvals)").fetchall()
        }
        task7_columns = {
            "tool_call_id",
            "recovery_id",
            "sdk_version",
            "protocol_version",
            "agent_graph_version",
            "definition_digest",
            "root_trace_id",
            "model_ids_json",
            "execution_mode",
            "action_digest",
            "remedy_id",
            "consent_digest",
            "state_json",
            "status",
            "created_at",
            "updated_at",
        }
        if columns == task7_columns:
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
                    model_ids_json TEXT NOT NULL,
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
                    model_ids_json, execution_mode, action_digest, remedy_id, consent_digest,
                    state_json, status,
                    created_at, updated_at
                )
                SELECT
                    tool_call_id,
                    recovery_id,
                    {legacy_value("sdk_version")},
                    {legacy_value("protocol_version")},
                    {legacy_value("agent_graph_version")},
                    {legacy_value("definition_digest")},
                    {legacy_value("root_trace_id")},
                    {legacy_value("model_ids_json", "'[]'")},
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
            connection.execute("ALTER TABLE pending_approvals_task5 RENAME TO pending_approvals")
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
                connection.execute(f"ALTER TABLE remedies ADD COLUMN {column} {column_type}")

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
            for row in connection.execute("PRAGMA table_info(approval_decisions)").fetchall()
        }
        if "action" in columns:
            return

        connection.commit()
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
            locked_columns = {
                cast(str, row["name"])
                for row in connection.execute("PRAGMA table_info(approval_decisions)").fetchall()
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
            connection.execute("ALTER TABLE approval_decisions_task6 RENAME TO approval_decisions")
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

    @contextmanager
    def _expiry_transaction(
        self,
        existing_connection: sqlite3.Connection | None,
    ) -> Iterator[sqlite3.Connection]:
        """Reuse an authoritative transaction or own one for batch cleanup."""

        if existing_connection is not None:
            yield existing_connection
            return
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            yield connection

    def _connect_readiness(self, *, timeout_seconds: float) -> sqlite3.Connection:
        """Open a dedicated bounded connection for the public readiness probe."""

        if self.closed:
            raise RuntimeError("SQLiteStore is closed")
        connection = sqlite3.connect(
            self._database_path,
            timeout=timeout_seconds,
        )
        if self.closed:
            connection.close()
            raise RuntimeError("SQLiteStore is closed")
        connection.row_factory = sqlite3.Row
        return connection

    @property
    def closed(self) -> bool:
        """Return lock-free lifecycle state for bounded health probes."""

        return self._closed_event.is_set()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _require_opaque_session_key(session_key: str) -> None:
        if len(session_key) != 64 or any(
            character not in "0123456789abcdef" for character in session_key
        ):
            raise ValueError("Session key must be an opaque SHA-256 correlation value")

    @classmethod
    def _bind_recovery_access(
        cls,
        connection: sqlite3.Connection,
        *,
        recovery_id: str,
        session_key: str | None,
    ) -> None:
        if session_key is None:
            return
        cls._require_opaque_session_key(session_key)
        connection.execute(
            """
            INSERT INTO recovery_access (recovery_id, session_key)
            VALUES (?, ?)
            ON CONFLICT(recovery_id, session_key) DO NOTHING
            """,
            (recovery_id, session_key),
        )

    @staticmethod
    def _recovery_from_row(
        row: sqlite3.Row,
        *,
        pending_approval: PendingApprovalView | None = None,
        claimed_decision: ClaimedDecisionView | None = None,
    ) -> RecoverySnapshot:
        parsed_model_ids = json.loads(cast(str, row["model_ids_json"]))
        if not isinstance(parsed_model_ids, list) or not all(
            isinstance(model_id, str) for model_id in parsed_model_ids
        ):
            raise ValueError("Recovery model IDs must be a JSON string array")
        return RecoverySnapshot(
            recoveryId=cast(str, row["id"]),
            scenarioId=ScenarioId(cast(str, row["scenario_id"])),
            executionMode=ExecutionMode(cast(str, row["execution_mode"])),
            modelIds=parsed_model_ids,
            rootTraceId=cast(str | None, row["root_trace_id"]),
            status=RecoveryStatus(cast(str, row["status"])),
            currentStep=cast(int, row["current_step"]),
            currentStepSummary=cast(str, row["current_step_summary"]),
            createdAt=datetime.fromisoformat(cast(str, row["created_at"])),
            updatedAt=datetime.fromisoformat(cast(str, row["updated_at"])),
            pendingApproval=pending_approval,
            claimedDecision=claimed_decision,
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
        model_ids = json.loads(cast(str, row["model_ids_json"]))
        if not isinstance(model_ids, list) or not all(
            isinstance(model_id, str) for model_id in model_ids
        ):
            raise ValueError("Pending approval model IDs must be a string array")
        return PendingApprovalEnvelope(
            tool_call_id=cast(str, row["tool_call_id"]),
            recovery_id=cast(str, row["recovery_id"]),
            sdk_version=cast(str, row["sdk_version"]),
            protocol_version=cast(str, row["protocol_version"]),
            agent_graph_version=cast(str, row["agent_graph_version"]),
            definition_digest=cast(str, row["definition_digest"]),
            root_trace_id=cast(str, row["root_trace_id"]),
            model_ids=tuple(model_ids),
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
        provider_commitments = json.loads(cast(str, row["provider_commitments_json"]))
        if not isinstance(changed_fields, list) or not all(
            isinstance(item, str) for item in changed_fields
        ):
            raise ValueError("Stored changed fields must be a string array")
        if not isinstance(provider_commitments, list) or not all(
            isinstance(item, str) for item in provider_commitments
        ):
            raise ValueError("Stored provider commitments must be a string array")

        def stored_policy_flag(field: str) -> bool:
            value = row[field]
            if type(value) is not int or value not in (0, 1):
                raise ValueError(f"Stored {field} must be the integer 0 or 1")
            return value == 1

        return RemedyConsentRecord(
            remedy_id=cast(str, row["id"]),
            recovery_id=cast(str, row["recovery_id"]),
            terms=HotelRemedyTerms.model_validate_json(cast(str, row["terms_json"])),
            cost_delta_minor=cast(int, row["cost_delta_minor"]),
            changed_fields=tuple(changed_fields),
            provider_commitments=tuple(provider_commitments),
            expiry=datetime.fromisoformat(cast(str, row["expiry"])),
            consent_digest=cast(str, row["digest"]),
            hard_constraint_satisfied=stored_policy_flag("hard_constraint_satisfied"),
            delegated_authority_satisfied=stored_policy_flag("delegated_authority_satisfied"),
            evidence=CommitRemedyArguments.model_validate_json(cast(str, row["evidence_json"])),
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
    def _require_pending_consent_binding(
        pending: PendingApprovalEnvelope,
        consent: RemedyConsentRecord,
    ) -> None:
        consent.require_policy_eligible()
        if (
            pending.recovery_id != consent.recovery_id
            or pending.remedy_id != consent.remedy_id
            or pending.consent_digest != consent.consent_digest
            or pending.action_digest != remedy_action_digest(consent.evidence)
        ):
            raise ValueError("Pending approval binding does not match consent")

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
            response = ApprovalDecisionResponse.model_validate_json(cast(str, row["result_json"]))
        return ApprovalDecisionClaim(
            recovery_id=cast(str, row["recovery_id"]),
            request=cls._decision_request_from_row(row),
            request_fingerprint=cast(str, row["request_fingerprint"]),
            response=response,
        )

    @classmethod
    def _verified_decision_claim_from_row(
        cls,
        row: sqlite3.Row,
        *,
        recovery_id: str,
    ) -> ApprovalDecisionClaim:
        """Recompute the complete stored tuple fingerprint before trusting it."""

        try:
            claim = cls._decision_claim_from_row(row)
            recomputed = cls._decision_fingerprint(claim.recovery_id, claim.request)
            claimed_at = datetime.fromisoformat(cast(str, row["claimed_at"]))
            status_value = cast(str, row["status"])
            if status_value == "completed":
                completed_at = datetime.fromisoformat(cast(str, row["completed_at"]))
            else:
                completed_at = None
        except (TypeError, ValueError):
            raise ApprovalDecisionError(
                "resume_incompatible",
                recovery_id,
                status_code=409,
            ) from None
        if (
            claim.recovery_id != recovery_id
            or claim.request_fingerprint != recomputed
            or claimed_at.tzinfo is None
            or claimed_at.utcoffset() != timedelta(0)
            or (
                status_value == "claimed"
                and (
                    row["result_json"] is not None
                    or row["completed_at"] is not None
                    or claim.response is not None
                )
            )
            or (
                status_value == "completed"
                and (
                    row["result_json"] is None
                    or completed_at is None
                    or completed_at.tzinfo is None
                    or completed_at.utcoffset() != timedelta(0)
                    or completed_at < claimed_at
                    or claim.response is None
                )
            )
            or status_value not in {"claimed", "completed"}
        ):
            raise ApprovalDecisionError(
                "resume_incompatible",
                recovery_id,
                status_code=409,
            )
        return claim

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
            raise ApprovalDecisionError("decision_unavailable", recovery_id, status_code=404)
        if (
            cast(str, recovery["status"]) != RecoveryStatus.PENDING_APPROVAL.value
            or cast(str, recovery["scenario_id"]) != ScenarioId.HOTEL.value
            or cast(str, recovery["execution_mode"])
            not in {ExecutionMode.SDK_STUB.value, ExecutionMode.OPENAI_LIVE.value}
        ):
            raise ApprovalDecisionError("decision_unavailable", recovery_id, status_code=409)

        pending_row = connection.execute(
            "SELECT * FROM pending_approvals WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone()
        if pending_row is None:
            raise ApprovalDecisionError("decision_unavailable", recovery_id, status_code=409)
        try:
            pending = self._pending_approval_from_row(pending_row)
        except (TypeError, ValueError):
            raise ApprovalDecisionError(
                "resume_incompatible", recovery_id, status_code=409
            ) from None
        if pending.status not in {"pending", "approved"}:
            raise ApprovalDecisionError("decision_unavailable", recovery_id, status_code=409)
        if request.remedy_id != pending.remedy_id:
            raise ApprovalDecisionError("remedy_mismatch", recovery_id, status_code=422)
        if request.tool_call_id != pending.tool_call_id:
            raise ApprovalDecisionError("tool_call_mismatch", recovery_id, status_code=422)
        if request.remedy_digest != pending.consent_digest:
            raise ApprovalDecisionError("remedy_digest_mismatch", recovery_id, status_code=422)

        remedy_row = connection.execute(
            "SELECT * FROM remedies WHERE recovery_id = ? AND id = ?",
            (recovery_id, pending.remedy_id),
        ).fetchone()
        if remedy_row is None:
            raise ApprovalDecisionError("decision_unavailable", recovery_id, status_code=409)
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
            raise ApprovalDecisionError("remedy_digest_mismatch", recovery_id, status_code=422)
        if consent.expiry <= now:
            raise ApprovalDecisionError("remedy_expired", recovery_id, status_code=422)
        if exact_hotel_terms(consent.evidence) != consent.terms:
            raise ApprovalDecisionError("constraint_denied", recovery_id, status_code=422)
        policy = evaluate_hotel_policy(
            consent.evidence,
            DETERMINISTIC_HOTEL_AUTHORITY,
        )
        if (
            not policy.hard_constraint_satisfied
            or consent.hard_constraint_satisfied != policy.hard_constraint_satisfied
        ):
            raise ApprovalDecisionError("constraint_denied", recovery_id, status_code=422)
        if (
            not policy.delegated_authority_satisfied
            or consent.delegated_authority_satisfied != policy.delegated_authority_satisfied
        ):
            raise ApprovalDecisionError("authority_denied", recovery_id, status_code=422)
        try:
            self._require_pending_consent_binding(pending, consent)
        except (TypeError, ValueError):
            raise ApprovalDecisionError(
                "resume_incompatible", recovery_id, status_code=409
            ) from None
        return pending, consent

    def _require_claim_evidence(
        self,
        connection: sqlite3.Connection,
        recovery_id: str,
        request: ApprovalDecisionRequest,
    ) -> tuple[sqlite3.Row, PendingApprovalEnvelope, RemedyConsentRecord]:
        """Validate immutable claim, consent, policy, and provenance bindings."""

        recovery = connection.execute(
            "SELECT * FROM recoveries WHERE id = ?",
            (recovery_id,),
        ).fetchone()
        pending_row = connection.execute(
            "SELECT * FROM pending_approvals WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone()
        if (
            recovery is None
            or pending_row is None
            or cast(str, recovery["scenario_id"]) != ScenarioId.HOTEL.value
            or cast(str, recovery["execution_mode"])
            not in {ExecutionMode.SDK_STUB.value, ExecutionMode.OPENAI_LIVE.value}
        ):
            raise ApprovalDecisionError(
                "resume_incompatible",
                recovery_id,
                status_code=409,
            )
        try:
            pending = self._pending_approval_from_row(pending_row)
            remedy_row = connection.execute(
                "SELECT * FROM remedies WHERE recovery_id = ? AND id = ?",
                (recovery_id, pending.remedy_id),
            ).fetchone()
            if remedy_row is None:
                raise ValueError("Claim remedy is missing")
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
            self._require_pending_consent_binding(pending, consent)
            provenance = self._provenance_from_row(recovery)
            stored_model_ids = json.loads(cast(str, recovery["model_ids_json"]))
            policy = evaluate_hotel_policy(
                consent.evidence,
                DETERMINISTIC_HOTEL_AUTHORITY,
            )
            if (
                request.remedy_id != pending.remedy_id
                or request.tool_call_id != pending.tool_call_id
                or request.remedy_digest != pending.consent_digest
                or consent.consent_digest != pending.consent_digest
                or recomputed_digest != pending.consent_digest
                or exact_hotel_terms(consent.evidence) != consent.terms
                or not policy.hard_constraint_satisfied
                or not policy.delegated_authority_satisfied
                or consent.hard_constraint_satisfied != policy.hard_constraint_satisfied
                or consent.delegated_authority_satisfied != policy.delegated_authority_satisfied
                or consent.expiry.tzinfo is None
                or consent.expiry.utcoffset() != timedelta(0)
                or pending.execution_mode is not provenance.execution_mode
                or list(pending.model_ids) != stored_model_ids
                or pending.root_trace_id != provenance.root_trace_id
                or pending.sdk_version != provenance.sdk_version
                or pending.protocol_version != provenance.protocol_version
                or pending.agent_graph_version != provenance.agent_graph_version
                or pending.definition_digest != provenance.definition_digest
            ):
                raise ValueError("Claim evidence does not match")
        except (TypeError, ValueError):
            raise ApprovalDecisionError(
                "resume_incompatible",
                recovery_id,
                status_code=409,
            ) from None
        return recovery, pending, consent

    def _public_pending_view(
        self,
        connection: sqlite3.Connection,
        recovery_id: str,
    ) -> PendingApprovalView | None:
        pending_row = connection.execute(
            """
            SELECT *
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
        if pending_row is None:
            return None
        try:
            pending = self._pending_approval_from_row(pending_row)
            if pending.consent_digest == "legacy-incompatible":
                return None
            remedy = connection.execute(
                """
                SELECT * FROM remedies
                WHERE recovery_id = ? AND id = ?
                """,
                (recovery_id, pending.remedy_id),
            ).fetchone()
            if remedy is None:
                return None
            consent = self._remedy_consent_from_row(remedy)
            self._require_pending_consent_binding(pending, consent)
        except (TypeError, ValueError):
            return None
        return consent.public_view(tool_call_id=pending.tool_call_id)

    def _public_claimed_decision_view(
        self,
        connection: sqlite3.Connection,
        recovery_id: str,
    ) -> ClaimedDecisionView | None:
        row = connection.execute(
            """
            SELECT approval_decisions.*
            FROM approval_decisions
            WHERE recovery_id = ? AND status = 'claimed'
            """,
            (recovery_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            claim = self._verified_decision_claim_from_row(
                row,
                recovery_id=recovery_id,
            )
            pending_row = connection.execute(
                "SELECT * FROM pending_approvals WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone()
            if pending_row is None:
                return None
            pending = self._pending_approval_from_row(pending_row)
            if pending.status not in {"pending", "approved"}:
                return None
            remedy = connection.execute(
                """
                SELECT * FROM remedies
                WHERE recovery_id = ? AND id = ?
                """,
                (recovery_id, pending.remedy_id),
            ).fetchone()
            if remedy is None:
                return None
            consent = self._remedy_consent_from_row(remedy)
            self._require_pending_consent_binding(pending, consent)
            if (
                claim.request.remedy_id != pending.remedy_id
                or claim.request.remedy_digest != pending.consent_digest
                or claim.request.tool_call_id != pending.tool_call_id
            ):
                return None
        except (ApprovalDecisionError, TypeError, ValueError):
            return None
        return ClaimedDecisionView(
            action=claim.request.action,
            remedyDigest=claim.request.remedy_digest,
            expiry=consent.expiry,
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

    @staticmethod
    def _provenance_from_row(row: sqlite3.Row) -> RecoveryProvenance:
        model_ids = json.loads(cast(str, row["model_ids_json"]))
        if not isinstance(model_ids, list) or not all(
            isinstance(model_id, str) for model_id in model_ids
        ):
            raise ValueError("Recovery model IDs must be a string array")
        return RecoveryProvenance(
            recovery_id=cast(str, row["id"]),
            execution_mode=ExecutionMode(cast(str, row["execution_mode"])),
            model_ids=tuple(model_ids),
            root_trace_id=cast(str | None, row["root_trace_id"]),
            model_call=bool(cast(int, row["model_call"])),
            sdk_version=cast(str | None, row["sdk_version"]),
            protocol_version=cast(str | None, row["protocol_version"]),
            agent_graph_version=cast(str | None, row["agent_graph_version"]),
            definition_digest=cast(str | None, row["definition_digest"]),
        )

    @staticmethod
    def _require_creation_digest(value: str, *, name: str) -> None:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError(f"{name} must be a lowercase SHA-256 digest")

    @staticmethod
    def _require_creation_recovery_id(recovery_id: str) -> None:
        try:
            canonical = str(UUID(recovery_id))
        except ValueError as error:
            raise ValueError("Creation recovery ID must be a UUID") from error
        if canonical != recovery_id:
            raise ValueError("Creation recovery ID must use canonical UUID text")

    @staticmethod
    def _require_creation_time(value: datetime, *, name: str) -> None:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError(f"{name} must be timezone-aware UTC")

    @staticmethod
    def _creation_receipt_matches(
        row: sqlite3.Row,
        receipt: RecoveryReceipt,
        *,
        scenario_id: ScenarioId,
        execution_mode: ExecutionMode,
    ) -> bool:
        try:
            stored_status = RecoveryStatus(cast(str, row["status"]))
            stored_model_ids = json.loads(cast(str, row["model_ids_json"]))
        except (TypeError, ValueError):
            return False
        if (
            not stored_status.terminal
            or receipt.recovery_id != cast(str, row["id"])
            or receipt.execution_mode is not execution_mode
            or receipt.status != stored_status.value
            or receipt.model_ids != stored_model_ids
            or receipt.root_trace_id != cast(str | None, row["root_trace_id"])
            or receipt.model_call != bool(cast(int, row["model_call"]))
            or receipt.sdk_version != cast(str | None, row["sdk_version"])
            or receipt.protocol_version != cast(str | None, row["protocol_version"])
            or receipt.agent_graph_version != cast(str | None, row["agent_graph_version"])
            or receipt.definition_digest != cast(str | None, row["definition_digest"])
        ):
            return False
        if scenario_id is ScenarioId.API_QUOTA:
            return (
                execution_mode is ExecutionMode.SDK_STUB
                and stored_status is RecoveryStatus.COMPLETED
                and receipt.approval_count == 0
                and receipt.has_canonical_quota_evidence
            )
        if scenario_id is not ScenarioId.HOTEL:
            return False
        return stored_status is not RecoveryStatus.COMPLETED or receipt.approval_count == 1

    @classmethod
    def _quota_public_terminal_bundle_matches(
        cls,
        connection: sqlite3.Connection,
        *,
        row: sqlite3.Row,
        receipt: RecoveryReceipt,
    ) -> bool:
        """Validate completed legacy or durable restart-aware quota evidence."""

        recovery_id = cast(str, row["id"])
        quota_execution_contract = row["quota_execution_contract"]
        if type(quota_execution_contract) is not int or quota_execution_contract not in {0, 1}:
            return False
        execution_rows = connection.execute(
            "SELECT * FROM executions WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchall()
        legacy_rows = connection.execute(
            """
            SELECT recovery_fingerprint, recorded_at
            FROM quota_legacy_completions
            WHERE recovery_id = ?
            """,
            (recovery_id,),
        ).fetchall()
        try:
            recovery_status = RecoveryStatus(cast(str, row["status"]))
            if not execution_rows:
                if (
                    quota_execution_contract != 0
                    or recovery_status is not RecoveryStatus.COMPLETED
                    or len(legacy_rows) != 1
                    or cast(str, legacy_rows[0]["recovery_fingerprint"])
                    != cls._legacy_quota_recovery_fingerprint(row)
                ):
                    return False
                legacy_recorded_at = datetime.fromisoformat(
                    cast(str, legacy_rows[0]["recorded_at"])
                )
                if legacy_recorded_at.tzinfo is None or legacy_recorded_at.utcoffset() != timedelta(
                    0
                ):
                    return False
                execution = DurableExecution(
                    execution_id=cls._quota_execution_id(recovery_id),
                    recovery_id=recovery_id,
                    idempotency_key=cls._quota_idempotency_key(recovery_id),
                    status="completed",
                    provider_execution=True,
                    request_digest=quota_request_digest(DETERMINISTIC_QUOTA_REQUEST),
                    tool_call_id=QUOTA_TOOL_CALL_ID,
                    remedy_digest=None,
                    result_json=DETERMINISTIC_QUOTA_RESULT.model_dump(mode="json"),
                )
            elif len(execution_rows) == 1:
                if quota_execution_contract != 1 or legacy_rows:
                    return False
                execution = cls._require_exact_quota_execution_row(
                    execution_rows[0],
                    recovery_id=recovery_id,
                )
                if (
                    recovery_status is RecoveryStatus.COMPLETED and execution.status != "completed"
                ) or (
                    recovery_status is RecoveryStatus.OUTCOME_UNKNOWN
                    and execution.status != RecoveryStatus.OUTCOME_UNKNOWN.value
                ):
                    return False
                cls._require_quota_execution_chronology(
                    recovery=row,
                    execution=execution_rows[0],
                    terminal=True,
                )
            else:
                return False
            expected_receipt = cls._quota_receipt_for_execution_row(
                recovery=row,
                execution=execution,
            )
            specs = (
                cls._quota_completed_transition_specs(DETERMINISTIC_QUOTA_RESULT)
                if execution.status == "completed"
                else [cls._quota_unknown_transition_spec(recovery_id)]
            )
        except (
            ExecutionConflictError,
            ReceiptTransitionError,
            TypeError,
            ValueError,
        ):
            return False
        return receipt == expected_receipt and cls._quota_terminal_bundle_matches(
            connection,
            recovery=row,
            execution=execution,
            receipt=expected_receipt,
            specs=specs,
        )

    def _hotel_approval_evidence(
        self,
        connection: sqlite3.Connection,
        *,
        row: sqlite3.Row,
        events: list[RecoveryEvent],
    ) -> _HotelApprovalEvidence | None:
        """Load the immutable approval facts shared by active and expiry evidence."""

        recovery_id = cast(str, row["id"])
        expected_approval_data: dict[str, JsonValue] = {
            "phase": "Authorize",
            "providerExecution": False,
            "summary": "An internal Agents SDK approval interruption is pending.",
        }
        try:
            execution_mode = ExecutionMode(cast(str, row["execution_mode"]))
            provenance = self._provenance_from_row(row)
        except (TypeError, ValueError):
            return None
        raw_model_call = row["model_call"]
        quota_execution_contract = row["quota_execution_contract"]
        if (
            cast(str, row["scenario_id"]) != ScenarioId.HOTEL.value
            or execution_mode
            not in {
                ExecutionMode.SDK_STUB,
                ExecutionMode.OPENAI_LIVE,
            }
            or type(raw_model_call) is not int
            or raw_model_call != int(execution_mode is ExecutionMode.OPENAI_LIVE)
            or type(quota_execution_contract) is not int
            or quota_execution_contract != 0
            or provenance.recovery_id != recovery_id
            or provenance.execution_mode is not execution_mode
            or (
                execution_mode is ExecutionMode.SDK_STUB
                and (
                    provenance.model_ids
                    or not isinstance(provenance.root_trace_id, str)
                    or not is_valid_qa_trace_id(provenance.root_trace_id)
                )
            )
            or (
                execution_mode is ExecutionMode.OPENAI_LIVE
                and (
                    provenance.model_ids != ("gpt-5.6-luna", "gpt-5.6-terra")
                    or not isinstance(provenance.root_trace_id, str)
                    or not is_valid_live_trace_id(provenance.root_trace_id)
                )
            )
            or any(
                not isinstance(marker, str) or not marker
                for marker in (
                    provenance.sdk_version,
                    provenance.protocol_version,
                    provenance.agent_graph_version,
                    provenance.definition_digest,
                )
            )
            or not isinstance(provenance.definition_digest, str)
            or len(provenance.definition_digest) != 64
            or any(
                character not in "0123456789abcdef" for character in provenance.definition_digest
            )
            or len(events) < 2
            or events[1].seq != 2
            or events[1].type != "approval.requested"
            or events[1].terminal
            or events[1].data != expected_approval_data
        ):
            return None

        pending_rows = connection.execute(
            "SELECT * FROM pending_approvals WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchall()
        remedy_rows = connection.execute(
            "SELECT * FROM remedies WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchall()
        decision_rows = connection.execute(
            "SELECT * FROM approval_decisions WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchall()
        execution_rows = connection.execute(
            "SELECT * FROM executions WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchall()
        if len(pending_rows) != 1 or len(remedy_rows) != 1 or len(decision_rows) > 1:
            return None

        try:
            pending_row = pending_rows[0]
            remedy_row = remedy_rows[0]
            pending = self._pending_approval_from_row(pending_row)
            consent = self._remedy_consent_from_row(remedy_row)
            self._require_pending_consent_binding(pending, consent)
            pending_created_at = datetime.fromisoformat(cast(str, pending_row["created_at"]))
            pending_updated_at = datetime.fromisoformat(cast(str, pending_row["updated_at"]))
            remedy_created_at = datetime.fromisoformat(cast(str, remedy_row["created_at"]))
            stored_model_ids = json.loads(cast(str, row["model_ids_json"]))
            if (
                pending.recovery_id != recovery_id
                or consent.recovery_id != recovery_id
                or list(pending.model_ids) != stored_model_ids
                or pending.execution_mode is not execution_mode
                or pending.root_trace_id != provenance.root_trace_id
                or pending.sdk_version != provenance.sdk_version
                or pending.protocol_version != provenance.protocol_version
                or pending.agent_graph_version != provenance.agent_graph_version
                or pending.definition_digest != provenance.definition_digest
                or not pending.state_json
                or pending_created_at.tzinfo is None
                or pending_created_at.utcoffset() != timedelta(0)
                or pending_updated_at.tzinfo is None
                or pending_updated_at.utcoffset() != timedelta(0)
                or remedy_created_at.tzinfo is None
                or remedy_created_at.utcoffset() != timedelta(0)
                or consent.expiry.tzinfo is None
                or consent.expiry.utcoffset() != timedelta(0)
                or pending_created_at != events[1].created_at
                or remedy_created_at != events[1].created_at
                or pending_updated_at < pending_created_at
                or events[1].created_at >= consent.expiry
            ):
                return None

            claim: ApprovalDecisionClaim | None = None
            decision_row: sqlite3.Row | None = None
            claimed_at: datetime | None = None
            if decision_rows:
                decision_row = decision_rows[0]
                claim = self._verified_decision_claim_from_row(
                    decision_row,
                    recovery_id=recovery_id,
                )
                claimed_at = datetime.fromisoformat(cast(str, decision_row["claimed_at"]))
                if (
                    cast(str, decision_row["status"]) != "claimed"
                    or claim.response is not None
                    or claim.request.remedy_id != pending.remedy_id
                    or claim.request.remedy_digest != pending.consent_digest
                    or claim.request.tool_call_id != pending.tool_call_id
                    or claimed_at.tzinfo is None
                    or claimed_at.utcoffset() != timedelta(0)
                    or not events[1].created_at <= claimed_at < consent.expiry
                ):
                    return None
        except (ApprovalDecisionError, TypeError, ValueError):
            return None

        return _HotelApprovalEvidence(
            pending=pending,
            consent=consent,
            pending_row=pending_row,
            remedy_row=remedy_row,
            claim=claim,
            decision_row=decision_row,
            execution_rows=tuple(execution_rows),
            approval_requested_at=events[1].created_at,
            pending_updated_at=pending_updated_at,
            claimed_at=claimed_at,
        )

    def _active_hotel_expiry_source_matches(
        self,
        connection: sqlite3.Connection,
        *,
        row: sqlite3.Row,
        events: list[RecoveryEvent],
        now: datetime,
    ) -> bool:
        """Validate active approval structure before any expiry writer can seal it."""

        evidence = self._hotel_approval_evidence(connection, row=row, events=events)
        try:
            recovery_updated_at = datetime.fromisoformat(cast(str, row["updated_at"]))
        except (TypeError, ValueError):
            return False
        if (
            evidence is None
            or now.tzinfo is None
            or now.utcoffset() != timedelta(0)
            or cast(str, row["status"]) != RecoveryStatus.PENDING_APPROVAL.value
            or cast(int, row["current_step"]) != 3
            or len(events) != 2
            or cast(str, evidence.remedy_row["status"]) != "pending"
            or recovery_updated_at.tzinfo is None
            or recovery_updated_at.utcoffset() != timedelta(0)
            or recovery_updated_at > now
        ):
            return False

        if evidence.claim is None:
            return (
                evidence.pending.status == "pending"
                and not evidence.execution_rows
                and cast(str, row["current_step_summary"])
                == "Approval required before demo-provider dispatch."
                and recovery_updated_at == evidence.approval_requested_at
                and evidence.pending_updated_at == evidence.approval_requested_at
            )

        claim = evidence.claim
        claimed_at = evidence.claimed_at
        if (
            claimed_at is None
            or cast(str, row["current_step_summary"])
            != f"Exact {claim.request.action.value} claimed; outcome pending."
            or recovery_updated_at != claimed_at
            or claimed_at > now
        ):
            return False
        if evidence.pending.status == "pending":
            valid_pending_chronology = evidence.pending_updated_at == evidence.approval_requested_at
        else:
            valid_pending_chronology = (
                claimed_at <= evidence.pending_updated_at <= now
                and evidence.pending_updated_at < evidence.consent.expiry
            )
        return valid_pending_chronology and (
            claim.request.action is DecisionAction.APPROVE
            and evidence.pending.status in {"pending", "approved"}
            or claim.request.action is DecisionAction.DECLINE
            and evidence.pending.status in {"pending", "approved", "outcome_unknown"}
        )

    def _hotel_expiry_source_row_matches(
        self,
        connection: sqlite3.Connection,
        *,
        row: sqlite3.Row,
        now: datetime,
    ) -> bool:
        """Load and validate one active expiry candidate without mutating it."""

        try:
            recovery_id = cast(str, row["id"])
            scenario_id = ScenarioId(cast(str, row["scenario_id"]))
            execution_mode = ExecutionMode(cast(str, row["execution_mode"]))
            recovery_created_at = datetime.fromisoformat(cast(str, row["created_at"]))
            recovery_updated_at = datetime.fromisoformat(cast(str, row["updated_at"]))
            events = self._validated_public_event_ledger_in_connection(
                connection,
                recovery_id=recovery_id,
                scenario_id=scenario_id,
                execution_mode=execution_mode,
                recovery_created_at=recovery_created_at,
                recovery_updated_at=recovery_updated_at,
            )
        except (PublicEvidenceIntegrityError, TypeError, ValueError):
            return False
        return self._active_hotel_expiry_source_matches(
            connection,
            row=row,
            events=events,
            now=now,
        )

    def _active_hotel_public_bundle_matches(
        self,
        connection: sqlite3.Connection,
        *,
        row: sqlite3.Row,
        events: list[RecoveryEvent],
        pending_view: PendingApprovalView | None,
        claimed_view: ClaimedDecisionView | None,
        now: datetime,
    ) -> bool:
        """Accept only one exact active hotel approval and its public ledger."""

        evidence = self._hotel_approval_evidence(connection, row=row, events=events)
        if evidence is None or not self._active_hotel_expiry_source_matches(
            connection,
            row=row,
            events=events,
            now=now,
        ):
            return False

        expected_pending_view = evidence.consent.public_view(
            tool_call_id=evidence.pending.tool_call_id
        )
        if evidence.claim is None:
            return (
                pending_view == expected_pending_view
                and claimed_view is None
                and not evidence.execution_rows
            )

        claim = evidence.claim
        claimed_at = evidence.claimed_at
        if claimed_at is None:
            return False
        expected_claimed_view = ClaimedDecisionView(
            action=claim.request.action,
            remedyDigest=claim.request.remedy_digest,
            expiry=evidence.consent.expiry,
        )
        if claim.request.action is DecisionAction.DECLINE:
            return (
                not evidence.execution_rows
                and pending_view is None
                and (
                    evidence.pending.status == "pending"
                    and claimed_view == expected_claimed_view
                    or evidence.pending.status == "outcome_unknown"
                    and claimed_view is None
                )
            )

        if pending_view is not None or claimed_view != expected_claimed_view:
            return False
        if evidence.pending.status == "pending":
            return not evidence.execution_rows
        if evidence.pending.status != "approved":
            return False
        if not evidence.execution_rows:
            return True
        if not self._claim_has_exact_completed_execution(
            list(evidence.execution_rows),
            claim=claim,
            consent=evidence.consent,
            claimed_at=claimed_at,
        ):
            return False
        try:
            execution_updated_at = datetime.fromisoformat(
                cast(str, evidence.execution_rows[0]["updated_at"])
            )
        except (TypeError, ValueError):
            return False
        return execution_updated_at <= now

    def _creation_authoritative_snapshot(
        self,
        connection: sqlite3.Connection,
        *,
        recovery_id: str,
        scenario_id: ScenarioId,
        execution_mode: ExecutionMode,
        session_key: str,
    ) -> RecoverySnapshot | None:
        row = connection.execute(
            """
            SELECT recoveries.*
            FROM recoveries
            WHERE recoveries.id = ?
              AND recoveries.scenario_id = ?
              AND recoveries.execution_mode = ?
              AND EXISTS (
                  SELECT 1
                  FROM recovery_access
                  WHERE recovery_access.recovery_id = recoveries.id
                    AND recovery_access.session_key = ?
              )
            """,
            (
                recovery_id,
                scenario_id.value,
                execution_mode.value,
                session_key,
            ),
        ).fetchone()
        if row is None:
            return None
        try:
            if execution_mode is ExecutionMode.REPLAY_FIXTURE:
                return self._recovery_from_row(row)
            recovery_status = RecoveryStatus(cast(str, row["status"]))
            if (
                scenario_id is ScenarioId.HOTEL
                and recovery_status is RecoveryStatus.PENDING_APPROVAL
            ):
                pending_view = self._public_pending_view(connection, recovery_id)
                claimed_view = self._public_claimed_decision_view(
                    connection,
                    recovery_id,
                )
                self._validate_public_evidence_in_connection(
                    connection,
                    row=row,
                    replay_scenarios={},
                    pending_view=pending_view,
                    claimed_view=claimed_view,
                )
                return self._recovery_from_row(
                    row,
                    pending_approval=pending_view,
                    claimed_decision=claimed_view,
                )
            if not recovery_status.terminal:
                return None
            if scenario_id is ScenarioId.HOTEL and execution_mode in {
                ExecutionMode.SDK_STUB,
                ExecutionMode.OPENAI_LIVE,
            }:
                if (
                    self._validate_public_evidence_in_connection(
                        connection,
                        row=row,
                        replay_scenarios={},
                        pending_view=None,
                        claimed_view=None,
                    )
                    is None
                ):
                    return None
                return self._recovery_from_row(row)
            receipt_row = connection.execute(
                "SELECT receipt_json FROM receipts WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone()
            if receipt_row is None:
                return None
            receipt = RecoveryReceipt.model_validate_json(cast(str, receipt_row["receipt_json"]))
            if scenario_id is ScenarioId.API_QUOTA and execution_mode is ExecutionMode.SDK_STUB:
                valid_terminal = self._quota_public_terminal_bundle_matches(
                    connection,
                    row=row,
                    receipt=receipt,
                )
            else:
                valid_terminal = self._creation_receipt_matches(
                    row,
                    receipt,
                    scenario_id=scenario_id,
                    execution_mode=execution_mode,
                )
            if not valid_terminal:
                return None
            return self._recovery_from_row(row)
        except (PublicEvidenceIntegrityError, TypeError, ValueError):
            return None

    def get_authoritative_recovery_creation(
        self,
        *,
        recovery_id: str,
        scenario_id: ScenarioId,
        execution_mode: ExecutionMode,
        session_key: str,
    ) -> RecoverySnapshot:
        """Load one mode-valid result rather than trusting a bare recovery row."""

        self._require_creation_recovery_id(recovery_id)
        self._require_opaque_session_key(session_key)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN")
            snapshot = self._creation_authoritative_snapshot(
                connection,
                recovery_id=recovery_id,
                scenario_id=scenario_id,
                execution_mode=execution_mode,
                session_key=session_key,
            )
        if snapshot is None:
            raise RecoveryNotFoundError("Recovery creation has no authoritative outcome evidence")
        return snapshot

    @staticmethod
    def _creation_claim_from_row(
        row: sqlite3.Row,
        *,
        disposition: RecoveryCreationDisposition,
    ) -> RecoveryCreationClaim:
        return RecoveryCreationClaim(
            disposition=disposition,
            recovery_id=cast(str, row["recovery_id"]),
            scenario_id=ScenarioId(cast(str, row["scenario_id"])),
            execution_mode=ExecutionMode(cast(str, row["execution_mode"])),
        )

    def claim_recovery_creation(
        self,
        *,
        request_key: str,
        request_fingerprint: str,
        scenario_id: ScenarioId,
        execution_mode: ExecutionMode,
        reserved_recovery_id: str,
        session_key: str,
        ip_key: str,
        expires_at: datetime,
        max_per_session: int = DEFAULT_MAX_RECOVERY_CREATIONS_PER_SESSION,
        max_per_ip: int = DEFAULT_MAX_RECOVERY_CREATIONS_PER_IP,
        max_global: int = DEFAULT_MAX_RECOVERY_CREATIONS_GLOBAL,
        stale_after: timedelta = RECOVERY_CREATION_STALE_AFTER,
        now: datetime | None = None,
    ) -> RecoveryCreationClaim:
        """Claim one durable start without holding the transaction across work."""

        self._require_creation_digest(request_key, name="request_key")
        self._require_creation_digest(
            request_fingerprint,
            name="request_fingerprint",
        )
        self._require_creation_recovery_id(reserved_recovery_id)
        self._require_opaque_session_key(session_key)
        self._require_creation_digest(ip_key, name="ip_key")
        self._require_creation_time(expires_at, name="expires_at")
        if (
            not isinstance(max_per_session, int)
            or isinstance(max_per_session, bool)
            or not 1 <= max_per_session <= MAX_RECOVERY_CREATIONS_PER_SESSION
        ):
            raise ValueError(
                f"max_per_session must be between 1 and {MAX_RECOVERY_CREATIONS_PER_SESSION}"
            )
        if (
            not isinstance(max_per_ip, int)
            or isinstance(max_per_ip, bool)
            or not 1 <= max_per_ip <= MAX_RECOVERY_CREATIONS_PER_IP
        ):
            raise ValueError(f"max_per_ip must be between 1 and {MAX_RECOVERY_CREATIONS_PER_IP}")
        if (
            not isinstance(max_global, int)
            or isinstance(max_global, bool)
            or not 1 <= max_global <= MAX_RECOVERY_CREATIONS_GLOBAL
        ):
            raise ValueError(f"max_global must be between 1 and {MAX_RECOVERY_CREATIONS_GLOBAL}")
        if max_per_session > max_global:
            raise ValueError("max_per_session cannot exceed max_global")
        if stale_after <= timedelta(0):
            raise ValueError("Creation claim stale interval must be positive")
        current = now or self._now()
        self._require_creation_time(current, name="now")
        if expires_at <= current:
            raise ValueError("Creation claim expiry must follow its creation time")
        current_text = current.isoformat()
        expires_at_text = expires_at.isoformat()

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT *
                FROM recovery_creations
                WHERE request_key = ?
                """,
                (request_key,),
            ).fetchone()
            if row is None:
                capacity = connection.execute(
                    """
                    SELECT
                        COUNT(*) AS global_count,
                        COALESCE(
                            SUM(CASE WHEN session_key = ? THEN 1 ELSE 0 END),
                            0
                        ) AS session_count,
                        COALESCE(
                            SUM(CASE WHEN ip_key = ? THEN 1 ELSE 0 END),
                            0
                        ) AS ip_count
                    FROM recovery_creations
                    WHERE expires_at > ?
                    """,
                    (session_key, ip_key, current_text),
                ).fetchone()
                if capacity is None:
                    raise RuntimeError("Recovery creation capacity could not be read")
                if (
                    cast(int, capacity["session_count"]) >= max_per_session
                    or cast(int, capacity["ip_count"]) >= max_per_ip
                    or cast(int, capacity["global_count"]) >= max_global
                ):
                    return RecoveryCreationClaim(
                        disposition="capacity",
                        recovery_id=reserved_recovery_id,
                        scenario_id=scenario_id,
                        execution_mode=execution_mode,
                    )
                connection.execute(
                    """
                    INSERT INTO recovery_creations (
                        request_key, request_fingerprint, session_key, ip_key,
                        scenario_id, execution_mode, recovery_id, status,
                        created_at, updated_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?)
                    """,
                    (
                        request_key,
                        request_fingerprint,
                        session_key,
                        ip_key,
                        scenario_id.value,
                        execution_mode.value,
                        reserved_recovery_id,
                        current_text,
                        current_text,
                        expires_at_text,
                    ),
                )
                return RecoveryCreationClaim(
                    disposition="owner",
                    recovery_id=reserved_recovery_id,
                    scenario_id=scenario_id,
                    execution_mode=execution_mode,
                )

            if (
                cast(str | None, row["session_key"]) != session_key
                or cast(str | None, row["expires_at"]) != expires_at_text
            ):
                connection.execute(
                    """
                    UPDATE recovery_creations
                    SET status = 'unknown', updated_at = ?
                    WHERE request_key = ?
                    """,
                    (current_text, request_key),
                )
                return self._creation_claim_from_row(
                    row,
                    disposition="unknown",
                )

            if (
                cast(str, row["request_fingerprint"]) != request_fingerprint
                or cast(str, row["scenario_id"]) != scenario_id.value
                or cast(str, row["execution_mode"]) != execution_mode.value
            ):
                return self._creation_claim_from_row(
                    row,
                    disposition="conflict",
                )

            stored_recovery_id = cast(str, row["recovery_id"])
            stored_status = cast(str, row["status"])
            if stored_status == "ready":
                if (
                    self._creation_authoritative_snapshot(
                        connection,
                        recovery_id=stored_recovery_id,
                        scenario_id=scenario_id,
                        execution_mode=execution_mode,
                        session_key=session_key,
                    )
                    is not None
                ):
                    return self._creation_claim_from_row(
                        row,
                        disposition="ready",
                    )
                connection.execute(
                    """
                    UPDATE recovery_creations
                    SET status = 'unknown', updated_at = ?
                    WHERE request_key = ?
                    """,
                    (current_text, request_key),
                )
                return self._creation_claim_from_row(
                    row,
                    disposition="unknown",
                )

            if (
                self._creation_authoritative_snapshot(
                    connection,
                    recovery_id=stored_recovery_id,
                    scenario_id=scenario_id,
                    execution_mode=execution_mode,
                    session_key=session_key,
                )
                is not None
            ):
                connection.execute(
                    """
                    UPDATE recovery_creations
                    SET status = 'ready', updated_at = ?
                    WHERE request_key = ?
                    """,
                    (current_text, request_key),
                )
                return self._creation_claim_from_row(
                    row,
                    disposition="ready",
                )

            if stored_status == "unknown":
                return self._creation_claim_from_row(
                    row,
                    disposition="unknown",
                )

            try:
                updated_at = datetime.fromisoformat(cast(str, row["updated_at"]))
            except (TypeError, ValueError):
                updated_at = current - stale_after
            if (
                updated_at.tzinfo is None
                or updated_at.utcoffset() != timedelta(0)
                or updated_at > current
                or current - updated_at >= stale_after
            ):
                connection.execute(
                    """
                    UPDATE recovery_creations
                    SET status = 'unknown', updated_at = ?
                    WHERE request_key = ?
                    """,
                    (current_text, request_key),
                )
                return self._creation_claim_from_row(
                    row,
                    disposition="unknown",
                )
            return self._creation_claim_from_row(
                row,
                disposition="pending",
            )

    def mark_recovery_creation_started(
        self,
        *,
        request_key: str,
        request_fingerprint: str,
        now: datetime | None = None,
    ) -> None:
        """Move the exact reserved owner to started before uncertain work."""

        self._require_creation_digest(request_key, name="request_key")
        self._require_creation_digest(
            request_fingerprint,
            name="request_fingerprint",
        )
        current = now or self._now()
        current_text = current.isoformat()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE recovery_creations
                SET status = 'started', updated_at = ?
                WHERE request_key = ?
                  AND request_fingerprint = ?
                  AND status = 'reserved'
                """,
                (current_text, request_key, request_fingerprint),
            )
            if updated.rowcount == 1:
                return
            row = connection.execute(
                """
                SELECT status, request_fingerprint
                FROM recovery_creations
                WHERE request_key = ?
                """,
                (request_key,),
            ).fetchone()
            if (
                row is not None
                and cast(str, row["request_fingerprint"]) == request_fingerprint
                and cast(str, row["status"]) == "started"
            ):
                return
            raise RuntimeError("Recovery creation owner is no longer reserved")

    def abandon_reserved_recovery_creation(
        self,
        *,
        request_key: str,
        request_fingerprint: str,
    ) -> bool:
        """Delete only an untouched reservation after a known admission denial."""

        self._require_creation_digest(request_key, name="request_key")
        self._require_creation_digest(
            request_fingerprint,
            name="request_fingerprint",
        )
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            deleted = connection.execute(
                """
                DELETE FROM recovery_creations
                WHERE request_key = ?
                  AND request_fingerprint = ?
                  AND status = 'reserved'
                """,
                (request_key, request_fingerprint),
            )
        return deleted.rowcount == 1

    def mark_recovery_creation_ready(
        self,
        *,
        request_key: str,
        request_fingerprint: str,
        recovery_id: str,
        session_key: str,
        now: datetime | None = None,
    ) -> RecoveryCreationClaim:
        """Bind an authoritative accessible recovery to the durable claim."""

        self._require_creation_digest(request_key, name="request_key")
        self._require_creation_digest(
            request_fingerprint,
            name="request_fingerprint",
        )
        self._require_creation_recovery_id(recovery_id)
        self._require_opaque_session_key(session_key)
        current_text = (now or self._now()).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT *
                FROM recovery_creations
                WHERE request_key = ?
                """,
                (request_key,),
            ).fetchone()
            if row is None or cast(str, row["request_fingerprint"]) != request_fingerprint:
                raise RuntimeError("Recovery creation claim no longer matches")
            scenario_id = ScenarioId(cast(str, row["scenario_id"]))
            execution_mode = ExecutionMode(cast(str, row["execution_mode"]))
            reserved_recovery_id = cast(str, row["recovery_id"])
            if (
                reserved_recovery_id != recovery_id
                and execution_mode is not ExecutionMode.REPLAY_FIXTURE
            ):
                raise RuntimeError("Recovery creation changed its reserved ID")
            authoritative = self._creation_authoritative_snapshot(
                connection,
                recovery_id=recovery_id,
                scenario_id=scenario_id,
                execution_mode=execution_mode,
                session_key=session_key,
            )
            if authoritative is None:
                raise RuntimeError("Recovery creation result is not authoritative")
            if cast(str, row["status"]) == "ready" and reserved_recovery_id == recovery_id:
                return RecoveryCreationClaim(
                    disposition="ready",
                    recovery_id=recovery_id,
                    scenario_id=scenario_id,
                    execution_mode=execution_mode,
                )
            connection.execute(
                """
                UPDATE recovery_creations
                SET recovery_id = ?, status = 'ready', updated_at = ?
                WHERE request_key = ? AND request_fingerprint = ?
                """,
                (
                    recovery_id,
                    current_text,
                    request_key,
                    request_fingerprint,
                ),
            )
            return RecoveryCreationClaim(
                disposition="ready",
                recovery_id=recovery_id,
                scenario_id=scenario_id,
                execution_mode=execution_mode,
            )

    def mark_recovery_creation_unknown_or_reconcile(
        self,
        *,
        request_key: str,
        request_fingerprint: str,
        session_key: str,
        now: datetime | None = None,
    ) -> RecoveryCreationClaim:
        """Fail closed after started work unless a complete result is durable."""

        self._require_creation_digest(request_key, name="request_key")
        self._require_creation_digest(
            request_fingerprint,
            name="request_fingerprint",
        )
        self._require_opaque_session_key(session_key)
        current_text = (now or self._now()).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT *
                FROM recovery_creations
                WHERE request_key = ?
                """,
                (request_key,),
            ).fetchone()
            if row is None or cast(str, row["request_fingerprint"]) != request_fingerprint:
                raise RuntimeError("Recovery creation claim no longer matches")
            scenario_id = ScenarioId(cast(str, row["scenario_id"]))
            execution_mode = ExecutionMode(cast(str, row["execution_mode"]))
            recovery_id = cast(str, row["recovery_id"])
            authoritative = (
                self._creation_authoritative_snapshot(
                    connection,
                    recovery_id=recovery_id,
                    scenario_id=scenario_id,
                    execution_mode=execution_mode,
                    session_key=session_key,
                )
                is not None
            )
            if authoritative:
                disposition: RecoveryCreationDisposition = "ready"
            else:
                disposition = "unknown"
            if cast(str, row["status"]) == disposition:
                return self._creation_claim_from_row(
                    row,
                    disposition=disposition,
                )
            connection.execute(
                """
                UPDATE recovery_creations
                SET status = ?, updated_at = ?
                WHERE request_key = ? AND request_fingerprint = ?
                """,
                (
                    disposition,
                    current_text,
                    request_key,
                    request_fingerprint,
                ),
            )
            return RecoveryCreationClaim(
                disposition=disposition,
                recovery_id=recovery_id,
                scenario_id=scenario_id,
                execution_mode=execution_mode,
            )

    def mark_recovery_creation_unknown(
        self,
        *,
        request_key: str,
        request_fingerprint: str,
        now: datetime | None = None,
    ) -> RecoveryCreationClaim:
        """Irreversibly fail closed when mode-specific integrity validation fails."""

        self._require_creation_digest(request_key, name="request_key")
        self._require_creation_digest(
            request_fingerprint,
            name="request_fingerprint",
        )
        current = now or self._now()
        self._require_creation_time(current, name="now")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM recovery_creations WHERE request_key = ?",
                (request_key,),
            ).fetchone()
            if row is None or cast(str, row["request_fingerprint"]) != request_fingerprint:
                raise RuntimeError("Recovery creation claim no longer matches")
            connection.execute(
                """
                UPDATE recovery_creations
                SET status = 'unknown', updated_at = ?
                WHERE request_key = ? AND request_fingerprint = ?
                """,
                (current.isoformat(), request_key, request_fingerprint),
            )
            return self._creation_claim_from_row(
                row,
                disposition="unknown",
            )

    def create_recovery(
        self,
        *,
        recovery_id: str,
        scenario_id: ScenarioId,
        execution_mode: ExecutionMode,
        current_step: int,
        current_step_summary: str,
        model_ids: list[str] | None = None,
        root_trace_id: str | None = None,
        model_call: bool = False,
        sdk_version: str | None = None,
        protocol_version: str | None = None,
        agent_graph_version: str | None = None,
        definition_digest: str | None = None,
        session_key: str | None = None,
        quota_execution_claim: bool = False,
    ) -> RecoverySnapshot:
        selected_model_ids = list(model_ids or [])
        if quota_execution_claim and (
            scenario_id is not ScenarioId.API_QUOTA or execution_mode is not ExecutionMode.SDK_STUB
        ):
            raise ValueError("Durable quota claims require an API quota SDK recovery")
        if execution_mode is ExecutionMode.REPLAY_FIXTURE:
            if (
                selected_model_ids
                or root_trace_id is not None
                or model_call
                or any(
                    marker is not None
                    for marker in (
                        sdk_version,
                        protocol_version,
                        agent_graph_version,
                        definition_digest,
                    )
                )
            ):
                raise ValueError("Replay recoveries cannot carry model or trace provenance")
        elif execution_mode is ExecutionMode.SDK_STUB:
            if selected_model_ids or model_call:
                raise ValueError("SDK stub recoveries cannot carry model IDs")
            root_trace_id = root_trace_id or f"qa_trace_{uuid4().hex}"
            if not is_valid_qa_trace_id(root_trace_id):
                raise ValueError("SDK stub recovery has invalid trace provenance")
        elif (
            selected_model_ids != ["gpt-5.6-luna", "gpt-5.6-terra"]
            or root_trace_id is None
            or not is_valid_live_trace_id(root_trace_id)
            or not model_call
            or any(
                marker is None
                for marker in (
                    sdk_version,
                    protocol_version,
                    agent_graph_version,
                    definition_digest,
                )
            )
        ):
            raise ValueError("OpenAI live recoveries require model and trace provenance")
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
                    current_step_summary, model_ids_json, root_trace_id,
                    model_call, sdk_version, protocol_version,
                    agent_graph_version, definition_digest,
                    quota_execution_contract, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    recovery_id,
                    scenario_id.value,
                    execution_mode.value,
                    RecoveryStatus.IN_PROGRESS.value,
                    current_step,
                    current_step_summary,
                    json.dumps(selected_model_ids, separators=(",", ":")),
                    root_trace_id,
                    int(model_call),
                    sdk_version,
                    protocol_version,
                    agent_graph_version,
                    definition_digest,
                    int(quota_execution_claim),
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
            self._bind_recovery_access(
                connection,
                recovery_id=recovery_id,
                session_key=session_key,
            )
            if quota_execution_claim:
                self._insert_quota_execution_claim_in_connection(
                    connection,
                    recovery_id=recovery_id,
                    now=now,
                )
        return self.get_recovery(recovery_id)

    def get_or_create_replay(
        self,
        *,
        recovery_id: str,
        scenario: ReplayScenarioDefinition,
        session_key: str | None = None,
    ) -> RecoverySnapshot:
        """Atomically persist or reuse one complete canonical replay fixture."""

        if scenario.execution_mode is not ExecutionMode.REPLAY_FIXTURE:
            raise ValueError("Canonical replay persistence requires replay_fixture mode")
        final_event = scenario.events[-1]
        if scenario.receipt is not None and not final_event.status.terminal:
            raise ReceiptTransitionError("Replay receipt requires a terminal final event")

        now = self._now()
        now_text = now.isoformat()
        created_payload = self._canonical_replay_created_payload(scenario)
        receipt = self._canonical_replay_receipt(
            recovery_id=recovery_id,
            scenario=scenario,
        )

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM recoveries WHERE id = ?", (recovery_id,)
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO recoveries (
                        id, scenario_id, execution_mode, status, current_step,
                        current_step_summary, model_ids_json, root_trace_id,
                        model_call, sdk_version, protocol_version,
                        agent_graph_version, definition_digest,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, '[]', NULL, 0, NULL, NULL, NULL, NULL, ?, ?)
                    """,
                    (
                        recovery_id,
                        scenario.id.value,
                        ExecutionMode.REPLAY_FIXTURE.value,
                        final_event.status.value,
                        final_event.current_step,
                        final_event.summary,
                        now_text,
                        now_text,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO events (
                        recovery_id, seq, type, terminal, data_json, created_at
                    ) VALUES (?, 1, 'recovery.created', 0, ?, ?)
                    """,
                    (
                        recovery_id,
                        json.dumps(created_payload, separators=(",", ":"), sort_keys=True),
                        now_text,
                    ),
                )
                for sequence, event in enumerate(scenario.events, start=2):
                    connection.execute(
                        """
                        INSERT INTO events (
                            recovery_id, seq, type, terminal, data_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            recovery_id,
                            sequence,
                            event.type,
                            int(event.status.terminal),
                            json.dumps(event.data, separators=(",", ":"), sort_keys=True),
                            now_text,
                        ),
                    )
                if receipt is not None:
                    connection.execute(
                        """
                        INSERT INTO receipts (recovery_id, receipt_json, created_at)
                        VALUES (?, ?, ?)
                        """,
                        (
                            recovery_id,
                            receipt.model_dump_json(by_alias=True),
                            now_text,
                        ),
                    )
                row = connection.execute(
                    "SELECT * FROM recoveries WHERE id = ?", (recovery_id,)
                ).fetchone()

            if row is None:
                raise ReplayIntegrityError("Canonical replay row was not persisted")
            self._validate_canonical_replay(
                connection,
                row=row,
                scenario=scenario,
                created_payload=created_payload,
                receipt=receipt,
            )
            self._bind_recovery_access(
                connection,
                recovery_id=recovery_id,
                session_key=session_key,
            )
            return self._recovery_from_row(row)

    @staticmethod
    def _canonical_replay_created_payload(
        scenario: ReplayScenarioDefinition,
    ) -> dict[str, JsonValue]:
        return {
            "scenarioId": scenario.id.value,
            "executionMode": ExecutionMode.REPLAY_FIXTURE.value,
            "summary": "Recovery created for the selected execution mode.",
        }

    @staticmethod
    def _canonical_replay_receipt(
        *,
        recovery_id: str,
        scenario: ReplayScenarioDefinition,
    ) -> RecoveryReceipt | None:
        return (
            RecoveryReceipt(
                recoveryId=recovery_id,
                executionMode=ExecutionMode.REPLAY_FIXTURE,
                modelCall=False,
                rootTraceId=None,
                sdkVersion=None,
                protocolVersion=None,
                agentGraphVersion=None,
                definitionDigest=None,
                **scenario.receipt.model_dump(),
            )
            if scenario.receipt is not None
            else None
        )

    @staticmethod
    def _validate_canonical_replay(
        connection: sqlite3.Connection,
        *,
        row: sqlite3.Row,
        scenario: ReplayScenarioDefinition,
        created_payload: dict[str, JsonValue],
        receipt: RecoveryReceipt | None,
    ) -> None:
        """Fail closed instead of returning a partial or mutable replay snapshot."""

        recovery_id = cast(str, row["id"])
        final_event = scenario.events[-1]
        try:
            replay_created_at = datetime.fromisoformat(
                cast(str, row["created_at"]).replace("Z", "+00:00")
            )
            replay_updated_at = datetime.fromisoformat(
                cast(str, row["updated_at"]).replace("Z", "+00:00")
            )
        except (TypeError, ValueError):
            raise ReplayIntegrityError("Canonical replay timestamps are malformed") from None
        provenance_values = (
            json.loads(cast(str, row["model_ids_json"])),
            row["root_trace_id"],
            cast(int, row["model_call"]),
            row["sdk_version"],
            row["protocol_version"],
            row["agent_graph_version"],
            row["definition_digest"],
        )
        if (
            cast(str, row["scenario_id"]) != scenario.id.value
            or cast(str, row["execution_mode"]) != ExecutionMode.REPLAY_FIXTURE.value
            or cast(str, row["status"]) != final_event.status.value
            or cast(int, row["current_step"]) != final_event.current_step
            or cast(str, row["current_step_summary"]) != final_event.summary
            or provenance_values != ([], None, 0, None, None, None, None)
            or replay_created_at.tzinfo is None
            or replay_created_at.utcoffset() != timedelta(0)
            or replay_updated_at.tzinfo is None
            or replay_updated_at.utcoffset() != timedelta(0)
            or replay_created_at != replay_updated_at
        ):
            raise ReplayIntegrityError("Canonical replay snapshot does not match its definition")

        stored_events = connection.execute(
            """
            SELECT seq, type, terminal, data_json, created_at
            FROM events
            WHERE recovery_id = ?
            ORDER BY seq ASC
            """,
            (recovery_id,),
        ).fetchall()
        expected_events = [
            (1, "recovery.created", False, created_payload),
            *[
                (
                    sequence,
                    event.type,
                    event.status.terminal,
                    event.data,
                )
                for sequence, event in enumerate(scenario.events, start=2)
            ],
        ]
        actual_events = [
            (
                cast(int, event["seq"]),
                cast(str, event["type"]),
                bool(cast(int, event["terminal"])),
                json.loads(cast(str, event["data_json"])),
            )
            for event in stored_events
        ]
        try:
            event_created_at = [
                datetime.fromisoformat(cast(str, event["created_at"]).replace("Z", "+00:00"))
                for event in stored_events
            ]
        except (TypeError, ValueError):
            raise ReplayIntegrityError("Canonical replay event timestamps are malformed") from None
        if actual_events != expected_events or any(
            created_at.tzinfo is None
            or created_at.utcoffset() != timedelta(0)
            or created_at != replay_updated_at
            for created_at in event_created_at
        ):
            raise ReplayIntegrityError("Canonical replay event set is incomplete or changed")

        receipt_row = connection.execute(
            """
            SELECT receipt_json, created_at
            FROM receipts
            WHERE recovery_id = ?
            """,
            (recovery_id,),
        ).fetchone()
        if receipt is None:
            if receipt_row is not None:
                raise ReplayIntegrityError("Canonical replay has an unexpected receipt")
        else:
            try:
                receipt_created_at = (
                    datetime.fromisoformat(
                        cast(str, receipt_row["created_at"]).replace("Z", "+00:00")
                    )
                    if receipt_row is not None
                    else None
                )
                stored_receipt = (
                    RecoveryReceipt.model_validate_json(cast(str, receipt_row["receipt_json"]))
                    if receipt_row is not None
                    else None
                )
            except (TypeError, ValueError):
                raise ReplayIntegrityError("Canonical replay receipt is malformed") from None
            if (
                stored_receipt != receipt
                or receipt_created_at is None
                or receipt_created_at.tzinfo is None
                or receipt_created_at.utcoffset() != timedelta(0)
                or receipt_created_at != replay_updated_at
            ):
                raise ReplayIntegrityError("Canonical replay receipt is incomplete or changed")

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
        receipt_json = receipt.model_dump_json(by_alias=True) if receipt is not None else None
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
            raise ValueError("Pending SDK envelope and authoritative consent must persist together")
        if pending_approval is not None and remedy_consent is not None:
            if (
                pending_approval.recovery_id != recovery_id
                or remedy_consent.recovery_id != recovery_id
            ):
                raise ValueError("Pending approval binding does not match recovery")
            self._require_pending_consent_binding(pending_approval, remedy_consent)
            remedy_consent.public_view(tool_call_id=pending_approval.tool_call_id)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM recoveries WHERE id = ?", (recovery_id,)
            ).fetchone()
            if existing is None:
                raise RecoveryNotFoundError("Recovery not found")
            if receipt is not None:
                if not status.terminal:
                    raise ReceiptTransitionError("Receipt requires a terminal transition")
                if receipt.recovery_id != recovery_id:
                    raise ReceiptTransitionError(
                        "Receipt recovery ID does not match the transition"
                    )
                existing_mode = ExecutionMode(cast(str, existing["execution_mode"]))
                if receipt.execution_mode is not existing_mode:
                    raise ReceiptTransitionError(
                        "Receipt execution mode does not match the recovery"
                    )
                existing_scenario = ScenarioId(cast(str, existing["scenario_id"]))
                is_quota_sdk_receipt = (
                    receipt.execution_mode is ExecutionMode.SDK_STUB
                    and receipt.approval_count == 0
                    and receipt.boundary == QUOTA_SDK_STUB_BOUNDARY
                    and receipt.status
                    in {
                        "completed",
                        RecoveryStatus.OUTCOME_UNKNOWN.value,
                    }
                )
                if (
                    receipt.execution_mode is ExecutionMode.SDK_STUB
                    and receipt.status
                    in {
                        "completed",
                        RecoveryStatus.OUTCOME_UNKNOWN.value,
                    }
                    and is_quota_sdk_receipt != (existing_scenario is ScenarioId.API_QUOTA)
                ):
                    raise ReceiptTransitionError(
                        "Delegated quota receipt does not match the recovery scenario"
                    )
                if (
                    is_quota_sdk_receipt
                    and receipt.status == "completed"
                    and not receipt.has_canonical_quota_evidence
                ):
                    raise ReceiptTransitionError(
                        "Delegated quota receipt evidence does not match canonical facts"
                    )
                if (
                    is_quota_sdk_receipt
                    and receipt.status == RecoveryStatus.OUTCOME_UNKNOWN.value
                    and not receipt.has_canonical_quota_unknown_evidence
                ):
                    raise ReceiptTransitionError(
                        "Unknown quota receipt evidence does not match canonical facts"
                    )
            if pending_approval is not None:
                if pending_approval.recovery_id != recovery_id:
                    raise ValueError("Pending approval recovery ID does not match transition")
                existing_mode = ExecutionMode(cast(str, existing["execution_mode"]))
                if pending_approval.execution_mode is not existing_mode:
                    raise ValueError("Pending approval execution mode does not match recovery")
                stored_model_ids = json.loads(cast(str, existing["model_ids_json"]))
                marker_pairs = {
                    "model_ids": (stored_model_ids, list(pending_approval.model_ids)),
                    "root_trace_id": (
                        cast(str | None, existing["root_trace_id"]),
                        pending_approval.root_trace_id,
                    ),
                    "sdk_version": (
                        cast(str | None, existing["sdk_version"]),
                        pending_approval.sdk_version,
                    ),
                    "protocol_version": (
                        cast(str | None, existing["protocol_version"]),
                        pending_approval.protocol_version,
                    ),
                    "agent_graph_version": (
                        cast(str | None, existing["agent_graph_version"]),
                        pending_approval.agent_graph_version,
                    ),
                    "definition_digest": (
                        cast(str | None, existing["definition_digest"]),
                        pending_approval.definition_digest,
                    ),
                }
                for marker, (stored, pending) in marker_pairs.items():
                    if stored is not None and stored != pending:
                        raise ValueError(f"Pending approval {marker} does not match recovery")
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
                        model_ids_json, execution_mode, action_digest, remedy_id, consent_digest,
                        state_json, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        pending_approval.tool_call_id,
                        pending_approval.recovery_id,
                        pending_approval.sdk_version,
                        pending_approval.protocol_version,
                        pending_approval.agent_graph_version,
                        pending_approval.definition_digest,
                        pending_approval.root_trace_id,
                        json.dumps(pending_approval.model_ids, separators=(",", ":")),
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

    def _validated_public_event_ledger_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        recovery_id: str,
        scenario_id: ScenarioId,
        execution_mode: ExecutionMode,
        recovery_created_at: datetime,
        recovery_updated_at: datetime,
    ) -> list[RecoveryEvent]:
        """Load one complete UTC event chronology for a public evidence read."""

        event_rows = connection.execute(
            """
            SELECT recovery_id, seq, type, terminal, data_json, created_at
            FROM events
            WHERE recovery_id = ?
            ORDER BY seq ASC
            """,
            (recovery_id,),
        ).fetchall()
        try:
            events = [self._event_from_row(event_row) for event_row in event_rows]
        except (TypeError, ValueError):
            raise PublicEvidenceIntegrityError("Recovery event ledger is malformed") from None
        expected_created_payload: dict[str, JsonValue] = {
            "scenarioId": scenario_id.value,
            "executionMode": execution_mode.value,
            "summary": "Recovery created for the selected execution mode.",
        }
        if (
            not events
            or events[0].seq != 1
            or events[0].type != "recovery.created"
            or events[0].terminal
            or events[0].data != expected_created_payload
            or events[0].created_at != recovery_created_at
        ):
            raise PublicEvidenceIntegrityError(
                "Recovery creation event does not match its snapshot"
            )

        previous_created_at = recovery_created_at
        for expected_sequence, event in enumerate(events, start=1):
            if (
                event.recovery_id != recovery_id
                or event.seq != expected_sequence
                or event.created_at.tzinfo is None
                or event.created_at.utcoffset() != timedelta(0)
                or event.created_at < previous_created_at
                or event.created_at > recovery_updated_at
            ):
                raise PublicEvidenceIntegrityError(
                    "Recovery event chronology does not match its snapshot"
                )
            previous_created_at = event.created_at
        return events

    def _validate_public_evidence_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        row: sqlite3.Row,
        replay_scenarios: Mapping[ScenarioId, ReplayScenarioDefinition],
        pending_view: PendingApprovalView | None,
        claimed_view: ClaimedDecisionView | None,
    ) -> RecoveryReceipt | None:
        """Validate one public snapshot, terminal event, and receipt as a bundle."""

        recovery_id = cast(str, row["id"])
        try:
            scenario_id = ScenarioId(cast(str, row["scenario_id"]))
            execution_mode = ExecutionMode(cast(str, row["execution_mode"]))
            recovery_status = RecoveryStatus(cast(str, row["status"]))
            provenance = self._provenance_from_row(row)
            recovery_created_at = datetime.fromisoformat(
                cast(str, row["created_at"]).replace("Z", "+00:00")
            )
            recovery_updated_at = datetime.fromisoformat(
                cast(str, row["updated_at"]).replace("Z", "+00:00")
            )
        except (TypeError, ValueError):
            raise PublicEvidenceIntegrityError("Recovery provenance is invalid") from None
        if (
            recovery_created_at.tzinfo is None
            or recovery_created_at.utcoffset() != timedelta(0)
            or recovery_updated_at.tzinfo is None
            or recovery_updated_at.utcoffset() != timedelta(0)
            or recovery_created_at > recovery_updated_at
        ):
            raise PublicEvidenceIntegrityError("Recovery timestamps are invalid")
        events = self._validated_public_event_ledger_in_connection(
            connection,
            recovery_id=recovery_id,
            scenario_id=scenario_id,
            execution_mode=execution_mode,
            recovery_created_at=recovery_created_at,
            recovery_updated_at=recovery_updated_at,
        )

        if execution_mode is ExecutionMode.REPLAY_FIXTURE:
            scenario = replay_scenarios.get(scenario_id)
            if scenario is None:
                raise PublicEvidenceIntegrityError("Replay scenario definition is unavailable")
            try:
                self._validate_canonical_replay(
                    connection,
                    row=row,
                    scenario=scenario,
                    created_payload=self._canonical_replay_created_payload(scenario),
                    receipt=self._canonical_replay_receipt(
                        recovery_id=recovery_id,
                        scenario=scenario,
                    ),
                )
            except (ReplayIntegrityError, TypeError, ValueError):
                raise PublicEvidenceIntegrityError("Canonical replay evidence is invalid") from None

        receipt_row = connection.execute(
            """
            SELECT receipt_json, created_at
            FROM receipts
            WHERE recovery_id = ?
            """,
            (recovery_id,),
        ).fetchone()
        terminal_events = [event for event in events if event.terminal]

        if not recovery_status.terminal:
            if receipt_row is not None or terminal_events:
                raise PublicEvidenceIntegrityError("Nonterminal recovery has terminal evidence")
            if (
                scenario_id is ScenarioId.HOTEL
                and execution_mode
                in {
                    ExecutionMode.SDK_STUB,
                    ExecutionMode.OPENAI_LIVE,
                }
                and not self._active_hotel_public_bundle_matches(
                    connection,
                    row=row,
                    events=events,
                    pending_view=pending_view,
                    claimed_view=claimed_view,
                    now=self._now(),
                )
            ):
                raise PublicEvidenceIntegrityError(
                    "Active hotel approval evidence is not canonical"
                )
            if scenario_id is ScenarioId.API_QUOTA and execution_mode is ExecutionMode.SDK_STUB:
                try:
                    self._require_pristine_quota_recovery(
                        connection,
                        recovery_id=recovery_id,
                        allowed_creation_statuses=frozenset({"started", "unknown"}),
                        now=self._now(),
                    )
                    execution_rows = connection.execute(
                        "SELECT * FROM executions WHERE recovery_id = ?",
                        (recovery_id,),
                    ).fetchall()
                    if len(execution_rows) != 1:
                        raise ExecutionConflictError(
                            "Quota recovery has no exact durable execution"
                        )
                    execution = self._require_exact_quota_execution_row(
                        execution_rows[0],
                        recovery_id=recovery_id,
                    )
                    if execution.status not in {
                        "pending",
                        QUOTA_EXECUTION_RESULT_RECORDED,
                        "completed",
                    }:
                        raise ExecutionConflictError(
                            "Quota execution has no reconcilable durable state"
                        )
                    self._require_quota_execution_chronology(
                        recovery=row,
                        execution=execution_rows[0],
                        terminal=False,
                        now=self._now(),
                    )
                except ExecutionConflictError:
                    raise PublicEvidenceIntegrityError(
                        "SDK quota in-progress evidence is not canonical"
                    ) from None
            return None

        if (
            cast(int, row["current_step"]) != 5
            or receipt_row is None
            or len(terminal_events) != 1
            or terminal_events[0].seq != events[-1].seq
        ):
            raise PublicEvidenceIntegrityError(
                "Terminal recovery evidence is incomplete or ambiguous"
            )

        try:
            receipt = RecoveryReceipt.model_validate_json(cast(str, receipt_row["receipt_json"]))
            terminal_event = terminal_events[0]
            receipt_created_at = datetime.fromisoformat(
                cast(str, receipt_row["created_at"]).replace("Z", "+00:00")
            )
            event_created_at = terminal_event.created_at
        except (TypeError, ValueError):
            raise PublicEvidenceIntegrityError("Terminal recovery evidence is malformed") from None

        expected_receipt_status = (
            "simulated_completed"
            if execution_mode is ExecutionMode.REPLAY_FIXTURE
            and recovery_status is RecoveryStatus.COMPLETED
            else recovery_status.value
        )
        if (
            receipt.recovery_id != recovery_id
            or receipt.execution_mode is not execution_mode
            or receipt.status != expected_receipt_status
            or receipt.model_call != provenance.model_call
            or tuple(receipt.model_ids) != provenance.model_ids
            or receipt.root_trace_id != provenance.root_trace_id
            or receipt.sdk_version != provenance.sdk_version
            or receipt.protocol_version != provenance.protocol_version
            or receipt.agent_graph_version != provenance.agent_graph_version
            or receipt.definition_digest != provenance.definition_digest
            or terminal_event.recovery_id != recovery_id
            or not terminal_event.terminal
            or receipt_created_at.tzinfo is None
            or receipt_created_at.utcoffset() != timedelta(0)
            or event_created_at.tzinfo is None
            or event_created_at.utcoffset() != timedelta(0)
            or recovery_updated_at != receipt_created_at
            or receipt_created_at != event_created_at
        ):
            raise PublicEvidenceIntegrityError("Terminal recovery provenance does not match")

        event_data = terminal_event.data
        if (
            ("recoveryId" in event_data and event_data["recoveryId"] != recovery_id)
            or (
                "executionMode" in event_data
                and event_data["executionMode"] != execution_mode.value
            )
            or (
                "providerExecution" in event_data
                and event_data["providerExecution"] is not receipt.provider_execution
            )
            or ("phase" in event_data and event_data["phase"] != "Verify & seal")
        ):
            raise PublicEvidenceIntegrityError("Terminal event provenance does not match")

        if scenario_id is ScenarioId.API_QUOTA:
            if execution_mode is ExecutionMode.SDK_STUB:
                if not self._quota_public_terminal_bundle_matches(
                    connection,
                    row=row,
                    receipt=receipt,
                ):
                    raise PublicEvidenceIntegrityError(
                        "SDK quota terminal evidence is not canonical"
                    )
                return receipt
            if (
                recovery_status is not RecoveryStatus.COMPLETED
                or execution_mode is ExecutionMode.OPENAI_LIVE
                or receipt.approval_count != 0
                or receipt.approved_remedy_digest is not None
                or terminal_event.type != "quota.receipt_sealed"
            ):
                raise PublicEvidenceIntegrityError(
                    "Quota terminal evidence does not match its scenario"
                )
            return receipt

        if execution_mode is ExecutionMode.REPLAY_FIXTURE:
            raise PublicEvidenceIntegrityError("Replay hotel recovery cannot be terminal")
        allowed_hotel_event_types = {
            RecoveryStatus.COMPLETED: {"recovery.completed"},
            RecoveryStatus.CLOSED_WITHOUT_ACTION: {
                "recovery.closed_without_action",
                "recovery.expired",
                "recovery.claim_expired",
            },
            RecoveryStatus.OUTCOME_UNKNOWN: {
                "recovery.outcome_unknown",
                "recovery.claim_expired",
            },
        }
        if terminal_event.type not in allowed_hotel_event_types[recovery_status] or (
            recovery_status is RecoveryStatus.COMPLETED and receipt.approval_count != 1
        ):
            raise PublicEvidenceIntegrityError("Hotel terminal evidence does not match its outcome")

        decision_row = connection.execute(
            "SELECT * FROM approval_decisions WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone()
        if decision_row is not None:
            try:
                claim = self._verified_decision_claim_from_row(
                    decision_row,
                    recovery_id=recovery_id,
                )
            except (ApprovalDecisionError, TypeError, ValueError):
                raise PublicEvidenceIntegrityError(
                    "Terminal decision evidence is invalid"
                ) from None
            if terminal_event.type == "recovery.claim_expired":
                valid_decision_evidence = self._recovery_has_expiration_evidence(
                    connection,
                    recovery_id,
                )
            elif claim.response is None:
                valid_decision_evidence = (
                    terminal_event.type == "recovery.completed"
                    and self._reconcilable_completed_claim(
                        connection,
                        recovery_id=recovery_id,
                        decision_row=decision_row,
                    )
                    is not None
                )
            else:
                valid_decision_evidence = self._completed_decision_has_terminal_evidence(
                    connection,
                    recovery_id=recovery_id,
                    decision_row=decision_row,
                    claim=claim,
                )
            if not valid_decision_evidence:
                raise PublicEvidenceIntegrityError("Terminal decision evidence does not match")
            return receipt

        if terminal_event.type == "recovery.expired":
            if not self._recovery_has_expiration_evidence(connection, recovery_id):
                raise PublicEvidenceIntegrityError("Expiration evidence does not match")
            return receipt

        raise PublicEvidenceIntegrityError("Terminal recovery has no authoritative evidence source")

    def _expire_targeted_approval_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        recovery_id: str,
        now: datetime,
    ) -> int:
        """Seal at most one authorized target without leaving the transaction."""

        claimed_count = self._expire_claimed_decisions_in_transaction(
            now=now,
            batch_size=1,
            recovery_id=recovery_id,
            connection=connection,
        )
        if claimed_count:
            return claimed_count
        return self._expire_pending_approvals_in_transaction(
            now=now,
            batch_size=1,
            recovery_id=recovery_id,
            connection=connection,
        )

    def _read_public_evidence(
        self,
        recovery_id: str,
        *,
        session_key: str,
        replay_scenarios: Mapping[ScenarioId, ReplayScenarioDefinition],
        after_seq: int | None,
        seal_expired: bool,
    ) -> tuple[
        RecoverySnapshot,
        RecoveryReceipt | None,
        list[RecoveryEvent],
    ]:
        """Read and validate one public evidence bundle in one SQLite snapshot."""

        self._require_opaque_session_key(session_key)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE" if seal_expired else "BEGIN")
            access = connection.execute(
                """
                SELECT 1 FROM recovery_access
                WHERE recovery_id = ? AND session_key = ?
                """,
                (recovery_id, session_key),
            ).fetchone()
            if access is None:
                raise PublicRecoveryAccessRevokedError("Recovery not found")
            row = connection.execute(
                "SELECT * FROM recoveries WHERE id = ?",
                (recovery_id,),
            ).fetchone()
            if row is None:
                raise PublicEvidenceIntegrityError(
                    "Public recovery access references a missing recovery"
                )
            if seal_expired:
                current_time = self._now()
                if (
                    cast(str, row["status"]) == RecoveryStatus.PENDING_APPROVAL.value
                    and cast(str, row["scenario_id"]) == ScenarioId.HOTEL.value
                    and cast(str, row["execution_mode"])
                    in {
                        ExecutionMode.SDK_STUB.value,
                        ExecutionMode.OPENAI_LIVE.value,
                    }
                    and not self._hotel_expiry_source_row_matches(
                        connection,
                        row=row,
                        now=current_time,
                    )
                ):
                    raise PublicEvidenceIntegrityError(
                        "Active hotel approval evidence is not canonical"
                    )
                self._expire_targeted_approval_in_transaction(
                    connection,
                    recovery_id=recovery_id,
                    now=current_time,
                )
                row = connection.execute(
                    "SELECT * FROM recoveries WHERE id = ?",
                    (recovery_id,),
                ).fetchone()
                if row is None:
                    raise RecoveryNotFoundError("Recovery not found")
            is_pending_hotel = (
                cast(str, row["status"]) == RecoveryStatus.PENDING_APPROVAL.value
                and cast(str, row["scenario_id"]) == ScenarioId.HOTEL.value
            )
            pending_view = (
                self._public_pending_view(connection, recovery_id) if is_pending_hotel else None
            )
            claimed_view = (
                self._public_claimed_decision_view(connection, recovery_id)
                if is_pending_hotel
                else None
            )
            receipt = self._validate_public_evidence_in_connection(
                connection,
                row=row,
                replay_scenarios=replay_scenarios,
                pending_view=pending_view,
                claimed_view=claimed_view,
            )
            event_rows = (
                connection.execute(
                    """
                    SELECT recovery_id, seq, type, terminal, data_json, created_at
                    FROM events
                    WHERE recovery_id = ? AND seq > ?
                    ORDER BY seq ASC
                    """,
                    (recovery_id, after_seq),
                ).fetchall()
                if after_seq is not None
                else []
            )
            snapshot = self._recovery_from_row(
                row,
                pending_approval=pending_view,
                claimed_decision=claimed_view,
            )
            events = [self._event_from_row(event_row) for event_row in event_rows]
            connection.commit()
        return snapshot, receipt, events

    def get_public_recovery(
        self,
        recovery_id: str,
        *,
        session_key: str,
        replay_scenarios: Mapping[ScenarioId, ReplayScenarioDefinition],
    ) -> RecoverySnapshot:
        """Return a snapshot only after its public terminal bundle is coherent."""

        snapshot, _receipt, _events = self._read_public_evidence(
            recovery_id,
            session_key=session_key,
            replay_scenarios=replay_scenarios,
            after_seq=None,
            seal_expired=True,
        )
        return snapshot

    def get_public_receipt(
        self,
        recovery_id: str,
        *,
        session_key: str,
        replay_scenarios: Mapping[ScenarioId, ReplayScenarioDefinition],
    ) -> RecoveryReceipt:
        """Return a receipt only after its public terminal bundle is coherent."""

        _snapshot, receipt, _events = self._read_public_evidence(
            recovery_id,
            session_key=session_key,
            replay_scenarios=replay_scenarios,
            after_seq=None,
            seal_expired=True,
        )
        if receipt is None:
            raise RecoveryNotFoundError("Receipt not found")
        return receipt

    def read_public_event_batch(
        self,
        recovery_id: str,
        *,
        after_seq: int = 0,
        session_key: str,
        replay_scenarios: Mapping[ScenarioId, ReplayScenarioDefinition],
    ) -> tuple[list[RecoveryEvent], RecoveryStatus]:
        """Read an event batch only after validating the same public DB snapshot."""

        snapshot, _receipt, events = self._read_public_evidence(
            recovery_id,
            session_key=session_key,
            replay_scenarios=replay_scenarios,
            after_seq=after_seq,
            seal_expired=False,
        )
        return events, snapshot.status

    def read_initial_public_event_batch(
        self,
        recovery_id: str,
        *,
        after_seq: int = 0,
        session_key: str,
        replay_scenarios: Mapping[ScenarioId, ReplayScenarioDefinition],
    ) -> tuple[list[RecoveryEvent], RecoveryStatus]:
        """Authorize, seal expiry, and validate an initial public event batch."""

        snapshot, _receipt, events = self._read_public_evidence(
            recovery_id,
            session_key=session_key,
            replay_scenarios=replay_scenarios,
            after_seq=after_seq,
            seal_expired=True,
        )
        return events, snapshot.status

    def get_recovery(self, recovery_id: str) -> RecoverySnapshot:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT * FROM recoveries WHERE id = ?", (recovery_id,)
            ).fetchone()
            is_pending_hotel = (
                row is not None
                and cast(str, row["status"]) == RecoveryStatus.PENDING_APPROVAL.value
                and cast(str, row["scenario_id"]) == ScenarioId.HOTEL.value
            )
            pending_view = (
                self._public_pending_view(connection, recovery_id) if is_pending_hotel else None
            )
            claimed_view = (
                self._public_claimed_decision_view(connection, recovery_id)
                if is_pending_hotel
                else None
            )
        if row is None:
            raise RecoveryNotFoundError("Recovery not found")
        return self._recovery_from_row(
            row,
            pending_approval=pending_view,
            claimed_decision=claimed_view,
        )

    def get_recovery_provenance(self, recovery_id: str) -> RecoveryProvenance:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM recoveries WHERE id = ?", (recovery_id,)
            ).fetchone()
        if row is None:
            raise RecoveryNotFoundError("Recovery not found")
        return self._provenance_from_row(row)

    def completed_receipt_for_execution(
        self,
        execution: DurableExecution,
    ) -> RecoveryReceipt:
        """Build a terminal receipt only from durable result and recovery provenance."""

        provenance = self.get_recovery_provenance(execution.recovery_id)
        return self._completed_receipt_for_execution(
            execution,
            provenance=provenance,
        )

    @staticmethod
    def _completed_receipt_for_execution(
        execution: DurableExecution,
        *,
        provenance: RecoveryProvenance,
    ) -> RecoveryReceipt:
        """Build a completed receipt from already-verified durable rows."""

        if execution.result_json is None or execution.remedy_digest is None:
            raise ValueError("Completed durable execution is missing receipt evidence")
        provider_result = execution.result_json.get("provider_result")
        if not isinstance(provider_result, str):
            raise ValueError("Completed durable execution has no provider result")
        if provenance.recovery_id != execution.recovery_id:
            raise ValueError("Execution provenance recovery ID mismatch")
        if provenance.execution_mode is ExecutionMode.REPLAY_FIXTURE:
            raise ValueError("Replay fixtures cannot create provider execution receipts")
        return RecoveryReceipt(
            recoveryId=execution.recovery_id,
            executionMode=provenance.execution_mode,
            status="completed",
            simulated=True,
            providerExecution=True,
            modelCall=provenance.model_call,
            modelIds=list(provenance.model_ids),
            rootTraceId=provenance.root_trace_id,
            sdkVersion=provenance.sdk_version,
            protocolVersion=provenance.protocol_version,
            agentGraphVersion=provenance.agent_graph_version,
            definitionDigest=provenance.definition_digest,
            boundary=(
                OPENAI_LIVE_BOUNDARY
                if provenance.execution_mode is ExecutionMode.OPENAI_LIVE
                else SDK_STUB_BOUNDARY
            ),
            providerResult=provider_result,
            authorizationSource="Approved Agents SDK commit_remedy interruption.",
            verificationResults=[
                "Immediate pre-execution remedy digest matched the approved digest.",
                "Demo provider dispatch returned confirmed.",
                "Provider result stored under one idempotency key.",
                ("Temporary provider-dispatch permission revoked after the approved execution."),
            ],
            approvedRemedyDigest=execution.remedy_digest,
        )

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
                reused_claim = self._verified_decision_claim_from_row(
                    reused_id,
                    recovery_id=recovery_id,
                )
                if (
                    cast(str, reused_id["recovery_id"]) != recovery_id
                    or cast(str, reused_id["request_fingerprint"]) != fingerprint
                ):
                    raise ApprovalDecisionError(
                        "decision_id_conflict", recovery_id, status_code=409
                    )
                if (
                    reused_claim.response is not None
                    and not self._completed_decision_has_terminal_evidence(
                        connection,
                        recovery_id=recovery_id,
                        decision_row=reused_id,
                        claim=reused_claim,
                    )
                    and not self._legacy_completed_decision_replay_is_compatible(
                        connection,
                        recovery_id=recovery_id,
                        claim=reused_claim,
                        decision_row=reused_id,
                    )
                ):
                    raise ApprovalDecisionError(
                        "resume_incompatible",
                        recovery_id,
                        status_code=409,
                    )
                return reused_claim

            existing = connection.execute(
                "SELECT * FROM approval_decisions WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone()
            if existing is not None:
                raise ApprovalDecisionError("already_decided", recovery_id, status_code=409)

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
            durable_claim = self._verified_decision_claim_from_row(
                row,
                recovery_id=claim.recovery_id,
            )
            if (
                durable_claim.request.client_decision_id != claim.request.client_decision_id
                or durable_claim.request_fingerprint != claim.request_fingerprint
            ):
                raise ApprovalDecisionError(
                    "decision_id_conflict", claim.recovery_id, status_code=409
                )
            if durable_claim.response is not None:
                if not self._completed_decision_has_terminal_evidence(
                    connection,
                    recovery_id=claim.recovery_id,
                    decision_row=row,
                    claim=durable_claim,
                ):
                    raise ApprovalDecisionError(
                        "resume_incompatible",
                        claim.recovery_id,
                        status_code=409,
                    )
                return durable_claim
            self._validate_consent_for_decision(
                connection,
                claim.recovery_id,
                claim.request,
                now=now,
            )
            return durable_claim

    def load_decision_claim_for_resume(
        self,
        recovery_id: str,
    ) -> ApprovalDecisionClaim:
        """Load the sole durable resume authority and run owner-safe preflight checks."""

        now = self._now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM approval_decisions WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone()
            if row is None:
                raise ApprovalDecisionError(
                    "decision_resume_unavailable",
                    recovery_id,
                    status_code=409,
                )
            claim = self._verified_decision_claim_from_row(
                row,
                recovery_id=recovery_id,
            )
            if claim.response is not None:
                if not self._completed_decision_has_terminal_evidence(
                    connection,
                    recovery_id=recovery_id,
                    decision_row=row,
                    claim=claim,
                ):
                    raise ApprovalDecisionError(
                        "resume_incompatible",
                        recovery_id,
                        status_code=409,
                    )
                return claim
            reconcilable = self._reconcilable_completed_claim(
                connection,
                recovery_id=recovery_id,
                decision_row=row,
            )
            if reconcilable is not None:
                return claim
            self._validate_consent_for_decision(
                connection,
                recovery_id,
                claim.request,
                now=now,
            )
            return claim

    def _reconcilable_completed_claim(
        self,
        connection: sqlite3.Connection,
        *,
        recovery_id: str,
        decision_row: sqlite3.Row,
    ) -> tuple[ApprovalDecisionClaim, DurableExecution] | None:
        """Recognize only the exact pre-expiry result awaiting an atomic seal."""

        if (
            cast(str, decision_row["status"]) != "claimed"
            or decision_row["result_json"] is not None
            or decision_row["completed_at"] is not None
        ):
            return None
        try:
            claim = self._verified_decision_claim_from_row(
                decision_row,
                recovery_id=recovery_id,
            )
            recovery, pending, consent = self._require_claim_evidence(
                connection,
                recovery_id,
                claim.request,
            )
            claimed_at = datetime.fromisoformat(cast(str, decision_row["claimed_at"]))
            execution_rows = connection.execute(
                "SELECT * FROM executions WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchall()
            remedy_status = connection.execute(
                """
                SELECT status FROM remedies
                WHERE recovery_id = ? AND id = ?
                """,
                (recovery_id, pending.remedy_id),
            ).fetchone()
            terminal_evidence = connection.execute(
                """
                SELECT 1 FROM receipts WHERE recovery_id = ?
                UNION ALL
                SELECT 1 FROM events
                WHERE recovery_id = ? AND terminal = 1
                LIMIT 1
                """,
                (recovery_id, recovery_id),
            ).fetchone()
        except (ApprovalDecisionError, TypeError, ValueError):
            return None
        if (
            claim.response is not None
            or claim.request.action is not DecisionAction.APPROVE
            or claimed_at.tzinfo is None
            or claimed_at.utcoffset() != timedelta(0)
            or claimed_at >= consent.expiry
        ):
            return None
        active_crash_state = (
            cast(str, recovery["status"]) == RecoveryStatus.PENDING_APPROVAL.value
            and pending.status == "approved"
            and remedy_status is not None
            and cast(str, remedy_status["status"]) == "pending"
            and terminal_evidence is None
            and self._claim_has_canonical_completed_execution(
                execution_rows,
                claim=claim,
                pending=pending,
                consent=consent,
                claimed_at=claimed_at,
            )
        )
        finalized_crash_state = self._approved_execution_has_terminal_evidence(
            connection,
            recovery=recovery,
            pending=pending,
            consent=consent,
            claim=claim,
            claimed_at=claimed_at,
            execution_rows=execution_rows,
        )
        if not active_crash_state and not finalized_crash_state:
            return None
        try:
            execution = self._execution_from_row(execution_rows[0])
        except (TypeError, ValueError):
            return None
        return claim, execution

    def _approved_execution_has_terminal_evidence(
        self,
        connection: sqlite3.Connection,
        *,
        recovery: sqlite3.Row,
        pending: PendingApprovalEnvelope,
        consent: RemedyConsentRecord,
        claim: ApprovalDecisionClaim,
        claimed_at: datetime,
        execution_rows: list[sqlite3.Row],
    ) -> bool:
        """Recognize an exact finalized execution whose response write was interrupted."""

        recovery_id = claim.recovery_id
        try:
            receipt_row = connection.execute(
                """
                SELECT receipt_json, created_at FROM receipts
                WHERE recovery_id = ?
                """,
                (recovery_id,),
            ).fetchone()
            terminal_rows = connection.execute(
                """
                SELECT type, data_json, created_at FROM events
                WHERE recovery_id = ? AND terminal = 1
                ORDER BY seq ASC
                """,
                (recovery_id,),
            ).fetchall()
            remedy_row = connection.execute(
                """
                SELECT status FROM remedies
                WHERE recovery_id = ? AND id = ?
                """,
                (recovery_id, pending.remedy_id),
            ).fetchone()
            if receipt_row is None or len(terminal_rows) != 1 or remedy_row is None:
                return False
            receipt = RecoveryReceipt.model_validate_json(cast(str, receipt_row["receipt_json"]))
            terminal_data = json.loads(cast(str, terminal_rows[0]["data_json"]))
            receipt_created_at = datetime.fromisoformat(cast(str, receipt_row["created_at"]))
            event_created_at = datetime.fromisoformat(cast(str, terminal_rows[0]["created_at"]))
            recovery_updated_at = datetime.fromisoformat(cast(str, recovery["updated_at"]))
            provenance = self._provenance_from_row(recovery)
            if not self._claim_has_exact_completed_execution(
                execution_rows,
                claim=claim,
                consent=consent,
                claimed_at=claimed_at,
                sealed_at=receipt_created_at,
            ):
                return False
            execution = self._execution_from_row(execution_rows[0])
            expected_receipt = self._completed_receipt_for_execution(
                execution,
                provenance=provenance,
            )
        except (TypeError, ValueError):
            return False
        expected_terminal_data: dict[str, JsonValue] = {
            "recoveryId": recovery_id,
            "executionMode": provenance.execution_mode.value,
            "providerExecution": True,
            "approvedRemedyDigest": claim.request.remedy_digest,
            "phase": "Verify & seal",
            "summary": "Committed demo-provider result finalized after restart.",
        }
        return (
            cast(str, recovery["status"]) == RecoveryStatus.COMPLETED.value
            and cast(int, recovery["current_step"]) == 5
            and cast(str, recovery["current_step_summary"])
            == "Demo provider result verified and receipt sealed."
            and pending.status == "completed"
            and cast(str, remedy_row["status"]) == "approved"
            and receipt == expected_receipt
            and cast(str, terminal_rows[0]["type"]) == "recovery.completed"
            and terminal_data == expected_terminal_data
            and receipt_created_at.tzinfo is not None
            and receipt_created_at.utcoffset() == timedelta(0)
            and event_created_at == receipt_created_at
            and recovery_updated_at.tzinfo is not None
            and recovery_updated_at.utcoffset() == timedelta(0)
            and recovery_updated_at >= receipt_created_at
        )

    def _completed_decision_has_terminal_evidence(
        self,
        connection: sqlite3.Connection,
        *,
        recovery_id: str,
        decision_row: sqlite3.Row,
        claim: ApprovalDecisionClaim,
    ) -> bool:
        """Accept a stored decision response only with its exact terminal ledger."""

        if (
            cast(str, decision_row["status"]) != "completed"
            or decision_row["result_json"] is None
            or decision_row["completed_at"] is None
            or claim.response is None
        ):
            return False
        try:
            recovery, pending, consent = self._require_claim_evidence(
                connection,
                recovery_id,
                claim.request,
            )
            provenance = self._provenance_from_row(recovery)
            receipt_row = connection.execute(
                """
                SELECT receipt_json, created_at FROM receipts
                WHERE recovery_id = ?
                """,
                (recovery_id,),
            ).fetchone()
            terminal_rows = connection.execute(
                """
                SELECT type, data_json, created_at FROM events
                WHERE recovery_id = ? AND terminal = 1
                ORDER BY seq ASC
                """,
                (recovery_id,),
            ).fetchall()
            remedy_row = connection.execute(
                """
                SELECT status FROM remedies
                WHERE recovery_id = ? AND id = ?
                """,
                (recovery_id, pending.remedy_id),
            ).fetchone()
            execution_rows = connection.execute(
                "SELECT * FROM executions WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchall()
            if receipt_row is None or len(terminal_rows) != 1 or remedy_row is None:
                return False
            receipt = RecoveryReceipt.model_validate_json(cast(str, receipt_row["receipt_json"]))
            terminal_data = json.loads(cast(str, terminal_rows[0]["data_json"]))
            claimed_at = datetime.fromisoformat(cast(str, decision_row["claimed_at"]))
            completed_at = datetime.fromisoformat(cast(str, decision_row["completed_at"]))
            receipt_created_at = datetime.fromisoformat(cast(str, receipt_row["created_at"]))
            event_created_at = datetime.fromisoformat(cast(str, terminal_rows[0]["created_at"]))
            recovery_updated_at = datetime.fromisoformat(cast(str, recovery["updated_at"]))
        except (ApprovalDecisionError, TypeError, ValueError):
            return False
        response = claim.response
        if (
            response.recovery_id != recovery_id
            or response.client_decision_id != claim.request.client_decision_id
            or response.action is not claim.request.action
            or receipt.recovery_id != recovery_id
            or receipt.execution_mode is not provenance.execution_mode
            or receipt.model_call != provenance.model_call
            or tuple(receipt.model_ids) != provenance.model_ids
            or receipt.root_trace_id != provenance.root_trace_id
            or receipt.sdk_version != provenance.sdk_version
            or receipt.protocol_version != provenance.protocol_version
            or receipt.agent_graph_version != provenance.agent_graph_version
            or receipt.definition_digest != provenance.definition_digest
            or receipt.status != response.status
            or cast(str, recovery["status"]) != response.status
            or cast(int, recovery["current_step"]) != 5
            or cast(str, terminal_rows[0]["type"]) != f"recovery.{response.status}"
            or not isinstance(terminal_data, dict)
            or terminal_data.get("recoveryId") != recovery_id
            or terminal_data.get("executionMode") != provenance.execution_mode.value
            or terminal_data.get("providerExecution") is not receipt.provider_execution
            or terminal_data.get("phase") != "Verify & seal"
            or claimed_at.tzinfo is None
            or claimed_at.utcoffset() != timedelta(0)
            or completed_at.tzinfo is None
            or completed_at.utcoffset() != timedelta(0)
            or receipt_created_at.tzinfo is None
            or receipt_created_at.utcoffset() != timedelta(0)
            or event_created_at.tzinfo is None
            or event_created_at.utcoffset() != timedelta(0)
            or recovery_updated_at.tzinfo is None
            or recovery_updated_at.utcoffset() != timedelta(0)
            or claimed_at >= consent.expiry
            or receipt_created_at != event_created_at
            or recovery_updated_at < receipt_created_at
            or completed_at < recovery_updated_at
        ):
            return False

        if claim.request.action is DecisionAction.APPROVE:
            if not self._claim_has_exact_completed_execution(
                execution_rows,
                claim=claim,
                consent=consent,
                claimed_at=claimed_at,
                sealed_at=receipt_created_at,
            ):
                return False
            try:
                execution = self._execution_from_row(execution_rows[0])
                expected_receipt = self._completed_receipt_for_execution(
                    execution,
                    provenance=provenance,
                )
            except (TypeError, ValueError):
                return False
            expected_terminal_data: dict[str, JsonValue] = {
                "recoveryId": recovery_id,
                "executionMode": receipt.execution_mode.value,
                "providerExecution": True,
                "approvedRemedyDigest": claim.request.remedy_digest,
                "phase": "Verify & seal",
                "summary": "Committed demo-provider result finalized after restart.",
            }
            return (
                response.status == "completed"
                and response.execution_started is True
                and response.approved_remedy_digest == claim.request.remedy_digest
                and pending.status == "completed"
                and cast(str, remedy_row["status"]) == "approved"
                and receipt == expected_receipt
                and terminal_data == expected_terminal_data
                and cast(str, recovery["current_step_summary"])
                == "Demo provider result verified and receipt sealed."
            )

        closed = response.status == "closed_without_action"
        unknown = response.status == "outcome_unknown"
        expected_summary = (
            "Exact remedy declined before provider dispatch."
            if closed
            else "Decline recorded; prior provider outcome remains unknown."
        )
        expected_terminal_data = {
            "recoveryId": recovery_id,
            "executionMode": receipt.execution_mode.value,
            "providerExecution": False if closed else None,
            "phase": "Verify & seal",
            "summary": expected_summary,
        }
        if closed:
            expected_terminal_data["executionCount"] = 0
        expected_verification_results = (
            (
                "Human consent requested.",
                "Remedy declined by operator.",
                "Exact interruption rejected.",
                "No replacement action selected.",
                "Execution count is zero.",
                "Provider dispatch did not begin.",
                "Temporary permission revoked.",
                "Cancellation receipt sealed.",
            )
            if closed
            else (
                "Exact interruption rejected.",
                "No replacement remedy selected.",
                "Prior execution or dispatch evidence detected.",
                "Outcome marked unknown instead of cancelled.",
                "Temporary permission revoked.",
                "Uncertain-outcome receipt sealed.",
            )
        )
        return (
            (closed or unknown)
            and response.approved_remedy_digest is None
            and response.execution_started is (False if closed else None)
            and pending.status == ("rejected" if closed else "outcome_unknown")
            and cast(str, remedy_row["status"]) == ("declined" if closed else "outcome_unknown")
            and (not execution_rows if closed else True)
            and receipt.approval_count == 0
            and receipt.approved_remedy_digest is None
            and receipt.provider_execution is (False if closed else None)
            and receipt.provider_result
            == (
                "Provider dispatch did not begin."
                if closed
                else "Provider dispatch may have begun; its outcome is unknown."
            )
            and receipt.authorization_source
            == (
                "User declined the exact Agents SDK commit_remedy interruption."
                if closed
                else (
                    "Decline arrived after execution or dispatch may have begun; "
                    "cancellation was not claimed."
                )
            )
            and tuple(receipt.verification_results) == expected_verification_results
            and terminal_data == expected_terminal_data
            and cast(str, recovery["current_step_summary"]) == expected_summary
        )

    @staticmethod
    def _legacy_completed_decision_replay_is_compatible(
        connection: sqlite3.Connection,
        *,
        recovery_id: str,
        claim: ApprovalDecisionClaim,
        decision_row: sqlite3.Row,
    ) -> bool:
        """Preserve the one explicit Task 6 approval-only migration contract."""

        if (
            cast(str, decision_row["status"]) != "completed"
            or decision_row["result_json"] is None
            or decision_row["completed_at"] is None
            or claim.response is None
            or claim.request.action is not DecisionAction.APPROVE
            or claim.response.action is not DecisionAction.APPROVE
            or claim.response.status != "completed"
            or claim.response.execution_started is not True
            or claim.response.client_decision_id != claim.request.client_decision_id
            or claim.response.recovery_id != recovery_id
            or claim.response.approved_remedy_digest != claim.request.remedy_digest
        ):
            return False
        row = connection.execute(
            """
            SELECT recoveries.*,
                   (SELECT COUNT(*) FROM pending_approvals
                    WHERE recovery_id = recoveries.id) AS pending_count,
                   (SELECT COUNT(*) FROM remedies
                    WHERE recovery_id = recoveries.id) AS remedy_count,
                   (SELECT COUNT(*) FROM executions
                    WHERE recovery_id = recoveries.id) AS execution_count,
                   (SELECT COUNT(*) FROM receipts
                    WHERE recovery_id = recoveries.id) AS receipt_count,
                   (SELECT COUNT(*) FROM events
                    WHERE recovery_id = recoveries.id AND terminal = 1)
                    AS terminal_count
            FROM recoveries
            WHERE recoveries.id = ?
            """,
            (recovery_id,),
        ).fetchone()
        return bool(
            row is not None
            and cast(str, row["scenario_id"]) == ScenarioId.HOTEL.value
            and cast(str, row["execution_mode"]) == ExecutionMode.SDK_STUB.value
            and cast(str, row["status"]) == RecoveryStatus.COMPLETED.value
            and cast(int, row["current_step"]) == 5
            and cast(str, row["current_step_summary"]) == "Legacy approval completed."
            and cast(int, row["pending_count"]) == 0
            and cast(int, row["remedy_count"]) == 0
            and cast(int, row["execution_count"]) == 0
            and cast(int, row["receipt_count"]) == 0
            and cast(int, row["terminal_count"]) == 0
        )

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
                raise ApprovalDecisionError("decision_unavailable", recovery_id, status_code=409)
            claim = self._verified_decision_claim_from_row(
                row,
                recovery_id=recovery_id,
            )
            if (
                claim.request.action is not DecisionAction.APPROVE
                or claim.request.tool_call_id != tool_call_id
                or claim.request.remedy_digest != remedy_digest
            ):
                raise ApprovalDecisionError("decision_id_conflict", recovery_id, status_code=409)
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
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._complete_approval_decision_in_connection(
                connection,
                claim=claim,
                response=response,
                now=now,
            )

    def _complete_approval_decision_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        claim: ApprovalDecisionClaim,
        response: ApprovalDecisionResponse,
        now: datetime,
    ) -> ApprovalDecisionResponse:
        """Complete one exact approve claim inside the caller's transaction."""

        if (
            claim.request.action is not DecisionAction.APPROVE
            or response.action is not DecisionAction.APPROVE
            or response.recovery_id != claim.recovery_id
            or response.client_decision_id != claim.request.client_decision_id
            or response.approved_remedy_digest != claim.request.remedy_digest
        ):
            raise ApprovalDecisionError(
                "decision_id_conflict",
                claim.recovery_id,
                status_code=409,
            )
        row = connection.execute(
            "SELECT * FROM approval_decisions WHERE recovery_id = ?",
            (claim.recovery_id,),
        ).fetchone()
        if row is None:
            raise ApprovalDecisionError("decision_unavailable", claim.recovery_id, status_code=409)
        durable_claim = self._verified_decision_claim_from_row(
            row,
            recovery_id=claim.recovery_id,
        )
        if (
            durable_claim.request != claim.request
            or durable_claim.request_fingerprint != claim.request_fingerprint
        ):
            raise ApprovalDecisionError("decision_id_conflict", claim.recovery_id, status_code=409)
        if durable_claim.response is not None:
            if not self._completed_decision_has_terminal_evidence(
                connection,
                recovery_id=claim.recovery_id,
                decision_row=row,
                claim=durable_claim,
            ):
                raise ApprovalDecisionError(
                    "resume_incompatible",
                    claim.recovery_id,
                    status_code=409,
                )
            return durable_claim.response
        serialized = response.model_dump_json(by_alias=True)
        updated = connection.execute(
            """
            UPDATE approval_decisions
            SET status = 'completed', result_json = ?, completed_at = ?
            WHERE recovery_id = ?
              AND status = 'claimed'
              AND result_json IS NULL
              AND completed_at IS NULL
            """,
            (serialized, now.isoformat(), claim.recovery_id),
        )
        if updated.rowcount != 1:
            raise ApprovalDecisionError("decision_unavailable", claim.recovery_id, status_code=409)
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
            durable_claim = self._verified_decision_claim_from_row(
                row,
                recovery_id=claim.recovery_id,
            )
            if (
                durable_claim.request.client_decision_id != claim.request.client_decision_id
                or durable_claim.request_fingerprint != claim.request_fingerprint
                or durable_claim.request.action is not DecisionAction.DECLINE
            ):
                raise ApprovalDecisionError(
                    "decision_id_conflict", claim.recovery_id, status_code=409
                )
            if durable_claim.response is not None:
                if not self._completed_decision_has_terminal_evidence(
                    connection,
                    recovery_id=claim.recovery_id,
                    decision_row=row,
                    claim=durable_claim,
                ):
                    raise ApprovalDecisionError(
                        "resume_incompatible",
                        claim.recovery_id,
                        status_code=409,
                    )
                return durable_claim.response

            recovery = connection.execute(
                "SELECT * FROM recoveries WHERE id = ?",
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
                or cast(str, recovery["status"]) != RecoveryStatus.PENDING_APPROVAL.value
            )
            decision_status: Literal["closed_without_action", "outcome_unknown"] = (
                "outcome_unknown" if may_have_begun else "closed_without_action"
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
                    "Human consent requested.",
                    "Remedy declined by operator.",
                    "Exact interruption rejected.",
                    "No replacement action selected.",
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

            provenance = self._provenance_from_row(recovery)
            receipt = RecoveryReceipt(
                recoveryId=claim.recovery_id,
                executionMode=provenance.execution_mode,
                status=decision_status,
                simulated=True,
                providerExecution=None if may_have_begun else False,
                modelCall=provenance.model_call,
                modelIds=list(provenance.model_ids),
                rootTraceId=provenance.root_trace_id,
                sdkVersion=provenance.sdk_version,
                protocolVersion=provenance.protocol_version,
                agentGraphVersion=provenance.agent_graph_version,
                definitionDigest=provenance.definition_digest,
                boundary=(
                    OPENAI_LIVE_BOUNDARY
                    if provenance.execution_mode is ExecutionMode.OPENAI_LIVE
                    else SDK_STUB_BOUNDARY
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
                "providerExecution": receipt.provider_execution,
                "phase": "Verify & seal",
                "summary": summary,
            }
            if not may_have_begun:
                terminal_data["executionCount"] = len(execution_rows)
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

    def recovery_is_accessible(self, recovery_id: str, session_key: str) -> bool:
        """Return only whether this opaque session owns access to one recovery."""

        self._require_opaque_session_key(session_key)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM recovery_access
                WHERE recovery_id = ? AND session_key = ?
                """,
                (recovery_id, session_key),
            ).fetchone()
        return row is not None

    @staticmethod
    def _claim_expiry_authorization_source(
        action: DecisionAction,
        *,
        closed: bool,
    ) -> str:
        if action is DecisionAction.DECLINE and closed:
            return CLAIM_EXPIRY_CLOSED_AUTHORIZATION_SOURCE
        return CLAIM_EXPIRY_UNKNOWN_AUTHORIZATION_SOURCE

    @classmethod
    def _claim_expiry_receipt(
        cls,
        *,
        recovery_id: str,
        provenance: RecoveryProvenance,
        action: DecisionAction,
        status: Literal["closed_without_action", "outcome_unknown"],
    ) -> RecoveryReceipt:
        closed = status == RecoveryStatus.CLOSED_WITHOUT_ACTION.value
        return RecoveryReceipt(
            recoveryId=recovery_id,
            executionMode=provenance.execution_mode,
            status=status,
            simulated=True,
            providerExecution=False if closed else None,
            modelCall=provenance.model_call,
            modelIds=list(provenance.model_ids),
            rootTraceId=provenance.root_trace_id,
            sdkVersion=provenance.sdk_version,
            protocolVersion=provenance.protocol_version,
            agentGraphVersion=provenance.agent_graph_version,
            definitionDigest=provenance.definition_digest,
            boundary=(
                OPENAI_LIVE_BOUNDARY
                if provenance.execution_mode is ExecutionMode.OPENAI_LIVE
                else SDK_STUB_BOUNDARY
            ),
            providerResult=(
                CLAIM_EXPIRY_CLOSED_PROVIDER_RESULT
                if closed
                else CLAIM_EXPIRY_UNKNOWN_PROVIDER_RESULT
            ),
            authorizationSource=cls._claim_expiry_authorization_source(
                action,
                closed=closed,
            ),
            verificationResults=list(
                CLAIM_EXPIRY_CLOSED_VERIFICATION_RESULTS
                if closed
                else CLAIM_EXPIRY_UNKNOWN_VERIFICATION_RESULTS
            ),
            approvalCount=int(action is DecisionAction.APPROVE),
            approvedRemedyDigest=None,
        )

    @staticmethod
    def _claim_expiry_event_data(
        *,
        recovery_id: str,
        receipt: RecoveryReceipt,
        action: DecisionAction,
    ) -> dict[str, JsonValue]:
        closed = receipt.status == RecoveryStatus.CLOSED_WITHOUT_ACTION.value
        event_data: dict[str, JsonValue] = {
            "recoveryId": recovery_id,
            "executionMode": receipt.execution_mode.value,
            "providerExecution": receipt.provider_execution,
            "approvalDecisionCount": 1,
            "decisionAction": action.value,
            "phase": "Verify & seal",
            "summary": (CLAIM_EXPIRY_CLOSED_SUMMARY if closed else CLAIM_EXPIRY_UNKNOWN_SUMMARY),
        }
        if closed:
            event_data["executionCount"] = 0
        return event_data

    def _recovery_has_expiration_evidence(
        self,
        connection: sqlite3.Connection,
        recovery_id: str,
    ) -> bool:
        recovery_row = connection.execute(
            "SELECT * FROM recoveries WHERE id = ?",
            (recovery_id,),
        ).fetchone()
        if recovery_row is None:
            return False
        try:
            scenario_id = ScenarioId(cast(str, recovery_row["scenario_id"]))
            execution_mode = ExecutionMode(cast(str, recovery_row["execution_mode"]))
            recovery_created_at = datetime.fromisoformat(cast(str, recovery_row["created_at"]))
            recovery_updated_at = datetime.fromisoformat(cast(str, recovery_row["updated_at"]))
            events = self._validated_public_event_ledger_in_connection(
                connection,
                recovery_id=recovery_id,
                scenario_id=scenario_id,
                execution_mode=execution_mode,
                recovery_created_at=recovery_created_at,
                recovery_updated_at=recovery_updated_at,
            )
        except (PublicEvidenceIntegrityError, TypeError, ValueError):
            return False
        approval_evidence = self._hotel_approval_evidence(
            connection,
            row=recovery_row,
            events=events,
        )
        if len(events) != 3 or approval_evidence is None:
            return False

        rows = connection.execute(
            """
            SELECT
                recoveries.*,
                pending_approvals.status AS pending_status,
                remedies.status AS remedy_status,
                receipts.receipt_json AS receipt_json,
                receipts.created_at AS receipt_created_at,
                events.type AS event_type,
                events.data_json AS event_data_json,
                events.created_at AS event_created_at,
                (
                    SELECT COUNT(*) FROM events AS terminal_events
                    WHERE terminal_events.recovery_id = recoveries.id
                      AND terminal_events.terminal = 1
                ) AS terminal_count,
                (
                    SELECT COUNT(*) FROM approval_decisions
                    WHERE approval_decisions.recovery_id = recoveries.id
                ) AS decision_count,
                (
                    SELECT COUNT(*) FROM executions
                    WHERE executions.recovery_id = recoveries.id
                ) AS execution_count,
                (
                    SELECT COUNT(*) FROM live_admissions
                    WHERE live_admissions.recovery_id = recoveries.id
                      AND live_admissions.released_at IS NULL
                ) AS active_live_admission_count
            FROM recoveries
            JOIN pending_approvals
              ON pending_approvals.recovery_id = recoveries.id
            JOIN remedies
              ON remedies.recovery_id = recoveries.id
             AND remedies.id = pending_approvals.remedy_id
            JOIN receipts
              ON receipts.recovery_id = recoveries.id
            JOIN events
              ON events.recovery_id = recoveries.id
             AND events.terminal = 1
            WHERE recoveries.id = ?
            """,
            (recovery_id,),
        ).fetchall()
        if len(rows) != 1:
            return False
        row = rows[0]
        if (
            cast(str, row["execution_mode"])
            not in {ExecutionMode.SDK_STUB.value, ExecutionMode.OPENAI_LIVE.value}
            or cast(int, row["terminal_count"]) != 1
            or cast(int, row["active_live_admission_count"]) != 0
            or cast(str, row["updated_at"]) != cast(str, row["receipt_created_at"])
            or cast(str, row["receipt_created_at"]) != cast(str, row["event_created_at"])
        ):
            return False
        try:
            receipt = RecoveryReceipt.model_validate_json(cast(str, row["receipt_json"]))
            event_data = json.loads(cast(str, row["event_data_json"]))
            seal_at = datetime.fromisoformat(cast(str, row["receipt_created_at"]))
        except (TypeError, ValueError):
            return False
        if (
            seal_at.tzinfo is None
            or seal_at.utcoffset() != timedelta(0)
            or seal_at < approval_evidence.consent.expiry
            or approval_evidence.pending_updated_at != seal_at
        ):
            return False

        if cast(str, row["event_type"]) == "recovery.expired":
            expected_event_data: dict[str, JsonValue] = {
                "recoveryId": recovery_id,
                "executionMode": receipt.execution_mode.value,
                "providerExecution": False,
                "approvalDecisionCount": 0,
                "executionCount": 0,
                "phase": "Verify & seal",
                "summary": EXPIRATION_SUMMARY,
            }
            return (
                cast(str, row["status"]) == RecoveryStatus.CLOSED_WITHOUT_ACTION.value
                and cast(int, row["current_step"]) == 5
                and cast(str, row["current_step_summary"]) == EXPIRATION_SUMMARY
                and cast(str, row["pending_status"]) == "expired"
                and cast(str, row["remedy_status"]) == "expired"
                and approval_evidence.claim is None
                and cast(int, row["decision_count"]) == 0
                and cast(int, row["execution_count"]) == 0
                and receipt.recovery_id == recovery_id
                and receipt.execution_mode.value == cast(str, row["execution_mode"])
                and receipt.status == "closed_without_action"
                and receipt.simulated is True
                and receipt.provider_execution is False
                and receipt.approval_count == 0
                and receipt.approved_remedy_digest is None
                and receipt.provider_result == "Provider dispatch did not begin."
                and receipt.authorization_source
                == "Consent window expired before an approval decision."
                and tuple(receipt.verification_results) == EXPIRATION_VERIFICATION_RESULTS
                and event_data == expected_event_data
            )

        if (
            cast(str, row["event_type"]) != "recovery.claim_expired"
            or cast(int, row["decision_count"]) != 1
        ):
            return False
        decision_row = connection.execute(
            "SELECT * FROM approval_decisions WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone()
        if (
            decision_row is None
            or cast(str, decision_row["status"]) != "claimed"
            or decision_row["result_json"] is not None
            or decision_row["completed_at"] is not None
        ):
            return False
        try:
            claim = self._verified_decision_claim_from_row(
                decision_row,
                recovery_id=recovery_id,
            )
            recovery, _pending, consent = self._require_claim_evidence(
                connection,
                recovery_id,
                claim.request,
            )
            receipt_created_at = datetime.fromisoformat(cast(str, row["receipt_created_at"]))
            event_created_at = datetime.fromisoformat(cast(str, row["event_created_at"]))
            claimed_at = datetime.fromisoformat(cast(str, decision_row["claimed_at"]))
            provenance = self._provenance_from_row(recovery)
        except (ApprovalDecisionError, TypeError, ValueError):
            return False
        if (
            claim.response is not None
            or receipt_created_at != event_created_at
            or receipt_created_at.tzinfo is None
            or receipt_created_at.utcoffset() != timedelta(0)
            or receipt_created_at < consent.expiry
            or claimed_at.tzinfo is None
            or claimed_at.utcoffset() != timedelta(0)
            or claimed_at >= consent.expiry
        ):
            return False

        closed = cast(str, row["status"]) == RecoveryStatus.CLOSED_WITHOUT_ACTION.value
        unknown = cast(str, row["status"]) == RecoveryStatus.OUTCOME_UNKNOWN.value
        if claim.request.action is DecisionAction.APPROVE:
            exact_terminal_state = (
                unknown
                and cast(int, row["current_step"]) == 5
                and cast(str, row["current_step_summary"]) == CLAIM_EXPIRY_UNKNOWN_SUMMARY
                and cast(str, row["pending_status"]) == "outcome_unknown"
                and cast(str, row["remedy_status"]) == "outcome_unknown"
            )
        else:
            exact_terminal_state = (
                closed
                and cast(int, row["current_step"]) == 5
                and cast(str, row["current_step_summary"]) == CLAIM_EXPIRY_CLOSED_SUMMARY
                and cast(str, row["pending_status"]) == "rejected"
                and cast(str, row["remedy_status"]) == "declined"
                and cast(int, row["execution_count"]) == 0
            ) or (
                unknown
                and cast(int, row["current_step"]) == 5
                and cast(str, row["current_step_summary"]) == CLAIM_EXPIRY_UNKNOWN_SUMMARY
                and cast(str, row["pending_status"]) == "outcome_unknown"
                and cast(str, row["remedy_status"]) == "outcome_unknown"
            )
        if not exact_terminal_state:
            return False
        expected_status: Literal["closed_without_action", "outcome_unknown"] = (
            "closed_without_action" if closed else "outcome_unknown"
        )
        expected_receipt = self._claim_expiry_receipt(
            recovery_id=recovery_id,
            provenance=provenance,
            action=claim.request.action,
            status=expected_status,
        )
        return receipt == expected_receipt and event_data == self._claim_expiry_event_data(
            recovery_id=recovery_id,
            receipt=expected_receipt,
            action=claim.request.action,
        )

    def recovery_has_expiration_evidence(self, recovery_id: str) -> bool:
        """Recognize only the exact durable terminal evidence authored by expiry."""

        with self._lock, self._connect() as connection:
            return self._recovery_has_expiration_evidence(connection, recovery_id)

    def create_demo_session(
        self,
        session_key: str,
        *,
        created_at: datetime,
        expires_at: datetime,
    ) -> None:
        if expires_at <= created_at:
            raise ValueError("Demo session expiry must follow creation")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                DELETE FROM demo_sessions
                WHERE id IN (
                    SELECT id FROM demo_sessions
                    WHERE expires_at <= ?
                    ORDER BY expires_at ASC
                    LIMIT 100
                )
                """,
                (created_at.isoformat(),),
            )
            connection.execute(
                """
                INSERT INTO demo_sessions (id, created_at, expires_at)
                VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    expires_at = excluded.expires_at
                """,
                (session_key, created_at.isoformat(), expires_at.isoformat()),
            )

    def demo_session_is_active(self, session_key: str, *, now: datetime) -> bool:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM demo_sessions
                WHERE id = ? AND expires_at > ?
                """,
                (session_key, now.isoformat()),
            ).fetchone()
        return row is not None

    def try_admit_live_recovery(
        self,
        *,
        recovery_id: str,
        ip_key: str,
        session_key: str,
        max_active: int,
        ip_cooldown: timedelta,
        session_cooldown: timedelta,
        daily_budget_units: int,
        lease_ttl: timedelta,
        now: datetime,
    ) -> Literal["live_capacity", "cooldown", "daily_budget"] | None:
        """Atomically reserve one durable live-demo unit across app instances."""

        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("Admission time must be timezone-aware UTC")
        if (
            max_active <= 0
            or daily_budget_units <= 0
            or ip_cooldown <= timedelta(0)
            or session_cooldown <= timedelta(0)
            or lease_ttl <= timedelta(0)
        ):
            raise ValueError("Admission policy must contain positive limits")

        now_text = now.isoformat()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        next_day = day_start + timedelta(days=1)
        ip_cutoff = now - ip_cooldown
        session_cutoff = now - session_cooldown
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE live_admissions
                SET released_at = ?
                WHERE released_at IS NULL
                  AND (
                    expires_at <= ?
                    OR EXISTS (
                        SELECT 1 FROM recoveries
                        WHERE recoveries.id = live_admissions.recovery_id
                          AND recoveries.status IN (
                            'completed', 'closed_without_action', 'outcome_unknown'
                          )
                    )
                  )
                """,
                (now_text, now_text),
            )
            connection.execute(
                """
                DELETE FROM live_admissions
                WHERE recovery_id IN (
                    SELECT recovery_id FROM live_admissions
                    WHERE released_at IS NOT NULL
                      AND admitted_at < ?
                      AND NOT EXISTS (
                          SELECT 1 FROM recoveries
                          WHERE recoveries.id = live_admissions.recovery_id
                            AND recoveries.status NOT IN (
                                'completed', 'closed_without_action', 'outcome_unknown'
                            )
                      )
                    ORDER BY admitted_at ASC
                    LIMIT 100
                )
                """,
                (min(ip_cutoff, session_cutoff).isoformat(),),
            )

            active_count = cast(
                int,
                connection.execute(
                    """
                    SELECT COUNT(*) FROM live_admissions
                    WHERE released_at IS NULL AND expires_at > ?
                    """,
                    (now_text,),
                ).fetchone()[0],
            )
            if active_count >= max_active:
                return "live_capacity"

            used_units = cast(
                int,
                connection.execute(
                    """
                    SELECT COALESCE(SUM(amount), 0) FROM usage_ledger
                    WHERE category = 'live_demo_budget_unit'
                      AND recorded_at >= ? AND recorded_at < ?
                    """,
                    (day_start.isoformat(), next_day.isoformat()),
                ).fetchone()[0],
            )
            if used_units + 1 > daily_budget_units:
                return "daily_budget"

            recent_ip = connection.execute(
                """
                SELECT 1 FROM live_admissions
                WHERE ip_key = ? AND admitted_at > ?
                LIMIT 1
                """,
                (ip_key, ip_cutoff.isoformat()),
            ).fetchone()
            recent_session = connection.execute(
                """
                SELECT 1 FROM live_admissions
                WHERE session_key = ? AND admitted_at > ?
                LIMIT 1
                """,
                (session_key, session_cutoff.isoformat()),
            ).fetchone()
            if recent_ip is not None or recent_session is not None:
                return "cooldown"

            connection.execute(
                """
                INSERT INTO live_admissions (
                    recovery_id, ip_key, session_key, budget_units,
                    admitted_at, expires_at, released_at
                ) VALUES (?, ?, ?, 1, ?, ?, NULL)
                """,
                (
                    recovery_id,
                    ip_key,
                    session_key,
                    now_text,
                    (now + lease_ttl).isoformat(),
                ),
            )
            connection.execute(
                """
                INSERT INTO usage_ledger (
                    recovery_id, category, amount, recorded_at
                ) VALUES (?, 'live_demo_budget_unit', 1, ?)
                """,
                (recovery_id, now_text),
            )
        return None

    def renew_live_admission_lease(
        self,
        recovery_id: str,
        *,
        lease_ttl: timedelta,
        now: datetime,
    ) -> bool:
        """Renew one exact active row without requiring a persisted recovery yet."""

        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("Admission time must be timezone-aware UTC")
        if lease_ttl <= timedelta(0):
            raise ValueError("Live lease policy must contain positive limits")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE live_admissions
                SET expires_at = ?
                WHERE recovery_id = ?
                  AND released_at IS NULL
                  AND expires_at > ?
                """,
                (
                    (now + lease_ttl).isoformat(),
                    recovery_id,
                    now.isoformat(),
                ),
            )
        return updated.rowcount == 1

    def renew_or_reacquire_live_admission(
        self,
        recovery_id: str,
        *,
        max_active: int,
        lease_ttl: timedelta,
        now: datetime,
    ) -> bool:
        """Renew one live lease, or atomically reacquire it without rebilling."""

        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("Admission time must be timezone-aware UTC")
        if max_active <= 0 or lease_ttl <= timedelta(0):
            raise ValueError("Live lease policy must contain positive limits")
        now_text = now.isoformat()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE live_admissions
                SET released_at = ?
                WHERE released_at IS NULL
                  AND expires_at <= ?
                """,
                (now_text, now_text),
            )
            row = connection.execute(
                """
                SELECT live_admissions.released_at, recoveries.status,
                       recoveries.execution_mode
                FROM live_admissions
                JOIN recoveries ON recoveries.id = live_admissions.recovery_id
                WHERE live_admissions.recovery_id = ?
                """,
                (recovery_id,),
            ).fetchone()
            if (
                row is None
                or cast(str, row["execution_mode"]) != "openai_live"
                or cast(str, row["status"])
                in {"completed", "closed_without_action", "outcome_unknown"}
            ):
                return False
            if row["released_at"] is None:
                connection.execute(
                    "UPDATE live_admissions SET expires_at = ? WHERE recovery_id = ?",
                    ((now + lease_ttl).isoformat(), recovery_id),
                )
                return True
            active_count = cast(
                int,
                connection.execute(
                    """
                    SELECT COUNT(*) FROM live_admissions
                    WHERE released_at IS NULL AND expires_at > ?
                    """,
                    (now_text,),
                ).fetchone()[0],
            )
            if active_count >= max_active:
                return False
            updated = connection.execute(
                """
                UPDATE live_admissions
                SET expires_at = ?, released_at = NULL
                WHERE recovery_id = ?
                """,
                ((now + lease_ttl).isoformat(), recovery_id),
            )
            return updated.rowcount == 1

    def release_live_admission(self, recovery_id: str, *, now: datetime) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE live_admissions
                SET released_at = COALESCE(released_at, ?)
                WHERE recovery_id = ?
                """,
                (now.isoformat(), recovery_id),
            )

    @classmethod
    def _claim_has_canonical_completed_execution(
        cls,
        rows: list[sqlite3.Row],
        *,
        claim: ApprovalDecisionClaim,
        pending: PendingApprovalEnvelope,
        consent: RemedyConsentRecord,
        claimed_at: datetime,
    ) -> bool:
        """Leave only one exact committed approve result for startup reconciliation."""

        if pending.status != "approved":
            return False
        return cls._claim_has_exact_completed_execution(
            rows,
            claim=claim,
            consent=consent,
            claimed_at=claimed_at,
        )

    @classmethod
    def _claim_has_exact_completed_execution(
        cls,
        rows: list[sqlite3.Row],
        *,
        claim: ApprovalDecisionClaim,
        consent: RemedyConsentRecord,
        claimed_at: datetime,
        sealed_at: datetime | None = None,
    ) -> bool:
        """Recognize one exact provider result written under active consent."""

        if (
            claim.request.action is not DecisionAction.APPROVE
            or len(rows) != 1
            or cls._execution_persistence_shape(rows[0]) != "completed"
        ):
            return False
        try:
            execution = cls._execution_from_row(rows[0])
            created_at = datetime.fromisoformat(cast(str, rows[0]["created_at"]))
            updated_at = datetime.fromisoformat(cast(str, rows[0]["updated_at"]))
        except (TypeError, ValueError):
            return False
        result = execution.result_json
        request_payload = {
            "recovery_id": claim.recovery_id,
            "remedy": consent.evidence.remedy.model_dump(mode="json"),
        }
        expected_request_digest = hashlib.sha256(
            json.dumps(
                request_payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        return (
            execution.recovery_id == claim.recovery_id
            and execution.idempotency_key
            == (f"{claim.recovery_id}:{claim.request.tool_call_id}:{claim.request.remedy_digest}")
            and execution.request_digest == expected_request_digest
            and execution.tool_call_id == claim.request.tool_call_id
            and execution.remedy_digest == claim.request.remedy_digest
            and result is not None
            and set(result) == {"dispatch_id", "status", "simulated", "provider_result"}
            and isinstance(result["dispatch_id"], str)
            and bool(result["dispatch_id"])
            and result["status"] == "confirmed"
            and result["simulated"] is True
            and isinstance(result["provider_result"], str)
            and bool(result["provider_result"])
            and created_at.tzinfo is not None
            and created_at.utcoffset() == timedelta(0)
            and updated_at.tzinfo is not None
            and updated_at.utcoffset() == timedelta(0)
            and claimed_at.tzinfo is not None
            and claimed_at.utcoffset() == timedelta(0)
            and claimed_at <= created_at <= updated_at < consent.expiry
            and (sealed_at is None or updated_at <= sealed_at)
        )

    @staticmethod
    def _expiry_scan_cursor(
        connection: sqlite3.Connection,
        candidate_kind: Literal["oldest", "claimed", "untouched"],
    ) -> tuple[str, str] | None:
        row = connection.execute(
            """
            SELECT last_expiry, last_recovery_id
            FROM expiry_scan_state
            WHERE candidate_kind = ?
            """,
            (candidate_kind,),
        ).fetchone()
        if row is None:
            return None
        return cast(str, row["last_expiry"]), cast(str, row["last_recovery_id"])

    @staticmethod
    def _set_expiry_scan_cursor(
        connection: sqlite3.Connection,
        candidate_kind: Literal["oldest", "claimed", "untouched"],
        cursor: tuple[str, str] | None,
    ) -> None:
        if cursor is None:
            connection.execute(
                "DELETE FROM expiry_scan_state WHERE candidate_kind = ?",
                (candidate_kind,),
            )
            return
        connection.execute(
            """
            INSERT INTO expiry_scan_state (
                candidate_kind, last_expiry, last_recovery_id
            ) VALUES (?, ?, ?)
            ON CONFLICT(candidate_kind) DO UPDATE SET
                last_expiry = excluded.last_expiry,
                last_recovery_id = excluded.last_recovery_id
            """,
            (candidate_kind, cursor[0], cursor[1]),
        )

    def oldest_expiry_candidate_kind(
        self,
        *,
        now: datetime,
    ) -> Literal["claimed", "untouched"] | None:
        """Return the oldest parseable expiry class for fair bounded cleanup."""

        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("now must be timezone-aware UTC")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = self._expiry_scan_cursor(connection, "oldest")
            cursor_clause = ""
            parameters: list[str | int] = []
            if cursor is not None:
                cursor_clause = """
                  AND (
                      remedies.expiry > ?
                      OR (
                          remedies.expiry = ?
                          AND recoveries.id > ?
                      )
                  )
                """
                parameters.extend((cursor[0], cursor[0], cursor[1]))
            parameters.append(MAX_STORE_EXPIRY_BATCH_SIZE)
            rows = connection.execute(
                f"""
                SELECT
                    recoveries.id AS recovery_id,
                    remedies.expiry AS remedy_expiry,
                    CASE
                        WHEN EXISTS (
                            SELECT 1 FROM approval_decisions
                            WHERE approval_decisions.recovery_id = recoveries.id
                              AND approval_decisions.status = 'claimed'
                              AND approval_decisions.result_json IS NULL
                              AND approval_decisions.completed_at IS NULL
                        )
                        THEN 'claimed'
                        ELSE 'untouched'
                    END AS candidate_kind
                FROM recoveries
                JOIN pending_approvals
                  ON pending_approvals.recovery_id = recoveries.id
                JOIN remedies
                  ON remedies.recovery_id = recoveries.id
                 AND remedies.id = pending_approvals.remedy_id
                WHERE recoveries.status = 'pending_approval'
                  AND recoveries.scenario_id = 'hotel'
                  AND recoveries.execution_mode IN ('sdk_stub', 'openai_live')
                  AND remedies.status = 'pending'
                  AND remedies.expiry IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM receipts
                      WHERE receipts.recovery_id = recoveries.id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM events
                      WHERE events.recovery_id = recoveries.id
                        AND events.terminal = 1
                  )
                  AND (
                      (
                          pending_approvals.status IN (
                              'pending', 'approved', 'outcome_unknown'
                          )
                          AND EXISTS (
                              SELECT 1 FROM approval_decisions
                              WHERE approval_decisions.recovery_id = recoveries.id
                                AND approval_decisions.status = 'claimed'
                                AND approval_decisions.result_json IS NULL
                                AND approval_decisions.completed_at IS NULL
                          )
                      )
                      OR
                      (
                          pending_approvals.status = 'pending'
                          AND NOT EXISTS (
                              SELECT 1 FROM approval_decisions
                              WHERE approval_decisions.recovery_id = recoveries.id
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM executions
                              WHERE executions.recovery_id = recoveries.id
                          )
                      )
                  )
                  {cursor_clause}
                ORDER BY remedies.expiry ASC, recoveries.id ASC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
            selected_kind: Literal["claimed", "untouched"] | None = None
            last_scanned: tuple[str, str] | None = None
            for row in rows:
                raw_expiry = cast(str, row["remedy_expiry"])
                recovery_id = cast(str, row["recovery_id"])
                last_scanned = raw_expiry, recovery_id
                try:
                    expiry = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
                except (TypeError, ValueError):
                    continue
                if expiry.tzinfo is None or expiry.utcoffset() != timedelta(0) or expiry > now:
                    continue
                candidate_kind = cast(str, row["candidate_kind"])
                if candidate_kind in {"claimed", "untouched"}:
                    selected_kind = cast(
                        Literal["claimed", "untouched"],
                        candidate_kind,
                    )
                    break
            if not rows:
                self._set_expiry_scan_cursor(connection, "oldest", None)
            elif selected_kind is not None or len(rows) == MAX_STORE_EXPIRY_BATCH_SIZE:
                self._set_expiry_scan_cursor(connection, "oldest", last_scanned)
            else:
                self._set_expiry_scan_cursor(connection, "oldest", None)
            connection.commit()
        return selected_kind

    def expire_claimed_decisions(
        self,
        *,
        now: datetime,
        batch_size: int,
        recovery_id: str | None = None,
    ) -> int:
        """Atomically seal bounded exact decision claims after consent expiry."""

        return self._expire_claimed_decisions_in_transaction(
            now=now,
            batch_size=batch_size,
            recovery_id=recovery_id,
            connection=None,
        )

    def _expire_claimed_decisions_in_transaction(
        self,
        *,
        now: datetime,
        batch_size: int,
        recovery_id: str | None = None,
        connection: sqlite3.Connection | None,
    ) -> int:
        """Seal decision claims in an existing or newly owned transaction."""

        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("now must be timezone-aware UTC")
        if not 1 <= batch_size <= MAX_STORE_EXPIRY_BATCH_SIZE:
            raise ValueError(f"batch_size must be between 1 and {MAX_STORE_EXPIRY_BATCH_SIZE}")
        target_clause = "AND recoveries.id = ?" if recovery_id is not None else ""
        now_text = now.isoformat()
        expired_count = 0

        with self._expiry_transaction(connection) as active_connection:
            parameters: list[str | int] = []
            if recovery_id is not None:
                parameters.append(recovery_id)
            scan_cursor = (
                None
                if recovery_id is not None
                else self._expiry_scan_cursor(active_connection, "claimed")
            )
            cursor_clause = ""
            if scan_cursor is not None:
                cursor_clause = """
                  AND (
                      remedies.expiry > ?
                      OR (
                          remedies.expiry = ?
                          AND recoveries.id > ?
                      )
                  )
                """
                parameters.extend((scan_cursor[0], scan_cursor[0], scan_cursor[1]))
            parameters.append(1 if recovery_id is not None else MAX_STORE_EXPIRY_BATCH_SIZE)
            rows = active_connection.execute(
                f"""
                SELECT recoveries.*, remedies.expiry AS remedy_expiry
                FROM recoveries
                JOIN pending_approvals
                  ON pending_approvals.recovery_id = recoveries.id
                JOIN remedies
                  ON remedies.recovery_id = recoveries.id
                 AND remedies.id = pending_approvals.remedy_id
                JOIN approval_decisions
                  ON approval_decisions.recovery_id = recoveries.id
                WHERE recoveries.status = 'pending_approval'
                  AND recoveries.scenario_id = 'hotel'
                  AND recoveries.execution_mode IN ('sdk_stub', 'openai_live')
                  AND pending_approvals.status IN (
                      'pending', 'approved', 'outcome_unknown'
                  )
                  AND remedies.status = 'pending'
                  AND remedies.expiry IS NOT NULL
                  AND approval_decisions.status = 'claimed'
                  AND approval_decisions.result_json IS NULL
                  AND approval_decisions.completed_at IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM receipts
                      WHERE receipts.recovery_id = recoveries.id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM events
                      WHERE events.recovery_id = recoveries.id
                        AND events.terminal = 1
                  )
                  {target_clause}
                  {cursor_clause}
                ORDER BY remedies.expiry ASC, recoveries.id ASC
                LIMIT ?
                """,
                parameters,
            ).fetchall()

            last_scanned: tuple[str, str] | None = None
            scanned_all = True
            for row in rows:
                if expired_count >= batch_size:
                    scanned_all = False
                    break
                target_id = cast(str, row["id"])
                raw_expiry = cast(str, row["remedy_expiry"])
                last_scanned = raw_expiry, target_id
                if not self._hotel_expiry_source_row_matches(
                    active_connection,
                    row=row,
                    now=now,
                ):
                    continue
                try:
                    expiry = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
                    if expiry.tzinfo is None or expiry.utcoffset() != timedelta(0) or expiry > now:
                        continue
                    decision_rows = active_connection.execute(
                        "SELECT * FROM approval_decisions WHERE recovery_id = ?",
                        (target_id,),
                    ).fetchall()
                    if len(decision_rows) != 1:
                        continue
                    decision_row = decision_rows[0]
                    claim = self._verified_decision_claim_from_row(
                        decision_row,
                        recovery_id=target_id,
                    )
                    recovery, pending, consent = self._require_claim_evidence(
                        active_connection,
                        target_id,
                        claim.request,
                    )
                    claimed_at = datetime.fromisoformat(cast(str, decision_row["claimed_at"]))
                    if (
                        claim.response is not None
                        or consent.expiry != expiry
                        or consent.expiry > now
                        or claimed_at.tzinfo is None
                        or claimed_at.utcoffset() != timedelta(0)
                        or claimed_at >= consent.expiry
                    ):
                        continue
                    provenance = self._provenance_from_row(recovery)
                    execution_rows = active_connection.execute(
                        "SELECT * FROM executions WHERE recovery_id = ?",
                        (target_id,),
                    ).fetchall()
                    if self._claim_has_canonical_completed_execution(
                        execution_rows,
                        claim=claim,
                        pending=pending,
                        consent=consent,
                        claimed_at=claimed_at,
                    ):
                        continue
                    closed = (
                        claim.request.action is DecisionAction.DECLINE
                        and pending.status == "pending"
                        and not execution_rows
                    )
                    terminal_status: Literal["closed_without_action", "outcome_unknown"] = (
                        "closed_without_action" if closed else "outcome_unknown"
                    )
                    summary = (
                        CLAIM_EXPIRY_CLOSED_SUMMARY if closed else CLAIM_EXPIRY_UNKNOWN_SUMMARY
                    )
                    receipt = self._claim_expiry_receipt(
                        recovery_id=target_id,
                        provenance=provenance,
                        action=claim.request.action,
                        status=terminal_status,
                    )
                    event_data = self._claim_expiry_event_data(
                        recovery_id=target_id,
                        receipt=receipt,
                        action=claim.request.action,
                    )
                except (ApprovalDecisionError, TypeError, ValueError):
                    continue

                next_sequence = cast(
                    int,
                    active_connection.execute(
                        """
                        SELECT COALESCE(MAX(seq), 0) + 1
                        FROM events WHERE recovery_id = ?
                        """,
                        (target_id,),
                    ).fetchone()[0],
                )
                active_connection.execute(
                    """
                    INSERT INTO receipts (recovery_id, receipt_json, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (
                        target_id,
                        receipt.model_dump_json(by_alias=True),
                        now_text,
                    ),
                )
                active_connection.execute(
                    """
                    INSERT INTO events (
                        recovery_id, seq, type, terminal, data_json, created_at
                    ) VALUES (?, ?, 'recovery.claim_expired', 1, ?, ?)
                    """,
                    (
                        target_id,
                        next_sequence,
                        json.dumps(
                            event_data,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        now_text,
                    ),
                )
                recovery_cursor = active_connection.execute(
                    """
                    UPDATE recoveries
                    SET status = ?, current_step = 5,
                        current_step_summary = ?, updated_at = ?
                    WHERE id = ? AND status = 'pending_approval'
                    """,
                    (terminal_status, summary, now_text, target_id),
                )
                pending_cursor = active_connection.execute(
                    """
                    UPDATE pending_approvals
                    SET status = ?, updated_at = ?
                    WHERE recovery_id = ? AND status = ?
                    """,
                    (
                        "rejected" if closed else "outcome_unknown",
                        now_text,
                        target_id,
                        pending.status,
                    ),
                )
                remedy_cursor = active_connection.execute(
                    """
                    UPDATE remedies
                    SET status = ?
                    WHERE recovery_id = ? AND id = ? AND status = 'pending'
                    """,
                    (
                        "declined" if closed else "outcome_unknown",
                        target_id,
                        pending.remedy_id,
                    ),
                )
                if (
                    recovery_cursor.rowcount != 1
                    or pending_cursor.rowcount != 1
                    or remedy_cursor.rowcount != 1
                ):
                    raise ReceiptTransitionError(
                        "Claim expiry state changed during terminal transition"
                    )
                active_connection.execute(
                    """
                    UPDATE live_admissions
                    SET released_at = COALESCE(released_at, ?)
                    WHERE recovery_id = ?
                    """,
                    (now_text, target_id),
                )
                expired_count += 1

            if recovery_id is None:
                if not rows:
                    self._set_expiry_scan_cursor(
                        active_connection,
                        "claimed",
                        None,
                    )
                elif not scanned_all or len(rows) == MAX_STORE_EXPIRY_BATCH_SIZE:
                    self._set_expiry_scan_cursor(
                        active_connection,
                        "claimed",
                        last_scanned,
                    )
                else:
                    self._set_expiry_scan_cursor(
                        active_connection,
                        "claimed",
                        None,
                    )

        return expired_count

    def expire_pending_approvals(
        self,
        *,
        now: datetime,
        batch_size: int,
        recovery_id: str | None = None,
    ) -> int:
        """Atomically seal bounded, untouched consent windows that have expired."""

        return self._expire_pending_approvals_in_transaction(
            now=now,
            batch_size=batch_size,
            recovery_id=recovery_id,
            connection=None,
        )

    def _expire_pending_approvals_in_transaction(
        self,
        *,
        now: datetime,
        batch_size: int,
        recovery_id: str | None = None,
        connection: sqlite3.Connection | None,
    ) -> int:
        """Seal untouched approvals in an existing or newly owned transaction."""

        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("now must be timezone-aware UTC")
        if not 1 <= batch_size <= MAX_STORE_EXPIRY_BATCH_SIZE:
            raise ValueError(f"batch_size must be between 1 and {MAX_STORE_EXPIRY_BATCH_SIZE}")
        target_clause = "AND recoveries.id = ?" if recovery_id is not None else ""
        now_text = now.isoformat()
        expired_count = 0

        with self._expiry_transaction(connection) as active_connection:
            parameters: list[str | int] = []
            if recovery_id is not None:
                parameters.append(recovery_id)
            scan_cursor = (
                None
                if recovery_id is not None
                else self._expiry_scan_cursor(active_connection, "untouched")
            )
            cursor_clause = ""
            if scan_cursor is not None:
                cursor_clause = """
                  AND (
                      remedies.expiry > ?
                      OR (
                          remedies.expiry = ?
                          AND recoveries.id > ?
                      )
                  )
                """
                parameters.extend((scan_cursor[0], scan_cursor[0], scan_cursor[1]))
            parameters.append(1 if recovery_id is not None else MAX_STORE_EXPIRY_BATCH_SIZE)
            rows = active_connection.execute(
                f"""
                SELECT recoveries.*, remedies.expiry AS remedy_expiry
                FROM recoveries
                JOIN pending_approvals
                  ON pending_approvals.recovery_id = recoveries.id
                JOIN remedies
                  ON remedies.recovery_id = recoveries.id
                 AND remedies.id = pending_approvals.remedy_id
                WHERE recoveries.status = 'pending_approval'
                  AND recoveries.scenario_id = 'hotel'
                  AND recoveries.execution_mode IN ('sdk_stub', 'openai_live')
                  AND pending_approvals.status = 'pending'
                  AND remedies.status = 'pending'
                  AND remedies.expiry IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM approval_decisions
                      WHERE approval_decisions.recovery_id = recoveries.id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM executions
                      WHERE executions.recovery_id = recoveries.id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM receipts
                      WHERE receipts.recovery_id = recoveries.id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM events
                      WHERE events.recovery_id = recoveries.id
                        AND events.terminal = 1
                  )
                  {target_clause}
                  {cursor_clause}
                ORDER BY remedies.expiry ASC, recoveries.id ASC
                LIMIT ?
                """,
                parameters,
            ).fetchall()

            last_scanned: tuple[str, str] | None = None
            scanned_all = True
            for row in rows:
                if expired_count >= batch_size:
                    scanned_all = False
                    break
                target_id = cast(str, row["id"])
                raw_expiry = cast(str, row["remedy_expiry"])
                last_scanned = raw_expiry, target_id
                if not self._hotel_expiry_source_row_matches(
                    active_connection,
                    row=row,
                    now=now,
                ):
                    continue
                try:
                    expiry = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if expiry.tzinfo is None or expiry.utcoffset() != timedelta(0) or expiry > now:
                    continue

                provenance = self._provenance_from_row(row)
                receipt = RecoveryReceipt(
                    recoveryId=target_id,
                    executionMode=provenance.execution_mode,
                    status="closed_without_action",
                    simulated=True,
                    providerExecution=False,
                    modelCall=provenance.model_call,
                    modelIds=list(provenance.model_ids),
                    rootTraceId=provenance.root_trace_id,
                    sdkVersion=provenance.sdk_version,
                    protocolVersion=provenance.protocol_version,
                    agentGraphVersion=provenance.agent_graph_version,
                    definitionDigest=provenance.definition_digest,
                    boundary=(
                        OPENAI_LIVE_BOUNDARY
                        if provenance.execution_mode is ExecutionMode.OPENAI_LIVE
                        else SDK_STUB_BOUNDARY
                    ),
                    providerResult="Provider dispatch did not begin.",
                    authorizationSource=("Consent window expired before an approval decision."),
                    verificationResults=list(EXPIRATION_VERIFICATION_RESULTS),
                    approvalCount=0,
                    approvedRemedyDigest=None,
                )
                terminal_data: dict[str, JsonValue] = {
                    "recoveryId": target_id,
                    "executionMode": provenance.execution_mode.value,
                    "providerExecution": False,
                    "approvalDecisionCount": 0,
                    "executionCount": 0,
                    "phase": "Verify & seal",
                    "summary": EXPIRATION_SUMMARY,
                }
                next_sequence = cast(
                    int,
                    active_connection.execute(
                        """
                        SELECT COALESCE(MAX(seq), 0) + 1
                        FROM events WHERE recovery_id = ?
                        """,
                        (target_id,),
                    ).fetchone()[0],
                )
                active_connection.execute(
                    """
                    INSERT INTO receipts (recovery_id, receipt_json, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (
                        target_id,
                        receipt.model_dump_json(by_alias=True),
                        now_text,
                    ),
                )
                active_connection.execute(
                    """
                    INSERT INTO events (
                        recovery_id, seq, type, terminal, data_json, created_at
                    ) VALUES (?, ?, 'recovery.expired', 1, ?, ?)
                    """,
                    (
                        target_id,
                        next_sequence,
                        json.dumps(
                            terminal_data,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        now_text,
                    ),
                )
                active_connection.execute(
                    """
                    UPDATE recoveries
                    SET status = 'closed_without_action', current_step = 5,
                        current_step_summary = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (EXPIRATION_SUMMARY, now_text, target_id),
                )
                active_connection.execute(
                    """
                    UPDATE pending_approvals
                    SET status = 'expired', updated_at = ?
                    WHERE recovery_id = ?
                    """,
                    (now_text, target_id),
                )
                active_connection.execute(
                    "UPDATE remedies SET status = 'expired' WHERE recovery_id = ?",
                    (target_id,),
                )
                active_connection.execute(
                    """
                    UPDATE live_admissions
                    SET released_at = COALESCE(released_at, ?)
                    WHERE recovery_id = ?
                    """,
                    (now_text, target_id),
                )
                expired_count += 1

            if recovery_id is None:
                if not rows:
                    self._set_expiry_scan_cursor(
                        active_connection,
                        "untouched",
                        None,
                    )
                elif not scanned_all or len(rows) == MAX_STORE_EXPIRY_BATCH_SIZE:
                    self._set_expiry_scan_cursor(
                        active_connection,
                        "untouched",
                        last_scanned,
                    )
                else:
                    self._set_expiry_scan_cursor(
                        active_connection,
                        "untouched",
                        None,
                    )

        return expired_count

    def delete_terminal_recoveries(
        self,
        *,
        updated_before: datetime,
        batch_size: int,
    ) -> int:
        """Delete bounded disposable detail while retaining aggregate usage rows."""

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT id FROM recoveries
                WHERE (
                    status IN (
                        'completed', 'closed_without_action', 'outcome_unknown'
                    )
                    OR execution_mode = 'replay_fixture'
                )
                  AND updated_at < ?
                ORDER BY updated_at ASC, id ASC
                LIMIT ?
                """,
                (updated_before.isoformat(), batch_size),
            ).fetchall()
            recovery_ids = [cast(str, row["id"]) for row in rows]
            if recovery_ids:
                placeholders = ",".join("?" for _ in recovery_ids)
                connection.execute(
                    f"DELETE FROM recoveries WHERE id IN ({placeholders})",
                    recovery_ids,
                )
        return len(recovery_ids)

    def delete_expired_recovery_creations(
        self,
        *,
        expired_at: datetime,
        batch_size: int,
    ) -> int:
        """Delete a bounded oldest batch after its signed-session scope expires."""

        self._require_creation_time(expired_at, name="expired_at")
        if not 1 <= batch_size <= MAX_STORE_EXPIRY_BATCH_SIZE:
            raise ValueError(f"batch_size must be between 1 and {MAX_STORE_EXPIRY_BATCH_SIZE}")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT request_key
                FROM recovery_creations
                WHERE expires_at <= ?
                ORDER BY expires_at ASC, request_key ASC
                LIMIT ?
                """,
                (expired_at.isoformat(), batch_size),
            ).fetchall()
            request_keys = [cast(str, row["request_key"]) for row in rows]
            if request_keys:
                placeholders = ",".join("?" for _ in request_keys)
                connection.execute(
                    f"DELETE FROM recovery_creations WHERE request_key IN ({placeholders})",
                    request_keys,
                )
        return len(request_keys)

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

    def update_pending_approval_status(
        self,
        recovery_id: str,
        *,
        expected_status: str,
        status: str,
    ) -> None:
        """Compare-and-set one active claimed transition without crossing a seal."""

        allowed_transitions = {
            ("pending", "approved"),
            ("approved", "approved"),
            ("pending", "outcome_unknown"),
            ("approved", "outcome_unknown"),
        }
        if (expected_status, status) not in allowed_transitions:
            raise ValueError("Unsupported pending approval status transition")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = self._now()
            if self._recovery_has_expiration_evidence(connection, recovery_id):
                raise ApprovalDecisionError(
                    "remedy_expired",
                    recovery_id,
                    status_code=422,
                )
            decision_row = connection.execute(
                "SELECT * FROM approval_decisions WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone()
            if decision_row is None:
                raise ApprovalDecisionError(
                    "decision_unavailable",
                    recovery_id,
                    status_code=409,
                )
            claim = self._verified_decision_claim_from_row(
                decision_row,
                recovery_id=recovery_id,
            )
            if (
                cast(str, decision_row["status"]) != "claimed"
                or decision_row["result_json"] is not None
                or decision_row["completed_at"] is not None
                or claim.response is not None
                or (status == "approved" and claim.request.action is not DecisionAction.APPROVE)
                or (
                    status == "outcome_unknown"
                    and claim.request.action is not DecisionAction.DECLINE
                )
            ):
                raise ApprovalDecisionError(
                    "decision_unavailable",
                    recovery_id,
                    status_code=409,
                )
            _recovery, pending, consent = self._require_claim_evidence(
                connection,
                recovery_id,
                claim.request,
            )
            try:
                claimed_at = datetime.fromisoformat(cast(str, decision_row["claimed_at"]))
            except (TypeError, ValueError):
                raise ApprovalDecisionError(
                    "resume_incompatible",
                    recovery_id,
                    status_code=409,
                ) from None
            if pending.status not in {expected_status, status}:
                raise ApprovalDecisionError(
                    "decision_unavailable",
                    recovery_id,
                    status_code=409,
                )
            if (
                claimed_at.tzinfo is None
                or claimed_at.utcoffset() != timedelta(0)
                or claimed_at >= consent.expiry
            ):
                raise ApprovalDecisionError(
                    "resume_incompatible",
                    recovery_id,
                    status_code=409,
                )
            if consent.expiry <= now:
                raise ApprovalDecisionError(
                    "remedy_expired",
                    recovery_id,
                    status_code=422,
                )
            cursor = connection.execute(
                """
                UPDATE pending_approvals
                SET status = ?, updated_at = ?
                WHERE recovery_id = ?
                  AND status IN (?, ?)
                  AND EXISTS (
                      SELECT 1 FROM recoveries
                      WHERE recoveries.id = pending_approvals.recovery_id
                        AND recoveries.status = 'pending_approval'
                  )
                  AND EXISTS (
                      SELECT 1 FROM approval_decisions
                      WHERE approval_decisions.recovery_id =
                            pending_approvals.recovery_id
                        AND approval_decisions.status = 'claimed'
                        AND approval_decisions.result_json IS NULL
                        AND approval_decisions.completed_at IS NULL
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM receipts
                      WHERE receipts.recovery_id = pending_approvals.recovery_id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM events
                      WHERE events.recovery_id = pending_approvals.recovery_id
                        AND events.terminal = 1
                  )
                """,
                (
                    status,
                    now.isoformat(),
                    recovery_id,
                    expected_status,
                    status,
                ),
            )
            if cursor.rowcount != 1:
                if self._recovery_has_expiration_evidence(connection, recovery_id):
                    raise ApprovalDecisionError(
                        "remedy_expired",
                        recovery_id,
                        status_code=422,
                    )
                raise ApprovalDecisionError(
                    "decision_unavailable",
                    recovery_id,
                    status_code=409,
                )

    def _require_new_execution_authorization(
        self,
        connection: sqlite3.Connection,
        *,
        recovery_id: str,
        tool_call_id: str,
        remedy_digest: str,
        now: datetime,
    ) -> None:
        """Recheck the exact active approval inside the execution write transaction."""

        decision_row = connection.execute(
            "SELECT * FROM approval_decisions WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone()
        if decision_row is None:
            raise ApprovalDecisionError(
                "decision_unavailable",
                recovery_id,
                status_code=409,
            )
        claim = self._verified_decision_claim_from_row(
            decision_row,
            recovery_id=recovery_id,
        )
        if (
            cast(str, decision_row["status"]) != "claimed"
            or decision_row["result_json"] is not None
            or decision_row["completed_at"] is not None
            or claim.response is not None
        ):
            raise ApprovalDecisionError(
                "decision_unavailable",
                recovery_id,
                status_code=409,
            )
        if (
            claim.request.action is not DecisionAction.APPROVE
            or claim.request.tool_call_id != tool_call_id
            or claim.request.remedy_digest != remedy_digest
        ):
            raise ApprovalDecisionError(
                "decision_id_conflict",
                recovery_id,
                status_code=409,
            )

        pending, _consent = self._validate_consent_for_decision(
            connection,
            recovery_id,
            claim.request,
            now=now,
        )
        if pending.status != "approved":
            raise ApprovalDecisionError(
                "decision_unavailable",
                recovery_id,
                status_code=409,
            )

        terminal_evidence = connection.execute(
            """
            SELECT 1
            WHERE EXISTS (
                SELECT 1 FROM receipts WHERE recovery_id = ?
            )
               OR EXISTS (
                SELECT 1 FROM events
                WHERE recovery_id = ? AND terminal = 1
            )
            """,
            (recovery_id, recovery_id),
        ).fetchone()
        if terminal_evidence is not None:
            raise ApprovalDecisionError(
                "decision_unavailable",
                recovery_id,
                status_code=409,
            )

    @staticmethod
    def _execution_persistence_shape(
        row: sqlite3.Row,
    ) -> Literal["completed", "pending", "invalid"]:
        """Classify only the two durable states the execution writer can trust."""

        status = row["status"]
        provider_execution = row["provider_execution"]
        result_json = row["result_json"]
        if (
            type(status) is str
            and status == "completed"
            and type(provider_execution) is int
            and provider_execution == 1
            and result_json is not None
        ):
            return "completed"
        if (
            type(status) is str
            and status == "pending"
            and type(provider_execution) is int
            and provider_execution == 0
            and result_json is None
        ):
            return "pending"
        return "invalid"

    @staticmethod
    def _quota_execution_id(recovery_id: str) -> str:
        return f"{recovery_id}:quota-execution"

    @staticmethod
    def _quota_idempotency_key(recovery_id: str) -> str:
        return f"{recovery_id}:quota-burst"

    @staticmethod
    def _canonical_quota_result_payload(
        result: QuotaRecoveryResult | dict[str, Any],
    ) -> dict[str, Any]:
        try:
            validated = QuotaRecoveryResult.model_validate(result)
        except (TypeError, ValueError):
            raise ExecutionConflictError("Durable quota result is invalid") from None
        if validated != DETERMINISTIC_QUOTA_RESULT:
            raise ExecutionConflictError(
                "Durable quota result does not match the exact demo contract"
            )
        return validated.model_dump(mode="json")

    @classmethod
    def _require_exact_quota_execution_row(
        cls,
        row: sqlite3.Row,
        *,
        recovery_id: str,
    ) -> DurableExecution:
        expected_request_digest = quota_request_digest(DETERMINISTIC_QUOTA_REQUEST)
        if (
            cast(str, row["id"]) != cls._quota_execution_id(recovery_id)
            or cast(str, row["recovery_id"]) != recovery_id
            or cast(str, row["idempotency_key"]) != cls._quota_idempotency_key(recovery_id)
            or cast(str | None, row["request_digest"]) != expected_request_digest
            or cast(str | None, row["tool_call_id"]) != QUOTA_TOOL_CALL_ID
            or row["remedy_digest"] is not None
        ):
            raise ExecutionConflictError("Durable quota execution identity is invalid")
        try:
            created_at = datetime.fromisoformat(cast(str, row["created_at"]))
            updated_at = datetime.fromisoformat(cast(str, row["updated_at"]))
        except (TypeError, ValueError):
            raise ExecutionConflictError("Durable quota execution timestamps are invalid") from None
        if (
            created_at.tzinfo is None
            or created_at.utcoffset() != timedelta(0)
            or updated_at.tzinfo is None
            or updated_at.utcoffset() != timedelta(0)
            or created_at > updated_at
        ):
            raise ExecutionConflictError("Durable quota execution timestamps are invalid")
        status = row["status"]
        provider_execution = row["provider_execution"]
        raw_result = row["result_json"]
        if (
            type(status) is not str
            or type(provider_execution) is not int
            or provider_execution not in {0, 1}
        ):
            raise ExecutionConflictError("Durable quota execution state is invalid")
        if status == "pending":
            valid_shape = provider_execution == 0 and raw_result is None
        elif status in {QUOTA_EXECUTION_RESULT_RECORDED, "completed"}:
            valid_shape = provider_execution == 1 and raw_result is not None
        elif status == RecoveryStatus.OUTCOME_UNKNOWN.value:
            valid_shape = provider_execution == 0 and raw_result is None
        else:
            valid_shape = False
        if not valid_shape:
            raise ExecutionConflictError("Durable quota execution state is invalid")
        execution = cls._execution_from_row(row)
        if execution.result_json is not None:
            cls._canonical_quota_result_payload(execution.result_json)
        return execution

    @staticmethod
    def _require_quota_execution_chronology(
        *,
        recovery: sqlite3.Row,
        execution: sqlite3.Row,
        terminal: bool,
        now: datetime | None = None,
    ) -> None:
        try:
            recovery_created_at = datetime.fromisoformat(cast(str, recovery["created_at"]))
            recovery_updated_at = datetime.fromisoformat(cast(str, recovery["updated_at"]))
            execution_created_at = datetime.fromisoformat(cast(str, execution["created_at"]))
            execution_updated_at = datetime.fromisoformat(cast(str, execution["updated_at"]))
            execution_status = cast(str, execution["status"])
        except (TypeError, ValueError):
            raise ExecutionConflictError("Quota execution chronology is invalid") from None
        timestamps = (
            recovery_created_at,
            recovery_updated_at,
            execution_created_at,
            execution_updated_at,
        )
        if (
            any(
                timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(0)
                for timestamp in timestamps
            )
            or execution_created_at != recovery_created_at
            or execution_updated_at < execution_created_at
            or (execution_status == "pending" and execution_updated_at != execution_created_at)
            or (now is not None and execution_updated_at > now)
            or (terminal and execution_updated_at > recovery_updated_at)
            or (
                terminal
                and execution_status == RecoveryStatus.OUTCOME_UNKNOWN.value
                and execution_updated_at != recovery_updated_at
            )
        ):
            raise ExecutionConflictError("Quota execution chronology is invalid")

    @staticmethod
    def _require_quota_recovery_provenance(row: sqlite3.Row) -> None:
        try:
            model_ids = json.loads(cast(str, row["model_ids_json"]))
            root_trace_id = cast(str | None, row["root_trace_id"])
            definition_digest = cast(str | None, row["definition_digest"])
            sdk_version = row["sdk_version"]
            protocol_version = row["protocol_version"]
            agent_graph_version = row["agent_graph_version"]
            quota_execution_contract = row["quota_execution_contract"]
        except (TypeError, ValueError):
            raise ExecutionConflictError("Quota recovery provenance is invalid") from None
        if (
            cast(str, row["scenario_id"]) != ScenarioId.API_QUOTA.value
            or cast(str, row["execution_mode"]) != ExecutionMode.SDK_STUB.value
            or model_ids != []
            or cast(int, row["model_call"]) != 0
            or not isinstance(root_trace_id, str)
            or not is_valid_qa_trace_id(root_trace_id)
            or not isinstance(sdk_version, str)
            or not sdk_version
            or not isinstance(protocol_version, str)
            or not protocol_version
            or not isinstance(agent_graph_version, str)
            or not agent_graph_version
            or not isinstance(definition_digest, str)
            or len(definition_digest) != 64
            or type(quota_execution_contract) is not int
            or quota_execution_contract not in {0, 1}
            or any(character not in "0123456789abcdef" for character in definition_digest)
        ):
            raise ExecutionConflictError("Quota recovery provenance is invalid")

    @classmethod
    def _require_quota_creation_binding(
        cls,
        connection: sqlite3.Connection,
        *,
        recovery_id: str,
        allowed_statuses: frozenset[str],
        now: datetime | None = None,
    ) -> sqlite3.Row | None:
        creation_rows = connection.execute(
            """
            SELECT * FROM recovery_creations
            WHERE recovery_id = ?
            ORDER BY created_at ASC
            """,
            (recovery_id,),
        ).fetchall()
        access_rows = connection.execute(
            """
            SELECT session_key FROM recovery_access
            WHERE recovery_id = ?
            ORDER BY session_key ASC
            """,
            (recovery_id,),
        ).fetchall()
        if not creation_rows:
            if access_rows:
                raise ExecutionConflictError("Quota recovery access has no creation owner")
            return None
        if len(creation_rows) != 1 or len(access_rows) != 1:
            raise ExecutionConflictError("Quota recovery creation ownership is ambiguous")
        creation = creation_rows[0]
        expected_fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "executionMode": ExecutionMode.SDK_STUB.value,
                    "scenarioId": ScenarioId.API_QUOTA.value,
                },
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
        ).hexdigest()
        try:
            created_at = datetime.fromisoformat(cast(str, creation["created_at"]))
            updated_at = datetime.fromisoformat(cast(str, creation["updated_at"]))
            expires_at = datetime.fromisoformat(cast(str, creation["expires_at"]))
            recovery = connection.execute(
                """
                SELECT created_at, updated_at, quota_execution_contract
                FROM recoveries
                WHERE id = ?
                """,
                (recovery_id,),
            ).fetchone()
            if recovery is None:
                raise ValueError
            recovery_created_at = datetime.fromisoformat(cast(str, recovery["created_at"]))
            recovery_updated_at = datetime.fromisoformat(cast(str, recovery["updated_at"]))
            quota_execution_contract = recovery["quota_execution_contract"]
        except (TypeError, ValueError):
            raise ExecutionConflictError("Quota recovery creation timestamps are invalid") from None
        creation_status = cast(str, creation["status"])
        digest_fields = (
            creation["request_key"],
            creation["session_key"],
            creation["ip_key"],
        )
        if (
            cast(str, creation["scenario_id"]) != ScenarioId.API_QUOTA.value
            or cast(str, creation["execution_mode"]) != ExecutionMode.SDK_STUB.value
            or cast(str, creation["recovery_id"]) != recovery_id
            or creation_status not in allowed_statuses
            or cast(str, creation["request_fingerprint"]) != expected_fingerprint
            or cast(str, creation["session_key"]) != cast(str, access_rows[0]["session_key"])
            or any(
                type(value) is not str
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
                for value in digest_fields
            )
            or created_at.tzinfo is None
            or created_at.utcoffset() != timedelta(0)
            or updated_at.tzinfo is None
            or updated_at.utcoffset() != timedelta(0)
            or expires_at.tzinfo is None
            or expires_at.utcoffset() != timedelta(0)
            or recovery_created_at.tzinfo is None
            or recovery_created_at.utcoffset() != timedelta(0)
            or recovery_updated_at.tzinfo is None
            or recovery_updated_at.utcoffset() != timedelta(0)
            or type(quota_execution_contract) is not int
            or quota_execution_contract not in {0, 1}
            or created_at > updated_at
            or created_at >= expires_at
            or created_at > recovery_created_at
            or (
                creation_status == "ready"
                and quota_execution_contract == 1
                and updated_at != recovery_updated_at
            )
            or (
                creation_status == "ready"
                and quota_execution_contract == 0
                and not (recovery_updated_at <= updated_at <= expires_at)
            )
            or (creation_status != "ready" and now is not None and updated_at > now)
        ):
            raise ExecutionConflictError("Quota recovery creation ownership is invalid")
        return cast(sqlite3.Row, creation)

    @classmethod
    def _require_pristine_quota_recovery(
        cls,
        connection: sqlite3.Connection,
        *,
        recovery_id: str,
        allowed_creation_statuses: frozenset[str] = frozenset({"started"}),
        now: datetime | None = None,
    ) -> sqlite3.Row:
        recovery = connection.execute(
            "SELECT * FROM recoveries WHERE id = ?",
            (recovery_id,),
        ).fetchone()
        if recovery is None:
            raise RecoveryNotFoundError("Recovery not found")
        cls._require_quota_recovery_provenance(recovery)
        try:
            recovery_created_at = datetime.fromisoformat(cast(str, recovery["created_at"]))
            recovery_updated_at = datetime.fromisoformat(cast(str, recovery["updated_at"]))
            exact_initial_snapshot = (
                type(recovery["current_step"]) is int
                and recovery["current_step"] == 0
                and cast(str, recovery["current_step_summary"]) == QUOTA_SDK_INITIAL_SUMMARY
                and recovery_created_at.tzinfo is not None
                and recovery_created_at.utcoffset() == timedelta(0)
                and recovery_updated_at.tzinfo is not None
                and recovery_updated_at.utcoffset() == timedelta(0)
                and recovery_updated_at == recovery_created_at
                and (now is None or (recovery_created_at <= now and recovery_updated_at <= now))
            )
        except (TypeError, ValueError):
            exact_initial_snapshot = False
        expected_created_payload = {
            "scenarioId": ScenarioId.API_QUOTA.value,
            "executionMode": ExecutionMode.SDK_STUB.value,
            "summary": "Recovery created for the selected execution mode.",
        }
        events = connection.execute(
            """
            SELECT seq, type, terminal, data_json, created_at
            FROM events
            WHERE recovery_id = ?
            ORDER BY seq ASC
            """,
            (recovery_id,),
        ).fetchall()
        try:
            exact_creation_event = (
                len(events) == 1
                and cast(int, events[0]["seq"]) == 1
                and cast(str, events[0]["type"]) == "recovery.created"
                and cast(int, events[0]["terminal"]) == 0
                and json.loads(cast(str, events[0]["data_json"])) == expected_created_payload
                and datetime.fromisoformat(cast(str, events[0]["created_at"]))
                == datetime.fromisoformat(cast(str, recovery["created_at"]))
            )
        except (TypeError, ValueError):
            exact_creation_event = False
        terminal_or_approval_evidence = connection.execute(
            """
            SELECT 1
            WHERE EXISTS (
                SELECT 1 FROM receipts WHERE recovery_id = ?
            )
               OR EXISTS (
                SELECT 1 FROM pending_approvals WHERE recovery_id = ?
            )
               OR EXISTS (
                SELECT 1 FROM approval_decisions WHERE recovery_id = ?
            )
               OR EXISTS (
                SELECT 1 FROM remedies WHERE recovery_id = ?
            )
            """,
            (recovery_id, recovery_id, recovery_id, recovery_id),
        ).fetchone()
        if (
            cast(str, recovery["status"]) != RecoveryStatus.IN_PROGRESS.value
            or cast(int, recovery["quota_execution_contract"]) != 1
            or not exact_initial_snapshot
            or not exact_creation_event
            or terminal_or_approval_evidence is not None
        ):
            raise ExecutionConflictError("Quota recovery is not eligible for durable dispatch")
        cls._require_quota_creation_binding(
            connection,
            recovery_id=recovery_id,
            allowed_statuses=allowed_creation_statuses,
            now=now,
        )
        return cast(sqlite3.Row, recovery)

    @classmethod
    def _insert_quota_execution_claim_in_connection(
        cls,
        connection: sqlite3.Connection,
        *,
        recovery_id: str,
        now: datetime,
    ) -> None:
        """Insert the pre-dispatch quota claim in the recovery transaction."""

        existing = connection.execute(
            "SELECT 1 FROM executions WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone()
        if existing is not None:
            raise ExecutionConflictError("Quota recovery already has a durable execution")
        connection.execute(
            """
            INSERT INTO executions (
                id, recovery_id, idempotency_key, status,
                provider_execution, request_digest, tool_call_id,
                remedy_digest, result_json, created_at, updated_at
            ) VALUES (?, ?, ?, 'pending', 0, ?, ?, NULL, NULL, ?, ?)
            """,
            (
                cls._quota_execution_id(recovery_id),
                recovery_id,
                cls._quota_idempotency_key(recovery_id),
                quota_request_digest(DETERMINISTIC_QUOTA_REQUEST),
                QUOTA_TOOL_CALL_ID,
                now.isoformat(),
                now.isoformat(),
            ),
        )

    def record_completed_quota_execution(
        self,
        execution: DurableExecution,
        *,
        result: QuotaRecoveryResult,
    ) -> tuple[DurableExecution, bool]:
        """Commit the exact provider result before SDK completion is validated."""

        canonical_result = self._canonical_quota_result_payload(result)
        serialized_result = json.dumps(
            canonical_result,
            separators=(",", ":"),
            sort_keys=True,
        )
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current_time = self._now()
            recovery = self._require_pristine_quota_recovery(
                connection,
                recovery_id=execution.recovery_id,
                now=current_time,
            )
            rows = connection.execute(
                "SELECT * FROM executions WHERE recovery_id = ?",
                (execution.recovery_id,),
            ).fetchall()
            if not rows:
                raise ExecutionConflictError("Quota execution claim is missing")
            if len(rows) != 1:
                raise ExecutionConflictError("Quota recovery has ambiguous durable executions")
            row = rows[0]
            current = self._require_exact_quota_execution_row(
                row,
                recovery_id=execution.recovery_id,
            )
            self._require_quota_execution_chronology(
                recovery=recovery,
                execution=row,
                terminal=False,
                now=current_time,
            )
            if current.status in {QUOTA_EXECUTION_RESULT_RECORDED, "completed"}:
                if current.result_json != canonical_result:
                    raise ExecutionConflictError("Durable quota result changed")
                return current, False
            if current.status != "pending" or current != execution:
                raise ExecutionConflictError("Quota execution claim is no longer dispatchable")
            updated = connection.execute(
                """
                UPDATE executions
                SET status = ?, provider_execution = 1,
                    result_json = ?, updated_at = ?
                WHERE id = ? AND status = 'pending'
                  AND provider_execution = 0 AND result_json IS NULL
                """,
                (
                    QUOTA_EXECUTION_RESULT_RECORDED,
                    serialized_result,
                    current_time.isoformat(),
                    execution.execution_id,
                ),
            )
            if updated.rowcount != 1:
                raise ExecutionConflictError("Quota execution result lost its durable claim")
            completed_row = connection.execute(
                "SELECT * FROM executions WHERE id = ?",
                (execution.execution_id,),
            ).fetchone()
            if completed_row is None:
                raise RuntimeError("Quota execution result did not persist")
            return (
                self._require_exact_quota_execution_row(
                    completed_row,
                    recovery_id=execution.recovery_id,
                ),
                True,
            )

    def validate_quota_sdk_completion(
        self,
        execution: DurableExecution,
    ) -> DurableExecution:
        """Durably attest that the SDK returned with zero human interruptions."""

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current_time = self._now()
            recovery = connection.execute(
                "SELECT * FROM recoveries WHERE id = ?",
                (execution.recovery_id,),
            ).fetchone()
            execution_rows = connection.execute(
                "SELECT * FROM executions WHERE recovery_id = ?",
                (execution.recovery_id,),
            ).fetchall()
            if recovery is None or not execution_rows:
                raise RecoveryNotFoundError("Quota recovery execution not found")
            if len(execution_rows) != 1:
                raise ExecutionConflictError("Quota recovery has ambiguous durable executions")
            current_row = execution_rows[0]
            current = self._require_exact_quota_execution_row(
                current_row,
                recovery_id=execution.recovery_id,
            )
            self._require_quota_recovery_provenance(recovery)
            if cast(int, recovery["quota_execution_contract"]) != 1:
                raise ExecutionConflictError("Quota recovery has no durable execution contract")
            recovery_is_terminal = cast(str, recovery["status"]) in {
                RecoveryStatus.COMPLETED.value,
                RecoveryStatus.OUTCOME_UNKNOWN.value,
            }
            self._require_quota_execution_chronology(
                recovery=recovery,
                execution=current_row,
                terminal=recovery_is_terminal,
                now=current_time,
            )
            if (
                current.execution_id != execution.execution_id
                or current.result_json != execution.result_json
            ):
                raise ExecutionConflictError("Recorded quota result changed before SDK validation")
            if current.status == "completed":
                receipt = self._quota_receipt_for_execution_row(
                    recovery=recovery,
                    execution=current,
                )
                specs = self._quota_completed_transition_specs(
                    QuotaRecoveryResult.model_validate(current.result_json)
                )
                if recovery_is_terminal:
                    if not self._quota_terminal_bundle_matches(
                        connection,
                        recovery=recovery,
                        execution=current,
                        receipt=receipt,
                        specs=specs,
                    ):
                        raise ReceiptTransitionError("Existing quota terminal evidence mismatch")
                else:
                    self._require_pristine_quota_recovery(
                        connection,
                        recovery_id=execution.recovery_id,
                        allowed_creation_statuses=frozenset({"started", "unknown"}),
                        now=current_time,
                    )
                return current
            if recovery_is_terminal:
                raise ExecutionConflictError("Unvalidated quota result has terminal evidence")
            self._require_pristine_quota_recovery(
                connection,
                recovery_id=execution.recovery_id,
                allowed_creation_statuses=frozenset({"started", "unknown"}),
                now=current_time,
            )
            if current.status != QUOTA_EXECUTION_RESULT_RECORDED:
                raise ExecutionConflictError("Recorded quota result changed before SDK validation")
            updated = connection.execute(
                """
                UPDATE executions
                SET status = 'completed', updated_at = ?
                WHERE id = ? AND status = ?
                  AND provider_execution = 1 AND result_json IS NOT NULL
                """,
                (
                    current_time.isoformat(),
                    current.execution_id,
                    QUOTA_EXECUTION_RESULT_RECORDED,
                ),
            )
            if updated.rowcount != 1:
                raise ExecutionConflictError("Quota SDK validation marker did not persist")
            validated_row = connection.execute(
                "SELECT * FROM executions WHERE id = ?",
                (current.execution_id,),
            ).fetchone()
            if validated_row is None:
                raise RuntimeError("Validated quota execution did not persist")
            validated = self._require_exact_quota_execution_row(
                validated_row,
                recovery_id=execution.recovery_id,
            )
            self._require_quota_execution_chronology(
                recovery=recovery,
                execution=validated_row,
                terminal=False,
                now=current_time,
            )
            return validated

    def quarantine_quota_execution_invariant_failure(
        self,
        execution: DurableExecution,
    ) -> None:
        """Retain evidence that cannot support the zero-interruption contract."""

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current_time = self._now()
            recovery = connection.execute(
                "SELECT * FROM recoveries WHERE id = ?",
                (execution.recovery_id,),
            ).fetchone()
            execution_rows = connection.execute(
                "SELECT * FROM executions WHERE recovery_id = ?",
                (execution.recovery_id,),
            ).fetchall()
            if recovery is None or not execution_rows:
                raise RecoveryNotFoundError("Quota recovery execution not found")
            if len(execution_rows) != 1:
                raise ExecutionConflictError("Quota recovery has ambiguous durable executions")
            current_row = execution_rows[0]
            current = self._require_exact_quota_execution_row(
                current_row,
                recovery_id=execution.recovery_id,
            )
            self._require_quota_recovery_provenance(recovery)
            if cast(int, recovery["quota_execution_contract"]) != 1:
                raise ExecutionConflictError("Quota recovery has no durable execution contract")
            recovery_status = RecoveryStatus(cast(str, recovery["status"]))
            self._require_quota_execution_chronology(
                recovery=recovery,
                execution=current_row,
                terminal=recovery_status is not RecoveryStatus.IN_PROGRESS,
                now=current_time,
            )
            if recovery_status is not RecoveryStatus.IN_PROGRESS or current.status not in {
                "pending",
                QUOTA_EXECUTION_RESULT_RECORDED,
            }:
                raise ExecutionConflictError("Quota invariant state cannot be quarantined")
            if current != execution:
                raise ExecutionConflictError("Quota invariant execution changed before quarantine")
            self._require_pristine_quota_recovery(
                connection,
                recovery_id=execution.recovery_id,
                allowed_creation_statuses=frozenset({"started", "unknown"}),
                now=current_time,
            )
            creation = connection.execute(
                """
                SELECT request_key, request_fingerprint, status
                FROM recovery_creations
                WHERE recovery_id = ?
                """,
                (execution.recovery_id,),
            ).fetchone()
            updated = connection.execute(
                """
                UPDATE executions
                SET status = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    QUOTA_EXECUTION_INVARIANT_FAILED,
                    current_time.isoformat(),
                    current.execution_id,
                    current.status,
                ),
            )
            if updated.rowcount != 1:
                raise ExecutionConflictError("Quota invariant evidence was not quarantined")
            if creation is not None and cast(str, creation["status"]) == "started":
                creation_updated = connection.execute(
                    """
                    UPDATE recovery_creations
                    SET status = 'unknown', updated_at = ?
                    WHERE request_key = ? AND request_fingerprint = ?
                      AND recovery_id = ? AND status = 'started'
                    """,
                    (
                        current_time.isoformat(),
                        cast(str, creation["request_key"]),
                        cast(str, creation["request_fingerprint"]),
                        execution.recovery_id,
                    ),
                )
                if creation_updated.rowcount != 1:
                    raise ExecutionConflictError(
                        "Quota invariant creation evidence was not quarantined"
                    )
            self._require_quota_creation_binding(
                connection,
                recovery_id=execution.recovery_id,
                allowed_statuses=frozenset({"unknown"}),
                now=current_time,
            )

    @staticmethod
    def _quota_completed_transition_specs(
        result: QuotaRecoveryResult,
    ) -> list[tuple[str, int, str, dict[str, JsonValue]]]:
        proof = result.ceiling_proof
        grant = result.grant
        authority = result.authority
        return [
            (
                "quota.pressure_detected",
                0,
                "Quota demand exceeds the provider-proven baseline ceiling.",
                {
                    "phase": "Detect",
                    "region": grant.region,
                    "baselineCeilingUnits": proof.baseline_ceiling_units,
                    "requiredUnits": proof.required_units,
                    "shortfallUnits": proof.shortfall_units,
                },
            ),
            (
                "quota.ceiling_proven",
                1,
                "Provider evidence proves the exact quota ceiling and shortfall.",
                {
                    "phase": "Prove",
                    "providerEvidenceId": proof.provider_evidence_id,
                    "baselineCeilingUnits": proof.baseline_ceiling_units,
                    "requiredUnits": proof.required_units,
                    "shortfallUnits": proof.shortfall_units,
                },
            ),
            (
                "quota.burst_selected",
                2,
                "A temporary US-region burst covers the proven shortfall.",
                {
                    "phase": "Negotiate",
                    "permissionId": grant.permission_id,
                    "region": grant.region,
                    "burstUnits": grant.burst_units,
                    "effectiveCeilingUnits": grant.effective_ceiling_units,
                    "durationSeconds": grant.duration_seconds,
                    "extraCostMinor": grant.extra_cost_minor,
                    "currency": grant.currency,
                },
            ),
            (
                "quota.delegated_authority_confirmed",
                3,
                ("Delegated policy authorizes the exact burst with zero human approvals."),
                {
                    "phase": "Authorize",
                    "approvalCount": result.approval_count,
                    "hardConstraintsSatisfied": result.hard_constraints_satisfied,
                    "delegatedAuthoritySatisfied": (result.delegated_authority_satisfied),
                    "maximumExtraCostMinor": authority.maximum_extra_cost_minor,
                    "maximumDurationSeconds": authority.maximum_duration_seconds,
                    "allowedRegions": list(authority.allowed_regions),
                },
            ),
            (
                "quota.burst_executed",
                4,
                "The demo adapter executed and verified the temporary burst.",
                {
                    "phase": "Execute",
                    "providerExecution": True,
                    "executionVerified": result.execution_verified,
                    "effectiveCeilingUnits": grant.effective_ceiling_units,
                },
            ),
            (
                "quota.receipt_sealed",
                5,
                ("Execution verified, temporary permission revoked, and receipt sealed."),
                {
                    "phase": "Verify & seal",
                    "approvalCount": result.approval_count,
                    "providerExecution": True,
                    "executionVerified": result.execution_verified,
                    "permissionRevoked": result.permission_revoked,
                    "restoredCeilingUnits": result.restored_ceiling_units,
                    "summary": "Verified quota recovery evidence sealed.",
                },
            ),
        ]

    @staticmethod
    def _quota_unknown_transition_spec(
        recovery_id: str,
    ) -> tuple[str, int, str, dict[str, JsonValue]]:
        return (
            "quota.outcome_unknown",
            5,
            QUOTA_SDK_UNKNOWN_SUMMARY,
            {
                "recoveryId": recovery_id,
                "executionMode": ExecutionMode.SDK_STUB.value,
                "approvalCount": 0,
                "providerExecution": None,
                "phase": "Verify & seal",
                "redispatched": False,
                "summary": QUOTA_SDK_UNKNOWN_SUMMARY,
            },
        )

    @classmethod
    def _quota_receipt_for_execution_row(
        cls,
        *,
        recovery: sqlite3.Row,
        execution: DurableExecution,
    ) -> RecoveryReceipt:
        cls._require_quota_recovery_provenance(recovery)
        provenance = cls._provenance_from_row(recovery)
        if provenance.recovery_id != execution.recovery_id:
            raise ReceiptTransitionError("Quota execution provenance recovery ID mismatch")
        if execution.status == "completed":
            if execution.result_json is None:
                raise ReceiptTransitionError("Completed quota execution has no durable result")
            cls._canonical_quota_result_payload(execution.result_json)
            return RecoveryReceipt(
                recoveryId=execution.recovery_id,
                executionMode=ExecutionMode.SDK_STUB,
                status="completed",
                simulated=True,
                providerExecution=True,
                modelCall=False,
                modelIds=[],
                rootTraceId=provenance.root_trace_id,
                sdkVersion=provenance.sdk_version,
                protocolVersion=provenance.protocol_version,
                agentGraphVersion=provenance.agent_graph_version,
                definitionDigest=provenance.definition_digest,
                boundary=QUOTA_SDK_STUB_BOUNDARY,
                providerResult=QUOTA_SDK_PROVIDER_RESULT,
                authorizationSource=QUOTA_SDK_AUTHORIZATION_SOURCE,
                verificationResults=list(QUOTA_SDK_VERIFICATION_RESULTS),
                approvalCount=0,
            )
        if execution.status != RecoveryStatus.OUTCOME_UNKNOWN.value:
            raise ReceiptTransitionError("Quota execution cannot produce an unknown receipt")
        return RecoveryReceipt(
            recoveryId=execution.recovery_id,
            executionMode=ExecutionMode.SDK_STUB,
            status=RecoveryStatus.OUTCOME_UNKNOWN.value,
            simulated=True,
            providerExecution=None,
            modelCall=False,
            modelIds=[],
            rootTraceId=provenance.root_trace_id,
            sdkVersion=provenance.sdk_version,
            protocolVersion=provenance.protocol_version,
            agentGraphVersion=provenance.agent_graph_version,
            definitionDigest=provenance.definition_digest,
            boundary=QUOTA_SDK_STUB_BOUNDARY,
            providerResult=QUOTA_SDK_UNKNOWN_PROVIDER_RESULT,
            authorizationSource=QUOTA_SDK_UNKNOWN_AUTHORIZATION_SOURCE,
            verificationResults=list(QUOTA_SDK_UNKNOWN_VERIFICATION_RESULTS),
            approvalCount=0,
        )

    @classmethod
    def _quota_terminal_bundle_matches(
        cls,
        connection: sqlite3.Connection,
        *,
        recovery: sqlite3.Row,
        execution: DurableExecution,
        receipt: RecoveryReceipt,
        specs: list[tuple[str, int, str, dict[str, JsonValue]]],
    ) -> bool:
        recovery_id = execution.recovery_id
        if execution.status == "completed":
            expected_status = RecoveryStatus.COMPLETED
        elif execution.status == RecoveryStatus.OUTCOME_UNKNOWN.value:
            expected_status = RecoveryStatus.OUTCOME_UNKNOWN
        else:
            return False
        expected_summary = specs[-1][2]
        if (
            cast(str, recovery["status"]) != expected_status.value
            or cast(int, recovery["current_step"]) != 5
            or cast(str, recovery["current_step_summary"]) != expected_summary
        ):
            return False
        receipt_row = connection.execute(
            """
            SELECT receipt_json, created_at
            FROM receipts
            WHERE recovery_id = ?
            """,
            (recovery_id,),
        ).fetchone()
        event_rows = connection.execute(
            """
            SELECT seq, type, terminal, data_json, created_at
            FROM events
            WHERE recovery_id = ?
            ORDER BY seq ASC
            """,
            (recovery_id,),
        ).fetchall()
        forbidden_human_evidence = connection.execute(
            """
            SELECT 1
            WHERE EXISTS (
                SELECT 1 FROM pending_approvals WHERE recovery_id = ?
            )
               OR EXISTS (
                SELECT 1 FROM approval_decisions WHERE recovery_id = ?
            )
               OR EXISTS (
                SELECT 1 FROM remedies WHERE recovery_id = ?
            )
            """,
            (recovery_id, recovery_id, recovery_id),
        ).fetchone()
        if (
            receipt_row is None
            or len(event_rows) != len(specs) + 1
            or forbidden_human_evidence is not None
        ):
            return False
        expected_created_payload: dict[str, JsonValue] = {
            "scenarioId": ScenarioId.API_QUOTA.value,
            "executionMode": ExecutionMode.SDK_STUB.value,
            "summary": "Recovery created for the selected execution mode.",
        }
        expected_events = [
            ("recovery.created", False, expected_created_payload),
            *[
                (event_type, index == len(specs) - 1, data)
                for index, (event_type, _step, _summary, data) in enumerate(specs)
            ],
        ]
        try:
            stored_receipt = RecoveryReceipt.model_validate_json(
                cast(str, receipt_row["receipt_json"])
            )
            event_times = [
                datetime.fromisoformat(cast(str, event["created_at"])) for event in event_rows
            ]
            receipt_created_at = datetime.fromisoformat(cast(str, receipt_row["created_at"]))
            recovery_created_at = datetime.fromisoformat(cast(str, recovery["created_at"]))
            recovery_updated_at = datetime.fromisoformat(cast(str, recovery["updated_at"]))
            actual_events = [
                (
                    cast(str, event["type"]),
                    bool(cast(int, event["terminal"])),
                    json.loads(cast(str, event["data_json"])),
                )
                for event in event_rows
            ]
        except (TypeError, ValueError):
            return False
        if (
            stored_receipt != receipt
            or actual_events != expected_events
            or [cast(int, event["seq"]) for event in event_rows]
            != list(range(1, len(event_rows) + 1))
            or event_times[0] != recovery_created_at
            or receipt_created_at != recovery_updated_at
            or event_times[-1] != recovery_updated_at
            or any(
                event_time.tzinfo is None or event_time.utcoffset() != timedelta(0)
                for event_time in event_times
            )
            or any(
                later < earlier
                for earlier, later in zip(
                    event_times,
                    event_times[1:],
                    strict=False,
                )
            )
        ):
            return False
        try:
            quota_execution_contract = recovery["quota_execution_contract"]
            if type(quota_execution_contract) is not int or quota_execution_contract not in {0, 1}:
                return False
            allowed_creation_statuses = (
                frozenset({"started", "unknown", "ready"})
                if quota_execution_contract == 0
                else frozenset({"ready"})
            )
            cls._require_quota_creation_binding(
                connection,
                recovery_id=recovery_id,
                allowed_statuses=allowed_creation_statuses,
            )
        except ExecutionConflictError:
            return False
        return True

    @classmethod
    def _mark_quota_creation_ready_in_connection(
        cls,
        connection: sqlite3.Connection,
        *,
        recovery_id: str,
        now: datetime,
    ) -> None:
        creation = cls._require_quota_creation_binding(
            connection,
            recovery_id=recovery_id,
            allowed_statuses=frozenset({"started", "unknown"}),
            now=now,
        )
        if creation is None:
            return
        updated = connection.execute(
            """
            UPDATE recovery_creations
            SET status = 'ready', updated_at = ?
            WHERE request_key = ? AND recovery_id = ?
              AND status IN ('started', 'unknown')
            """,
            (
                now.isoformat(),
                cast(str, creation["request_key"]),
                recovery_id,
            ),
        )
        if updated.rowcount != 1:
            raise ExecutionConflictError("Quota recovery creation could not be finalized")
        cls._require_quota_creation_binding(
            connection,
            recovery_id=recovery_id,
            allowed_statuses=frozenset({"ready"}),
        )

    def _finalize_quota_execution_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        requested_execution: DurableExecution,
        expected_result: bool,
        now: datetime,
    ) -> bool:
        recovery_id = requested_execution.recovery_id
        recovery = connection.execute(
            "SELECT * FROM recoveries WHERE id = ?",
            (recovery_id,),
        ).fetchone()
        execution_rows = connection.execute(
            "SELECT * FROM executions WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchall()
        if recovery is None or not execution_rows:
            raise RecoveryNotFoundError("Quota recovery execution not found")
        if len(execution_rows) != 1:
            raise ExecutionConflictError("Quota recovery has ambiguous durable executions")
        execution_row = execution_rows[0]
        current = self._require_exact_quota_execution_row(
            execution_row,
            recovery_id=recovery_id,
        )
        self._require_quota_recovery_provenance(recovery)
        if cast(int, recovery["quota_execution_contract"]) != 1:
            raise ExecutionConflictError("Quota recovery has no durable execution contract")
        recovery_is_terminal = cast(str, recovery["status"]) in {
            RecoveryStatus.COMPLETED.value,
            RecoveryStatus.OUTCOME_UNKNOWN.value,
        }
        self._require_quota_execution_chronology(
            recovery=recovery,
            execution=execution_row,
            terminal=recovery_is_terminal,
            now=now,
        )
        if recovery_is_terminal:
            receipt = self._quota_receipt_for_execution_row(
                recovery=recovery,
                execution=current,
            )
            specs = (
                self._quota_completed_transition_specs(
                    QuotaRecoveryResult.model_validate(current.result_json)
                )
                if current.status == "completed"
                else [self._quota_unknown_transition_spec(recovery_id)]
            )
            if not self._quota_terminal_bundle_matches(
                connection,
                recovery=recovery,
                execution=current,
                receipt=receipt,
                specs=specs,
            ):
                raise ReceiptTransitionError("Existing quota terminal evidence mismatch")
            return False

        self._require_pristine_quota_recovery(
            connection,
            recovery_id=recovery_id,
            allowed_creation_statuses=frozenset({"started", "unknown"}),
            now=now,
        )
        if expected_result and current.status != "completed":
            raise ExecutionConflictError("Completed quota finalization lost its durable result")
        if not expected_result and current.status == "completed":
            expected_result = True
        if current.status not in {"pending", "completed"}:
            raise ExecutionConflictError("Quota execution cannot be reconciled")

        if expected_result:
            if current.result_json is None:
                raise ExecutionConflictError("Completed quota result is missing")
            result = QuotaRecoveryResult.model_validate(
                self._canonical_quota_result_payload(current.result_json)
            )
            specs = self._quota_completed_transition_specs(result)
            terminal_status = RecoveryStatus.COMPLETED
            durable_execution = current
        else:
            updated = connection.execute(
                """
                UPDATE executions
                SET status = 'outcome_unknown', updated_at = ?
                WHERE id = ? AND status = 'pending'
                  AND provider_execution = 0 AND result_json IS NULL
                """,
                (now.isoformat(), current.execution_id),
            )
            if updated.rowcount != 1:
                raise ExecutionConflictError("Quota dispatch claim could not be sealed unknown")
            unknown_row = connection.execute(
                "SELECT * FROM executions WHERE id = ?",
                (current.execution_id,),
            ).fetchone()
            if unknown_row is None:
                raise RuntimeError("Unknown quota execution did not persist")
            durable_execution = self._require_exact_quota_execution_row(
                unknown_row,
                recovery_id=recovery_id,
            )
            specs = [self._quota_unknown_transition_spec(recovery_id)]
            terminal_status = RecoveryStatus.OUTCOME_UNKNOWN

        receipt = self._quota_receipt_for_execution_row(
            recovery=recovery,
            execution=durable_execution,
        )
        next_sequence = 2
        for index, (event_type, _step, _summary, data) in enumerate(specs):
            terminal = index == len(specs) - 1
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
                    int(terminal),
                    json.dumps(
                        data,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    now.isoformat(),
                ),
            )
            next_sequence += 1
        connection.execute(
            """
            INSERT INTO receipts (recovery_id, receipt_json, created_at)
            VALUES (?, ?, ?)
            """,
            (
                recovery_id,
                receipt.model_dump_json(by_alias=True),
                now.isoformat(),
            ),
        )
        connection.execute(
            """
            UPDATE recoveries
            SET status = ?, current_step = 5,
                current_step_summary = ?, updated_at = ?
            WHERE id = ? AND status = 'in_progress'
            """,
            (
                terminal_status.value,
                specs[-1][2],
                now.isoformat(),
                recovery_id,
            ),
        )
        self._mark_quota_creation_ready_in_connection(
            connection,
            recovery_id=recovery_id,
            now=now,
        )
        return True

    def get_quota_execution(
        self,
        recovery_id: str,
    ) -> DurableExecution | None:
        """Return one exact quota execution row without accepting hotel rows."""

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN")
            recovery = connection.execute(
                """
                SELECT * FROM recoveries
                WHERE id = ?
                  AND scenario_id = 'api-quota'
                  AND execution_mode = 'sdk_stub'
                """,
                (recovery_id,),
            ).fetchone()
            if recovery is None:
                return None
            self._require_quota_recovery_provenance(recovery)
            if cast(int, recovery["quota_execution_contract"]) != 1:
                raise ExecutionConflictError("Quota recovery has no durable execution contract")
            rows = connection.execute(
                """
                SELECT executions.*
                FROM executions
                JOIN recoveries ON recoveries.id = executions.recovery_id
                WHERE executions.recovery_id = ?
                  AND recoveries.scenario_id = 'api-quota'
                  AND recoveries.execution_mode = 'sdk_stub'
                """,
                (recovery_id,),
            ).fetchall()
            if not rows:
                raise ExecutionConflictError("Quota recovery has no exact durable execution")
            if len(rows) != 1:
                raise ExecutionConflictError("Quota recovery has ambiguous durable executions")
            execution = self._require_exact_quota_execution_row(
                rows[0],
                recovery_id=recovery_id,
            )
            current_time = self._now()
            recovery_status = RecoveryStatus(cast(str, recovery["status"]))
            if recovery_status is RecoveryStatus.IN_PROGRESS:
                if execution.status not in {
                    "pending",
                    QUOTA_EXECUTION_RESULT_RECORDED,
                    "completed",
                }:
                    raise ExecutionConflictError(
                        "Quota execution has no dispatchable durable state"
                    )
                self._require_quota_execution_chronology(
                    recovery=recovery,
                    execution=rows[0],
                    terminal=False,
                    now=current_time,
                )
                self._require_pristine_quota_recovery(
                    connection,
                    recovery_id=recovery_id,
                    allowed_creation_statuses=frozenset({"started", "unknown"}),
                    now=current_time,
                )
                return execution
            if recovery_status not in {
                RecoveryStatus.COMPLETED,
                RecoveryStatus.OUTCOME_UNKNOWN,
            }:
                raise ExecutionConflictError("Quota recovery has no readable durable execution")
            if (
                recovery_status is RecoveryStatus.COMPLETED and execution.status != "completed"
            ) or (
                recovery_status is RecoveryStatus.OUTCOME_UNKNOWN
                and execution.status != RecoveryStatus.OUTCOME_UNKNOWN.value
            ):
                raise ExecutionConflictError("Quota terminal execution state does not match")
            self._require_quota_execution_chronology(
                recovery=recovery,
                execution=rows[0],
                terminal=True,
                now=current_time,
            )
            receipt = self._quota_receipt_for_execution_row(
                recovery=recovery,
                execution=execution,
            )
            specs = (
                self._quota_completed_transition_specs(
                    QuotaRecoveryResult.model_validate(execution.result_json)
                )
                if execution.status == "completed"
                else [self._quota_unknown_transition_spec(recovery_id)]
            )
            if not self._quota_terminal_bundle_matches(
                connection,
                recovery=recovery,
                execution=execution,
                receipt=receipt,
                specs=specs,
            ):
                raise ExecutionConflictError("Quota terminal execution evidence does not match")
            return execution

    @classmethod
    def _preflight_quota_execution_evidence(
        cls,
        connection: sqlite3.Connection,
        *,
        now: datetime,
    ) -> list[DurableExecution]:
        """Validate every durable SDK quota row in one database snapshot."""

        invalid_legacy = connection.execute(
            """
            SELECT quota_legacy_completions.recovery_id
            FROM quota_legacy_completions
            LEFT JOIN recoveries
              ON recoveries.id = quota_legacy_completions.recovery_id
            WHERE recoveries.id IS NULL
               OR recoveries.scenario_id != 'api-quota'
               OR recoveries.execution_mode != 'sdk_stub'
               OR recoveries.status != 'completed'
               OR recoveries.quota_execution_contract != 0
            LIMIT 1
            """
        ).fetchone()
        if invalid_legacy is not None:
            raise ExecutionConflictError("Legacy quota completion provenance is invalid")
        quarantined = connection.execute(
            """
            SELECT executions.recovery_id
            FROM executions
            JOIN recoveries ON recoveries.id = executions.recovery_id
            WHERE recoveries.scenario_id = 'api-quota'
              AND recoveries.execution_mode = 'sdk_stub'
              AND recoveries.quota_execution_contract = 1
              AND executions.status = ?
            LIMIT 1
            """,
            (QUOTA_EXECUTION_INVARIANT_FAILED,),
        ).fetchone()
        if quarantined is not None:
            raise ExecutionConflictError("Quota SDK invariant failure requires operator review")
        unsupported_mode = connection.execute(
            """
            SELECT id
            FROM recoveries
            WHERE scenario_id = 'api-quota'
              AND execution_mode NOT IN ('sdk_stub', 'replay_fixture')
            LIMIT 1
            """
        ).fetchone()
        if unsupported_mode is not None:
            raise ExecutionConflictError("API quota recovery has an unsupported execution mode")
        recovery_rows = connection.execute(
            """
            SELECT recoveries.*
            FROM recoveries
            WHERE recoveries.scenario_id = 'api-quota'
              AND recoveries.execution_mode = 'sdk_stub'
            ORDER BY recoveries.created_at ASC, recoveries.id ASC
            """
        ).fetchall()
        reconciliations: list[DurableExecution] = []
        for recovery in recovery_rows:
            recovery_id = cast(str, recovery["id"])
            cls._require_quota_recovery_provenance(recovery)
            recovery_status = RecoveryStatus(cast(str, recovery["status"]))
            if recovery_status is RecoveryStatus.IN_PROGRESS:
                if cast(int, recovery["quota_execution_contract"]) != 1:
                    raise ExecutionConflictError(
                        "In-progress quota recovery has no exact durable execution"
                    )
                execution_rows = connection.execute(
                    "SELECT * FROM executions WHERE recovery_id = ?",
                    (recovery_id,),
                ).fetchall()
                if len(execution_rows) != 1:
                    raise ExecutionConflictError(
                        "In-progress quota recovery has no exact durable execution"
                    )
                execution = cls._require_exact_quota_execution_row(
                    execution_rows[0],
                    recovery_id=recovery_id,
                )
                if execution.status not in {
                    "pending",
                    QUOTA_EXECUTION_RESULT_RECORDED,
                    "completed",
                }:
                    raise ExecutionConflictError(
                        "Quota execution has no reconcilable durable state"
                    )
                cls._require_quota_execution_chronology(
                    recovery=recovery,
                    execution=execution_rows[0],
                    terminal=False,
                    now=now,
                )
                cls._require_pristine_quota_recovery(
                    connection,
                    recovery_id=recovery_id,
                    allowed_creation_statuses=frozenset({"started", "unknown"}),
                    now=now,
                )
                reconciliations.append(execution)
                continue
            if recovery_status not in {
                RecoveryStatus.COMPLETED,
                RecoveryStatus.OUTCOME_UNKNOWN,
            }:
                raise ExecutionConflictError("SDK quota recovery has an invalid durable status")
            receipt_rows = connection.execute(
                "SELECT receipt_json FROM receipts WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchall()
            if len(receipt_rows) != 1:
                raise ExecutionConflictError("Terminal quota recovery has no exact durable receipt")
            try:
                receipt = RecoveryReceipt.model_validate_json(
                    cast(str, receipt_rows[0]["receipt_json"])
                )
            except (TypeError, ValueError):
                raise ExecutionConflictError("Terminal quota recovery receipt is invalid") from None
            if not cls._quota_public_terminal_bundle_matches(
                connection,
                row=recovery,
                receipt=receipt,
            ):
                raise ExecutionConflictError("Terminal quota recovery evidence is not canonical")
        return reconciliations

    def quota_evidence_is_ready(
        self,
        connection: sqlite3.Connection,
    ) -> bool:
        """Return whether all durable quota evidence passes startup preflight."""

        try:
            self._preflight_quota_execution_evidence(
                connection,
                now=self._now(),
            )
        except (
            ExecutionConflictError,
            ReceiptTransitionError,
            RecoveryNotFoundError,
            TypeError,
            ValueError,
        ):
            return False
        return True

    def list_quota_executions_needing_reconciliation(
        self,
    ) -> list[DurableExecution]:
        """Return unfinished exact quota claims/results after full startup preflight."""

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN")
            return self._preflight_quota_execution_evidence(
                connection,
                now=self._now(),
            )

    def finalize_completed_quota_execution(
        self,
        execution: DurableExecution,
    ) -> bool:
        """Atomically seal a committed quota result without provider redispatch."""

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._finalize_quota_execution_in_connection(
                connection,
                requested_execution=execution,
                expected_result=True,
                now=self._now(),
            )

    def finalize_pending_quota_execution_unknown(
        self,
        execution: DurableExecution,
    ) -> bool:
        """Atomically seal an unresolved quota dispatch claim as unknown."""

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._finalize_quota_execution_in_connection(
                connection,
                requested_execution=execution,
                expected_result=False,
                now=self._now(),
            )

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

        serialized_result = json.dumps(
            result_json,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = self._now()
            existing = connection.execute(
                "SELECT * FROM executions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if (
                    cast(str, existing["recovery_id"]) != recovery_id
                    or cast(str | None, existing["request_digest"]) != request_digest
                    or cast(str | None, existing["tool_call_id"]) != tool_call_id
                    or cast(str | None, existing["remedy_digest"]) != remedy_digest
                ):
                    raise ExecutionConflictError(
                        "Idempotency key was already used for a different execution"
                    )

            conflicting_execution = connection.execute(
                """
                SELECT 1
                FROM executions
                WHERE recovery_id = ? AND idempotency_key <> ?
                LIMIT 1
                """,
                (recovery_id, idempotency_key),
            ).fetchone()
            if conflicting_execution is not None:
                raise ExecutionConflictError("Recovery already has a different durable execution")

            if existing is not None:
                existing_shape = self._execution_persistence_shape(existing)
                if existing_shape == "completed":
                    try:
                        return self._execution_from_row(existing), False
                    except (TypeError, ValueError):
                        raise ExecutionConflictError(
                            "Completed durable execution evidence is invalid"
                        ) from None
                if existing_shape != "pending":
                    raise ExecutionConflictError(
                        "Durable execution has an invalid persistence state"
                    )

            self._require_new_execution_authorization(
                connection,
                recovery_id=recovery_id,
                tool_call_id=tool_call_id,
                remedy_digest=remedy_digest,
                now=now,
            )
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
            else:
                connection.execute(
                    """
                    UPDATE executions
                    SET status = 'completed', provider_execution = 1,
                        result_json = ?, updated_at = ?
                    WHERE idempotency_key = ?
                      AND status = 'pending'
                      AND provider_execution = 0
                      AND result_json IS NULL
                    """,
                    (serialized_result, now.isoformat(), idempotency_key),
                )
            row = connection.execute(
                "SELECT * FROM executions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is None:
                raise RuntimeError("Durable execution write did not persist")
            if self._execution_persistence_shape(row) != "completed":
                raise ExecutionConflictError(
                    "Durable execution write produced an invalid persistence state"
                )
            try:
                execution = self._execution_from_row(row)
            except (TypeError, ValueError):
                raise ExecutionConflictError(
                    "Completed durable execution evidence is invalid"
                ) from None
        return execution, True

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
                JOIN recoveries
                  ON recoveries.id = executions.recovery_id
                LEFT JOIN receipts
                  ON receipts.recovery_id = executions.recovery_id
                WHERE executions.status = 'completed'
                  AND executions.provider_execution = 1
                  AND recoveries.scenario_id = 'hotel'
                  AND NOT EXISTS (
                    SELECT 1 FROM approval_decisions
                    WHERE approval_decisions.recovery_id = executions.recovery_id
                  )
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

        now = self._now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._finalize_completed_execution_in_connection(
                connection,
                execution=execution,
                receipt=receipt,
                now=now,
            )

    def finalize_completed_execution_claim(
        self,
        execution: DurableExecution,
        *,
        receipt: RecoveryReceipt,
        claim: ApprovalDecisionClaim,
        response: ApprovalDecisionResponse,
    ) -> ApprovalDecisionResponse:
        """Atomically seal one committed result and its exact decision response."""

        now = self._now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            decision_row = connection.execute(
                "SELECT * FROM approval_decisions WHERE recovery_id = ?",
                (claim.recovery_id,),
            ).fetchone()
            if decision_row is None:
                raise ApprovalDecisionError(
                    "decision_unavailable",
                    claim.recovery_id,
                    status_code=409,
                )
            durable_claim = self._verified_decision_claim_from_row(
                decision_row,
                recovery_id=claim.recovery_id,
            )
            if (
                durable_claim.request != claim.request
                or durable_claim.request_fingerprint != claim.request_fingerprint
            ):
                raise ApprovalDecisionError(
                    "decision_id_conflict",
                    claim.recovery_id,
                    status_code=409,
                )
            if durable_claim.response is not None:
                if not self._completed_decision_has_terminal_evidence(
                    connection,
                    recovery_id=claim.recovery_id,
                    decision_row=decision_row,
                    claim=durable_claim,
                ):
                    raise ApprovalDecisionError(
                        "resume_incompatible",
                        claim.recovery_id,
                        status_code=409,
                    )
                return durable_claim.response
            reconcilable = self._reconcilable_completed_claim(
                connection,
                recovery_id=claim.recovery_id,
                decision_row=decision_row,
            )
            if reconcilable is None:
                raise ApprovalDecisionError(
                    "resume_incompatible",
                    claim.recovery_id,
                    status_code=409,
                )
            exact_claim, exact_execution = reconcilable
            if exact_claim != durable_claim or exact_execution != execution:
                raise ApprovalDecisionError(
                    "resume_incompatible",
                    claim.recovery_id,
                    status_code=409,
                )
            self._finalize_completed_execution_in_connection(
                connection,
                execution=exact_execution,
                receipt=receipt,
                now=now,
            )
            return self._complete_approval_decision_in_connection(
                connection,
                claim=exact_claim,
                response=response,
                now=now,
            )

    def _finalize_completed_execution_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        execution: DurableExecution,
        receipt: RecoveryReceipt,
        now: datetime,
    ) -> bool:
        """Seal one committed execution inside the caller's transaction."""

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
        receipt_json = receipt.model_dump_json(by_alias=True)
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
        recovery = connection.execute(
            "SELECT * FROM recoveries WHERE id = ?",
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
                raise ReceiptTransitionError("Existing receipt evidence mismatch") from None
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
                stored_terminal_data = json.loads(cast(str, terminal_event["data_json"]))
            except (TypeError, ValueError):
                raise ReceiptTransitionError("Existing terminal evidence mismatch") from None
            if (
                cast(str, terminal_event["type"]) != "recovery.completed"
                or stored_terminal_data != terminal_data
            ):
                raise ReceiptTransitionError("Existing terminal evidence mismatch")

        if existing_receipt is not None and terminal_events:
            pending = connection.execute(
                """
                SELECT status FROM pending_approvals
                WHERE recovery_id = ?
                """,
                (execution.recovery_id,),
            ).fetchone()
            remedy = connection.execute(
                """
                SELECT remedies.status
                FROM remedies
                JOIN pending_approvals
                  ON pending_approvals.recovery_id = remedies.recovery_id
                 AND pending_approvals.remedy_id = remedies.id
                WHERE remedies.recovery_id = ?
                """,
                (execution.recovery_id,),
            ).fetchone()
            if (
                cast(str, recovery["status"]) == RecoveryStatus.COMPLETED.value
                and cast(int, recovery["current_step"]) == 5
                and cast(str, recovery["current_step_summary"])
                == "Demo provider result verified and receipt sealed."
                and pending is not None
                and cast(str, pending["status"]) == "completed"
                and remedy is not None
                and cast(str, remedy["status"]) == "approved"
            ):
                return False

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

    def reset(self, session_key: str) -> None:
        """Detach one session and delete only detail no other session can access."""

        self._require_opaque_session_key(session_key)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            unresolved_creation = connection.execute(
                """
                SELECT 1
                FROM recovery_creations
                WHERE session_key = ?
                  AND status IN ('reserved', 'started', 'unknown')
                LIMIT 1
                """,
                (session_key,),
            ).fetchone()
            if unresolved_creation is not None:
                raise ResetCreationPendingError("Session has an unresolved recovery creation")
            now_text = self._now().isoformat()
            connection.execute(
                """
                UPDATE live_admissions
                SET released_at = COALESCE(released_at, ?)
                WHERE session_key = ?
                """,
                (now_text, session_key),
            )
            connection.execute(
                "DELETE FROM recovery_creations WHERE session_key = ?",
                (session_key,),
            )
            connection.execute(
                """
                DELETE FROM recoveries
                WHERE EXISTS (
                    SELECT 1
                    FROM recovery_access AS caller_access
                    WHERE caller_access.recovery_id = recoveries.id
                      AND caller_access.session_key = ?
                )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM recovery_access AS other_access
                    WHERE other_access.recovery_id = recoveries.id
                      AND other_access.session_key <> ?
                )
                """,
                (session_key, session_key),
            )
            connection.execute(
                "DELETE FROM recovery_access WHERE session_key = ?",
                (session_key,),
            )

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._closed_event.set()
