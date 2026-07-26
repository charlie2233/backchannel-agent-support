from __future__ import annotations

import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Lock
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.types import ASGIApp, Receive, Scope, Send

from server import main as server_main
from server.config import RuntimeSettings
from server.controls import PublicDemoControls
from server.main import create_app
from server.models import (
    CreateRecoveryRequest,
    ExecutionMode,
    ScenarioId,
)
from server.orchestrator import RecoveryOrchestrator
from server.providers.hotel_simulator import HotelSimulator
from server.replay.engine import ReplayEngine, replay_recovery_id
from server.replay.loader import ScenarioLoader
from server.store import ResetCreationPendingError, SQLiteStore

_IDENTITY_SECRET = "creation-idempotency-test-secret-that-is-long-enough"
_COOKIE_NAME = "backchannel_demo_session"
_REQUEST_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_CLIENT_REQUEST_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$"
_RESET_CREATION_PENDING_MESSAGE = (
    "Demo reset is unavailable while a recovery start is unresolved. "
    "Retry reset after the start resolves or the signed session expires."
)
_CREATION_MESSAGES = {
    "idempotency_conflict": (
        "This recovery start no longer matches its original request. No additional run was started."
    ),
    "creation_pending": ("Recovery creation is unresolved. Retry the same start shortly."),
    "creation_outcome_unknown": (
        "The recovery start outcome could not be confirmed. No replacement run was started."
    ),
    "creation_capacity": (
        "Recovery creation is temporarily at capacity. Existing starts can still "
        "be retried; try a new start later."
    ),
}


@dataclass
class _Attempts:
    lock: Lock = field(default_factory=Lock)
    orchestrations: int = 0
    admission_checks: int = 0
    budget_checks: int = 0

    def record_orchestration(self) -> int:
        with self.lock:
            self.orchestrations += 1
            return self.orchestrations

    def record_admission(self) -> None:
        with self.lock:
            self.admission_checks += 1
            self.budget_checks += 1


class _CountingStore(SQLiteStore):
    def __init__(self, database_path: Path, attempts: _Attempts) -> None:
        self._creation_attempts = attempts
        super().__init__(database_path)

    def try_admit_live_recovery(self, **kwargs: Any) -> Any:
        self._creation_attempts.record_admission()
        return super().try_admit_live_recovery(**kwargs)


class _RejectFirstAdmissionStore(_CountingStore):
    def __init__(self, database_path: Path, attempts: _Attempts) -> None:
        self._reject_next_admission = True
        super().__init__(database_path, attempts)

    def try_admit_live_recovery(self, **kwargs: Any) -> Any:
        self._creation_attempts.record_admission()
        if self._reject_next_admission:
            self._reject_next_admission = False
            return "live_capacity"
        return SQLiteStore.try_admit_live_recovery(self, **kwargs)


class _RecordingOrchestrator:
    def __init__(self, store: SQLiteStore, attempts: _Attempts) -> None:
        self._store = store
        self._attempts = attempts

    async def _persist(
        self,
        scenario_id: str | ScenarioId,
        *,
        execution_mode: ExecutionMode,
        recovery_id: str | None,
        session_key: str | None,
    ) -> Any:
        selected_scenario = ScenarioId(scenario_id)
        orchestrator = RecoveryOrchestrator(
            store=self._store,
            hotel_provider=HotelSimulator(store=self._store),
        )
        if (
            selected_scenario is not ScenarioId.HOTEL
            or execution_mode is not ExecutionMode.OPENAI_LIVE
        ):
            return await orchestrator.start(
                selected_scenario,
                execution_mode=execution_mode,
                recovery_id=recovery_id,
                session_key=session_key,
            )

        pending = await orchestrator.start(
            selected_scenario,
            execution_mode=ExecutionMode.SDK_STUB,
            recovery_id=recovery_id,
            session_key=session_key,
        )
        selected_recovery_id = pending.recovery.recovery_id
        model_ids_json = '["gpt-5.6-luna","gpt-5.6-terra"]'
        root_trace_id = f"trace_{uuid4().hex}"
        agent_graph_version = "backchannel.hotel-agent.live.v1"
        with sqlite3.connect(self._store._database_path) as connection:
            connection.execute(
                """
                UPDATE recoveries
                SET execution_mode = 'openai_live',
                    model_ids_json = ?,
                    root_trace_id = ?,
                    model_call = 1,
                    agent_graph_version = ?
                WHERE id = ?
                """,
                (
                    model_ids_json,
                    root_trace_id,
                    agent_graph_version,
                    selected_recovery_id,
                ),
            )
            connection.execute(
                """
                UPDATE pending_approvals
                SET execution_mode = 'openai_live',
                    model_ids_json = ?,
                    root_trace_id = ?,
                    agent_graph_version = ?
                WHERE recovery_id = ?
                """,
                (
                    model_ids_json,
                    root_trace_id,
                    agent_graph_version,
                    selected_recovery_id,
                ),
            )
            connection.execute(
                """
                UPDATE events
                SET data_json = ?
                WHERE recovery_id = ? AND seq = 1
                """,
                (
                    '{"executionMode":"openai_live","scenarioId":"hotel",'
                    '"summary":"Recovery created for the selected execution mode."}',
                    selected_recovery_id,
                ),
            )
        return SimpleNamespace(recovery=self._store.get_recovery(selected_recovery_id))

    async def start(
        self,
        scenario_id: str | ScenarioId,
        *,
        execution_mode: ExecutionMode,
        recovery_id: str | None = None,
        session_key: str | None = None,
    ) -> Any:
        self._attempts.record_orchestration()
        return await self._persist(
            scenario_id,
            execution_mode=execution_mode,
            recovery_id=recovery_id,
            session_key=session_key,
        )


class _BareOrchestrator:
    def __init__(self, store: SQLiteStore, attempts: _Attempts) -> None:
        self._store = store
        self._attempts = attempts

    async def start(
        self,
        scenario_id: str | ScenarioId,
        *,
        execution_mode: ExecutionMode,
        recovery_id: str | None = None,
        session_key: str | None = None,
    ) -> Any:
        self._attempts.record_orchestration()
        recovery = self._store.create_recovery(
            recovery_id=recovery_id or str(uuid4()),
            scenario_id=ScenarioId(scenario_id),
            execution_mode=execution_mode,
            current_step=0,
            current_step_summary="Bare persistence without outcome evidence.",
            session_key=session_key,
        )
        return SimpleNamespace(recovery=recovery)


@dataclass
class _BlockingCoordinator:
    attempts: _Attempts
    entered: Event = field(default_factory=Event)
    release: Event = field(default_factory=Event)


class _BlockingOrchestrator(_RecordingOrchestrator):
    def __init__(self, store: SQLiteStore, coordinator: _BlockingCoordinator) -> None:
        super().__init__(store, coordinator.attempts)
        self._coordinator = coordinator

    async def start(
        self,
        scenario_id: str | ScenarioId,
        *,
        execution_mode: ExecutionMode,
        recovery_id: str | None = None,
        session_key: str | None = None,
    ) -> Any:
        attempt = self._coordinator.attempts.record_orchestration()
        if attempt == 1:
            self._coordinator.entered.set()
            if not self._coordinator.release.wait(timeout=5):
                raise AssertionError("Timed out waiting to release creation owner")
        return await self._persist(
            scenario_id,
            execution_mode=execution_mode,
            recovery_id=recovery_id,
            session_key=session_key,
        )


class _UncertainOrchestrator:
    def __init__(self, attempts: _Attempts) -> None:
        self._attempts = attempts

    async def start(self, *_args: Any, **_kwargs: Any) -> Any:
        self._attempts.record_orchestration()
        raise RuntimeError("simulated process loss after creation work started")


class _CompletedThenUncertainOrchestrator:
    def __init__(self, store: SQLiteStore, attempts: _Attempts) -> None:
        self._store = store
        self._attempts = attempts

    async def start(
        self,
        scenario_id: str | ScenarioId,
        *,
        execution_mode: ExecutionMode,
        recovery_id: str | None = None,
        session_key: str | None = None,
    ) -> Any:
        self._attempts.record_orchestration()
        recovery = self._store.create_recovery(
            recovery_id=recovery_id or str(uuid4()),
            scenario_id=ScenarioId(scenario_id),
            execution_mode=execution_mode,
            current_step=0,
            current_step_summary="Bare recovery before a simulated status flip.",
            session_key=session_key,
        )
        with sqlite3.connect(self._store._database_path) as connection:
            connection.execute(
                """
                UPDATE recoveries
                SET status = 'completed',
                    current_step = 5,
                    current_step_summary = 'Durable completion survived response loss.'
                WHERE id = ?
                """,
                (recovery.recovery_id,),
            )
        raise RuntimeError("simulated response loss after durable completion")


class _AuthoritativeThenUncertainOrchestrator(_RecordingOrchestrator):
    async def start(
        self,
        scenario_id: str | ScenarioId,
        *,
        execution_mode: ExecutionMode,
        recovery_id: str | None = None,
        session_key: str | None = None,
    ) -> Any:
        self._attempts.record_orchestration()
        await self._persist(
            scenario_id,
            execution_mode=execution_mode,
            recovery_id=recovery_id,
            session_key=session_key,
        )
        raise RuntimeError("simulated response loss after authoritative completion")


class _LostResponse(RuntimeError):
    pass


