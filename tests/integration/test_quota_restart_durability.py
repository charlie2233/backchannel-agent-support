import asyncio
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from server.config import RuntimeSettings
from server.main import create_app
from server.models import (
    QUOTA_SDK_UNKNOWN_AUTHORIZATION_SOURCE,
    QUOTA_SDK_UNKNOWN_PROVIDER_RESULT,
    QUOTA_SDK_UNKNOWN_VERIFICATION_RESULTS,
    ExecutionMode,
    RecoveryStatus,
    ScenarioId,
)
from server.orchestrator import RecoveryOrchestrator
from server.providers.hotel_simulator import HotelSimulator
from server.providers.quota_simulator import (
    DETERMINISTIC_QUOTA_RESULT,
    QuotaSimulator,
)
from server.replay.loader import ScenarioLoader
from server.store import (
    RECOVERY_CREATION_STALE_AFTER,
    ExecutionConflictError,
    PublicEvidenceIntegrityError,
    RecoveryNotFoundError,
    SQLiteStore,
)

EXPECTED_QUOTA_EVENTS = [
    "recovery.created",
    "quota.pressure_detected",
    "quota.ceiling_proven",
    "quota.burst_selected",
    "quota.delegated_authority_confirmed",
    "quota.burst_executed",
    "quota.receipt_sealed",
]


class SimulatedProcessCrash(RuntimeError):
    pass


