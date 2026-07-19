import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

import pytest
from agents.agent_output import AgentOutputSchemaBase
from agents.exceptions import UserError
from agents.handoffs import Handoff
from agents.items import (
    ModelResponse,
    TResponseInputItem,
    TResponseStreamEvent,
)
from agents.model_settings import ModelSettings
from agents.models.interface import Model, ModelProvider, ModelTracing
from agents.tool import Tool
from agents.tracing import get_trace_provider, set_trace_provider
from agents.tracing.processor_interface import TracingProcessor
from agents.tracing.provider import DefaultTraceProvider
from agents.tracing.spans import Span
from agents.tracing.traces import Trace
from agents.usage import Usage
from fastapi.testclient import TestClient
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)
from openai.types.responses.response_prompt_param import ResponsePromptParam
from pydantic import ValidationError

from server.agents.schemas import (
    ConsumerProof,
    ProviderProof,
    deterministic_hotel_arguments,
)
from server.config import RuntimeSettings
from server.main import create_app
from server.models import ExecutionMode, RecoverySnapshot
from server.orchestrator import RecoveryOrchestrator, UnsupportedOrchestrationError
from server.providers.hotel_simulator import HotelSimulator
from server.store import RecoveryNotFoundError, SQLiteStore

LUNA_MODEL = "gpt-5.6-luna"
TERRA_MODEL = "gpt-5.6-terra"


@dataclass(frozen=True, slots=True)
class RecordedModelCall:
    model_name: str
    output_schema_name: str | None
    tool_names: tuple[str, ...]
    tool_choice: object
    parallel_tool_calls: bool | None
    tracing: ModelTracing


class ScriptedLiveModel(Model):
    def __init__(self, provider: "ScriptedLiveProvider", model_name: str) -> None:
        self._provider = provider
        self._model_name = model_name

    @staticmethod
    def _matching_function_output(
        model_input: str | list[TResponseInputItem],
    ) -> str | None:
        if not isinstance(model_input, list):
            return None
        for item in model_input:
            if isinstance(item, Mapping):
                item_type = item.get("type")
                output = item.get("output")
            else:
                item_type = getattr(item, "type", None)
                output = getattr(item, "output", None)
            if item_type == "function_call_output" and isinstance(output, str):
                return output
        return None

    @staticmethod
    def _message(text: str, response_id: str) -> ModelResponse:
        return ModelResponse(
            output=[
                ResponseOutputMessage(
                    id=f"message-{response_id}",
                    role="assistant",
                    status="completed",
                    type="message",
                    content=[
                        ResponseOutputText(
                            type="output_text",
                            text=text,
                            annotations=[],
                        )
                    ],
                )
            ],
            usage=Usage(),
            response_id=response_id,
        )

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: ResponsePromptParam | None,
    ) -> ModelResponse:
        del (
            system_instructions,
            handoffs,
            previous_response_id,
            conversation_id,
            prompt,
        )
        schema_name = output_schema.name() if output_schema is not None else None
        self._provider.calls.append(
            RecordedModelCall(
                model_name=self._model_name,
                output_schema_name=schema_name,
                tool_names=tuple(tool.name for tool in tools),
                tool_choice=model_settings.tool_choice,
                parallel_tool_calls=model_settings.parallel_tool_calls,
                tracing=tracing,
            )
        )
        response_id = f"fake-live-{len(self._provider.calls)}"
        arguments = deterministic_hotel_arguments()
        if schema_name == ConsumerProof.__name__:
            assert self._model_name == LUNA_MODEL
            assert not tools
            return self._message(arguments.consumer_proof.model_dump_json(), response_id)
        if schema_name == ProviderProof.__name__:
            assert self._model_name == LUNA_MODEL
            assert not tools
            return self._message(arguments.provider_proof.model_dump_json(), response_id)
        assert self._model_name == TERRA_MODEL
        assert [tool.name for tool in tools] == ["commit_remedy"]
        function_output = self._matching_function_output(input)
        if function_output is not None:
            # The SDK deliberately resets a required tool choice after that tool
            # returns so the resumed broker can produce its terminal message.
            assert model_settings.tool_choice is None
            return self._message("Live demo-provider recovery completed.", response_id)
        assert model_settings.tool_choice == "commit_remedy"
        assert model_settings.parallel_tool_calls is False
        return ModelResponse(
            output=[
                ResponseFunctionToolCall(
                    type="function_call",
                    name="commit_remedy",
                    call_id="live-commit-remedy",
                    arguments=arguments.model_dump_json(),
                )
            ],
            usage=Usage(),
            response_id=response_id,
        )

    def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: ResponsePromptParam | None,
    ) -> AsyncIterator[TResponseStreamEvent]:
        del (
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            previous_response_id,
            conversation_id,
            prompt,
        )
        raise NotImplementedError