class _DropFirstCreateResponse:
    """Run the first create request fully, then drop its response bytes."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app
        self._dropped = False

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        should_drop = (
            not self._dropped
            and scope["type"] == "http"
            and scope.get("method") == "POST"
            and scope.get("path") == "/api/recoveries"
        )
        if not should_drop:
            await self._app(scope, receive, send)
            return

        self._dropped = True

        async def discard(_message: Any) -> None:
            return None

        await self._app(scope, receive, discard)
        raise _LostResponse("simulated response loss after request completion")


def _settings(*, live_ready: bool = False) -> RuntimeSettings:
    return RuntimeSettings(
        live_ready=live_ready,
        identity_hash_secret=_IDENTITY_SECRET,
        max_concurrent_live_recoveries=4,
        daily_demo_budget_units=100,
    )


def _create_test_app(
    settings: RuntimeSettings,
    *,
    store: SQLiteStore,
    orchestrator: object | None = None,
) -> FastAPI:
    return create_app(
        settings,
        store=store,
        orchestrator=cast(RecoveryOrchestrator | None, orchestrator),
    )


def _payload(
    client_request_id: str,
    *,
    scenario_id: str = "hotel",
    execution_mode: str = "sdk_stub",
) -> dict[str, str]:
    return {
        "scenarioId": scenario_id,
        "executionMode": execution_mode,
        "clientRequestId": client_request_id,
    }


def _bootstrap_session(client: TestClient) -> str:
    response = client.get("/health")
    assert response.status_code == 200
    raw_cookie = client.cookies.get(_COOKIE_NAME)
    assert isinstance(raw_cookie, str)
    return raw_cookie


def _attach_session(client: TestClient, raw_cookie: str) -> None:
    client.cookies.set(_COOKIE_NAME, raw_cookie)


def _scalar(database_path: Path, query: str) -> int:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(query).fetchone()
    assert row is not None
    return int(row[0])


def _database_dump(database_path: Path) -> str:
    with sqlite3.connect(database_path) as connection:
        return "\n".join(connection.iterdump())


def _assert_raw_key_absent(
    raw_key: str,
    *,
    database_path: Path,
    responses: list[Any],
) -> None:
    assert raw_key not in _database_dump(database_path)
    for response in responses:
        assert raw_key not in response.text
        assert raw_key not in repr(dict(response.headers))


def _assert_creation_error(
    response: Any,
    *,
    code: str,
) -> str:
    expected_status = 429 if code == "creation_capacity" else 409
    assert response.status_code == expected_status
    body = response.json()
    assert set(body) == {"code", "message", "requestId"}
    assert body["code"] == code
    assert body["message"] == _CREATION_MESSAGES[code]
    assert _REQUEST_ID_PATTERN.fullmatch(body["requestId"]) is not None
    assert response.headers["cache-control"] == "no-store"
    return str(body["message"])


def _claim_creation(
    store: SQLiteStore,
    *,
    request_key: str,
    session_key: str,
    ip_key: str = "a" * 64,
    now: datetime,
    expires_at: datetime | None = None,
    request_fingerprint: str = "f" * 64,
    max_per_session: int = 32,
    max_per_ip: int = 128,
    max_global: int = 2_048,
) -> Any:
    return store.claim_recovery_creation(
        request_key=request_key,
        request_fingerprint=request_fingerprint,
        scenario_id=ScenarioId.HOTEL,
        execution_mode=ExecutionMode.SDK_STUB,
        reserved_recovery_id=str(uuid4()),
        session_key=session_key,
        ip_key=ip_key,
        expires_at=expires_at or now + timedelta(days=1),
        max_per_session=max_per_session,
        max_per_ip=max_per_ip,
        max_global=max_global,
        now=now,
    )


def _assert_reset_creation_pending(response: Any) -> None:
    assert response.status_code == 409
    assert response.json() == {
        "code": "reset_creation_pending",
        "message": _RESET_CREATION_PENDING_MESSAGE,
        "requestId": response.headers["x-request-id"],
    }
    assert _REQUEST_ID_PATTERN.fullmatch(response.json()["requestId"]) is not None
    assert response.headers["cache-control"] == "no-store"


def test_create_request_schema_requires_a_strict_bounded_client_request_id() -> None:
    schema = CreateRecoveryRequest.model_json_schema(by_alias=True)

    assert set(schema["required"]) == {
        "scenarioId",
        "executionMode",
        "clientRequestId",
    }
    property_schema = schema["properties"]["clientRequestId"]
    assert property_schema["type"] == "string"
    assert property_schema["minLength"] == 1
    assert property_schema["maxLength"] == 128
    assert property_schema["pattern"] == _CLIENT_REQUEST_ID_PATTERN

    parsed = CreateRecoveryRequest.model_validate(_payload("creation-request-001"))
    assert parsed.client_request_id == "creation-request-001"


def test_creation_ledger_binds_claims_to_opaque_session_expiry(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "creation-ledger-schema.sqlite3"
    store = SQLiteStore(database_path)
    with sqlite3.connect(database_path) as connection:
        columns = {
            str(row[1]): (str(row[2]), bool(row[3]))
            for row in connection.execute("PRAGMA table_info(recovery_creations)").fetchall()
        }
        indexes = {
            str(row[1])
            for row in connection.execute("PRAGMA index_list(recovery_creations)").fetchall()
        }

    assert columns["session_key"] == ("TEXT", True)
    assert columns["ip_key"] == ("TEXT", True)
    assert columns["expires_at"] == ("TEXT", True)
    assert "recovery_creations_session_idx" in indexes
    assert "recovery_creations_ip_expiry_idx" in indexes
    assert "recovery_creations_expiry_idx" in indexes
    store.close()


def test_creation_ledger_database_rejects_noncanonical_ip_key_lengths(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "creation-ip-constraints.sqlite3"
    store = SQLiteStore(database_path)
    now = datetime(2026, 7, 24, tzinfo=UTC)
    owner = _claim_creation(
        store,
        request_key="a" * 64,
        session_key="b" * 64,
        ip_key="c" * 64,
        now=now,
    )
    assert owner.disposition == "owner"

    for invalid_ip_key in (None, "d" * 63, "e" * 65):
        with (
            sqlite3.connect(database_path) as connection,
            pytest.raises(sqlite3.IntegrityError),
        ):
            connection.execute(
                "UPDATE recovery_creations SET ip_key = ?",
                (invalid_ip_key,),
            )

    with sqlite3.connect(database_path) as connection:
        stored_ip_key = connection.execute("SELECT ip_key FROM recovery_creations").fetchone()
    assert stored_ip_key == ("c" * 64,)
    store.close()


def test_legacy_creation_ledger_backfills_opaque_ip_key_without_invalidating_claim(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy-creation-ledger.sqlite3"
    request_key = "a" * 64
    request_fingerprint = "b" * 64
    session_key = "c" * 64
    recovery_id = "11111111-2222-4333-8444-555555555555"
    created_at = datetime(2026, 7, 24, tzinfo=UTC)
    expires_at = created_at + timedelta(days=1)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE recovery_creations (
                request_key TEXT PRIMARY KEY,
                request_fingerprint TEXT NOT NULL,
                session_key TEXT NOT NULL,
                scenario_id TEXT NOT NULL,
                execution_mode TEXT NOT NULL,
                recovery_id TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO recovery_creations (
                request_key, request_fingerprint, session_key, scenario_id,
                execution_mode, recovery_id, status, created_at, updated_at,
                expires_at
            ) VALUES (?, ?, ?, 'hotel', 'sdk_stub', ?, 'reserved', ?, ?, ?)
            """,
            (
                request_key,
                request_fingerprint,
                session_key,
                recovery_id,
                created_at.isoformat(),
                created_at.isoformat(),
                expires_at.isoformat(),
            ),
        )

    store = SQLiteStore(database_path)
    retry = store.claim_recovery_creation(
        request_key=request_key,
        request_fingerprint=request_fingerprint,
        scenario_id=ScenarioId.HOTEL,
        execution_mode=ExecutionMode.SDK_STUB,
        reserved_recovery_id=str(uuid4()),
        session_key=session_key,
        ip_key="d" * 64,
        expires_at=expires_at,
        now=created_at + timedelta(seconds=1),
    )
    with sqlite3.connect(database_path) as connection:
        stored_ip_key = connection.execute(
            "SELECT ip_key FROM recovery_creations WHERE request_key = ?",
            (request_key,),
        ).fetchone()

    assert retry.disposition == "pending"
    assert stored_ip_key == (session_key,)
    store.close()


