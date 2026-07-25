from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event
from threading import enumerate as enumerate_threads

import pytest

from server.cleanup import RecoveryCleanupService
from server.config import RuntimeSettings
from server.main import create_app
from server.models import (
    ApprovalDecisionRequest,
    ExecutionMode,
    RecoveryStatus,
    ScenarioId,
)
from server.orchestrator import RecoveryOrchestrator
from server.providers.hotel_simulator import HotelSimulator
from server.replay.engine import ReplayEngine
from server.replay.loader import ScenarioLoader
from server.store import RecoveryNotFoundError, SQLiteStore


class _StartupFailureOrchestrator:
    def __init__(self) -> None:
        self.shutdown_calls = 0

    async def startup(self) -> None:
        raise RuntimeError("startup-failure-canary")

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


class _TrackingStartupOrchestrator:
    def __init__(self) -> None:
        self.startup_calls = 0
        self.shutdown_calls = 0

    async def startup(self) -> None:
        self.startup_calls += 1

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


def test_app_lifespan_tears_down_after_orchestrator_startup_failure(tmp_path) -> None:
    orchestrator = _StartupFailureOrchestrator()
    app = create_app(
        RuntimeSettings(live_ready=False),
        store=SQLiteStore(tmp_path / "startup-failure.sqlite3"),
        orchestrator=orchestrator,  # type: ignore[arg-type]
    )

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="startup-failure-canary"):
            async with app.router.lifespan_context(app):
                raise AssertionError("A failed startup must not yield application control")

    asyncio.run(exercise())

    cleanup_service = app.state.cleanup_service
    assert not cleanup_service.running
    assert cleanup_service.task is None
    assert orchestrator.shutdown_calls == 1


def test_app_lifespan_tears_down_orchestrator_after_cleanup_startup_failure(
    tmp_path,
    monkeypatch,
) -> None:
    orchestrator = _TrackingStartupOrchestrator()

    async def failing_cleanup_startup(_service: RecoveryCleanupService) -> None:
        raise RuntimeError("cleanup-startup-failure-canary")

    monkeypatch.setattr(RecoveryCleanupService, "startup", failing_cleanup_startup)
    app = create_app(
        RuntimeSettings(live_ready=False),
        store=SQLiteStore(tmp_path / "cleanup-startup-failure.sqlite3"),
        orchestrator=orchestrator,  # type: ignore[arg-type]
    )

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="cleanup-startup-failure-canary"):
            async with app.router.lifespan_context(app):
                raise AssertionError("A failed startup must not yield application control")

    asyncio.run(exercise())

    assert orchestrator.startup_calls == 1
    assert orchestrator.shutdown_calls == 1
    assert app.state.cleanup_service.task is None


def _age_recovery(database_path, recovery_id: str, timestamp: datetime) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE recoveries SET created_at = ?, updated_at = ? WHERE id = ?",
            (timestamp.isoformat(), timestamp.isoformat(), recovery_id),
        )


def _recovery(
    store: SQLiteStore,
    recovery_id: str,
    *,
    status: RecoveryStatus,
) -> None:
    store.create_recovery(
        recovery_id=recovery_id,
        scenario_id=ScenarioId.HOTEL,
        execution_mode=ExecutionMode.REPLAY_FIXTURE,
        current_step=0,
        current_step_summary="Created for cleanup test.",
    )
    if status is not RecoveryStatus.IN_PROGRESS:
        store.record_transition(
            recovery_id,
            status=status,
            current_step=5 if status is not RecoveryStatus.PENDING_APPROVAL else 3,
            current_step_summary="Cleanup state.",
            event_type=f"recovery.{status.value}",
            event_data={"status": status.value},
        )


