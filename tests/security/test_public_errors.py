from __future__ import annotations

import asyncio
import json
import logging
from typing import NoReturn

import httpx
import pytest
from fastapi.testclient import TestClient

from server.config import RuntimeSettings
from server.logging import SafeLogFilter
from server.main import create_app
from server.models import ExecutionMode
from server.store import SQLiteStore


class ExplodingOrchestrator:
    async def start(self, _scenario_id: str, *, execution_mode: ExecutionMode) -> NoReturn:
        raise RuntimeError(
            "sk-secret Authorization: Bearer private-token prompt=private-prompt "
            "state_json=serialized-state tool_args=private-args tool_results=private-result"
        )


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