def test_interrupted_ip_migration_rebuilds_canonical_schema_and_preserves_claims(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "interrupted-ip-migration.sqlite3"
    created_at = datetime(2026, 7, 24, tzinfo=UTC)
    expires_at = created_at + timedelta(days=1)
    rows = [
        (
            "a" * 64,
            "d" * 64,
            "1" * 64,
            "4" * 64,
            "11111111-2222-4333-8444-555555555555",
        ),
        (
            "b" * 64,
            "e" * 64,
            "2" * 64,
            None,
            "22222222-3333-4444-8555-666666666666",
        ),
        (
            "c" * 64,
            "f" * 64,
            "3" * 64,
            "z" * 64,
            "33333333-4444-4555-8666-777777777777",
        ),
    ]
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE recovery_creations (
                request_key TEXT PRIMARY KEY,
                request_fingerprint TEXT NOT NULL,
                session_key TEXT NOT NULL,
                ip_key TEXT,
                scenario_id TEXT NOT NULL,
                execution_mode TEXT NOT NULL,
                recovery_id TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO recovery_creations (
                request_key, request_fingerprint, session_key, ip_key,
                scenario_id, execution_mode, recovery_id, status,
                created_at, updated_at, expires_at
            ) VALUES (?, ?, ?, ?, 'hotel', 'sdk_stub', ?, 'reserved', ?, ?, ?)
            """,
            [
                (
                    request_key,
                    request_fingerprint,
                    session_key,
                    ip_key,
                    recovery_id,
                    created_at.isoformat(),
                    created_at.isoformat(),
                    expires_at.isoformat(),
                )
                for (
                    request_key,
                    request_fingerprint,
                    session_key,
                    ip_key,
                    recovery_id,
                ) in rows
            ],
        )

    store = SQLiteStore(database_path)
    with sqlite3.connect(database_path) as connection:
        ip_column = next(
            row
            for row in connection.execute("PRAGMA table_info(recovery_creations)").fetchall()
            if row[1] == "ip_key"
        )
        table_sql = str(
            connection.execute(
                """
                SELECT sql
                FROM sqlite_master
                WHERE type = 'table' AND name = 'recovery_creations'
                """
            ).fetchone()[0]
        )
        stored_rows = connection.execute(
            """
            SELECT request_key, request_fingerprint, session_key, ip_key,
                   recovery_id, status, created_at, updated_at, expires_at
            FROM recovery_creations
            ORDER BY request_key
            """
        ).fetchall()
        indexes = {
            str(row[1])
            for row in connection.execute("PRAGMA index_list(recovery_creations)").fetchall()
        }

    assert (ip_column[2], ip_column[3]) == ("TEXT", 1)
    assert "ip_keytextnotnullcheck(length(ip_key)=64)" in "".join(table_sql.lower().split())
    assert [row[3] for row in stored_rows] == [
        "4" * 64,
        "2" * 64,
        "3" * 64,
    ]
    assert [
        (
            row[0],
            row[1],
            row[2],
            row[4],
            row[5],
            row[6],
            row[7],
            row[8],
        )
        for row in stored_rows
    ] == [
        (
            request_key,
            request_fingerprint,
            session_key,
            recovery_id,
            "reserved",
            created_at.isoformat(),
            created_at.isoformat(),
            expires_at.isoformat(),
        )
        for (
            request_key,
            request_fingerprint,
            session_key,
            _ip_key,
            recovery_id,
        ) in rows
    ]
    assert {
        "recovery_creations_status_updated_idx",
        "recovery_creations_session_idx",
        "recovery_creations_ip_expiry_idx",
        "recovery_creations_expiry_idx",
    }.issubset(indexes)

    for (
        request_key,
        request_fingerprint,
        session_key,
        _ip_key,
        _recovery_id,
    ) in rows:
        retry = store.claim_recovery_creation(
            request_key=request_key,
            request_fingerprint=request_fingerprint,
            scenario_id=ScenarioId.HOTEL,
            execution_mode=ExecutionMode.SDK_STUB,
            reserved_recovery_id=str(uuid4()),
            session_key=session_key,
            ip_key="9" * 64,
            expires_at=expires_at,
            now=created_at + timedelta(seconds=1),
        )
        assert retry.disposition == "pending"
    store.close()


def test_creation_claim_expiry_matches_signed_cookie_scope(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "creation-cookie-expiry.sqlite3"
    store = SQLiteStore(database_path)
    settings = RuntimeSettings(
        live_ready=False,
        demo_session_lifetime_seconds=3_600,
        identity_hash_secret=_IDENTITY_SECRET,
    )
    with TestClient(_create_test_app(settings, store=store)) as client:
        response = client.post(
            "/api/recoveries",
            json=_payload(
                "cookie-expiry-001",
                scenario_id="api-quota",
                execution_mode="replay_fixture",
            ),
        )
        raw_cookie = client.cookies.get(_COOKIE_NAME)

    assert response.status_code == 201
    assert isinstance(raw_cookie, str)
    cookie_expiry = datetime.fromtimestamp(int(raw_cookie.split(".")[1]), tz=UTC)
    with sqlite3.connect(database_path) as connection:
        claim_session, claim_expiry = connection.execute(
            "SELECT session_key, expires_at FROM recovery_creations"
        ).fetchone()
        access_session = connection.execute("SELECT session_key FROM recovery_access").fetchone()[0]
    assert claim_session == access_session
    assert len(str(claim_session)) == 64
    assert raw_cookie not in {claim_session, claim_expiry}
    assert datetime.fromisoformat(str(claim_expiry)) == cookie_expiry
    store.close()


@pytest.mark.parametrize(
    "invalid_client_request_id",
    [
        None,
        True,
        7,
        "",
        "x" * 129,
        " leading-space",
        "trailing-space ",
        "création-request",
        "path/request",
        "line\nbreak",
        "@unsafe-prefix",
        "query?unsafe",
    ],
)
def test_create_request_rejects_non_strict_or_unbounded_client_request_id(
    invalid_client_request_id: object,
) -> None:
    with pytest.raises(ValidationError):
        CreateRecoveryRequest.model_validate(
            {
                "scenarioId": "hotel",
                "executionMode": "sdk_stub",
                "clientRequestId": invalid_client_request_id,
            }
        )


def test_missing_client_request_id_is_rejected_before_orchestration(
    tmp_path: Path,
) -> None:
    attempts = _Attempts()
    store = _CountingStore(tmp_path / "missing-key.sqlite3", attempts)
    orchestrator = _RecordingOrchestrator(store, attempts)
    with TestClient(
        _create_test_app(_settings(), store=store, orchestrator=orchestrator)
    ) as client:
        response = client.post(
            "/api/recoveries",
            json={"scenarioId": "hotel", "executionMode": "sdk_stub"},
        )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"
    assert attempts.orchestrations == 0
    assert _scalar(store._database_path, "SELECT COUNT(*) FROM recoveries") == 0
    store.close()


def test_bare_recovery_row_cannot_be_promoted_to_ready_creation(
    tmp_path: Path,
) -> None:
    attempts = _Attempts()
    store = _CountingStore(tmp_path / "bare-row.sqlite3", attempts)
    payload = _payload("bare-row-001")
    with TestClient(
        _create_test_app(
            _settings(),
            store=store,
            orchestrator=_BareOrchestrator(store, attempts),
        )
    ) as client:
        first = client.post("/api/recoveries", json=payload)
        retry = client.post("/api/recoveries", json=payload)

    _assert_creation_error(first, code="creation_outcome_unknown")
    _assert_creation_error(retry, code="creation_outcome_unknown")
    assert attempts.orchestrations == 1
    assert (
        _scalar(
            store._database_path,
            "SELECT COUNT(*) FROM recovery_creations WHERE status = 'unknown'",
        )
        == 1
    )
    store.close()


def test_same_session_key_replays_one_live_creation_across_restart(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "sequential-restart.sqlite3"
    attempts = _Attempts()
    raw_key = "live-create-retry-001"
    payload = _payload(raw_key, execution_mode="openai_live")
    settings = _settings(live_ready=True)
    first_store = _CountingStore(database_path, attempts)
    first_orchestrator = _RecordingOrchestrator(first_store, attempts)

    with TestClient(
        _create_test_app(
            settings,
            store=first_store,
            orchestrator=first_orchestrator,
        )
    ) as first_client:
        session_cookie = _bootstrap_session(first_client)
        first = first_client.post("/api/recoveries", json=payload)
        sequential_retry = first_client.post("/api/recoveries", json=payload)
    first_store.close()

    second_store = _CountingStore(database_path, attempts)
    second_orchestrator = _RecordingOrchestrator(second_store, attempts)
    with TestClient(
        _create_test_app(
            settings,
            store=second_store,
            orchestrator=second_orchestrator,
        )
    ) as restarted_client:
        _attach_session(restarted_client, session_cookie)
        restart_retry = restarted_client.post("/api/recoveries", json=payload)

    responses = [first, sequential_retry, restart_retry]
    assert [response.status_code for response in responses] == [201, 201, 201]
    recovery_ids = {response.json()["recoveryId"] for response in responses}
    assert len(recovery_ids) == 1
    assert attempts.orchestrations == 1
    assert attempts.admission_checks == 1
    assert attempts.budget_checks == 1
    assert _scalar(database_path, "SELECT COUNT(*) FROM recoveries") == 1
    assert _scalar(database_path, "SELECT COUNT(*) FROM executions") == 0
    assert (
        _scalar(
            database_path,
            """
            SELECT COALESCE(SUM(amount), 0)
            FROM usage_ledger
            WHERE category = 'live_demo_budget_unit'
            """,
        )
        == 1
    )
    _assert_raw_key_absent(
        raw_key,
        database_path=database_path,
        responses=responses,
    )
    second_store.close()


def test_ready_retry_fails_closed_then_expires_its_target_behind_a_backlog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "ready-retry-expiry-backlog.sqlite3"
    store = SQLiteStore(database_path)
    payload = _payload("ready-retry-expiry-target")

    with TestClient(_create_test_app(_settings(), store=store)) as client:
        backlog_ids: list[str] = []
        for index in range(26):
            created = client.post(
                "/api/recoveries",
                json=_payload(f"ready-retry-expiry-backlog-{index:02d}"),
            )
            assert created.status_code == 201
            backlog_ids.append(str(created.json()["recoveryId"]))

        first = client.post("/api/recoveries", json=payload)
        assert first.status_code == 201
        target_id = str(first.json()["recoveryId"])
        assert first.json()["pendingApproval"] is not None

        cleanup_now = datetime.now(UTC) + timedelta(hours=2)

        class _ExpiredConsentClock(datetime):
            @classmethod
            def now(cls, tz: object = None) -> datetime:
                if tz is not None:
                    return cleanup_now
                return cleanup_now.replace(tzinfo=None)

        monkeypatch.setattr("server.cleanup.datetime", _ExpiredConsentClock)

        original_expiry = server_main.expire_pending_approvals
        fail_target_once = True

        def expire_with_one_target_failure(
            target_store: SQLiteStore,
            **kwargs: Any,
        ) -> int:
            nonlocal fail_target_once
            if kwargs.get("recovery_id") == target_id and fail_target_once:
                fail_target_once = False
                raise sqlite3.OperationalError("simulated one-shot expiry failure")
            return original_expiry(target_store, **kwargs)

        monkeypatch.setattr(
            server_main,
            "expire_pending_approvals",
            expire_with_one_target_failure,
        )

        failed_retry = client.post("/api/recoveries", json=payload)
        retry = client.post("/api/recoveries", json=payload)

    _assert_creation_error(failed_retry, code="creation_outcome_unknown")
    assert retry.status_code == 201
    assert retry.json()["recoveryId"] == target_id
    assert retry.json()["status"] == "closed_without_action"
    assert retry.json()["pendingApproval"] is None
    with sqlite3.connect(database_path) as connection:
        stored_status = connection.execute(
            "SELECT status FROM recoveries WHERE id = ?",
            (target_id,),
        ).fetchone()
    assert stored_status == ("closed_without_action",)
    store.close()


def test_known_live_admission_rejection_abandons_untouched_claim(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "admission-retry.sqlite3"
    attempts = _Attempts()
    store = _RejectFirstAdmissionStore(database_path, attempts)
    orchestrator = _RecordingOrchestrator(store, attempts)
    payload = _payload(
        "live-admission-retry-001",
        execution_mode="openai_live",
    )

    with TestClient(
        _create_test_app(
            _settings(live_ready=True),
            store=store,
            orchestrator=orchestrator,
        )
    ) as client:
        rejected = client.post("/api/recoveries", json=payload)
        assert (
            _scalar(
                database_path,
                "SELECT COUNT(*) FROM recovery_creations",
            )
            == 0
        )
        retry = client.post("/api/recoveries", json=payload)

    assert rejected.status_code == 429
    assert rejected.json()["code"] == "live_capacity"
    assert retry.status_code == 201
    assert attempts.admission_checks == 2
    assert attempts.budget_checks == 2
    assert attempts.orchestrations == 1
    store.close()


def test_same_session_key_with_changed_fingerprint_conflicts_before_accounting(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "fingerprint-conflict.sqlite3"
    attempts = _Attempts()
    store = _CountingStore(database_path, attempts)
    orchestrator = _RecordingOrchestrator(store, attempts)
    raw_key = "fingerprint-key-001"

    with TestClient(
        _create_test_app(
            _settings(live_ready=True),
            store=store,
            orchestrator=orchestrator,
        )
    ) as client:
        _bootstrap_session(client)
        first = client.post("/api/recoveries", json=_payload(raw_key))
        changed_scenario = client.post(
            "/api/recoveries",
            json=_payload(raw_key, scenario_id="api-quota"),
        )
        changed_mode = client.post(
            "/api/recoveries",
            json=_payload(raw_key, execution_mode="openai_live"),
        )

    assert first.status_code == 201
    scenario_message = _assert_creation_error(
        changed_scenario,
        code="idempotency_conflict",
    )
    mode_message = _assert_creation_error(
        changed_mode,
        code="idempotency_conflict",
    )
    assert scenario_message == mode_message
    assert attempts.orchestrations == 1
    assert attempts.admission_checks == 0
    assert attempts.budget_checks == 0
    assert _scalar(database_path, "SELECT COUNT(*) FROM recoveries") == 1
    assert _scalar(database_path, "SELECT COUNT(*) FROM usage_ledger") == 0
    _assert_raw_key_absent(
        raw_key,
        database_path=database_path,
        responses=[first, changed_scenario, changed_mode],
    )
    store.close()


def test_same_raw_key_in_distinct_signed_sessions_is_independent(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "session-scoped.sqlite3"
    attempts = _Attempts()
    store = _CountingStore(database_path, attempts)
    orchestrator = _RecordingOrchestrator(store, attempts)
    raw_key = "session-scoped-key-001"
    payload = _payload(raw_key)

    with (
        TestClient(
            _create_test_app(_settings(), store=store, orchestrator=orchestrator)
        ) as first_client,
        TestClient(
            _create_test_app(_settings(), store=store, orchestrator=orchestrator)
        ) as second_client,
    ):
        first_cookie = _bootstrap_session(first_client)
        second_cookie = _bootstrap_session(second_client)
        assert first_cookie != second_cookie
        first = first_client.post("/api/recoveries", json=payload)
        second = second_client.post("/api/recoveries", json=payload)

    assert [first.status_code, second.status_code] == [201, 201]
    assert first.json()["recoveryId"] != second.json()["recoveryId"]
    assert attempts.orchestrations == 2
    assert _scalar(database_path, "SELECT COUNT(*) FROM recoveries") == 2
    assert _scalar(database_path, "SELECT COUNT(*) FROM recovery_access") == 2
    _assert_raw_key_absent(
        raw_key,
        database_path=database_path,
        responses=[first, second],
    )
    store.close()


def test_concurrent_same_key_has_one_owner_and_fixed_pending_contenders(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "concurrent-pending.sqlite3"
    attempts = _Attempts()
    coordinator = _BlockingCoordinator(attempts)
    first_store = _CountingStore(database_path, attempts)
    second_store = _CountingStore(database_path, attempts)
    settings = _settings()
    first_app = _create_test_app(
        settings,
        store=first_store,
        orchestrator=_BlockingOrchestrator(first_store, coordinator),
    )
    second_app = _create_test_app(
        settings,
        store=second_store,
        orchestrator=_BlockingOrchestrator(second_store, coordinator),
    )
    payload = _payload("concurrent-create-001")

    try:
        with (
            TestClient(first_app) as owner_client,
            TestClient(second_app) as contender_client,
        ):
            session_cookie = _bootstrap_session(owner_client)
            _attach_session(contender_client, session_cookie)
            with ThreadPoolExecutor(max_workers=1) as executor:
                owner_future = executor.submit(
                    owner_client.post,
                    "/api/recoveries",
                    json=payload,
                )
                try:
                    if not coordinator.entered.wait(timeout=2):
                        early = owner_future.result(timeout=1)
                        pytest.fail(
                            "creation owner never reached orchestration; "
                            f"status={early.status_code}"
                        )
                    first_pending = contender_client.post(
                        "/api/recoveries",
                        json=payload,
                    )
                    second_pending = contender_client.post(
                        "/api/recoveries",
                        json=payload,
                    )
                finally:
                    coordinator.release.set()
                owner = owner_future.result(timeout=3)
    finally:
        first_store.close()
        second_store.close()

    assert owner.status_code == 201
    first_message = _assert_creation_error(
        first_pending,
        code="creation_pending",
    )
    second_message = _assert_creation_error(
        second_pending,
        code="creation_pending",
    )
    assert first_message == second_message
    assert first_pending.headers["retry-after"] == "2"
    assert second_pending.headers["retry-after"] == "2"
    assert attempts.orchestrations == 1
    assert _scalar(database_path, "SELECT COUNT(*) FROM recoveries") == 1


def test_hard_loss_started_claim_returns_neutral_pending_http_contract(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "hard-loss-neutral-pending.sqlite3"
    store = SQLiteStore(database_path)
    settings = _settings()
    attempts = _Attempts()
    controls = PublicDemoControls(store, settings)
    payload = _payload("hard-loss-neutral-pending-001")
    app = _create_test_app(
        settings,
        store=store,
        orchestrator=_RecordingOrchestrator(store, attempts),
    )

    with TestClient(app) as client:
        raw_cookie = _bootstrap_session(client)
        _version, expiry_text, nonce, _signature = raw_cookie.split(".")
        session_key = controls._correlation_key("session", nonce)
        session_expires_at = datetime.fromtimestamp(int(expiry_text), tz=UTC)
        request_key = controls.recovery_creation_request_key(
            session_key=session_key,
            client_request_id=payload["clientRequestId"],
        )
        request_fingerprint = server_main._recovery_creation_fingerprint(
            CreateRecoveryRequest.model_validate(payload)
        )
        now = datetime.now(UTC)
        owner = store.claim_recovery_creation(
            request_key=request_key,
            request_fingerprint=request_fingerprint,
            scenario_id=ScenarioId.HOTEL,
            execution_mode=ExecutionMode.SDK_STUB,
            reserved_recovery_id=str(uuid4()),
            session_key=session_key,
            ip_key="a" * 64,
            expires_at=session_expires_at,
            now=now,
        )
        assert owner.disposition == "owner"
        store.mark_recovery_creation_started(
            request_key=request_key,
            request_fingerprint=request_fingerprint,
            now=now,
        )

        response = client.post("/api/recoveries", json=payload)

    message = _assert_creation_error(response, code="creation_pending")
    assert message == "Recovery creation is unresolved. Retry the same start shortly."
    assert response.headers["retry-after"] == "2"
    assert attempts.orchestrations == 0
    assert _scalar(database_path, "SELECT COUNT(*) FROM recoveries") == 0
    assert _scalar(database_path, "SELECT COUNT(*) FROM executions") == 0
    assert (
        _scalar(
            database_path,
            "SELECT COUNT(*) FROM recovery_creations WHERE status = 'started'",
        )
        == 1
    )
    store.close()


@pytest.mark.parametrize("stale_owner", [False, True])
def test_reset_refuses_unresolved_start_before_explicit_post_reset_restart(
    tmp_path: Path,
    *,
    stale_owner: bool,
) -> None:
    database_path = tmp_path / f"reset-during-creation-{stale_owner}.sqlite3"
    attempts = _Attempts()
    coordinator = _BlockingCoordinator(attempts)
    settings = RuntimeSettings(
        live_ready=False,
        demo_reset_enabled=True,
        identity_hash_secret=_IDENTITY_SECRET,
    )
    owner_store = _CountingStore(database_path, attempts)
    contender_store = _CountingStore(database_path, attempts)
    owner_app = _create_test_app(
        settings,
        store=owner_store,
        orchestrator=_BlockingOrchestrator(owner_store, coordinator),
    )
    contender_app = _create_test_app(
        settings,
        store=contender_store,
        orchestrator=_BlockingOrchestrator(contender_store, coordinator),
    )
    raw_key = f"reset-race-create-{int(stale_owner)}"
    payload = _payload(raw_key)

    try:
        with (
            TestClient(owner_app) as owner_client,
            TestClient(contender_app) as contender_client,
        ):
            session_cookie = _bootstrap_session(owner_client)
            _attach_session(contender_client, session_cookie)
            with ThreadPoolExecutor(max_workers=1) as executor:
                owner_future = executor.submit(
                    owner_client.post,
                    "/api/recoveries",
                    json=payload,
                )
                try:
                    if not coordinator.entered.wait(timeout=2):
                        early = owner_future.result(timeout=1)
                        pytest.fail(
                            "creation owner never reached orchestration; "
                            f"status={early.status_code}"
                        )
                    if stale_owner:
                        with sqlite3.connect(database_path) as connection:
                            connection.execute(
                                """
                                UPDATE recovery_creations
                                SET updated_at = ?
                                """,
                                ((datetime.now(UTC) - timedelta(minutes=10)).isoformat(),),
                            )
                        same_key_contender = contender_client.post(
                            "/api/recoveries",
                            json=payload,
                        )
                        before_reset = _database_dump(database_path)
                        reset_while_pending = contender_client.post("/api/demo/reset")
                    else:
                        before_reset = _database_dump(database_path)
                        reset_while_pending = contender_client.post("/api/demo/reset")
                        same_key_contender = contender_client.post(
                            "/api/recoveries",
                            json=payload,
                        )
                    after_refused_reset = _database_dump(database_path)
                    attempts_before_release = attempts.orchestrations
                finally:
                    coordinator.release.set()
                owner = owner_future.result(timeout=3)

            completed_recovery_id = owner.json()["recoveryId"]
            later_reset = contender_client.post("/api/demo/reset")
            post_reset = contender_client.post(
                "/api/recoveries",
                json=payload,
            )
    finally:
        owner_store.close()
        contender_store.close()

    _assert_reset_creation_pending(reset_while_pending)
    assert after_refused_reset == before_reset
    _assert_creation_error(
        same_key_contender,
        code="creation_outcome_unknown" if stale_owner else "creation_pending",
    )
    assert attempts_before_release == 1
    assert owner.status_code == 201
    assert later_reset.status_code == 200
    assert later_reset.json() == {"reset": True}
    assert post_reset.status_code == 201
    assert post_reset.json()["recoveryId"] != completed_recovery_id
    assert attempts.orchestrations == 2
    assert _scalar(database_path, "SELECT COUNT(*) FROM recovery_creations") == 1
    assert _scalar(database_path, "SELECT COUNT(*) FROM recoveries") == 1
    _assert_raw_key_absent(
        raw_key,
        database_path=database_path,
        responses=[
            reset_while_pending,
            same_key_contender,
            owner,
            later_reset,
            post_reset,
        ],
    )


@pytest.mark.parametrize("claim_status", ["reserved", "started", "unknown"])
def test_store_reset_rolls_back_without_mutation_for_unresolved_creation(
    tmp_path: Path,
    *,
    claim_status: str,
) -> None:
    database_path = tmp_path / f"reset-fence-{claim_status}.sqlite3"
    store = SQLiteStore(database_path)
    session_key = "a" * 64
    existing_recovery_id = str(uuid4())
    reserved_recovery_id = str(uuid4())
    started_at = datetime(2026, 7, 24, tzinfo=UTC)
    store.create_recovery(
        recovery_id=existing_recovery_id,
        scenario_id=ScenarioId.HOTEL,
        execution_mode=ExecutionMode.SDK_STUB,
        current_step=0,
        current_step_summary="Existing session recovery before guarded reset.",
        session_key=session_key,
    )
    assert (
        store.try_admit_live_recovery(
            recovery_id=existing_recovery_id,
            ip_key="b" * 64,
            session_key=session_key,
            max_active=2,
            ip_cooldown=timedelta(seconds=60),
            session_cooldown=timedelta(seconds=60),
            daily_budget_units=10,
            lease_ttl=timedelta(minutes=5),
            now=started_at,
        )
        is None
    )
    claim = store.claim_recovery_creation(
        request_key="c" * 64,
        request_fingerprint="d" * 64,
        scenario_id=ScenarioId.HOTEL,
        execution_mode=ExecutionMode.SDK_STUB,
        reserved_recovery_id=reserved_recovery_id,
        session_key=session_key,
        ip_key="a" * 64,
        expires_at=started_at + timedelta(days=1),
        now=started_at,
    )
    assert claim.disposition == "owner"
    if claim_status in {"started", "unknown"}:
        store.mark_recovery_creation_started(
            request_key="c" * 64,
            request_fingerprint="d" * 64,
            now=started_at,
        )
    if claim_status == "unknown":
        store.mark_recovery_creation_unknown(
            request_key="c" * 64,
            request_fingerprint="d" * 64,
            now=started_at,
        )
    before_reset = _database_dump(database_path)

    with pytest.raises(
        ResetCreationPendingError,
        match="unresolved recovery creation",
    ):
        store.reset(session_key)

    assert _database_dump(database_path) == before_reset
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT status FROM recovery_creations WHERE session_key = ?",
            (session_key,),
        ).fetchone() == (claim_status,)
        assert connection.execute(
            "SELECT COUNT(*) FROM recovery_access WHERE session_key = ?",
            (session_key,),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT released_at FROM live_admissions WHERE session_key = ?",
            (session_key,),
        ).fetchone() == (None,)
    blocked = store.claim_recovery_creation(
        request_key="e" * 64,
        request_fingerprint="f" * 64,
        scenario_id=ScenarioId.HOTEL,
        execution_mode=ExecutionMode.SDK_STUB,
        reserved_recovery_id=str(uuid4()),
        session_key=session_key,
        ip_key="a" * 64,
        expires_at=started_at + timedelta(days=1),
        max_per_session=1,
        max_global=1,
        now=started_at,
    )
    assert blocked.disposition == "capacity"
    store.close()


@pytest.mark.parametrize("mark_started", [False, True])
def test_stale_reserved_or_started_claim_becomes_permanently_unknown(
    tmp_path: Path,
    *,
    mark_started: bool,
) -> None:
    store = SQLiteStore(tmp_path / f"stale-{mark_started}.sqlite3")
    request_key = "a" * 64
    request_fingerprint = "b" * 64
    session_key = "c" * 64
    recovery_id = str(uuid4())
    started_at = datetime(2026, 7, 24, tzinfo=UTC)
    first = store.claim_recovery_creation(
        request_key=request_key,
        request_fingerprint=request_fingerprint,
        scenario_id=ScenarioId.HOTEL,
        execution_mode=ExecutionMode.SDK_STUB,
        reserved_recovery_id=recovery_id,
        session_key=session_key,
        ip_key="a" * 64,
        expires_at=started_at + timedelta(days=1),
        now=started_at,
    )
    assert first.disposition == "owner"
    if mark_started:
        store.mark_recovery_creation_started(
            request_key=request_key,
            request_fingerprint=request_fingerprint,
            now=started_at,
        )

    stale = store.claim_recovery_creation(
        request_key=request_key,
        request_fingerprint=request_fingerprint,
        scenario_id=ScenarioId.HOTEL,
        execution_mode=ExecutionMode.SDK_STUB,
        reserved_recovery_id=str(uuid4()),
        session_key=session_key,
        ip_key="a" * 64,
        expires_at=started_at + timedelta(days=1),
        stale_after=timedelta(seconds=1),
        now=started_at + timedelta(seconds=1),
    )
    retry = store.claim_recovery_creation(
        request_key=request_key,
        request_fingerprint=request_fingerprint,
        scenario_id=ScenarioId.HOTEL,
        execution_mode=ExecutionMode.SDK_STUB,
        reserved_recovery_id=str(uuid4()),
        session_key=session_key,
        ip_key="a" * 64,
        expires_at=started_at + timedelta(days=1),
        now=started_at + timedelta(seconds=2),
    )

    assert stale.disposition == "unknown"
    assert stale.recovery_id == recovery_id
    assert retry.disposition == "unknown"
    store.close()


def test_future_dated_creation_claim_fails_closed_instead_of_remaining_pending(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "future-dated-claim.sqlite3"
    store = SQLiteStore(database_path)
    request_key = "d" * 64
    request_fingerprint = "e" * 64
    session_key = "f" * 64
    recovery_id = str(uuid4())
    started_at = datetime(2026, 7, 24, tzinfo=UTC)
    owner = store.claim_recovery_creation(
        request_key=request_key,
        request_fingerprint=request_fingerprint,
        scenario_id=ScenarioId.HOTEL,
        execution_mode=ExecutionMode.SDK_STUB,
        reserved_recovery_id=recovery_id,
        session_key=session_key,
        ip_key="a" * 64,
        expires_at=started_at + timedelta(days=2),
        now=started_at,
    )
    assert owner.disposition == "owner"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE recovery_creations SET updated_at = ? WHERE request_key = ?",
            ((started_at + timedelta(days=1)).isoformat(), request_key),
        )

    retry = store.claim_recovery_creation(
        request_key=request_key,
        request_fingerprint=request_fingerprint,
        scenario_id=ScenarioId.HOTEL,
        execution_mode=ExecutionMode.SDK_STUB,
        reserved_recovery_id=str(uuid4()),
        session_key=session_key,
        ip_key="a" * 64,
        expires_at=started_at + timedelta(days=2),
        now=started_at + timedelta(seconds=1),
    )

    assert retry.disposition == "unknown"
    assert retry.recovery_id == recovery_id
    store.close()


def test_expired_creation_claim_cleanup_is_bounded_and_session_scoped(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "expired-claims.sqlite3"
    store = SQLiteStore(database_path)
    created_at = datetime(2026, 7, 24, tzinfo=UTC)
    sessions = ("1" * 64, "2" * 64, "3" * 64)
    for index, session_key in enumerate(sessions, start=1):
        store.claim_recovery_creation(
            request_key=f"{index:x}" * 64,
            request_fingerprint=f"{index + 3:x}" * 64,
            scenario_id=ScenarioId.HOTEL,
            execution_mode=ExecutionMode.SDK_STUB,
            reserved_recovery_id=str(uuid4()),
            session_key=session_key,
            ip_key="a" * 64,
            expires_at=created_at + timedelta(seconds=index),
            now=created_at,
        )

    first_deleted = store.delete_expired_recovery_creations(
        expired_at=created_at + timedelta(seconds=3),
        batch_size=1,
    )
    second_deleted = store.delete_expired_recovery_creations(
        expired_at=created_at + timedelta(seconds=3),
        batch_size=1,
    )

    assert first_deleted == second_deleted == 1
    with sqlite3.connect(database_path) as connection:
        remaining = connection.execute("SELECT session_key FROM recovery_creations").fetchall()
    assert remaining == [(sessions[2],)]
    store.close()


def test_store_enforces_session_and_global_creation_capacity(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "creation-capacity.sqlite3")
    now = datetime(2026, 7, 24, tzinfo=UTC)
    first_session = "1" * 64
    second_session = "2" * 64
    third_session = "3" * 64

    first = _claim_creation(
        store,
        request_key="a" * 64,
        session_key=first_session,
        now=now,
        max_per_session=1,
        max_global=2,
    )
    session_full = _claim_creation(
        store,
        request_key="b" * 64,
        session_key=first_session,
        now=now,
        max_per_session=1,
        max_global=2,
    )
    second = _claim_creation(
        store,
        request_key="c" * 64,
        session_key=second_session,
        now=now,
        max_per_session=1,
        max_global=2,
    )
    globally_full = _claim_creation(
        store,
        request_key="d" * 64,
        session_key=third_session,
        now=now,
        max_per_session=1,
        max_global=2,
    )

    assert first.disposition == second.disposition == "owner"
    assert session_full.disposition == globally_full.disposition == "capacity"
    assert (
        _scalar(
            tmp_path / "creation-capacity.sqlite3",
            "SELECT COUNT(*) FROM recovery_creations",
        )
        == 2
    )
    store.close()


def test_public_creation_capacity_bounds_fresh_sessions_by_opaque_ip(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "public-ip-capacity.sqlite3"
    store = SQLiteStore(database_path)
    settings = RuntimeSettings(
        live_ready=False,
        identity_hash_secret=_IDENTITY_SECRET,
        max_recovery_creations_per_session=8,
        max_recovery_creations_per_ip=2,
        max_recovery_creations_global=8,
    )
    app = _create_test_app(settings, store=store)
    shared_ip = "198.51.100.21"
    different_ip = "203.0.113.22"

    with TestClient(app, client=(shared_ip, 50_000)) as client:
        accepted = []
        for request_id in ("same-ip-one", "same-ip-two"):
            client.cookies.clear()
            accepted.append(
                client.post(
                    "/api/recoveries",
                    json=_payload(
                        request_id,
                        scenario_id="api-quota",
                        execution_mode="replay_fixture",
                    ),
                )
            )
        client.cookies.clear()
        denied = client.post(
            "/api/recoveries",
            json=_payload(
                "same-ip-denied",
                scenario_id="api-quota",
                execution_mode="replay_fixture",
            ),
        )
    with TestClient(app, client=(different_ip, 50_000)) as other_client:
        other = other_client.post(
            "/api/recoveries",
            json=_payload(
                "different-ip-owner",
                scenario_id="api-quota",
                execution_mode="replay_fixture",
            ),
        )

    assert [response.status_code for response in accepted] == [201, 201]
    _assert_creation_error(denied, code="creation_capacity")
    assert "retry-after" not in denied.headers
    assert other.status_code == 201
    with sqlite3.connect(database_path) as connection:
        ip_keys = [
            str(row[0])
            for row in connection.execute(
                "SELECT ip_key FROM recovery_creations ORDER BY request_key"
            ).fetchall()
        ]
    assert len(ip_keys) == 3
    assert len(set(ip_keys)) == 2
    assert all(re.fullmatch(r"[0-9a-f]{64}", ip_key) for ip_key in ip_keys)
    _assert_raw_key_absent(
        shared_ip,
        database_path=database_path,
        responses=[*accepted, denied, other],
    )
    _assert_raw_key_absent(
        different_ip,
        database_path=database_path,
        responses=[*accepted, denied, other],
    )
    store.close()


def test_exact_creation_retry_bypasses_new_ip_capacity_after_ip_mobility(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "creation-ip-mobility.sqlite3"
    store = SQLiteStore(database_path)
    settings = RuntimeSettings(
        live_ready=False,
        identity_hash_secret=_IDENTITY_SECRET,
        max_recovery_creations_per_session=4,
        max_recovery_creations_per_ip=1,
        max_recovery_creations_global=4,
    )
    app = _create_test_app(settings, store=store)
    first_ip = "198.51.100.31"
    moved_ip = "203.0.113.32"
    payload = _payload(
        "mobile-exact-key",
        scenario_id="api-quota",
        execution_mode="replay_fixture",
    )

    with TestClient(app, client=(first_ip, 50_000)) as first_client:
        first = first_client.post("/api/recoveries", json=payload)
        raw_cookie = first_client.cookies.get(_COOKIE_NAME)
    assert isinstance(raw_cookie, str)
    with sqlite3.connect(database_path) as connection:
        original_row = connection.execute(
            "SELECT request_key, ip_key FROM recovery_creations"
        ).fetchone()
    assert original_row is not None
    original_request_key, original_ip_key = original_row

    with TestClient(app, client=(moved_ip, 50_000)) as moved_client:
        saturated = moved_client.post(
            "/api/recoveries",
            json=_payload(
                "moved-ip-owner",
                scenario_id="api-quota",
                execution_mode="replay_fixture",
            ),
        )
        moved_client.cookies.clear()
        _attach_session(moved_client, raw_cookie)
        exact_retry = moved_client.post("/api/recoveries", json=payload)

    assert first.status_code == saturated.status_code == exact_retry.status_code == 201
    assert exact_retry.json() == first.json()
    with sqlite3.connect(database_path) as connection:
        stored_ip_key = connection.execute(
            "SELECT ip_key FROM recovery_creations WHERE request_key = ?",
            (original_request_key,),
        ).fetchone()
        row_count = connection.execute("SELECT COUNT(*) FROM recovery_creations").fetchone()
    assert stored_ip_key == (original_ip_key,)
    assert row_count == (2,)
    store.close()


def test_expired_same_ip_creation_backlog_does_not_consume_capacity(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "expired-ip-capacity.sqlite3"
    store = SQLiteStore(database_path)
    now = datetime(2026, 7, 24, tzinfo=UTC)
    ip_key = "a" * 64
    expired = _claim_creation(
        store,
        request_key="b" * 64,
        session_key="c" * 64,
        ip_key=ip_key,
        now=now,
        expires_at=now + timedelta(seconds=1),
        max_per_ip=1,
    )
    replacement = _claim_creation(
        store,
        request_key="d" * 64,
        session_key="e" * 64,
        ip_key=ip_key,
        now=now + timedelta(seconds=2),
        expires_at=now + timedelta(days=1),
        max_per_ip=1,
    )

    assert expired.disposition == replacement.disposition == "owner"
    assert _scalar(database_path, "SELECT COUNT(*) FROM recovery_creations") == 2
    store.close()


def test_per_ip_creation_capacity_is_atomic_across_store_instances(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "atomic-ip-capacity.sqlite3"
    stores = (SQLiteStore(database_path), SQLiteStore(database_path))
    now = datetime(2026, 7, 24, tzinfo=UTC)
    barrier = Event()

    def claim(index: int) -> str:
        barrier.wait(timeout=5)
        result = _claim_creation(
            stores[index],
            request_key=("a" if index == 0 else "b") * 64,
            session_key=("c" if index == 0 else "d") * 64,
            ip_key="e" * 64,
            now=now,
            max_per_session=1,
            max_per_ip=1,
            max_global=2,
        )
        return result.disposition

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(claim, index) for index in range(2)]
        barrier.set()
        dispositions = sorted(future.result(timeout=10) for future in futures)

    assert dispositions == ["capacity", "owner"]
    assert _scalar(database_path, "SELECT COUNT(*) FROM recovery_creations") == 1
    for store in stores:
        store.close()


@pytest.mark.parametrize(
    ("stored_status", "retry_fingerprint", "expected_retry"),
    [
        ("reserved", "f" * 64, "pending"),
        ("started", "f" * 64, "pending"),
        ("ready", "f" * 64, "unknown"),
        ("unknown", "f" * 64, "unknown"),
        ("reserved", "e" * 64, "conflict"),
    ],
)
def test_exact_creation_key_semantics_bypass_capacity_for_every_status(
    tmp_path: Path,
    *,
    stored_status: str,
    retry_fingerprint: str,
    expected_retry: str,
) -> None:
    database_path = tmp_path / f"exact-capacity-{stored_status}-{expected_retry}.sqlite3"
    store = SQLiteStore(database_path)
    now = datetime(2026, 7, 24, tzinfo=UTC)
    request_key = "a" * 64
    session_key = "b" * 64
    expires_at = now + timedelta(days=1)
    owner = _claim_creation(
        store,
        request_key=request_key,
        session_key=session_key,
        now=now,
        expires_at=expires_at,
        max_per_session=1,
        max_global=1,
    )
    assert owner.disposition == "owner"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE recovery_creations SET status = ? WHERE request_key = ?",
            (stored_status, request_key),
        )

    retry = _claim_creation(
        store,
        request_key=request_key,
        request_fingerprint=retry_fingerprint,
        session_key=session_key,
        now=now + timedelta(seconds=1),
        expires_at=expires_at,
        max_per_session=1,
        max_global=1,
    )
    new_key = _claim_creation(
        store,
        request_key="c" * 64,
        session_key=session_key,
        now=now + timedelta(seconds=1),
        expires_at=expires_at,
        max_per_session=1,
        max_global=1,
    )

    assert retry.disposition == expected_retry
    assert new_key.disposition == "capacity"
    assert _scalar(database_path, "SELECT COUNT(*) FROM recovery_creations") == 1
    store.close()


def test_expired_creation_backlog_does_not_consume_capacity(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "expired-capacity.sqlite3"
    store = SQLiteStore(database_path)
    now = datetime(2026, 7, 24, tzinfo=UTC)
    expired_claims = [
        _claim_creation(
            store,
            request_key=f"{index:064x}",
            session_key="b" * 64,
            now=now,
            expires_at=now + timedelta(seconds=1),
            max_per_session=32,
            max_global=32,
        )
        for index in range(1, 31)
    ]
    replacement = _claim_creation(
        store,
        request_key="f" * 64,
        session_key="b" * 64,
        now=now + timedelta(seconds=2),
        expires_at=now + timedelta(days=1),
        max_per_session=1,
        max_global=1,
    )

    assert {claim.disposition for claim in expired_claims} == {"owner"}
    assert replacement.disposition == "owner"
    assert _scalar(database_path, "SELECT COUNT(*) FROM recovery_creations") == 31
    store.close()


def test_terminal_recovery_cleanup_does_not_release_unexpired_creation_capacity(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "terminal-cleanup-capacity.sqlite3"
    store = SQLiteStore(database_path)
    now = datetime(2026, 7, 24, tzinfo=UTC)
    session_key = "b" * 64
    request_key = "a" * 64
    request_fingerprint = "f" * 64
    loader = ScenarioLoader()
    scenario = loader.get(ScenarioId.HOTEL)
    recovery_id = replay_recovery_id(scenario)
    claim = store.claim_recovery_creation(
        request_key=request_key,
        request_fingerprint=request_fingerprint,
        scenario_id=ScenarioId.HOTEL,
        execution_mode=ExecutionMode.REPLAY_FIXTURE,
        reserved_recovery_id=recovery_id,
        session_key=session_key,
        ip_key="a" * 64,
        expires_at=now + timedelta(days=1),
        max_per_session=1,
        max_global=1,
        now=now,
    )
    assert claim.disposition == "owner"
    store.mark_recovery_creation_started(
        request_key=request_key,
        request_fingerprint=request_fingerprint,
        now=now,
    )
    created = ReplayEngine(store, loader).start(
        ScenarioId.HOTEL,
        execution_mode=ExecutionMode.REPLAY_FIXTURE,
        session_key=session_key,
    )
    store.mark_recovery_creation_ready(
        request_key=request_key,
        request_fingerprint=request_fingerprint,
        recovery_id=created.recovery_id,
        session_key=session_key,
        now=now,
    )

    deleted = store.delete_terminal_recoveries(
        updated_before=now + timedelta(days=7),
        batch_size=1,
    )
    blocked = _claim_creation(
        store,
        request_key="c" * 64,
        session_key=session_key,
        now=now + timedelta(seconds=1),
        expires_at=now + timedelta(days=1),
        max_per_session=1,
        max_global=1,
    )

    assert deleted == 1
    assert blocked.disposition == "capacity"
    assert _scalar(database_path, "SELECT COUNT(*) FROM recovery_creations") == 1
    store.close()


def test_global_creation_capacity_is_atomic_across_store_instances(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "atomic-global-capacity.sqlite3"
    stores = (SQLiteStore(database_path), SQLiteStore(database_path))
    now = datetime(2026, 7, 24, tzinfo=UTC)
    barrier = Event()

    def claim(index: int) -> str:
        barrier.wait(timeout=5)
        result = _claim_creation(
            stores[index],
            request_key=("a" if index == 0 else "b") * 64,
            session_key=("c" if index == 0 else "d") * 64,
            now=now,
            max_per_session=1,
            max_global=1,
        )
        return result.disposition

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(claim, index) for index in range(2)]
        barrier.set()
        dispositions = sorted(future.result(timeout=10) for future in futures)

    assert dispositions == ["capacity", "owner"]
    assert _scalar(database_path, "SELECT COUNT(*) FROM recovery_creations") == 1
    for store in stores:
        store.close()


def test_abandon_and_ready_reset_release_creation_capacity(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "released-capacity.sqlite3"
    store = SQLiteStore(database_path)
    now = datetime(2026, 7, 24, tzinfo=UTC)
    session_key = "b" * 64
    reserved = _claim_creation(
        store,
        request_key="a" * 64,
        session_key=session_key,
        now=now,
        max_per_session=1,
        max_global=1,
    )
    assert reserved.disposition == "owner"
    assert store.abandon_reserved_recovery_creation(
        request_key="a" * 64,
        request_fingerprint="f" * 64,
    )
    ready = _claim_creation(
        store,
        request_key="c" * 64,
        session_key=session_key,
        now=now,
        max_per_session=1,
        max_global=1,
    )
    assert ready.disposition == "owner"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE recovery_creations SET status = 'ready' WHERE request_key = ?",
            ("c" * 64,),
        )

    store.reset(session_key)
    after_reset = _claim_creation(
        store,
        request_key="d" * 64,
        session_key=session_key,
        now=now,
        max_per_session=1,
        max_global=1,
    )
    assert after_reset.disposition == "owner"
    store.close()


def test_public_creation_capacity_is_exact_and_has_no_start_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "public-capacity.sqlite3"
    attempts = _Attempts()
    store = _CountingStore(database_path, attempts)
    settings = RuntimeSettings(
        live_ready=False,
        identity_hash_secret=_IDENTITY_SECRET,
        max_recovery_creations_per_session=1,
        max_recovery_creations_global=1,
    )
    generated_ids = iter(
        (
            UUID("11111111-2222-4333-8444-555555555555"),
            UUID("99999999-8888-4777-8666-555555555555"),
            UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"),
        )
    )
    monkeypatch.setattr(server_main, "uuid4", lambda: next(generated_ids))
    raw_first = "capacity-first-raw-token"
    raw_denied = "capacity-denied-raw-token"
    client = TestClient(
        _create_test_app(
            settings,
            store=store,
            orchestrator=_RecordingOrchestrator(store, attempts),
        )
    )

    accepted = client.post("/api/recoveries", json=_payload(raw_first))
    denied = client.post("/api/recoveries", json=_payload(raw_denied))
    exact_retry = client.post("/api/recoveries", json=_payload(raw_first))

    assert accepted.status_code == 201
    _assert_creation_error(denied, code="creation_capacity")
    assert exact_retry.status_code == 201
    assert exact_retry.json() == accepted.json()
    assert set(denied.json()) == {"code", "message", "requestId"}
    assert "retry-after" not in denied.headers
    assert "fallbackExecutionMode" not in denied.text
    assert "count" not in denied.text.lower()
    assert "limit" not in denied.text.lower()
    assert attempts.orchestrations == 1
    assert attempts.admission_checks == attempts.budget_checks == 0
    assert _scalar(database_path, "SELECT COUNT(*) FROM recoveries") == 1
    assert _scalar(database_path, "SELECT COUNT(*) FROM recovery_creations") == 1
    _assert_raw_key_absent(
        raw_denied,
        database_path=database_path,
        responses=[denied, exact_retry],
    )
    denied_reserved_id = "99999999-8888-4777-8666-555555555555"
    assert denied_reserved_id not in _database_dump(database_path)
    assert denied_reserved_id not in denied.text
    assert denied_reserved_id not in repr(dict(denied.headers))
    assert "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee" not in _database_dump(database_path)
    store.close()


def test_known_live_admission_denial_releases_creation_capacity(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "admission-release-capacity.sqlite3"
    attempts = _Attempts()
    store = _CountingStore(database_path, attempts)
    settings = RuntimeSettings(
        live_ready=False,
        identity_hash_secret=_IDENTITY_SECRET,
        max_recovery_creations_per_session=1,
        max_recovery_creations_global=1,
    )
    client = TestClient(
        _create_test_app(
            settings,
            store=store,
            orchestrator=_RecordingOrchestrator(store, attempts),
        )
    )

    denied = client.post(
        "/api/recoveries",
        json=_payload("known-live-denial", execution_mode="openai_live"),
    )
    accepted = client.post(
        "/api/recoveries",
        json=_payload("after-live-denial", execution_mode="sdk_stub"),
    )

    assert denied.status_code == 422
    assert accepted.status_code == 201
    assert attempts.orchestrations == 1
    assert _scalar(database_path, "SELECT COUNT(*) FROM recovery_creations") == 1
    store.close()


def test_lost_response_retry_uses_durable_ready_claim_without_rerun(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "lost-response.sqlite3"
    attempts = _Attempts()
    settings = _settings()
    raw_key = "lost-response-key-001"
    payload = _payload(raw_key)
    first_store = _CountingStore(database_path, attempts)
    first_app = _create_test_app(
        settings,
        store=first_store,
        orchestrator=_RecordingOrchestrator(first_store, attempts),
    )

    with TestClient(_DropFirstCreateResponse(first_app)) as first_client:
        session_cookie = _bootstrap_session(first_client)
        with pytest.raises(_LostResponse):
            first_client.post("/api/recoveries", json=payload)
    first_store.close()

    with sqlite3.connect(database_path) as connection:
        recovery_rows = connection.execute("SELECT id FROM recoveries").fetchall()
    assert len(recovery_rows) == 1
    persisted_recovery_id = str(recovery_rows[0][0])

    second_store = _CountingStore(database_path, attempts)
    restarted_orchestrator = _RecordingOrchestrator(second_store, attempts)
    with TestClient(
        _create_test_app(
            settings,
            store=second_store,
            orchestrator=restarted_orchestrator,
        )
    ) as restarted_client:
        _attach_session(restarted_client, session_cookie)
        retried = restarted_client.post("/api/recoveries", json=payload)

    assert retried.status_code == 201
    assert retried.json()["recoveryId"] == persisted_recovery_id
    assert attempts.orchestrations == 1
    assert _scalar(database_path, "SELECT COUNT(*) FROM recoveries") == 1
    _assert_raw_key_absent(
        raw_key,
        database_path=database_path,
        responses=[retried],
    )
    second_store.close()


def test_post_start_error_does_not_reconcile_bare_status_flip(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "completed-before-error.sqlite3"
    attempts = _Attempts()
    store = _CountingStore(database_path, attempts)
    payload = _payload("completed-before-error-001")
    orchestrator = _CompletedThenUncertainOrchestrator(store, attempts)

    with TestClient(
        _create_test_app(
            _settings(),
            store=store,
            orchestrator=orchestrator,
        )
    ) as client:
        first = client.post("/api/recoveries", json=payload)
        retry = client.post("/api/recoveries", json=payload)

    _assert_creation_error(first, code="creation_outcome_unknown")
    _assert_creation_error(retry, code="creation_outcome_unknown")
    assert attempts.orchestrations == 1
    store.close()


def test_post_start_error_reconciles_authoritative_active_evidence(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "authoritative-before-error.sqlite3"
    attempts = _Attempts()
    store = _CountingStore(database_path, attempts)
    payload = _payload("authoritative-before-error-001")
    orchestrator = _AuthoritativeThenUncertainOrchestrator(store, attempts)

    with TestClient(
        _create_test_app(
            _settings(),
            store=store,
            orchestrator=orchestrator,
        )
    ) as client:
        first = client.post("/api/recoveries", json=payload)
        retry = client.post("/api/recoveries", json=payload)

    assert [first.status_code, retry.status_code] == [201, 201]
    assert first.json()["recoveryId"] == retry.json()["recoveryId"]
    assert first.json()["status"] == retry.json()["status"] == "pending_approval"
    assert attempts.orchestrations == 1
    store.close()


def test_uncertain_started_claim_fails_closed_and_never_reruns(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "uncertain-start.sqlite3"
    first_attempts = _Attempts()
    settings = _settings()
    raw_key = "uncertain-create-key-001"
    payload = _payload(raw_key)
    first_store = _CountingStore(database_path, first_attempts)

    with TestClient(
        _create_test_app(
            settings,
            store=first_store,
            orchestrator=_UncertainOrchestrator(first_attempts),
        )
    ) as first_client:
        session_cookie = _bootstrap_session(first_client)
        first = first_client.post("/api/recoveries", json=payload)
    first_store.close()

    _assert_creation_error(first, code="creation_outcome_unknown")
    assert first_attempts.orchestrations == 1

    retry_attempts = _Attempts()
    second_store = _CountingStore(database_path, retry_attempts)
    with TestClient(
        _create_test_app(
            settings,
            store=second_store,
            orchestrator=_RecordingOrchestrator(second_store, retry_attempts),
        )
    ) as restarted_client:
        _attach_session(restarted_client, session_cookie)
        first_retry = restarted_client.post("/api/recoveries", json=payload)
        second_retry = restarted_client.post("/api/recoveries", json=payload)

    first_message = _assert_creation_error(
        first_retry,
        code="creation_outcome_unknown",
    )
    second_message = _assert_creation_error(
        second_retry,
        code="creation_outcome_unknown",
    )
    assert first_message == second_message
    assert retry_attempts.orchestrations == 0
    assert _scalar(database_path, "SELECT COUNT(*) FROM recoveries") == 0
    _assert_raw_key_absent(
        raw_key,
        database_path=database_path,
        responses=[first, first_retry, second_retry],
    )
    second_store.close()


def test_session_scoped_creation_claims_preserve_canonical_replay_sharing(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "canonical-replay.sqlite3"
    store = SQLiteStore(database_path)
    settings = _settings()
    first_key = "replay-session-one"
    second_key = "replay-session-two"

    with (
        TestClient(_create_test_app(settings, store=store)) as first_client,
        TestClient(_create_test_app(settings, store=store)) as second_client,
    ):
        assert _bootstrap_session(first_client) != _bootstrap_session(second_client)
        first = first_client.post(
            "/api/recoveries",
            json=_payload(
                first_key,
                scenario_id="api-quota",
                execution_mode="replay_fixture",
            ),
        )
        second = second_client.post(
            "/api/recoveries",
            json=_payload(
                second_key,
                scenario_id="api-quota",
                execution_mode="replay_fixture",
            ),
        )

    expected_recovery_id = replay_recovery_id(ScenarioLoader().get("api-quota"))
    assert [first.status_code, second.status_code] == [201, 201]
    assert first.json()["recoveryId"] == expected_recovery_id
    assert second.json()["recoveryId"] == expected_recovery_id
    assert _scalar(database_path, "SELECT COUNT(*) FROM recoveries") == 1
    assert _scalar(database_path, "SELECT COUNT(*) FROM recovery_access") == 2
    _assert_raw_key_absent(
        first_key,
        database_path=database_path,
        responses=[first, second],
    )
    _assert_raw_key_absent(
        second_key,
        database_path=database_path,
        responses=[first, second],
    )
    store.close()


def test_reset_deletes_only_calling_session_creation_claims(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "creation-reset.sqlite3"
    store = SQLiteStore(database_path)
    settings = RuntimeSettings(
        live_ready=False,
        demo_reset_enabled=True,
        identity_hash_secret=_IDENTITY_SECRET,
    )
    first_payload = _payload(
        "reset-first-replay",
        scenario_id="api-quota",
        execution_mode="replay_fixture",
    )
    second_payload = _payload(
        "reset-second-replay",
        scenario_id="api-quota",
        execution_mode="replay_fixture",
    )

    with (
        TestClient(_create_test_app(settings, store=store)) as first,
        TestClient(_create_test_app(settings, store=store)) as second,
    ):
        first_created = first.post("/api/recoveries", json=first_payload)
        second_created = second.post("/api/recoveries", json=second_payload)
        assert first_created.status_code == second_created.status_code == 201
        recovery_id = first_created.json()["recoveryId"]
        assert second_created.json()["recoveryId"] == recovery_id

        with sqlite3.connect(database_path) as connection:
            before = connection.execute(
                "SELECT session_key FROM recovery_creations ORDER BY session_key"
            ).fetchall()
        assert len(before) == 2

        assert first.post("/api/demo/reset").status_code == 200
        with sqlite3.connect(database_path) as connection:
            after = connection.execute("SELECT session_key FROM recovery_creations").fetchall()
            remaining_access = connection.execute(
                "SELECT session_key FROM recovery_access WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchall()
        assert after == remaining_access
        assert len(after) == 1
        second_retry = second.post("/api/recoveries", json=second_payload)

    assert second_retry.status_code == 201
    assert second_retry.json()["recoveryId"] == recovery_id
    store.close()


def test_create_removes_expired_claims_before_new_ownership(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "per-create-claim-cleanup.sqlite3"
    store = SQLiteStore(database_path)
    settings = _settings()
    with TestClient(_create_test_app(settings, store=store)) as client:
        first = client.post(
            "/api/recoveries",
            json=_payload(
                "expired-before-next-create",
                scenario_id="api-quota",
                execution_mode="replay_fixture",
            ),
        )
        assert first.status_code == 201
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                "UPDATE recovery_creations SET expires_at = ?",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
            )
        second = client.post(
            "/api/recoveries",
            json=_payload(
                "new-create-after-expiry",
                scenario_id="api-quota",
                execution_mode="replay_fixture",
            ),
        )

    assert second.status_code == 201
    assert _scalar(database_path, "SELECT COUNT(*) FROM recovery_creations") == 1
    store.close()


@pytest.mark.parametrize(
    "tamper_sql",
    [
        "DELETE FROM receipts",
        "DELETE FROM events WHERE seq = 2",
    ],
)
def test_exact_ready_replay_retry_revalidates_canonical_integrity(
    tmp_path: Path,
    tamper_sql: str,
) -> None:
    database_path = tmp_path / f"tampered-replay-{uuid4().hex}.sqlite3"
    store = SQLiteStore(database_path)
    payload = _payload(
        "tampered-replay-retry-001",
        scenario_id="api-quota",
        execution_mode="replay_fixture",
    )
    with TestClient(_create_test_app(_settings(), store=store)) as client:
        first = client.post("/api/recoveries", json=payload)
        assert first.status_code == 201
        with sqlite3.connect(database_path) as connection:
            connection.execute(tamper_sql)
        retry = client.post("/api/recoveries", json=payload)

    _assert_creation_error(retry, code="creation_outcome_unknown")
    assert (
        _scalar(
            database_path,
            "SELECT COUNT(*) FROM recovery_creations WHERE status = 'unknown'",
        )
        == 1
    )
    store.close()


@pytest.mark.parametrize(
    "tamper_sql",
    [
        "UPDATE remedies SET cost_delta_minor = cost_delta_minor + 1",
        "UPDATE events SET type = 'provider.executed' WHERE seq = 2",
        (
            "INSERT INTO receipts (recovery_id, receipt_json, created_at) "
            "SELECT id, '{}', updated_at FROM recoveries"
        ),
    ],
)
def test_exact_ready_sdk_retry_revalidates_active_hotel_integrity(
    tmp_path: Path,
    tamper_sql: str,
) -> None:
    database_path = tmp_path / f"tampered-sdk-{uuid4().hex}.sqlite3"
    store = SQLiteStore(database_path)
    payload = _payload(f"tampered-sdk-retry-{uuid4().hex}")
    with TestClient(_create_test_app(_settings(), store=store)) as client:
        first = client.post("/api/recoveries", json=payload)
        assert first.status_code == 201

        valid_retry = client.post("/api/recoveries", json=payload)
        assert valid_retry.status_code == 201
        assert valid_retry.json() == first.json()

        with sqlite3.connect(database_path) as connection:
            connection.execute(tamper_sql)
        corrupt_retry = client.post("/api/recoveries", json=payload)

    _assert_creation_error(corrupt_retry, code="creation_outcome_unknown")
    assert _scalar(database_path, "SELECT COUNT(*) FROM recoveries") == 1
    assert _scalar(database_path, "SELECT COUNT(*) FROM recovery_creations") == 1
    assert (
        _scalar(
            database_path,
            "SELECT COUNT(*) FROM recovery_creations WHERE status = 'unknown'",
        )
        == 1
    )
    store.close()


@pytest.mark.parametrize(
    "tamper_sql",
    [
        "UPDATE remedies SET cost_delta_minor = cost_delta_minor + 1",
        "UPDATE events SET type = 'provider.executed' WHERE seq = 2",
        """
        UPDATE recoveries
        SET updated_at = (
            SELECT created_at FROM events
            WHERE events.recovery_id = recoveries.id AND seq = 2
        );
        UPDATE pending_approvals
        SET updated_at = (
            SELECT created_at FROM events
            WHERE events.recovery_id = pending_approvals.recovery_id AND seq = 2
        );
        UPDATE receipts
        SET created_at = (
            SELECT created_at FROM events
            WHERE events.recovery_id = receipts.recovery_id AND seq = 2
        );
        UPDATE events
        SET created_at = (
            SELECT approval.created_at FROM events AS approval
            WHERE approval.recovery_id = events.recovery_id AND approval.seq = 2
        )
        WHERE terminal = 1;
        """,
    ],
)
def test_exact_ready_sdk_retry_revalidates_expiry_source_integrity(
    tmp_path: Path,
    tamper_sql: str,
) -> None:
    database_path = tmp_path / f"tampered-sdk-expiry-{uuid4().hex}.sqlite3"
    store = SQLiteStore(database_path)
    payload = _payload(f"tampered-sdk-expiry-retry-{uuid4().hex}")
    with TestClient(_create_test_app(_settings(), store=store)) as client:
        first = client.post("/api/recoveries", json=payload)
        assert first.status_code == 201
        recovery_id = str(first.json()["recoveryId"])
        with sqlite3.connect(database_path) as connection:
            expiry_row = connection.execute(
                "SELECT expiry FROM remedies WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone()
            assert expiry_row is not None
        expired_at = datetime.fromisoformat(str(expiry_row[0])) + timedelta(seconds=1)
        assert (
            store.expire_pending_approvals(
                now=expired_at,
                batch_size=1,
                recovery_id=recovery_id,
            )
            == 1
        )
        with sqlite3.connect(database_path) as connection:
            connection.executescript(tamper_sql)
        corrupt_retry = client.post("/api/recoveries", json=payload)

    _assert_creation_error(corrupt_retry, code="creation_outcome_unknown")
    assert _scalar(database_path, "SELECT COUNT(*) FROM recoveries") == 1
    assert _scalar(database_path, "SELECT COUNT(*) FROM recovery_creations") == 1
    store.close()
