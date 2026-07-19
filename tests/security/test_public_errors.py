from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import NoReturn
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient

from server.config import RuntimeSettings
from server.logging import SafeLogFilter
from server.main import create_app
from server.models import ExecutionMode, ScenarioId
from server.store import SQLiteStore


class ExplodingOrchestrator:
    async def start(self, _scenario_id: str, *, execution_mode: ExecutionMode) -> NoReturn:
        raise RuntimeError(
            "sk-secret Authorization: Bearer private-token prompt=private-prompt "
            "state_json=serialized-state tool_args=private-args tool_results=private-result"
        )


class DecisionExplodingOrchestrator:
    async def approve_decision(self, _recovery_id: str, _payload: object) -> NoReturn:
        raise RuntimeError("sk-decision-secret state_json=private-decision-state")


class LiveStartExplodingOrchestrator:
    def __init__(self) -> None:
        self.recovery_ids: list[str] = []

    async def start(
        self,
        _scenario_id: str,
        *,
        execution_mode: ExecutionMode,
        recovery_id: str | None = None,
    ) -> NoReturn:
        assert execution_mode is ExecutionMode.OPENAI_LIVE
        assert recovery_id is not None
        self.recovery_ids.append(recovery_id)
        raise RuntimeError("sk-live-start-secret prompt=private-live-start")


def test_unhandled_exception_maps_to_generic_correlated_public_error(tmp_path, caplog) -> None:
    caplog.set_level(logging.ERROR)
    client = TestClient(
        create_app(
            RuntimeSettings(live_ready=False),
            store=SQLiteStore(tmp_path / "public-errors.sqlite3"),
            orchestrator=ExplodingOrchestrator(),  # type: ignore[arg-type]
        ),
        raise_server_exceptions=False,
    )

    response = client.post(
        "/api/recoveries",
        json={"scenarioId": "hotel", "executionMode": "sdk_stub"},
        headers={"Authorization": "Bearer request-header-secret"},
        cookies={"backchannel_demo_session": "cookie-secret"},
    )

    assert response.status_code == 500
    assert response.headers["x-request-id"]
    assert response.json() == {
        "code": "internal_error",
        "message": "The request could not be completed.",
        "requestId": response.headers["x-request-id"],
    }
    combined = response.text + caplog.text
    for secret in (
        "sk-secret",
        "private-token",
        "private-prompt",
        "serialized-state",
        "private-args",
        "private-result",
        "request-header-secret",
        "cookie-secret",
    ):
        assert secret not in combined


def test_unhandled_exception_is_sanitized_before_testclient_reraises(tmp_path) -> None:
    client = TestClient(
        create_app(
            RuntimeSettings(live_ready=False),
            store=SQLiteStore(tmp_path / "testclient-reraise.sqlite3"),
            orchestrator=ExplodingOrchestrator(),  # type: ignore[arg-type]
        )
    )

    with pytest.raises(RuntimeError) as raised:
        client.post(
            "/api/recoveries",
            json={"scenarioId": "hotel", "executionMode": "sdk_stub"},
        )

    assert type(raised.value).__name__ == "SanitizedApplicationError"
    assert "sk-secret" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_server_logger_filter_drops_raw_exception_info(tmp_path, caplog) -> None:
    create_app(
        RuntimeSettings(live_ready=False),
        store=SQLiteStore(tmp_path / "server-logger-filter.sqlite3"),
    )
    caplog.set_level(logging.ERROR, logger="uvicorn.error")
    try:
        raise RuntimeError("sk-server-secret state_json=private-server-state")
    except RuntimeError:
        logging.getLogger("uvicorn.error").error(
            "Exception in ASGI application",
            exc_info=sys.exc_info(),
        )

    assert "sk-server-secret" not in caplog.text
    assert "private-server-state" not in caplog.text
    assert "RuntimeError" not in caplog.text


def test_unexpected_decision_error_logs_validated_recovery_correlation_only(
    tmp_path,
    caplog,
) -> None:
    caplog.set_level(logging.ERROR)
    store = SQLiteStore(tmp_path / "decision-correlation.sqlite3")
    recovery_id = "11111111-2222-4333-8444-555555555555"
    UUID(recovery_id)
    store.create_recovery(
        recovery_id=recovery_id,
        scenario_id=ScenarioId.HOTEL,
        execution_mode=ExecutionMode.SDK_STUB,
        current_step=0,
        current_step_summary="Decision correlation fixture.",
        sdk_version="0.18.3",
        protocol_version="backchannel.approval.v1",
        agent_graph_version="backchannel.hotel-agent.v1",
        definition_digest="sdk-definition",
    )
    client = TestClient(
        create_app(
            RuntimeSettings(live_ready=False),
            store=store,
            orchestrator=DecisionExplodingOrchestrator(),  # type: ignore[arg-type]
        ),
        raise_server_exceptions=False,
    )

    response = client.post(
        f"/api/recoveries/{recovery_id}/decisions",
        json={
            "action": "approve",
            "clientDecisionId": "decision-correlation",
            "remedyId": "remedy-correlation",
            "remedyDigest": "sha256:" + "0" * 64,
            "toolCallId": "tool-correlation",
        },
    )

    assert response.status_code == 500
    assert f"recovery_id={recovery_id}" in caplog.text
    assert "sk-decision-secret" not in caplog.text
    assert "private-decision-state" not in caplog.text