def test_cleanup_is_terminal_only_bounded_idempotent_and_preserves_aggregates(
    tmp_path,
) -> None:
    database_path = tmp_path / "cleanup.sqlite3"
    store = SQLiteStore(database_path)
    now = datetime(2026, 7, 19, 12, tzinfo=UTC)
    old = now - timedelta(days=8)
    recent = now - timedelta(hours=1)

    for recovery_id, status, timestamp in (
        ("old-completed-1", RecoveryStatus.COMPLETED, old),
        ("old-completed-2", RecoveryStatus.CLOSED_WITHOUT_ACTION, old),
        ("old-in-progress", RecoveryStatus.IN_PROGRESS, old),
        ("old-pending", RecoveryStatus.PENDING_APPROVAL, old),
        ("recent-terminal", RecoveryStatus.COMPLETED, recent),
    ):
        _recovery(store, recovery_id, status=status)
        _age_recovery(database_path, recovery_id, timestamp)

    session_hash = "hmac-sha256:" + "c" * 64
    ip_hash = "hmac-sha256:" + "d" * 64
    store.claim_public_live_admission(
        session_hash=session_hash,
        ip_hash=ip_hash,
        now=now,
        cooldown=timedelta(0),
        daily_budget=3,
        session_expires_at=now + timedelta(days=1),
    )

    first = store.cleanup_terminal_recoveries(
        cutoff=now - timedelta(days=7),
        batch_size=1,
    )
    second = store.cleanup_terminal_recoveries(
        cutoff=now - timedelta(days=7),
        batch_size=1,
    )
    third = store.cleanup_terminal_recoveries(
        cutoff=now - timedelta(days=7),
        batch_size=1,
    )

    assert first == 1
    assert second == 1
    assert third == 0
    assert store.count_recoveries() == 3
    assert store.get_recovery("old-in-progress").status is RecoveryStatus.IN_PROGRESS
    assert store.get_recovery("old-pending").status is RecoveryStatus.PENDING_APPROVAL
    assert store.get_recovery("recent-terminal").status is RecoveryStatus.COMPLETED
    assert store.public_live_usage(
        identity_kind="session",
        identity_hash=session_hash,
        usage_day="2026-07-19",
    ) == 1
    assert store.count_demo_sessions() == 1

    store.reset()
    assert store.count_recoveries() == 0
    assert store.public_live_usage(
        identity_kind="session",
        identity_hash=session_hash,
        usage_day="2026-07-19",
    ) == 1
    assert store.count_demo_sessions() == 1


def test_cleanup_service_owns_one_task_and_joins_it_on_shutdown(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "cleanup-service.sqlite3")
    calls = 0

    original_cleanup = store.cleanup_terminal_recoveries

    def tracked_cleanup(*, cutoff: datetime, batch_size: int) -> int:
        nonlocal calls
        calls += 1
        return original_cleanup(cutoff=cutoff, batch_size=batch_size)

    store.cleanup_terminal_recoveries = tracked_cleanup  # type: ignore[method-assign]
    service = RecoveryCleanupService(
        store=store,
        ttl=timedelta(days=7),
        interval=timedelta(milliseconds=10),
        batch_size=10,
    )

    async def exercise() -> None:
        await service.startup()
        first_task = service.task
        assert first_task is not None
        assert service.running
        await service.startup()
        assert service.task is first_task
        await asyncio.sleep(0.03)
        await service.shutdown()
        assert first_task.done()
        assert not service.running
        assert service.task is None

    asyncio.run(exercise())
    assert calls >= 1