def _reserve_started_quota_creation(
    store: SQLiteStore,
    recovery_id: str,
) -> tuple[str, str, str]:
    request_key = "a" * 64
    session_key = "b" * 64
    ip_key = "c" * 64
    request_fingerprint = hashlib.sha256(
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
    now = datetime.now(UTC)
    claim = store.claim_recovery_creation(
        request_key=request_key,
        request_fingerprint=request_fingerprint,
        scenario_id=ScenarioId.API_QUOTA,
        execution_mode=ExecutionMode.SDK_STUB,
        reserved_recovery_id=recovery_id,
        session_key=session_key,
        ip_key=ip_key,
        expires_at=now + timedelta(days=1),
        now=now,
    )
    assert claim.disposition == "owner"
    store.mark_recovery_creation_started(
        request_key=request_key,
        request_fingerprint=request_fingerprint,
        now=now,
    )
    return request_key, request_fingerprint, session_key


def _replay_scenarios():
    return {scenario.id: scenario for scenario in ScenarioLoader().list()}


def _quota_database_state(database_path, recovery_id: str):
    with sqlite3.connect(database_path) as connection:
        return {
            "recovery": connection.execute(
                "SELECT * FROM recoveries WHERE id = ?",
                (recovery_id,),
            ).fetchall(),
            "events": connection.execute(
                "SELECT * FROM events WHERE recovery_id = ? ORDER BY seq",
                (recovery_id,),
            ).fetchall(),
            "receipt": connection.execute(
                "SELECT * FROM receipts WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchall(),
            "executions": connection.execute(
                "SELECT * FROM executions WHERE recovery_id = ? ORDER BY id",
                (recovery_id,),
            ).fetchall(),
            "creation": connection.execute(
                "SELECT * FROM recovery_creations WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchall(),
            "pending": connection.execute(
                "SELECT * FROM pending_approvals WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchall(),
            "decisions": connection.execute(
                "SELECT * FROM approval_decisions WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchall(),
            "remedies": connection.execute(
                "SELECT * FROM remedies WHERE recovery_id = ? ORDER BY id",
                (recovery_id,),
            ).fetchall(),
        }


def test_quota_recovery_and_dispatch_claim_commit_atomically(
    tmp_path,
) -> None:
    database_path = tmp_path / "quota-claim-atomic.sqlite3"
    store = SQLiteStore(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_quota_claim
            BEFORE INSERT ON executions
            WHEN NEW.tool_call_id = 'recover-api-quota-demo'
            BEGIN
                SELECT RAISE(ABORT, 'injected quota claim failure');
            END
            """
        )
    provider = QuotaSimulator()
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(),
        quota_provider=provider,
    )

    with pytest.raises(
        sqlite3.IntegrityError,
        match="injected quota claim failure",
    ):
        asyncio.run(
            orchestrator.start(
                ScenarioId.API_QUOTA,
                execution_mode=ExecutionMode.SDK_STUB,
                recovery_id=str(uuid4()),
            )
        )

    assert provider.execution_count == 0
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM recoveries").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM recovery_access").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM executions").fetchone() == (0,)


def test_restart_finalizes_durable_quota_result_without_redispatch(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "quota-result-restart.sqlite3"
    recovery_id = str(uuid4())
    store = SQLiteStore(database_path)
    provider = QuotaSimulator()
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(),
        quota_provider=provider,
    )

    def crash_before_finalization(*_args, **_kwargs) -> None:
        raise SimulatedProcessCrash("crash after durable quota result")

    monkeypatch.setattr(
        store,
        "finalize_completed_quota_execution",
        crash_before_finalization,
    )

    with pytest.raises(
        SimulatedProcessCrash,
        match="crash after durable quota result",
    ):
        asyncio.run(
            orchestrator.start(
                ScenarioId.API_QUOTA,
                execution_mode=ExecutionMode.SDK_STUB,
                recovery_id=recovery_id,
            )
        )

    assert provider.execution_count == 1
    assert store.count_executions(recovery_id) == 1
    assert [event.type for event in store.list_events(recovery_id)] == ["recovery.created"]
    with pytest.raises(RecoveryNotFoundError, match="Receipt not found"):
        store.get_receipt(recovery_id)
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT status, provider_execution, result_json
            FROM executions
            WHERE recovery_id = ?
            """,
            (recovery_id,),
        ).fetchone()
    assert row is not None
    assert row[0:2] == ("completed", 1)
    assert row[2] is not None
    store.close()

    fresh_store = SQLiteStore(database_path)
    fresh_provider = QuotaSimulator()
    RecoveryOrchestrator(
        store=fresh_store,
        hotel_provider=HotelSimulator(),
        quota_provider=fresh_provider,
    )

    assert fresh_provider.execution_count == 0
    assert fresh_store.get_recovery(recovery_id).status is RecoveryStatus.COMPLETED
    assert [event.type for event in fresh_store.list_events(recovery_id)] == EXPECTED_QUOTA_EVENTS
    assert sum(event.terminal for event in fresh_store.list_events(recovery_id)) == 1
    receipt = fresh_store.get_receipt(recovery_id)
    assert receipt.status == "completed"
    assert receipt.provider_execution is True
    assert receipt.approval_count == 0

    fresh_store.close()
    second_store = SQLiteStore(database_path)
    second_provider = QuotaSimulator()
    RecoveryOrchestrator(
        store=second_store,
        hotel_provider=HotelSimulator(),
        quota_provider=second_provider,
    )
    assert second_provider.execution_count == 0
    assert [event.type for event in second_store.list_events(recovery_id)] == EXPECTED_QUOTA_EVENTS
    assert sum(event.terminal for event in second_store.list_events(recovery_id)) == 1


def test_original_quota_runtime_accepts_concurrent_validated_result_finalization(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "quota-result-concurrent-finalization.sqlite3"
    recovery_id = str(uuid4())
    store = SQLiteStore(database_path)
    provider = QuotaSimulator()
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(),
        quota_provider=provider,
    )
    validation_persisted = Event()
    release_original = Event()
    finalize_result = store.finalize_completed_quota_execution

    def pause_then_finalize(*args, **kwargs):
        validation_persisted.set()
        assert release_original.wait(timeout=5)
        return finalize_result(*args, **kwargs)

    monkeypatch.setattr(
        store,
        "finalize_completed_quota_execution",
        pause_then_finalize,
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        original = executor.submit(
            lambda: asyncio.run(
                orchestrator.start(
                    ScenarioId.API_QUOTA,
                    execution_mode=ExecutionMode.SDK_STUB,
                    recovery_id=recovery_id,
                )
            )
        )
        assert validation_persisted.wait(timeout=5)

        fresh_store = SQLiteStore(database_path)
        fresh_provider = QuotaSimulator()
        RecoveryOrchestrator(
            store=fresh_store,
            hotel_provider=HotelSimulator(),
            quota_provider=fresh_provider,
        )
        assert fresh_store.get_recovery(recovery_id).status is RecoveryStatus.COMPLETED
        release_original.set()
        original_result = original.result(timeout=5)

    assert provider.execution_count == 1
    assert fresh_provider.execution_count == 0
    assert original_result.recovery.status is RecoveryStatus.COMPLETED
    terminal_execution = store.get_quota_execution(recovery_id)
    assert terminal_execution is not None
    assert terminal_execution.status == "completed"
    assert [event.type for event in store.list_events(recovery_id)] == EXPECTED_QUOTA_EVENTS
    assert sum(event.terminal for event in store.list_events(recovery_id)) == 1


def test_stale_restart_quarantine_cannot_retract_concurrent_validated_completion(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "quota-stale-quarantine.sqlite3"
    recovery_id = str(uuid4())
    store = SQLiteStore(database_path)
    _request_key, _request_fingerprint, session_key = _reserve_started_quota_creation(
        store,
        recovery_id,
    )
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(),
        quota_provider=QuotaSimulator(),
    )

    async def crash_before_sdk_dispatch(*_args, **_kwargs):
        raise SimulatedProcessCrash("crash after durable quota claim")

    monkeypatch.setattr("server.orchestrator.Runner.run", crash_before_sdk_dispatch)
    with pytest.raises(SimulatedProcessCrash):
        asyncio.run(
            orchestrator.start(
                ScenarioId.API_QUOTA,
                execution_mode=ExecutionMode.SDK_STUB,
                recovery_id=recovery_id,
                session_key=session_key,
            )
        )
    pending = store.get_quota_execution(recovery_id)
    assert pending is not None
    recorded, changed = store.record_completed_quota_execution(
        pending,
        result=DETERMINISTIC_QUOTA_RESULT,
    )
    assert changed is True
    assert recorded.status == "result_recorded"

    fresh_store = SQLiteStore(database_path)
    stale_snapshot_observed = Event()
    release_stale_quarantine = Event()
    quarantine = fresh_store.quarantine_quota_execution_invariant_failure

    def pause_stale_quarantine(execution):
        assert execution.status == "result_recorded"
        stale_snapshot_observed.set()
        assert release_stale_quarantine.wait(timeout=5)
        return quarantine(execution)

    monkeypatch.setattr(
        fresh_store,
        "quarantine_quota_execution_invariant_failure",
        pause_stale_quarantine,
    )
    fresh_provider = QuotaSimulator()
    with ThreadPoolExecutor(max_workers=1) as executor:
        restarted = executor.submit(
            lambda: RecoveryOrchestrator(
                store=fresh_store,
                hotel_provider=HotelSimulator(),
                quota_provider=fresh_provider,
            )
        )
        assert stale_snapshot_observed.wait(timeout=5)
        validated = store.validate_quota_sdk_completion(recorded)
        assert validated.status == "completed"
        assert store.finalize_completed_quota_execution(validated) is True
        release_stale_quarantine.set()
        restarted.result(timeout=5)

    assert fresh_provider.execution_count == 0
    execution = fresh_store.get_quota_execution(recovery_id)
    assert execution is not None
    assert execution.status == "completed"
    assert fresh_store.get_recovery(recovery_id).status is RecoveryStatus.COMPLETED
    assert [event.type for event in fresh_store.list_events(recovery_id)] == EXPECTED_QUOTA_EVENTS
    assert sum(event.terminal for event in fresh_store.list_events(recovery_id)) == 1
    assert fresh_store.get_receipt(recovery_id).status == "completed"
    fresh_store.close()
    store.close()


def test_concurrent_restarts_seal_claimed_quota_dispatch_unknown_once(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "quota-claim-restart.sqlite3"
    recovery_id = str(uuid4())
    store = SQLiteStore(database_path)
    provider = QuotaSimulator()
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(),
        quota_provider=provider,
    )

    async def crash_before_sdk_dispatch(*_args, **_kwargs):
        raise SimulatedProcessCrash("crash after durable quota claim")

    monkeypatch.setattr("server.orchestrator.Runner.run", crash_before_sdk_dispatch)

    with pytest.raises(
        SimulatedProcessCrash,
        match="crash after durable quota claim",
    ):
        asyncio.run(
            orchestrator.start(
                ScenarioId.API_QUOTA,
                execution_mode=ExecutionMode.SDK_STUB,
                recovery_id=recovery_id,
            )
        )

    assert provider.execution_count == 0
    assert store.count_executions(recovery_id) == 1
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            """
            SELECT quota_execution_contract
            FROM recoveries
            WHERE id = ?
            """,
            (recovery_id,),
        ).fetchone() == (1,)
        assert connection.execute(
            """
            SELECT status, provider_execution, result_json
            FROM executions
            WHERE recovery_id = ?
            """,
            (recovery_id,),
        ).fetchone() == ("pending", 0, None)
    store.close()

    def reconcile_in_fresh_runtime() -> int:
        fresh_store = SQLiteStore(database_path)
        fresh_provider = QuotaSimulator()
        RecoveryOrchestrator(
            store=fresh_store,
            hotel_provider=HotelSimulator(),
            quota_provider=fresh_provider,
        )
        fresh_store.close()
        return fresh_provider.execution_count

    with ThreadPoolExecutor(max_workers=2) as executor:
        provider_counts = list(executor.map(lambda _index: reconcile_in_fresh_runtime(), range(2)))

    assert provider_counts == [0, 0]
    verified = SQLiteStore(database_path)
    snapshot = verified.get_recovery(recovery_id)
    assert snapshot.status is RecoveryStatus.OUTCOME_UNKNOWN
    assert snapshot.current_step == 5
    events = verified.list_events(recovery_id)
    assert [event.type for event in events] == [
        "recovery.created",
        "quota.outcome_unknown",
    ]
    assert sum(event.terminal for event in events) == 1
    assert events[-1].data["providerExecution"] is None
    assert events[-1].data["redispatched"] is False

    receipt = verified.get_receipt(recovery_id)
    assert receipt.status == "outcome_unknown"
    assert receipt.provider_execution is None
    assert receipt.approval_count == 0
    assert receipt.approved_remedy_digest is None
    assert receipt.provider_result == QUOTA_SDK_UNKNOWN_PROVIDER_RESULT
    assert receipt.authorization_source == QUOTA_SDK_UNKNOWN_AUTHORIZATION_SOURCE
    assert receipt.verification_results == list(QUOTA_SDK_UNKNOWN_VERIFICATION_RESULTS)
    assert verified.count_executions(recovery_id) == 1
    assert verified.count_decisions(recovery_id) == 0
    terminal_execution = verified.get_quota_execution(recovery_id)
    assert terminal_execution is not None
    assert terminal_execution.status == "outcome_unknown"
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            """
            SELECT status, provider_execution, result_json
            FROM executions
            WHERE recovery_id = ?
            """,
            (recovery_id,),
        ).fetchone() == ("outcome_unknown", 0, None)
        assert connection.execute(
            "SELECT COUNT(*) FROM pending_approvals WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone() == (0,)

    verified.close()
    replay_store = SQLiteStore(database_path)
    replay_provider = QuotaSimulator()
    RecoveryOrchestrator(
        store=replay_store,
        hotel_provider=HotelSimulator(),
        quota_provider=replay_provider,
    )
    assert replay_provider.execution_count == 0
    assert len(replay_store.list_events(recovery_id)) == 2
    assert replay_store.get_receipt(recovery_id) == receipt


def test_quota_reconciliation_preflight_uses_one_database_snapshot(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "quota-reconciliation-snapshot.sqlite3"
    recovery_id = str(uuid4())
    initial_store = SQLiteStore(database_path)
    orchestrator = RecoveryOrchestrator(
        store=initial_store,
        hotel_provider=HotelSimulator(),
        quota_provider=QuotaSimulator(),
    )

    async def crash_before_sdk_dispatch(*_args, **_kwargs):
        raise SimulatedProcessCrash("crash after durable quota claim")

    monkeypatch.setattr("server.orchestrator.Runner.run", crash_before_sdk_dispatch)
    with pytest.raises(SimulatedProcessCrash):
        asyncio.run(
            orchestrator.start(
                ScenarioId.API_QUOTA,
                execution_mode=ExecutionMode.SDK_STUB,
                recovery_id=recovery_id,
            )
        )
    initial_store.close()
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)

    fast_store = SQLiteStore(database_path)
    slow_store = SQLiteStore(database_path)
    pending = fast_store.get_quota_execution(recovery_id)
    assert pending is not None
    recovery_scanned = Event()
    release_scan = Event()
    original_connect = slow_store._connect

    class PausingConnection:
        def __init__(self, connection):
            self._connection = connection

        def __enter__(self):
            self._connection.__enter__()
            return self

        def __exit__(self, *args):
            return self._connection.__exit__(*args)

        def execute(self, sql, parameters=()):
            cursor = self._connection.execute(sql, parameters)
            normalized = " ".join(sql.split())
            if (
                "SELECT recoveries.* FROM recoveries" in normalized
                and "recoveries.scenario_id = 'api-quota'" in normalized
                and "ORDER BY recoveries.created_at" in normalized
            ):
                recovery_scanned.set()
                assert release_scan.wait(timeout=5)
            return cursor

        def __getattr__(self, name):
            return getattr(self._connection, name)

    monkeypatch.setattr(
        slow_store,
        "_connect",
        lambda: PausingConnection(original_connect()),
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        slow_preflight = executor.submit(slow_store.list_quota_executions_needing_reconciliation)
        assert recovery_scanned.wait(timeout=5)
        assert fast_store.finalize_pending_quota_execution_unknown(pending)
        release_scan.set()
        stale_snapshot_claims = slow_preflight.result(timeout=5)

    assert len(stale_snapshot_claims) == 1
    assert stale_snapshot_claims[0].status == "pending"
    assert not slow_store.finalize_pending_quota_execution_unknown(stale_snapshot_claims[0])
    assert fast_store.get_recovery(recovery_id).status is RecoveryStatus.OUTCOME_UNKNOWN
    assert [event.type for event in fast_store.list_events(recovery_id)] == [
        "recovery.created",
        "quota.outcome_unknown",
    ]


def test_owner_retry_recovers_unknown_quota_creation_without_redispatch(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "quota-owner-retry.sqlite3"
    settings = RuntimeSettings(
        live_ready=False,
        identity_hash_secret="quota-restart-test-secret-that-is-long-enough",
    )
    payload = {
        "scenarioId": "api-quota",
        "executionMode": "sdk_stub",
        "clientRequestId": "quota-restart-owner-001",
    }
    first_store = SQLiteStore(database_path)
    first_provider = QuotaSimulator()
    first_orchestrator = RecoveryOrchestrator(
        store=first_store,
        hotel_provider=HotelSimulator(),
        quota_provider=first_provider,
    )

    async def crash_before_sdk_dispatch(*_args, **_kwargs):
        raise SimulatedProcessCrash("crash after durable quota claim")

    monkeypatch.setattr("server.orchestrator.Runner.run", crash_before_sdk_dispatch)
    with TestClient(
        create_app(
            settings,
            store=first_store,
            orchestrator=first_orchestrator,
        )
    ) as first_client:
        assert first_client.get("/health").status_code == 200
        session_cookie = first_client.cookies.get(settings.demo_session_cookie_name)
        assert isinstance(session_cookie, str)
        failed = first_client.post("/api/recoveries", json=payload)

    assert failed.status_code == 409
    assert failed.json()["code"] == "creation_outcome_unknown"
    assert first_provider.execution_count == 0
    with sqlite3.connect(database_path) as connection:
        recovery_id = connection.execute("SELECT recovery_id FROM recovery_creations").fetchone()[0]
        assert connection.execute("SELECT status FROM recovery_creations").fetchone() == (
            "unknown",
        )
    first_store.close()

    restarted_store = SQLiteStore(database_path)
    restarted_provider = QuotaSimulator()
    restarted_app = create_app(
        settings,
        store=restarted_store,
        quota_provider=restarted_provider,
    )
    with TestClient(restarted_app) as restarted_client:
        restarted_client.cookies.set(
            settings.demo_session_cookie_name,
            session_cookie,
        )
        retried = restarted_client.post("/api/recoveries", json=payload)
        snapshot = restarted_client.get(f"/api/recoveries/{recovery_id}")
        receipt = restarted_client.get(f"/api/recoveries/{recovery_id}/receipt")
        events = restarted_client.get(f"/api/recoveries/{recovery_id}/events")

        restarted_client.cookies.clear()
        assert restarted_client.get("/health").status_code == 200
        foreign_snapshot = restarted_client.get(f"/api/recoveries/{recovery_id}")

    assert restarted_provider.execution_count == 0
    assert retried.status_code == 201
    assert retried.json()["recoveryId"] == recovery_id
    assert retried.json()["status"] == "outcome_unknown"
    assert snapshot.status_code == 200
    assert snapshot.json() == retried.json()
    assert receipt.status_code == 200
    assert receipt.json()["providerExecution"] is None
    assert receipt.json()["approvalCount"] == 0
    assert events.status_code == 200
    assert '"type":"quota.outcome_unknown"' in events.text
    assert foreign_snapshot.status_code == 404
    assert foreign_snapshot.json() == {"detail": "Not found"}
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT status FROM recovery_creations").fetchone() == ("ready",)
        assert connection.execute("SELECT COUNT(*) FROM receipts").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM events WHERE terminal = 1").fetchone() == (
            1,
        )


def test_unknown_quota_reconciliation_rolls_back_as_one_transaction(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "quota-unknown-rollback.sqlite3"
    recovery_id = str(uuid4())
    store = SQLiteStore(database_path)
    _request_key, _request_fingerprint, session_key = _reserve_started_quota_creation(
        store, recovery_id
    )
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(),
        quota_provider=QuotaSimulator(),
    )

    async def crash_before_sdk_dispatch(*_args, **_kwargs):
        raise SimulatedProcessCrash("crash after durable quota claim")

    monkeypatch.setattr("server.orchestrator.Runner.run", crash_before_sdk_dispatch)
    with pytest.raises(SimulatedProcessCrash):
        asyncio.run(
            orchestrator.start(
                ScenarioId.API_QUOTA,
                execution_mode=ExecutionMode.SDK_STUB,
                recovery_id=recovery_id,
                session_key=session_key,
            )
        )
    store.close()

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_quota_receipt
            BEFORE INSERT ON receipts
            BEGIN
                SELECT RAISE(ABORT, 'injected quota receipt failure');
            END
            """
        )

    failed_store = SQLiteStore(database_path)
    with pytest.raises(
        sqlite3.IntegrityError,
        match="injected quota receipt failure",
    ):
        RecoveryOrchestrator(
            store=failed_store,
            hotel_provider=HotelSimulator(),
            quota_provider=QuotaSimulator(),
        )
    failed_store.close()

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT status FROM executions WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone() == ("pending",)
        assert connection.execute(
            "SELECT status, current_step FROM recoveries WHERE id = ?",
            (recovery_id,),
        ).fetchone() == ("in_progress", 0)
        assert connection.execute(
            "SELECT type FROM events WHERE recovery_id = ? ORDER BY seq",
            (recovery_id,),
        ).fetchall() == [("recovery.created",)]
        assert connection.execute(
            "SELECT COUNT(*) FROM receipts WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT status FROM recovery_creations WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone() == ("started",)
        connection.execute("DROP TRIGGER fail_quota_receipt")

    recovered_store = SQLiteStore(database_path)
    recovered_provider = QuotaSimulator()
    RecoveryOrchestrator(
        store=recovered_store,
        hotel_provider=HotelSimulator(),
        quota_provider=recovered_provider,
    )
    assert recovered_provider.execution_count == 0
    assert recovered_store.get_recovery(recovery_id).status is RecoveryStatus.OUTCOME_UNKNOWN
    assert len(recovered_store.list_events(recovery_id)) == 2
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT status FROM recovery_creations WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone() == ("ready",)


def test_completed_quota_reconciliation_rolls_back_as_one_transaction(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "quota-completed-rollback.sqlite3"
    recovery_id = str(uuid4())
    store = SQLiteStore(database_path)
    _request_key, _request_fingerprint, session_key = _reserve_started_quota_creation(
        store, recovery_id
    )
    provider = QuotaSimulator()
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(),
        quota_provider=provider,
    )

    def crash_before_finalization(*_args, **_kwargs) -> None:
        raise SimulatedProcessCrash("crash after durable quota result")

    monkeypatch.setattr(
        store,
        "finalize_completed_quota_execution",
        crash_before_finalization,
    )
    with pytest.raises(SimulatedProcessCrash):
        asyncio.run(
            orchestrator.start(
                ScenarioId.API_QUOTA,
                execution_mode=ExecutionMode.SDK_STUB,
                recovery_id=recovery_id,
                session_key=session_key,
            )
        )
    assert provider.execution_count == 1
    store.close()

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_completed_quota_receipt
            BEFORE INSERT ON receipts
            BEGIN
                SELECT RAISE(ABORT, 'injected completed receipt failure');
            END
            """
        )

    failed_store = SQLiteStore(database_path)
    with pytest.raises(
        sqlite3.IntegrityError,
        match="injected completed receipt failure",
    ):
        RecoveryOrchestrator(
            store=failed_store,
            hotel_provider=HotelSimulator(),
            quota_provider=QuotaSimulator(),
        )
    failed_store.close()

    with sqlite3.connect(database_path) as connection:
        execution = connection.execute(
            """
            SELECT status, provider_execution, result_json
            FROM executions
            WHERE recovery_id = ?
            """,
            (recovery_id,),
        ).fetchone()
        assert execution is not None
        assert execution[0:2] == ("completed", 1)
        assert execution[2] is not None
        assert connection.execute(
            "SELECT status, current_step FROM recoveries WHERE id = ?",
            (recovery_id,),
        ).fetchone() == ("in_progress", 0)
        assert connection.execute(
            "SELECT type FROM events WHERE recovery_id = ? ORDER BY seq",
            (recovery_id,),
        ).fetchall() == [("recovery.created",)]
        assert connection.execute(
            "SELECT COUNT(*) FROM receipts WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT status FROM recovery_creations WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone() == ("started",)
        connection.execute("DROP TRIGGER fail_completed_quota_receipt")

    recovered_store = SQLiteStore(database_path)
    recovered_provider = QuotaSimulator()
    RecoveryOrchestrator(
        store=recovered_store,
        hotel_provider=HotelSimulator(),
        quota_provider=recovered_provider,
    )
    assert recovered_provider.execution_count == 0
    assert recovered_store.get_recovery(recovery_id).status is RecoveryStatus.COMPLETED
    assert [
        event.type for event in recovered_store.list_events(recovery_id)
    ] == EXPECTED_QUOTA_EVENTS
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT status FROM recovery_creations WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone() == ("ready",)


def test_handled_error_before_atomic_quota_claim_stays_creation_unknown(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "quota-before-atomic-claim.sqlite3"
    settings = RuntimeSettings(
        live_ready=False,
        identity_hash_secret="quota-outer-window-secret-that-is-long-enough",
    )
    store = SQLiteStore(database_path)
    provider = QuotaSimulator()
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(),
        quota_provider=provider,
    )

    async def fail_before_recovery_claim(*_args, **_kwargs):
        raise SimulatedProcessCrash("loss before atomic quota claim")

    monkeypatch.setattr(
        orchestrator,
        "start",
        fail_before_recovery_claim,
    )
    payload = {
        "scenarioId": "api-quota",
        "executionMode": "sdk_stub",
        "clientRequestId": "quota-before-claim-001",
    }
    with TestClient(
        create_app(
            settings,
            store=store,
            orchestrator=orchestrator,
        )
    ) as client:
        assert client.get("/health").status_code == 200
        first = client.post("/api/recoveries", json=payload)
        retry = client.post("/api/recoveries", json=payload)

    assert first.status_code == 409
    assert first.json()["code"] == "creation_outcome_unknown"
    assert retry.status_code == 409
    assert retry.json()["code"] == "creation_outcome_unknown"
    assert provider.execution_count == 0
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM recoveries").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM executions").fetchone() == (0,)
        assert connection.execute("SELECT status FROM recovery_creations").fetchone() == (
            "unknown",
        )


def test_process_loss_before_atomic_quota_claim_stays_pending_until_stale(
    tmp_path,
) -> None:
    database_path = tmp_path / "quota-hard-loss-before-atomic-claim.sqlite3"
    recovery_id = str(uuid4())
    started_at = datetime(2026, 7, 24, tzinfo=UTC)
    store = SQLiteStore(database_path)
    request_key, request_fingerprint, session_key = _reserve_started_quota_creation(
        store,
        recovery_id,
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            UPDATE recovery_creations
            SET created_at = ?, updated_at = ?, expires_at = ?
            WHERE recovery_id = ?
            """,
            (
                started_at.isoformat(),
                started_at.isoformat(),
                (started_at + timedelta(days=1)).isoformat(),
                recovery_id,
            ),
        )
    store.close()

    fresh_store = SQLiteStore(database_path)
    immediate = fresh_store.claim_recovery_creation(
        request_key=request_key,
        request_fingerprint=request_fingerprint,
        scenario_id=ScenarioId.API_QUOTA,
        execution_mode=ExecutionMode.SDK_STUB,
        reserved_recovery_id=str(uuid4()),
        session_key=session_key,
        ip_key="c" * 64,
        expires_at=started_at + timedelta(days=1),
        now=started_at + RECOVERY_CREATION_STALE_AFTER - timedelta(microseconds=1),
    )
    stale = fresh_store.claim_recovery_creation(
        request_key=request_key,
        request_fingerprint=request_fingerprint,
        scenario_id=ScenarioId.API_QUOTA,
        execution_mode=ExecutionMode.SDK_STUB,
        reserved_recovery_id=str(uuid4()),
        session_key=session_key,
        ip_key="c" * 64,
        expires_at=started_at + timedelta(days=1),
        now=started_at + RECOVERY_CREATION_STALE_AFTER,
    )
    retry = fresh_store.claim_recovery_creation(
        request_key=request_key,
        request_fingerprint=request_fingerprint,
        scenario_id=ScenarioId.API_QUOTA,
        execution_mode=ExecutionMode.SDK_STUB,
        reserved_recovery_id=str(uuid4()),
        session_key=session_key,
        ip_key="c" * 64,
        expires_at=started_at + timedelta(days=1),
        now=started_at + RECOVERY_CREATION_STALE_AFTER + timedelta(seconds=1),
    )

    assert immediate.disposition == "pending"
    assert immediate.recovery_id == recovery_id
    assert stale.disposition == "unknown"
    assert stale.recovery_id == recovery_id
    assert retry.disposition == "unknown"
    assert retry.recovery_id == recovery_id
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM recoveries").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM executions").fetchone() == (0,)
        assert connection.execute(
            "SELECT status FROM recovery_creations WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone() == ("unknown",)


def test_startup_rejects_legacy_in_progress_quota_without_execution(
    tmp_path,
) -> None:
    database_path = tmp_path / "legacy-in-progress-quota.sqlite3"
    recovery_id = str(uuid4())
    store = SQLiteStore(database_path)
    store.create_recovery(
        recovery_id=recovery_id,
        scenario_id=ScenarioId.API_QUOTA,
        execution_mode=ExecutionMode.SDK_STUB,
        current_step=0,
        current_step_summary="Legacy quota recovery started.",
        root_trace_id=f"qa_trace_{'1' * 32}",
        sdk_version="legacy-sdk",
        protocol_version="legacy-protocol",
        agent_graph_version="legacy-quota-agent",
        definition_digest="d" * 64,
    )
    for event_type, step, summary, data in SQLiteStore._quota_completed_transition_specs(
        DETERMINISTIC_QUOTA_RESULT
    )[:-1]:
        store.record_transition(
            recovery_id,
            status=RecoveryStatus.IN_PROGRESS,
            current_step=step,
            current_step_summary=summary,
            event_type=event_type,
            event_data=data,
        )
    store.close()
    with sqlite3.connect(database_path) as connection:
        before = (
            connection.execute(
                "SELECT status, current_step FROM recoveries WHERE id = ?",
                (recovery_id,),
            ).fetchone(),
            connection.execute(
                "SELECT seq, type, terminal, data_json FROM events "
                "WHERE recovery_id = ? ORDER BY seq",
                (recovery_id,),
            ).fetchall(),
            connection.execute(
                "SELECT COUNT(*) FROM receipts WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone(),
            connection.execute(
                "SELECT COUNT(*) FROM executions WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone(),
        )

    fresh_store = SQLiteStore(database_path)
    provider = QuotaSimulator()
    with pytest.raises(
        ExecutionConflictError,
        match="no exact durable execution",
    ):
        RecoveryOrchestrator(
            store=fresh_store,
            hotel_provider=HotelSimulator(),
            quota_provider=provider,
        )
    assert provider.execution_count == 0
    fresh_store.close()
    with sqlite3.connect(database_path) as connection:
        after = (
            connection.execute(
                "SELECT status, current_step FROM recoveries WHERE id = ?",
                (recovery_id,),
            ).fetchone(),
            connection.execute(
                "SELECT seq, type, terminal, data_json FROM events "
                "WHERE recovery_id = ? ORDER BY seq",
                (recovery_id,),
            ).fetchall(),
            connection.execute(
                "SELECT COUNT(*) FROM receipts WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone(),
            connection.execute(
                "SELECT COUNT(*) FROM executions WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone(),
        )
    assert after == before


def test_startup_rejects_in_progress_quota_with_terminal_execution_state(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "in-progress-terminal-execution.sqlite3"
    recovery_id = str(uuid4())
    store = SQLiteStore(database_path)
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(),
        quota_provider=QuotaSimulator(),
    )

    async def crash_before_sdk_dispatch(*_args, **_kwargs):
        raise SimulatedProcessCrash("crash after durable quota claim")

    monkeypatch.setattr("server.orchestrator.Runner.run", crash_before_sdk_dispatch)
    with pytest.raises(SimulatedProcessCrash):
        asyncio.run(
            orchestrator.start(
                ScenarioId.API_QUOTA,
                execution_mode=ExecutionMode.SDK_STUB,
                recovery_id=recovery_id,
            )
        )
    store.close()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            UPDATE executions
            SET status = 'outcome_unknown'
            WHERE recovery_id = ?
            """,
            (recovery_id,),
        )

    fresh_store = SQLiteStore(database_path)
    provider = QuotaSimulator()
    with pytest.raises(
        ExecutionConflictError,
        match="no reconcilable durable state",
    ):
        RecoveryOrchestrator(
            store=fresh_store,
            hotel_provider=HotelSimulator(),
            quota_provider=provider,
        )
    assert provider.execution_count == 0
    assert fresh_store.get_recovery(recovery_id).status is RecoveryStatus.IN_PROGRESS
    assert [event.type for event in fresh_store.list_events(recovery_id)] == ["recovery.created"]
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT status FROM executions WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone() == ("outcome_unknown",)
        assert connection.execute(
            "SELECT COUNT(*) FROM receipts WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    ("name", "mutation"),
    [
        (
            "creation fingerprint",
            """
            UPDATE recovery_creations
            SET request_fingerprint = lower(hex(randomblob(32)))
            WHERE recovery_id = ?
            """,
        ),
        (
            "creation chronology",
            """
            UPDATE recovery_creations
            SET created_at = '2099-01-01T00:00:00+00:00',
                updated_at = '2099-01-01T00:00:00+00:00',
                expires_at = '2099-01-02T00:00:00+00:00'
            WHERE recovery_id = ?
            """,
        ),
    ],
)
def test_startup_rejects_tampered_quota_creation_binding(
    tmp_path,
    monkeypatch,
    name,
    mutation,
) -> None:
    database_path = tmp_path / f"tampered-{name.replace(' ', '-')}.sqlite3"
    recovery_id = str(uuid4())
    store = SQLiteStore(database_path)
    _request_key, _request_fingerprint, session_key = _reserve_started_quota_creation(
        store, recovery_id
    )
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(),
        quota_provider=QuotaSimulator(),
    )

    async def crash_before_sdk_dispatch(*_args, **_kwargs):
        raise SimulatedProcessCrash("crash after durable quota claim")

    monkeypatch.setattr("server.orchestrator.Runner.run", crash_before_sdk_dispatch)
    with pytest.raises(SimulatedProcessCrash):
        asyncio.run(
            orchestrator.start(
                ScenarioId.API_QUOTA,
                execution_mode=ExecutionMode.SDK_STUB,
                recovery_id=recovery_id,
                session_key=session_key,
            )
        )
    store.close()
    with sqlite3.connect(database_path) as connection:
        connection.execute(mutation, (recovery_id,))

    fresh_store = SQLiteStore(database_path)
    provider = QuotaSimulator()
    with pytest.raises(
        ExecutionConflictError,
        match="creation",
    ):
        RecoveryOrchestrator(
            store=fresh_store,
            hotel_provider=HotelSimulator(),
            quota_provider=provider,
        )
    assert provider.execution_count == 0
    assert fresh_store.get_recovery(recovery_id).status is RecoveryStatus.IN_PROGRESS
    assert [event.type for event in fresh_store.list_events(recovery_id)] == ["recovery.created"]
    with pytest.raises(RecoveryNotFoundError, match="Receipt not found"):
        fresh_store.get_receipt(recovery_id)


@pytest.mark.parametrize(
    ("name", "mutations"),
    [
        (
            "step",
            (
                """
                UPDATE recoveries
                SET current_step = 4
                WHERE id = ?
                """,
            ),
        ),
        (
            "summary",
            (
                """
                UPDATE recoveries
                SET current_step_summary = 'tampered snapshot'
                WHERE id = ?
                """,
            ),
        ),
        (
            "updated timestamp",
            (
                """
                UPDATE recoveries
                SET updated_at = '2099-01-01T00:00:00+00:00'
                WHERE id = ?
                """,
            ),
        ),
        (
            "future atomic timestamps",
            (
                """
                UPDATE recoveries
                SET created_at = '2099-01-01T00:00:00+00:00',
                    updated_at = '2099-01-01T00:00:00+00:00'
                WHERE id = ?
                """,
                """
                UPDATE events
                SET created_at = '2099-01-01T00:00:00+00:00'
                WHERE recovery_id = ? AND seq = 1
                """,
                """
                UPDATE executions
                SET created_at = '2099-01-01T00:00:00+00:00',
                    updated_at = '2099-01-01T00:00:00+00:00'
                WHERE recovery_id = ?
                """,
            ),
        ),
    ],
)
def test_startup_rejects_tampered_pristine_quota_snapshot_unchanged(
    tmp_path,
    monkeypatch,
    name,
    mutations,
) -> None:
    database_path = tmp_path / f"tampered-pristine-{name.replace(' ', '-')}.sqlite3"
    recovery_id = str(uuid4())
    store = SQLiteStore(database_path)
    _request_key, _request_fingerprint, session_key = _reserve_started_quota_creation(
        store,
        recovery_id,
    )
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(),
        quota_provider=QuotaSimulator(),
    )

    async def crash_before_sdk_dispatch(*_args, **_kwargs):
        raise SimulatedProcessCrash("crash after durable quota claim")

    monkeypatch.setattr("server.orchestrator.Runner.run", crash_before_sdk_dispatch)
    with pytest.raises(SimulatedProcessCrash):
        asyncio.run(
            orchestrator.start(
                ScenarioId.API_QUOTA,
                execution_mode=ExecutionMode.SDK_STUB,
                recovery_id=recovery_id,
                session_key=session_key,
            )
        )
    store.close()
    with sqlite3.connect(database_path) as connection:
        for mutation in mutations:
            connection.execute(mutation, (recovery_id,))
    before = _quota_database_state(database_path, recovery_id)

    fresh_store = SQLiteStore(database_path)
    provider = QuotaSimulator()
    with pytest.raises(ExecutionConflictError):
        RecoveryOrchestrator(
            store=fresh_store,
            hotel_provider=HotelSimulator(),
            quota_provider=provider,
        )
    assert provider.execution_count == 0
    fresh_store.close()
    assert _quota_database_state(database_path, recovery_id) == before


def test_quota_result_writer_and_finalizer_reject_sibling_execution_unchanged(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "ambiguous-quota-executions.sqlite3"
    recovery_id = str(uuid4())
    store = SQLiteStore(database_path)
    _request_key, _request_fingerprint, session_key = _reserve_started_quota_creation(
        store,
        recovery_id,
    )
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(),
        quota_provider=QuotaSimulator(),
    )

    async def crash_before_sdk_dispatch(*_args, **_kwargs):
        raise SimulatedProcessCrash("crash after durable quota claim")

    monkeypatch.setattr("server.orchestrator.Runner.run", crash_before_sdk_dispatch)
    with pytest.raises(SimulatedProcessCrash):
        asyncio.run(
            orchestrator.start(
                ScenarioId.API_QUOTA,
                execution_mode=ExecutionMode.SDK_STUB,
                recovery_id=recovery_id,
                session_key=session_key,
            )
        )
    execution = store.get_quota_execution(recovery_id)
    assert execution is not None
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO executions (
                id, recovery_id, idempotency_key, status,
                provider_execution, request_digest, tool_call_id,
                remedy_digest, result_json, created_at, updated_at
            )
            SELECT
                'sibling-' || id, recovery_id, 'sibling-' || idempotency_key, status,
                provider_execution, request_digest, tool_call_id,
                remedy_digest, result_json, created_at, updated_at
            FROM executions
            WHERE recovery_id = ?
            """,
            (recovery_id,),
        )
    before = _quota_database_state(database_path, recovery_id)

    with pytest.raises(ExecutionConflictError, match="ambiguous durable executions"):
        store.record_completed_quota_execution(
            execution,
            result=DETERMINISTIC_QUOTA_RESULT,
        )
    assert _quota_database_state(database_path, recovery_id) == before

    with pytest.raises(ExecutionConflictError, match="ambiguous durable executions"):
        store.finalize_pending_quota_execution_unknown(execution)
    assert _quota_database_state(database_path, recovery_id) == before


def test_completed_quota_finalizer_rejects_lost_durable_result_unchanged(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "lost-completed-quota-result.sqlite3"
    recovery_id = str(uuid4())
    store = SQLiteStore(database_path)
    _request_key, _request_fingerprint, session_key = _reserve_started_quota_creation(
        store,
        recovery_id,
    )
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(),
        quota_provider=QuotaSimulator(),
    )

    async def crash_before_sdk_dispatch(*_args, **_kwargs):
        raise SimulatedProcessCrash("crash after durable quota claim")

    monkeypatch.setattr("server.orchestrator.Runner.run", crash_before_sdk_dispatch)
    with pytest.raises(SimulatedProcessCrash):
        asyncio.run(
            orchestrator.start(
                ScenarioId.API_QUOTA,
                execution_mode=ExecutionMode.SDK_STUB,
                recovery_id=recovery_id,
                session_key=session_key,
            )
        )
    pending = store.get_quota_execution(recovery_id)
    assert pending is not None
    recorded, changed = store.record_completed_quota_execution(
        pending,
        result=DETERMINISTIC_QUOTA_RESULT,
    )
    assert changed is True
    assert recorded.status == "result_recorded"
    recorded_state = _quota_database_state(database_path, recovery_id)
    with pytest.raises(
        ExecutionConflictError,
        match="lost its durable result",
    ):
        store.finalize_completed_quota_execution(recorded)
    assert _quota_database_state(database_path, recovery_id) == recorded_state
    completed = store.validate_quota_sdk_completion(recorded)
    assert completed.status == "completed"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            UPDATE executions
            SET status = 'pending', provider_execution = 0,
                result_json = NULL, updated_at = created_at
            WHERE recovery_id = ?
            """,
            (recovery_id,),
        )
    before = _quota_database_state(database_path, recovery_id)

    with pytest.raises(
        ExecutionConflictError,
        match="lost its durable result",
    ):
        store.finalize_completed_quota_execution(completed)
    assert _quota_database_state(database_path, recovery_id) == before


def test_quota_invariant_quarantine_updates_execution_and_creation_atomically(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "quota-quarantine-atomic.sqlite3"
    recovery_id = str(uuid4())
    store = SQLiteStore(database_path)
    _request_key, _request_fingerprint, session_key = _reserve_started_quota_creation(
        store,
        recovery_id,
    )
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(),
        quota_provider=QuotaSimulator(),
    )

    async def crash_before_sdk_dispatch(*_args, **_kwargs):
        raise SimulatedProcessCrash("crash after durable quota claim")

    monkeypatch.setattr("server.orchestrator.Runner.run", crash_before_sdk_dispatch)
    with pytest.raises(SimulatedProcessCrash):
        asyncio.run(
            orchestrator.start(
                ScenarioId.API_QUOTA,
                execution_mode=ExecutionMode.SDK_STUB,
                recovery_id=recovery_id,
                session_key=session_key,
            )
        )
    pending = store.get_quota_execution(recovery_id)
    assert pending is not None
    recorded, changed = store.record_completed_quota_execution(
        pending,
        result=DETERMINISTIC_QUOTA_RESULT,
    )
    assert changed is True
    assert recorded.status == "result_recorded"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_quota_creation_quarantine
            BEFORE UPDATE OF status ON recovery_creations
            WHEN NEW.status = 'unknown'
            BEGIN
                SELECT RAISE(ABORT, 'injected quota quarantine failure');
            END
            """
        )
    before = _quota_database_state(database_path, recovery_id)

    with pytest.raises(
        sqlite3.IntegrityError,
        match="injected quota quarantine failure",
    ):
        store.quarantine_quota_execution_invariant_failure(recorded)
    assert _quota_database_state(database_path, recovery_id) == before

    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TRIGGER fail_quota_creation_quarantine")
    store.quarantine_quota_execution_invariant_failure(recorded)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT status FROM executions WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone() == ("sdk_invariant_failed",)
        assert connection.execute(
            "SELECT status FROM recovery_creations WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone() == ("unknown",)


@pytest.mark.parametrize(
    "sdk_outcome",
    (
        "human-interruption",
        "none",
        "false",
        "zero",
        "empty-string",
        "empty-tuple",
        "missing-interruptions",
        "raises-after-result",
    ),
)
def test_invalid_sdk_completion_after_durable_quota_result_is_quarantined(
    tmp_path,
    monkeypatch,
    sdk_outcome,
) -> None:
    database_path = tmp_path / "quota-sdk-interruption.sqlite3"
    settings = RuntimeSettings(
        live_ready=False,
        identity_hash_secret="quota-interruption-secret-that-is-long-enough",
    )
    store = SQLiteStore(database_path)
    provider = QuotaSimulator()
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(),
        quota_provider=provider,
    )

    async def return_invalid_completion_after_result(*_args, context, **_kwargs):
        result = context.provider.recover(
            context.request,
            idempotency_key=context.idempotency_key,
        )
        context.persist_result(result)
        if sdk_outcome == "raises-after-result":
            raise ValueError("invalid SDK completion")
        if sdk_outcome == "missing-interruptions":
            return SimpleNamespace()
        interruptions = {
            "human-interruption": [object()],
            "none": None,
            "false": False,
            "zero": 0,
            "empty-string": "",
            "empty-tuple": (),
        }[sdk_outcome]
        return SimpleNamespace(interruptions=interruptions)

    monkeypatch.setattr(
        "server.orchestrator.Runner.run",
        return_invalid_completion_after_result,
    )
    payload = {
        "scenarioId": "api-quota",
        "executionMode": "sdk_stub",
        "clientRequestId": "quota-sdk-interruption-001",
    }
    with TestClient(
        create_app(
            settings,
            store=store,
            orchestrator=orchestrator,
        )
    ) as client:
        first = client.post("/api/recoveries", json=payload)
        retry = client.post("/api/recoveries", json=payload)

    assert first.status_code == 409
    assert first.json()["code"] == "creation_outcome_unknown"
    assert retry.status_code == 409
    assert retry.json()["code"] == "creation_outcome_unknown"
    assert provider.execution_count == 1
    assert provider.active_permission_ids == ()
    with sqlite3.connect(database_path) as connection:
        recovery_id = connection.execute("SELECT recovery_id FROM recovery_creations").fetchone()[0]
        assert connection.execute(
            """
            SELECT status, current_step
            FROM recoveries
            WHERE id = ?
            """,
            (recovery_id,),
        ).fetchone() == ("in_progress", 0)
        execution = connection.execute(
            """
            SELECT status, provider_execution, result_json
            FROM executions
            WHERE recovery_id = ?
            """,
            (recovery_id,),
        ).fetchone()
        assert execution is not None
        assert execution[0:2] == ("sdk_invariant_failed", 1)
        assert execution[2] is not None
        assert connection.execute(
            "SELECT status FROM recovery_creations WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone() == ("unknown",)
        assert connection.execute(
            "SELECT type FROM events WHERE recovery_id = ? ORDER BY seq",
            (recovery_id,),
        ).fetchall() == [("recovery.created",)]
        assert connection.execute(
            "SELECT COUNT(*) FROM receipts WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone() == (0,)
    before = _quota_database_state(database_path, recovery_id)
    store.close()

    fresh_store = SQLiteStore(database_path)
    fresh_provider = QuotaSimulator()
    with pytest.raises(
        ExecutionConflictError,
        match="requires operator review",
    ):
        RecoveryOrchestrator(
            store=fresh_store,
            hotel_provider=HotelSimulator(),
            quota_provider=fresh_provider,
        )
    assert fresh_provider.execution_count == 0
    fresh_store.close()
    assert _quota_database_state(database_path, recovery_id) == before


def test_sdk_interruption_without_durable_result_is_quarantined(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "quota-sdk-interruption-before-result.sqlite3"
    settings = RuntimeSettings(
        live_ready=False,
        identity_hash_secret="quota-pending-interruption-secret-that-is-long-enough",
    )
    store = SQLiteStore(database_path)
    provider = QuotaSimulator()
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(),
        quota_provider=provider,
    )

    async def return_interruption_without_result(*_args, **_kwargs):
        return SimpleNamespace(interruptions=[object()])

    monkeypatch.setattr(
        "server.orchestrator.Runner.run",
        return_interruption_without_result,
    )
    payload = {
        "scenarioId": "api-quota",
        "executionMode": "sdk_stub",
        "clientRequestId": "quota-sdk-pending-interruption-001",
    }
    with TestClient(
        create_app(
            settings,
            store=store,
            orchestrator=orchestrator,
        )
    ) as client:
        response = client.post("/api/recoveries", json=payload)

    assert response.status_code == 409
    assert response.json()["code"] == "creation_outcome_unknown"
    assert provider.execution_count == 0
    with sqlite3.connect(database_path) as connection:
        recovery_id = connection.execute("SELECT recovery_id FROM recovery_creations").fetchone()[0]
        assert connection.execute(
            """
            SELECT status, provider_execution, result_json
            FROM executions
            WHERE recovery_id = ?
            """,
            (recovery_id,),
        ).fetchone() == ("sdk_invariant_failed", 0, None)
        assert connection.execute(
            "SELECT status FROM recovery_creations WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone() == ("unknown",)
        assert connection.execute(
            "SELECT COUNT(*) FROM receipts WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM events WHERE recovery_id = ? AND terminal = 1",
            (recovery_id,),
        ).fetchone() == (0,)
    before = _quota_database_state(database_path, recovery_id)
    store.close()

    blocked_store = SQLiteStore(database_path)
    with pytest.raises(
        ExecutionConflictError,
        match="requires operator review",
    ):
        RecoveryOrchestrator(
            store=blocked_store,
            hotel_provider=HotelSimulator(),
            quota_provider=QuotaSimulator(),
        )
    blocked_store.close()
    assert _quota_database_state(database_path, recovery_id) == before


def test_fresh_runtime_cannot_publish_result_before_sdk_validation(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "quota-result-before-sdk-validation.sqlite3"
    settings = RuntimeSettings(
        live_ready=False,
        identity_hash_secret="quota-validation-race-secret-that-is-long-enough",
    )
    store = SQLiteStore(database_path)
    provider = QuotaSimulator()
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(),
        quota_provider=provider,
    )
    result_recorded = Event()
    release_runner = Event()

    async def pause_after_result(*_args, context, **_kwargs):
        result = context.provider.recover(
            context.request,
            idempotency_key=context.idempotency_key,
        )
        context.persist_result(result)
        result_recorded.set()
        assert release_runner.wait(timeout=5)
        return SimpleNamespace(interruptions=[object()])

    monkeypatch.setattr("server.orchestrator.Runner.run", pause_after_result)
    payload = {
        "scenarioId": "api-quota",
        "executionMode": "sdk_stub",
        "clientRequestId": "quota-validation-race-001",
    }
    with (
        TestClient(
            create_app(
                settings,
                store=store,
                orchestrator=orchestrator,
            )
        ) as client,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        assert client.get("/health").status_code == 200
        future = executor.submit(client.post, "/api/recoveries", json=payload)
        assert result_recorded.wait(timeout=5)
        with sqlite3.connect(database_path) as connection:
            recovery_id, session_key = connection.execute(
                "SELECT recovery_id, session_key FROM recovery_creations"
            ).fetchone()
            assert connection.execute(
                "SELECT status FROM executions WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone() == ("result_recorded",)
            assert connection.execute(
                "SELECT COUNT(*) FROM receipts WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone() == (0,)
        public_before_restart = store.get_public_recovery(
            recovery_id,
            session_key=session_key,
            replay_scenarios=_replay_scenarios(),
        )
        assert public_before_restart.status is RecoveryStatus.IN_PROGRESS

        fresh_store = SQLiteStore(database_path)
        with pytest.raises(
            ExecutionConflictError,
            match="not durably validated",
        ):
            RecoveryOrchestrator(
                store=fresh_store,
                hotel_provider=HotelSimulator(),
                quota_provider=QuotaSimulator(),
            )
        with sqlite3.connect(database_path) as connection:
            assert connection.execute(
                "SELECT status FROM recovery_creations WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone() == ("unknown",)
        with pytest.raises(
            PublicEvidenceIntegrityError,
            match="in-progress evidence is not canonical",
        ):
            fresh_store.get_public_recovery(
                recovery_id,
                session_key=session_key,
                replay_scenarios=_replay_scenarios(),
            )
        release_runner.set()
        response = future.result(timeout=5)
        fresh_store.close()

    assert response.status_code == 409
    assert response.json()["code"] == "creation_outcome_unknown"
    assert provider.execution_count == 1
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT status FROM executions WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone() == ("sdk_invariant_failed",)
        assert connection.execute(
            "SELECT status FROM recovery_creations WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone() == ("unknown",)
        assert connection.execute(
            "SELECT COUNT(*) FROM receipts WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM events WHERE recovery_id = ? AND terminal = 1",
            (recovery_id,),
        ).fetchone() == (0,)
    before = _quota_database_state(database_path, recovery_id)
    store.close()

    blocked_store = SQLiteStore(database_path)
    with pytest.raises(
        ExecutionConflictError,
        match="requires operator review",
    ):
        RecoveryOrchestrator(
            store=blocked_store,
            hotel_provider=HotelSimulator(),
            quota_provider=QuotaSimulator(),
        )
    blocked_store.close()
    assert _quota_database_state(database_path, recovery_id) == before


def test_startup_rejects_future_durable_quota_result_timestamp(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "future-quota-result.sqlite3"
    recovery_id = str(uuid4())
    store = SQLiteStore(database_path)
    provider = QuotaSimulator()
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(),
        quota_provider=provider,
    )

    def crash_before_finalization(*_args, **_kwargs) -> None:
        raise SimulatedProcessCrash("crash after durable quota result")

    monkeypatch.setattr(
        store,
        "finalize_completed_quota_execution",
        crash_before_finalization,
    )
    with pytest.raises(SimulatedProcessCrash):
        asyncio.run(
            orchestrator.start(
                ScenarioId.API_QUOTA,
                execution_mode=ExecutionMode.SDK_STUB,
                recovery_id=recovery_id,
            )
        )
    assert provider.execution_count == 1
    store.close()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            UPDATE executions
            SET updated_at = '2099-01-01T00:00:00+00:00'
            WHERE recovery_id = ?
            """,
            (recovery_id,),
        )

    fresh_store = SQLiteStore(database_path)
    fresh_provider = QuotaSimulator()
    with pytest.raises(ExecutionConflictError, match="chronology"):
        RecoveryOrchestrator(
            store=fresh_store,
            hotel_provider=HotelSimulator(),
            quota_provider=fresh_provider,
        )
    assert fresh_provider.execution_count == 0
    assert fresh_store.get_recovery(recovery_id).status is RecoveryStatus.IN_PROGRESS
    with pytest.raises(RecoveryNotFoundError, match="Receipt not found"):
        fresh_store.get_receipt(recovery_id)


def test_terminal_quota_bundle_rejects_pending_execution_status(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "terminal-pending-quota.sqlite3"
    recovery_id = str(uuid4())
    store = SQLiteStore(database_path)
    _request_key, _request_fingerprint, session_key = _reserve_started_quota_creation(
        store, recovery_id
    )
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(),
        quota_provider=QuotaSimulator(),
    )

    async def crash_before_sdk_dispatch(*_args, **_kwargs):
        raise SimulatedProcessCrash("crash after durable quota claim")

    monkeypatch.setattr("server.orchestrator.Runner.run", crash_before_sdk_dispatch)
    with pytest.raises(SimulatedProcessCrash):
        asyncio.run(
            orchestrator.start(
                ScenarioId.API_QUOTA,
                execution_mode=ExecutionMode.SDK_STUB,
                recovery_id=recovery_id,
                session_key=session_key,
            )
        )
    store.close()
    reconciled = SQLiteStore(database_path)
    RecoveryOrchestrator(
        store=reconciled,
        hotel_provider=HotelSimulator(),
        quota_provider=QuotaSimulator(),
    )
    assert reconciled.get_recovery(recovery_id).status is RecoveryStatus.OUTCOME_UNKNOWN
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            UPDATE executions
            SET status = 'pending', updated_at = created_at
            WHERE recovery_id = ?
            """,
            (recovery_id,),
        )

    with pytest.raises(
        PublicEvidenceIntegrityError,
        match="terminal evidence is not canonical",
    ):
        reconciled.get_public_recovery(
            recovery_id,
            session_key=session_key,
            replay_scenarios=_replay_scenarios(),
        )


@pytest.mark.parametrize("terminal_status", ["completed", "outcome_unknown"])
def test_terminal_quota_bundle_rejects_human_approval_evidence(
    tmp_path,
    monkeypatch,
    terminal_status,
) -> None:
    database_path = tmp_path / f"terminal-human-evidence-{terminal_status}.sqlite3"
    recovery_id = str(uuid4())
    store = SQLiteStore(database_path)
    _request_key, _request_fingerprint, session_key = _reserve_started_quota_creation(
        store,
        recovery_id,
    )
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(),
        quota_provider=QuotaSimulator(),
    )
    if terminal_status == "completed":
        asyncio.run(
            orchestrator.start(
                ScenarioId.API_QUOTA,
                execution_mode=ExecutionMode.SDK_STUB,
                recovery_id=recovery_id,
                session_key=session_key,
            )
        )
    else:

        async def crash_before_sdk_dispatch(*_args, **_kwargs):
            raise SimulatedProcessCrash("crash after durable quota claim")

        monkeypatch.setattr("server.orchestrator.Runner.run", crash_before_sdk_dispatch)
        with pytest.raises(SimulatedProcessCrash):
            asyncio.run(
                orchestrator.start(
                    ScenarioId.API_QUOTA,
                    execution_mode=ExecutionMode.SDK_STUB,
                    recovery_id=recovery_id,
                    session_key=session_key,
                )
            )
        store.close()
        store = SQLiteStore(database_path)
        RecoveryOrchestrator(
            store=store,
            hotel_provider=HotelSimulator(),
            quota_provider=QuotaSimulator(),
        )
    assert store.get_recovery(recovery_id).status.value == terminal_status
    with sqlite3.connect(database_path) as connection:
        terminal_at = connection.execute(
            "SELECT updated_at FROM recoveries WHERE id = ?",
            (recovery_id,),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO approval_decisions (
                recovery_id, client_decision_id, action, remedy_id,
                remedy_digest, tool_call_id, request_fingerprint,
                status, result_json, claimed_at, completed_at
            ) VALUES (?, ?, 'approve', 'foreign-remedy', ?, 'foreign-tool', ?,
                      'claimed', NULL, ?, NULL)
            """,
            (
                recovery_id,
                str(uuid4()),
                "d" * 64,
                "e" * 64,
                terminal_at,
            ),
        )

    with pytest.raises(
        PublicEvidenceIntegrityError,
        match="terminal evidence is not canonical",
    ):
        store.get_public_recovery(
            recovery_id,
            session_key=session_key,
            replay_scenarios=_replay_scenarios(),
        )
    with pytest.raises(
        PublicEvidenceIntegrityError,
        match="terminal evidence is not canonical",
    ):
        store.get_public_receipt(
            recovery_id,
            session_key=session_key,
            replay_scenarios=_replay_scenarios(),
        )


