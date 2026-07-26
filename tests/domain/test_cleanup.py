from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from server.cleanup import (
    cleanup_expired_recovery_creations,
    cleanup_terminal_recoveries,
    expire_pending_approvals,
)
from server.config import RuntimeSettings
from server.main import create_app
from server.models import ExecutionMode, RecoveryStatus, ScenarioId
from server.orchestrator import RecoveryOrchestrator
from server.providers.hotel_simulator import HotelSimulator
from server.replay.engine import ReplayEngine
from server.replay.loader import ScenarioLoader
from server.store import RecoveryNotFoundError, SQLiteStore


def _start_pending_hotel(store: SQLiteStore) -> str:
    pending = asyncio.run(
        RecoveryOrchestrator(
            store=store,
            hotel_provider=HotelSimulator(store=store),
        ).start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    return pending.recovery.recovery_id


def _expired_time_for_pending_hotel(database_path, recovery_id: str) -> datetime:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT expiry FROM remedies WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone()
    assert row is not None
    return datetime.fromisoformat(str(row[0])) + timedelta(seconds=1)


def _create_recovery(
    store: SQLiteStore,
    recovery_id: str,
    *,
    terminal: bool,
    execution_mode: ExecutionMode = ExecutionMode.REPLAY_FIXTURE,
) -> None:
    store.create_recovery(
        recovery_id=recovery_id,
        scenario_id=ScenarioId.HOTEL,
        execution_mode=execution_mode,
        current_step=0,
        current_step_summary="Created for cleanup test.",
        root_trace_id=(
            "qa_trace_0123456789abcdef0123456789abcdef"
            if execution_mode is ExecutionMode.SDK_STUB
            else None
        ),
        sdk_version=("0.18.3" if execution_mode is ExecutionMode.SDK_STUB else None),
        protocol_version=(
            "backchannel.approval.v1" if execution_mode is ExecutionMode.SDK_STUB else None
        ),
        agent_graph_version=(
            "backchannel.hotel-agent.v1" if execution_mode is ExecutionMode.SDK_STUB else None
        ),
        definition_digest=("a" * 64 if execution_mode is ExecutionMode.SDK_STUB else None),
    )
    if terminal:
        store.record_transition(
            recovery_id,
            status=RecoveryStatus.COMPLETED,
            current_step=5,
            current_step_summary="Terminal cleanup fixture.",
            event_type="recovery.completed",
            event_data={"summary": "Terminal cleanup fixture."},
        )


def _create_scoped_creation_claim(
    store: SQLiteStore,
    *,
    request_key: str,
    session_key: str,
    created_at: datetime,
    expires_at: datetime,
) -> None:
    claim = store.claim_recovery_creation(
        request_key=request_key,
        request_fingerprint="f" * 64,
        scenario_id=ScenarioId.HOTEL,
        execution_mode=ExecutionMode.SDK_STUB,
        reserved_recovery_id=str(uuid4()),
        session_key=session_key,
        ip_key="a" * 64,
        expires_at=expires_at,
        now=created_at,
    )
    assert claim.disposition == "owner"


def test_cleanup_deletes_only_expired_terminal_rows_and_preserves_usage(tmp_path) -> None:
    database_path = tmp_path / "cleanup.sqlite3"
    store = SQLiteStore(database_path)
    _create_recovery(store, "expired-terminal", terminal=True)
    _create_recovery(store, "recent-terminal", terminal=True)
    _create_recovery(
        store,
        "old-in-progress",
        terminal=False,
        execution_mode=ExecutionMode.SDK_STUB,
    )
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    old = (now - timedelta(days=2)).isoformat()
    recent = (now - timedelta(minutes=5)).isoformat()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE recoveries SET updated_at = ? WHERE id IN (?, ?)",
            (old, "expired-terminal", "old-in-progress"),
        )
        connection.execute(
            "UPDATE recoveries SET updated_at = ? WHERE id = ?",
            (recent, "recent-terminal"),
        )
        connection.execute(
            "INSERT INTO usage_ledger (recovery_id, category, amount, recorded_at) "
            "VALUES (?, ?, ?, ?)",
            ("expired-terminal", "live_demo_budget_unit", 1, old),
        )

    deleted = cleanup_terminal_recoveries(
        store,
        terminal_ttl=timedelta(days=1),
        now=now,
        batch_size=10,
    )

    assert deleted == 1
    with pytest.raises(RecoveryNotFoundError):
        store.get_recovery("expired-terminal")
    assert store.get_recovery("recent-terminal").status is RecoveryStatus.COMPLETED
    assert store.get_recovery("old-in-progress").status is RecoveryStatus.IN_PROGRESS
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT category, amount FROM usage_ledger WHERE recovery_id = ?",
            ("expired-terminal",),
        ).fetchall() == [("live_demo_budget_unit", 1)]

    assert (
        cleanup_terminal_recoveries(
            store,
            terminal_ttl=timedelta(days=1),
            now=now,
            batch_size=10,
        )
        == 0
    )


