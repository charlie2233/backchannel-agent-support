from __future__ import annotations

import asyncio
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from server.config import RuntimeSettings
from server.controls import (
    ClientIdentity,
    LiveAdmissionCode,
    LiveAdmissionError,
    PublicDemoControls,
)
from server.main import create_app
from server.models import (
    ApprovalDecisionResponse,
    DecisionAction,
    ExecutionMode,
    RecoveryStatus,
    ScenarioId,
)
from server.store import SQLiteStore


def _new_demo_identity(controls: PublicDemoControls) -> ClientIdentity:
    request = Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/health",
            "raw_path": b"/health",
            "query_string": b"",
            "root_path": "",
            "headers": [],
            "client": ("testclient", 50_000),
            "server": ("testserver", 80),
        }
    )
    identity = controls.resolve_client_identity(request)
    assert identity.new_session_cookie is not None
    return identity


class RecordingLiveOrchestrator:
    def __init__(self, store: SQLiteStore, *, terminal: bool = False) -> None:
        self.store = store
        self.terminal = terminal
        self.calls: list[str] = []

    async def start(
        self,
        _scenario_id: str,
        *,
        execution_mode: ExecutionMode,
        recovery_id: str | None = None,
        session_key: str | None = None,
    ) -> Any:
        assert execution_mode is ExecutionMode.OPENAI_LIVE
        assert recovery_id is not None
        self.calls.append(recovery_id)
        recovery = self.store.create_recovery(
            recovery_id=recovery_id,
            scenario_id=ScenarioId.HOTEL,
            execution_mode=ExecutionMode.OPENAI_LIVE,
            current_step=0,
            current_step_summary="Live model start completed.",
            model_ids=["gpt-5.6-luna", "gpt-5.6-terra"],
            root_trace_id=f"trace_{len(self.calls):032x}",
            model_call=True,
            sdk_version="0.18.3",
            protocol_version="backchannel.approval.v1",
            agent_graph_version="backchannel.hotel-agent.live.v1",
            definition_digest="live-definition",
            session_key=session_key,
        )
        if self.terminal:
            recovery = self.store.record_transition(
                recovery_id,
                status=RecoveryStatus.COMPLETED,
                current_step=5,
                current_step_summary="Live recovery completed.",
                event_type="recovery.completed",
                event_data={"summary": "Live recovery completed."},
            )
        return SimpleNamespace(recovery=recovery)


class RecordingLiveDecisionOrchestrator:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store
        self.calls: list[str] = []

    async def approve_decision(self, recovery_id: str, payload: Any) -> Any:
        self.calls.append(recovery_id)
        self.store.record_transition(
            recovery_id,
            status=RecoveryStatus.COMPLETED,
            current_step=5,
            current_step_summary="Guarded live resume completed.",
            event_type="recovery.completed",
            event_data={"summary": "Guarded live resume completed."},
        )
        return ApprovalDecisionResponse(
            action=DecisionAction.APPROVE,
            clientDecisionId=payload.client_decision_id,
            recoveryId=recovery_id,
            status="completed",
            approvedRemedyDigest=payload.remedy_digest,
            executionStarted=True,
        )


class UnboundLiveOrchestrator:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    async def start(
        self,
        _scenario_id: str,
        *,
        execution_mode: ExecutionMode,
        recovery_id: str | None = None,
        session_key: str | None = None,
    ) -> Any:
        assert execution_mode is ExecutionMode.OPENAI_LIVE
        assert recovery_id is not None
        assert session_key is not None
        recovery = self.store.create_recovery(
            recovery_id=recovery_id,
            scenario_id=ScenarioId.HOTEL,
            execution_mode=ExecutionMode.OPENAI_LIVE,
            current_step=0,
            current_step_summary="Intentionally unbound live test recovery.",
            model_ids=["gpt-5.6-luna", "gpt-5.6-terra"],
            root_trace_id="trace_abcdef0123456789abcdef0123456789",
            model_call=True,
            sdk_version="0.18.3",
            protocol_version="backchannel.approval.v1",
            agent_graph_version="backchannel.hotel-agent.live.v1",
            definition_digest="b" * 64,
        )
        return SimpleNamespace(recovery=recovery)


class DemoSessionAccessRecordingStore(SQLiteStore):
    """Records use of the legacy durable demo-session compatibility API."""

    def __init__(self, database_path) -> None:
        self.demo_session_reads = 0
        self.demo_session_writes = 0
        super().__init__(database_path)

    def create_demo_session(
        self,
        session_key: str,
        *,
        created_at: datetime,
        expires_at: datetime,
    ) -> None:
        self.demo_session_writes += 1
        super().create_demo_session(
            session_key,
            created_at=created_at,
            expires_at=expires_at,
        )

    def demo_session_is_active(self, session_key: str, *, now: datetime) -> bool:
        self.demo_session_reads += 1
        return super().demo_session_is_active(session_key, now=now)