def test_new_quota_terminal_bundle_rejects_deleted_execution(
    tmp_path,
) -> None:
    database_path = tmp_path / "deleted-new-quota-execution.sqlite3"
    recovery_id = str(uuid4())
    store = SQLiteStore(database_path)
    _request_key, _request_fingerprint, session_key = _reserve_started_quota_creation(
        store, recovery_id
    )
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(),
        quota_provider=QuotaSimulator(),
    )
    asyncio.run(
        orchestrator.start(
            ScenarioId.API_QUOTA,
            execution_mode=ExecutionMode.SDK_STUB,
            recovery_id=recovery_id,
            session_key=session_key,
        )
    )
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            """
            SELECT quota_execution_contract
            FROM recoveries
            WHERE id = ?
            """,
            (recovery_id,),
        ).fetchone() == (1,)
        connection.execute(
            "DELETE FROM executions WHERE recovery_id = ?",
            (recovery_id,),
        )

    with TestClient(
        create_app(
            RuntimeSettings(live_ready=False),
            store=store,
            orchestrator=orchestrator,
        )
    ) as client:
        readiness = client.get("/readyz")
    assert readiness.status_code == 503
    assert readiness.json() == {"status": "not_ready"}
    fresh_store = SQLiteStore(database_path)
    with pytest.raises(
        ExecutionConflictError,
        match="Terminal quota recovery evidence is not canonical",
    ):
        RecoveryOrchestrator(
            store=fresh_store,
            hotel_provider=HotelSimulator(),
            quota_provider=QuotaSimulator(),
        )
    fresh_store.close()

    with pytest.raises(
        PublicEvidenceIntegrityError,
        match="terminal evidence is not canonical",
    ):
        store.get_public_recovery(
            recovery_id,
            session_key=session_key,
            replay_scenarios=_replay_scenarios(),
        )
    with pytest.raises(
        RecoveryNotFoundError,
        match="no authoritative outcome evidence",
    ):
        store.get_authoritative_recovery_creation(
            recovery_id=recovery_id,
            scenario_id=ScenarioId.API_QUOTA,
            execution_mode=ExecutionMode.SDK_STUB,
            session_key=session_key,
        )
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM quota_legacy_completions").fetchone() == (
            0,
        )
        connection.execute(
            """
            UPDATE recoveries
            SET quota_execution_contract = 0
            WHERE id = ?
            """,
            (recovery_id,),
        )
    with pytest.raises(
        PublicEvidenceIntegrityError,
        match="terminal evidence is not canonical",
    ):
        store.get_public_recovery(
            recovery_id,
            session_key=session_key,
            replay_scenarios=_replay_scenarios(),
        )
    downgraded_store = SQLiteStore(database_path)
    with pytest.raises(
        ExecutionConflictError,
        match="Terminal quota recovery evidence is not canonical",
    ):
        RecoveryOrchestrator(
            store=downgraded_store,
            hotel_provider=HotelSimulator(),
            quota_provider=QuotaSimulator(),
        )
    downgraded_store.close()


