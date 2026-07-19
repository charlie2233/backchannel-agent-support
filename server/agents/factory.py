"""Build the shared hotel Agent graph and approval-gated commit tool."""

from __future__ import annotations

from dataclasses import dataclass

from agents import Agent, function_tool
from agents.tool_context import ToolContext

from server.agents.schemas import (
    BrokerRemedy,
    CommitRemedyArguments,
    ConsumerProof,
    ProviderProof,
)
from server.agents.stub_model import DeterministicApprovalModel
from server.agents.versioning import (
    HOTEL_AGENT_INSTRUCTIONS,
    HOTEL_AGENT_NAME,
    broker_remedy_action_digest,
)
from server.models import ExecutionMode, RecoveryReceipt, RecoveryStatus
from server.providers.hotel_simulator import HotelDispatchRequest, HotelSimulator
from server.store import SQLiteStore


@dataclass(frozen=True, slots=True)
class HotelAgentContext:
    recovery_id: str
    store: SQLiteStore
    hotel_provider: HotelSimulator
    remedy_digest: str | None = None


def build_hotel_agent(
    *,
    context: HotelAgentContext,
    arguments: CommitRemedyArguments,
) -> Agent[HotelAgentContext]:
    """Build the root Agent whose commit tool is guarded by an SDK interruption."""

    @function_tool(needs_approval=True, failure_error_function=None)
    def commit_remedy(
        tool_context: ToolContext[HotelAgentContext],
        consumer_proof: ConsumerProof,
        provider_proof: ProviderProof,
        remedy: BrokerRemedy,
    ) -> str:
        """Commit the typed hotel remedy through the idempotent demo provider."""

        if consumer_proof.booking_id != provider_proof.booking_id:
            raise ValueError("Consumer and provider proofs identify different bookings")

        remedy_digest = (
            tool_context.context.remedy_digest or broker_remedy_action_digest(remedy)
        )
        idempotency_key = f"{tool_context.context.recovery_id}:{tool_context.tool_call_id}"
        idempotency_key = f"{idempotency_key}:{remedy_digest}"
        dispatch = tool_context.context.hotel_provider.dispatch(
            HotelDispatchRequest(
                recovery_id=tool_context.context.recovery_id,
                remedy=remedy,
            ),
            idempotency_key=idempotency_key,
            tool_call_id=tool_context.tool_call_id,
            remedy_digest=remedy_digest,
        )
        receipt = RecoveryReceipt(
            recoveryId=tool_context.context.recovery_id,
            executionMode=ExecutionMode.SDK_STUB,
            status="completed",
            simulated=True,
            providerExecution=True,
            modelIds=[],
            boundary=(
                "Deterministic Agents SDK model and demo hotel adapter only; "
                "no OpenAI model call, real booking, or payment change."
            ),
            providerResult=dispatch.provider_result,
            authorizationSource="Approved Agents SDK commit_remedy interruption.",
            verificationResults=[
                "Demo provider dispatch returned confirmed.",
                "Provider result stored under one idempotency key.",
            ],
        )
        tool_context.context.store.record_transition(
            tool_context.context.recovery_id,
            status=RecoveryStatus.COMPLETED,
            current_step=5,
            current_step_summary="Demo provider result verified and receipt sealed.",
            event_type="recovery.completed",
            event_data={
                "phase": "Verify & seal",
                "providerExecution": True,
                "summary": "One idempotent demo-provider dispatch completed.",
            },
            receipt=receipt,
        )
        return dispatch.model_dump_json()

    model = DeterministicApprovalModel(
        arguments=arguments,
        call_id=f"commit-remedy-{context.recovery_id}",
    )
    return Agent[HotelAgentContext](
        name=HOTEL_AGENT_NAME,
        instructions=HOTEL_AGENT_INSTRUCTIONS,
        model=model,
        tools=[commit_remedy],
    )