@pytest.mark.parametrize(
    ("code", "expected_message"),
    [
        (
            LiveAdmissionCode.LIVE_UNAVAILABLE,
            "Live recovery is unavailable in this demo. "
            "A replay fixture is starting automatically; you can rerun it explicitly.",
        ),
        (
            LiveAdmissionCode.LIVE_CAPACITY,
            "Live recovery is currently at capacity. "
            "A replay fixture is starting automatically; you can rerun it explicitly.",
        ),
        (
            LiveAdmissionCode.COOLDOWN,
            "Please wait before starting another live recovery. "
            "A replay fixture is starting automatically; you can rerun it explicitly.",
        ),
        (
            LiveAdmissionCode.DAILY_BUDGET,
            "The daily live demo budget is currently reached. "
            "A replay fixture is starting automatically; you can rerun it explicitly.",
        ),
    ],
)
def test_live_admission_messages_match_the_public_client_allowlist(
    code: LiveAdmissionCode,
    expected_message: str,
) -> None:
    assert LiveAdmissionError(code).public_message == expected_message


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_concurrent_live_recoveries", 0),
        ("max_concurrent_live_recoveries", 101),
        ("live_ip_cooldown_seconds", 0),
        ("live_ip_cooldown_seconds", 86_401),
        ("live_session_cooldown_seconds", 0),
        ("live_session_cooldown_seconds", 86_401),
        ("daily_demo_budget_units", 0),
        ("daily_demo_budget_units", 1_000_001),
        ("live_admission_lease_seconds", 0),
        ("live_admission_lease_seconds", 86_401),
        ("terminal_recovery_ttl_seconds", 0),
        ("terminal_recovery_ttl_seconds", 2_592_001),
        ("terminal_cleanup_interval_seconds", 0),
        ("terminal_cleanup_interval_seconds", 86_401),
        ("request_body_size_limit_bytes", 0),
        ("request_body_size_limit_bytes", 1_048_577),
        ("max_concurrent_event_streams", 0),
        ("max_concurrent_event_streams", 1_025),
        ("max_event_streams_per_recovery", 0),
        ("max_event_streams_per_recovery", 1_025),
        ("event_stream_retry_seconds", 0),
        ("event_stream_retry_seconds", 301),
        ("demo_session_lifetime_seconds", 0),
        ("demo_session_lifetime_seconds", 604_801),
    ],
)
def test_runtime_settings_reject_nonpositive_or_unbounded_public_limits(
    field: str,
    value: int,
) -> None:
    with pytest.raises(ValueError, match=field):
        RuntimeSettings(live_ready=False, **{field: value})


def test_invalid_integer_environment_limit_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BACKCHANNEL_MAX_CONCURRENT_LIVE_RECOVERIES", "not-an-int")

    with pytest.raises(ValueError, match="BACKCHANNEL_MAX_CONCURRENT_LIVE_RECOVERIES"):
        RuntimeSettings.from_environment()


def test_event_stream_limits_have_bounded_defaults_and_environment_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    defaults = RuntimeSettings(live_ready=False)
    assert defaults.max_concurrent_event_streams == 16
    assert defaults.max_event_streams_per_recovery == 4
    assert defaults.event_stream_retry_seconds == 5

    monkeypatch.setenv("BACKCHANNEL_MAX_CONCURRENT_EVENT_STREAMS", "12")
    monkeypatch.setenv("BACKCHANNEL_MAX_EVENT_STREAMS_PER_RECOVERY", "3")
    monkeypatch.setenv("BACKCHANNEL_EVENT_STREAM_RETRY_SECONDS", "7")
    configured = RuntimeSettings.from_environment()
    assert configured.max_concurrent_event_streams == 12
    assert configured.max_event_streams_per_recovery == 3
    assert configured.event_stream_retry_seconds == 7


def test_per_recovery_event_stream_limit_cannot_exceed_process_limit() -> None:
    with pytest.raises(ValueError, match="max_event_streams_per_recovery"):
        RuntimeSettings(
            live_ready=False,
            max_concurrent_event_streams=4,
            max_event_streams_per_recovery=5,
        )


def test_deployed_mode_requires_explicit_https_origin_and_identity_secret() -> None:
    with pytest.raises(ValueError, match="deployed"):
        RuntimeSettings(live_ready=False, deployed_mode=True)


def test_proxy_trust_requires_valid_explicit_cidrs() -> None:
    with pytest.raises(ValueError, match="trusted_proxy_cidrs"):
        RuntimeSettings(live_ready=False, trusted_proxy_enabled=True)
    with pytest.raises(ValueError, match="trusted_proxy_cidrs"):
        RuntimeSettings(
            live_ready=False,
            trusted_proxy_enabled=True,
            trusted_proxy_cidrs=("not-a-network",),
        )


def test_live_capacity_admission_is_atomic_across_store_instances(tmp_path) -> None:
    database_path = tmp_path / "shared-admission.sqlite3"
    settings = RuntimeSettings(
        live_ready=True,
        max_concurrent_live_recoveries=1,
        live_ip_cooldown_seconds=60,
        live_session_cooldown_seconds=60,
        daily_demo_budget_units=10,
    )
    first = PublicDemoControls(SQLiteStore(database_path), settings)
    second = PublicDemoControls(SQLiteStore(database_path), settings)
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)

    def admit(index: int) -> str:
        try:
            controls = first if index == 0 else second
            controls.admit_live(
                recovery_id=f"race-{index}",
                ip_key=f"ip-{index}",
                session_key=f"session-{index}",
                now=now,
            )
        except LiveAdmissionError as error:
            return error.code.value
        return "accepted"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(admit, range(2)))

    assert sorted(results) == ["accepted", LiveAdmissionCode.LIVE_CAPACITY.value]


def test_ip_and_session_cooldowns_are_independent(tmp_path) -> None:
    settings = RuntimeSettings(
        live_ready=True,
        max_concurrent_live_recoveries=10,
        live_ip_cooldown_seconds=60,
        live_session_cooldown_seconds=60,
        daily_demo_budget_units=10,
    )
    controls = PublicDemoControls(SQLiteStore(tmp_path / "cooldowns.sqlite3"), settings)
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    controls.admit_live(
        recovery_id="first",
        ip_key="ip-one",
        session_key="session-one",
        now=now,
    )

    with pytest.raises(LiveAdmissionError) as same_ip:
        controls.admit_live(
            recovery_id="same-ip",
            ip_key="ip-one",
            session_key="session-two",
            now=now,
        )
    assert same_ip.value.code is LiveAdmissionCode.COOLDOWN

    with pytest.raises(LiveAdmissionError) as same_session:
        controls.admit_live(
            recovery_id="same-session",
            ip_key="ip-two",
            session_key="session-one",
            now=now,
        )
    assert same_session.value.code is LiveAdmissionCode.COOLDOWN


