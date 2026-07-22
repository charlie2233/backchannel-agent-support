"""SQLite-backed recovery snapshots, ordered events, and replay receipts."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import Any, Literal, cast

from pydantic import JsonValue

from server.agents.live_models import ModelResponseMetadata
from server.agents.schemas import CommitRemedyArguments
from server.controls import (
    HASH_PREFIX,
    PublicCreationAdmissionError,
    PublicLiveAdmissionError,
)
from server.digest import remedy_consent_digest
from server.models import (
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    DecisionResponse,
    DeclineDecisionResponse,
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
from server.providers.quota_simulator import QuotaGrantResult

SDK_STUB_RECEIPT_BOUNDARY = (
    "Deterministic Agents SDK model and demo hotel adapter only; "
    "no OpenAI model call, real booking, or payment change."
)
QUOTA_SDK_STUB_RECEIPT_BOUNDARY = (
    "Deterministic Agents SDK quota model and demo quota adapter only; "
    "no OpenAI model call or real provider quota change."
)
QUOTA_SDK_STUB_AUTHORIZATION = (
    "Delegated quota authority covered the temporary grant; "
    "no human approval was requested."
)
QUOTA_SDK_STUB_VERIFICATIONS = (
    "Provider proof established the 1000 rpm base ceiling.",
    "The 1500 rpm temporary burst covered 1200 rpm demand in US for 900 seconds.",
    "The 250 USD-minor cost stayed within the delegated 500 USD-minor maximum.",
    "The base quota remained unchanged.",
    "Runtime permission was revoked after grant verification.",
)
QUOTA_PROTOCOL_PHASES = (
    "Detect",
    "Prove",
    "Negotiate",
    "Authorize",
    "Execute",
    "Verify & seal",
)
QUOTA_TERMINAL_SUMMARY = (
    "The grant was verified, runtime permission revoked, and receipt sealed."
)
PUBLIC_CREATION_USAGE_TABLE_SQL = """
CREATE TABLE public_creation_usage (
    identity_kind TEXT NOT NULL CHECK (
        identity_kind IN ('session', 'ip', 'global')
    ),
    identity_hash TEXT NOT NULL,
    usage_day TEXT NOT NULL,
    amount INTEGER NOT NULL DEFAULT 0 CHECK (amount >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (identity_kind, identity_hash, usage_day)
)
"""
_SCHEMA_SQL_TOKEN = re.compile(
    r"'(?:''|[^'])*'|>=|<=|<>|!=|[(),;]|[A-Za-z_][A-Za-z0-9_]*|[0-9]+"
)
OPENAI_LIVE_RECEIPT_BOUNDARY = (
    "Live OpenAI model orchestration and demo hotel adapter only; "
    "no real booking or payment change."
)
APPROVED_RECEIPT_AUTHORIZATION = "Approved Agents SDK commit_remedy interruption."
APPROVED_RECEIPT_VERIFICATIONS = (
    "Demo provider dispatch returned confirmed.",
    "Provider result stored under one idempotency key.",
    "Temporary permission revoked after terminal completion.",
)
DECLINED_RECEIPT_AUTHORIZATION = "Operator declined the exact pending remedy."
DECLINED_CLOSED_PROVIDER_RESULT = "Provider dispatch did not begin."
DECLINED_CLOSED_VERIFICATIONS = (
    "Human consent requested.",
    "Remedy declined by operator.",
    "Exact interruption rejected.",
    "No replacement action selected.",
    "Temporary permission revoked.",
    "Cancellation receipt sealed.",
)
DECLINED_UNKNOWN_PROVIDER_RESULT = (
    "Provider dispatch evidence exists; Backchannel does not claim "
    "cancellation or completion."
)
DECLINED_UNKNOWN_VERIFICATIONS = (
    "Human consent requested.",
    "Remedy declined by operator.",
    "Exact interruption rejected.",
    "Provider dispatch evidence requires manual reconciliation.",
    "Temporary permission revoked.",
    "Outcome-unknown receipt sealed.",
)


def non_replay_receipt_boundary(execution_mode: ExecutionMode) -> str:
    if execution_mode is ExecutionMode.OPENAI_LIVE:
        return OPENAI_LIVE_RECEIPT_BOUNDARY
    if execution_mode is ExecutionMode.SDK_STUB:
        return SDK_STUB_RECEIPT_BOUNDARY
    raise ValueError("Replay fixtures do not use the SDK receipt boundary")


def _canonical_schema_tokens(sql: str) -> tuple[str, ...] | None:
    """Tokenize exact owned DDL while allowing only whitespace and keyword case."""

    if "--" in sql or "/*" in sql or "*/" in sql:
        return None
    tokens: list[str] = []
    cursor = 0
    for matched in _SCHEMA_SQL_TOKEN.finditer(sql):
        if sql[cursor : matched.start()].strip():
            return None
        token = matched.group(0)
        tokens.append(token if token.startswith("'") else token.casefold())
        cursor = matched.end()
    if sql[cursor:].strip():
        return None
    if tokens and tokens[-1] == ";":
        tokens.pop()
    return tuple(tokens)

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
    model_metadata_json TEXT NOT NULL DEFAULT '[]',
    model_metadata_revision INTEGER NOT NULL DEFAULT 0
        CHECK (model_metadata_revision >= 0),
    state_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approval_decisions (
    recovery_id TEXT PRIMARY KEY REFERENCES recoveries(id) ON DELETE CASCADE,
    client_decision_id TEXT NOT NULL UNIQUE,
    decision TEXT NOT NULL CHECK (decision IN ('approve', 'decline')),
    remedy_id TEXT NOT NULL,
    remedy_digest TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('claimed', 'completed')),
    result_json TEXT,
    claimed_at TEXT NOT NULL,
    completed_at TEXT,
    resume_owner_id TEXT,
    resume_generation INTEGER NOT NULL DEFAULT 0 CHECK (resume_generation >= 0),
    resume_lease_expires_at TEXT,
    resume_heartbeat_at TEXT,
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

CREATE TABLE IF NOT EXISTS receipt_provenance_migrations (
    recovery_id TEXT PRIMARY KEY REFERENCES recoveries(id) ON DELETE CASCADE,
    migrated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS application_migrations (
    name TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_ledger (
    identity_kind TEXT NOT NULL CHECK (
        identity_kind IN ('session', 'ip', 'global', 'legacy')
    ),
    identity_hash TEXT NOT NULL,
    usage_day TEXT NOT NULL,
    amount INTEGER NOT NULL DEFAULT 0 CHECK (amount >= 0),
    last_admitted_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (identity_kind, identity_hash, usage_day)
);

CREATE TABLE IF NOT EXISTS public_creation_usage (
    identity_kind TEXT NOT NULL CHECK (
        identity_kind IN ('session', 'ip', 'global')
    ),
    identity_hash TEXT NOT NULL,
    usage_day TEXT NOT NULL,
    amount INTEGER NOT NULL DEFAULT 0 CHECK (amount >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (identity_kind, identity_hash, usage_day)
);

CREATE TABLE IF NOT EXISTS public_live_cooldowns (
    identity_kind TEXT NOT NULL CHECK (identity_kind IN ('session', 'ip')),
    identity_hash TEXT NOT NULL,
    last_admitted_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (identity_kind, identity_hash)
);

CREATE TABLE IF NOT EXISTS demo_sessions (
    session_hash TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recovery_access (
    recovery_id TEXT NOT NULL REFERENCES recoveries(id) ON DELETE CASCADE,
    session_hash TEXT NOT NULL,
    PRIMARY KEY (recovery_id, session_hash)
);

CREATE INDEX IF NOT EXISTS recovery_access_session_idx
ON recovery_access(session_hash);
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
    response: DecisionResponse | None = None

    @property
    def resume_required(self) -> bool:
        return self.response is None


@dataclass(frozen=True, slots=True)
class DecisionResumeLease:
    """One atomic owner, waiter, or completed replay decision."""

    disposition: Literal["owner", "wait", "replay"]
    claim: ApprovalDecisionClaim
    resume_owner_id: str | None
    resume_generation: int
    lease_expires_at: datetime | None


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
    model_metadata: tuple[ModelResponseMetadata, ...] = ()
    status: str = "pending"


@dataclass(frozen=True, slots=True)
class PermissionScopeRecord:
    """Exact temporary permission for one recovery, tool call, and remedy digest."""

    recovery_id: str
    tool_call_id: str
    remedy_digest: str
    action_digest: str
    status: str
    created_at: datetime
    activated_at: datetime | None
    revoked_at: datetime | None
    updated_at: datetime


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
            self._migrate_task6_permission_scopes(connection)
            self._migrate_task8_public_controls(connection)
            self._migrate_public_creation_usage(connection)
            self._migrate_recovery_access(connection)
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
            self._migrate_task6_receipts(connection)
            self._migrate_task7_receipt_provenance(connection)
            self._migrate_task8_inert_replay_fixtures(connection)

    @staticmethod
    def _recovery_access_schema_is_exact(connection: sqlite3.Connection) -> bool:
        columns = [
            (
                cast(str, row["name"]),
                cast(str, row["type"]).upper(),
                cast(int, row["notnull"]),
                cast(int, row["pk"]),
            )
            for row in connection.execute(
                "PRAGMA table_info(recovery_access)"
            ).fetchall()
        ]
        if columns != [
            ("recovery_id", "TEXT", 1, 1),
            ("session_hash", "TEXT", 1, 2),
        ]:
            return False
        foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(recovery_access)"
        ).fetchall()
        if len(foreign_keys) != 1:
            return False
        foreign_key = foreign_keys[0]
        if (
            cast(str, foreign_key["table"]),
            cast(str, foreign_key["from"]),
            cast(str, foreign_key["to"]),
            cast(str, foreign_key["on_delete"]).upper(),
        ) != ("recoveries", "recovery_id", "id", "CASCADE"):
            return False
        indexes = {
            cast(str, row["name"]): (
                cast(int, row["unique"]),
                cast(str, row["origin"]),
                cast(int, row["partial"]),
            )
            for row in connection.execute(
                "PRAGMA index_list(recovery_access)"
            ).fetchall()
        }
        if indexes != {
            "recovery_access_session_idx": (0, "c", 0),
            "sqlite_autoindex_recovery_access_1": (1, "pk", 0),
        }:
            return False
        index = connection.execute(
            "PRAGMA index_info(recovery_access_session_idx)"
        ).fetchall()
        return [cast(str, row["name"]) for row in index] == ["session_hash"]

    @staticmethod
    def _public_creation_usage_schema_is_exact(
        connection: sqlite3.Connection,
    ) -> bool:
        columns = [
            (
                cast(str, row["name"]),
                cast(str, row["type"]).upper(),
                cast(int, row["notnull"]),
                row["dflt_value"],
                cast(int, row["pk"]),
            )
            for row in connection.execute(
                "PRAGMA table_info(public_creation_usage)"
            ).fetchall()
        ]
        if columns != [
            ("identity_kind", "TEXT", 1, None, 1),
            ("identity_hash", "TEXT", 1, None, 2),
            ("usage_day", "TEXT", 1, None, 3),
            ("amount", "INTEGER", 1, "0", 0),
            ("updated_at", "TEXT", 1, None, 0),
        ]:
            return False
        if connection.execute(
            "PRAGMA foreign_key_list(public_creation_usage)"
        ).fetchall():
            return False
        indexes = {
            cast(str, row["name"]): (
                cast(int, row["unique"]),
                cast(str, row["origin"]),
                cast(int, row["partial"]),
            )
            for row in connection.execute(
                "PRAGMA index_list(public_creation_usage)"
            ).fetchall()
        }
        if indexes != {
            "public_creation_usage_day_idx": (0, "c", 0),
            "sqlite_autoindex_public_creation_usage_1": (1, "pk", 0),
        }:
            return False
        index_columns = connection.execute(
            "PRAGMA index_info(public_creation_usage_day_idx)"
        ).fetchall()
        if [cast(str, row["name"]) for row in index_columns] != ["usage_day"]:
            return False
        index_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            ("public_creation_usage_day_idx",),
        ).fetchone()
        if index_row is None or index_row["sql"] is None:
            return False
        index_sql = re.sub(r"\s+", " ", cast(str, index_row["sql"]).strip())
        if re.fullmatch(
            r"CREATE INDEX public_creation_usage_day_idx "
            r"ON public_creation_usage\s*\(\s*usage_day\s*\)",
            index_sql,
            flags=re.IGNORECASE,
        ) is None:
            return False
        table_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            ("public_creation_usage",),
        ).fetchone()
        if table_row is None or table_row["sql"] is None:
            return False
        return _canonical_schema_tokens(
            cast(str, table_row["sql"])
        ) == _canonical_schema_tokens(PUBLIC_CREATION_USAGE_TABLE_SQL)

    @classmethod
    def _migrate_public_creation_usage(cls, connection: sqlite3.Connection) -> None:
        """Add and then fail-closed validate the independent creation ledger."""

        try:
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS public_creation_usage_day_idx
                ON public_creation_usage(usage_day)
                """
            )
        except sqlite3.DatabaseError as error:
            raise RuntimeError("Unsupported public creation usage schema") from error
        if not cls._public_creation_usage_schema_is_exact(connection):
            raise RuntimeError("Unsupported public creation usage schema")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("Public creation usage migration violated foreign keys")

    @classmethod
    def _migrate_recovery_access(cls, connection: sqlite3.Connection) -> None:
        """Validate the exact fail-closed public recovery ownership schema."""

        if not cls._recovery_access_schema_is_exact(connection):
            raise RuntimeError("Unsupported recovery access session index")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("Recovery access migration violated foreign keys")

    @staticmethod
    def _migrate_task8_inert_replay_fixtures(
        connection: sqlite3.Connection,
    ) -> None:
        """Remove only legacy replay graphs that can never produce terminal evidence."""

        migration_name = "task8_terminal_replay_fixtures"
        if connection.execute(
            "SELECT 1 FROM application_migrations WHERE name = ?",
            (migration_name,),
        ).fetchone() is not None:
            return
        connection.execute(
            """
            DELETE FROM recoveries
            WHERE execution_mode = 'replay_fixture'
              AND status IN ('in_progress', 'pending_approval')
              AND NOT EXISTS (
                  SELECT 1 FROM receipts WHERE receipts.recovery_id = recoveries.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM pending_approvals
                  WHERE pending_approvals.recovery_id = recoveries.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM approval_decisions
                  WHERE approval_decisions.recovery_id = recoveries.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM executions
                  WHERE executions.recovery_id = recoveries.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM permission_scopes
                  WHERE permission_scopes.recovery_id = recoveries.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM remedies WHERE remedies.recovery_id = recoveries.id
              )
            """
        )
        connection.execute(
            "INSERT INTO application_migrations (name, applied_at) VALUES (?, ?)",
            (migration_name, datetime.now(UTC).isoformat()),
        )
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("Replay fixture migration violated foreign keys")

    @staticmethod
    def _migrate_task8_public_controls(connection: sqlite3.Connection) -> None:
        """Remove recovery-cascade and raw-session storage while preserving aggregates."""

        usage_columns = {
            cast(str, row["name"])
            for row in connection.execute("PRAGMA table_info(usage_ledger)").fetchall()
        }
        task8_usage_columns = {
            "identity_kind",
            "identity_hash",
            "usage_day",
            "amount",
            "last_admitted_at",
            "updated_at",
        }
        usage_foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(usage_ledger)"
        ).fetchall()
        if usage_columns == task8_usage_columns and usage_foreign_keys:
            current_rows = connection.execute(
                """
                SELECT identity_kind, identity_hash, usage_day, amount,
                       last_admitted_at, updated_at
                FROM usage_ledger
                """
            ).fetchall()
            connection.execute("DROP TABLE IF EXISTS usage_ledger_task8")
            connection.execute(
                """
                CREATE TABLE usage_ledger_task8 (
                    identity_kind TEXT NOT NULL CHECK (
                        identity_kind IN ('session', 'ip', 'global', 'legacy')
                    ),
                    identity_hash TEXT NOT NULL,
                    usage_day TEXT NOT NULL,
                    amount INTEGER NOT NULL DEFAULT 0 CHECK (amount >= 0),
                    last_admitted_at TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (identity_kind, identity_hash, usage_day)
                )
                """
            )
            connection.executemany(
                """
                INSERT INTO usage_ledger_task8 (
                    identity_kind, identity_hash, usage_day, amount,
                    last_admitted_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [tuple(row) for row in current_rows],
            )
            connection.execute("DROP TABLE usage_ledger")
            connection.execute("ALTER TABLE usage_ledger_task8 RENAME TO usage_ledger")
        elif usage_columns != task8_usage_columns:
            legacy_columns = {"id", "recovery_id", "category", "amount", "recorded_at"}
            if usage_columns != legacy_columns:
                raise RuntimeError("Unsupported usage ledger schema")
            legacy_rows = connection.execute(
                "SELECT recovery_id, category, amount, recorded_at FROM usage_ledger"
            ).fetchall()
            connection.execute("DROP TABLE IF EXISTS usage_ledger_task8")
            connection.execute(
                """
                CREATE TABLE usage_ledger_task8 (
                    identity_kind TEXT NOT NULL CHECK (
                        identity_kind IN ('session', 'ip', 'global', 'legacy')
                    ),
                    identity_hash TEXT NOT NULL,
                    usage_day TEXT NOT NULL,
                    amount INTEGER NOT NULL DEFAULT 0 CHECK (amount >= 0),
                    last_admitted_at TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (identity_kind, identity_hash, usage_day)
                )
                """
            )
            for row in legacy_rows:
                recorded_at = cast(str, row["recorded_at"])
                try:
                    recorded = datetime.fromisoformat(recorded_at)
                except ValueError as error:
                    raise RuntimeError("Legacy usage timestamp is invalid") from error
                if recorded.tzinfo is None:
                    recorded = recorded.replace(tzinfo=UTC)
                else:
                    recorded = recorded.astimezone(UTC)
                identity_hash = "sha256:" + hashlib.sha256(
                    (
                        f"legacy\0{cast(str, row['recovery_id'])}\0"
                        f"{cast(str, row['category'])}"
                    ).encode()
                ).hexdigest()
                connection.execute(
                    """
                    INSERT INTO usage_ledger_task8 (
                        identity_kind, identity_hash, usage_day, amount,
                        last_admitted_at, updated_at
                    ) VALUES ('legacy', ?, ?, ?, NULL, ?)
                    ON CONFLICT(identity_kind, identity_hash, usage_day) DO UPDATE SET
                        amount = amount + excluded.amount,
                        updated_at = excluded.updated_at
                    """,
                    (
                        identity_hash,
                        recorded.date().isoformat(),
                        max(cast(int, row["amount"]), 0),
                        recorded.isoformat(),
                    ),
                )
            connection.execute("DROP TABLE usage_ledger")
            connection.execute("ALTER TABLE usage_ledger_task8 RENAME TO usage_ledger")

        connection.execute(
            """
            INSERT INTO public_live_cooldowns (
                identity_kind, identity_hash, last_admitted_at, updated_at
            )
            SELECT identity_kind, identity_hash, MAX(last_admitted_at), MAX(updated_at)
            FROM usage_ledger
            WHERE identity_kind IN ('session', 'ip')
              AND last_admitted_at IS NOT NULL
            GROUP BY identity_kind, identity_hash
            ON CONFLICT(identity_kind, identity_hash) DO UPDATE SET
                last_admitted_at = MAX(
                    public_live_cooldowns.last_admitted_at,
                    excluded.last_admitted_at
                ),
                updated_at = MAX(public_live_cooldowns.updated_at, excluded.updated_at)
            """
        )

        session_columns = {
            cast(str, row["name"])
            for row in connection.execute("PRAGMA table_info(demo_sessions)").fetchall()
        }
        task8_session_columns = {
            "session_hash",
            "created_at",
            "last_seen_at",
            "expires_at",
        }
        if session_columns != task8_session_columns:
            legacy_session_columns = {"id", "created_at", "expires_at"}
            if session_columns != legacy_session_columns:
                raise RuntimeError("Unsupported demo session schema")
            connection.execute("DROP TABLE IF EXISTS demo_sessions_task8")
            connection.execute(
                """
                CREATE TABLE demo_sessions_task8 (
                    session_hash TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )
            # Legacy rows contain attacker-supplied plaintext identifiers. Invalidate
            # them; aggregate usage was migrated independently above.
            connection.execute("DROP TABLE demo_sessions")
            connection.execute("ALTER TABLE demo_sessions_task8 RENAME TO demo_sessions")
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError("Public-control migration violated foreign keys")

    @staticmethod
    def _migrate_task3_pending_approvals(connection: sqlite3.Connection) -> None:
        """Rebuild every legacy envelope table to the exact Task 7 contract."""

        columns = {
            cast(str, row["name"])
            for row in connection.execute(
                "PRAGMA table_info(pending_approvals)"
            ).fetchall()
        }
        task7_columns = {
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
            "model_metadata_json",
            "model_metadata_revision",
            "state_json",
            "status",
            "created_at",
            "updated_at",
        }
        if columns == task7_columns:
            rows = connection.execute(
                """
                SELECT model_metadata_json, model_metadata_revision
                FROM pending_approvals
                """
            ).fetchall()
            for row in rows:
                metadata = json.loads(cast(str, row["model_metadata_json"]))
                if (
                    not isinstance(metadata, list)
                    or cast(int, row["model_metadata_revision"]) != len(metadata)
                ):
                    raise RuntimeError("Stored model metadata revision mismatch")
            return
        task7_without_revision = task7_columns - {"model_metadata_revision"}
        if columns == task7_without_revision:
            connection.execute(
                "ALTER TABLE pending_approvals ADD COLUMN "
                "model_metadata_revision INTEGER NOT NULL DEFAULT 0 "
                "CHECK (model_metadata_revision >= 0)"
            )
            rows = connection.execute(
                "SELECT tool_call_id, model_metadata_json FROM pending_approvals"
            ).fetchall()
            for row in rows:
                metadata = json.loads(cast(str, row["model_metadata_json"]))
                if not isinstance(metadata, list):
                    raise RuntimeError("Stored model metadata is not a JSON array")
                connection.execute(
                    "UPDATE pending_approvals SET model_metadata_revision = ? "
                    "WHERE tool_call_id = ?",
                    (len(metadata), cast(str, row["tool_call_id"])),
                )
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
        sdk_version_source = (
            legacy_value("sdk_version")
            if {"action_digest", "remedy_id", "consent_digest"} <= columns
            else "'legacy-incompatible'"
        )

        connection.commit()
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DROP TABLE IF EXISTS pending_approvals_task7")
            connection.execute(
                """
                CREATE TABLE pending_approvals_task7 (
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
                    model_metadata_json TEXT NOT NULL,
                    model_metadata_revision INTEGER NOT NULL DEFAULT 0
                        CHECK (model_metadata_revision >= 0),
                    state_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                f"""
                INSERT INTO pending_approvals_task7 (
                    tool_call_id, recovery_id, sdk_version, protocol_version,
                    agent_graph_version, definition_digest, root_trace_id,
                    execution_mode, action_digest, remedy_id, consent_digest,
                    model_metadata_json, model_metadata_revision, state_json, status,
                    created_at, updated_at
                )
                SELECT
                    tool_call_id,
                    recovery_id,
                    {sdk_version_source},
                    {legacy_value("protocol_version")},
                    {legacy_value("agent_graph_version")},
                    {legacy_value("definition_digest")},
                    {legacy_value("root_trace_id")},
                    {execution_mode_source},
                    {action_source},
                    {legacy_value("remedy_id")},
                    {legacy_value("consent_digest")},
                    {legacy_value("model_metadata_json", "'[]'")},
                    {legacy_value("model_metadata_revision", "0")},
                    {state_source},
                    status,
                    created_at,
                    updated_at
                FROM pending_approvals
                """
            )
            connection.execute("DROP TABLE pending_approvals")
            connection.execute(
                "ALTER TABLE pending_approvals_task7 RENAME TO pending_approvals"
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
        """Migrate decision actions and add durable resume-owner fencing."""

        columns = {
            cast(str, row["name"])
            for row in connection.execute(
                "PRAGMA table_info(approval_decisions)"
            ).fetchall()
        }
        task6_columns = {
            "recovery_id",
            "client_decision_id",
            "decision",
            "remedy_id",
            "remedy_digest",
            "tool_call_id",
            "request_fingerprint",
            "status",
            "result_json",
            "claimed_at",
            "completed_at",
        }
        lease_columns = {
            "resume_owner_id",
            "resume_generation",
            "resume_lease_expires_at",
            "resume_heartbeat_at",
        }
        if columns == task6_columns | lease_columns:
            return
        if columns == task6_columns:
            connection.execute("ALTER TABLE approval_decisions ADD COLUMN resume_owner_id TEXT")
            connection.execute(
                "ALTER TABLE approval_decisions ADD COLUMN "
                "resume_generation INTEGER NOT NULL DEFAULT 0 "
                "CHECK (resume_generation >= 0)"
            )
            connection.execute(
                "ALTER TABLE approval_decisions ADD COLUMN resume_lease_expires_at TEXT"
            )
            connection.execute(
                "ALTER TABLE approval_decisions ADD COLUMN resume_heartbeat_at TEXT"
            )
            return
        task5_columns = task6_columns - {"decision"}
        if columns != task5_columns:
            raise RuntimeError("Unsupported approval decision schema")

        rows = connection.execute("SELECT * FROM approval_decisions").fetchall()
        migrated_rows: list[tuple[object, ...]] = []
        for row in rows:
            request = ApprovalDecisionRequest(
                decision="approve",
                clientDecisionId=cast(str, row["client_decision_id"]),
                remedyId=cast(str, row["remedy_id"]),
                remedyDigest=cast(str, row["remedy_digest"]),
                toolCallId=cast(str, row["tool_call_id"]),
            )
            fingerprint = SQLiteStore._decision_fingerprint(
                cast(str, row["recovery_id"]),
                request,
            )
            result_json = cast(str | None, row["result_json"])
            if result_json is not None:
                parsed = json.loads(result_json)
                if not isinstance(parsed, dict):
                    raise RuntimeError("Legacy decision response is not an object")
                parsed["decision"] = "approve"
                result_json = json.dumps(
                    parsed,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            migrated_rows.append(
                (
                    cast(str, row["recovery_id"]),
                    cast(str, row["client_decision_id"]),
                    "approve",
                    cast(str, row["remedy_id"]),
                    cast(str, row["remedy_digest"]),
                    cast(str, row["tool_call_id"]),
                    fingerprint,
                    cast(str, row["status"]),
                    result_json,
                    cast(str, row["claimed_at"]),
                    cast(str | None, row["completed_at"]),
                )
            )

        connection.commit()
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DROP TABLE IF EXISTS approval_decisions_task6")
            connection.execute(
                """
                CREATE TABLE approval_decisions_task6 (
                    recovery_id TEXT PRIMARY KEY
                        REFERENCES recoveries(id) ON DELETE CASCADE,
                    client_decision_id TEXT NOT NULL UNIQUE,
                    decision TEXT NOT NULL CHECK (decision IN ('approve', 'decline')),
                    remedy_id TEXT NOT NULL,
                    remedy_digest TEXT NOT NULL,
                    tool_call_id TEXT NOT NULL,
                    request_fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('claimed', 'completed')),
                    result_json TEXT,
                    claimed_at TEXT NOT NULL,
                    completed_at TEXT,
                    resume_owner_id TEXT,
                    resume_generation INTEGER NOT NULL DEFAULT 0
                        CHECK (resume_generation >= 0),
                    resume_lease_expires_at TEXT,
                    resume_heartbeat_at TEXT,
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
            connection.executemany(
                """
                INSERT INTO approval_decisions_task6 (
                    recovery_id, client_decision_id, decision, remedy_id,
                    remedy_digest, tool_call_id, request_fingerprint, status,
                    result_json, claimed_at, completed_at, resume_owner_id,
                    resume_generation, resume_lease_expires_at, resume_heartbeat_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, NULL, NULL)
                """,
                migrated_rows,
            )
            connection.execute("DROP TABLE approval_decisions")
            connection.execute(
                "ALTER TABLE approval_decisions_task6 RENAME TO approval_decisions"
            )
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError("Decision migration violated foreign keys")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _migrate_task6_permission_scopes(connection: sqlite3.Connection) -> None:
        """Create exact temporary scopes and derive truthful Task 5 states."""

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS permission_scopes (
                recovery_id TEXT PRIMARY KEY
                    REFERENCES recoveries(id) ON DELETE CASCADE,
                tool_call_id TEXT NOT NULL UNIQUE,
                remedy_digest TEXT NOT NULL,
                action_digest TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'revoked')),
                created_at TEXT NOT NULL,
                activated_at TEXT,
                revoked_at TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        now = datetime.now(UTC).isoformat()
        pending_rows = connection.execute(
            """
            SELECT pending_approvals.*, recoveries.status AS recovery_status,
                   approval_decisions.decision AS decision_action,
                   approval_decisions.status AS decision_status,
                   EXISTS (
                       SELECT 1 FROM executions
                       WHERE executions.recovery_id = pending_approvals.recovery_id
                   ) AS has_execution
            FROM pending_approvals
            JOIN recoveries ON recoveries.id = pending_approvals.recovery_id
            LEFT JOIN approval_decisions
              ON approval_decisions.recovery_id = pending_approvals.recovery_id
            WHERE NOT EXISTS (
                SELECT 1 FROM permission_scopes
                WHERE permission_scopes.recovery_id = pending_approvals.recovery_id
            )
            """
        ).fetchall()
        for row in pending_rows:
            decision = cast(str | None, row["decision_action"])
            recovery_status = cast(str, row["recovery_status"])
            has_execution = bool(cast(int, row["has_execution"]))
            if (
                recovery_status
                in {
                    RecoveryStatus.COMPLETED.value,
                    RecoveryStatus.CLOSED_WITHOUT_ACTION.value,
                    RecoveryStatus.OUTCOME_UNKNOWN.value,
                }
                or has_execution
                or cast(str, row["status"]) == "completed"
            ):
                scope_status = "revoked"
                activated_at = cast(str, row["updated_at"]) if decision == "approve" else None
                revoked_at = cast(str, row["updated_at"])
            elif decision == "approve":
                scope_status = "active"
                activated_at = cast(str, row["updated_at"])
                revoked_at = None
            else:
                scope_status = "pending"
                activated_at = None
                revoked_at = None
            connection.execute(
                """
                INSERT INTO permission_scopes (
                    recovery_id, tool_call_id, remedy_digest, action_digest,
                    status, created_at, activated_at, revoked_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cast(str, row["recovery_id"]),
                    cast(str, row["tool_call_id"]),
                    cast(str, row["consent_digest"]),
                    cast(str, row["action_digest"]),
                    scope_status,
                    cast(str, row["created_at"]),
                    activated_at,
                    revoked_at,
                    now,
                ),
            )

    @staticmethod
    def _migrate_task6_receipts(connection: sqlite3.Connection) -> None:
        """Enrich truthful Task 5 approval receipts with closed-scope evidence."""

        rows = connection.execute(
            """
            SELECT receipts.recovery_id, receipts.receipt_json,
                   recoveries.execution_mode,
                   receipt_provenance_migrations.recovery_id AS migration_marker
            FROM receipts
            JOIN recoveries ON recoveries.id = receipts.recovery_id
            LEFT JOIN receipt_provenance_migrations
              ON receipt_provenance_migrations.recovery_id = receipts.recovery_id
            """
        ).fetchall()
        for row in rows:
            if cast(str, row["execution_mode"]) != ExecutionMode.SDK_STUB.value:
                continue
            raw = json.loads(cast(str, row["receipt_json"]))
            if not isinstance(raw, dict):
                raise RuntimeError("Legacy receipt is not a JSON object")
            if raw.get("quotaEvidence") is not None:
                # Quota receipts intentionally have no human-decision envelope.
                # Task 7 validates their complete typed provenance graph.
                continue
            if raw.get("decision") in {"approved", "declined"}:
                continue
            if row["migration_marker"] is not None:
                raise ReceiptTransitionError("Stored receipt decision evidence is missing")
            recovery_id = cast(str, row["recovery_id"])
            decision = connection.execute(
                "SELECT * FROM approval_decisions WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone()
            executions = connection.execute(
                "SELECT * FROM executions WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchall()
            if (
                decision is None
                or cast(str, decision["decision"]) != "approve"
                or len(executions) != 1
                or not bool(cast(int, executions[0]["provider_execution"]))
            ):
                raise RuntimeError(
                    "Legacy SDK receipt lacks a provable approved execution"
                )
            remedy_digest = cast(str, decision["remedy_digest"])
            raw.update(
                {
                    "decision": "approved",
                    "decisionRemedyDigest": remedy_digest,
                    "executionCount": 1,
                    "providerDispatchStarted": True,
                    "exactInterruptionRejected": False,
                    "permissionRevoked": True,
                    "scopeClosed": True,
                }
            )
            verification = raw.get("verificationResults")
            if not isinstance(verification, list) or not all(
                isinstance(item, str) for item in verification
            ):
                raise RuntimeError("Legacy SDK receipt verification is invalid")
            permission_statement = "Temporary permission revoked after terminal completion."
            if permission_statement not in verification:
                verification.append(permission_statement)
            migrated = RecoveryReceipt.model_validate(raw)
            connection.execute(
                "UPDATE receipts SET receipt_json = ? WHERE recovery_id = ?",
                (
                    migrated.model_dump_json(by_alias=True, exclude_none=True),
                    recovery_id,
                ),
            )
            terminal = connection.execute(
                """
                SELECT id FROM events
                WHERE recovery_id = ? AND terminal = 1
                """,
                (recovery_id,),
            ).fetchall()
            if len(terminal) > 1:
                raise RuntimeError("Legacy SDK receipt has ambiguous terminal events")
            if terminal:
                terminal_data: dict[str, JsonValue] = {
                    "decision": "approved",
                    "decisionRemedyDigest": remedy_digest,
                    "executionCount": 1,
                    "exactInterruptionRejected": False,
                    "permissionRevoked": True,
                    "recoveryId": recovery_id,
                    "executionMode": ExecutionMode.SDK_STUB.value,
                    "providerExecution": True,
                    "providerDispatchStarted": True,
                    "approvedRemedyDigest": remedy_digest,
                    "phase": "Verify & seal",
                    "scopeClosed": True,
                    "summary": "Committed demo-provider result finalized after restart.",
                }
                connection.execute(
                    """
                    UPDATE events
                    SET type = 'recovery.completed', data_json = ?
                    WHERE id = ?
                    """,
                    (
                        json.dumps(
                            terminal_data,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        cast(int, terminal[0]["id"]),
                    ),
                )

    @staticmethod
    def _receipt_provenance_from_pending(
        pending: sqlite3.Row,
    ) -> tuple[dict[str, str], list[str]]:
        try:
            raw_metadata = json.loads(cast(str, pending["model_metadata_json"]))
            if not isinstance(raw_metadata, list):
                raise ValueError
            metadata = tuple(
                ModelResponseMetadata.from_json_value(item) for item in raw_metadata
            )
        except (TypeError, ValueError):
            raise ReceiptTransitionError("Stored receipt provenance is invalid") from None
        definition_digest = cast(str, pending["definition_digest"])
        expected = {
            "rootTraceId": cast(str, pending["root_trace_id"]),
            "sdkVersion": cast(str, pending["sdk_version"]),
            "protocolVersion": cast(str, pending["protocol_version"]),
            "agentGraphVersion": cast(str, pending["agent_graph_version"]),
            "promptToolSchemaHash": definition_digest,
        }
        if (
            not all(value.strip() for value in expected.values())
            or re.fullmatch(r"[0-9a-f]{64}", definition_digest) is None
        ):
            raise ReceiptTransitionError("Stored receipt provenance is invalid")
        model_ids = list(dict.fromkeys(item.returned_model for item in metadata))
        return expected, model_ids

    @staticmethod
    def _validate_quota_event_graph(
        connection: sqlite3.Connection,
        recovery_id: str,
        *,
        runtime: bool,
    ) -> None:
        rows = connection.execute(
            """
            SELECT seq, type, terminal, data_json FROM events
            WHERE recovery_id = ? ORDER BY seq ASC
            """,
            (recovery_id,),
        ).fetchall()
        if len(rows) != 7 or [cast(int, row["seq"]) for row in rows] != list(
            range(1, 8)
        ):
            raise ReceiptTransitionError("Stored quota event evidence is incomplete")
        try:
            data = [json.loads(cast(str, row["data_json"])) for row in rows]
        except (TypeError, ValueError):
            raise ReceiptTransitionError("Stored quota event evidence is invalid") from None
        if not all(isinstance(item, dict) for item in data):
            raise ReceiptTransitionError("Stored quota event evidence is invalid")
        if [item.get("phase") for item in data[1:]] != list(QUOTA_PROTOCOL_PHASES):
            raise ReceiptTransitionError("Stored quota protocol phase evidence mismatch")
        if [bool(cast(int, row["terminal"])) for row in rows] != [
            False,
            False,
            False,
            False,
            False,
            False,
            True,
        ]:
            raise ReceiptTransitionError("Stored quota terminal evidence mismatch")
        terminal = data[-1]
        expected_terminal = {
            "providerExecution": runtime,
            "grantVerified": True,
        }
        if runtime:
            expected_terminal.update(
                {
                    "permissionRevoked": True,
                    "scopeClosed": True,
                }
            )
        if cast(str, rows[-1]["type"]) != "recovery.completed" or any(
            terminal.get(key) != value for key, value in expected_terminal.items()
        ):
            raise ReceiptTransitionError("Stored quota terminal evidence mismatch")

    @classmethod
    def _validate_durable_receipt_evidence(
        cls,
        connection: sqlite3.Connection,
        receipt: RecoveryReceipt,
        *,
        phase: Literal["preseal", "sealed"],
    ) -> None:
        """Bind a non-replay receipt to its complete durable evidence graph."""

        recovery_rows = connection.execute(
            "SELECT * FROM recoveries WHERE id = ?",
            (receipt.recovery_id,),
        ).fetchall()
        if len(recovery_rows) != 1:
            raise ReceiptTransitionError("Stored receipt recovery evidence is missing")
        recovery = recovery_rows[0]
        try:
            recovery_mode = ExecutionMode(cast(str, recovery["execution_mode"]))
            scenario_id = ScenarioId(cast(str, recovery["scenario_id"]))
        except ValueError:
            raise ReceiptTransitionError("Stored receipt evidence is invalid") from None
        if receipt.execution_mode is not recovery_mode:
            raise ReceiptTransitionError("Stored receipt provenance mismatch")
        is_quota_scenario = scenario_id is ScenarioId.API_QUOTA
        has_quota_evidence = receipt.quota_evidence is not None
        if is_quota_scenario != has_quota_evidence:
            raise ReceiptTransitionError(
                "Stored receipt scenario and quota evidence mismatch"
            )
        if recovery_mode is ExecutionMode.REPLAY_FIXTURE:
            if phase != "sealed":
                raise ReceiptTransitionError("Replay receipts cannot enter SDK finalization")
            terminal_count = cast(
                int,
                connection.execute(
                    """
                    SELECT COUNT(*) FROM events
                    WHERE recovery_id = ? AND terminal = 1
                    """,
                    (receipt.recovery_id,),
                ).fetchone()[0],
            )
            if (
                cast(str, recovery["status"]) != RecoveryStatus.COMPLETED.value
                or receipt.status != RecoveryStatus.COMPLETED.value
                or terminal_count != 1
            ):
                raise ReceiptTransitionError("Stored replay receipt is not terminal")
            if has_quota_evidence:
                quota_evidence = receipt.quota_evidence
                assert quota_evidence is not None
                if quota_evidence.source != "recorded_fixture":
                    raise ReceiptTransitionError(
                        "Stored replay quota provenance mismatch"
                    )
                for table in (
                    "executions",
                    "permission_scopes",
                    "pending_approvals",
                    "approval_decisions",
                    "remedies",
                ):
                    count = cast(
                        int,
                        connection.execute(
                            f"SELECT COUNT(*) FROM {table} WHERE recovery_id = ?",
                            (receipt.recovery_id,),
                        ).fetchone()[0],
                    )
                    if count != 0:
                        raise ReceiptTransitionError(
                            "Stored replay quota contains runtime evidence"
                        )
                cls._validate_quota_event_graph(
                    connection,
                    receipt.recovery_id,
                    runtime=False,
                )
            return

        if has_quota_evidence:
            quota_evidence = receipt.quota_evidence
            assert quota_evidence is not None
            if phase != "sealed" or recovery_mode is not ExecutionMode.SDK_STUB:
                raise ReceiptTransitionError(
                    "Quota SDK receipts require sealed local provenance"
                )
            if (
                quota_evidence.source != "sdk_simulator"
                or cast(str, recovery["status"]) != RecoveryStatus.COMPLETED.value
                or cast(int, recovery["current_step"]) != 5
                or cast(str, recovery["current_step_summary"])
                != QUOTA_TERMINAL_SUMMARY
                or receipt.boundary != QUOTA_SDK_STUB_RECEIPT_BOUNDARY
                or receipt.authorization_source != QUOTA_SDK_STUB_AUTHORIZATION
                or tuple(receipt.verification_results)
                != QUOTA_SDK_STUB_VERIFICATIONS
                or receipt.protocol_version != "backchannel.quota.v1"
                or receipt.agent_graph_version != "backchannel.quota-agent.v1"
            ):
                raise ReceiptTransitionError(
                    "Stored quota receipt narrative or provenance mismatch"
                )
            for table in ("pending_approvals", "approval_decisions", "remedies"):
                count = cast(
                    int,
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE recovery_id = ?",
                        (receipt.recovery_id,),
                    ).fetchone()[0],
                )
                if count != 0:
                    raise ReceiptTransitionError(
                        "Stored quota receipt contains human-decision evidence"
                    )
            executions = connection.execute(
                "SELECT * FROM executions WHERE recovery_id = ?",
                (receipt.recovery_id,),
            ).fetchall()
            scopes = connection.execute(
                "SELECT * FROM permission_scopes WHERE recovery_id = ?",
                (receipt.recovery_id,),
            ).fetchall()
            if len(executions) != 1 or len(scopes) != 1:
                raise ReceiptTransitionError(
                    "Stored quota execution or permission evidence is missing"
                )
            execution = executions[0]
            scope = scopes[0]
            try:
                result = QuotaGrantResult.model_validate_json(
                    cast(str, execution["result_json"])
                )
            except (TypeError, ValueError):
                raise ReceiptTransitionError(
                    "Stored quota provider result is invalid"
                ) from None
            request_digest = cast(str | None, execution["request_digest"])
            tool_call_id = cast(str | None, execution["tool_call_id"])
            if (
                cast(str, execution["status"]) != "completed"
                or not bool(cast(int, execution["provider_execution"]))
                or request_digest is None
                or re.fullmatch(r"[0-9a-f]{64}", request_digest) is None
                or tool_call_id is None
                or not tool_call_id.strip()
                or execution["remedy_digest"] is not None
                or result.provider_result != receipt.provider_result
                or cast(str, scope["tool_call_id"]) != tool_call_id
                or cast(str, scope["remedy_digest"]) != request_digest
                or cast(str, scope["action_digest"]) != request_digest
                or cast(str, scope["status"]) != "revoked"
                or scope["revoked_at"] is None
            ):
                raise ReceiptTransitionError(
                    "Stored quota execution or permission evidence mismatch"
                )
            cls._validate_quota_event_graph(
                connection,
                receipt.recovery_id,
                runtime=True,
            )
            return

        pending_rows = connection.execute(
            "SELECT * FROM pending_approvals WHERE recovery_id = ?",
            (receipt.recovery_id,),
        ).fetchall()
        if len(pending_rows) != 1:
            raise ReceiptTransitionError(
                "Stored non-replay receipt requires one unambiguous pending envelope"
            )
        pending = pending_rows[0]
        expected_provenance, expected_model_ids = cls._receipt_provenance_from_pending(
            pending
        )
        if (
            receipt.execution_mode.value != cast(str, pending["execution_mode"])
            or receipt.root_trace_id != expected_provenance["rootTraceId"]
            or receipt.sdk_version != expected_provenance["sdkVersion"]
            or receipt.protocol_version != expected_provenance["protocolVersion"]
            or receipt.agent_graph_version
            != expected_provenance["agentGraphVersion"]
            or receipt.prompt_tool_schema_hash
            != expected_provenance["promptToolSchemaHash"]
            or receipt.model_ids != expected_model_ids
        ):
            raise ReceiptTransitionError("Stored receipt provenance mismatch")
        if receipt.boundary != non_replay_receipt_boundary(recovery_mode):
            raise ReceiptTransitionError("Stored receipt narrative evidence mismatch")

        decision_rows = connection.execute(
            "SELECT * FROM approval_decisions WHERE recovery_id = ?",
            (receipt.recovery_id,),
        ).fetchall()
        scope_rows = connection.execute(
            "SELECT * FROM permission_scopes WHERE recovery_id = ?",
            (receipt.recovery_id,),
        ).fetchall()
        execution_rows = connection.execute(
            "SELECT * FROM executions WHERE recovery_id = ? ORDER BY created_at ASC",
            (receipt.recovery_id,),
        ).fetchall()
        terminal_rows = connection.execute(
            """
            SELECT type, data_json FROM events
            WHERE recovery_id = ? AND terminal = 1
            ORDER BY seq ASC
            """,
            (receipt.recovery_id,),
        ).fetchall()
        remedy_rows = connection.execute(
            "SELECT * FROM remedies WHERE recovery_id = ? AND id = ?",
            (receipt.recovery_id, cast(str, pending["remedy_id"])),
        ).fetchall()
        if len(decision_rows) != 1 or len(scope_rows) != 1 or len(remedy_rows) != 1:
            raise ReceiptTransitionError("Stored receipt durable evidence is missing")
        if (phase == "sealed" and len(terminal_rows) != 1) or (
            phase == "preseal" and terminal_rows
        ):
            raise ReceiptTransitionError("Stored receipt terminal evidence mismatch")

        decision_row = decision_rows[0]
        scope = scope_rows[0]
        remedy = remedy_rows[0]
        try:
            claim = cls._decision_claim_from_row(decision_row)
        except (TypeError, ValueError):
            raise ReceiptTransitionError("Stored receipt decision evidence is invalid") from None
        request = claim.request
        consent_digest = cast(str, pending["consent_digest"])
        if (
            request.remedy_id != cast(str, pending["remedy_id"])
            or request.remedy_digest != consent_digest
            or request.tool_call_id != cast(str, pending["tool_call_id"])
            or receipt.decision_remedy_digest != consent_digest
            or cast(str, scope["tool_call_id"]) != request.tool_call_id
            or cast(str, scope["remedy_digest"]) != consent_digest
            or cast(str, scope["action_digest"])
            != cast(str, pending["action_digest"])
            or cast(str | None, remedy["digest"]) != consent_digest
        ):
            raise ReceiptTransitionError("Stored receipt durable evidence mismatch")

        if phase == "sealed":
            try:
                terminal_data = json.loads(cast(str, terminal_rows[0]["data_json"]))
            except (TypeError, ValueError):
                raise ReceiptTransitionError(
                    "Stored receipt terminal evidence is invalid"
                ) from None
            if not isinstance(terminal_data, dict):
                raise ReceiptTransitionError("Stored receipt terminal evidence is invalid")
        else:
            terminal_data = {}

        decision_status = cast(str, decision_row["status"])
        if request.decision == "approve":
            if receipt.decision != "approved" or (
                receipt.status != RecoveryStatus.COMPLETED.value
                or not receipt.provider_execution
                or receipt.execution_count != 1
                or not receipt.provider_dispatch_started
                or receipt.exact_interruption_rejected
                or not receipt.permission_revoked
                or not receipt.scope_closed
                or receipt.approved_remedy_digest != consent_digest
                or receipt.authorization_source != APPROVED_RECEIPT_AUTHORIZATION
                or tuple(receipt.verification_results)
                != APPROVED_RECEIPT_VERIFICATIONS
            ):
                raise ReceiptTransitionError("Stored receipt approval evidence mismatch")
            if len(execution_rows) != 1:
                raise ReceiptTransitionError("Stored receipt execution evidence mismatch")
            execution = execution_rows[0]
            try:
                result = json.loads(cast(str, execution["result_json"]))
            except (TypeError, ValueError):
                raise ReceiptTransitionError(
                    "Stored receipt execution evidence is invalid"
                ) from None
            if (
                not isinstance(result, dict)
                or cast(str, execution["status"]) != "completed"
                or not bool(cast(int, execution["provider_execution"]))
                or cast(str | None, execution["tool_call_id"]) != request.tool_call_id
                or cast(str | None, execution["remedy_digest"]) != consent_digest
                or result.get("provider_result") != receipt.provider_result
            ):
                raise ReceiptTransitionError("Stored receipt execution evidence mismatch")
            if decision_status == "completed":
                response = claim.response
                if not isinstance(response, ApprovalDecisionResponse) or (
                    response.recovery_id != receipt.recovery_id
                    or response.approved_remedy_digest != consent_digest
                    or not response.execution_started
                ):
                    raise ReceiptTransitionError(
                        "Stored receipt decision evidence mismatch"
                    )
            elif decision_status != "claimed" or claim.response is not None:
                raise ReceiptTransitionError("Stored receipt decision evidence mismatch")
            expected_recovery_status = (
                RecoveryStatus.PENDING_APPROVAL.value
                if phase == "preseal"
                else RecoveryStatus.COMPLETED.value
            )
            expected_pending_status = "approved" if phase == "preseal" else "completed"
            expected_scope_status = "active" if phase == "preseal" else "revoked"
            expected_remedy_status = "pending" if phase == "preseal" else "approved"
            if (
                cast(str, recovery["status"]) != expected_recovery_status
                or cast(str, pending["status"]) != expected_pending_status
                or cast(str, scope["status"]) != expected_scope_status
                or cast(str, remedy["status"]) != expected_remedy_status
            ):
                raise ReceiptTransitionError("Stored receipt status evidence mismatch")
            if phase == "sealed":
                expected_terminal = {
                    "decision": "approved",
                    "decisionRemedyDigest": consent_digest,
                    "executionCount": 1,
                    "exactInterruptionRejected": False,
                    "permissionRevoked": True,
                    "providerDispatchStarted": True,
                    "providerExecution": True,
                    "scopeClosed": True,
                    "approvedRemedyDigest": consent_digest,
                }
                if cast(str, terminal_rows[0]["type"]) != "recovery.completed" or any(
                    terminal_data.get(key) != value
                    for key, value in expected_terminal.items()
                ):
                    raise ReceiptTransitionError(
                        "Stored receipt terminal evidence mismatch"
                    )
            return

        if receipt.decision != "declined" or decision_status != "completed":
            raise ReceiptTransitionError("Stored receipt decision evidence mismatch")
        response = claim.response
        if not isinstance(response, DeclineDecisionResponse):
            raise ReceiptTransitionError("Stored receipt decision evidence mismatch")
        execution_count = len(execution_rows)
        provider_execution = any(
            bool(cast(int, execution["provider_execution"]))
            for execution in execution_rows
        )
        provider_dispatch_started = execution_count > 0 or provider_execution
        if provider_dispatch_started:
            expected_provider_result = DECLINED_UNKNOWN_PROVIDER_RESULT
            expected_verifications = DECLINED_UNKNOWN_VERIFICATIONS
        else:
            expected_provider_result = DECLINED_CLOSED_PROVIDER_RESULT
            expected_verifications = DECLINED_CLOSED_VERIFICATIONS
        for execution in execution_rows:
            if (
                cast(str | None, execution["tool_call_id"]) != request.tool_call_id
                or cast(str | None, execution["remedy_digest"]) != consent_digest
            ):
                raise ReceiptTransitionError("Stored receipt execution evidence mismatch")
        if (
            phase != "sealed"
            or receipt.status != cast(str, recovery["status"])
            or receipt.status != response.status
            or response.recovery_id != receipt.recovery_id
            or response.decision_remedy_digest != consent_digest
            or response.execution_started != provider_dispatch_started
            or receipt.execution_count != execution_count
            or receipt.provider_execution != provider_execution
            or receipt.provider_dispatch_started != provider_dispatch_started
            or not receipt.exact_interruption_rejected
            or not receipt.permission_revoked
            or not receipt.scope_closed
            or receipt.approved_remedy_digest is not None
            or receipt.provider_result != expected_provider_result
            or receipt.authorization_source != DECLINED_RECEIPT_AUTHORIZATION
            or tuple(receipt.verification_results) != expected_verifications
            or cast(str, pending["status"]) != "closed"
            or cast(str, scope["status"]) != "revoked"
            or cast(str, remedy["status"]) != "declined"
        ):
            raise ReceiptTransitionError("Stored receipt decline evidence mismatch")
        expected_terminal_type = (
            "recovery.closed_without_action"
            if receipt.status == RecoveryStatus.CLOSED_WITHOUT_ACTION.value
            else "recovery.outcome_unknown"
        )
        expected_terminal = {
            "decision": "declined",
            "decisionRemedyDigest": consent_digest,
            "executionCount": execution_count,
            "exactInterruptionRejected": True,
            "permissionRevoked": True,
            "providerDispatchStarted": provider_dispatch_started,
            "providerExecution": provider_execution,
            "scopeClosed": True,
        }
        if cast(str, terminal_rows[0]["type"]) != expected_terminal_type or any(
            terminal_data.get(key) != value for key, value in expected_terminal.items()
        ):
            raise ReceiptTransitionError("Stored receipt terminal evidence mismatch")

    @classmethod
    def _migrate_task7_receipt_provenance(cls, connection: sqlite3.Connection) -> None:
        """Backfill missing legacy provenance once; otherwise validate without rewrite."""

        rows = connection.execute(
            """
            SELECT receipts.recovery_id AS receipt_recovery_id,
                   receipts.receipt_json,
                   recoveries.execution_mode AS recovery_execution_mode,
                   recoveries.scenario_id AS recovery_scenario_id,
                   receipt_provenance_migrations.recovery_id AS migration_marker
            FROM receipts
            LEFT JOIN recoveries
              ON recoveries.id = receipts.recovery_id
            LEFT JOIN receipt_provenance_migrations
              ON receipt_provenance_migrations.recovery_id = receipts.recovery_id
            """
        ).fetchall()
        for row in rows:
            recovery_id = cast(str, row["receipt_recovery_id"])
            if row["recovery_execution_mode"] is None:
                raise ReceiptTransitionError("Stored receipt recovery evidence is missing")
            try:
                raw = json.loads(cast(str, row["receipt_json"]))
                recovery_mode = ExecutionMode(cast(str, row["recovery_execution_mode"]))
            except (TypeError, ValueError):
                raise ReceiptTransitionError("Stored receipt provenance is invalid") from None
            if not isinstance(raw, dict):
                raise ReceiptTransitionError("Stored receipt provenance is invalid")
            if recovery_mode is ExecutionMode.REPLAY_FIXTURE:
                legacy_status = raw.get("status") == "simulated_completed"
                if legacy_status:
                    raw["status"] = RecoveryStatus.COMPLETED.value
                try:
                    receipt = RecoveryReceipt.model_validate(raw)
                except (TypeError, ValueError):
                    raise ReceiptTransitionError(
                        "Stored receipt provenance is invalid"
                    ) from None
                if receipt.recovery_id != recovery_id:
                    raise ReceiptTransitionError("Stored receipt evidence mismatch")
                legacy_quota_without_evidence = (
                    cast(str, row["recovery_scenario_id"])
                    == ScenarioId.API_QUOTA.value
                    and receipt.quota_evidence is None
                )
                if legacy_quota_without_evidence:
                    marked_legacy = row["migration_marker"] is not None
                    if not legacy_status and not marked_legacy:
                        raise ReceiptTransitionError(
                            "Modern quota replay is missing typed evidence"
                        )
                    terminal_count = cast(
                        int,
                        connection.execute(
                            """
                            SELECT COUNT(*) FROM events
                            WHERE recovery_id = ? AND terminal = 1
                            """,
                            (recovery_id,),
                        ).fetchone()[0],
                    )
                    runtime_count = 0
                    for table in (
                        "executions",
                        "permission_scopes",
                        "pending_approvals",
                        "approval_decisions",
                        "remedies",
                    ):
                        runtime_count += cast(
                            int,
                            connection.execute(
                                f"SELECT COUNT(*) FROM {table} WHERE recovery_id = ?",
                                (recovery_id,),
                            ).fetchone()[0],
                        )
                    if terminal_count != 1 or runtime_count != 0:
                        raise ReceiptTransitionError(
                            "Legacy quota replay evidence is invalid"
                        )
                    if not marked_legacy:
                        connection.execute(
                            """
                            INSERT INTO receipt_provenance_migrations (
                                recovery_id, migrated_at
                            ) VALUES (?, ?)
                            """,
                            (recovery_id, datetime.now(UTC).isoformat()),
                        )
                else:
                    cls._validate_durable_receipt_evidence(
                        connection,
                        receipt,
                        phase="sealed",
                    )
                if legacy_status:
                    connection.execute(
                        "UPDATE receipts SET receipt_json = ? WHERE recovery_id = ?",
                        (
                            receipt.model_dump_json(by_alias=True),
                            recovery_id,
                        ),
                    )
                continue

            if raw.get("quotaEvidence") is not None:
                try:
                    receipt = RecoveryReceipt.model_validate(raw)
                except (TypeError, ValueError):
                    raise ReceiptTransitionError(
                        "Stored quota receipt provenance is invalid"
                    ) from None
                if receipt.recovery_id != recovery_id:
                    raise ReceiptTransitionError("Stored receipt evidence mismatch")
                cls._validate_durable_receipt_evidence(
                    connection,
                    receipt,
                    phase="sealed",
                )
                if row["migration_marker"] is None:
                    connection.execute(
                        """
                        INSERT INTO receipt_provenance_migrations (
                            recovery_id, migrated_at
                        ) VALUES (?, ?)
                        """,
                        (recovery_id, datetime.now(UTC).isoformat()),
                    )
                continue

            pending_rows = connection.execute(
                "SELECT * FROM pending_approvals WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchall()
            if len(pending_rows) != 1:
                raise ReceiptTransitionError(
                    "Stored non-replay receipt requires one unambiguous pending envelope"
                )
            expected, expected_model_ids = cls._receipt_provenance_from_pending(
                pending_rows[0]
            )
            provenance_keys = set(expected)
            present_keys = provenance_keys.intersection(raw)
            marked = row["migration_marker"] is not None
            if not present_keys:
                if marked:
                    raise ReceiptTransitionError("Stored receipt provenance is missing")
                raw.update(expected)
                raw["modelIds"] = expected_model_ids
                should_backfill = True
            elif present_keys != provenance_keys:
                raise ReceiptTransitionError("Stored receipt provenance is partial")
            else:
                should_backfill = False

            try:
                receipt = RecoveryReceipt.model_validate(raw)
            except (TypeError, ValueError):
                raise ReceiptTransitionError("Stored receipt provenance is invalid") from None
            if receipt.recovery_id != recovery_id:
                raise ReceiptTransitionError("Stored receipt evidence mismatch")
            cls._validate_durable_receipt_evidence(
                connection,
                receipt,
                phase="sealed",
            )
            if should_backfill:
                connection.execute(
                    "UPDATE receipts SET receipt_json = ? WHERE recovery_id = ?",
                    (
                        receipt.model_dump_json(by_alias=True, exclude_none=True),
                        recovery_id,
                    ),
                )
            if not marked:
                connection.execute(
                    """
                    INSERT INTO receipt_provenance_migrations (recovery_id, migrated_at)
                    VALUES (?, ?)
                    """,
                    (recovery_id, datetime.now(UTC).isoformat()),
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
        envelope: PendingApprovalEnvelope | None = None,
    ) -> RecoverySnapshot:
        if envelope is not None and envelope.sdk_version == "legacy-incompatible":
            envelope = None
        model_ids = (
            list(dict.fromkeys(item.returned_model for item in envelope.model_metadata))
            if envelope is not None
            else []
        )
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
            rootTraceId=envelope.root_trace_id if envelope is not None else None,
            modelIds=model_ids,
            sdkVersion=envelope.sdk_version if envelope is not None else None,
            protocolVersion=(envelope.protocol_version if envelope is not None else None),
            agentGraphVersion=(
                envelope.agent_graph_version if envelope is not None else None
            ),
            promptToolSchemaHash=(
                envelope.definition_digest if envelope is not None else None
            ),
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
        raw_model_metadata = json.loads(cast(str, row["model_metadata_json"]))
        if not isinstance(raw_model_metadata, list):
            raise ValueError("Stored model metadata must be a JSON array")
        model_metadata = tuple(
            ModelResponseMetadata.from_json_value(item) for item in raw_model_metadata
        )
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
            model_metadata=model_metadata,
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
            decision=cast(Literal["approve", "decline"], row["decision"]),
            clientDecisionId=cast(str, row["client_decision_id"]),
            remedyId=cast(str, row["remedy_id"]),
            remedyDigest=cast(str, row["remedy_digest"]),
            toolCallId=cast(str, row["tool_call_id"]),
        )

    @classmethod
    def _decision_claim_from_row(cls, row: sqlite3.Row) -> ApprovalDecisionClaim:
        response: DecisionResponse | None = None
        if row["result_json"] is not None:
            raw_response = json.loads(cast(str, row["result_json"]))
            if not isinstance(raw_response, dict):
                raise ValueError("Decision response must be a JSON object")
            if raw_response.get("decision") == "approve":
                response = ApprovalDecisionResponse.model_validate(raw_response)
            elif raw_response.get("decision") == "decline":
                response = DeclineDecisionResponse.model_validate(raw_response)
            else:
                raise ValueError("Decision response has an unknown action")
        return ApprovalDecisionClaim(
            recovery_id=cast(str, row["recovery_id"]),
            request=cls._decision_request_from_row(row),
            request_fingerprint=cast(str, row["request_fingerprint"]),
            response=response,
        )

    def _validate_decision_identity(
        self,
        connection: sqlite3.Connection,
        recovery_id: str,
        request: ApprovalDecisionRequest,
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
            or cast(str, recovery["execution_mode"])
            not in {
                ExecutionMode.SDK_STUB.value,
                ExecutionMode.OPENAI_LIVE.value,
            }
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
        if pending.execution_mode.value != cast(str, recovery["execution_mode"]):
            raise ApprovalDecisionError(
                "resume_incompatible", recovery_id, status_code=409
            )
        allowed_pending_statuses = (
            {"pending", "approved"}
            if request.decision == "approve"
            else {"pending", "declined"}
        )
        if pending.status not in allowed_pending_statuses:
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
        if exact_hotel_terms(consent.evidence) != consent.terms:
            raise ApprovalDecisionError(
                "remedy_digest_mismatch", recovery_id, status_code=422
            )
        return pending, consent

    @staticmethod
    def _validate_approval_policy(
        recovery_id: str,
        consent: RemedyConsentRecord,
        *,
        now: datetime,
    ) -> None:
        """Apply freshness, current constraints, and authority to approval only."""

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

    @staticmethod
    def _permission_scope_from_row(row: sqlite3.Row) -> PermissionScopeRecord:
        return PermissionScopeRecord(
            recovery_id=cast(str, row["recovery_id"]),
            tool_call_id=cast(str, row["tool_call_id"]),
            remedy_digest=cast(str, row["remedy_digest"]),
            action_digest=cast(str, row["action_digest"]),
            status=cast(str, row["status"]),
            created_at=datetime.fromisoformat(cast(str, row["created_at"])),
            activated_at=(
                datetime.fromisoformat(cast(str, row["activated_at"]))
                if row["activated_at"] is not None
                else None
            ),
            revoked_at=(
                datetime.fromisoformat(cast(str, row["revoked_at"]))
                if row["revoked_at"] is not None
                else None
            ),
            updated_at=datetime.fromisoformat(cast(str, row["updated_at"])),
        )

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

    @classmethod
    def _bind_recovery_access(
        cls,
        connection: sqlite3.Connection,
        *,
        recovery_id: str,
        session_hash: str | None,
    ) -> None:
        if session_hash is None:
            return
        cls._require_public_identity_hash(session_hash)
        connection.execute(
            """
            INSERT INTO recovery_access (recovery_id, session_hash)
            VALUES (?, ?)
            """,
            (recovery_id, session_hash),
        )

    def create_recovery(
        self,
        *,
        recovery_id: str,
        scenario_id: ScenarioId,
        execution_mode: ExecutionMode,
        current_step: int,
        current_step_summary: str,
        session_hash: str | None = None,
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
            self._bind_recovery_access(
                connection,
                recovery_id=recovery_id,
                session_hash=session_hash,
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

    def create_replay_recovery(
        self,
        *,
        recovery_id: str,
        scenario: ReplayScenarioDefinition,
        session_hash: str | None = None,
    ) -> RecoverySnapshot:
        """Persist one complete replay graph atomically or leave no trace."""

        if (
            scenario.initial_step != 0
            or len(scenario.events) != 6
            or [event.current_step for event in scenario.events] != list(range(6))
            or any(
                event.status is not RecoveryStatus.IN_PROGRESS
                for event in scenario.events[:-1]
            )
            or scenario.events[-1].status is not RecoveryStatus.COMPLETED
            or scenario.receipt is None
        ):
            raise ReceiptTransitionError(
                "Replay fixtures require six ordered steps and terminal receipt evidence"
            )
        receipt = RecoveryReceipt(
            recoveryId=recovery_id,
            executionMode=ExecutionMode.REPLAY_FIXTURE,
            **scenario.receipt.model_dump(),
        )
        now = self._now()
        created_data = json.dumps(
            {
                "scenarioId": scenario.id.value,
                "executionMode": ExecutionMode.REPLAY_FIXTURE.value,
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
                ) VALUES (?, ?, 'replay_fixture', 'in_progress', ?, ?, ?, ?)
                """,
                (
                    recovery_id,
                    scenario.id.value,
                    scenario.initial_step,
                    scenario.initial_summary,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            self._bind_recovery_access(
                connection,
                recovery_id=recovery_id,
                session_hash=session_hash,
            )
            connection.execute(
                """
                INSERT INTO events (
                    recovery_id, seq, type, terminal, data_json, created_at
                ) VALUES (?, 1, 'recovery.created', 0, ?, ?)
                """,
                (recovery_id, created_data, now.isoformat()),
            )
            for sequence, event in enumerate(scenario.events, start=2):
                event_json = json.dumps(
                    event.data,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                connection.execute(
                    """
                    UPDATE recoveries
                    SET status = ?, current_step = ?, current_step_summary = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        event.status.value,
                        event.current_step,
                        event.summary,
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
                        sequence,
                        event.type,
                        int(event.status is RecoveryStatus.COMPLETED),
                        event_json,
                        now.isoformat(),
                    ),
                )
            connection.execute(
                """
                INSERT INTO receipts (recovery_id, receipt_json, created_at)
                VALUES (?, ?, ?)
                """,
                (
                    recovery_id,
                    receipt.model_dump_json(by_alias=True, exclude_none=True),
                    now.isoformat(),
                ),
            )
            self._validate_durable_receipt_evidence(
                connection,
                receipt,
                phase="sealed",
            )
        return self.get_recovery(recovery_id)

    def create_completed_quota_recovery(
        self,
        *,
        recovery_id: str,
        execution: DurableExecution,
        receipt: RecoveryReceipt,
        session_hash: str | None = None,
    ) -> RecoverySnapshot:
        """Persist the complete runtime quota trace and receipt atomically."""

        if execution.recovery_id != recovery_id or receipt.recovery_id != recovery_id:
            raise ReceiptTransitionError("Quota recovery ID evidence mismatch")
        if (
            receipt.execution_mode is not ExecutionMode.SDK_STUB
            or receipt.quota_evidence is None
            or receipt.quota_evidence.source != "sdk_simulator"
            or execution.status != "completed"
            or not execution.provider_execution
            or execution.request_digest is None
            or re.fullmatch(r"[0-9a-f]{64}", execution.request_digest) is None
            or execution.tool_call_id is None
            or not execution.tool_call_id.strip()
            or execution.remedy_digest is not None
            or execution.result_json is None
        ):
            raise ReceiptTransitionError("Quota terminal execution evidence is invalid")
        try:
            provider_result = QuotaGrantResult.model_validate(execution.result_json)
        except (TypeError, ValueError):
            raise ReceiptTransitionError(
                "Quota terminal provider result is invalid"
            ) from None
        if provider_result.provider_result != receipt.provider_result:
            raise ReceiptTransitionError("Quota terminal provider result mismatch")

        evidence = receipt.quota_evidence
        hard_constraints = evidence.hard_constraints.model_dump(
            mode="json",
            by_alias=True,
        )
        events: list[tuple[str, dict[str, JsonValue]]] = [
            (
                "recovery.detected",
                {
                    "phase": "Detect",
                    "recordedDemandRpm": evidence.recorded_demand_rpm,
                    "summary": "Recorded demand reached 1200 rpm.",
                },
            ),
            (
                "evidence.proved",
                {
                    "phase": "Prove",
                    "providerCeilingRpm": evidence.provider_ceiling_rpm,
                    "providerProofVerified": evidence.provider_proof_verified,
                    "region": evidence.region,
                    "summary": "Provider proof verified the 1000 rpm US ceiling.",
                },
            ),
            (
                "quota.burst_selected",
                {
                    "phase": "Negotiate",
                    "temporaryBurstRpm": evidence.temporary_burst_rpm,
                    "durationSeconds": evidence.duration_seconds,
                    "extraCostMinor": evidence.extra_cost_minor,
                    "currency": evidence.currency,
                    "summary": (
                        "A temporary 1500 rpm US burst was selected for 900 seconds."
                    ),
                },
            ),
            (
                "delegated_authority.authorized",
                {
                    "phase": "Authorize",
                    "delegatedAuthorityMaxMinor": (
                        evidence.delegated_authority_max_minor
                    ),
                    "humanInterruptions": evidence.human_interruptions,
                    "approvals": evidence.approvals,
                    "hardConstraints": hard_constraints,
                    "summary": (
                        "All constraints passed inside delegated authority; "
                        "no human approval was needed."
                    ),
                },
            ),
            (
                "execution.completed",
                {
                    "phase": "Execute",
                    "providerExecution": True,
                    "providerDispatchStarted": True,
                    "dispatchId": provider_result.dispatch_id,
                    "grantId": provider_result.grant_id,
                    "summary": (
                        "The demo quota adapter dispatched one temporary grant."
                    ),
                },
            ),
            (
                "recovery.completed",
                {
                    "phase": "Verify & seal",
                    "providerExecution": True,
                    "grantVerified": evidence.grant_verified,
                    "permissionRevoked": True,
                    "scopeClosed": True,
                    "summary": QUOTA_TERMINAL_SUMMARY,
                },
            ),
        ]
        now = self._now()
        created_data = {
            "scenarioId": ScenarioId.API_QUOTA.value,
            "executionMode": ExecutionMode.SDK_STUB.value,
            "summary": "Recovery created for the selected execution mode.",
        }
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO recoveries (
                    id, scenario_id, execution_mode, status, current_step,
                    current_step_summary, created_at, updated_at
                ) VALUES (?, 'api-quota', 'sdk_stub', 'completed', 5, ?, ?, ?)
                """,
                (
                    recovery_id,
                    QUOTA_TERMINAL_SUMMARY,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            self._bind_recovery_access(
                connection,
                recovery_id=recovery_id,
                session_hash=session_hash,
            )
            connection.execute(
                """
                INSERT INTO events (
                    recovery_id, seq, type, terminal, data_json, created_at
                ) VALUES (?, 1, 'recovery.created', 0, ?, ?)
                """,
                (
                    recovery_id,
                    json.dumps(created_data, separators=(",", ":"), sort_keys=True),
                    now.isoformat(),
                ),
            )
            for sequence, (event_type, event_data) in enumerate(events, start=2):
                connection.execute(
                    """
                    INSERT INTO events (
                        recovery_id, seq, type, terminal, data_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        recovery_id,
                        sequence,
                        event_type,
                        int(sequence == 7),
                        json.dumps(event_data, separators=(",", ":"), sort_keys=True),
                        now.isoformat(),
                    ),
                )
            connection.execute(
                """
                INSERT INTO executions (
                    id, recovery_id, idempotency_key, status,
                    provider_execution, request_digest, tool_call_id,
                    remedy_digest, result_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'completed', 1, ?, ?, NULL, ?, ?, ?)
                """,
                (
                    execution.execution_id,
                    recovery_id,
                    execution.idempotency_key,
                    execution.request_digest,
                    execution.tool_call_id,
                    json.dumps(
                        execution.result_json,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            connection.execute(
                """
                INSERT INTO permission_scopes (
                    recovery_id, tool_call_id, remedy_digest, action_digest,
                    status, created_at, activated_at, revoked_at, updated_at
                ) VALUES (?, ?, ?, ?, 'revoked', ?, ?, ?, ?)
                """,
                (
                    recovery_id,
                    execution.tool_call_id,
                    execution.request_digest,
                    execution.request_digest,
                    now.isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            connection.execute(
                """
                INSERT INTO receipts (recovery_id, receipt_json, created_at)
                VALUES (?, ?, ?)
                """,
                (
                    recovery_id,
                    receipt.model_dump_json(by_alias=True, exclude_none=True),
                    now.isoformat(),
                ),
            )
            connection.execute(
                """
                INSERT INTO receipt_provenance_migrations (recovery_id, migrated_at)
                VALUES (?, ?)
                """,
                (recovery_id, now.isoformat()),
            )
            self._validate_durable_receipt_evidence(
                connection,
                receipt,
                phase="sealed",
            )
        return self.get_recovery(recovery_id)

    def create_pending_recovery(
        self,
        *,
        recovery_id: str,
        scenario_id: ScenarioId,
        execution_mode: ExecutionMode,
        current_step_summary: str,
        event_data: dict[str, JsonValue],
        pending_approval: PendingApprovalEnvelope,
        remedy_consent: RemedyConsentRecord,
        session_hash: str | None = None,
    ) -> RecoverySnapshot:
        """Insert the full first durable approval boundary in one transaction."""

        if pending_approval.recovery_id != recovery_id:
            raise ValueError("Pending approval recovery ID does not match creation")
        if pending_approval.execution_mode is not execution_mode:
            raise ValueError("Pending approval execution mode does not match creation")
        if remedy_consent.recovery_id != recovery_id:
            raise ValueError("Remedy consent recovery ID does not match creation")
        if pending_approval.remedy_id != remedy_consent.remedy_id:
            raise ValueError("Pending approval remedy linkage does not match consent")
        if pending_approval.consent_digest != remedy_consent.consent_digest:
            raise ValueError("Pending approval digest linkage does not match consent")
        remedy_consent.public_view(tool_call_id=pending_approval.tool_call_id)
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
        approval_data = json.dumps(event_data, separators=(",", ":"), sort_keys=True)
        pending_state_json = json.dumps(
            pending_approval.state_json,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        metadata_json = json.dumps(
            [item.to_json_value() for item in pending_approval.model_metadata],
            ensure_ascii=False,
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
                ) VALUES (?, ?, ?, 'pending_approval', 3, ?, ?, ?)
                """,
                (
                    recovery_id,
                    scenario_id.value,
                    execution_mode.value,
                    current_step_summary,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            self._bind_recovery_access(
                connection,
                recovery_id=recovery_id,
                session_hash=session_hash,
            )
            connection.execute(
                """
                INSERT INTO events (
                    recovery_id, seq, type, terminal, data_json, created_at
                ) VALUES (?, 1, 'recovery.created', 0, ?, ?)
                """,
                (recovery_id, created_data, now.isoformat()),
            )
            connection.execute(
                """
                INSERT INTO events (
                    recovery_id, seq, type, terminal, data_json, created_at
                ) VALUES (?, 2, 'approval.requested', 0, ?, ?)
                """,
                (recovery_id, approval_data, now.isoformat()),
            )
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
                    recovery_id,
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
            connection.execute(
                """
                INSERT INTO pending_approvals (
                    tool_call_id, recovery_id, sdk_version, protocol_version,
                    agent_graph_version, definition_digest, root_trace_id,
                    execution_mode, action_digest, remedy_id, consent_digest,
                    model_metadata_json, model_metadata_revision, state_json,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pending_approval.tool_call_id,
                    recovery_id,
                    pending_approval.sdk_version,
                    pending_approval.protocol_version,
                    pending_approval.agent_graph_version,
                    pending_approval.definition_digest,
                    pending_approval.root_trace_id,
                    execution_mode.value,
                    pending_approval.action_digest,
                    pending_approval.remedy_id,
                    pending_approval.consent_digest,
                    metadata_json,
                    len(pending_approval.model_metadata),
                    pending_state_json,
                    pending_approval.status,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            connection.execute(
                """
                INSERT INTO permission_scopes (
                    recovery_id, tool_call_id, remedy_digest, action_digest,
                    status, created_at, activated_at, revoked_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', ?, NULL, NULL, ?)
                """,
                (
                    recovery_id,
                    pending_approval.tool_call_id,
                    pending_approval.consent_digest,
                    pending_approval.action_digest,
                    now.isoformat(),
                    now.isoformat(),
                ),
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
                "SELECT id, scenario_id, execution_mode FROM recoveries WHERE id = ?",
                (recovery_id,),
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
                is_quota_scenario = (
                    cast(str, existing["scenario_id"]) == ScenarioId.API_QUOTA.value
                )
                if is_quota_scenario != (receipt.quota_evidence is not None):
                    raise ReceiptTransitionError(
                        "Receipt scenario and quota evidence do not match"
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
                    int(
                        status
                        in {
                            RecoveryStatus.COMPLETED,
                            RecoveryStatus.CLOSED_WITHOUT_ACTION,
                            RecoveryStatus.OUTCOME_UNKNOWN,
                        }
                    ),
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
                        model_metadata_json, model_metadata_revision, state_json,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        json.dumps(
                            [
                                item.to_json_value()
                                for item in pending_approval.model_metadata
                            ],
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        len(pending_approval.model_metadata),
                        pending_state_json,
                        pending_approval.status,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO permission_scopes (
                        recovery_id, tool_call_id, remedy_digest, action_digest,
                        status, created_at, activated_at, revoked_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'pending', ?, NULL, NULL, ?)
                    """,
                    (
                        pending_approval.recovery_id,
                        pending_approval.tool_call_id,
                        pending_approval.consent_digest,
                        pending_approval.action_digest,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
            if status in {
                RecoveryStatus.COMPLETED,
                RecoveryStatus.CLOSED_WITHOUT_ACTION,
                RecoveryStatus.OUTCOME_UNKNOWN,
            }:
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
            pending_view = (
                self._public_pending_view(connection, recovery_id)
                if row is not None
                and cast(str, row["status"])
                == RecoveryStatus.PENDING_APPROVAL.value
                else None
            )
            envelope_row = connection.execute(
                """
                SELECT * FROM pending_approvals
                WHERE recovery_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (recovery_id,),
            ).fetchone()
            envelope = (
                self._pending_approval_from_row(envelope_row)
                if envelope_row is not None
                else None
            )
        if row is None:
            raise RecoveryNotFoundError("Recovery not found")
        return self._recovery_from_row(
            row,
            pending_approval=pending_view,
            envelope=envelope,
        )

    def recovery_is_accessible(self, recovery_id: str, session_hash: str) -> bool:
        """Return whether one exact signed-session identity owns the recovery."""

        self._require_public_identity_hash(session_hash)
        with self._lock, self._connect() as connection:
            return (
                connection.execute(
                    """
                    SELECT 1 FROM recovery_access
                    WHERE recovery_id = ? AND session_hash = ?
                    """,
                    (recovery_id, session_hash),
                ).fetchone()
                is not None
            )

    def get_recovery_for_session(
        self,
        recovery_id: str,
        session_hash: str,
    ) -> RecoverySnapshot:
        self._require_public_identity_hash(session_hash)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT recoveries.* FROM recoveries
                JOIN recovery_access
                  ON recovery_access.recovery_id = recoveries.id
                WHERE recoveries.id = ? AND recovery_access.session_hash = ?
                """,
                (recovery_id, session_hash),
            ).fetchone()
            if row is None:
                raise RecoveryNotFoundError("Recovery not found")
            pending_view = (
                self._public_pending_view(connection, recovery_id)
                if cast(str, row["status"])
                == RecoveryStatus.PENDING_APPROVAL.value
                else None
            )
            envelope_row = connection.execute(
                """
                SELECT * FROM pending_approvals
                WHERE recovery_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (recovery_id,),
            ).fetchone()
            envelope = (
                self._pending_approval_from_row(envelope_row)
                if envelope_row is not None
                else None
            )
        return self._recovery_from_row(
            row,
            pending_approval=pending_view,
            envelope=envelope,
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

    def claim_decision(
        self,
        recovery_id: str,
        request: ApprovalDecisionRequest,
    ) -> ApprovalDecisionClaim:
        """Claim one approve-or-decline winner and update its exact scope."""

        return self._claim_decision(
            recovery_id,
            request,
            session_hash=None,
        )

    def claim_decision_for_session(
        self,
        recovery_id: str,
        request: ApprovalDecisionRequest,
        *,
        session_hash: str,
    ) -> ApprovalDecisionClaim:
        """Claim a public decision only while ownership holds in this transaction."""

        self._require_public_identity_hash(session_hash)
        return self._claim_decision(
            recovery_id,
            request,
            session_hash=session_hash,
        )

    def _claim_decision(
        self,
        recovery_id: str,
        request: ApprovalDecisionRequest,
        *,
        session_hash: str | None,
    ) -> ApprovalDecisionClaim:

        fingerprint = self._decision_fingerprint(recovery_id, request)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if session_hash is not None and connection.execute(
                """
                SELECT 1 FROM recovery_access
                WHERE recovery_id = ? AND session_hash = ?
                """,
                (recovery_id, session_hash),
            ).fetchone() is None:
                raise RecoveryNotFoundError("Recovery not found")
            now = self._now()
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

            pending, consent = self._validate_decision_identity(
                connection,
                recovery_id,
                request,
            )
            if request.decision == "approve":
                self._validate_approval_policy(recovery_id, consent, now=now)
            scope = connection.execute(
                "SELECT * FROM permission_scopes WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone()
            if scope is None:
                raise ApprovalDecisionError(
                    "decision_unavailable", recovery_id, status_code=409
                )
            if (
                cast(str, scope["tool_call_id"]) != pending.tool_call_id
                or cast(str, scope["remedy_digest"]) != pending.consent_digest
                or cast(str, scope["action_digest"]) != pending.action_digest
                or cast(str, scope["status"]) != "pending"
            ):
                raise ApprovalDecisionError(
                    "resume_incompatible", recovery_id, status_code=409
                )
            connection.execute(
                """
                INSERT INTO approval_decisions (
                    recovery_id, client_decision_id, decision, remedy_id,
                    remedy_digest, tool_call_id, request_fingerprint, status,
                    result_json, claimed_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'claimed', NULL, ?, NULL)
                """,
                (
                    recovery_id,
                    request.client_decision_id,
                    request.decision,
                    request.remedy_id,
                    request.remedy_digest,
                    request.tool_call_id,
                    fingerprint,
                    now.isoformat(),
                ),
            )
            if request.decision == "approve":
                connection.execute(
                    """
                    UPDATE permission_scopes
                    SET status = 'active', activated_at = ?, updated_at = ?
                    WHERE recovery_id = ? AND status = 'pending'
                    """,
                    (now.isoformat(), now.isoformat(), recovery_id),
                )
                pending_status = "approved"
                summary = "Exact approval claimed; execution outcome pending."
            else:
                pending_status = "declined"
                summary = "Exact decline claimed; closure outcome pending."
            connection.execute(
                """
                UPDATE pending_approvals
                SET status = ?, updated_at = ?
                WHERE recovery_id = ?
                """,
                (pending_status, now.isoformat(), recovery_id),
            )
            connection.execute(
                """
                UPDATE recoveries
                SET current_step_summary = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    summary,
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

    def claim_approval_decision(
        self,
        recovery_id: str,
        request: ApprovalDecisionRequest,
    ) -> ApprovalDecisionClaim:
        """Compatibility wrapper for callers migrated from the Task 5 name."""

        return self.claim_decision(recovery_id, request)

    @staticmethod
    def _decision_lease_expiry(row: sqlite3.Row) -> datetime | None:
        raw_expiry = cast(str | None, row["resume_lease_expires_at"])
        return datetime.fromisoformat(raw_expiry) if raw_expiry is not None else None

    @staticmethod
    def _assert_claim_matches_row(
        claim: ApprovalDecisionClaim,
        durable_claim: ApprovalDecisionClaim,
    ) -> None:
        if (
            durable_claim.request.client_decision_id
            != claim.request.client_decision_id
            or durable_claim.request_fingerprint != claim.request_fingerprint
        ):
            raise ApprovalDecisionError(
                "decision_id_conflict", claim.recovery_id, status_code=409
            )

    @staticmethod
    def _require_resume_owner(
        row: sqlite3.Row,
        *,
        recovery_id: str,
        resume_owner_id: str,
        resume_generation: int,
        now: datetime,
    ) -> None:
        expiry = SQLiteStore._decision_lease_expiry(row)
        if (
            cast(str, row["status"]) != "claimed"
            or cast(str | None, row["resume_owner_id"]) != resume_owner_id
            or cast(int, row["resume_generation"]) != resume_generation
            or expiry is None
            or expiry <= now
        ):
            raise ApprovalDecisionError(
                "resume_owner_lost", recovery_id, status_code=409
            )

    def acquire_decision_resume(
        self,
        claim: ApprovalDecisionClaim,
        *,
        resume_owner_id: str,
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> DecisionResumeLease:
        """Atomically acquire, wait for, or replay one decision resume."""

        if not resume_owner_id.strip():
            raise ValueError("Resume owner ID must not be empty")
        if lease_duration <= timedelta(0):
            raise ValueError("Resume lease duration must be positive")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            observed_at = now or self._now()
            expires_at = observed_at + lease_duration
            row = connection.execute(
                "SELECT * FROM approval_decisions WHERE recovery_id = ?",
                (claim.recovery_id,),
            ).fetchone()
            if row is None:
                raise ApprovalDecisionError(
                    "decision_unavailable", claim.recovery_id, status_code=409
                )
            durable_claim = self._decision_claim_from_row(row)
            self._assert_claim_matches_row(claim, durable_claim)
            generation = cast(int, row["resume_generation"])
            current_owner = cast(str | None, row["resume_owner_id"])
            current_expiry = self._decision_lease_expiry(row)
            if durable_claim.response is not None:
                return DecisionResumeLease(
                    disposition="replay",
                    claim=durable_claim,
                    resume_owner_id=None,
                    resume_generation=generation,
                    lease_expires_at=None,
                )
            if current_owner is not None and current_expiry is not None:
                if current_expiry > observed_at and current_owner != resume_owner_id:
                    return DecisionResumeLease(
                        disposition="wait",
                        claim=durable_claim,
                        resume_owner_id=current_owner,
                        resume_generation=generation,
                        lease_expires_at=current_expiry,
                    )
                if current_expiry > observed_at and current_owner == resume_owner_id:
                    connection.execute(
                        """
                        UPDATE approval_decisions
                        SET resume_lease_expires_at = ?, resume_heartbeat_at = ?
                        WHERE recovery_id = ? AND status = 'claimed'
                          AND resume_owner_id = ? AND resume_generation = ?
                        """,
                        (
                            expires_at.isoformat(),
                            observed_at.isoformat(),
                            claim.recovery_id,
                            resume_owner_id,
                            generation,
                        ),
                    )
                    return DecisionResumeLease(
                        disposition="owner",
                        claim=durable_claim,
                        resume_owner_id=resume_owner_id,
                        resume_generation=generation,
                        lease_expires_at=expires_at,
                    )
            next_generation = generation + 1
            cursor = connection.execute(
                """
                UPDATE approval_decisions
                SET resume_owner_id = ?, resume_generation = ?,
                    resume_lease_expires_at = ?, resume_heartbeat_at = ?
                WHERE recovery_id = ? AND status = 'claimed'
                  AND resume_generation = ?
                """,
                (
                    resume_owner_id,
                    next_generation,
                    expires_at.isoformat(),
                    observed_at.isoformat(),
                    claim.recovery_id,
                    generation,
                ),
            )
            if cursor.rowcount != 1:
                raise ApprovalDecisionError(
                    "resume_owner_lost", claim.recovery_id, status_code=409
                )
            return DecisionResumeLease(
                disposition="owner",
                claim=durable_claim,
                resume_owner_id=resume_owner_id,
                resume_generation=next_generation,
                lease_expires_at=expires_at,
            )

    def renew_decision_resume(
        self,
        claim: ApprovalDecisionClaim,
        *,
        resume_owner_id: str,
        resume_generation: int,
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> bool:
        """Heartbeat only the exact unexpired claimed owner generation."""

        if lease_duration <= timedelta(0):
            raise ValueError("Resume lease duration must be positive")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            observed_at = now or self._now()
            expires_at = observed_at + lease_duration
            row = connection.execute(
                "SELECT * FROM approval_decisions WHERE recovery_id = ?",
                (claim.recovery_id,),
            ).fetchone()
            if row is None:
                return False
            durable_claim = self._decision_claim_from_row(row)
            self._assert_claim_matches_row(claim, durable_claim)
            try:
                self._require_resume_owner(
                    row,
                    recovery_id=claim.recovery_id,
                    resume_owner_id=resume_owner_id,
                    resume_generation=resume_generation,
                    now=observed_at,
                )
            except ApprovalDecisionError:
                return False
            cursor = connection.execute(
                """
                UPDATE approval_decisions
                SET resume_lease_expires_at = ?, resume_heartbeat_at = ?
                WHERE recovery_id = ? AND status = 'claimed'
                  AND resume_owner_id = ? AND resume_generation = ?
                """,
                (
                    expires_at.isoformat(),
                    observed_at.isoformat(),
                    claim.recovery_id,
                    resume_owner_id,
                    resume_generation,
                ),
            )
            return cursor.rowcount == 1

    def release_decision_resume(
        self,
        claim: ApprovalDecisionClaim,
        *,
        resume_owner_id: str,
        resume_generation: int,
    ) -> bool:
        """Release only the exact still-claimed owner after a controlled stop."""

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM approval_decisions WHERE recovery_id = ?",
                (claim.recovery_id,),
            ).fetchone()
            if row is None:
                return False
            durable_claim = self._decision_claim_from_row(row)
            self._assert_claim_matches_row(claim, durable_claim)
            cursor = connection.execute(
                """
                UPDATE approval_decisions
                SET resume_owner_id = NULL, resume_lease_expires_at = NULL,
                    resume_heartbeat_at = NULL
                WHERE recovery_id = ? AND status = 'claimed'
                  AND resume_owner_id = ? AND resume_generation = ?
                """,
                (claim.recovery_id, resume_owner_id, resume_generation),
            )
            return cursor.rowcount == 1

    def validate_claimed_decision(
        self,
        claim: ApprovalDecisionClaim,
        *,
        resume_owner_id: str,
        resume_generation: int,
    ) -> ApprovalDecisionClaim:
        """Repeat every binding check immediately before SDK approval."""

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = self._now()
            row = connection.execute(
                "SELECT * FROM approval_decisions WHERE recovery_id = ?",
                (claim.recovery_id,),
            ).fetchone()
            if row is None:
                raise ApprovalDecisionError(
                    "decision_unavailable", claim.recovery_id, status_code=409
                )
            durable_claim = self._decision_claim_from_row(row)
            self._assert_claim_matches_row(claim, durable_claim)
            if durable_claim.response is not None:
                return durable_claim
            self._require_resume_owner(
                row,
                recovery_id=claim.recovery_id,
                resume_owner_id=resume_owner_id,
                resume_generation=resume_generation,
                now=now,
            )
            pending, consent = self._validate_decision_identity(
                connection,
                claim.recovery_id,
                claim.request,
            )
            if claim.request.decision == "approve":
                self._validate_approval_policy(
                    claim.recovery_id,
                    consent,
                    now=now,
                )
                required_scope_status = "active"
            else:
                required_scope_status = "pending"
            scope = connection.execute(
                "SELECT * FROM permission_scopes WHERE recovery_id = ?",
                (claim.recovery_id,),
            ).fetchone()
            if scope is None or (
                cast(str, scope["tool_call_id"]) != pending.tool_call_id
                or cast(str, scope["remedy_digest"]) != pending.consent_digest
                or cast(str, scope["action_digest"]) != pending.action_digest
                or cast(str, scope["status"]) != required_scope_status
            ):
                raise ApprovalDecisionError(
                    "resume_incompatible", claim.recovery_id, status_code=409
                )
            return durable_claim

    def assert_provider_dispatch_authorized(
        self,
        *,
        recovery_id: str,
        tool_call_id: str,
        remedy_digest: str,
        action_digest: str,
        resume_owner_id: str,
        resume_generation: int,
        now: datetime | None = None,
    ) -> None:
        """Block dispatch unless the exact claimed action retains its active scope."""

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            observed_at = now or self._now()
            row = connection.execute(
                "SELECT * FROM approval_decisions WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone()
            if row is None:
                raise ApprovalDecisionError(
                    "decision_unavailable", recovery_id, status_code=409
                )
            self._require_resume_owner(
                row,
                recovery_id=recovery_id,
                resume_owner_id=resume_owner_id,
                resume_generation=resume_generation,
                now=observed_at,
            )
            claim = self._decision_claim_from_row(row)
            if (
                claim.request.decision != "approve"
                or claim.request.tool_call_id != tool_call_id
                or claim.request.remedy_digest != remedy_digest
            ):
                raise ApprovalDecisionError(
                    "decision_id_conflict", recovery_id, status_code=409
                )
            scope = connection.execute(
                """
                SELECT * FROM permission_scopes
                WHERE recovery_id = ?
                """,
                (recovery_id,),
            ).fetchone()
            if scope is None or (
                cast(str, scope["status"]) != "active"
                or cast(str, scope["tool_call_id"]) != tool_call_id
                or cast(str, scope["remedy_digest"]) != remedy_digest
                or cast(str, scope["action_digest"]) != action_digest
            ):
                raise ApprovalDecisionError(
                    "decision_unavailable", recovery_id, status_code=409
                )
            pending, consent = self._validate_decision_identity(
                connection,
                recovery_id,
                claim.request,
            )
            if pending.action_digest != action_digest:
                raise ApprovalDecisionError(
                    "decision_unavailable", recovery_id, status_code=409
                )
            self._validate_approval_policy(recovery_id, consent, now=observed_at)

    def complete_decision(
        self,
        claim: ApprovalDecisionClaim,
        response: DecisionResponse,
        *,
        resume_owner_id: str,
        resume_generation: int,
    ) -> DecisionResponse:
        """Store and replay the one safe public response for the winning claim."""

        if (
            response.client_decision_id != claim.request.client_decision_id
            or response.recovery_id != claim.recovery_id
            or response.decision != claim.request.decision
        ):
            raise ApprovalDecisionError(
                "decision_id_conflict", claim.recovery_id, status_code=409
            )
        if isinstance(response, ApprovalDecisionResponse):
            response_digest = response.approved_remedy_digest
        else:
            response_digest = response.decision_remedy_digest
        if response_digest != claim.request.remedy_digest:
            raise ApprovalDecisionError(
                "decision_id_conflict", claim.recovery_id, status_code=409
            )
        serialized = response.model_dump_json(by_alias=True)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = self._now()
            row = connection.execute(
                "SELECT * FROM approval_decisions WHERE recovery_id = ?",
                (claim.recovery_id,),
            ).fetchone()
            if row is None:
                raise ApprovalDecisionError(
                    "decision_unavailable", claim.recovery_id, status_code=409
                )
            durable_claim = self._decision_claim_from_row(row)
            self._assert_claim_matches_row(claim, durable_claim)
            if durable_claim.response is not None:
                return durable_claim.response
            self._require_resume_owner(
                row,
                recovery_id=claim.recovery_id,
                resume_owner_id=resume_owner_id,
                resume_generation=resume_generation,
                now=now,
            )
            cursor = connection.execute(
                """
                UPDATE approval_decisions
                SET status = 'completed', result_json = ?, completed_at = ?,
                    resume_owner_id = NULL, resume_lease_expires_at = NULL,
                    resume_heartbeat_at = NULL
                WHERE recovery_id = ? AND status = 'claimed'
                  AND resume_owner_id = ? AND resume_generation = ?
                """,
                (
                    serialized,
                    now.isoformat(),
                    claim.recovery_id,
                    resume_owner_id,
                    resume_generation,
                ),
            )
            if cursor.rowcount != 1:
                raise ApprovalDecisionError(
                    "resume_owner_lost", claim.recovery_id, status_code=409
                )
        return response

    def complete_approval_decision(
        self,
        claim: ApprovalDecisionClaim,
        response: ApprovalDecisionResponse,
        *,
        resume_owner_id: str,
        resume_generation: int,
    ) -> ApprovalDecisionResponse:
        """Compatibility wrapper for the Task 5 approval-only method name."""

        completed = self.complete_decision(
            claim,
            response,
            resume_owner_id=resume_owner_id,
            resume_generation=resume_generation,
        )
        if not isinstance(completed, ApprovalDecisionResponse):
            raise TypeError("Approval completion returned a decline response")
        return completed

    def finalize_declined_decision(
        self,
        claim: ApprovalDecisionClaim,
        *,
        exact_interruption_rejected: bool,
        resume_owner_id: str,
        resume_generation: int,
    ) -> DeclineDecisionResponse:
        """Atomically close an exact decline or seal truthful outcome uncertainty."""

        if claim.request.decision != "decline":
            raise ValueError("Decline finalization requires a decline claim")
        if not exact_interruption_rejected:
            raise ValueError("Decline finalization requires exact SDK rejection proof")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = self._now()
            decision_row = connection.execute(
                "SELECT * FROM approval_decisions WHERE recovery_id = ?",
                (claim.recovery_id,),
            ).fetchone()
            if decision_row is None:
                raise ApprovalDecisionError(
                    "decision_unavailable", claim.recovery_id, status_code=409
                )
            durable_claim = self._decision_claim_from_row(decision_row)
            if (
                durable_claim.request.client_decision_id
                != claim.request.client_decision_id
                or durable_claim.request_fingerprint != claim.request_fingerprint
                or durable_claim.request.decision != "decline"
            ):
                raise ApprovalDecisionError(
                    "decision_id_conflict", claim.recovery_id, status_code=409
                )
            if durable_claim.response is not None:
                if not isinstance(durable_claim.response, DeclineDecisionResponse):
                    raise ApprovalDecisionError(
                        "decision_id_conflict", claim.recovery_id, status_code=409
                    )
                return durable_claim.response
            self._require_resume_owner(
                decision_row,
                recovery_id=claim.recovery_id,
                resume_owner_id=resume_owner_id,
                resume_generation=resume_generation,
                now=now,
            )

            recovery = connection.execute(
                "SELECT * FROM recoveries WHERE id = ?",
                (claim.recovery_id,),
            ).fetchone()
            if recovery is None:
                raise RecoveryNotFoundError("Recovery not found")
            if (
                cast(str, recovery["status"])
                != RecoveryStatus.PENDING_APPROVAL.value
                or cast(str, recovery["execution_mode"])
                not in {
                    ExecutionMode.SDK_STUB.value,
                    ExecutionMode.OPENAI_LIVE.value,
                }
            ):
                raise ApprovalDecisionError(
                    "decision_unavailable", claim.recovery_id, status_code=409
                )
            pending = connection.execute(
                "SELECT * FROM pending_approvals WHERE recovery_id = ?",
                (claim.recovery_id,),
            ).fetchone()
            if pending is None or (
                cast(str, pending["status"]) != "rejected"
                or cast(str, pending["tool_call_id"])
                != claim.request.tool_call_id
                or cast(str, pending["consent_digest"])
                != claim.request.remedy_digest
            ):
                raise ApprovalDecisionError(
                    "resume_incompatible", claim.recovery_id, status_code=409
                )
            envelope = self._pending_approval_from_row(pending)
            scope = connection.execute(
                "SELECT * FROM permission_scopes WHERE recovery_id = ?",
                (claim.recovery_id,),
            ).fetchone()
            if scope is None or (
                cast(str, scope["tool_call_id"]) != claim.request.tool_call_id
                or cast(str, scope["remedy_digest"])
                != claim.request.remedy_digest
                or cast(str, scope["status"]) != "pending"
            ):
                raise ApprovalDecisionError(
                    "resume_incompatible", claim.recovery_id, status_code=409
                )
            execution_rows = connection.execute(
                "SELECT provider_execution FROM executions WHERE recovery_id = ?",
                (claim.recovery_id,),
            ).fetchall()
            execution_count = len(execution_rows)
            provider_execution = any(
                bool(cast(int, row["provider_execution"])) for row in execution_rows
            )
            provider_dispatch_started = execution_count > 0 or provider_execution
            if provider_dispatch_started:
                terminal_status = RecoveryStatus.OUTCOME_UNKNOWN
                response_status: Literal[
                    "closed_without_action", "outcome_unknown"
                ] = "outcome_unknown"
                terminal_type = "recovery.outcome_unknown"
                summary = (
                    "Dispatch evidence exists; outcome requires manual reconciliation."
                )
                provider_result = DECLINED_UNKNOWN_PROVIDER_RESULT
                verification_results = list(DECLINED_UNKNOWN_VERIFICATIONS)
            else:
                terminal_status = RecoveryStatus.CLOSED_WITHOUT_ACTION
                response_status = "closed_without_action"
                terminal_type = "recovery.closed_without_action"
                summary = "Closed without action; provider dispatch did not begin."
                provider_result = DECLINED_CLOSED_PROVIDER_RESULT
                verification_results = list(DECLINED_CLOSED_VERIFICATIONS)
            response = DeclineDecisionResponse(
                clientDecisionId=claim.request.client_decision_id,
                recoveryId=claim.recovery_id,
                decision="decline",
                status=response_status,
                decisionRemedyDigest=claim.request.remedy_digest,
                executionStarted=provider_dispatch_started,
            )
            receipt = RecoveryReceipt(
                recoveryId=claim.recovery_id,
                executionMode=envelope.execution_mode,
                status=terminal_status.value,
                simulated=True,
                providerExecution=provider_execution,
                modelIds=list(
                    dict.fromkeys(
                        item.returned_model for item in envelope.model_metadata
                    )
                ),
                rootTraceId=envelope.root_trace_id,
                sdkVersion=envelope.sdk_version,
                protocolVersion=envelope.protocol_version,
                agentGraphVersion=envelope.agent_graph_version,
                promptToolSchemaHash=envelope.definition_digest,
                boundary=non_replay_receipt_boundary(envelope.execution_mode),
                providerResult=provider_result,
                authorizationSource=DECLINED_RECEIPT_AUTHORIZATION,
                verificationResults=verification_results,
                decision="declined",
                decisionRemedyDigest=claim.request.remedy_digest,
                executionCount=execution_count,
                providerDispatchStarted=provider_dispatch_started,
                exactInterruptionRejected=True,
                permissionRevoked=True,
                scopeClosed=True,
            )
            if connection.execute(
                "SELECT 1 FROM receipts WHERE recovery_id = ?",
                (claim.recovery_id,),
            ).fetchone() is not None:
                raise ReceiptTransitionError("Unexpected receipt before decline finalization")
            if connection.execute(
                "SELECT 1 FROM events WHERE recovery_id = ? AND terminal = 1",
                (claim.recovery_id,),
            ).fetchone() is not None:
                raise ReceiptTransitionError(
                    "Unexpected terminal event before decline finalization"
                )
            next_sequence = cast(
                int,
                connection.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE recovery_id = ?",
                    (claim.recovery_id,),
                ).fetchone()[0],
            )
            terminal_data: dict[str, JsonValue] = {
                "decision": "declined",
                "decisionRemedyDigest": claim.request.remedy_digest,
                "executionCount": execution_count,
                "exactInterruptionRejected": True,
                "permissionRevoked": True,
                "phase": "Verify & seal",
                "providerDispatchStarted": provider_dispatch_started,
                "providerExecution": provider_execution,
                "scopeClosed": True,
                "summary": summary,
            }
            connection.execute(
                """
                INSERT INTO receipts (recovery_id, receipt_json, created_at)
                VALUES (?, ?, ?)
                """,
                (
                    claim.recovery_id,
                    receipt.model_dump_json(by_alias=True, exclude_none=True),
                    now.isoformat(),
                ),
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
                    terminal_type,
                    json.dumps(terminal_data, separators=(",", ":"), sort_keys=True),
                    now.isoformat(),
                ),
            )
            connection.execute(
                """
                UPDATE recoveries
                SET status = ?, current_step = 5, current_step_summary = ?,
                    updated_at = ?
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
                SET status = 'closed', updated_at = ?
                WHERE recovery_id = ?
                """,
                (now.isoformat(), claim.recovery_id),
            )
            connection.execute(
                "UPDATE remedies SET status = 'declined' WHERE recovery_id = ?",
                (claim.recovery_id,),
            )
            connection.execute(
                """
                UPDATE permission_scopes
                SET status = 'revoked', revoked_at = ?, updated_at = ?
                WHERE recovery_id = ? AND status = 'pending'
                """,
                (now.isoformat(), now.isoformat(), claim.recovery_id),
            )
            decision_cursor = connection.execute(
                """
                UPDATE approval_decisions
                SET status = 'completed', result_json = ?, completed_at = ?,
                    resume_owner_id = NULL, resume_lease_expires_at = NULL,
                    resume_heartbeat_at = NULL
                WHERE recovery_id = ? AND status = 'claimed'
                  AND resume_owner_id = ? AND resume_generation = ?
                """,
                (
                    response.model_dump_json(by_alias=True),
                    now.isoformat(),
                    claim.recovery_id,
                    resume_owner_id,
                    resume_generation,
                ),
            )
            if decision_cursor.rowcount != 1:
                raise ApprovalDecisionError(
                    "resume_owner_lost", claim.recovery_id, status_code=409
                )
            self._validate_durable_receipt_evidence(
                connection,
                receipt,
                phase="sealed",
            )
            connection.execute(
                """
                INSERT INTO receipt_provenance_migrations (recovery_id, migrated_at)
                VALUES (?, ?)
                """,
                (claim.recovery_id, now.isoformat()),
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

    def is_ready(self) -> bool:
        """Check SQLite integrity and the exact public-ownership schema without repair."""

        try:
            with self._lock, self._connect() as connection:
                quick_check = connection.execute("PRAGMA quick_check(1)").fetchone()
                if quick_check is None or cast(str, quick_check[0]) != "ok":
                    return False
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    return False
                return self._recovery_access_schema_is_exact(
                    connection
                ) and self._public_creation_usage_schema_is_exact(connection)
        except (RuntimeError, sqlite3.DatabaseError):
            return False

    @staticmethod
    def _require_public_identity_hash(value: str) -> None:
        digest = value.removeprefix(HASH_PREFIX)
        if (
            not value.startswith(HASH_PREFIX)
            or len(digest) != 64
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise ValueError("Public identity must be an HMAC-SHA256 digest")

    def is_demo_session_active(
        self,
        *,
        session_hash: str,
        now: datetime,
    ) -> bool:
        """Recognize an unexpired admitted session without mutating durable state."""

        self._require_public_identity_hash(session_hash)
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("Session time must be timezone-aware UTC")
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT expires_at FROM demo_sessions WHERE session_hash = ?",
                (session_hash,),
            ).fetchone()
            if row is None:
                return False
            try:
                stored_expiry = datetime.fromisoformat(cast(str, row["expires_at"]))
            except ValueError as error:
                raise RuntimeError("Stored demo session expiry is invalid") from error
            return stored_expiry > now

    def claim_public_live_admission(
        self,
        *,
        session_hash: str,
        ip_hash: str,
        cooldown: timedelta,
        daily_budget: int,
        session_ttl: timedelta | None = None,
        now: datetime | None = None,
        session_expires_at: datetime | None = None,
    ) -> None:
        """Atomically charge one live start against both durable identities."""

        self._require_public_identity_hash(session_hash)
        self._require_public_identity_hash(ip_hash)
        if cooldown < timedelta(0):
            raise ValueError("Cooldown cannot be negative")
        if daily_budget < 0:
            raise ValueError("Daily budget cannot be negative")
        if session_ttl is not None and session_ttl <= timedelta(0):
            raise ValueError("Session TTL must be positive")
        if session_ttl is None and session_expires_at is None:
            raise ValueError("Session TTL or expiry is required")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            admission_now = self._now() if now is None else now
            if admission_now.tzinfo is None or admission_now.utcoffset() != timedelta(0):
                raise ValueError("Admission time must be timezone-aware UTC")
            resolved_expiry = (
                admission_now + session_ttl
                if session_ttl is not None
                else session_expires_at
            )
            assert resolved_expiry is not None
            if (
                resolved_expiry.tzinfo is None
                or resolved_expiry.utcoffset() != timedelta(0)
            ):
                raise ValueError("Session expiry must be timezone-aware UTC")
            if resolved_expiry <= admission_now:
                raise ValueError("Session expiry must be after admission")
            usage_day = admission_now.date().isoformat()
            identities = (
                ("session", session_hash),
                ("ip", ip_hash),
                ("global", "public-live-global"),
            )
            rows: dict[tuple[str, str], sqlite3.Row] = {}
            for identity_kind, identity_hash in identities:
                row = connection.execute(
                    """
                    SELECT amount, last_admitted_at
                    FROM usage_ledger
                    WHERE identity_kind = ? AND identity_hash = ? AND usage_day = ?
                    """,
                    (identity_kind, identity_hash, usage_day),
                ).fetchone()
                if row is not None:
                    rows[(identity_kind, identity_hash)] = row
            for identity in identities[:2]:
                row = connection.execute(
                    """
                    SELECT last_admitted_at
                    FROM public_live_cooldowns
                    WHERE identity_kind = ? AND identity_hash = ?
                    """,
                    identity,
                ).fetchone()
                if row is None:
                    continue
                last_admitted_at = cast(str, row["last_admitted_at"])
                try:
                    last_admitted = datetime.fromisoformat(last_admitted_at)
                except ValueError as error:
                    raise RuntimeError(
                        "Stored admission timestamp is invalid"
                    ) from error
                if admission_now < last_admitted + cooldown:
                    retry_after_seconds = max(
                        1,
                        math.ceil(
                            (
                                last_admitted
                                + cooldown
                                - admission_now
                            ).total_seconds()
                        ),
                    )
                    raise PublicLiveAdmissionError(
                        "live_cooldown",
                        retry_after_seconds=retry_after_seconds,
                    )
            if any(cast(int, row["amount"]) >= daily_budget for row in rows.values()):
                raise PublicLiveAdmissionError("live_daily_budget_exceeded")
            if daily_budget == 0:
                raise PublicLiveAdmissionError("live_daily_budget_exceeded")

            connection.execute(
                """
                INSERT INTO demo_sessions (
                    session_hash, created_at, last_seen_at, expires_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(session_hash) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at,
                    expires_at = excluded.expires_at
                """,
                (
                    session_hash,
                    admission_now.isoformat(),
                    admission_now.isoformat(),
                    resolved_expiry.isoformat(),
                ),
            )
            for identity_kind, identity_hash in identities:
                connection.execute(
                    """
                    INSERT INTO usage_ledger (
                        identity_kind, identity_hash, usage_day, amount,
                        last_admitted_at, updated_at
                    ) VALUES (?, ?, ?, 1, ?, ?)
                    ON CONFLICT(identity_kind, identity_hash, usage_day) DO UPDATE SET
                        amount = usage_ledger.amount + 1,
                        last_admitted_at = excluded.last_admitted_at,
                        updated_at = excluded.updated_at
                    """,
                    (
                        identity_kind,
                        identity_hash,
                        usage_day,
                        admission_now.isoformat(),
                        admission_now.isoformat(),
                    ),
                )
            for identity_kind, identity_hash in identities[:2]:
                connection.execute(
                    """
                    INSERT INTO public_live_cooldowns (
                        identity_kind, identity_hash, last_admitted_at, updated_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(identity_kind, identity_hash) DO UPDATE SET
                        last_admitted_at = excluded.last_admitted_at,
                        updated_at = excluded.updated_at
                    """,
                    (
                        identity_kind,
                        identity_hash,
                        admission_now.isoformat(),
                        admission_now.isoformat(),
                    ),
                )

    def claim_public_creation_admission(
        self,
        *,
        session_hash: str,
        ip_hash: str,
        session_daily_budget: int,
        ip_daily_budget: int,
        global_daily_budget: int,
        now: datetime | None = None,
    ) -> None:
        """Atomically charge one supported public recovery creation."""

        self._require_public_identity_hash(session_hash)
        self._require_public_identity_hash(ip_hash)
        budgets = (
            session_daily_budget,
            ip_daily_budget,
            global_daily_budget,
        )
        if min(budgets) < 0:
            raise ValueError("Public creation daily budgets cannot be negative")
        identities = (
            ("session", session_hash, session_daily_budget),
            ("ip", ip_hash, ip_daily_budget),
            ("global", "public-creation-global", global_daily_budget),
        )
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            admission_now = self._now() if now is None else now
            if (
                admission_now.tzinfo is None
                or admission_now.utcoffset() != timedelta(0)
            ):
                raise ValueError("Creation admission time must be timezone-aware UTC")
            usage_day = admission_now.date().isoformat()
            next_day = admission_now.date() + timedelta(days=1)
            next_midnight = datetime(
                next_day.year,
                next_day.month,
                next_day.day,
                tzinfo=UTC,
            )
            retry_after_seconds = min(
                86_400,
                max(1, math.ceil((next_midnight - admission_now).total_seconds())),
            )
            amounts: dict[tuple[str, str], int] = {}
            for identity_kind, identity_hash, _budget in identities:
                row = connection.execute(
                    """
                    SELECT amount FROM public_creation_usage
                    WHERE identity_kind = ? AND identity_hash = ? AND usage_day = ?
                    """,
                    (identity_kind, identity_hash, usage_day),
                ).fetchone()
                amounts[(identity_kind, identity_hash)] = (
                    0 if row is None else cast(int, row["amount"])
                )
            if any(
                amounts[(identity_kind, identity_hash)] >= budget
                for identity_kind, identity_hash, budget in identities
            ):
                raise PublicCreationAdmissionError(
                    retry_after_seconds=retry_after_seconds
                )
            for identity_kind, identity_hash, _budget in identities:
                connection.execute(
                    """
                    INSERT INTO public_creation_usage (
                        identity_kind, identity_hash, usage_day, amount, updated_at
                    ) VALUES (?, ?, ?, 1, ?)
                    ON CONFLICT(identity_kind, identity_hash, usage_day) DO UPDATE SET
                        amount = public_creation_usage.amount + 1,
                        updated_at = excluded.updated_at
                    """,
                    (
                        identity_kind,
                        identity_hash,
                        usage_day,
                        admission_now.isoformat(),
                    ),
                )

    def public_creation_usage(
        self,
        *,
        identity_kind: Literal["session", "ip", "global"],
        identity_hash: str,
        usage_day: str,
    ) -> int:
        if identity_kind == "global":
            if identity_hash != "public-creation-global":
                raise ValueError("Global public creation usage uses one fixed key")
        else:
            self._require_public_identity_hash(identity_hash)
        try:
            parsed_day = date.fromisoformat(usage_day)
        except ValueError as error:
            raise ValueError("Usage day must be canonical ISO date") from error
        if parsed_day.isoformat() != usage_day:
            raise ValueError("Usage day must be canonical ISO date")
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT amount FROM public_creation_usage
                WHERE identity_kind = ? AND identity_hash = ? AND usage_day = ?
                """,
                (identity_kind, identity_hash, usage_day),
            ).fetchone()
        return 0 if row is None else cast(int, row["amount"])

    def count_public_creation_usage_rows(self) -> int:
        with self._lock, self._connect() as connection:
            return cast(
                int,
                connection.execute(
                    "SELECT COUNT(*) FROM public_creation_usage"
                ).fetchone()[0],
            )

    def public_live_usage(
        self,
        *,
        identity_kind: Literal["session", "ip", "global"],
        identity_hash: str,
        usage_day: str,
    ) -> int:
        if identity_kind == "global":
            if identity_hash != "public-live-global":
                raise ValueError("Global public-live usage uses one fixed aggregate key")
        else:
            self._require_public_identity_hash(identity_hash)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT amount FROM usage_ledger
                WHERE identity_kind = ? AND identity_hash = ? AND usage_day = ?
                """,
                (identity_kind, identity_hash, usage_day),
            ).fetchone()
        return 0 if row is None else cast(int, row["amount"])

    def count_public_live_usage_rows(self) -> int:
        with self._lock, self._connect() as connection:
            return cast(
                int,
                connection.execute(
                    "SELECT COUNT(*) FROM usage_ledger WHERE identity_kind IN ('session', 'ip')"
                ).fetchone()[0],
            )

    def count_demo_sessions(self) -> int:
        with self._lock, self._connect() as connection:
            return cast(
                int,
                connection.execute("SELECT COUNT(*) FROM demo_sessions").fetchone()[0],
            )

    def cleanup_terminal_recoveries(
        self,
        *,
        cutoff: datetime,
        batch_size: int,
    ) -> int:
        """Delete at most one bounded batch of old terminal recovery graphs."""

        if cutoff.tzinfo is None or cutoff.utcoffset() != timedelta(0):
            raise ValueError("Cleanup cutoff must be timezone-aware UTC")
        if batch_size < 1:
            raise ValueError("Cleanup batch size must be positive")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT id FROM recoveries
                WHERE status IN ('completed', 'closed_without_action', 'outcome_unknown')
                  AND updated_at < ?
                ORDER BY updated_at ASC, id ASC
                LIMIT ?
                """,
                (cutoff.isoformat(), batch_size),
            ).fetchall()
            recovery_ids = [cast(str, row["id"]) for row in rows]
            for recovery_id in recovery_ids:
                deleted = connection.execute(
                    """
                    DELETE FROM recoveries
                    WHERE id = ?
                      AND status IN (
                          'completed', 'closed_without_action', 'outcome_unknown'
                      )
                      AND updated_at < ?
                    """,
                    (recovery_id, cutoff.isoformat()),
                )
                if deleted.rowcount != 1:
                    raise RuntimeError("Cleanup lost its terminal recovery candidate")
        return len(recovery_ids)

    def cleanup_expired_demo_sessions(
        self,
        *,
        cutoff: datetime,
        batch_size: int,
    ) -> int:
        """Delete only expired session hashes; durable usage and cooldowns remain."""

        if cutoff.tzinfo is None or cutoff.utcoffset() != timedelta(0):
            raise ValueError("Session cleanup cutoff must be timezone-aware UTC")
        if batch_size < 1:
            raise ValueError("Session cleanup batch size must be positive")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT session_hash FROM demo_sessions
                WHERE expires_at <= ?
                ORDER BY expires_at ASC, session_hash ASC
                LIMIT ?
                """,
                (cutoff.isoformat(), batch_size),
            ).fetchall()
            session_hashes = [cast(str, row["session_hash"]) for row in rows]
            for session_hash in session_hashes:
                deleted = connection.execute(
                    """
                    DELETE FROM demo_sessions
                    WHERE session_hash = ? AND expires_at <= ?
                    """,
                    (session_hash, cutoff.isoformat()),
                )
                if deleted.rowcount != 1:
                    raise RuntimeError("Session cleanup lost its expired candidate")
        return len(session_hashes)

    def cleanup_public_creation_usage(
        self,
        *,
        cutoff_day: str,
        batch_size: int,
    ) -> int:
        """Delete one indexed batch strictly older than a canonical UTC day."""

        try:
            parsed_cutoff = date.fromisoformat(cutoff_day)
        except (TypeError, ValueError) as error:
            raise ValueError("Creation usage cutoff must be a canonical ISO date") from error
        if parsed_cutoff.isoformat() != cutoff_day:
            raise ValueError("Creation usage cutoff must be a canonical ISO date")
        if batch_size < 1:
            raise ValueError("Creation usage cleanup batch size must be positive")
        canonical_cutoff = min(parsed_cutoff, self._now().date()).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT identity_kind, identity_hash, usage_day
                FROM public_creation_usage
                WHERE usage_day < ?
                ORDER BY usage_day ASC, identity_kind ASC, identity_hash ASC
                LIMIT ?
                """,
                (canonical_cutoff, batch_size),
            ).fetchall()
            keys = [
                (
                    cast(str, row["identity_kind"]),
                    cast(str, row["identity_hash"]),
                    cast(str, row["usage_day"]),
                )
                for row in rows
            ]
            for identity_kind, identity_hash, usage_day in keys:
                deleted = connection.execute(
                    """
                    DELETE FROM public_creation_usage
                    WHERE identity_kind = ? AND identity_hash = ? AND usage_day = ?
                      AND usage_day < ?
                    """,
                    (identity_kind, identity_hash, usage_day, canonical_cutoff),
                )
                if deleted.rowcount != 1:
                    raise RuntimeError("Creation usage cleanup lost its candidate")
        return len(keys)

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

    def update_pending_model_metadata(
        self,
        recovery_id: str,
        metadata: tuple[ModelResponseMetadata, ...],
        *,
        resume_owner_id: str,
        resume_generation: int,
    ) -> None:
        """Append one allowlisted live response under the exact resume owner."""

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = self._now()
            recovery = connection.execute(
                "SELECT status, execution_mode FROM recoveries WHERE id = ?",
                (recovery_id,),
            ).fetchone()
            if recovery is None:
                raise RecoveryNotFoundError("Recovery not found")
            if (
                cast(str, recovery["status"])
                != RecoveryStatus.PENDING_APPROVAL.value
                or cast(str, recovery["execution_mode"])
                != ExecutionMode.OPENAI_LIVE.value
            ):
                raise ApprovalDecisionError(
                    "decision_unavailable", recovery_id, status_code=409
                )
            decision = connection.execute(
                "SELECT * FROM approval_decisions WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone()
            if decision is None:
                raise ApprovalDecisionError(
                    "decision_unavailable", recovery_id, status_code=409
                )
            self._require_resume_owner(
                decision,
                recovery_id=recovery_id,
                resume_owner_id=resume_owner_id,
                resume_generation=resume_generation,
                now=now,
            )
            pending = connection.execute(
                "SELECT * FROM pending_approvals WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone()
            if pending is None:
                raise RecoveryNotFoundError("Pending approval not found")
            if cast(str, pending["status"]) not in {"approved", "declined"}:
                raise ApprovalDecisionError(
                    "decision_unavailable", recovery_id, status_code=409
                )
            raw_current = json.loads(cast(str, pending["model_metadata_json"]))
            if not isinstance(raw_current, list):
                raise ApprovalDecisionError(
                    "model_metadata_conflict", recovery_id, status_code=409
                )
            current = tuple(
                ModelResponseMetadata.from_json_value(item) for item in raw_current
            )
            revision = cast(int, pending["model_metadata_revision"])
            if (
                revision != len(current)
                or len(metadata) != len(current) + 1
                or metadata[:-1] != current
            ):
                raise ApprovalDecisionError(
                    "model_metadata_conflict", recovery_id, status_code=409
                )
            serialized = json.dumps(
                [item.to_json_value() for item in metadata],
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            cursor = connection.execute(
                """
                UPDATE pending_approvals
                SET model_metadata_json = ?, model_metadata_revision = ?, updated_at = ?
                WHERE recovery_id = ? AND model_metadata_revision = ?
                """,
                (serialized, revision + 1, now.isoformat(), recovery_id, revision),
            )
            if cursor.rowcount != 1:
                raise ApprovalDecisionError(
                    "model_metadata_conflict", recovery_id, status_code=409
                )

    def delete_unstarted_recovery(self, recovery_id: str) -> bool:
        """Remove only this run's pre-pending orphan after a live model failure."""

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            recovery = connection.execute(
                "SELECT status FROM recoveries WHERE id = ?",
                (recovery_id,),
            ).fetchone()
            if recovery is None:
                return False
            if cast(str, recovery["status"]) != RecoveryStatus.IN_PROGRESS.value:
                return False
            protected_tables = (
                "pending_approvals",
                "approval_decisions",
                "executions",
                "receipts",
            )
            for table in protected_tables:
                if connection.execute(
                    f"SELECT 1 FROM {table} WHERE recovery_id = ? LIMIT 1",
                    (recovery_id,),
                ).fetchone() is not None:
                    return False
            cursor = connection.execute(
                "DELETE FROM recoveries WHERE id = ? AND status = ?",
                (recovery_id, RecoveryStatus.IN_PROGRESS.value),
            )
            return cursor.rowcount == 1

    def get_permission_scope(self, recovery_id: str) -> PermissionScopeRecord:
        """Load the internal exact temporary-permission state for verification."""

        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM permission_scopes WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone()
        if row is None:
            raise RecoveryNotFoundError("Permission scope not found")
        return self._permission_scope_from_row(row)

    def update_pending_approval_status(self, recovery_id: str, *, status: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = self._now()
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

    def record_exact_interruption_rejected(
        self,
        claim: ApprovalDecisionClaim,
        *,
        resume_owner_id: str,
        resume_generation: int,
    ) -> None:
        """Persist proof only after the rejected SDK run returns terminal closure."""

        if claim.request.decision != "decline":
            raise ValueError("SDK rejection proof requires a decline claim")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = self._now()
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
                or durable_claim.request.decision != "decline"
            ):
                raise ApprovalDecisionError(
                    "decision_id_conflict", claim.recovery_id, status_code=409
                )
            if durable_claim.response is not None:
                return
            self._require_resume_owner(
                row,
                recovery_id=claim.recovery_id,
                resume_owner_id=resume_owner_id,
                resume_generation=resume_generation,
                now=now,
            )
            cursor = connection.execute(
                """
                UPDATE pending_approvals
                SET status = 'rejected', updated_at = ?
                WHERE recovery_id = ? AND status IN ('declined', 'rejected')
                  AND tool_call_id = ? AND consent_digest = ?
                """,
                (
                    now.isoformat(),
                    claim.recovery_id,
                    claim.request.tool_call_id,
                    claim.request.remedy_digest,
                ),
            )
            if cursor.rowcount != 1:
                raise ApprovalDecisionError(
                    "resume_incompatible", claim.recovery_id, status_code=409
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
                JOIN approval_decisions
                  ON approval_decisions.recovery_id = executions.recovery_id
                 AND approval_decisions.decision = 'approve'
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
        resume_owner_id: str,
        resume_generation: int,
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
        if (
            receipt.decision != "approved"
            or receipt.decision_remedy_digest != execution.remedy_digest
            or receipt.execution_count != 1
            or not receipt.provider_dispatch_started
            or receipt.exact_interruption_rejected
            or not receipt.permission_revoked
            or not receipt.scope_closed
        ):
            raise ReceiptTransitionError("Execution receipt decision evidence mismatch")
        provider_result = execution.result_json.get("provider_result")
        if not isinstance(provider_result, str) or receipt.provider_result != provider_result:
            raise ReceiptTransitionError("Execution receipt provider result mismatch")
        receipt_json = receipt.model_dump_json(by_alias=True, exclude_none=True)
        terminal_data: dict[str, JsonValue] = {
            "decision": "approved",
            "decisionRemedyDigest": execution.remedy_digest,
            "executionCount": 1,
            "exactInterruptionRejected": False,
            "permissionRevoked": True,
            "recoveryId": execution.recovery_id,
            "executionMode": receipt.execution_mode.value,
            "providerExecution": execution.provider_execution,
            "providerDispatchStarted": True,
            "scopeClosed": True,
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
            now = self._now()
            recovery = connection.execute(
                "SELECT execution_mode FROM recoveries WHERE id = ?",
                (execution.recovery_id,),
            ).fetchone()
            if recovery is None:
                raise RecoveryNotFoundError("Recovery not found")
            if ExecutionMode(cast(str, recovery["execution_mode"])) is not receipt.execution_mode:
                raise ReceiptTransitionError("Execution receipt mode mismatch")
            decision = connection.execute(
                "SELECT * FROM approval_decisions WHERE recovery_id = ?",
                (execution.recovery_id,),
            ).fetchone()
            if decision is None:
                raise ReceiptTransitionError("Execution is missing its approval decision")
            self._require_resume_owner(
                decision,
                recovery_id=execution.recovery_id,
                resume_owner_id=resume_owner_id,
                resume_generation=resume_generation,
                now=now,
            )
            decision_claim = self._decision_claim_from_row(decision)
            if (
                decision_claim.request.decision != "approve"
                or decision_claim.request.tool_call_id != execution.tool_call_id
                or decision_claim.request.remedy_digest != execution.remedy_digest
            ):
                raise ReceiptTransitionError("Execution approval decision mismatch")
            execution_count = cast(
                int,
                connection.execute(
                    "SELECT COUNT(*) FROM executions WHERE recovery_id = ?",
                    (execution.recovery_id,),
                ).fetchone()[0],
            )
            if execution_count != 1:
                raise ReceiptTransitionError("Execution receipt count mismatch")
            scope = connection.execute(
                "SELECT * FROM permission_scopes WHERE recovery_id = ?",
                (execution.recovery_id,),
            ).fetchone()
            if scope is None or (
                cast(str, scope["tool_call_id"]) != execution.tool_call_id
                or cast(str, scope["remedy_digest"]) != execution.remedy_digest
                or cast(str, scope["status"]) not in {"active", "revoked"}
            ):
                raise ReceiptTransitionError("Execution permission scope mismatch")

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
            self._validate_durable_receipt_evidence(
                connection,
                receipt,
                phase="sealed" if existing_receipt is not None else "preseal",
            )

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
            connection.execute(
                """
                UPDATE permission_scopes
                SET status = 'revoked', revoked_at = COALESCE(revoked_at, ?),
                    updated_at = ?
                WHERE recovery_id = ? AND status IN ('active', 'revoked')
                """,
                (now.isoformat(), now.isoformat(), execution.recovery_id),
            )
            self._validate_durable_receipt_evidence(
                connection,
                receipt,
                phase="sealed",
            )
            marker_cursor = connection.execute(
                """
                INSERT OR IGNORE INTO receipt_provenance_migrations (
                    recovery_id, migrated_at
                ) VALUES (?, ?)
                """,
                (execution.recovery_id, now.isoformat()),
            )
            if marker_cursor.rowcount == 1:
                changed = True
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

    def read_event_batch_for_session(
        self,
        recovery_id: str,
        *,
        session_hash: str,
        after_seq: int = 0,
    ) -> tuple[list[RecoveryEvent], RecoveryStatus]:
        """Read an owner-authorized event batch from one SQLite snapshot."""

        self._require_public_identity_hash(session_hash)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN")
            access = connection.execute(
                """
                SELECT 1 FROM recovery_access
                WHERE recovery_id = ? AND session_hash = ?
                """,
                (recovery_id, session_hash),
            ).fetchone()
            if access is None:
                raise RecoveryNotFoundError("Recovery not found")
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

    def get_receipt_for_session(
        self,
        recovery_id: str,
        session_hash: str,
    ) -> RecoveryReceipt:
        self._require_public_identity_hash(session_hash)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT receipts.receipt_json FROM receipts
                JOIN recovery_access
                  ON recovery_access.recovery_id = receipts.recovery_id
                WHERE receipts.recovery_id = ? AND recovery_access.session_hash = ?
                """,
                (recovery_id, session_hash),
            ).fetchone()
        if row is None:
            raise RecoveryNotFoundError("Receipt not found")
        return RecoveryReceipt.model_validate_json(cast(str, row["receipt_json"]))

    def reset(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM recoveries")

    def reset_for_session(self, session_hash: str) -> None:
        """Delete only singly-owned detail and detach this session from shared rows."""

        self._require_public_identity_hash(session_hash)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                DELETE FROM recoveries
                WHERE EXISTS (
                    SELECT 1 FROM recovery_access AS caller_access
                    WHERE caller_access.recovery_id = recoveries.id
                      AND caller_access.session_hash = ?
                )
                  AND NOT EXISTS (
                    SELECT 1 FROM recovery_access AS other_access
                    WHERE other_access.recovery_id = recoveries.id
                      AND other_access.session_hash <> ?
                )
                """,
                (session_hash, session_hash),
            )
            connection.execute(
                "DELETE FROM recovery_access WHERE session_hash = ?",
                (session_hash,),
            )

    def close(self) -> None:
        with self._lock:
            self._closed = True
