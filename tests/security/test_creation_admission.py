from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta, timezone
from threading import Barrier, Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from server.config import RuntimeSettings
from server.controls import (
    LiveConcurrencyLimitError,
    PublicCreationAdmissionError,
    PublicIdentityHasher,
)
from server.main import create_app
from server.models import ExecutionMode, RecoverySnapshot, ScenarioId
from server.store import SQLiteStore

_SECRET = "test-identity-secret-that-is-at-least-32-bytes"
_SESSION_A = "hmac-sha256:" + "1" * 64
_SESSION_B = "hmac-sha256:" + "2" * 64
_IP_A = "hmac-sha256:" + "a" * 64
_IP_B = "hmac-sha256:" + "b" * 64


def _settings(**overrides: object) -> RuntimeSettings:
    values: dict[str, object] = {
        "live_ready": False,
        "identity_hmac_secret": _SECRET,
        "live_cooldown": timedelta(0),
        "cleanup_interval": timedelta(hours=1),
    }
    values.update(overrides)
    return RuntimeSettings(**values)  # type: ignore[arg-type]


def _creation_rows(database_path) -> list[tuple[object, ...]]:
    with sqlite3.connect(database_path) as connection:
        return connection.execute(
            """
            SELECT identity_kind, identity_hash, usage_day, amount, updated_at
            FROM public_creation_usage
            ORDER BY identity_kind, identity_hash, usage_day
            """
        ).fetchall()


def test_creation_settings_defaults_environment_and_validation(monkeypatch) -> None:
    defaults = RuntimeSettings(live_ready=False)
    assert defaults.creation_session_daily_budget == 12
    assert defaults.creation_ip_daily_budget == 60
    assert defaults.creation_global_daily_budget == 120
    assert defaults.creation_usage_retention == timedelta(days=8)

    monkeypatch.setenv("BACKCHANNEL_CREATION_SESSION_DAILY_BUDGET", "3")
    monkeypatch.setenv("BACKCHANNEL_CREATION_IP_DAILY_BUDGET", "4")
    monkeypatch.setenv("BACKCHANNEL_CREATION_GLOBAL_DAILY_BUDGET", "5")
    monkeypatch.setenv("BACKCHANNEL_CREATION_USAGE_RETENTION_SECONDS", "777600")
    configured = RuntimeSettings.from_environment()
    assert configured.creation_session_daily_budget == 3
    assert configured.creation_ip_daily_budget == 4
    assert configured.creation_global_daily_budget == 5
    assert configured.creation_usage_retention == timedelta(days=9)

    RuntimeSettings(live_ready=False, creation_session_daily_budget=0)
    RuntimeSettings(live_ready=False, creation_ip_daily_budget=0)
    RuntimeSettings(live_ready=False, creation_global_daily_budget=0)
    for field in (
        "creation_session_daily_budget",
        "creation_ip_daily_budget",
        "creation_global_daily_budget",
    ):
        with pytest.raises(ValueError, match="creation"):
            RuntimeSettings(live_ready=False, **{field: -1})
    with pytest.raises(ValueError, match="retention"):
        RuntimeSettings(
            live_ready=False,
            creation_usage_retention=timedelta(days=1) - timedelta(seconds=1),
        )
    RuntimeSettings(
        live_ready=False,
        creation_session_daily_budget=100,
        creation_ip_daily_budget=1,
        creation_global_daily_budget=50,
    )

    monkeypatch.setenv("BACKCHANNEL_CREATION_SESSION_DAILY_BUDGET", "-1")
    with pytest.raises(
        ValueError,
        match="BACKCHANNEL_CREATION_SESSION_DAILY_BUDGET",
    ):
        RuntimeSettings.from_environment()
    monkeypatch.setenv("BACKCHANNEL_CREATION_SESSION_DAILY_BUDGET", "1")
    monkeypatch.setenv("BACKCHANNEL_CREATION_USAGE_RETENTION_SECONDS", "86399")
    with pytest.raises(
        ValueError,
        match="BACKCHANNEL_CREATION_USAGE_RETENTION_SECONDS",
    ):
        RuntimeSettings.from_environment()