def test_cleanup_expires_stale_in_progress_replay_but_preserves_pending_modes(
    tmp_path,
) -> None:
    database_path = tmp_path / "replay-cleanup.sqlite3"
    store = SQLiteStore(database_path)
    replay = ReplayEngine(store, ScenarioLoader()).start(
        "hotel",
        execution_mode=ExecutionMode.REPLAY_FIXTURE,
    )
    assert replay.status is RecoveryStatus.IN_PROGRESS

    pending_modes = {
        "pending-sdk": ExecutionMode.SDK_STUB,
        "pending-live": ExecutionMode.OPENAI_LIVE,
    }
    for recovery_id, execution_mode in pending_modes.items():
        store.create_recovery(
            recovery_id=recovery_id,
            scenario_id=ScenarioId.HOTEL,
            execution_mode=execution_mode,
            current_step=0,
            current_step_summary="Real approval flow started.",
            model_ids=(
                ["gpt-5.6-luna", "gpt-5.6-terra"]
                if execution_mode is ExecutionMode.OPENAI_LIVE
                else []
            ),
            root_trace_id=(
                "trace_0123456789abcdef0123456789abcdef"
                if execution_mode is ExecutionMode.OPENAI_LIVE
                else "qa_trace_0123456789abcdef0123456789abcdef"
            ),
            model_call=execution_mode is ExecutionMode.OPENAI_LIVE,
            sdk_version="0.18.3",
            protocol_version="backchannel.approval.v1",
            agent_graph_version="backchannel.hotel-agent.v1",
            definition_digest="a" * 64,
        )
        store.record_transition(
            recovery_id,
            status=RecoveryStatus.PENDING_APPROVAL,
            current_step=3,
            current_step_summary="A genuine approval remains pending.",
            event_type="approval.requested",
            event_data={"providerExecution": False},
        )

    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    old = (now - timedelta(days=2)).isoformat()
    with sqlite3.connect(database_path) as connection:
        connection.execute("UPDATE recoveries SET updated_at = ?", (old,))
        connection.execute(
            "INSERT INTO usage_ledger (recovery_id, category, amount, recorded_at) "
            "VALUES (?, ?, ?, ?)",
            (replay.recovery_id, "replay_fixture_start", 1, old),
        )

    assert (
        cleanup_terminal_recoveries(
            store,
            terminal_ttl=timedelta(days=1),
            now=now,
            batch_size=10,
        )
        == 1
    )

    with pytest.raises(RecoveryNotFoundError):
        store.get_recovery(replay.recovery_id)
    for recovery_id in pending_modes:
        assert store.get_recovery(recovery_id).status is RecoveryStatus.PENDING_APPROVAL
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT category, amount FROM usage_ledger WHERE recovery_id = ?",
            (replay.recovery_id,),
        ).fetchall() == [("replay_fixture_start", 1)]


def test_cleanup_rejects_unbounded_or_nonpositive_inputs(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "cleanup-bounds.sqlite3")

    with pytest.raises(ValueError, match="terminal_ttl"):
        cleanup_terminal_recoveries(store, terminal_ttl=timedelta(0), batch_size=10)
    with pytest.raises(ValueError, match="batch_size"):
        cleanup_terminal_recoveries(store, terminal_ttl=timedelta(days=1), batch_size=0)


def test_lifespan_periodically_removes_idle_expired_terminal_detail(tmp_path) -> None:
    database_path = tmp_path / "periodic-cleanup.sqlite3"
    store = SQLiteStore(database_path)
    _create_recovery(store, "idle-terminal", terminal=True)
    recorded_at = datetime.now(UTC).isoformat()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO usage_ledger (recovery_id, category, amount, recorded_at) "
            "VALUES (?, ?, ?, ?)",
            ("idle-terminal", "live_demo_budget_unit", 1, recorded_at),
        )
    settings = RuntimeSettings(
        live_ready=False,
        terminal_recovery_ttl_seconds=1,
        terminal_cleanup_interval_seconds=1,
    )

    with TestClient(create_app(settings, store=store)):
        expired = (datetime.now(UTC) - timedelta(seconds=2)).isoformat()
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                "UPDATE recoveries SET updated_at = ? WHERE id = ?",
                (expired, "idle-terminal"),
            )
        deadline = time.monotonic() + 3
        remaining = 1
        while remaining and time.monotonic() < deadline:
            time.sleep(0.05)
            with sqlite3.connect(database_path) as connection:
                remaining = connection.execute(
                    "SELECT COUNT(*) FROM recoveries WHERE id = ?",
                    ("idle-terminal",),
                ).fetchone()[0]

    assert remaining == 0
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT category, amount FROM usage_ledger WHERE recovery_id = ?",
            ("idle-terminal",),
        ).fetchall() == [("live_demo_budget_unit", 1)]