def test_periodic_cleanup_keeps_event_loop_live_during_sqlite_writer_contention(
    tmp_path,
) -> None:
    database_path = tmp_path / "cleanup-liveness.sqlite3"
    store = SQLiteStore(database_path)
    writer_lock_held = Event()
    release_writer = Event()
    cleanup_started = Event()
    cleanup_finished = Event()
    cleanup_results: list[int] = []

    def hold_writer_lock() -> None:
        connection = sqlite3.connect(database_path, timeout=1)
        try:
            connection.execute("BEGIN IMMEDIATE")
            writer_lock_held.set()
            cleanup_started.wait(timeout=2)
            release_writer.wait(timeout=1)
            connection.rollback()
        finally:
            writer_lock_held.clear()
            connection.close()

    original_cleanup = store.cleanup_terminal_recoveries

    def tracked_cleanup(*, cutoff: datetime, batch_size: int) -> int:
        cleanup_started.set()
        return original_cleanup(cutoff=cutoff, batch_size=batch_size)

    store.cleanup_terminal_recoveries = tracked_cleanup  # type: ignore[method-assign]
    service = RecoveryCleanupService(
        store=store,
        ttl=timedelta(days=7),
        interval=timedelta(hours=1),
        batch_size=10,
    )
    original_run_once = service.run_once

    def tracked_run_once() -> int:
        result = original_run_once()
        cleanup_results.append(result)
        cleanup_finished.set()
        return result

    service.run_once = tracked_run_once  # type: ignore[method-assign]

    async def exercise() -> None:
        await service.startup()
        cleanup_task = service.task
        assert cleanup_task is not None

        while not cleanup_started.is_set():
            await asyncio.sleep(0)
        assert writer_lock_held.is_set()
        assert not cleanup_finished.is_set()

        release_writer.set()
        while not cleanup_finished.is_set():
            await asyncio.sleep(0)

        await service.shutdown()
        assert cleanup_task.done()
        assert service.task is None
        assert not service.running

    with ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="cleanup-writer",
    ) as writer_pool:
        writer = writer_pool.submit(hold_writer_lock)
        assert writer_lock_held.wait(timeout=2)
        try:
            asyncio.run(asyncio.wait_for(exercise(), timeout=5))
        finally:
            release_writer.set()
        writer.result(timeout=2)

    assert cleanup_results == [0]
    assert not any(
        thread.name.startswith("cleanup-writer") for thread in enumerate_threads()
    )
    connection = sqlite3.connect(database_path, timeout=0.1)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.rollback()
    finally:
        connection.close()
    store.close()


def test_creation_usage_cleanup_is_retained_bounded_and_graph_independent(
    tmp_path,
) -> None:
    database_path = tmp_path / "creation-usage-cleanup.sqlite3"
    store = SQLiteStore(database_path)
    now = datetime(2026, 7, 21, 12, tzinfo=UTC)
    session_hash = "hmac-sha256:" + "c" * 64
    ip_hash = "hmac-sha256:" + "d" * 64
    for admitted_at in (
        datetime(2026, 7, 11, 12, tzinfo=UTC),
        datetime(2026, 7, 12, 12, tzinfo=UTC),
        datetime(2026, 7, 13, 12, tzinfo=UTC),
        now,
    ):
        store.claim_public_creation_admission(
            session_hash=session_hash,
            ip_hash=ip_hash,
            session_daily_budget=20,
            ip_daily_budget=20,
            global_daily_budget=20,
            now=admitted_at,
        )
    _recovery(store, "current-graph", status=RecoveryStatus.COMPLETED)

    service = RecoveryCleanupService(
        store=store,
        ttl=timedelta(days=7),
        interval=timedelta(hours=1),
        batch_size=3,
        creation_usage_retention=timedelta(days=8),
        clock=lambda: now,
    )

    assert service.run_once() == 3
    assert store.count_public_creation_usage_rows() == 9
    assert store.public_creation_usage(
        identity_kind="session",
        identity_hash=session_hash,
        usage_day="2026-07-13",
    ) == 1
    assert store.public_creation_usage(
        identity_kind="session",
        identity_hash=session_hash,
        usage_day="2026-07-21",
    ) == 1

    store.reset()
    assert store.count_recoveries() == 0
    assert store.count_public_creation_usage_rows() == 9
    assert service.run_once() == 3
    assert store.count_public_creation_usage_rows() == 6
    assert service.run_once() == 0
    assert store.count_public_creation_usage_rows() == 6


