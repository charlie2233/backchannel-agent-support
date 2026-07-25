"""Build the shared hotel Agent graph and approval-gated commit tool."""

from __future__ import annotations

from dataclasses import dataclass

from agents import Agent, function_tool
from agents.model_settings import ModelSettings
from agents.tool import FunctionTool
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
    LIVE_BROKER_INSTRUCTIONS,
    LIVE_BROKER_MODEL,
    LIVE_BROKER_NAME,
    LIVE_CONSUMER_INSTRUCTIONS,
    LIVE_CONSUMER_MODEL,
    LIVE_CONSUMER_NAME,
    LIVE_PROVIDER_INSTRUCTIONS,
    LIVE_PROVIDER_MODEL,
    LIVE_PROVIDER_NAME,
)
from server.providers.hotel_simulator import HotelDispatchRequest, HotelSimulator
from server.store import SQLiteStore


@dataclass(frozen=True, slots=True)
class HotelAgentContext:
    recovery_id: str
    store: SQLiteStore
    hotel_provider: HotelSimulator
    approved_remedy_digest: str | None = None


@dataclass(frozen=True, slots=True)
class LiveHotelAgents:
    consumer: Agent[HotelAgentContext]
    provider: Agent[HotelAgentContext]
    broker: Agent[HotelAgentContext]


def _commit_remedy_tool() -> FunctionTool:
    """Build the one exact approval-gated demo-provider commit tool."""

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
        execution = tool_context.context.store.get_completed_execution(
            tool_context.context.recovery_id
        )
        if execution is None:
            raise RuntimeError("Durable provider result was not recorded")
        return dispatch.model_dump_json()

    return commit_remedy


def build_hotel_agent(
    *,
    context: HotelAgentContext,
    arguments: CommitRemedyArguments,
) -> Agent[HotelAgentContext]:
    """Build the deterministic root Agent guarded by the shared interruption."""

    model = DeterministicApprovalModel(
        arguments=arguments,
        call_id=f"commit-remedy-{context.recovery_id}",
    )
    return Agent[HotelAgentContext](
        name=HOTEL_AGENT_NAME,
        instructions=HOTEL_AGENT_INSTRUCTIONS,
        model=model,
        tools=[_commit_remedy_tool()],
    )


def build_live_hotel_agents(*, context: HotelAgentContext) -> LiveHotelAgents:
    """Build the typed live proof agents and forced-tool broker graph."""

    return LiveHotelAgents(
        consumer=Agent[HotelAgentContext](
            name=LIVE_CONSUMER_NAME,
            instructions=LIVE_CONSUMER_INSTRUCTIONS,
            model=LIVE_CONSUMER_MODEL,
            output_type=ConsumerProof,
        ),
        provider=Agent[HotelAgentContext](
            name=LIVE_PROVIDER_NAME,
            instructions=LIVE_PROVIDER_INSTRUCTIONS,
            model=LIVE_PROVIDER_MODEL,
            output_type=ProviderProof,
        ),
        broker=Agent[HotelAgentContext](
            name=LIVE_BROKER_NAME,
            instructions=LIVE_BROKER_INSTRUCTIONS,
            model=LIVE_BROKER_MODEL,
            model_settings=ModelSettings(
                tool_choice="commit_remedy",
                parallel_tool_calls=False,
            ),
            tools=[_commit_remedy_tool()],
        ),
    )
