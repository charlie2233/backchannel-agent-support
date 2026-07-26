from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import traceback
from pathlib import Path

import pytest
from agents import ModelSettings
from agents.models.interface import ModelTracing
from openai import AsyncOpenAI
from openai.types.responses import Response

from scripts.smoke_live import _safe_error
from server.agents.live_models import (
    LIVE_MODEL_ALIASES,
    LiveModelRequestError,
    ModelResponseMetadata,
    ResponseMetadataRecorder,
    SafeOpenAIResponsesModel,
    SafeOpenAIResponsesProvider,
)
from server.agents.tracing import configure_openai_live_tracing
from server.async_store import AsyncStoreOverloadedError


class _ScriptedResponses:
    def __init__(self, response: Response) -> None:
        self._response = response
        self.calls = 0

    async def create(self, **_kwargs: object) -> Response:
        self.calls += 1
        return self._response


class _LeakyResponsesError(RuntimeError):
    def __init__(self) -> None:
        super().__init__(
            "failure-message-prompt-canary tool-arguments-canary authorization-canary"
        )
        self.request_id = "req_request_id_canary_sensitive_payload"
        self.code = "invalid_api_key_canary_payload"


class _FailingResponses:
    def __init__(self, error: Exception) -> None:
        self._error = error

    async def create(self, **_kwargs: object) -> Response:
        raise self._error


def _response(*, model: str, response_id: str) -> Response:
    return Response.model_construct(
        id=response_id,
        created_at=0.0,
        model=model,
        object="response",
        output=[],
        parallel_tool_calls=False,
        tool_choice="auto",
        tools=[],
        status="completed",
    )


def test_application_forces_sdk_payload_logging_off_before_agents_import() -> None:
    project_root = Path(__file__).parents[2]
    hostile_environment = {
        **os.environ,
        "OPENAI_AGENTS_DONT_LOG_MODEL_DATA": "false",
        "OPENAI_AGENTS_DONT_LOG_TOOL_DATA": "0",
        "OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA": "true",
        "PYTHONPATH": str(project_root),
    }
    probe = """
import json
import server
from agents import RunConfig
from agents import _debug
print(json.dumps({
    "model": _debug.DONT_LOG_MODEL_DATA,
    "tool": _debug.DONT_LOG_TOOL_DATA,
    "trace": RunConfig().trace_include_sensitive_data,
}))
"""

    completed = subprocess.run(
        [sys.executable, "-c", probe],
        check=True,
        cwd=project_root,
        env=hostile_environment,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "model": True,
        "tool": True,
        "trace": False,
    }


def test_application_suppresses_openai_base_client_debug_request_payloads() -> None:
    project_root = Path(__file__).parents[2]
    hostile_environment = {
        **os.environ,
        "OPENAI_LOG": "debug",
        "PYTHONPATH": str(project_root),
    }
    probe = r"""
import asyncio
import io
import json
import logging
import httpx

stream = io.StringIO()
root = logging.getLogger()
root.setLevel(logging.DEBUG)
root.addHandler(logging.StreamHandler(stream))

import server
from openai import AsyncOpenAI

async def run():
    async def stop(request):
        raise httpx.ConnectError("expected transport stop", request=request)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(stop))
    client = AsyncOpenAI(
        api_key="test-only-not-a-live-key",
        http_client=http_client,
        max_retries=0,
    )
    try:
        await client.responses.create(
            model="gpt-5.6-luna",
            input="base-client-prompt-canary",
            tools=[{
                "type": "function",
                "name": "base_client_tool_canary",
                "description": "base-client-tool-payload-canary",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                "strict": True,
            }],
        )
    except Exception:
        pass
    await client.close()

asyncio.run(run())
logs = stream.getvalue()
print(json.dumps({
    "promptLeaked": "base-client-prompt-canary" in logs,
    "toolLeaked": "base_client_tool_canary" in logs
        or "base-client-tool-payload-canary" in logs,
    "baseDebugEnabled": logging.getLogger("openai._base_client").isEnabledFor(
        logging.DEBUG
    ),
}))
"""

    completed = subprocess.run(
        [sys.executable, "-c", probe],
        check=True,
        cwd=project_root,
        env=hostile_environment,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "promptLeaked": False,
        "toolLeaked": False,
        "baseDebugEnabled": False,
    }