@pytest.mark.parametrize("creation_status", ["started", "unknown", "ready"])
def test_legacy_completed_quota_without_execution_remains_readable(
    tmp_path,
    creation_status,
) -> None:
    database_path = tmp_path / "legacy-completed-quota.sqlite3"
    recovery_id = str(uuid4())
    store = SQLiteStore(database_path)
    request_key, request_fingerprint, session_key = _reserve_started_quota_creation(
        store, recovery_id
    )
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(),
        quota_provider=QuotaSimulator(),
    )
    asyncio.run(
        orchestrator.start(
            ScenarioId.API_QUOTA,
            execution_mode=ExecutionMode.SDK_STUB,
            recovery_id=recovery_id,
            session_key=session_key,
        )
    )
    store.close()
    with sqlite3.connect(database_path) as connection:
        recovery_created_at, recovery_updated_at = (
            datetime.fromisoformat(value)
            for value in connection.execute(
                "SELECT created_at, updated_at FROM recoveries WHERE id = ?",
                (recovery_id,),
            ).fetchone()
        )
        creation_updated_at = (
            recovery_created_at
            if creation_status == "started"
            else recovery_updated_at + timedelta(microseconds=1)
        )
        connection.execute(
            """
            UPDATE recovery_creations
            SET status = ?, updated_at = ?
            WHERE recovery_id = ?
            """,
            (
                creation_status,
                creation_updated_at.isoformat(),
                recovery_id,
            ),
        )
        creation_expires_at = datetime.fromisoformat(
            connection.execute(
                "SELECT expires_at FROM recovery_creations WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone()[0]
        )
        connection.execute(
            "DELETE FROM executions WHERE recovery_id = ?",
            (recovery_id,),
        )
        connection.execute("DROP TABLE quota_legacy_completions")
        connection.execute(
            """
            ALTER TABLE recoveries
            DROP COLUMN quota_execution_contract
            """
        )
        assert "quota_execution_contract" not in {
            row[1] for row in connection.execute("PRAGMA table_info(recoveries)").fetchall()
        }

    legacy_store = SQLiteStore(database_path)
    provider = QuotaSimulator()
    RecoveryOrchestrator(
        store=legacy_store,
        hotel_provider=HotelSimulator(),
        quota_provider=provider,
    )
    assert provider.execution_count == 0
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            """
            SELECT quota_execution_contract
            FROM recoveries
            WHERE id = ?
            """,
            (recovery_id,),
        ).fetchone() == (0,)
        legacy_provenance = connection.execute(
            """
            SELECT recovery_fingerprint, recorded_at
            FROM quota_legacy_completions
            WHERE recovery_id = ?
            """,
            (recovery_id,),
        ).fetchone()
        assert legacy_provenance is not None
        assert len(legacy_provenance[0]) == 64
        assert datetime.fromisoformat(legacy_provenance[1]).utcoffset() == timedelta(0)
    snapshot = legacy_store.get_public_recovery(
        recovery_id,
        session_key=session_key,
        replay_scenarios=_replay_scenarios(),
    )
    receipt = legacy_store.get_public_receipt(
        recovery_id,
        session_key=session_key,
        replay_scenarios=_replay_scenarios(),
    )
    events, status = legacy_store.read_public_event_batch(
        recovery_id,
        session_key=session_key,
        replay_scenarios=_replay_scenarios(),
    )
    assert snapshot.status is RecoveryStatus.COMPLETED
    assert receipt.status == "completed"
    assert receipt.provider_execution is True
    assert receipt.approval_count == 0
    assert status is RecoveryStatus.COMPLETED
    assert [event.type for event in events] == EXPECTED_QUOTA_EVENTS
    retry = legacy_store.claim_recovery_creation(
        request_key=request_key,
        request_fingerprint=request_fingerprint,
        scenario_id=ScenarioId.API_QUOTA,
        execution_mode=ExecutionMode.SDK_STUB,
        reserved_recovery_id=str(uuid4()),
        session_key=session_key,
        ip_key="c" * 64,
        expires_at=creation_expires_at,
        now=max(datetime.now(UTC), creation_updated_at + timedelta(microseconds=1)),
    )
    assert retry.disposition == "ready"
    assert retry.recovery_id == recovery_id
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT status FROM recovery_creations WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone() == ("ready",)
    with pytest.raises(
        ExecutionConflictError,
        match="no durable execution contract",
    ):
        legacy_store.get_quota_execution(recovery_id)


