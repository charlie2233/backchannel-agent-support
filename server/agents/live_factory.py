"""Build the three explicit, strict-output Agents in the live hotel graph."""

from __future__ import annotations

import json
from dataclasses import dataclass

from agents import Agent, ModelSettings

from server.agents.factory import HotelAgentContext, build_commit_remedy_tool
from server.agents.live_models import (
    LIVE_BROKER_MODEL,
    LIVE_CONSUMER_MODEL,
    LIVE_PROVIDER_MODEL,
)
from server.agents.schemas import (
    BrokerOutcome,
    CommitRemedyArguments,
    ConsumerProof,
    ProviderProof,
)

LIVE_CONSUMER_AGENT_NAME = "Backchannel consumer proof"
LIVE_PROVIDER_AGENT_NAME = "Backchannel provider proof"
LIVE_BROKER_AGENT_NAME = "Backchannel remedy broker"
LIVE_CONSUMER_INSTRUCTIONS = (
    "Return only strict ConsumerProof JSON for the supplied demo booking record. "
    "Do not infer facts not present in the record."
)
LIVE_PROVIDER_INSTRUCTIONS = (
    "Return only strict ProviderProof JSON for the supplied demo hotel evidence. "
    "This is a demo adapter record, not a real provider lookup."
)
LIVE_BROKER_INSTRUCTIONS = (
    "Use the supplied strict proofs and proposed remedy. Call commit_remedy exactly "
    "once with those exact values. The approval boundary is mandatory. After an "
    "approved tool result return strict BrokerOutcome JSON with status completed. "
    "After an operator rejection return strict BrokerOutcome JSON with status "
    "closed_without_action and select no alternative. Never claim a real booking, "
    "payment, or hotel-provider mutation."
)
LIVE_CONSUMER_PROMPT = json.dumps(
    {
        "bookingId": "booking-demo-001",
        "requestedRoomType": "king",
        "bookingDates": ["2026-08-14", "2026-08-16"],
        "paidTotalMinor": 42000,
        "currency": "USD",
    },
    separators=(",", ":"),
    sort_keys=True,
)
LIVE_PROVIDER_PROMPT = json.dumps(
    {
        "bookingId": "booking-demo-001",
        "confirmedRoomType": "double",
        "availableRoomTypes": ["king"],
        "conflictCode": "room_assignment_mismatch",
    },
    separators=(",", ":"),
    sort_keys=True,
)


@dataclass(frozen=True, slots=True)
class LiveHotelAgents:
    consumer: Agent[HotelAgentContext]
    provider: Agent[HotelAgentContext]
    broker: Agent[HotelAgentContext]


def build_live_consumer_agent() -> Agent[HotelAgentContext]:
    return Agent[HotelAgentContext](
        name=LIVE_CONSUMER_AGENT_NAME,
        instructions=LIVE_CONSUMER_INSTRUCTIONS,
        model=LIVE_CONSUMER_MODEL,
        output_type=ConsumerProof,
    )


def build_live_provider_agent() -> Agent[HotelAgentContext]:
    return Agent[HotelAgentContext](
        name=LIVE_PROVIDER_AGENT_NAME,
        instructions=LIVE_PROVIDER_INSTRUCTIONS,
        model=LIVE_PROVIDER_MODEL,
        output_type=ProviderProof,
    )


def build_live_broker_agent() -> Agent[HotelAgentContext]:
    return Agent[HotelAgentContext](
        name=LIVE_BROKER_AGENT_NAME,
        instructions=LIVE_BROKER_INSTRUCTIONS,
        model=LIVE_BROKER_MODEL,
        model_settings=ModelSettings(tool_choice="required", parallel_tool_calls=False),
        output_type=BrokerOutcome,
        tools=[build_commit_remedy_tool()],
        reset_tool_choice=True,
    )


def build_live_hotel_agents() -> LiveHotelAgents:
    return LiveHotelAgents(
        consumer=build_live_consumer_agent(),
        provider=build_live_provider_agent(),
        broker=build_live_broker_agent(),
    )


def live_broker_prompt(arguments: CommitRemedyArguments) -> str:
    return json.dumps(
        {
            "consumerProof": arguments.consumer_proof.model_dump(mode="json"),
            "providerProof": arguments.provider_proof.model_dump(mode="json"),
            "proposedRemedy": arguments.remedy.model_dump(mode="json"),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