def test_daily_budget_uses_named_demo_units_and_survives_released_admission(tmp_path) -> None:
    settings = RuntimeSettings(
        live_ready=True,
        max_concurrent_live_recoveries=5,
        live_ip_cooldown_seconds=1,
        live_session_cooldown_seconds=1,
        daily_demo_budget_units=1,
    )
    controls = PublicDemoControls(SQLiteStore(tmp_path / "budget.sqlite3"), settings)
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    controls.admit_live(
        recovery_id="budget-first",
        ip_key="ip-one",
        session_key="session-one",
        now=now,
    )
    controls.release_live("budget-first", now=now)

    with pytest.raises(LiveAdmissionError) as blocked:
        controls.admit_live(
            recovery_id="budget-second",
            ip_key="ip-two",
            session_key="session-two",
            now=now,
        )

    assert blocked.value.code is LiveAdmissionCode.DAILY_BUDGET
    assert "money" not in blocked.value.public_message.lower()
    assert "token" not in blocked.value.public_message.lower()


def test_terminal_recovery_releases_durable_capacity_without_deleting_usage(tmp_path) -> None:
    database_path = tmp_path / "terminal-capacity.sqlite3"
    store = SQLiteStore(database_path)
    settings = RuntimeSettings(
        live_ready=True,
        max_concurrent_live_recoveries=1,
        live_ip_cooldown_seconds=60,
        live_session_cooldown_seconds=60,
        daily_demo_budget_units=10,
    )
    controls = PublicDemoControls(store, settings)
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    controls.admit_live(
        recovery_id="finished-live",
        ip_key="ip-one",
        session_key="session-one",
        now=now,
    )
    store.create_recovery(
        recovery_id="finished-live",
        scenario_id=ScenarioId.HOTEL,
        execution_mode=ExecutionMode.OPENAI_LIVE,
        current_step=0,
        current_step_summary="Live recovery started.",
        model_ids=["gpt-5.6-luna", "gpt-5.6-terra"],
        root_trace_id="trace_0123456789abcdef0123456789abcdef",
        model_call=True,
        sdk_version="0.18.3",
        protocol_version="backchannel.approval.v1",
        agent_graph_version="backchannel.hotel-agent.live.v1",
        definition_digest="live-definition",
    )
    store.record_transition(
        "finished-live",
        status=RecoveryStatus.COMPLETED,
        current_step=5,
        current_step_summary="Live recovery completed.",
        event_type="recovery.completed",
        event_data={"summary": "Live recovery completed."},
    )

    controls.admit_live(
        recovery_id="next-live",
        ip_key="ip-two",
        session_key="session-two",
        now=now,
    )

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM usage_ledger"
        ).fetchone() == (2,)


def test_nonterminal_live_admission_outlives_terminal_record_ttl(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "independent-live-lease.sqlite3")
    settings = RuntimeSettings(
        live_ready=True,
        max_concurrent_live_recoveries=1,
        live_ip_cooldown_seconds=1,
        live_session_cooldown_seconds=1,
        daily_demo_budget_units=10,
        terminal_recovery_ttl_seconds=1,
        live_admission_lease_seconds=60,
    )
    controls = PublicDemoControls(store, settings)
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    controls.admit_live(
        recovery_id="long-running-live",
        ip_key="ip-one",
        session_key="session-one",
        now=now,
    )
    store.create_recovery(
        recovery_id="long-running-live",
        scenario_id=ScenarioId.HOTEL,
        execution_mode=ExecutionMode.OPENAI_LIVE,
        current_step=0,
        current_step_summary="Live recovery remains active.",
        model_ids=["gpt-5.6-luna", "gpt-5.6-terra"],
        root_trace_id="trace_0123456789abcdef0123456789abcdef",
        model_call=True,
        sdk_version="0.18.3",
        protocol_version="backchannel.approval.v1",
        agent_graph_version="backchannel.hotel-agent.live.v1",
        definition_digest="live-definition",
    )

    with pytest.raises(LiveAdmissionError) as blocked:
        controls.admit_live(
            recovery_id="second-live",
            ip_key="ip-two",
            session_key="session-two",
            now=now + timedelta(seconds=2),
        )

    assert blocked.value.code is LiveAdmissionCode.LIVE_CAPACITY


def test_expired_live_resume_reacquires_only_when_capacity_is_free_without_rebilling(
    tmp_path,
) -> None:
    database_path = tmp_path / "resume-live-lease.sqlite3"
    store = SQLiteStore(database_path)
    settings = RuntimeSettings(
        live_ready=True,
        max_concurrent_live_recoveries=1,
        live_ip_cooldown_seconds=1,
        live_session_cooldown_seconds=1,
        daily_demo_budget_units=10,
        live_admission_lease_seconds=1,
    )
    controls = PublicDemoControls(store, settings)
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    controls.admit_live(
        recovery_id="pending-live",
        ip_key="ip-one",
        session_key="session-one",
        now=now,
    )
    store.create_recovery(
        recovery_id="pending-live",
        scenario_id=ScenarioId.HOTEL,
        execution_mode=ExecutionMode.OPENAI_LIVE,
        current_step=0,
        current_step_summary="Live recovery awaits a resume.",
        model_ids=["gpt-5.6-luna", "gpt-5.6-terra"],
        root_trace_id="trace_0123456789abcdef0123456789abcdef",
        model_call=True,
        sdk_version="0.18.3",
        protocol_version="backchannel.approval.v1",
        agent_graph_version="backchannel.hotel-agent.live.v1",
        definition_digest="live-definition",
    )
    later = now + timedelta(seconds=2)
    controls.admit_live(
        recovery_id="capacity-holder",
        ip_key="ip-two",
        session_key="session-two",
        now=later,
    )

    with pytest.raises(LiveAdmissionError) as blocked:
        controls.guard_live_resume("pending-live", now=later)
    assert blocked.value.code is LiveAdmissionCode.LIVE_CAPACITY

    controls.release_live("capacity-holder", now=later)
    controls.guard_live_resume("pending-live", now=later)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM usage_ledger"
        ).fetchone() == (2,)


