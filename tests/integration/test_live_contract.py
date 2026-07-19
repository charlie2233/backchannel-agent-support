from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from agents import Agent, Runner, RunState, trace
from agents.agent_output import AgentOutputSchemaBase
from agents.handoffs import Handoff
from agents.items import ModelResponse, TResponseInputItem, TResponseStreamEvent
from agents.model_settings import ModelSettings
from agents.models.interface import Model, ModelProvider, ModelTracing
from agents.tool import Tool
from agents.tracing import Span, Trace, TracingProcessor
from agents.tracing import setup as tracing_setup
from agents.tracing.provider import DefaultTraceProvider
from agents.usage import Usage
from openai import AsyncOpenAI
from openai.types.responses import (
    Response,
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)
from openai.types.responses.response_prompt_param import ResponsePromptParam

from server.agents.live_models import (
    ModelResponseMetadata,
    ResponseMetadataRecorder,
    SafeOpenAIResponsesProvider,
)
from server.agents.schemas import (
    BrokerOutcome,
    ConsumerProof,
    ProviderProof,
    deterministic_hotel_arguments,
)
from server.agents.stub_model import DECLINE_MESSAGE
from server.agents.tracing import configure_openai_live_tracing
from server.models import ApprovalDecisionRequest, ExecutionMode, RecoveryStatus
from server.orchestrator import LiveUnavailableError, RecoveryOrchestrator
from server.providers.hotel_simulator import HotelSimulator
from server.store import ApprovalDecisionError, SQLiteStore

RETURNED_MODELS = (
    "gpt-5.6-luna-2026-07-15-consumer",
    "gpt-5.6-luna-2026-07-15-provider",
    "gpt-5.6-terra-2026-07-15-broker",
)