def test_creation_usage_survives_session_reset_terminal_deletion_and_reopen(
    tmp_path,
) -> None:
    database_path = tmp_path / "creation-usage-persistence.sqlite3"
    store = SQLiteStore(database_path)
    session_hash = "hmac-sha256:" + "e" * 64
    ip_hash = "hmac-sha256:" + "f" * 64
    now = datetime(2026, 7, 21, 12, tzinfo=UTC)
    store.claim_public_creation_admission(
        session_hash=session_hash,
        ip_hash=ip_hash,
        session_daily_budget=5,
        ip_daily_budget=5,
        global_daily_budget=5,
        now=now,
    )
    store.create_recovery(
        recovery_id="session-owned-creation-graph",
        scenario_id=ScenarioId.HOTEL,
        execution_mode=ExecutionMode.REPLAY_FIXTURE,
        current_step=0,
        current_step_summary="Owned graph for creation usage persistence.",
        session_hash=session_hash,
    )
    store.record_transition(
        "session-owned-creation-graph",
        status=RecoveryStatus.COMPLETED,
        current_step=5,
        current_step_summary="Terminal graph for deletion.",
        event_type="recovery.completed",
        event_data={"status": "completed"},
    )
    _age_recovery(
        database_path,
        "session-owned-creation-graph",
        now - timedelta(days=9),
    )

    store.reset_for_session(session_hash)
    assert store.count_recoveries() == 0
    assert store.count_public_creation_usage_rows() == 3
    _recovery(store, "terminal-cleanup-graph", status=RecoveryStatus.COMPLETED)
    _age_recovery(
        database_path,
        "terminal-cleanup-graph",
        now - timedelta(days=9),
    )
    assert store.cleanup_terminal_recoveries(
        cutoff=now - timedelta(days=7),
        batch_size=1,
    ) == 1
    assert store.count_recoveries() == 0
    assert store.count_public_creation_usage_rows() == 3
    store.close()

    reopened = SQLiteStore(database_path)
    assert reopened.count_public_creation_usage_rows() == 3
    assert reopened.public_creation_usage(
        identity_kind="session",
        identity_hash=session_hash,
        usage_day="2026-07-21",
    ) == 1


def test_creation_usage_cleanup_uses_strict_canonical_cutoff_day(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "creation-cutoff-contract.sqlite3")
    session_hash = "hmac-sha256:" + "6" * 64
    ip_hash = "hmac-sha256:" + "7" * 64
    for admitted_at in (
        datetime(2026, 7, 12, 12, tzinfo=UTC),
        datetime(2026, 7, 13, 12, tzinfo=UTC),
    ):
        store.claim_public_creation_admission(
            session_hash=session_hash,
            ip_hash=ip_hash,
            session_daily_budget=5,
            ip_daily_budget=5,
            global_daily_budget=5,
            now=admitted_at,
        )

    assert store.cleanup_public_creation_usage(
        cutoff_day="2026-07-13",
        batch_size=10,
    ) == 3
    assert store.count_public_creation_usage_rows() == 3
    for invalid in ("20260713", "2026-7-13", "not-a-day"):
        with pytest.raises(ValueError, match="canonical ISO date"):
            store.cleanup_public_creation_usage(
                cutoff_day=invalid,
                batch_size=10,
            )
    with pytest.raises(ValueError, match="batch size"):
        store.cleanup_public_creation_usage(
            cutoff_day="2026-07-13",
            batch_size=0,
        )


def test_creation_usage_cleanup_never_deletes_the_current_utc_day(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "creation-current-day-protection.sqlite3")
    today = datetime.now(UTC).date()
    now = datetime(today.year, today.month, today.day, 12, tzinfo=UTC)
    session_hash = "hmac-sha256:" + "8" * 64
    ip_hash = "hmac-sha256:" + "9" * 64
    store.claim_public_creation_admission(
        session_hash=session_hash,
        ip_hash=ip_hash,
        session_daily_budget=5,
        ip_daily_budget=5,
        global_daily_budget=5,
        now=now,
    )

    assert store.cleanup_public_creation_usage(
        cutoff_day=(today + timedelta(days=1)).isoformat(),
        batch_size=10,
    ) == 0
    assert store.count_public_creation_usage_rows() == 3