def test_slow_initial_live_start_renews_its_exact_pre_recovery_lease(tmp_path) -> None:
    database_path = tmp_path / "slow-start-lease.sqlite3"
    settings = RuntimeSettings(
        live_ready=True,
        max_concurrent_live_recoveries=1,
        live_ip_cooldown_seconds=1,
        live_session_cooldown_seconds=1,
        daily_demo_budget_units=10,
        live_admission_lease_seconds=1,
    )
    controls = PublicDemoControls(SQLiteStore(database_path), settings)
    contender = PublicDemoControls(SQLiteStore(database_path), settings)
    controls.admit_live(
        recovery_id="slow-start",
        ip_key="ip-one",
        session_key="session-one",
    )

    async def hold_initial_model_call() -> None:
        async with controls.live_model_slot("slow-start"):
            await asyncio.sleep(1.2)
            with pytest.raises(LiveAdmissionError) as blocked:
                contender.admit_live(
                    recovery_id="contender",
                    ip_key="ip-two",
                    session_key="session-two",
                )
            assert blocked.value.code is LiveAdmissionCode.LIVE_CAPACITY

    asyncio.run(hold_initial_model_call())


def test_public_live_decision_path_guards_then_reacquires_expired_lease(tmp_path) -> None:
    database_path = tmp_path / "guarded-live-decision.sqlite3"
    store = SQLiteStore(database_path)
    settings = RuntimeSettings(
        live_ready=True,
        max_concurrent_live_recoveries=1,
        live_ip_cooldown_seconds=1,
        live_session_cooldown_seconds=1,
        daily_demo_budget_units=10,
        live_admission_lease_seconds=1,
    )
    controls = PublicDemoControls(store, settings)
    identity = _new_demo_identity(controls)
    recovery_id = "11111111-2222-4333-8444-555555555555"
    now = datetime.now(UTC)
    controls.admit_live(
        recovery_id=recovery_id,
        ip_key="ip-one",
        session_key=identity.session_key,
        now=now,
    )
    store.create_recovery(
        recovery_id=recovery_id,
        scenario_id=ScenarioId.HOTEL,
        execution_mode=ExecutionMode.OPENAI_LIVE,
        current_step=4,
        current_step_summary="Live recovery awaits a guarded decision.",
        model_ids=["gpt-5.6-luna", "gpt-5.6-terra"],
        root_trace_id="trace_0123456789abcdef0123456789abcdef",
        model_call=True,
        sdk_version="0.18.3",
        protocol_version="backchannel.approval.v1",
        agent_graph_version="backchannel.hotel-agent.live.v1",
        definition_digest="live-definition",
        session_key=identity.session_key,
    )
    later = now + timedelta(seconds=2)
    controls.admit_live(
        recovery_id="capacity-holder",
        ip_key="ip-two",
        session_key="session-two",
        now=later,
    )
    orchestrator = RecordingLiveDecisionOrchestrator(store)
    client = TestClient(
        create_app(
            settings,
            store=store,
            orchestrator=orchestrator,  # type: ignore[arg-type]
        )
    )
    client.cookies.set("backchannel_demo_session", identity.new_session_cookie)
    payload = {
        "action": "approve",
        "clientDecisionId": "guarded-decision",
        "remedyId": "guarded-remedy",
        "remedyDigest": "sha256:" + "0" * 64,
        "toolCallId": "guarded-tool",
    }

    blocked = client.post(f"/api/recoveries/{recovery_id}/decisions", json=payload)
    assert blocked.status_code == 429
    assert blocked.json() == {
        "code": "decision_capacity",
        "message": (
            "Live decision processing is currently at capacity. "
            "Retry the same decision shortly."
        ),
        "requestId": blocked.headers["x-request-id"],
    }
    assert "fallbackExecutionMode" not in blocked.json()
    assert "replay" not in blocked.text.lower()
    assert orchestrator.calls == []

    controls.release_live("capacity-holder", now=later)
    resumed = client.post(f"/api/recoveries/{recovery_id}/decisions", json=payload)
    assert resumed.status_code == 200
    assert orchestrator.calls == [recovery_id]
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM usage_ledger"
        ).fetchone() == (2,)


def test_live_create_postcondition_failure_releases_reserved_admission(tmp_path) -> None:
    database_path = tmp_path / "unbound-live-postcondition.sqlite3"
    store = SQLiteStore(database_path)
    orchestrator = UnboundLiveOrchestrator(store)
    settings = RuntimeSettings(
        live_ready=True,
        max_concurrent_live_recoveries=1,
        live_ip_cooldown_seconds=60,
        live_session_cooldown_seconds=60,
        daily_demo_budget_units=10,
    )
    client = TestClient(
        create_app(
            settings,
            store=store,
            orchestrator=orchestrator,  # type: ignore[arg-type]
        ),
        raise_server_exceptions=False,
    )

    response = client.post(
        "/api/recoveries",
        json={"scenarioId": "hotel", "executionMode": "openai_live"},
    )

    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    with sqlite3.connect(database_path) as connection:
        admission = connection.execute(
            "SELECT recovery_id, released_at FROM live_admissions"
        ).fetchone()
        assert admission is not None
        assert admission[1] is not None
        assert connection.execute(
            "SELECT COUNT(*) FROM recovery_access WHERE recovery_id = ?",
            (admission[0],),
        ).fetchone() == (0,)


