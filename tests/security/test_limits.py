from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from server.config import RuntimeSettings
from server.controls import LiveAdmissionCode, LiveAdmissionError, PublicDemoControls
from server.main import create_app
from server.models import ExecutionMode, RecoveryStatus, ScenarioId
from server.store import SQLiteStore


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
        ("terminal_recovery_ttl_seconds", 0),
        ("terminal_recovery_ttl_seconds", 2_592_001),
        ("request_body_size_limit_bytes", 0),
        ("request_body_size_limit_bytes", 1_048_577),
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


def test_deployed_mode_requires_explicit_https_origin_and_identity_secret() -> None:
    with pytest.raises(ValueError, match="deployed"):
        RuntimeSettings(live_ready=False, deployed_mode=True)


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
    assert "explicitly run the replay fixture" in response.json()["message"]


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


@pytest.mark.parametrize(
    ("trusted_proxy_enabled", "expected_status", "expected_code"),
    [
        (False, 429, "cooldown"),
        (True, 201, None),
    ],
)
def test_forwarded_ip_is_ignored_unless_proxy_trust_is_explicitly_enabled(
    tmp_path,
    trusted_proxy_enabled: bool,
    expected_status: int,
    expected_code: str | None,
) -> None:
    settings = RuntimeSettings(
        live_ready=True,
        max_concurrent_live_recoveries=5,
        daily_demo_budget_units=10,
        trusted_proxy_enabled=trusted_proxy_enabled,
    )
    app, _ = _live_app(tmp_path / f"proxy-{trusted_proxy_enabled}.sqlite3", settings=settings)
    with TestClient(app, client=("198.51.100.20", 5000)) as first:
        assert _post_live(first, forwarded_for="203.0.113.1").status_code == 201
    with TestClient(app, client=("198.51.100.20", 5001)) as second:
        response = _post_live(second, forwarded_for="203.0.113.2")

    assert response.status_code == expected_status
    if expected_code is not None:
        assert response.json()["code"] == expected_code


def test_opaque_cookie_and_client_addresses_are_not_persisted_raw(tmp_path) -> None:
    database_path = tmp_path / "opaque-identities.sqlite3"
    settings = RuntimeSettings(live_ready=True, max_concurrent_live_recoveries=5)
    app, _ = _live_app(database_path, settings=settings)
    direct_ip = "198.51.100.77"
    forwarded_ip = "203.0.113.88"
    with TestClient(app, client=(direct_ip, 5000)) as client:
        response = _post_live(client, forwarded_for=forwarded_ip)
        cookie_value = response.cookies[settings.demo_session_cookie_name]

    with sqlite3.connect(database_path) as connection:
        durable_dump = "\n".join(connection.iterdump())

    assert response.status_code == 201
    assert direct_ip not in durable_dump
    assert forwarded_ip not in durable_dump
    assert cookie_value not in durable_dump
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
        create_app(settings, store=SQLiteStore(tmp_path / "cors-sse.sqlite3"))
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
