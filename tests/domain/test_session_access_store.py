from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from server.models import ExecutionMode, ScenarioId
from server.orchestrator import RecoveryOrchestrator
from server.providers.hotel_simulator import HotelSimulator
from server.replay.engine import ReplayEngine
from server.replay.loader import ScenarioLoader
from server.store import ReceiptTransitionError, SQLiteStore

_SESSION_HASH = "hmac-sha256:" + "b" * 64


def _assert_creation_tables_empty(database_path) -> None:
    with sqlite3.connect(database_path) as connection:
        for table in (
            "recoveries",
            "recovery_access",
            "events",
            "receipts",
            "executions",
            "pending_approvals",
            "permission_scopes",
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (
                0,
            )


@pytest.mark.parametrize(
    "malformed_schema",
    [
        """
        CREATE TABLE recovery_access (
            recovery_id TEXT NOT NULL REFERENCES recoveries(id) ON DELETE CASCADE,
            session_hash BLOB NOT NULL,
            PRIMARY KEY (recovery_id, session_hash)
        )
        """,
        """
        CREATE TABLE recovery_access (
            recovery_id TEXT REFERENCES recoveries(id) ON DELETE CASCADE,
            session_hash TEXT NOT NULL,
            PRIMARY KEY (recovery_id, session_hash)
        )
        """,
        """
        CREATE TABLE recovery_access (
            recovery_id TEXT NOT NULL REFERENCES recoveries(id),
            session_hash TEXT NOT NULL,
            PRIMARY KEY (recovery_id, session_hash)
        )
        """,
    ],
    ids=["wrong-type", "nullable", "wrong-foreign-key"],
)
def test_startup_rejects_malformed_recovery_access_table(
    tmp_path,
    malformed_schema: str,
) -> None:
    database_path = tmp_path / "malformed-access.sqlite3"
    SQLiteStore(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE recovery_access")
        connection.execute(malformed_schema)

    with pytest.raises(RuntimeError, match="Unsupported recovery access"):
        SQLiteStore(database_path)


def test_startup_rejects_wrong_named_session_index_metadata(tmp_path) -> None:
    database_path = tmp_path / "malformed-access-index.sqlite3"
    SQLiteStore(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP INDEX recovery_access_session_idx")
        connection.execute(
            "CREATE UNIQUE INDEX recovery_access_session_idx "
            "ON recovery_access(session_hash)"
        )

    with pytest.raises(RuntimeError, match="Unsupported recovery access"):
        SQLiteStore(database_path)


def test_hotel_sdk_initial_creation_rolls_back_owner_binding_with_graph(
    tmp_path,
) -> None:
    database_path = tmp_path / "sdk-owner-rollback.sqlite3"
    store = SQLiteStore(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_sdk_created_event
            BEFORE INSERT ON events
            BEGIN
                SELECT RAISE(ABORT, 'reject sdk creation');
            END
            """
        )
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(store=store),
    )

    with pytest.raises(sqlite3.IntegrityError, match="reject sdk creation"):
        asyncio.run(
            orchestrator.start(
                ScenarioId.HOTEL,
                execution_mode=ExecutionMode.SDK_STUB,
                session_hash=_SESSION_HASH,
            )
        )

    _assert_creation_tables_empty(database_path)


@pytest.mark.parametrize("scenario_id", [ScenarioId.HOTEL, ScenarioId.API_QUOTA])
def test_replay_creation_rolls_back_owner_binding_with_graph(
    tmp_path,
    scenario_id: ScenarioId,
) -> None:
    database_path = tmp_path / f"replay-owner-rollback-{scenario_id.value}.sqlite3"
    store = SQLiteStore(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_replay_receipt
            BEFORE INSERT ON receipts
            BEGIN
                SELECT RAISE(ABORT, 'reject replay creation');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="reject replay creation"):
        ReplayEngine(store, ScenarioLoader()).start(
            scenario_id,
            execution_mode=ExecutionMode.REPLAY_FIXTURE,
            session_hash=_SESSION_HASH,
        )

    _assert_creation_tables_empty(database_path)


def test_completed_quota_creation_rolls_back_owner_binding_with_graph(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "quota-owner-rollback.sqlite3"
    store = SQLiteStore(database_path)
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(store=store),
    )

    def reject_after_graph(*_args, **_kwargs) -> None:
        raise ReceiptTransitionError("reject quota creation")

    monkeypatch.setattr(store, "_validate_durable_receipt_evidence", reject_after_graph)
    with pytest.raises(ReceiptTransitionError, match="reject quota creation"):
        asyncio.run(orchestrator.run_quota_stub(session_hash=_SESSION_HASH))

    _assert_creation_tables_empty(database_path)


def test_pending_live_creation_rolls_back_owner_binding_with_graph(tmp_path) -> None:
    source_store = SQLiteStore(tmp_path / "pending-source.sqlite3")
    source_orchestrator = RecoveryOrchestrator(
        store=source_store,
        hotel_provider=HotelSimulator(store=source_store),
    )
    source_pending = asyncio.run(
        source_orchestrator.start(
            ScenarioId.HOTEL,
            execution_mode=ExecutionMode.SDK_STUB,
        )
    )
    source_id = source_pending.recovery.recovery_id
    envelope = replace(
        source_store.get_pending_approval(source_id),
        execution_mode=ExecutionMode.OPENAI_LIVE,
    )
    consent = source_store.get_remedy_consent(source_id)

    database_path = tmp_path / "pending-owner-rollback.sqlite3"
    store = SQLiteStore(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_pending_scope
            BEFORE INSERT ON permission_scopes
            BEGIN
                SELECT RAISE(ABORT, 'reject pending creation');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="reject pending creation"):
        store.create_pending_recovery(
            recovery_id=source_id,
            scenario_id=ScenarioId.HOTEL,
            execution_mode=ExecutionMode.OPENAI_LIVE,
            current_step_summary="Pending live recovery.",
            event_data={"summary": "Pending live recovery."},
            pending_approval=envelope,
            remedy_consent=consent,
            session_hash=_SESSION_HASH,
        )

    _assert_creation_tables_empty(database_path)


def test_terminal_cleanup_cascades_access_without_refunding_aggregates(tmp_path) -> None:
    database_path = tmp_path / "cleanup-access-cascade.sqlite3"
    store = SQLiteStore(database_path)
    replay = ReplayEngine(store, ScenarioLoader()).start(
        ScenarioId.HOTEL,
        execution_mode=ExecutionMode.REPLAY_FIXTURE,
        session_hash=_SESSION_HASH,
    )
    now = datetime.now(UTC)
    store.claim_public_live_admission(
        session_hash=_SESSION_HASH,
        ip_hash="hmac-sha256:" + "c" * 64,
        cooldown=timedelta(0),
        daily_budget=10,
        session_expires_at=now + timedelta(hours=1),
    )
    before_usage = store.count_public_live_usage_rows()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE recoveries SET updated_at = ? WHERE id = ?",
            ((now - timedelta(days=2)).isoformat(), replay.recovery_id),
        )

    assert store.cleanup_terminal_recoveries(cutoff=now, batch_size=10) == 1
    assert store.count_public_live_usage_rows() == before_usage
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM recovery_access WHERE recovery_id = ?",
            (replay.recovery_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM public_live_cooldowns"
        ).fetchone()[0] > 0