def test_direct_asgi_cancel_before_live_persistence_releases_only_admission(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Direct-ASGI cancellation proof; no socket/server-process behavior is claimed."""

    cancellation_marker = "private-cancel-before-live-persistence"
    database_path = tmp_path / "cancelled-live-admission.sqlite3"
    store = SQLiteStore(database_path)
    entered = asyncio.Event()

    class WaitingOneCallOrchestrator:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def start(
            self,
            _scenario_id: str,
            *,
            execution_mode: ExecutionMode,
            recovery_id: str | None = None,
            session_key: str | None = None,
        ) -> Any:
            del session_key
            assert execution_mode is ExecutionMode.OPENAI_LIVE
            assert recovery_id is not None
            self.calls.append(recovery_id)
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("the waiting orchestrator must be cancelled")

    orchestrator = WaitingOneCallOrchestrator()
    app = create_app(
        RuntimeSettings(
            live_ready=True,
            max_concurrent_live_recoveries=1,
            live_ip_cooldown_seconds=60,
            live_session_cooldown_seconds=60,
            daily_demo_budget_units=10,
        ),
        store=store,
        orchestrator=orchestrator,  # type: ignore[arg-type]
    )
    controls = app.state.public_demo_controls
    original_heartbeat = controls._heartbeat_live_lease
    heartbeat_tasks: list[asyncio.Task[None]] = []

    async def recording_heartbeat(recovery_id: str, stop: asyncio.Event) -> None:
        task = asyncio.current_task()
        assert task is not None
        heartbeat_tasks.append(task)
        await original_heartbeat(recovery_id, stop)

    monkeypatch.setattr(controls, "_heartbeat_live_lease", recording_heartbeat)
    caplog.set_level(logging.ERROR)

    async def cancel_waiting_request() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            request_task = asyncio.create_task(
                client.post(
                    "/api/recoveries",
                    json={"scenarioId": "hotel", "executionMode": "openai_live"},
                )
            )
            await asyncio.wait_for(entered.wait(), timeout=1)
            assert len(orchestrator.calls) == 1
            recovery_id = orchestrator.calls[0]
            with sqlite3.connect(database_path) as connection:
                assert connection.execute(
                    "SELECT recovery_id, released_at FROM live_admissions"
                ).fetchone() == (recovery_id, None)
                assert connection.execute(
                    "SELECT COALESCE(SUM(amount), 0) FROM usage_ledger"
                ).fetchone() == (1,)
                for table in (
                    "recoveries",
                    "pending_approvals",
                    "approval_decisions",
                    "recovery_access",
                ):
                    assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)

            request_task.cancel(cancellation_marker)
            with pytest.raises(asyncio.CancelledError) as raised:
                await request_task
            assert raised.value.args == (cancellation_marker,)

            with sqlite3.connect(database_path) as connection:
                admission = connection.execute(
                    "SELECT recovery_id, released_at FROM live_admissions"
                ).fetchone()
                assert admission is not None
                assert admission[0] == recovery_id
                assert admission[1] is not None
                assert connection.execute(
                    "SELECT COALESCE(SUM(amount), 0) FROM usage_ledger"
                ).fetchone() == (1,)
                for table in (
                    "recoveries",
                    "pending_approvals",
                    "approval_decisions",
                    "recovery_access",
                ):
                    assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)

            assert len(heartbeat_tasks) == 1
            assert heartbeat_tasks[0].done()

    asyncio.run(cancel_waiting_request())
    assert cancellation_marker not in caplog.text


def test_foreign_live_decision_cannot_renew_or_reacquire_owner_lease(tmp_path) -> None:
    database_path = tmp_path / "foreign-live-lease.sqlite3"
    store = SQLiteStore(database_path)
    settings = RuntimeSettings(
        live_ready=True,
        max_concurrent_live_recoveries=1,
        live_ip_cooldown_seconds=1,
        live_session_cooldown_seconds=1,
        daily_demo_budget_units=10,
        live_admission_lease_seconds=1,
    )
    orchestrator = RecordingLiveDecisionOrchestrator(store)
    app = create_app(
        settings,
        store=store,
        orchestrator=orchestrator,  # type: ignore[arg-type]
    )
    controls = app.state.public_demo_controls
    owner_identity = _new_demo_identity(controls)
    foreign_identity = _new_demo_identity(controls)
    recovery_id = "66666666-7777-4888-8999-aaaaaaaaaaaa"
    now = datetime.now(UTC)
    controls.admit_live(
        recovery_id=recovery_id,
        ip_key=owner_identity.ip_key,
        session_key=owner_identity.session_key,
        now=now,
    )
    store.create_recovery(
        recovery_id=recovery_id,
        scenario_id=ScenarioId.HOTEL,
        execution_mode=ExecutionMode.OPENAI_LIVE,
        current_step=4,
        current_step_summary="Owner live recovery awaits consent.",
        model_ids=["gpt-5.6-luna", "gpt-5.6-terra"],
        root_trace_id="trace_1234567890abcdef1234567890abcdef",
        model_call=True,
        sdk_version="0.18.3",
        protocol_version="backchannel.approval.v1",
        agent_graph_version="backchannel.hotel-agent.live.v1",
        definition_digest="c" * 64,
        session_key=owner_identity.session_key,
    )
    expired_at = (now - timedelta(seconds=1)).isoformat()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE live_admissions SET expires_at = ? WHERE recovery_id = ?",
            (expired_at, recovery_id),
        )

    client = TestClient(app)
    client.cookies.set(
        "backchannel_demo_session",
        foreign_identity.new_session_cookie,
    )
    response = client.post(
        f"/api/recoveries/{recovery_id}/decisions",
        json={
            "action": "approve",
            "clientDecisionId": "foreign-live-decision",
            "remedyId": "foreign-remedy",
            "remedyDigest": "sha256:" + "0" * 64,
            "toolCallId": "foreign-tool",
        },
    )

    assert response.status_code == 404
    assert response.content == b'{"detail":"Not found"}'
    assert orchestrator.calls == []
    with sqlite3.connect(database_path) as connection:
        admission = connection.execute(
            "SELECT expires_at, released_at FROM live_admissions "
            "WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone()
        assert admission == (expired_at, None)


def test_async_live_model_slots_never_exceed_in_process_limit(tmp_path) -> None:
    settings = RuntimeSettings(live_ready=True, max_concurrent_live_recoveries=2)
    controls = PublicDemoControls(SQLiteStore(tmp_path / "semaphore.sqlite3"), settings)
    active = 0
    maximum_active = 0

    async def worker() -> None:
        nonlocal active, maximum_active
        async with controls.live_model_slot():
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0.01)
            active -= 1

    async def run_workers() -> None:
        await asyncio.gather(*(worker() for _ in range(6)))

    asyncio.run(run_workers())

    assert maximum_active == 2


def _live_app(
    database_path,
    *,
    settings: RuntimeSettings,
    terminal: bool = False,
):
    store = SQLiteStore(database_path)
    orchestrator = RecordingLiveOrchestrator(store, terminal=terminal)
    return create_app(
        settings,
        store=store,
        orchestrator=orchestrator,  # type: ignore[arg-type]
    ), orchestrator


def _post_live(client: TestClient, *, forwarded_for: str | None = None):
    headers = {"X-Forwarded-For": forwarded_for} if forwarded_for is not None else None
    return client.post(
        "/api/recoveries",
        json={"scenarioId": "hotel", "executionMode": "openai_live"},
        headers=headers,
    )


def test_live_unavailable_has_stable_code_and_explicit_replay_offer(tmp_path) -> None:
    client = TestClient(
        create_app(
            RuntimeSettings(live_ready=False),
            store=SQLiteStore(tmp_path / "unavailable.sqlite3"),
        )
    )

    response = _post_live(client)

    assert response.status_code == 422
    assert response.json()["code"] == "live_unavailable"
    assert response.json()["fallbackExecutionMode"] == "replay_fixture"
    assert response.json()["message"] == (
        "Live recovery is unavailable in this demo. "
        "A replay fixture is starting automatically; you can rerun it explicitly."
    )


def test_capacity_and_daily_budget_errors_do_not_expose_internal_keys(tmp_path) -> None:
    capacity_settings = RuntimeSettings(
        live_ready=True,
        max_concurrent_live_recoveries=1,
        daily_demo_budget_units=10,
    )
    database_path = tmp_path / "capacity-api.sqlite3"
    first_app, _ = _live_app(database_path, settings=capacity_settings)
    second_app, second_orchestrator = _live_app(database_path, settings=capacity_settings)
    with TestClient(first_app, client=("198.51.100.1", 5000)) as first_client:
        assert _post_live(first_client).status_code == 201
    with TestClient(second_app, client=("198.51.100.2", 5000)) as second_client:
        capacity = _post_live(second_client)

    assert capacity.status_code == 429
    assert capacity.json()["code"] == "live_capacity"
    assert capacity.json()["fallbackExecutionMode"] == "replay_fixture"
    assert second_orchestrator.calls == []
    serialized_capacity = capacity.text.lower()
    assert "session_key" not in serialized_capacity
    assert "ip_key" not in serialized_capacity

    budget_settings = RuntimeSettings(
        live_ready=True,
        max_concurrent_live_recoveries=5,
        daily_demo_budget_units=1,
    )
    budget_path = tmp_path / "budget-api.sqlite3"
    budget_app, _ = _live_app(budget_path, settings=budget_settings, terminal=True)
    with TestClient(budget_app, client=("198.51.100.3", 5000)) as first_budget:
        assert _post_live(first_budget).status_code == 201
    with TestClient(budget_app, client=("198.51.100.4", 5000)) as second_budget:
        budget = _post_live(second_budget)
    assert budget.status_code == 429
    assert budget.json()["code"] == "daily_budget"
    assert budget.json()["fallbackExecutionMode"] == "replay_fixture"


def test_forwarded_ip_is_ignored_when_proxy_trust_is_disabled(tmp_path) -> None:
    settings = RuntimeSettings(
        live_ready=True,
        max_concurrent_live_recoveries=5,
        daily_demo_budget_units=10,
    )
    app, _ = _live_app(tmp_path / "proxy-disabled.sqlite3", settings=settings)
    with TestClient(app, client=("198.51.100.20", 5000)) as first:
        assert _post_live(first, forwarded_for="203.0.113.1").status_code == 201
    with TestClient(app, client=("198.51.100.20", 5001)) as second:
        response = _post_live(second, forwarded_for="203.0.113.2")

    assert response.status_code == 429
    assert response.json()["code"] == "cooldown"


def test_untrusted_peer_cannot_spoof_forwarded_ip_when_proxy_trust_is_enabled(
    tmp_path,
) -> None:
    settings = RuntimeSettings(
        live_ready=True,
        max_concurrent_live_recoveries=5,
        daily_demo_budget_units=10,
        trusted_proxy_enabled=True,
        trusted_proxy_cidrs=("10.0.0.0/8",),
    )
    app, _ = _live_app(tmp_path / "proxy-untrusted-peer.sqlite3", settings=settings)
    with TestClient(app, client=("198.51.100.20", 5000)) as first:
        assert _post_live(first, forwarded_for="203.0.113.1").status_code == 201
    with TestClient(app, client=("198.51.100.20", 5001)) as second:
        response = _post_live(second, forwarded_for="203.0.113.2")

    assert response.status_code == 429
    assert response.json()["code"] == "cooldown"


def test_trusted_proxy_chain_stops_at_nearest_untrusted_hop(tmp_path) -> None:
    settings = RuntimeSettings(
        live_ready=True,
        max_concurrent_live_recoveries=5,
        daily_demo_budget_units=10,
        trusted_proxy_enabled=True,
        trusted_proxy_cidrs=("10.0.0.0/8",),
    )
    app, _ = _live_app(tmp_path / "proxy-hop-safe.sqlite3", settings=settings)
    with TestClient(app, client=("10.0.0.2", 5000)) as first:
        assert _post_live(
            first,
            forwarded_for="192.0.2.10, 198.51.100.77, 10.0.0.1",
        ).status_code == 201
    with TestClient(app, client=("10.0.0.2", 5001)) as second:
        response = _post_live(
            second,
            forwarded_for="203.0.113.99, 198.51.100.77, 10.0.0.1",
        )

    assert response.status_code == 429
    assert response.json()["code"] == "cooldown"


def test_cookieless_health_requests_do_not_touch_durable_demo_sessions(
    tmp_path,
) -> None:
    database_path = tmp_path / "stateless-health.sqlite3"
    store = DemoSessionAccessRecordingStore(database_path)
    app = create_app(RuntimeSettings(live_ready=False), store=store)

    with TestClient(app) as client:
        for _ in range(32):
            client.cookies.clear()
            assert client.get("/health").status_code == 200
        client.cookies.clear()
        assert client.get("/api/scenarios").status_code == 200
        client.cookies.clear()
        assert client.post(
            "/api/recoveries",
            json={"scenarioId": "api-quota", "executionMode": "replay_fixture"},
        ).status_code == 201
        client.cookies.clear()
        assert client.get("/static/missing.js").status_code == 404

    with sqlite3.connect(database_path) as connection:
        session_rows = connection.execute(
            "SELECT COUNT(*) FROM demo_sessions"
        ).fetchone()[0]

    assert session_rows == 0
    assert store.demo_session_reads == 0
    assert store.demo_session_writes == 0


def test_signed_cookie_reuse_and_invalid_cookie_replacement_are_stateless(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issued_at = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)

    class FrozenDateTime(datetime):
        current = issued_at

        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return cls.current.replace(tzinfo=None)
            return cls.current.astimezone(tz)

    monkeypatch.setattr("server.controls.datetime", FrozenDateTime)
    database_path = tmp_path / "signed-cookie.sqlite3"
    settings = RuntimeSettings(
        live_ready=False,
        deployed_mode=True,
        deployed_cors_origins=("https://demo.example",),
        identity_hash_secret="deployment-identity-secret-that-is-long-enough",
        demo_session_lifetime_seconds=60,
    )
    store = DemoSessionAccessRecordingStore(database_path)
    app = create_app(settings, store=store)

    with TestClient(app, base_url="https://demo.example") as client:
        first = client.get("/health")
        original = first.cookies[settings.demo_session_cookie_name]
        valid = client.get("/health")

        client.cookies.clear()
        tampered = f"{original[:-1]}{'A' if original[-1] != 'A' else 'B'}"
        replaced_tampered = client.get(
            "/health",
            headers={
                "Cookie": f"{settings.demo_session_cookie_name}={tampered}",
            },
        )

        FrozenDateTime.current = issued_at + timedelta(seconds=61)
        client.cookies.clear()
        replaced_expired = client.get(
            "/health",
            headers={
                "Cookie": f"{settings.demo_session_cookie_name}={original}",
            },
        )

    tampered_replacement = replaced_tampered.cookies[
        settings.demo_session_cookie_name
    ]
    expired_replacement = replaced_expired.cookies[
        settings.demo_session_cookie_name
    ]
    cookie_header = first.headers["set-cookie"]
    with sqlite3.connect(database_path) as connection:
        durable_dump = "\n".join(connection.iterdump())

    assert "set-cookie" not in valid.headers
    assert tampered_replacement not in {original, tampered}
    assert expired_replacement != original
    assert "HttpOnly" in cookie_header
    assert "SameSite=Lax" in cookie_header
    assert "Secure" in cookie_header
    assert "Max-Age=60" in cookie_header
    assert original not in durable_dump
    assert tampered not in durable_dump
    assert tampered_replacement not in durable_dump
    assert expired_replacement not in durable_dump
    assert store.demo_session_reads == 0
    assert store.demo_session_writes == 0


def test_stateless_cookie_identity_preserves_live_ip_and_session_cooldowns(
    tmp_path,
) -> None:
    database_path = tmp_path / "stateless-cooldowns.sqlite3"
    settings = RuntimeSettings(
        live_ready=True,
        max_concurrent_live_recoveries=5,
        live_ip_cooldown_seconds=60,
        live_session_cooldown_seconds=60,
        daily_demo_budget_units=10,
    )
    first_store = DemoSessionAccessRecordingStore(database_path)
    first_orchestrator = RecordingLiveOrchestrator(first_store, terminal=True)
    first_app = create_app(
        settings,
        store=first_store,
        orchestrator=first_orchestrator,  # type: ignore[arg-type]
    )

    with TestClient(first_app, client=("198.51.100.1", 5000)) as first_client:
        first = _post_live(first_client)
        session_cookie = first.cookies[settings.demo_session_cookie_name]

    second_store = DemoSessionAccessRecordingStore(database_path)
    second_orchestrator = RecordingLiveOrchestrator(second_store, terminal=True)
    second_app = create_app(
        settings,
        store=second_store,
        orchestrator=second_orchestrator,  # type: ignore[arg-type]
    )
    with TestClient(second_app, client=("198.51.100.2", 5000)) as session_client:
        same_session = session_client.post(
            "/api/recoveries",
            json={"scenarioId": "hotel", "executionMode": "openai_live"},
            headers={
                "Cookie": f"{settings.demo_session_cookie_name}={session_cookie}",
            },
        )
    tampered = f"{session_cookie[:-1]}{'A' if session_cookie[-1] != 'A' else 'B'}"
    with TestClient(second_app, client=("198.51.100.1", 5001)) as ip_client:
        same_ip = ip_client.post(
            "/api/recoveries",
            json={"scenarioId": "hotel", "executionMode": "openai_live"},
            headers={
                "Cookie": f"{settings.demo_session_cookie_name}={tampered}",
            },
        )

    assert first.status_code == 201
    assert same_session.status_code == 429
    assert same_session.json()["code"] == "cooldown"
    assert same_ip.status_code == 429
    assert same_ip.json()["code"] == "cooldown"
    assert first_store.demo_session_reads == 0
    assert first_store.demo_session_writes == 0
    assert second_store.demo_session_reads == 0
    assert second_store.demo_session_writes == 0


def test_store_reopen_clears_legacy_demo_sessions_but_keeps_compatibility_table(
    tmp_path,
) -> None:
    database_path = tmp_path / "legacy-demo-sessions.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE demo_sessions (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            INSERT INTO demo_sessions (id, created_at, expires_at)
            VALUES (
                'legacy-session-row',
                '2026-07-18T12:00:00+00:00',
                '2026-07-20T12:00:00+00:00'
            );
            """
        )

    SQLiteStore(database_path)

    with sqlite3.connect(database_path) as connection:
        table_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'demo_sessions'"
        ).fetchone()
        session_rows = connection.execute(
            "SELECT COUNT(*) FROM demo_sessions"
        ).fetchone()[0]

    assert table_exists is not None
    assert session_rows == 0