class ScriptedLiveProvider(ModelProvider):
    def __init__(self) -> None:
        self.calls: list[RecordedModelCall] = []

    def get_model(self, model_name: str | None) -> Model:
        assert model_name in {LUNA_MODEL, TERRA_MODEL}
        return ScriptedLiveModel(self, model_name)


class CaptureProcessor(TracingProcessor):
    def __init__(self) -> None:
        self.started_traces: list[Trace] = []
        self.ended_traces: list[Trace] = []
        self.ended_spans: list[Span[Any]] = []

    def on_trace_start(self, trace: Trace) -> None:
        self.started_traces.append(trace)

    def on_trace_end(self, trace: Trace) -> None:
        self.ended_traces.append(trace)

    def on_span_start(self, span: Span[Any]) -> None:
        del span

    def on_span_end(self, span: Span[Any]) -> None:
        self.ended_spans.append(span)

    def shutdown(self) -> None:
        return None

    def force_flush(self) -> None:
        return None


def snapshot_payload(*, execution_mode: ExecutionMode) -> dict[str, object]:
    return {
        "recoveryId": "11111111-2222-4333-8444-555555555555",
        "scenarioId": "hotel",
        "executionMode": execution_mode,
        "status": "in_progress",
        "currentStep": 0,
        "currentStepSummary": "Recovery started.",
        "createdAt": "2026-07-19T12:00:00Z",
        "updatedAt": "2026-07-19T12:00:00Z",
        "pendingApproval": None,
    }


def test_every_snapshot_requires_mode_bound_model_and_root_trace_provenance() -> None:
    with pytest.raises(ValidationError):
        RecoverySnapshot.model_validate(snapshot_payload(execution_mode=ExecutionMode.SDK_STUB))

    replay = RecoverySnapshot.model_validate(
        {
            **snapshot_payload(execution_mode=ExecutionMode.REPLAY_FIXTURE),
            "modelIds": [],
            "rootTraceId": None,
        }
    )
    assert replay.model_ids == []
    assert replay.root_trace_id is None

    live = RecoverySnapshot.model_validate(
        {
            **snapshot_payload(execution_mode=ExecutionMode.OPENAI_LIVE),
            "modelIds": ["gpt-5.6-luna", "gpt-5.6-terra"],
            "rootTraceId": "trace_0123456789abcdef0123456789abcdef",
        }
    )
    assert live.model_ids == ["gpt-5.6-luna", "gpt-5.6-terra"]
    assert live.root_trace_id == "trace_0123456789abcdef0123456789abcdef"


def test_false_ready_direct_orchestrator_call_rejects_openai_live(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "false-ready.sqlite3")
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(store=store),
        live_ready=False,
    )

    with pytest.raises(UnsupportedOrchestrationError, match="live-ready"):
        asyncio.run(
            orchestrator.start("hotel", execution_mode=ExecutionMode.OPENAI_LIVE)
        )

    assert store.count_recoveries() == 0