def test_pinned_response_subclass_records_only_safe_returned_identifiers() -> None:
    returned_model = "gpt-5.6-luna-2026-07-15"
    raw_response = _response(model=returned_model, response_id="resp_safe_consumer")
    scripted = _ScriptedResponses(raw_response)
    client = AsyncOpenAI(api_key="test-only-not-a-live-key")
    client.responses = scripted  # type: ignore[assignment]
    recorder = ResponseMetadataRecorder()
    model = SafeOpenAIResponsesModel(
        "gpt-5.6-luna",
        client,
        recorder=recorder,
    )

    response = asyncio.run(
        model._fetch_response(  # noqa: SLF001 - pinned seam under test
            system_instructions=None,
            input="safe test input",
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            stream=False,
        )
    )

    assert response is raw_response
    assert scripted.calls == 1
    assert recorder.snapshot() == (
        ModelResponseMetadata(
            requested_model="gpt-5.6-luna",
            returned_model=returned_model,
            response_id="resp_safe_consumer",
            request_id=None,
        ),
    )
    serialized = json.dumps(recorder.public_summary())
    assert "safe test input" not in serialized
    assert "output" not in serialized.lower()


def test_pinned_response_waits_for_durable_metadata_callback() -> None:
    raw_response = _response(
        model="gpt-5.6-luna-2026-07-15",
        response_id="resp_durable_consumer",
    )
    scripted = _ScriptedResponses(raw_response)
    client = AsyncOpenAI(api_key="test-only-not-a-live-key")
    client.responses = scripted  # type: ignore[assignment]
    callback_entered = asyncio.Event()
    callback_release = asyncio.Event()
    persisted: list[tuple[ModelResponseMetadata, ...]] = []

    async def persist(metadata: tuple[ModelResponseMetadata, ...]) -> None:
        callback_entered.set()
        await callback_release.wait()
        persisted.append(metadata)

    recorder = ResponseMetadataRecorder(on_record=persist)
    model = SafeOpenAIResponsesModel(
        "gpt-5.6-luna",
        client,
        recorder=recorder,
    )

    async def exercise() -> None:
        response_task = asyncio.create_task(
            model._fetch_response(  # noqa: SLF001 - pinned seam under test
                system_instructions=None,
                input="safe test input",
                model_settings=ModelSettings(),
                tools=[],
                output_schema=None,
                handoffs=[],
                stream=False,
            )
        )
        await callback_entered.wait()
        assert not response_task.done()
        assert persisted == []
        callback_release.set()
        assert await response_task is raw_response

    asyncio.run(exercise())
    assert len(persisted) == 1
    assert persisted[0] == recorder.snapshot()


def test_metadata_is_not_published_in_memory_when_durable_callback_fails() -> None:
    expected = RuntimeError("durable-metadata-write-failed")

    async def fail_persistence(
        _metadata: tuple[ModelResponseMetadata, ...],
    ) -> None:
        raise expected

    recorder = ResponseMetadataRecorder(on_record=fail_persistence)
    metadata = ModelResponseMetadata(
        requested_model="gpt-5.6-luna",
        returned_model="gpt-5.6-luna-2026-07-15",
        response_id="resp_failed_persistence",
        request_id="req_failed_persistence",
    )

    async def exercise() -> None:
        with pytest.raises(RuntimeError) as raised:
            await recorder.record(metadata)
        assert raised.value is expected

    asyncio.run(exercise())
    assert recorder.snapshot() == ()


def test_live_model_seam_preserves_retryable_store_error_identity(caplog) -> None:
    raw_response = _response(
        model="gpt-5.6-luna-2026-07-15",
        response_id="resp_retryable_store_failure",
    )
    scripted = _ScriptedResponses(raw_response)
    client = AsyncOpenAI(api_key="test-only-not-a-live-key")
    client.responses = scripted  # type: ignore[assignment]
    expected = AsyncStoreOverloadedError("retryable-store-prompt-canary")

    async def fail_persistence(
        _metadata: tuple[ModelResponseMetadata, ...],
    ) -> None:
        raise expected

    model = SafeOpenAIResponsesModel(
        "gpt-5.6-luna",
        client,
        recorder=ResponseMetadataRecorder(on_record=fail_persistence),
    )

    with caplog.at_level(logging.ERROR):
        with pytest.raises(AsyncStoreOverloadedError) as raised:
            asyncio.run(
                model.get_response(
                    system_instructions=None,
                    input="retryable-store-input-canary",
                    model_settings=ModelSettings(),
                    tools=[],
                    output_schema=None,
                    handoffs=[],
                    tracing=ModelTracing.ENABLED_WITHOUT_DATA,
                )
            )
    asyncio.run(client.close())

    assert raised.value is expected
    assert "retryable-store-prompt-canary" not in caplog.text
    assert "retryable-store-input-canary" not in caplog.text
    assert "LiveModelRequestError" not in caplog.text