class _ScriptedLiveModel(Model):
    def __init__(
        self,
        owner: ScriptedLiveModelProvider,
        requested_model: str,
        recorder: ResponseMetadataRecorder,
    ) -> None:
        self._owner = owner
        self._requested_model = requested_model
        self._recorder = recorder

    @staticmethod
    def _function_output(model_input: str | list[TResponseInputItem]) -> object:
        if not isinstance(model_input, list):
            return None
        for item in model_input:
            if isinstance(item, Mapping):
                item_type = item.get("type")
                output = item.get("output")
            else:
                item_type = getattr(item, "type", None)
                output = getattr(item, "output", None)
            if item_type == "function_call_output":
                return output
        return None

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
            model_settings,
            handoffs,
            tracing,
            previous_response_id,
            conversation_id,
            prompt,
        )
        self._owner.call_count += 1
        if self._owner.before_call is not None:
            self._owner.before_call(self._owner.call_count)
        if self._owner.resume_entered is not None and self._owner.call_count >= 4:
            self._owner.resume_entered.set()
            assert self._owner.resume_release is not None
            await self._owner.resume_release.wait()
        if self._owner.fail_on_call == self._owner.call_count:
            raise RuntimeError("scripted live transport failure")
        self._owner.requested_models.append(self._requested_model)
        output_name = (
            getattr(getattr(output_schema, "output_type", None), "__name__", "None")
            if output_schema is not None
            else "None"
        )
        self._owner.strict_output_types.append(output_name)
        returned_model = self._owner.returned_model_for_call(self._owner.call_count)
        self._recorder.record(
            ModelResponseMetadata(
                requested_model=self._requested_model,
                returned_model=returned_model,
                response_id=f"resp_{self._owner.call_count}",
                request_id=f"req_{self._owner.call_count}",
            )
        )
        arguments = deterministic_hotel_arguments()
        if output_name == "ConsumerProof":
            text = (self._owner.consumer_proof or arguments.consumer_proof).model_dump_json()
        elif output_name == "ProviderProof":
            text = (self._owner.provider_proof or arguments.provider_proof).model_dump_json()
        elif output_name == "BrokerOutcome":
            function_output = self._function_output(input)
            if function_output is None:
                assert [tool.name for tool in tools] == ["commit_remedy"]
                return ModelResponse(
                    output=[
                        ResponseFunctionToolCall(
                            type="function_call",
                            name="commit_remedy",
                            call_id="live-commit-tool",
                            arguments=arguments.model_dump_json(),
                        )
                    ],
                    usage=Usage(),
                    response_id=f"resp_{self._owner.call_count}",
                )
            status = (
                "closed_without_action"
                if function_output == DECLINE_MESSAGE
                else "completed"
            )
            text = BrokerOutcome(
                status=status,
                summary="Scripted strict broker outcome.",
            ).model_dump_json()
        else:
            raise AssertionError(f"Unexpected strict output: {output_name}")
        return ModelResponse(
            output=[
                ResponseOutputMessage(
                    id=f"msg_{self._owner.call_count}",
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
            response_id=f"resp_{self._owner.call_count}",
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


class ScriptedLiveModelProvider(ModelProvider):
    def __init__(
        self,
        *,
        returned_models: tuple[str, ...],
        resume_only: bool = False,
        fail_on_call: int | None = None,
        consumer_proof: ConsumerProof | None = None,
        provider_proof: ProviderProof | None = None,
        before_call: Callable[[int], None] | None = None,
        resume_entered: asyncio.Event | None = None,
        resume_release: asyncio.Event | None = None,
    ) -> None:
        self.returned_models = returned_models
        self.resume_only = resume_only
        self.fail_on_call = fail_on_call
        self.consumer_proof = consumer_proof
        self.provider_proof = provider_proof
        self.before_call = before_call
        self.resume_entered = resume_entered
        self.resume_release = resume_release
        self.call_count = 0
        self.requested_models: list[str] = []
        self.strict_output_types: list[str] = []
        self.recorder = ResponseMetadataRecorder()

    def bind(self, recorder: ResponseMetadataRecorder) -> ScriptedLiveModelProvider:
        self.recorder = recorder
        return self

    def returned_model_for_call(self, call_count: int) -> str:
        index = min(call_count - 1, len(self.returned_models) - 1)
        return self.returned_models[index]

    def get_model(self, model_name: str | None) -> Model:
        if not isinstance(model_name, str):
            raise ValueError("Scripted model needs an explicit alias")
        return _ScriptedLiveModel(self, model_name, self.recorder)


class _SingleResponsesTransport:
    def __init__(self, response: Response) -> None:
        self._response = response

    async def create(self, **_kwargs: object) -> Response:
        return self._response


@dataclass
class _TraceObservation:
    trace_id: str
    group_id: str
    sensitive_payloads: list[object]


class _TraceRecorder:
    def __init__(self) -> None:
        self.roots: list[_TraceObservation] = []

    @contextmanager
    def root(self, *, trace_id: str, group_id: str) -> Iterator[None]:
        self.roots.append(
            _TraceObservation(
                trace_id=trace_id,
                group_id=group_id,
                sensitive_payloads=[],
            )
        )
        yield


class _EndedSpanCapture(TracingProcessor):
    def __init__(self) -> None:
        self.traces: list[Trace] = []
        self.spans: list[Span[Any]] = []

    def on_trace_start(self, trace: Trace) -> None:
        self.traces.append(trace)

    def on_trace_end(self, _trace: Trace) -> None:
        return None

    def on_span_start(self, _span: Span[Any]) -> None:
        return None

    def on_span_end(self, span: Span[Any]) -> None:
        self.spans.append(span)

    def shutdown(self) -> None:
        return None

    def force_flush(self) -> None:
        return None


def _decision(pending, action: str, client_id: str) -> ApprovalDecisionRequest:
    approval = pending.recovery.pending_approval
    assert approval is not None
    return ApprovalDecisionRequest(
        decision=action,
        clientDecisionId=client_id,
        remedyId=approval.remedy_id,
        remedyDigest=approval.remedy_digest,
        toolCallId=approval.tool_call_id,
    )


def test_live_key_gate_fails_before_recovery_model_or_provider_activity(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "no-live-key.sqlite3")
    hotel = HotelSimulator(store=store)
    model_provider = ScriptedLiveModelProvider(returned_models=RETURNED_MODELS)
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=hotel,
        live_ready=False,
        live_model_provider_factory=model_provider.bind,
    )

    with pytest.raises(LiveUnavailableError, match="live_unavailable"):
        asyncio.run(
            orchestrator.start("hotel", execution_mode=ExecutionMode.OPENAI_LIVE)
        )

    assert store.count_recoveries() == 0
    assert model_provider.call_count == 0
    assert hotel.dispatch_count == 0


def test_live_model_failure_before_pending_boundary_removes_only_its_orphan(
    tmp_path,
) -> None:
    store = SQLiteStore(tmp_path / "live-prepending-failure.sqlite3")
    hotel = HotelSimulator(store=store)
    model_provider = ScriptedLiveModelProvider(
        returned_models=RETURNED_MODELS,
        fail_on_call=2,
    )
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=hotel,
        live_ready=True,
        live_model_provider_factory=model_provider.bind,
    )

    with pytest.raises(RuntimeError, match="scripted live transport failure"):
        asyncio.run(
            orchestrator.start("hotel", execution_mode=ExecutionMode.OPENAI_LIVE)
        )

    assert model_provider.call_count == 2
    assert store.count_recoveries() == 0
    assert hotel.dispatch_count == 0


def test_live_start_has_no_durable_row_before_all_remote_preapproval_calls(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "live-preapproval-no-row.sqlite3")
    observed_counts: list[int] = []
    model_provider = ScriptedLiveModelProvider(
        returned_models=RETURNED_MODELS,
        before_call=lambda _call: observed_counts.append(store.count_recoveries()),
        fail_on_call=3,
    )
    monkeypatch.setattr(
        store,
        "delete_unstarted_recovery",
        lambda _recovery_id: (_ for _ in ()).throw(
            AssertionError("live start must not need orphan cleanup")
        ),
    )
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(store=store),
        live_ready=True,
        live_model_provider_factory=model_provider.bind,
    )

    with pytest.raises(RuntimeError, match="scripted live transport failure"):
        asyncio.run(
            orchestrator.start("hotel", execution_mode=ExecutionMode.OPENAI_LIVE)
        )

    assert observed_counts == [0, 0, 0]
    assert store.count_recoveries() == 0