def test_no_key_api_gate_uses_injected_live_provider_only_when_ready(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    blocked_models = ScriptedLiveProvider()
    blocked_store = SQLiteStore(tmp_path / "api-false-ready.sqlite3")
    with TestClient(
        create_app(
            RuntimeSettings(live_ready=False),
            store=blocked_store,
            model_provider=blocked_models,
        )
    ) as client:
        blocked = client.post(
            "/api/recoveries",
            json={"scenarioId": "hotel", "executionMode": "openai_live"},
        )
    assert blocked.status_code == 422
    assert blocked_models.calls == []

    ready_models = ScriptedLiveProvider()
    ready_store = SQLiteStore(tmp_path / "api-live-ready.sqlite3")
    with TestClient(
        create_app(
            RuntimeSettings(live_ready=True),
            store=ready_store,
            model_provider=ready_models,
        )
    ) as client:
        response = client.post(
            "/api/recoveries",
            json={"scenarioId": "hotel", "executionMode": "openai_live"},
        )
    assert response.status_code == 201
    payload = response.json()
    assert payload["executionMode"] == "openai_live"
    assert payload["status"] == "pending_approval"
    assert payload["modelIds"] == [LUNA_MODEL, TERRA_MODEL]
    assert [call.model_name for call in ready_models.calls] == [
        LUNA_MODEL,
        LUNA_MODEL,
        TERRA_MODEL,
    ]


def _approval_request(pending: Any) -> dict[str, str]:
    approval = pending.recovery.pending_approval
    assert approval is not None
    return {
        "action": "approve",
        "clientDecisionId": "live-restart-approval",
        "remedyId": approval.remedy_id,
        "remedyDigest": approval.remedy_digest,
        "toolCallId": approval.tool_call_id,
    }


def test_live_graph_uses_one_redacted_root_and_resumes_with_durable_provenance(
    tmp_path,
) -> None:
    previous_trace_provider = get_trace_provider()
    trace_provider = DefaultTraceProvider()
    capture = CaptureProcessor()
    trace_provider.set_processors([capture])
    set_trace_provider(trace_provider)
    database_path = tmp_path / "live-contract.sqlite3"
    try:
        store = SQLiteStore(database_path)
        hotel_provider = HotelSimulator(store=store)
        fake_models = ScriptedLiveProvider()
        orchestrator = RecoveryOrchestrator(
            store=store,
            hotel_provider=hotel_provider,
            live_ready=True,
            model_provider=fake_models,
        )

        pending = asyncio.run(
            orchestrator.start("hotel", execution_mode=ExecutionMode.OPENAI_LIVE)
        )
        snapshot = pending.recovery
        envelope = store.get_pending_approval(snapshot.recovery_id)

        assert snapshot.execution_mode is ExecutionMode.OPENAI_LIVE
        assert snapshot.model_ids == [LUNA_MODEL, TERRA_MODEL]
        assert snapshot.root_trace_id is not None
        assert snapshot.root_trace_id.startswith("trace_")
        assert len(pending.sdk_result.interruptions) == 1
        interruption = pending.sdk_result.interruptions[0]
        assert interruption.tool_name == "commit_remedy"
        assert hotel_provider.dispatch_count == 0
        assert [call.model_name for call in fake_models.calls] == [
            LUNA_MODEL,
            LUNA_MODEL,
            TERRA_MODEL,
        ]
        assert [call.output_schema_name for call in fake_models.calls] == [
            ConsumerProof.__name__,
            ProviderProof.__name__,
            None,
        ]
        assert all(
            call.tracing is ModelTracing.ENABLED_WITHOUT_DATA
            for call in fake_models.calls
        )
        assert envelope.execution_mode is ExecutionMode.OPENAI_LIVE
        assert envelope.model_ids == (LUNA_MODEL, TERRA_MODEL)
        assert envelope.root_trace_id == snapshot.root_trace_id
        assert envelope.sdk_version == "0.18.3"
        assert envelope.protocol_version
        assert envelope.agent_graph_version.endswith(".live.v1")
        assert len(envelope.definition_digest) == 64
        assert len(capture.started_traces) == 1
        trace_export = capture.started_traces[0].export()
        assert trace_export is not None
        assert trace_export["id"] == snapshot.root_trace_id
        assert trace_export["group_id"] == snapshot.recovery_id
        assert trace_export["metadata"] == {
            "agentGraphVersion": envelope.agent_graph_version,
            "executionMode": "openai_live",
            "protocolVersion": envelope.protocol_version,
            "providerBoundary": "demo_adapter_only",
            "scenarioId": "hotel",
        }

        store.close()
        fresh_store = SQLiteStore(database_path)
        fresh_hotel_provider = HotelSimulator(store=fresh_store)
        fresh_models = ScriptedLiveProvider()
        fresh_orchestrator = RecoveryOrchestrator(
            store=fresh_store,
            hotel_provider=fresh_hotel_provider,
            live_ready=True,
            model_provider=fresh_models,
        )
        from server.models import ApprovalDecisionRequest

        completed = asyncio.run(
            fresh_orchestrator.approve_decision(
                snapshot.recovery_id,
                ApprovalDecisionRequest.model_validate(_approval_request(pending)),
            )
        )

        assert completed.status == "completed"
        assert fresh_hotel_provider.dispatch_count == 1
        assert [call.model_name for call in fresh_models.calls] == [TERRA_MODEL]
        assert fresh_models.calls[0].tracing is ModelTracing.ENABLED_WITHOUT_DATA
        sealed_snapshot = fresh_store.get_recovery(snapshot.recovery_id)
        receipt = fresh_store.get_receipt(snapshot.recovery_id)
        assert sealed_snapshot.model_ids == snapshot.model_ids
        assert sealed_snapshot.root_trace_id == snapshot.root_trace_id
        assert receipt.execution_mode is ExecutionMode.OPENAI_LIVE
        assert receipt.model_ids == snapshot.model_ids
        assert receipt.root_trace_id == snapshot.root_trace_id
        assert receipt.model_call is True
        assert receipt.sdk_version == envelope.sdk_version
        assert receipt.protocol_version == envelope.protocol_version
        assert receipt.agent_graph_version == envelope.agent_graph_version
        assert receipt.definition_digest == envelope.definition_digest
        assert "demo hotel adapter" in receipt.boundary.lower()
        assert len(capture.started_traces) == 1
        assert {span.trace_id for span in capture.ended_spans} == {
            snapshot.root_trace_id
        }
        for span in capture.ended_spans:
            span_data = span.span_data
            if hasattr(span_data, "input"):
                assert span_data.input in (None, "")
            if hasattr(span_data, "output"):
                assert span_data.output in (None, "")
    finally:
        set_trace_provider(previous_trace_provider)


def test_live_crash_reconciliation_preserves_live_receipt_provenance(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "live-crash-reconcile.sqlite3"
    store = SQLiteStore(database_path)
    provider = HotelSimulator(store=store)
    models = ScriptedLiveProvider()
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=provider,
        live_ready=True,
        model_provider=models,
    )
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.OPENAI_LIVE)
    )
    recovery_id = pending.recovery.recovery_id
    original_finalize = store.finalize_completed_execution

    class SimulatedProcessCrash(RuntimeError):
        pass

    def crash_after_execution(*args: Any, **kwargs: Any) -> bool:
        if store.count_executions(recovery_id) == 1:
            raise SimulatedProcessCrash("crash after live provider commit")
        return original_finalize(*args, **kwargs)

    monkeypatch.setattr(store, "finalize_completed_execution", crash_after_execution)
    from server.models import ApprovalDecisionRequest

    with pytest.raises(UserError, match="crash after live provider commit"):
        asyncio.run(
            orchestrator.approve_decision(
                recovery_id,
                ApprovalDecisionRequest.model_validate(_approval_request(pending)),
            )
        )
    assert provider.dispatch_count == 1
    with pytest.raises(RecoveryNotFoundError):
        store.get_receipt(recovery_id)
    store.close()

    fresh_store = SQLiteStore(database_path)
    fresh_provider = HotelSimulator(store=fresh_store)
    fresh_models = ScriptedLiveProvider()
    RecoveryOrchestrator(
        store=fresh_store,
        hotel_provider=fresh_provider,
        live_ready=True,
        model_provider=fresh_models,
    )

    receipt = fresh_store.get_receipt(recovery_id)
    assert fresh_provider.dispatch_count == 0
    assert fresh_models.calls == []
    assert receipt.execution_mode is ExecutionMode.OPENAI_LIVE
    assert receipt.model_call is True
    assert receipt.model_ids == [LUNA_MODEL, TERRA_MODEL]
    assert receipt.root_trace_id == pending.recovery.root_trace_id
