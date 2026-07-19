import asyncio
import json
import sqlite3

import pytest
from agents.exceptions import UserError

from server.models import ExecutionMode, RecoveryStatus
from server.orchestrator import RecoveryOrchestrator
from server.providers.hotel_simulator import HotelSimulator
from server.store import RecoveryNotFoundError, SQLiteStore


def test_pending_sdk_approval_resumes_after_every_runtime_object_is_recreated(
    tmp_path,
) -> None:
    database_path = tmp_path / "restart-resume.sqlite3"
    store = SQLiteStore(database_path)
    provider = HotelSimulator(store=store)
    orchestrator = RecoveryOrchestrator(store=store, hotel_provider=provider)

    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    recovery_id = pending.recovery.recovery_id
    interruption = pending.sdk_result.interruptions[0]
    envelope = store.get_pending_approval(recovery_id)

    assert envelope.tool_call_id == interruption.call_id
    assert envelope.execution_mode is ExecutionMode.SDK_STUB
    assert envelope.sdk_version == "0.18.3"
    assert envelope.protocol_version
    assert envelope.agent_graph_version
    assert len(envelope.definition_digest) == 64
    assert envelope.root_trace_id.startswith("qa_trace_")
    assert len(envelope.remedy_digest) == 64
    assert envelope.state_json
    assert provider.dispatch_count == 0
    public_event_json = json.dumps(
        store.list_events(recovery_id)[-1].data,
        sort_keys=True,
    )
    assert interruption.call_id not in public_event_json
    assert "commit_remedy" not in public_event_json

    store.close()
    del orchestrator, provider, store, pending

    reopened_store = SQLiteStore(database_path)
    fresh_provider = HotelSimulator(store=reopened_store)
    fresh_orchestrator = RecoveryOrchestrator(
        store=reopened_store,
        hotel_provider=fresh_provider,
    )

    completed = asyncio.run(fresh_orchestrator.resume_approved(recovery_id))

    assert completed.interruptions == []
    assert fresh_provider.dispatch_count == 1
    assert reopened_store.get_recovery(recovery_id).status is RecoveryStatus.COMPLETED
    assert reopened_store.get_receipt(recovery_id).provider_execution is True
    assert reopened_store.count_executions(recovery_id) == 1


def test_startup_reconciles_a_committed_execution_without_redispatch(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "restart-reconcile.sqlite3"
    store = SQLiteStore(database_path)
    provider = HotelSimulator(store=store)
    orchestrator = RecoveryOrchestrator(store=store, hotel_provider=provider)
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    recovery_id = pending.recovery.recovery_id
    original_transition = store.record_transition

    class SimulatedProcessCrash(RuntimeError):
        pass

    def crash_after_execution(*args, **kwargs):
        if kwargs.get("status") is RecoveryStatus.COMPLETED:
            raise SimulatedProcessCrash("crash after provider commit")
        return original_transition(*args, **kwargs)

    monkeypatch.setattr(store, "record_transition", crash_after_execution)

    with pytest.raises(UserError, match="crash after provider commit"):
        asyncio.run(orchestrator.resume_approved(recovery_id))

    assert provider.dispatch_count == 1
    assert store.count_executions(recovery_id) == 1
    with pytest.raises(RecoveryNotFoundError, match="Receipt not found"):
        store.get_receipt(recovery_id)
    store.close()

    fresh_store = SQLiteStore(database_path)
    fresh_provider = HotelSimulator(store=fresh_store)
    RecoveryOrchestrator(store=fresh_store, hotel_provider=fresh_provider)

    assert fresh_provider.dispatch_count == 0
    assert fresh_store.get_recovery(recovery_id).status is RecoveryStatus.COMPLETED
    assert fresh_store.get_receipt(recovery_id).provider_execution is True
    assert fresh_store.count_executions(recovery_id) == 1
    terminal_events = [
        event for event in fresh_store.list_events(recovery_id) if event.terminal
    ]
    assert len(terminal_events) == 1
    fresh_store.close()

    second_store = SQLiteStore(database_path)
    second_provider = HotelSimulator(store=second_store)
    RecoveryOrchestrator(store=second_store, hotel_provider=second_provider)
    assert second_provider.dispatch_count == 0
    second_terminal_events = [
        event for event in second_store.list_events(recovery_id) if event.terminal
    ]
    assert len(second_terminal_events) == 1


def test_task3_schema_migration_preserves_parent_child_rows_and_foreign_keys(
    tmp_path,
) -> None:
    database_path = tmp_path / "task3-schema.sqlite3"
    timestamp = "2026-07-18T20:00:00+00:00"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE recoveries (
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
            CREATE TABLE remedies (
                id TEXT PRIMARY KEY,
                recovery_id TEXT NOT NULL REFERENCES recoveries(id) ON DELETE CASCADE,
                terms_json TEXT NOT NULL,
                digest TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE pending_approvals (
                tool_call_id TEXT PRIMARY KEY,
                recovery_id TEXT NOT NULL REFERENCES recoveries(id) ON DELETE CASCADE,
                serialized_state TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE executions (
                id TEXT PRIMARY KEY,
                recovery_id TEXT NOT NULL REFERENCES recoveries(id) ON DELETE CASCADE,
                idempotency_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                provider_execution INTEGER NOT NULL DEFAULT 0,
                result_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recovery_id TEXT NOT NULL REFERENCES recoveries(id) ON DELETE CASCADE,
                seq INTEGER NOT NULL,
                type TEXT NOT NULL,
                terminal INTEGER NOT NULL DEFAULT 0,
                data_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (recovery_id, seq)
            );
            """
        )
        connection.execute(
            "INSERT INTO recoveries VALUES (?, 'hotel', 'sdk_stub', "
            "'pending_approval', 3, 'Legacy pending state.', ?, ?)",
            ("legacy-recovery", timestamp, timestamp),
        )
        connection.execute(
            "INSERT INTO remedies VALUES "
            "('legacy-remedy', 'legacy-recovery', '{}', NULL, 'pending', ?)",
            (timestamp,),
        )
        connection.execute(
            "INSERT INTO pending_approvals VALUES "
            "('legacy-call', 'legacy-recovery', '{\"legacy\":true}', "
            "'pending', ?, ?)",
            (timestamp, timestamp),
        )
        connection.execute(
            "INSERT INTO executions VALUES "
            "('legacy-execution', 'legacy-recovery', 'legacy-key', 'pending', "
            "0, NULL, ?, ?)",
            (timestamp, timestamp),
        )
        connection.execute(
            "INSERT INTO events (recovery_id, seq, type, terminal, data_json, created_at) "
            "VALUES ('legacy-recovery', 1, 'approval.requested', 0, '{}', ?)",
            (timestamp,),
        )

    migrated = SQLiteStore(database_path)
    envelope = migrated.get_pending_approval("legacy-recovery")

    assert envelope.tool_call_id == "legacy-call"
    assert envelope.sdk_version == "legacy-incompatible"
    assert envelope.state_json == {"legacy": True}
    assert migrated.count_executions("legacy-recovery") == 1
    assert len(migrated.list_events("legacy-recovery")) == 1
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM remedies").fetchone() == (1,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