def test_creation_claim_cleanup_runs_at_startup_and_on_periodic_cadence(
    tmp_path,
) -> None:
    database_path = tmp_path / "periodic-creation-claims.sqlite3"
    store = SQLiteStore(database_path)
    now = datetime.now(UTC)
    _create_scoped_creation_claim(
        store,
        request_key="a" * 64,
        session_key="1" * 64,
        created_at=now - timedelta(hours=2),
        expires_at=now - timedelta(hours=1),
    )
    _create_scoped_creation_claim(
        store,
        request_key="b" * 64,
        session_key="2" * 64,
        created_at=now,
        expires_at=now + timedelta(hours=1),
    )
    settings = RuntimeSettings(
        live_ready=False,
        terminal_recovery_ttl_seconds=3_600,
        terminal_cleanup_interval_seconds=1,
    )

    with TestClient(create_app(settings, store=store)):
        with sqlite3.connect(database_path) as connection:
            assert connection.execute("SELECT request_key FROM recovery_creations").fetchall() == [
                ("b" * 64,)
            ]
            connection.execute(
                "UPDATE recovery_creations SET expires_at = ?",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
            )
        deadline = time.monotonic() + 3
        remaining = 1
        while remaining and time.monotonic() < deadline:
            time.sleep(0.05)
            with sqlite3.connect(database_path) as connection:
                remaining = connection.execute(
                    "SELECT COUNT(*) FROM recovery_creations"
                ).fetchone()[0]

    assert remaining == 0
    store.close()


def test_creation_claim_cleanup_wrapper_validates_bounds_and_utc(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "creation-claim-cleanup-bounds.sqlite3")

    with pytest.raises(ValueError, match="batch_size"):
        cleanup_expired_recovery_creations(store, batch_size=0)
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        cleanup_expired_recovery_creations(
            store,
            now=datetime(2026, 7, 24),
        )

    store.close()


def test_lifespan_expires_untouched_consent_at_startup_and_on_cleanup_cadence(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "periodic-consent-expiry.sqlite3"
    store = SQLiteStore(database_path)
    startup_recovery_id = _start_pending_hotel(store)
    cleanup_now = _expired_time_for_pending_hotel(database_path, startup_recovery_id)
    monkeypatch.setattr(
        "server.main.expire_pending_approvals",
        lambda store, **kwargs: expire_pending_approvals(
            store,
            now=cleanup_now,
            **kwargs,
        ),
    )
    settings = RuntimeSettings(
        live_ready=False,
        terminal_recovery_ttl_seconds=3_600,
        terminal_cleanup_interval_seconds=1,
    )

    with TestClient(create_app(settings, store=store)) as client:
        assert (
            store.get_recovery(startup_recovery_id).status is RecoveryStatus.CLOSED_WITHOUT_ACTION
        )
        assert store.get_receipt(startup_recovery_id).status == "closed_without_action"

        response = client.post(
            "/api/recoveries",
            json={
                "scenarioId": "hotel",
                "executionMode": "sdk_stub",
                "clientRequestId": uuid4().hex,
            },
        )
        assert response.status_code == 201
        periodic_recovery_id = str(response.json()["recoveryId"])
        cleanup_now = _expired_time_for_pending_hotel(
            database_path,
            periodic_recovery_id,
        )
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if (
                store.get_recovery(periodic_recovery_id).status
                is RecoveryStatus.CLOSED_WITHOUT_ACTION
            ):
                break
            time.sleep(0.05)

        assert (
            store.get_recovery(periodic_recovery_id).status is RecoveryStatus.CLOSED_WITHOUT_ACTION
        )
        assert store.get_receipt(periodic_recovery_id).status == "closed_without_action"


def test_create_runs_expiry_before_retention_without_deleting_new_terminal_evidence(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "create-consent-expiry.sqlite3"
    store = SQLiteStore(database_path)
    settings = RuntimeSettings(
        live_ready=False,
        terminal_recovery_ttl_seconds=3_600,
        terminal_cleanup_interval_seconds=300,
    )

    with TestClient(create_app(settings, store=store)) as client:
        first = client.post(
            "/api/recoveries",
            json={
                "scenarioId": "hotel",
                "executionMode": "sdk_stub",
                "clientRequestId": uuid4().hex,
            },
        )
        assert first.status_code == 201
        expired_recovery_id = str(first.json()["recoveryId"])
        expired_at = _expired_time_for_pending_hotel(
            database_path,
            expired_recovery_id,
        )
        monkeypatch.setattr(
            "server.main.expire_pending_approvals",
            lambda store, **kwargs: expire_pending_approvals(
                store,
                now=expired_at,
                **kwargs,
            ),
        )

        second = client.post(
            "/api/recoveries",
            json={
                "scenarioId": "hotel",
                "executionMode": "sdk_stub",
                "clientRequestId": uuid4().hex,
            },
        )

        assert second.status_code == 201
        assert (
            store.get_recovery(expired_recovery_id).status is RecoveryStatus.CLOSED_WITHOUT_ACTION
        )
        assert store.get_receipt(expired_recovery_id).status == "closed_without_action"
        assert store.count_decisions(expired_recovery_id) == 0
        assert store.count_executions(expired_recovery_id) == 0