def test_replay_creation_budget_charges_once_and_returns_one_generic_429(
    tmp_path,
) -> None:
    database_path = tmp_path / "replay-creation-budget.sqlite3"
    store = SQLiteStore(database_path)
    settings = _settings(
        creation_session_daily_budget=2,
        creation_ip_daily_budget=10,
        creation_global_daily_budget=10,
    )
    payload = {"scenarioId": "hotel", "executionMode": "replay_fixture"}

    with TestClient(create_app(settings, store=store)) as client:
        first = client.post("/api/recoveries", json=payload)
        second = client.post("/api/recoveries", json=payload)
        exhausted = client.post("/api/recoveries", json=payload)

    assert first.status_code == second.status_code == 201
    assert exhausted.status_code == 429
    assert exhausted.json()["error"] == {
        "code": "creation_daily_budget_exceeded",
        "message": "The public demo recovery creation budget is exhausted for today.",
        "requestId": exhausted.headers["x-request-id"],
        "recoveryId": None,
        "retryAfterSeconds": int(exhausted.headers["retry-after"]),
        "fallback": None,
    }
    assert 1 <= int(exhausted.headers["retry-after"]) <= 86_400
    assert "set-cookie" not in exhausted.headers
    assert "session" not in exhausted.text.lower()
    assert "global" not in exhausted.text.lower()
    assert "count" not in exhausted.text.lower()
    assert store.count_recoveries() == 2
    rows = _creation_rows(database_path)
    assert len(rows) == 3
    assert {row[0] for row in rows} == {"session", "ip", "global"}
    assert {row[3] for row in rows} == {2}


def test_zero_creation_budget_is_a_cookie_free_kill_switch(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "creation-kill-switch.sqlite3")
    settings = _settings(creation_global_daily_budget=0)

    with TestClient(create_app(settings, store=store)) as client:
        response = client.post(
            "/api/recoveries",
            json={"scenarioId": "hotel", "executionMode": "replay_fixture"},
        )

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "creation_daily_budget_exceeded"
    assert "set-cookie" not in response.headers
    assert store.count_recoveries() == 0
    assert store.count_public_creation_usage_rows() == 0


def test_unsupported_or_invalid_creation_requests_never_charge(tmp_path) -> None:
    database_path = tmp_path / "uncharged-invalid-creation.sqlite3"
    store = SQLiteStore(database_path)
    settings = _settings(
        deployed=True,
        cors_origins=("https://demo.example",),
    )
    origin = {"Origin": "https://demo.example"}

    with TestClient(create_app(settings, store=store)) as client:
        responses = (
            client.post(
                "/api/recoveries",
                content=b'{"scenarioId":',
                headers={"Content-Type": "application/json", **origin},
            ),
            client.post(
                "/api/recoveries",
                json={
                    "scenarioId": "hotel",
                    "executionMode": "replay_fixture",
                    "extra": True,
                },
                headers=origin,
            ),
            client.post(
                "/api/recoveries",
                json={"scenarioId": "unknown", "executionMode": "replay_fixture"},
                headers=origin,
            ),
            client.post(
                "/api/recoveries",
                json={"scenarioId": "api-quota", "executionMode": "openai_live"},
                headers=origin,
            ),
            client.post(
                "/api/recoveries",
                json={"scenarioId": "hotel", "executionMode": "sdk_stub"},
                headers=origin,
            ),
            client.post(
                "/api/recoveries",
                json={"scenarioId": "hotel", "executionMode": "openai_live"},
                headers=origin,
            ),
        )

    assert [response.status_code for response in responses] == [422, 422, 422, 422, 422, 503]
    assert all("set-cookie" not in response.headers for response in responses)
    assert store.count_recoveries() == 0
    assert store.count_public_creation_usage_rows() == 0
    assert _creation_rows(database_path) == []


