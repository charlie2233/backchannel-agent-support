from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

import scripts.smoke_live as smoke_live
import server.main as server_main
from server.agents.live_models import LiveModelRequestError
from server.config import RuntimeSettings
from server.controls import PublicIdentityHasher
from server.main import create_app
from server.models import ApprovalDecisionRequest, ExecutionMode, ScenarioId
from server.orchestrator import LiveOperationTimeoutError, RecoveryOrchestrator
from server.providers.hotel_simulator import HotelSimulator
from server.store import SQLiteStore

_SECRET = "test-identity-secret-that-is-at-least-32-bytes"


def _settings(**overrides: object) -> RuntimeSettings:
    values: dict[str, object] = {
        "live_ready": True,
        "identity_hmac_secret": _SECRET,
        "live_cooldown": timedelta(0),
        "cleanup_interval": timedelta(hours=1),
    }
    values.update(overrides)
    return RuntimeSettings(**values)  # type: ignore[arg-type]


def test_live_operation_timeout_configuration_is_bounded(tmp_path, monkeypatch) -> None:
    defaults = RuntimeSettings(live_ready=False)
    assert defaults.live_operation_timeout == timedelta(seconds=60)

    monkeypatch.setenv("BACKCHANNEL_LIVE_OPERATION_TIMEOUT_SECONDS", "17")
    assert RuntimeSettings.from_environment().live_operation_timeout == timedelta(
        seconds=17
    )

    for seconds in (timedelta(0), timedelta(seconds=-1), timedelta(seconds=301)):
        with pytest.raises(ValueError, match="Live operation timeout.*1.*300"):
            RuntimeSettings(live_ready=False, live_operation_timeout=seconds)
    for raw in ("0", "301"):
        monkeypatch.setenv("BACKCHANNEL_LIVE_OPERATION_TIMEOUT_SECONDS", raw)
        with pytest.raises(
            ValueError, match="BACKCHANNEL_LIVE_OPERATION_TIMEOUT_SECONDS"
        ):
            RuntimeSettings.from_environment()

    store = SQLiteStore(tmp_path / "direct-orchestrator-timeout.sqlite3")
    for seconds in (0, 301):
        with pytest.raises(ValueError, match="Live operation timeout.*1.*300"):
            RecoveryOrchestrator(
                store=store,
                hotel_provider=HotelSimulator(store=store),
                live_operation_timeout=timedelta(seconds=seconds),
            )


def test_default_app_constructs_zero_retry_client_with_exact_timeout(
    tmp_path,
    monkeypatch,
) -> None:
    observed: list[dict[str, object]] = []

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs: object) -> None:
            observed.append(kwargs)
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(server_main, "AsyncOpenAI", FakeAsyncOpenAI)
    app = create_app(
        _settings(live_operation_timeout=timedelta(seconds=23)),
        store=SQLiteStore(tmp_path / "app-client-timeout.sqlite3"),
    )

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200

    assert observed == [{"timeout": 23.0, "max_retries": 0}]
    assert app.state.recovery_orchestrator._live_operation_timeout == timedelta(
        seconds=23
    )


class _FailingLiveOrchestrator:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.start_calls = 0
        self.decide_calls = 0

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
        assert execution_mode is ExecutionMode.OPENAI_LIVE
        assert session_hash is not None
        self.start_calls += 1
        raise self.error

    async def decide(self, *_args: object, **_kwargs: object) -> object:
        self.decide_calls += 1
        raise self.error


@pytest.mark.parametrize(
    "error",
    [LiveOperationTimeoutError(), LiveModelRequestError("timeout")],
)
def test_live_start_timeout_is_504_charged_and_releases_gate(
    tmp_path,
    error: Exception,
) -> None:
    database_path = tmp_path / "live-start-timeout.sqlite3"
    store = SQLiteStore(database_path)
    orchestrator = _FailingLiveOrchestrator(error)
    app = create_app(
        _settings(),
        store=store,
        orchestrator=orchestrator,  # type: ignore[arg-type]
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/recoveries",
            json={"scenarioId": "hotel", "executionMode": "openai_live"},
        )

    assert response.status_code == 504
    assert response.json()["error"] == {
        "code": "live_timeout",
        "message": "Live processing did not finish before the server deadline.",
        "requestId": response.headers["x-request-id"],
        "recoveryId": None,
        "retryAfterSeconds": None,
        "fallback": {
            "kind": "show_replay_fixture",
            "scenarioId": "hotel",
            "executionMode": "replay_fixture",
        },
    }
    assert "retry-after" not in response.headers
    assert "set-cookie" not in response.headers
    assert app.state.live_gate.active == 0
    assert orchestrator.start_calls == 1
    assert store.count_recoveries() == 0
    assert store.count_public_creation_usage_rows() == 3
    assert store.count_public_live_usage_rows() == 2