def test_live_pending_boundary_rolls_back_every_row_on_insert_failure(tmp_path) -> None:
    database_path = tmp_path / "live-pending-atomic.sqlite3"
    store = SQLiteStore(database_path)
    model_provider = ScriptedLiveModelProvider(returned_models=RETURNED_MODELS)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_live_pending
            BEFORE INSERT ON pending_approvals
            BEGIN
                SELECT RAISE(ABORT, 'pending write blocked');
            END
            """
        )
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(store=store),
        live_ready=True,
        live_model_provider_factory=model_provider.bind,
    )

    with pytest.raises(sqlite3.IntegrityError, match="pending write blocked"):
        asyncio.run(
            orchestrator.start("hotel", execution_mode=ExecutionMode.OPENAI_LIVE)
        )

    assert model_provider.call_count == 3
    with sqlite3.connect(database_path) as connection:
        for table in (
            "recoveries",
            "events",
            "remedies",
            "pending_approvals",
            "permission_scopes",
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize(
    ("consumer_proof", "provider_proof", "expected_model_calls"),
    [
        (
            deterministic_hotel_arguments().consumer_proof.model_copy(
                update={"paid_total_minor": 42001}
            ),
            None,
            1,
        ),
        (
            None,
            deterministic_hotel_arguments().provider_proof.model_copy(
                update={"confirmed_room_type": "queen"}
            ),
            2,
        ),
    ],
)
def test_live_rejects_schema_valid_source_drift_before_broker_or_consent(
    tmp_path,
    consumer_proof: ConsumerProof | None,
    provider_proof: ProviderProof | None,
    expected_model_calls: int,
) -> None:
    store = SQLiteStore(tmp_path / f"live-source-drift-{expected_model_calls}.sqlite3")
    hotel = HotelSimulator(store=store)
    model_provider = ScriptedLiveModelProvider(
        returned_models=RETURNED_MODELS,
        consumer_proof=consumer_proof,
        provider_proof=provider_proof,
    )
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=hotel,
        live_ready=True,
        live_model_provider_factory=model_provider.bind,
    )

    with pytest.raises(RuntimeError, match="fixed source evidence"):
        asyncio.run(
            orchestrator.start("hotel", execution_mode=ExecutionMode.OPENAI_LIVE)
        )

    assert model_provider.call_count == expected_model_calls
    assert store.count_recoveries() == 0
    assert hotel.dispatch_count == 0


def test_live_hotel_uses_three_strict_agents_one_root_and_exact_approval(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "live-contract.sqlite3")
    hotel = HotelSimulator(store=store)
    trace_recorder = _TraceRecorder()
    model_provider = ScriptedLiveModelProvider(returned_models=RETURNED_MODELS)
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=hotel,
        live_ready=True,
        live_model_provider_factory=model_provider.bind,
        live_trace_factory=trace_recorder.root,
    )

    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.OPENAI_LIVE)
    )
    recovery_id = pending.recovery.recovery_id

    assert pending.recovery.status is RecoveryStatus.PENDING_APPROVAL
    assert hotel.dispatch_count == 0
    assert model_provider.requested_models[:3] == [
        "gpt-5.6-luna",
        "gpt-5.6-luna",
        "gpt-5.6-terra",
    ]
    assert model_provider.strict_output_types[:3] == [
        "ConsumerProof",
        "ProviderProof",
        "BrokerOutcome",
    ]
    envelope = store.get_pending_approval(recovery_id)
    assert envelope.execution_mode is ExecutionMode.OPENAI_LIVE
    assert envelope.model_metadata == tuple(
        ModelResponseMetadata(
            requested_model=requested,
            returned_model=returned,
            response_id=f"resp_{index}",
            request_id=f"req_{index}",
        )
        for index, (requested, returned) in enumerate(
            zip(
                ("gpt-5.6-luna", "gpt-5.6-luna", "gpt-5.6-terra"),
                RETURNED_MODELS,
                strict=True,
            ),
            start=1,
        )
    )
    assert envelope.root_trace_id.startswith("trace_")
    assert len(trace_recorder.roots) == 1
    assert trace_recorder.roots[0].trace_id == envelope.root_trace_id
    assert trace_recorder.roots[0].group_id == recovery_id
    assert trace_recorder.roots[0].sensitive_payloads == []

    response = asyncio.run(
        orchestrator.decide(
            recovery_id,
            _decision(pending, "approve", "live-approve"),
        )
    )

    assert response.status == "completed"
    assert hotel.dispatch_count == 1
    receipt = store.get_receipt(recovery_id)
    assert receipt.execution_mode is ExecutionMode.OPENAI_LIVE
    assert receipt.simulated is True
    assert receipt.model_ids == list(RETURNED_MODELS)
    assert receipt.root_trace_id == envelope.root_trace_id
    assert receipt.sdk_version == "0.18.3"
    assert receipt.protocol_version
    assert receipt.agent_graph_version
    assert len(receipt.prompt_tool_schema_hash) == 64
    assert receipt.approved_remedy_digest == _decision(
        pending, "approve", "ignored"
    ).remedy_digest
    assert receipt.permission_revoked is True
    assert len(trace_recorder.roots) == 2
    assert {root.trace_id for root in trace_recorder.roots} == {
        envelope.root_trace_id
    }
    assert {root.group_id for root in trace_recorder.roots} == {recovery_id}


def test_concurrent_identical_live_approvals_resume_and_dispatch_once(
    tmp_path,
    monkeypatch,
) -> None:
    async def exercise() -> tuple[
        object,
        object,
        ScriptedLiveModelProvider,
        HotelSimulator,
        HotelSimulator,
    ]:
        restore_entered = asyncio.Event()
        restore_release = asyncio.Event()
        restore_count = 0
        original_from_json = RunState.from_json

        async def held_from_json(*args, **kwargs):
            nonlocal restore_count
            restored = await original_from_json(*args, **kwargs)
            restore_count += 1
            restore_entered.set()
            await restore_release.wait()
            return restored

        monkeypatch.setattr(RunState, "from_json", staticmethod(held_from_json))
        database_path = tmp_path / "live-duplicate-resume.sqlite3"
        first_store = SQLiteStore(database_path)
        first_hotel = HotelSimulator(store=first_store)
        model_provider = ScriptedLiveModelProvider(
            returned_models=RETURNED_MODELS,
        )
        first_orchestrator = RecoveryOrchestrator(
            store=first_store,
            hotel_provider=first_hotel,
            live_ready=True,
            live_model_provider_factory=model_provider.bind,
            decision_lease_duration=timedelta(milliseconds=90),
            decision_wait_interval=0.01,
        )
        pending = await first_orchestrator.start(
            "hotel",
            execution_mode=ExecutionMode.OPENAI_LIVE,
        )
        second_store = SQLiteStore(database_path)
        second_hotel = HotelSimulator(store=second_store)
        second_orchestrator = RecoveryOrchestrator(
            store=second_store,
            hotel_provider=second_hotel,
            live_ready=True,
            live_model_provider_factory=model_provider.bind,
            decision_lease_duration=timedelta(milliseconds=90),
            decision_wait_interval=0.01,
        )
        request = _decision(pending, "approve", "live-duplicate-approval")
        first = asyncio.create_task(
            first_orchestrator.approve_decision(pending.recovery.recovery_id, request)
        )
        await asyncio.wait_for(restore_entered.wait(), timeout=2)
        second = asyncio.create_task(
            second_orchestrator.approve_decision(pending.recovery.recovery_id, request)
        )
        await asyncio.sleep(0.3)
        restores_before_release = restore_count
        restore_release.set()
        first_response, second_response = await asyncio.gather(first, second)
        assert restores_before_release == 1
        return (
            first_response,
            second_response,
            model_provider,
            first_hotel,
            second_hotel,
        )

    first_response, second_response, model_provider, first_hotel, second_hotel = (
        asyncio.run(exercise())
    )

    assert first_response == second_response
    assert model_provider.call_count == 4
    assert first_hotel.dispatch_count + second_hotel.dispatch_count == 1


def test_live_model_metadata_is_owner_scoped_append_only_and_nonterminal(
    tmp_path,
) -> None:
    database_path = tmp_path / "live-metadata-append-only.sqlite3"
    store = SQLiteStore(database_path)
    model_provider = ScriptedLiveModelProvider(returned_models=RETURNED_MODELS)
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(store=store),
        live_ready=True,
        live_model_provider_factory=model_provider.bind,
    )
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.OPENAI_LIVE)
    )
    recovery_id = pending.recovery.recovery_id
    claim = store.claim_decision(
        recovery_id,
        _decision(pending, "approve", "metadata-owner"),
    )
    lease = store.acquire_decision_resume(
        claim,
        resume_owner_id="metadata-owner",
        lease_duration=timedelta(minutes=1),
    )
    initial = store.get_pending_approval(recovery_id).model_metadata
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT model_metadata_revision FROM pending_approvals "
            "WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone() == (3,)
    fourth = ModelResponseMetadata(
        requested_model="gpt-5.6-terra",
        returned_model="gpt-5.6-terra-2026-07-15-resume",
        response_id="resp_4",
        request_id="req_4",
    )
    with pytest.raises(ApprovalDecisionError, match="resume_owner_lost"):
        store.update_pending_model_metadata(
            recovery_id,
            initial + (fourth,),
            resume_owner_id="not-the-owner",
            resume_generation=lease.resume_generation,
        )
    store.update_pending_model_metadata(
        recovery_id,
        initial + (fourth,),
        resume_owner_id="metadata-owner",
        resume_generation=lease.resume_generation,
    )
    assert store.get_pending_approval(recovery_id).model_metadata == initial + (fourth,)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT model_metadata_revision FROM pending_approvals "
            "WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone() == (4,)

    replacement = fourth.__class__(
        requested_model=fourth.requested_model,
        returned_model=fourth.returned_model,
        response_id="resp_replaced",
        request_id=fourth.request_id,
    )
    fifth = fourth.__class__(
        requested_model=fourth.requested_model,
        returned_model=fourth.returned_model,
        response_id="resp_5",
        request_id="req_5",
    )
    with pytest.raises(ApprovalDecisionError, match="model_metadata_conflict"):
        store.update_pending_model_metadata(
            recovery_id,
            initial + (replacement, fifth),
            resume_owner_id="metadata-owner",
            resume_generation=lease.resume_generation,
        )
    with pytest.raises(ApprovalDecisionError, match="model_metadata_conflict"):
        store.update_pending_model_metadata(
            recovery_id,
            (initial[1], initial[0], initial[2], fourth, fifth),
            resume_owner_id="metadata-owner",
            resume_generation=lease.resume_generation,
        )

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE recoveries SET status = 'completed' WHERE id = ?",
            (recovery_id,),
        )
        connection.execute(
            "UPDATE pending_approvals SET status = 'completed' WHERE recovery_id = ?",
            (recovery_id,),
        )
    with pytest.raises(ApprovalDecisionError, match="decision_unavailable"):
        store.update_pending_model_metadata(
            recovery_id,
            initial + (fourth, fifth),
            resume_owner_id="metadata-owner",
            resume_generation=lease.resume_generation,
        )


def test_actual_live_trace_spans_omit_model_tool_and_state_payloads(
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    capture = _EndedSpanCapture()
    trace_provider = DefaultTraceProvider()
    trace_provider.register_processor(capture)
    monkeypatch.setattr(tracing_setup, "GLOBAL_TRACE_PROVIDER", trace_provider)
    store = SQLiteStore(tmp_path / "live-real-trace-privacy.sqlite3")
    hotel = HotelSimulator(store=store)
    scripted = ScriptedLiveModelProvider(returned_models=RETURNED_MODELS)
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=hotel,
        live_ready=True,
        live_model_provider_factory=scripted.bind,
    )

    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.OPENAI_LIVE)
    )
    asyncio.run(
        orchestrator.decide(
            pending.recovery.recovery_id,
            _decision(pending, "approve", "live-span-privacy"),
        )
    )
    root_trace_id = store.get_receipt(pending.recovery.recovery_id).root_trace_id
    assert root_trace_id is not None
    consumer_json = deterministic_hotel_arguments().consumer_proof.model_dump_json()
    raw_response = Response.model_construct(
        id="resp_privacy_probe",
        created_at=0.0,
        model="gpt-5.6-luna-2026-07-15-privacy",
        object="response",
        output=[
            ResponseOutputMessage(
                id="msg_privacy_probe",
                role="assistant",
                status="completed",
                type="message",
                content=[
                    ResponseOutputText(
                        type="output_text",
                        text=consumer_json,
                        annotations=[],
                    )
                ],
            )
        ],
        parallel_tool_calls=False,
        tool_choice="auto",
        tools=[],
        status="completed",
    )
    client = AsyncOpenAI(api_key="test-only-not-a-live-key")
    client.responses = _SingleResponsesTransport(raw_response)  # type: ignore[assignment]
    safe_provider = SafeOpenAIResponsesProvider(
        client=client,
        recorder=ResponseMetadataRecorder(),
    )
    privacy_agent = Agent[None](
        name="Privacy generation probe",
        instructions="Return strict ConsumerProof JSON.",
        model="gpt-5.6-luna",
        output_type=ConsumerProof,
    )
    with trace(
        "Backchannel privacy probe",
        trace_id=root_trace_id,
        group_id=pending.recovery.recovery_id,
    ):
        asyncio.run(
            Runner.run(
                privacy_agent,
                "booking-demo-001 generation-input-canary",
                run_config=configure_openai_live_tracing(
                    model_provider=safe_provider
                ),
            )
        )

    response_spans = [
        span for span in capture.spans if span.span_data.type == "response"
    ]
    function_spans = [
        span for span in capture.spans if span.span_data.type == "function"
    ]
    assert function_spans, [span.span_data.type for span in capture.spans]
    assert response_spans, sorted({span.span_data.type for span in capture.spans})
    for span in response_spans:
        assert getattr(span.span_data, "input", None) is None
        assert getattr(span.span_data, "response", None) is None
    for span in function_spans:
        assert getattr(span.span_data, "input", None) is None
        assert getattr(span.span_data, "output", None) is None
    exported = json.dumps(
        [span.export() for span in capture.spans],
        default=str,
        sort_keys=True,
    )
    logs = caplog.text
    for canary in (
        "booking-demo-001",
        '"action":"replace_room"',
        DECLINE_MESSAGE,
        "OPENAI_API_KEY",
        "state_json",
        "serialized_state",
    ):
        assert canary not in exported
        assert canary not in logs
    assert {span.trace_id for span in capture.spans} == {root_trace_id}
    trace_provider.shutdown()


@pytest.mark.parametrize("decision", ["approve", "decline"])
def test_live_pending_state_resumes_under_same_persisted_root_after_restart(
    tmp_path,
    decision: str,
) -> None:
    database_path = tmp_path / f"live-restart-{decision}.sqlite3"
    initial_store = SQLiteStore(database_path)
    initial_provider = ScriptedLiveModelProvider(returned_models=RETURNED_MODELS)
    initial_trace = _TraceRecorder()
    initial = RecoveryOrchestrator(
        store=initial_store,
        hotel_provider=HotelSimulator(store=initial_store),
        live_ready=True,
        live_model_provider_factory=initial_provider.bind,
        live_trace_factory=initial_trace.root,
    )
    pending = asyncio.run(
        initial.start("hotel", execution_mode=ExecutionMode.OPENAI_LIVE)
    )
    recovery_id = pending.recovery.recovery_id
    request = _decision(pending, decision, f"live-restart-{decision}")
    root_trace_id = initial_store.get_pending_approval(recovery_id).root_trace_id
    initial_store.close()

    restarted_store = SQLiteStore(database_path)
    restarted_hotel = HotelSimulator(store=restarted_store)
    restarted_provider = ScriptedLiveModelProvider(
        returned_models=(RETURNED_MODELS[2],),
        resume_only=True,
    )
    restarted_trace = _TraceRecorder()
    restarted = RecoveryOrchestrator(
        store=restarted_store,
        hotel_provider=restarted_hotel,
        live_ready=True,
        live_model_provider_factory=restarted_provider.bind,
        live_trace_factory=restarted_trace.root,
    )

    response = asyncio.run(restarted.decide(recovery_id, request))

    assert restarted_trace.roots[0].trace_id == root_trace_id
    assert restarted_trace.roots[0].group_id == recovery_id
    receipt = restarted_store.get_receipt(recovery_id)
    assert receipt.root_trace_id == root_trace_id
    assert receipt.model_ids == list(RETURNED_MODELS)
    if decision == "approve":
        assert response.status == "completed"
        assert restarted_hotel.dispatch_count == 1
        assert receipt.execution_count == 1
    else:
        assert response.status == "closed_without_action"
        assert restarted_hotel.dispatch_count == 0
        assert receipt.execution_count == 0
        assert receipt.exact_interruption_rejected is True


def test_openai_live_rejects_non_hotel_scenario_before_model_call(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "live-hotel-only.sqlite3")
    provider = ScriptedLiveModelProvider(returned_models=RETURNED_MODELS)
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(store=store),
        live_ready=True,
        live_model_provider_factory=provider.bind,
    )

    with pytest.raises(ValueError, match="hotel"):
        asyncio.run(
            orchestrator.start("api-quota", execution_mode=ExecutionMode.OPENAI_LIVE)
        )

    assert store.count_recoveries() == 0
    assert provider.call_count == 0