def test_opaque_cookie_and_client_addresses_are_not_persisted_raw(tmp_path) -> None:
    database_path = tmp_path / "opaque-identities.sqlite3"
    settings = RuntimeSettings(live_ready=True, max_concurrent_live_recoveries=5)
    app, _ = _live_app(database_path, settings=settings)
    direct_ip = "198.51.100.77"
    forwarded_ip = "203.0.113.88"
    with TestClient(app, client=(direct_ip, 5000)) as client:
        response = _post_live(client, forwarded_for=forwarded_ip)
        cookie_value = response.cookies[settings.demo_session_cookie_name]
        cookie_nonce = cookie_value.split(".")[2]

    with sqlite3.connect(database_path) as connection:
        durable_dump = "\n".join(connection.iterdump())

    assert response.status_code == 201
    assert direct_ip not in durable_dump
    assert forwarded_ip not in durable_dump
    assert cookie_value not in durable_dump
    assert cookie_nonce not in durable_dump
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=Lax" in response.headers["set-cookie"]


def test_deployed_cookie_is_secure_and_has_bounded_lifetime(tmp_path) -> None:
    settings = RuntimeSettings(
        live_ready=False,
        deployed_mode=True,
        deployed_cors_origins=("https://demo.example",),
        identity_hash_secret="deployment-identity-secret-that-is-long-enough",
        demo_session_lifetime_seconds=3_600,
    )
    client = TestClient(
        create_app(settings, store=SQLiteStore(tmp_path / "secure-cookie.sqlite3"))
    )

    response = client.get("/health")
    cookie = response.headers["set-cookie"]

    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie
    assert "Secure" in cookie
    assert "Max-Age=3600" in cookie