def test_non_timeout_live_model_error_stays_generic_503(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "live-other-error.sqlite3")
    app = create_app(
        _settings(),
        store=store,
        orchestrator=_FailingLiveOrchestrator(
            LiveModelRequestError("connection_error")
        ),  # type: ignore[arg-type]
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/recoveries",
            json={"scenarioId": "hotel", "executionMode": "openai_live"},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "live_unavailable"


def test_live_decision_timeout_is_correlated_504_without_fallback(tmp_path) -> None:
    database_path = tmp_path / "live-decision-timeout.sqlite3"
    store = SQLiteStore(database_path)
    signed_cookie, credential = PublicIdentityHasher(
        _SECRET
    ).demo_session_cookie_codec(lifetime_seconds=3600).mint(now=datetime.now(UTC))
    session_hash = PublicIdentityHasher(_SECRET).session(credential.nonce)
    setup = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(store=store),
    )
    pending = asyncio.run(
        setup.start(
            ScenarioId.HOTEL,
            execution_mode=ExecutionMode.SDK_STUB,
            session_hash=session_hash,
        )
    )
    recovery_id = pending.recovery.recovery_id
    approval = pending.recovery.pending_approval
    assert approval is not None
    live_snapshot = pending.recovery.model_copy(
        update={"execution_mode": ExecutionMode.OPENAI_LIVE}
    )
    store.get_recovery = lambda _recovery_id: live_snapshot  # type: ignore[method-assign]
    orchestrator = _FailingLiveOrchestrator(LiveOperationTimeoutError())
    app = create_app(
        _settings(),
        store=store,
        orchestrator=orchestrator,  # type: ignore[arg-type]
    )
    request = ApprovalDecisionRequest(
        decision="decline",
        clientDecisionId="decision-timeout",
        remedyId=approval.remedy_id,
        remedyDigest=approval.remedy_digest,
        toolCallId=approval.tool_call_id,
    )

    with TestClient(app) as client:
        response = client.post(
            f"/api/recoveries/{recovery_id}/decisions",
            json=request.model_dump(by_alias=True, mode="json"),
            headers={"Cookie": f"backchannel_demo_session={signed_cookie}"},
        )

    assert response.status_code == 504
    assert response.json()["error"] == {
        "code": "live_timeout",
        "message": "Live processing did not finish before the server deadline.",
        "requestId": response.headers["x-request-id"],
        "recoveryId": recovery_id,
        "retryAfterSeconds": None,
        "fallback": None,
    }
    assert app.state.live_gate.active == 0
    assert orchestrator.decide_calls == 1


def test_live_smoke_uses_same_deadline_and_zero_retry_transport(
    monkeypatch,
) -> None:
    client_kwargs: list[dict[str, object]] = []
    orchestrator_kwargs: list[dict[str, Any]] = []

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs: object) -> None:
            client_kwargs.append(kwargs)

        async def close(self) -> None:
            return None

    class FakeOrchestrator:
        def __init__(self, **kwargs: Any) -> None:
            orchestrator_kwargs.append(kwargs)

        async def start(self, *_args: object, **_kwargs: object) -> object:
            raise LiveOperationTimeoutError

    settings = _settings(live_operation_timeout=timedelta(seconds=19))
    monkeypatch.setattr(
        smoke_live.RuntimeSettings,
        "from_environment",
        classmethod(lambda _cls: settings),
    )
    monkeypatch.setattr(smoke_live, "AsyncOpenAI", FakeAsyncOpenAI)
    monkeypatch.setattr(smoke_live, "RecoveryOrchestrator", FakeOrchestrator)
    monkeypatch.setattr(smoke_live, "flush_traces", lambda: None)

    result = asyncio.run(smoke_live.run_live_smoke())

    assert result.smoke == "failed"
    assert result.error_class == "LiveOperationTimeoutError"
    assert result.error_code == "live_timeout"
    assert client_kwargs == [{"timeout": 19.0, "max_retries": 0}]
    assert orchestrator_kwargs[0]["live_operation_timeout"] == timedelta(
        seconds=19
    )


def test_live_timeout_is_declared_on_creation_and_decision_openapi(tmp_path) -> None:
    app = create_app(
        RuntimeSettings(live_ready=False),
        store=SQLiteStore(tmp_path / "live-timeout-openapi.sqlite3"),
    )
    paths = app.openapi()["paths"]

    for response in (
        paths["/api/recoveries"]["post"]["responses"]["504"],
        paths["/api/recoveries/{recovery_id}/decisions"]["post"]["responses"][
            "504"
        ],
    ):
        assert response["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/PublicErrorResponse"
        }
        assert "Retry-After" not in response.get("headers", {})
    for path, method in (
        ("/health", "get"),
        ("/readyz", "get"),
        ("/api/scenarios", "get"),
        ("/api/recoveries/{recovery_id}", "get"),
        ("/api/recoveries/{recovery_id}/events", "get"),
        ("/api/recoveries/{recovery_id}/receipt", "get"),
    ):
        assert "504" not in paths[path][method]["responses"]