def test_legacy_quota_migration_retries_after_interrupted_first_open(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "interrupted-legacy-quota-migration.sqlite3"
    recovery_id = str(uuid4())
    store = SQLiteStore(database_path)
    _request_key, _request_fingerprint, session_key = _reserve_started_quota_creation(
        store,
        recovery_id,
    )
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(),
        quota_provider=QuotaSimulator(),
    )
    asyncio.run(
        orchestrator.start(
            ScenarioId.API_QUOTA,
            execution_mode=ExecutionMode.SDK_STUB,
            recovery_id=recovery_id,
            session_key=session_key,
        )
    )
    store.close()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "DELETE FROM executions WHERE recovery_id = ?",
            (recovery_id,),
        )
        connection.execute("DROP TABLE quota_legacy_completions")
        connection.execute("ALTER TABLE recoveries DROP COLUMN quota_execution_contract")

    fingerprint = SQLiteStore._legacy_quota_recovery_fingerprint

    def interrupt_fingerprint(_row):
        raise SimulatedProcessCrash("interrupt legacy quota migration")

    monkeypatch.setattr(
        SQLiteStore,
        "_legacy_quota_recovery_fingerprint",
        staticmethod(interrupt_fingerprint),
    )
    with pytest.raises(
        SimulatedProcessCrash,
        match="interrupt legacy quota migration",
    ):
        SQLiteStore(database_path)
    with sqlite3.connect(database_path) as connection:
        assert "quota_execution_contract" not in {
            row[1] for row in connection.execute("PRAGMA table_info(recoveries)").fetchall()
        }
        assert connection.execute(
            """
            SELECT COUNT(*)
            FROM sqlite_master
            WHERE type = 'table' AND name = 'quota_legacy_completions'
            """
        ).fetchone() == (0,)

    monkeypatch.setattr(
        SQLiteStore,
        "_legacy_quota_recovery_fingerprint",
        staticmethod(fingerprint),
    )
    migrated = SQLiteStore(database_path)
    provider = QuotaSimulator()
    RecoveryOrchestrator(
        store=migrated,
        hotel_provider=HotelSimulator(),
        quota_provider=provider,
    )
    assert provider.execution_count == 0
    assert (
        migrated.get_public_recovery(
            recovery_id,
            session_key=session_key,
            replay_scenarios=_replay_scenarios(),
        ).status
        is RecoveryStatus.COMPLETED
    )
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            """
            SELECT quota_execution_contract
            FROM recoveries
            WHERE id = ?
            """,
            (recovery_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            """
            SELECT COUNT(*)
            FROM quota_legacy_completions
            WHERE recovery_id = ?
            """,
            (recovery_id,),
        ).fetchone() == (1,)
    migrated.close()
