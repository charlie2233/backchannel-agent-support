import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Thread
from uuid import UUID

import pytest

from server.models import (
    SDK_STUB_BOUNDARY,
    ApprovalDecisionRequest,
    DecisionAction,
    ExecutionMode,
    RecoveryStatus,
    ScenarioId,
)
from server.replay.engine import ReplayEngine, replay_recovery_id
from server.replay.loader import ScenarioLoader
from server.store import ApprovalDecisionError, SQLiteStore

REQUIRED_TABLES = {
    "approval_decisions",
    "demo_sessions",
    "events",
    "executions",
    "pending_approvals",
    "readiness_probe",
    "receipts",
    "recovery_creations",
    "recoveries",
    "remedies",
    "usage_ledger",
}

APPROVED_DIGEST = f"sha256:{'a' * 64}"


def test_task7_migrates_legacy_sdk_receipt_from_durable_pending_provenance(
    tmp_path,
) -> None:
    database_path = tmp_path / "legacy-sdk-receipt.sqlite3"
    timestamp = "2026-07-18T20:00:00+00:00"
    recovery_id = "legacy-sdk-completed"
    root_trace_id = "qa_trace_0123456789abcdef0123456789abcdef"
    definition_digest = "b" * 64
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
            CREATE TABLE pending_approvals (
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
            CREATE TABLE receipts (
                recovery_id TEXT PRIMARY KEY REFERENCES recoveries(id) ON DELETE CASCADE,
                receipt_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO recoveries VALUES (?, 'hotel', 'sdk_stub', 'completed', 5, ?, ?, ?)",
            (recovery_id, "Legacy SDK recovery completed.", timestamp, timestamp),
        )
        connection.execute(
            """
            INSERT INTO pending_approvals VALUES (
                'legacy-call', ?, '0.18.3', 'backchannel.approval.v1',
                'backchannel.hotel-agent.v1', ?, ?, 'sdk_stub', ?, 'legacy-remedy',
                ?, '{}', 'completed', ?, ?
            )
            """,
            (
                recovery_id,
                definition_digest,
                root_trace_id,
                "c" * 64,
                APPROVED_DIGEST,
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            "INSERT INTO receipts VALUES (?, ?, ?)",
            (
                recovery_id,
                json.dumps(
                    {
                        "recoveryId": recovery_id,
                        "executionMode": "sdk_stub",
                        "status": "completed",
                        "simulated": True,
                        "providerExecution": True,
                        "modelIds": [],
                        "boundary": SDK_STUB_BOUNDARY,
                        "providerResult": "Legacy demo provider result.",
                        "authorizationSource": "Legacy exact approval.",
                        "verificationResults": ["Legacy provider result verified."],
                        "approvedRemedyDigest": APPROVED_DIGEST,
                    }
                ),
                timestamp,
            ),
        )

    store = SQLiteStore(database_path)
    receipt = store.get_receipt(recovery_id)

    assert receipt.execution_mode is ExecutionMode.SDK_STUB
    assert receipt.model_call is False
    assert receipt.model_ids == []
    assert receipt.root_trace_id == root_trace_id
    assert receipt.sdk_version == "0.18.3"
    assert receipt.protocol_version == "backchannel.approval.v1"
    assert receipt.agent_graph_version == "backchannel.hotel-agent.v1"
    assert receipt.definition_digest == definition_digest
    with sqlite3.connect(database_path) as connection:
        raw = json.loads(
            connection.execute(
                "SELECT receipt_json FROM receipts WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone()[0]
        )
    assert raw["modelCall"] is False
    assert raw["rootTraceId"] == root_trace_id


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


def test_concurrent_replay_starts_reuse_one_atomic_definition_snapshot(tmp_path) -> None:
    database_path = tmp_path / "atomic-replay.sqlite3"
    SQLiteStore(database_path).close()
    stores = [SQLiteStore(database_path) for _ in range(8)]
    loaders = [ScenarioLoader() for _ in stores]

    def start(index: int):
        return ReplayEngine(stores[index], loaders[index]).start(
            "api-quota",
            execution_mode=ExecutionMode.REPLAY_FIXTURE,
        )

    try:
        with ThreadPoolExecutor(max_workers=len(stores)) as executor:
            snapshots = list(executor.map(start, range(len(stores))))

        recovery_ids = {snapshot.recovery_id for snapshot in snapshots}
        assert len(recovery_ids) == 1
        recovery_id = recovery_ids.pop()
        expected = loaders[0].get("api-quota")
        assert recovery_id == replay_recovery_id(expected)
        events = stores[0].list_events(recovery_id)
        assert [event.type for event in events] == [
            "recovery.created",
            *(event.type for event in expected.events),
        ]
        assert events[-1].terminal is True
        assert stores[0].get_receipt(recovery_id).status == "simulated_completed"

        repeated = ReplayEngine(stores[0], loaders[0]).start(
            "api-quota",
            execution_mode=ExecutionMode.REPLAY_FIXTURE,
        )
        assert repeated == snapshots[0]
        with sqlite3.connect(database_path) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM recoveries WHERE execution_mode = 'replay_fixture'"
            ).fetchone() == (1,)
    finally:
        for store in stores:
            store.close()


def test_replay_persistence_rolls_back_an_incomplete_event_set(tmp_path) -> None:
    database_path = tmp_path / "atomic-replay-rollback.sqlite3"
    store = SQLiteStore(database_path)
    loader = ScenarioLoader()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_partial_replay
            BEFORE INSERT ON events
            WHEN NEW.type = 'quota.burst_selected'
            BEGIN
                SELECT RAISE(ABORT, 'injected replay event failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected replay event failure"):
        ReplayEngine(store, loader).start(
            "api-quota",
            execution_mode=ExecutionMode.REPLAY_FIXTURE,
        )

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM recoveries").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone() == (0,)
        connection.execute("DROP TRIGGER reject_partial_replay")

    completed = ReplayEngine(store, loader).start(
        "api-quota",
        execution_mode=ExecutionMode.REPLAY_FIXTURE,
    )
    assert len(store.list_events(completed.recovery_id)) == len(
        loader.get("api-quota").events
    ) + 1


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
    assert [event.type for event in store.list_events("legacy-replay")] == ["recovery.completed"]
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


def test_task6_action_migration_preserves_completed_approval_replay(tmp_path) -> None:
    database_path = tmp_path / "task6-action-migration.sqlite3"
    timestamp = "2026-07-18T20:00:00+00:00"
    recovery_id = "legacy-approved-recovery"
    decision_id = "legacy-approved-decision"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE recoveries (
                id TEXT PRIMARY KEY,
                scenario_id TEXT NOT NULL,
                execution_mode TEXT NOT NULL,
                status TEXT NOT NULL,
                current_step INTEGER NOT NULL,
                current_step_summary TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE approval_decisions (
                recovery_id TEXT PRIMARY KEY
                    REFERENCES recoveries(id) ON DELETE CASCADE,
                client_decision_id TEXT NOT NULL UNIQUE,
                remedy_id TEXT NOT NULL,
                remedy_digest TEXT NOT NULL,
                tool_call_id TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT,
                claimed_at TEXT NOT NULL,
                completed_at TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO recoveries VALUES (?, 'hotel', 'sdk_stub', 'completed', 5, "
            "'Legacy approval completed.', ?, ?)",
            (recovery_id, timestamp, timestamp),
        )
        connection.execute(
            """
            INSERT INTO approval_decisions VALUES (
                ?, ?, 'remedy-legacy', ?, 'tool-legacy',
                'legacy-fingerprint-without-action', 'completed', ?, ?, ?
            )
            """,
            (
                recovery_id,
                decision_id,
                APPROVED_DIGEST,
                json.dumps(
                    {
                        "clientDecisionId": decision_id,
                        "recoveryId": recovery_id,
                        "status": "completed",
                        "approvedRemedyDigest": APPROVED_DIGEST,
                        "executionStarted": True,
                    }
                ),
                timestamp,
                timestamp,
            ),
        )

    store = SQLiteStore(database_path)
    request = ApprovalDecisionRequest(
        action="approve",
        clientDecisionId=decision_id,
        remedyId="remedy-legacy",
        remedyDigest=APPROVED_DIGEST,
        toolCallId="tool-legacy",
    )

    replayed = store.claim_approval_decision(recovery_id, request)

    assert replayed.response is not None
    assert replayed.response.action is DecisionAction.APPROVE
    assert replayed.response.execution_started is True
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT action, request_fingerprint, result_json FROM approval_decisions"
        ).fetchone()
        assert row is not None
        assert row[0] == "approve"
        assert row[1] != "legacy-fingerprint-without-action"
        assert json.loads(row[2])["action"] == "approve"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    changed_action = request.model_copy(update={"action": DecisionAction.DECLINE})
    with pytest.raises(ApprovalDecisionError) as conflict:
        store.claim_approval_decision(recovery_id, changed_action)
    assert conflict.value.code == "decision_id_conflict"


def test_concurrent_action_migration_never_rewrites_a_new_decline(tmp_path) -> None:
    database_path = tmp_path / "task6-concurrent-action-migration.sqlite3"
    timestamp = "2026-07-18T20:00:00+00:00"
    recovery_id = "migration-race-recovery"
    with sqlite3.connect(database_path) as setup:
        setup.executescript(
            """
            CREATE TABLE recoveries (
                id TEXT PRIMARY KEY,
                scenario_id TEXT NOT NULL,
                execution_mode TEXT NOT NULL,
                status TEXT NOT NULL,
                current_step INTEGER NOT NULL,
                current_step_summary TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE approval_decisions (
                recovery_id TEXT PRIMARY KEY REFERENCES recoveries(id),
                client_decision_id TEXT NOT NULL UNIQUE,
                remedy_id TEXT NOT NULL,
                remedy_digest TEXT NOT NULL,
                tool_call_id TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT,
                claimed_at TEXT NOT NULL,
                completed_at TEXT
            );
            """
        )
        setup.execute(
            "INSERT INTO recoveries VALUES (?, 'hotel', 'sdk_stub', "
            "'pending_approval', 3, 'Pending.', ?, ?)",
            (recovery_id, timestamp, timestamp),
        )

    first = sqlite3.connect(database_path, timeout=10, check_same_thread=False)
    second = sqlite3.connect(database_path, timeout=10, check_same_thread=False)
    first.row_factory = second.row_factory = sqlite3.Row
    initial_checks = Barrier(2)
    allow_second_begin = Event()
    second_waiting = Event()
    errors: list[BaseException] = []
    first_check_seen = [False, False]

    def trace(index: int):
        def callback(statement: str) -> None:
            if "table_info(approval_decisions)" in statement and not first_check_seen[index]:
                first_check_seen[index] = True
                initial_checks.wait(timeout=5)
            if index == 1 and statement == "BEGIN IMMEDIATE":
                second_waiting.set()
                assert allow_second_begin.wait(timeout=5)

        return callback

    first.set_trace_callback(trace(0))
    second.set_trace_callback(trace(1))

    def migrate(connection: sqlite3.Connection) -> None:
        try:
            SQLiteStore._migrate_task6_approval_decisions(connection)
        except BaseException as error:
            errors.append(error)

    first_thread = Thread(target=migrate, args=(first,))
    second_thread = Thread(target=migrate, args=(second,))
    try:
        first_thread.start()
        second_thread.start()
        assert second_waiting.wait(timeout=5)
        first_thread.join(timeout=5)
        assert not first_thread.is_alive()
        with sqlite3.connect(database_path) as writer:
            writer.execute(
                """
                INSERT INTO approval_decisions (
                    recovery_id, client_decision_id, action, remedy_id,
                    remedy_digest, tool_call_id, request_fingerprint, status,
                    result_json, claimed_at, completed_at
                ) VALUES (?, 'decline-race', 'decline', 'remedy-race', ?,
                    'tool-race', 'decline-fingerprint', 'claimed', NULL, ?, NULL)
                """,
                (recovery_id, APPROVED_DIGEST, timestamp),
            )
    finally:
        allow_second_begin.set()
        first_thread.join(timeout=5)
        second_thread.join(timeout=5)
        first.close()
        second.close()

    assert not second_thread.is_alive()
    assert errors == []
    with sqlite3.connect(database_path) as verifier:
        assert verifier.execute(
            "SELECT action FROM approval_decisions WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone() == ("decline",)