def test_ip_budget_spans_distinct_provisional_sessions_without_partial_loser(
    tmp_path,
) -> None:
    database_path = tmp_path / "creation-ip-budget.sqlite3"
    store = SQLiteStore(database_path)
    settings = _settings(
        creation_session_daily_budget=10,
        creation_ip_daily_budget=1,
        creation_global_daily_budget=10,
    )
    payload = {"scenarioId": "api-quota", "executionMode": "replay_fixture"}

    with TestClient(create_app(settings, store=store)) as first_client:
        first = first_client.post("/api/recoveries", json=payload)
    with TestClient(create_app(settings, store=store)) as second_client:
        second = second_client.post("/api/recoveries", json=payload)

    assert first.status_code == 201
    assert second.status_code == 429
    assert "set-cookie" not in second.headers
    rows = _creation_rows(database_path)
    assert len(rows) == 3
    assert {row[0] for row in rows} == {"session", "ip", "global"}
    assert {row[3] for row in rows} == {1}
    assert store.count_recoveries() == 1


def test_ip_budget_spans_two_preexisting_distinct_signed_sessions(tmp_path) -> None:
    database_path = tmp_path / "creation-ip-signed-sessions.sqlite3"
    store = SQLiteStore(database_path)
    settings = _settings(
        creation_session_daily_budget=10,
        creation_ip_daily_budget=1,
        creation_global_daily_budget=10,
    )
    hasher = PublicIdentityHasher(_SECRET)
    codec = hasher.demo_session_cookie_codec(
        lifetime_seconds=int(settings.demo_session_ttl.total_seconds())
    )
    now = datetime.now(UTC)
    first_cookie, first_credential = codec.mint(now=now)
    second_cookie, second_credential = codec.mint(now=now)
    payload = {"scenarioId": "hotel", "executionMode": "replay_fixture"}

    with TestClient(create_app(settings, store=store)) as first_client:
        first_client.cookies.set("backchannel_demo_session", first_cookie)
        first = first_client.post("/api/recoveries", json=payload)
    with TestClient(create_app(settings, store=store)) as second_client:
        second_client.cookies.set("backchannel_demo_session", second_cookie)
        second = second_client.post("/api/recoveries", json=payload)

    assert first.status_code == 201
    assert second.status_code == 429
    assert "set-cookie" not in first.headers
    assert "set-cookie" not in second.headers
    usage_day = now.date().isoformat()
    assert store.public_creation_usage(
        identity_kind="session",
        identity_hash=hasher.session(first_credential.nonce),
        usage_day=usage_day,
    ) == 1
    assert store.public_creation_usage(
        identity_kind="session",
        identity_hash=hasher.session(second_credential.nonce),
        usage_day=usage_day,
    ) == 0
    rows = _creation_rows(database_path)
    assert len(rows) == 3
    assert {row[0] for row in rows} == {"session", "ip", "global"}


def test_two_store_global_creation_race_has_one_winner_and_no_partial_loser(
    tmp_path,
) -> None:
    database_path = tmp_path / "creation-global-race.sqlite3"
    first_store = SQLiteStore(database_path)
    second_store = SQLiteStore(database_path)
    barrier = Barrier(2)
    now = datetime(2026, 7, 21, 10, tzinfo=UTC)

    def claim(item: tuple[SQLiteStore, str, str]) -> str:
        store, session_hash, ip_hash = item
        barrier.wait(timeout=5)
        try:
            store.claim_public_creation_admission(
                session_hash=session_hash,
                ip_hash=ip_hash,
                session_daily_budget=5,
                ip_daily_budget=5,
                global_daily_budget=1,
                now=now,
            )
        except PublicCreationAdmissionError as error:
            assert 1 <= error.retry_after_seconds <= 86_400
            return error.code
        return "admitted"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                claim,
                (
                    (first_store, _SESSION_A, _IP_A),
                    (second_store, _SESSION_B, _IP_B),
                ),
            )
        )

    assert sorted(outcomes) == ["admitted", "creation_daily_budget_exceeded"]
    rows = _creation_rows(database_path)
    assert len(rows) == 3
    assert {row[0] for row in rows} == {"session", "ip", "global"}
    assert {row[3] for row in rows} == {1}