def test_pinned_response_failure_logs_only_safe_class_and_code(caplog) -> None:
    failure = _LeakyResponsesError()
    client = AsyncOpenAI(api_key="test-only-not-a-live-key")
    client.responses = _FailingResponses(failure)  # type: ignore[assignment]
    model = SafeOpenAIResponsesModel(
        "gpt-5.6-luna",
        client,
        recorder=ResponseMetadataRecorder(),
    )

    caught: Exception | None = None
    with caplog.at_level(logging.ERROR):
        try:
            asyncio.run(
                model.get_response(
                    system_instructions="failure-system-prompt-canary",
                    input="failure-input-prompt-canary",
                    model_settings=ModelSettings(),
                    tools=[],
                    output_schema=None,
                    handoffs=[],
                    tracing=ModelTracing.ENABLED_WITHOUT_DATA,
                )
            )
        except Exception as error:
            caught = error
            logging.getLogger("test.outer").exception("Outer live model failure")
    asyncio.run(client.close())

    assert caught is not None
    assert type(caught).__name__ == "LiveModelRequestError"
    assert getattr(caught, "code", None) == "external_error"
    logs = caplog.text
    formatted = "".join(traceback.format_exception(caught))
    for canary in (
        "failure-message-prompt-canary",
        "tool-arguments-canary",
        "authorization-canary",
        "req_request_id_canary_sensitive_payload",
        "invalid_api_key_canary_payload",
        "failure-system-prompt-canary",
        "failure-input-prompt-canary",
    ):
        assert canary not in logs
        assert canary not in formatted
    assert "LiveModelRequestError" in logs
    assert "external_error" in logs


def test_smoke_error_output_rejects_shaped_external_class_and_code() -> None:
    shaped_class = type(
        "PromptCanaryError",
        (RuntimeError,),
        {"code": "invalid_api_key_prompt_canary"},
    )

    assert _safe_error(shaped_class("raw-prompt-canary")) == (
        "ExternalError",
        "external_error",
    )


def test_live_model_boundary_preserves_only_an_exact_allowlisted_code(caplog) -> None:
    failure = _LeakyResponsesError()
    failure.code = "invalid_api_key"
    client = AsyncOpenAI(api_key="test-only-not-a-live-key")
    client.responses = _FailingResponses(failure)  # type: ignore[assignment]
    model = SafeOpenAIResponsesModel(
        "gpt-5.6-luna",
        client,
        recorder=ResponseMetadataRecorder(),
    )

    with caplog.at_level(logging.ERROR):
        with pytest.raises(LiveModelRequestError) as raised:
            asyncio.run(
                model.get_response(
                    system_instructions=None,
                    input="allowlisted-code-input-canary",
                    model_settings=ModelSettings(),
                    tools=[],
                    output_schema=None,
                    handoffs=[],
                    tracing=ModelTracing.ENABLED_WITHOUT_DATA,
                )
            )
    asyncio.run(client.close())

    assert raised.value.code == "invalid_api_key"
    assert _safe_error(raised.value) == (
        "LiveModelRequestError",
        "invalid_api_key",
    )
    assert "allowlisted-code-input-canary" not in caplog.text
    assert "failure-message-prompt-canary" not in caplog.text


def test_live_provider_resolves_only_three_explicit_agent_model_aliases() -> None:
    client = AsyncOpenAI(api_key="test-only-not-a-live-key")
    recorder = ResponseMetadataRecorder()
    provider = SafeOpenAIResponsesProvider(client=client, recorder=recorder)

    resolved = [provider.get_model(alias) for alias in LIVE_MODEL_ALIASES]

    assert [model.model for model in resolved] == [
        "gpt-5.6-luna",
        "gpt-5.6-luna",
        "gpt-5.6-terra",
    ]
    assert all(isinstance(model, SafeOpenAIResponsesModel) for model in resolved)
    with pytest.raises(ValueError, match="explicit live model alias"):
        provider.get_model(None)
    with pytest.raises(ValueError, match="explicit live model alias"):
        provider.get_model("gpt-5.6-sol")


def test_live_run_config_never_overrides_agent_models_and_disables_payloads() -> None:
    client = AsyncOpenAI(api_key="test-only-not-a-live-key")
    provider = SafeOpenAIResponsesProvider(
        client=client,
        recorder=ResponseMetadataRecorder(),
    )

    run_config = configure_openai_live_tracing(model_provider=provider)

    assert run_config.model is None
    assert run_config.model_provider is provider
    assert run_config.tracing_disabled is False
    assert run_config.trace_include_sensitive_data is False