def test_cleanup_covers_every_terminal_status_but_not_exact_cutoff_or_nonterminal(
    tmp_path,
) -> None:
    database_path = tmp_path / "cleanup-boundaries.sqlite3"
    store = SQLiteStore(database_path)
    cutoff = datetime(2026, 7, 12, 12, tzinfo=UTC)
    old = cutoff - timedelta(microseconds=1)
    cases = (
        ("old-completed", RecoveryStatus.COMPLETED, old),
        ("old-closed", RecoveryStatus.CLOSED_WITHOUT_ACTION, old),
        ("old-unknown", RecoveryStatus.OUTCOME_UNKNOWN, old),
        ("cutoff-completed", RecoveryStatus.COMPLETED, cutoff),
        ("old-in-progress-boundary", RecoveryStatus.IN_PROGRESS, old),
        ("old-pending-boundary", RecoveryStatus.PENDING_APPROVAL, old),
    )
    for recovery_id, recovery_status, timestamp in cases:
        _recovery(store, recovery_id, status=recovery_status)
        _age_recovery(database_path, recovery_id, timestamp)

    assert store.cleanup_terminal_recoveries(cutoff=cutoff, batch_size=10) == 3
    assert store.cleanup_terminal_recoveries(cutoff=cutoff, batch_size=10) == 0
    assert store.count_recoveries() == 3
    assert store.get_recovery("cutoff-completed").status is RecoveryStatus.COMPLETED
    assert (
        store.get_recovery("old-in-progress-boundary").status
        is RecoveryStatus.IN_PROGRESS
    )
    assert (
        store.get_recovery("old-pending-boundary").status
        is RecoveryStatus.PENDING_APPROVAL
    )


def test_expired_session_cleanup_is_bounded_and_preserves_usage_and_cooldowns(
    tmp_path,
) -> None:
    database_path = tmp_path / "expired-sessions.sqlite3"
    store = SQLiteStore(database_path)
    cleanup_now = datetime(2026, 7, 19, 12, tzinfo=UTC)
    old_admission = cleanup_now - timedelta(days=2)
    recent_admission = cleanup_now - timedelta(hours=1)
    old_sessions = [
        "hmac-sha256:" + "1" * 64,
        "hmac-sha256:" + "2" * 64,
    ]
    recent_session = "hmac-sha256:" + "3" * 64
    ip_hashes = [
        "hmac-sha256:" + "a" * 64,
        "hmac-sha256:" + "b" * 64,
        "hmac-sha256:" + "c" * 64,
    ]
    for session_hash, ip_hash in zip(old_sessions, ip_hashes, strict=False):
        store.claim_public_live_admission(
            session_hash=session_hash,
            ip_hash=ip_hash,
            now=old_admission,
            cooldown=timedelta(0),
            daily_budget=10,
            session_expires_at=old_admission + timedelta(days=1),
        )
    store.claim_public_live_admission(
        session_hash=recent_session,
        ip_hash=ip_hashes[-1],
        now=recent_admission,
        cooldown=timedelta(0),
        daily_budget=10,
        session_expires_at=cleanup_now + timedelta(days=1),
    )

    assert store.cleanup_expired_demo_sessions(cutoff=cleanup_now, batch_size=1) == 1
    assert store.count_demo_sessions() == 2
    assert store.cleanup_expired_demo_sessions(cutoff=cleanup_now, batch_size=1) == 1
    assert store.cleanup_expired_demo_sessions(cutoff=cleanup_now, batch_size=1) == 0
    assert store.count_demo_sessions() == 1
    for session_hash in old_sessions:
        assert store.public_live_usage(
            identity_kind="session",
            identity_hash=session_hash,
            usage_day=old_admission.date().isoformat(),
        ) == 1
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM public_live_cooldowns"
        ).fetchone() == (6,)