def test_creation_day_is_sampled_after_waiting_for_sqlite_writer_lock(tmp_path) -> None:
    database_path = tmp_path / "creation-midnight-contention.sqlite3"
    store = SQLiteStore(database_path)
    before_midnight = datetime(2026, 7, 21, 23, 59, 59, tzinfo=UTC)
    after_midnight = datetime(2026, 7, 22, 0, 0, 1, tzinfo=UTC)
    clock = {"now": before_midnight}
    clock_called = Event()
    claim_entered = Event()

    def store_clock() -> datetime:
        clock_called.set()
        return clock["now"]

    store._now = store_clock  # type: ignore[method-assign]
    locker = sqlite3.connect(database_path, timeout=5)
    locker.execute("BEGIN IMMEDIATE")

    def claim() -> None:
        claim_entered.set()
        store.claim_public_creation_admission(
            session_hash=_SESSION_A,
            ip_hash=_IP_A,
            session_daily_budget=1,
            ip_daily_budget=1,
            global_daily_budget=1,
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(claim)
        assert claim_entered.wait(timeout=2)
        clock_called.wait(timeout=0.2)
        clock["now"] = after_midnight
        locker.commit()
        future.result(timeout=5)
    locker.close()

    assert store.public_creation_usage(
        identity_kind="session",
        identity_hash=_SESSION_A,
        usage_day="2026-07-21",
    ) == 0
    assert store.public_creation_usage(
        identity_kind="session",
        identity_hash=_SESSION_A,
        usage_day="2026-07-22",
    ) == 1


@pytest.mark.parametrize("failed_kind", ["ip", "global"])
def test_creation_upsert_failure_rolls_back_every_identity(
    tmp_path,
    failed_kind: str,
) -> None:
    database_path = tmp_path / f"creation-upsert-{failed_kind}.sqlite3"
    store = SQLiteStore(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            f"""
            CREATE TRIGGER fail_creation_{failed_kind}
            BEFORE INSERT ON public_creation_usage
            WHEN NEW.identity_kind = '{failed_kind}'
            BEGIN
                SELECT RAISE(ABORT, 'injected creation upsert failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected creation"):
        store.claim_public_creation_admission(
            session_hash=_SESSION_A,
            ip_hash=_IP_A,
            session_daily_budget=5,
            ip_daily_budget=5,
            global_daily_budget=5,
            now=datetime(2026, 7, 21, 12, tzinfo=UTC),
        )

    assert _creation_rows(database_path) == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"session_hash": "raw-session"},
        {"ip_hash": "203.0.113.9"},
        {"now": datetime(2026, 7, 21, 12)},
        {
            "now": datetime(
                2026,
                7,
                21,
                12,
                tzinfo=timezone(timedelta(hours=1)),
            )
        },
        {"session_daily_budget": -1},
        {"ip_daily_budget": -1},
        {"global_daily_budget": -1},
    ],
)
def test_creation_claim_rejects_invalid_inputs_without_rows(
    tmp_path,
    overrides: dict[str, object],
) -> None:
    store = SQLiteStore(tmp_path / "invalid-creation-claim.sqlite3")
    arguments: dict[str, object] = {
        "session_hash": _SESSION_A,
        "ip_hash": _IP_A,
        "session_daily_budget": 5,
        "ip_daily_budget": 5,
        "global_daily_budget": 5,
        "now": datetime(2026, 7, 21, 12, tzinfo=UTC),
    }
    arguments.update(overrides)

    with pytest.raises(ValueError):
        store.claim_public_creation_admission(**arguments)  # type: ignore[arg-type]

    assert store.count_public_creation_usage_rows() == 0


def test_creation_usage_rolls_over_at_utc_midnight_and_is_separate_from_live(
    tmp_path,
) -> None:
    store = SQLiteStore(tmp_path / "creation-rollover.sqlite3")
    before_midnight = datetime(2026, 7, 21, 23, 59, 59, 500_000, tzinfo=UTC)
    store.claim_public_creation_admission(
        session_hash=_SESSION_A,
        ip_hash=_IP_A,
        session_daily_budget=1,
        ip_daily_budget=1,
        global_daily_budget=1,
        now=before_midnight,
    )
    with pytest.raises(PublicCreationAdmissionError) as caught:
        store.claim_public_creation_admission(
            session_hash=_SESSION_A,
            ip_hash=_IP_A,
            session_daily_budget=1,
            ip_daily_budget=1,
            global_daily_budget=1,
            now=before_midnight,
        )
    assert caught.value.code == "creation_daily_budget_exceeded"
    assert caught.value.retry_after_seconds == 1

    after_midnight = datetime(2026, 7, 22, 0, 0, tzinfo=UTC)
    store.claim_public_creation_admission(
        session_hash=_SESSION_A,
        ip_hash=_IP_A,
        session_daily_budget=1,
        ip_daily_budget=1,
        global_daily_budget=1,
        now=after_midnight,
    )
    assert store.public_creation_usage(
        identity_kind="session",
        identity_hash=_SESSION_A,
        usage_day="2026-07-21",
    ) == 1
    assert store.public_creation_usage(
        identity_kind="session",
        identity_hash=_SESSION_A,
        usage_day="2026-07-22",
    ) == 1
    assert store.public_live_usage(
        identity_kind="session",
        identity_hash=_SESSION_A,
        usage_day="2026-07-21",
    ) == 0

    store.claim_public_live_admission(
        session_hash=_SESSION_A,
        ip_hash=_IP_A,
        cooldown=timedelta(0),
        daily_budget=3,
        now=after_midnight,
        session_expires_at=after_midnight + timedelta(days=1),
    )
    assert store.public_live_usage(
        identity_kind="session",
        identity_hash=_SESSION_A,
        usage_day="2026-07-22",
    ) == 1
    assert store.public_creation_usage(
        identity_kind="session",
        identity_hash=_SESSION_A,
        usage_day="2026-07-22",
    ) == 1


class _FailingCreationOrchestrator:
    async def startup(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def start(
        self,
        _scenario_id: object,
        *,
        execution_mode: ExecutionMode,
        session_hash: str | None = None,
    ) -> object:
        assert execution_mode is ExecutionMode.SDK_STUB
        assert session_hash is not None
        raise RuntimeError("post-admission-orchestrator-failure")


def test_supported_orchestrator_failure_stays_charged_without_a_graph(tmp_path) -> None:
    database_path = tmp_path / "charged-orchestrator-failure.sqlite3"
    store = SQLiteStore(database_path)
    app = create_app(
        _settings(),
        store=store,
        orchestrator=_FailingCreationOrchestrator(),  # type: ignore[arg-type]
    )

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/recoveries",
            json={"scenarioId": "hotel", "executionMode": "sdk_stub"},
        )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "set-cookie" not in response.headers
    assert store.count_recoveries() == 0
    rows = _creation_rows(database_path)
    assert len(rows) == 3
    assert {row[3] for row in rows} == {1}


@pytest.mark.parametrize(
    ("scenario_id", "execution_mode"),
    [
        ("hotel", "replay_fixture"),
        ("api-quota", "replay_fixture"),
        ("hotel", "sdk_stub"),
        ("api-quota", "sdk_stub"),
    ],
)
def test_each_supported_keyless_creation_path_charges_exactly_once(
    tmp_path,
    scenario_id: str,
    execution_mode: str,
) -> None:
    database_path = tmp_path / f"creation-{scenario_id}-{execution_mode}.sqlite3"
    store = SQLiteStore(database_path)

    with TestClient(create_app(_settings(), store=store)) as client:
        response = client.post(
            "/api/recoveries",
            json={"scenarioId": scenario_id, "executionMode": execution_mode},
        )

    assert response.status_code == 201
    assert store.count_recoveries() == 1
    rows = _creation_rows(database_path)
    assert len(rows) == 3
    assert {row[3] for row in rows} == {1}


class _LiveCreationOrchestrator:
    def __init__(self) -> None:
        self.start_calls = 0

    async def startup(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def start(
        self,
        scenario_id: object,
        *,
        execution_mode: ExecutionMode,
        session_hash: str | None = None,
    ) -> SimpleNamespace:
        assert ScenarioId(scenario_id) is ScenarioId.HOTEL
        assert execution_mode is ExecutionMode.OPENAI_LIVE
        assert session_hash is not None
        self.start_calls += 1
        now = datetime.now(UTC)
        return SimpleNamespace(
            recovery=RecoverySnapshot(
                recoveryId="11111111-2222-4333-8444-555555555555",
                scenarioId=ScenarioId.HOTEL,
                executionMode=ExecutionMode.OPENAI_LIVE,
                status="pending_approval",
                currentStep=3,
                currentStepSummary="Live admission contract test.",
                createdAt=now,
                updatedAt=now,
                pendingApproval=None,
                rootTraceId="trace_11111111111111111111111111111111",
                modelIds=["gpt-test-returned"],
                sdkVersion="0.18.3",
                protocolVersion="backchannel.approval.v1",
                agentGraphVersion="backchannel.hotel-live-agent.v1",
                promptToolSchemaHash="a" * 64,
            )
        )


def test_live_capacity_follows_general_creation_charge_and_general_429_stops_early(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "creation-before-live-capacity.sqlite3"
    store = SQLiteStore(database_path)
    orchestrator = _LiveCreationOrchestrator()
    app = create_app(
        _settings(
            live_ready=True,
            creation_session_daily_budget=5,
            creation_ip_daily_budget=5,
            creation_global_daily_budget=1,
        ),
        store=store,
        orchestrator=orchestrator,  # type: ignore[arg-type]
    )
    gate_entries = 0

    @asynccontextmanager
    async def saturated_slot() -> AsyncIterator[None]:
        nonlocal gate_entries
        gate_entries += 1
        raise LiveConcurrencyLimitError
        yield

    monkeypatch.setattr(app.state.live_gate, "slot", saturated_slot)
    payload = {"scenarioId": "hotel", "executionMode": "openai_live"}

    with TestClient(app) as client:
        capacity = client.post("/api/recoveries", json=payload)
        general_exhausted = client.post("/api/recoveries", json=payload)

    assert capacity.status_code == 429
    assert capacity.json()["error"]["code"] == "live_capacity_reached"
    assert general_exhausted.status_code == 429
    assert general_exhausted.json()["error"]["code"] == (
        "creation_daily_budget_exceeded"
    )
    assert "set-cookie" not in general_exhausted.headers
    assert gate_entries == 1
    assert orchestrator.start_calls == 0
    assert store.count_public_live_usage_rows() == 0
    rows = _creation_rows(database_path)
    assert len(rows) == 3
    assert {row[3] for row in rows} == {1}


def test_valid_live_creation_charges_general_and_live_ledgers_once(tmp_path) -> None:
    database_path = tmp_path / "valid-live-creation.sqlite3"
    store = SQLiteStore(database_path)
    orchestrator = _LiveCreationOrchestrator()
    app = create_app(
        _settings(live_ready=True),
        store=store,
        orchestrator=orchestrator,  # type: ignore[arg-type]
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/recoveries",
            json={"scenarioId": "hotel", "executionMode": "openai_live"},
        )

    assert response.status_code == 201
    assert orchestrator.start_calls == 1
    assert len(_creation_rows(database_path)) == 3
    assert store.count_public_live_usage_rows() == 2


def test_creation_route_openapi_declares_generic_429_and_retry_after(tmp_path) -> None:
    app = create_app(_settings(), store=SQLiteStore(tmp_path / "openapi.sqlite3"))
    response = app.openapi()["paths"]["/api/recoveries"]["post"]["responses"]["429"]

    assert response["description"] == (
        "The request exceeded the public creation budget or a live-only capacity, "
        "cooldown, or daily-budget admission limit."
    )
    assert response["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/PublicErrorResponse"
    }
    assert response["headers"]["Retry-After"]["description"] == (
        "Retry delay in seconds when provided. Creation-budget exhaustion uses "
        "1..86400 seconds until the next UTC midnight; live-only outcomes may use "
        "a longer or otherwise different delay."
    )
    assert response["headers"]["Retry-After"]["schema"] == {
        "minimum": 1,
        "type": "integer",
    }