def test_deployed_cors_is_exact_and_security_headers_preserve_sse(tmp_path) -> None:
    settings = RuntimeSettings(
        live_ready=False,
        deployed_mode=True,
        deployed_cors_origins=("https://demo.example",),
        identity_hash_secret="deployment-identity-secret-that-is-long-enough",
    )
    client = TestClient(
        create_app(settings, store=SQLiteStore(tmp_path / "cors-sse.sqlite3")),
        base_url="https://testserver",
    )

    allowed = client.options(
        "/api/recoveries",
        headers={
            "Origin": "https://demo.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    denied = client.options(
        "/api/recoveries",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    recovery = client.post(
        "/api/recoveries",
        json={"scenarioId": "api-quota", "executionMode": "replay_fixture"},
    ).json()
    stream = client.get(f"/api/recoveries/{recovery['recoveryId']}/events")

    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "https://demo.example"
    assert "POST" in allowed.headers["access-control-allow-methods"]
    assert "PUT" not in allowed.headers["access-control-allow-methods"]
    assert "access-control-allow-origin" not in denied.headers
    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert stream.headers["cache-control"] == "no-cache"
    assert stream.headers["x-accel-buffering"] == "no"
    assert stream.headers["x-content-type-options"] == "nosniff"
    assert stream.headers["strict-transport-security"].startswith("max-age=")
    assert "id: " in stream.text
