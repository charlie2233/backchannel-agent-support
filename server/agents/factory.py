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
)
from server.models import ExecutionMode, RecoveryReceipt
from server.providers.hotel_simulator import HotelDispatchRequest, HotelSimulator
from server.store import SQLiteStore


@dataclass(frozen=True, slots=True)
class HotelAgentContext:
    recovery_id: str
    store: SQLiteStore
    hotel_provider: HotelSimulator
    approved_remedy_digest: str | None = None


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

        remedy_digest = tool_context.context.approved_remedy_digest
        if remedy_digest is None or not remedy_digest.startswith("sha256:"):
            raise ValueError("Exact public consent digest is required before dispatch")
        tool_context.context.store.assert_provider_dispatch_authorized(
            recovery_id=tool_context.context.recovery_id,
            tool_call_id=tool_context.tool_call_id,
            remedy_digest=remedy_digest,
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
                "Temporary permission revoked after terminal completion.",
            ],
            decision="approved",
            decisionRemedyDigest=remedy_digest,
            executionCount=1,
            providerDispatchStarted=True,
            exactInterruptionRejected=False,
            permissionRevoked=True,
            scopeClosed=True,
            approvedRemedyDigest=remedy_digest,
        )
        execution = tool_context.context.store.get_completed_execution(
            tool_context.context.recovery_id
        )
        if execution is None:
            raise RuntimeError("Durable provider result was not recorded")
        tool_context.context.store.finalize_completed_execution(
            execution,
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