def test_unexpected_live_start_error_logs_generated_recovery_correlation(
    tmp_path,
    caplog,
) -> None:
    caplog.set_level(logging.ERROR)
    orchestrator = LiveStartExplodingOrchestrator()
    client = TestClient(
        create_app(
            RuntimeSettings(live_ready=True),
            store=SQLiteStore(tmp_path / "live-start-correlation.sqlite3"),
            orchestrator=orchestrator,  # type: ignore[arg-type]
        ),
        raise_server_exceptions=False,
    )

    response = client.post(
        "/api/recoveries",
        json={"scenarioId": "hotel", "executionMode": "openai_live"},
    )

    assert response.status_code == 500
    assert len(orchestrator.recovery_ids) == 1
    UUID(orchestrator.recovery_ids[0])
    assert f"recovery_id={orchestrator.recovery_ids[0]}" in caplog.text
    assert "recovery_id=none" not in caplog.text
    assert "sk-live-start-secret" not in caplog.text
    assert "private-live-start" not in caplog.text


def test_uvicorn_access_log_never_retains_query_string_secret_markers(
    tmp_path,
    caplog,
) -> None:
    create_app(
        RuntimeSettings(live_ready=False),
        store=SQLiteStore(tmp_path / "access-log-filter.sqlite3"),
    )
    caplog.set_level(logging.INFO, logger="uvicorn.access")
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d',
        "198.51.100.20:5000",
        "GET",
        "/health?prompt=private-query&state_json=private-state&key=sk-access-secret",
        "1.1",
        200,
    )

    assert "private-query" not in caplog.text
    assert "private-state" not in caplog.text
    assert "sk-access-secret" not in caplog.text
    assert "[REDACTED]" in caplog.text


@pytest.mark.parametrize(
    "unsafe_message",
    [
        "sk-live-secret-value",
        "Authorization: Bearer header-secret",
        "prompt=do not retain this",
        "state_json={serialized: true}",
        "tool_args={card: secret}",
        "tool_results={provider: secret}",
        "Cookie: backchannel_demo_session=opaque-secret",
        "OPENAI_API_KEY=opaque-provider-value",
        "access_token=opaque-access-value",
    ],
)
def test_safe_log_filter_redacts_sensitive_payload_markers(unsafe_message: str) -> None:
    record = logging.LogRecord(
        "backchannel.test",
        logging.ERROR,
        __file__,
        1,
        "failure: %s",
        (unsafe_message,),
        None,
    )

    assert SafeLogFilter().filter(record) is True
    rendered = record.getMessage()
    assert unsafe_message not in rendered
    assert "[REDACTED]" in rendered


def test_safe_log_filter_never_retains_exception_info() -> None:
    try:
        raise RuntimeError("arbitrary body value that must not reach a server traceback")
    except RuntimeError:
        record = logging.LogRecord(
            "uvicorn.error",
            logging.ERROR,
            __file__,
            1,
            "Exception in ASGI application",
            (),
            sys.exc_info(),
        )

    assert SafeLogFilter().filter(record) is True
    assert record.exc_info is None
    assert record.exc_text is None


def test_validation_error_never_echoes_oversized_or_extra_payload(tmp_path) -> None:
    client = TestClient(
        create_app(
            RuntimeSettings(live_ready=False),
            store=SQLiteStore(tmp_path / "validation-errors.sqlite3"),
        )
    )
    marker = "private-prompt-marker"

    response = client.post(
        "/api/recoveries",
        json={
            "scenarioId": "hotel",
            "executionMode": "sdk_stub",
            "prompt": marker,
        },
    )

    assert response.status_code == 422
    serialized = json.dumps(response.json())
    assert marker not in serialized
    assert response.json()["code"] == "invalid_request"


@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Type": "application/json"},
        {"Content-Type": "application/json", "Content-Length": "1"},
    ],
)
def test_request_size_limit_counts_actual_bytes_with_missing_or_misleading_length(
    tmp_path,
    headers: dict[str, str],
) -> None:
    settings = RuntimeSettings(live_ready=False, request_body_size_limit_bytes=64)
    app = create_app(
        settings,
        store=SQLiteStore(tmp_path / f"size-{len(headers)}.sqlite3"),
    )
    body = b'{"scenarioId":"hotel","executionMode":"sdk_stub","padding":"' + b"x" * 80 + b'"}'

    response = TestClient(app).post("/api/recoveries", content=body, headers=headers)

    assert response.status_code == 413
    assert response.json()["code"] == "request_too_large"
    assert response.json()["requestId"] == response.headers["x-request-id"]
    assert b"x" * 20 not in response.content


def test_chunked_request_without_content_length_is_bounded_while_streaming(tmp_path) -> None:
    settings = RuntimeSettings(live_ready=False, request_body_size_limit_bytes=64)
    app = create_app(settings, store=SQLiteStore(tmp_path / "chunked-size.sqlite3"))

    async def send_chunks() -> httpx.Response:
        async def content():
            yield b"{" + b"x" * 40
            yield b"y" * 40 + b"}"

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.post(
                "/api/recoveries",
                content=content(),
                headers={"Content-Type": "application/json"},
            )

    response = asyncio.run(send_chunks())

    assert response.status_code == 413
    assert response.json()["code"] == "request_too_large"