def test_inert_replay_migration_is_idempotent_and_preserves_non_replay_runs(
    tmp_path,
) -> None:
    database_path = tmp_path / "inert-replay-migration.sqlite3"
    original = SQLiteStore(database_path)
    inert_replay = original.create_recovery(
        recovery_id="legacy-inert-replay",
        scenario_id=ScenarioId.HOTEL,
        execution_mode=ExecutionMode.REPLAY_FIXTURE,
        current_step=0,
        current_step_summary="Legacy replay started but never terminalized.",
    )
    original.record_transition(
        inert_replay.recovery_id,
        status=RecoveryStatus.IN_PROGRESS,
        current_step=1,
        current_step_summary="Legacy replay has inert recorded evidence.",
        event_type="evidence.recorded",
        event_data={"providerExecution": False},
    )
    sdk_recovery = original.create_recovery(
        recovery_id="sdk-pending-preserved",
        scenario_id=ScenarioId.HOTEL,
        execution_mode=ExecutionMode.SDK_STUB,
        current_step=3,
        current_step_summary="SDK recovery remains pending.",
    )
    original.record_transition(
        sdk_recovery.recovery_id,
        status=RecoveryStatus.PENDING_APPROVAL,
        current_step=3,
        current_step_summary="SDK recovery remains pending.",
        event_type="approval.requested",
        event_data={"providerExecution": False},
    )
    live_recovery = original.create_recovery(
        recovery_id="live-pending-preserved",
        scenario_id=ScenarioId.HOTEL,
        execution_mode=ExecutionMode.OPENAI_LIVE,
        current_step=0,
        current_step_summary="Live recovery remains in progress.",
    )
    original.close()

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "DELETE FROM application_migrations WHERE name = ?",
            ("task8_terminal_replay_fixtures",),
        )

    migrated = SQLiteStore(database_path)
    with pytest.raises(RecoveryNotFoundError):
        migrated.get_recovery(inert_replay.recovery_id)
    assert migrated.get_recovery(sdk_recovery.recovery_id).status is RecoveryStatus.PENDING_APPROVAL
    assert migrated.get_recovery(live_recovery.recovery_id).status is RecoveryStatus.IN_PROGRESS
    migrated.close()

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM events WHERE recovery_id = ?",
            (inert_replay.recovery_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM application_migrations WHERE name = ?",
            ("task8_terminal_replay_fixtures",),
        ).fetchone() == (1,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    reopened = SQLiteStore(database_path)
    assert reopened.count_recoveries() == 2
    reopened.close()


def test_task8_legacy_controls_migrate_once_without_raw_sessions_or_cascading_usage(
    tmp_path,
) -> None:
    database_path = tmp_path / "task8-legacy-controls.sqlite3"
    original = SQLiteStore(database_path)
    replay = ReplayEngine(original, ScenarioLoader()).start(
        "api-quota",
        execution_mode=ExecutionMode.REPLAY_FIXTURE,
    )
    original_receipt = original.get_receipt(replay.recovery_id)
    original.close()
    raw_session = "raw-legacy-session-canary"
    recorded_at = "2026-07-20T00:30:00+02:00"

    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE public_live_cooldowns")
        connection.execute("DROP TABLE usage_ledger")
        connection.execute("DROP TABLE demo_sessions")
        connection.executescript(
            """
            CREATE TABLE usage_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recovery_id TEXT NOT NULL REFERENCES recoveries(id) ON DELETE CASCADE,
                category TEXT NOT NULL,
                amount INTEGER NOT NULL DEFAULT 0,
                recorded_at TEXT NOT NULL
            );
            CREATE TABLE demo_sessions (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            """
            INSERT INTO usage_ledger (recovery_id, category, amount, recorded_at)
            VALUES (?, 'live', 2, ?)
            """,
            (replay.recovery_id, recorded_at),
        )
        connection.execute(
            """
            INSERT INTO demo_sessions (id, created_at, expires_at)
            VALUES (?, ?, ?)
            """,
            (
                raw_session,
                "2026-07-19T20:00:00+00:00",
                "2026-07-20T20:00:00+00:00",
            ),
        )

    migrated = SQLiteStore(database_path)
    assert migrated.get_recovery(replay.recovery_id) == replay
    assert migrated.get_receipt(replay.recovery_id) == original_receipt
    assert migrated.count_demo_sessions() == 0
    migrated.reset()
    migrated.close()

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        migrated_usage = connection.execute(
            """
            SELECT identity_kind, usage_day, amount, last_admitted_at
            FROM usage_ledger
            """
        ).fetchall()
        assert [tuple(row) for row in migrated_usage] == [
            ("legacy", "2026-07-19", 2, None)
        ]
        assert connection.execute("PRAGMA foreign_key_list(usage_ledger)").fetchall() == []
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        persisted = "\n".join(connection.iterdump())
    assert raw_session not in persisted

    reopened = SQLiteStore(database_path)
    reopened.close()
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM usage_ledger").fetchone()[0] == 1


def test_cleanup_racing_terminal_finalization_retains_or_deletes_whole_graph(
    tmp_path,
) -> None:
    database_path = tmp_path / "cleanup-finalization-race.sqlite3"
    finalizer_store = SQLiteStore(database_path)
    orchestrator = RecoveryOrchestrator(
        store=finalizer_store,
        hotel_provider=HotelSimulator(store=finalizer_store),
    )
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    recovery_id = pending.recovery.recovery_id
    approval = pending.recovery.pending_approval
    assert approval is not None
    request = ApprovalDecisionRequest(
        decision="decline",
        clientDecisionId="cleanup-race-decline",
        remedyId=approval.remedy_id,
        remedyDigest=approval.remedy_digest,
        toolCallId=approval.tool_call_id,
    )
    cutoff = datetime(2026, 7, 19, 12, tzinfo=UTC)
    old = cutoff - timedelta(days=8)
    _age_recovery(database_path, recovery_id, old)
    for protected_id, protected_status in (
        ("cleanup-race-in-progress", RecoveryStatus.IN_PROGRESS),
        ("cleanup-race-pending", RecoveryStatus.PENDING_APPROVAL),
    ):
        _recovery(finalizer_store, protected_id, status=protected_status)
        _age_recovery(database_path, protected_id, old)

    # Every durable write in the decline path observes the exact cleanup cutoff.
    finalizer_store._now = lambda: cutoff  # type: ignore[method-assign]
    cleanup_store = SQLiteStore(database_path)
    barrier = Barrier(2)

    def finalize() -> str:
        barrier.wait(timeout=5)
        return asyncio.run(orchestrator.decide(recovery_id, request)).status

    def cleanup() -> int:
        barrier.wait(timeout=5)
        return cleanup_store.cleanup_terminal_recoveries(
            cutoff=cutoff,
            batch_size=10,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        finalization = pool.submit(finalize)
        cleanup_result = pool.submit(cleanup)
        assert finalization.result(timeout=10) == "closed_without_action"
        assert cleanup_result.result(timeout=10) == 0

    # At the exact boundary the complete terminal graph is retained, never a partial
    # state assembled from separately committed receipt/event/decision writes.
    assert (
        cleanup_store.get_recovery(recovery_id).status
        is RecoveryStatus.CLOSED_WITHOUT_ACTION
    )
    assert cleanup_store.get_receipt(recovery_id).recovery_id == recovery_id
    assert cleanup_store.count_decisions(recovery_id) == 1
    assert len(
        [event for event in cleanup_store.list_events(recovery_id) if event.terminal]
    ) == 1
    assert (
        cleanup_store.get_recovery("cleanup-race-in-progress").status
        is RecoveryStatus.IN_PROGRESS
    )
    assert (
        cleanup_store.get_recovery("cleanup-race-pending").status
        is RecoveryStatus.PENDING_APPROVAL
    )

    # Once the fully committed terminal graph is strictly older than the cutoff, its
    # parent and every dependent row are removed together by foreign-key cascades.
    assert cleanup_store.cleanup_terminal_recoveries(
        cutoff=cutoff + timedelta(microseconds=1),
        batch_size=10,
    ) == 1
    with sqlite3.connect(database_path) as connection:
        for table in (
            "recoveries",
            "events",
            "receipts",
            "approval_decisions",
            "pending_approvals",
            "permission_scopes",
            "remedies",
            "executions",
        ):
            assert connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE "
                + ("id = ?" if table == "recoveries" else "recovery_id = ?"),
                (recovery_id,),
            ).fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert cleanup_store.count_recoveries() == 2
