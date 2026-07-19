"""Canonical version markers for durable Agents SDK approval snapshots."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from importlib.metadata import version
from typing import Any
from uuid import uuid4

from agents import Agent
from agents.tool import FunctionTool

from server.agents.schemas import (
    BrokerRemedy,
    CommitRemedyArguments,
    ConsumerProof,
    ProviderProof,
)
from server.config import (
    APPROVAL_PROTOCOL_VERSION,
    HOTEL_AGENT_GRAPH_VERSION,
    LIVE_HOTEL_AGENT_GRAPH_VERSION,
)

SDK_DISTRIBUTION = "openai-agents"
HOTEL_AGENT_NAME = "Backchannel hotel recovery"
HOTEL_AGENT_INSTRUCTIONS = (
    "Use the supplied typed evidence and request approval before commit_remedy."
)
HOTEL_START_PROMPT = "Run the deterministic hotel recovery to its authorization boundary."
LIVE_CONSUMER_MODEL = "gpt-5.6-luna"
LIVE_PROVIDER_MODEL = "gpt-5.6-luna"
LIVE_BROKER_MODEL = "gpt-5.6-terra"
LIVE_CONSUMER_NAME = "Backchannel live consumer proof"
LIVE_PROVIDER_NAME = "Backchannel live provider proof"
LIVE_BROKER_NAME = "Backchannel live hotel broker"
LIVE_CONSUMER_INSTRUCTIONS = (
    "Return only a typed ConsumerProof for the supplied demo hotel booking facts."
)
LIVE_PROVIDER_INSTRUCTIONS = (
    "Return only a typed ProviderProof for the supplied demo hotel inventory facts."
)
LIVE_BROKER_INSTRUCTIONS = (
    "Use only the supplied validated ConsumerProof and ProviderProof. Call commit_remedy "
    "exactly once with those proofs and one zero-cost replacement remedy. Never claim a real "
    "booking or payment change."
)
LIVE_CONSUMER_START_PROMPT = (
    "Demo booking booking-demo-001 requested a king room for 2026-08-14 through "
    "2026-08-16, paid 42000 USD minor units. Produce ConsumerProof."
)
LIVE_PROVIDER_START_PROMPT = (
    "Demo booking booking-demo-001 is assigned double while king is available; the conflict "
    "code is room_assignment_mismatch. Produce ProviderProof."
)
LIVE_BROKER_START_PROMPT_TEMPLATE = (
    "Validated consumer proof: {consumer_proof_json}\n"
    "Validated provider proof: {provider_proof_json}\n"
    "Request approval for a replace_room remedy to king with zero USD cost delta, changing "
    "only room_type, preserving booking dates, and adding no charge."
)


@dataclass(frozen=True, slots=True)
class ApprovalVersionPolicy:
    """Runtime contract that a serialized pending approval must exactly match."""

    sdk_version: str
    protocol_version: str
    agent_graph_version: str
    live_agent_graph_version: str = LIVE_HOTEL_AGENT_GRAPH_VERSION

    @classmethod
    def current(cls) -> ApprovalVersionPolicy:
        return cls(
            sdk_version=version(SDK_DISTRIBUTION),
            protocol_version=APPROVAL_PROTOCOL_VERSION,
            agent_graph_version=HOTEL_AGENT_GRAPH_VERSION,
            live_agent_graph_version=LIVE_HOTEL_AGENT_GRAPH_VERSION,
        )


def canonical_digest(value: Any) -> str:
    """Hash one JSON value using the stable encoding shared by restart markers."""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def hotel_definition_digest(agent: Agent[Any]) -> str:
    """Bind resume to the exact prompt, typed schemas, and approval tool schema."""

    if not isinstance(agent.instructions, str):
        raise TypeError("The durable hotel agent requires static instructions")
    tools: list[dict[str, Any]] = []
    for tool in agent.tools:
        if not isinstance(tool, FunctionTool):
            raise TypeError("The durable hotel agent requires function tools only")
        tools.append(
            {
                "description": tool.description,
                "name": tool.name,
                "needsApproval": tool.needs_approval,
                "paramsJsonSchema": tool.params_json_schema,
                "strictJsonSchema": tool.strict_json_schema,
            }
        )
    definition = {
        "agent": {
            "instructions": agent.instructions,
            "name": agent.name,
            "startPrompt": HOTEL_START_PROMPT,
        },
        "structuredOutputSchemas": {
            schema.__name__: schema.model_json_schema()
            for schema in (
                ConsumerProof,
                ProviderProof,
                BrokerRemedy,
                CommitRemedyArguments,
            )
        },
        "tools": tools,
    }
    return canonical_digest(definition)


def live_broker_start_prompt(
    consumer_proof: ConsumerProof,
    provider_proof: ProviderProof,
) -> str:
    """Render the broker input from already validated typed proofs."""

    return LIVE_BROKER_START_PROMPT_TEMPLATE.format(
        consumer_proof_json=consumer_proof.model_dump_json(),
        provider_proof_json=provider_proof.model_dump_json(),
    )


def live_hotel_definition_digest(
    *,
    consumer_agent: Agent[Any],
    provider_agent: Agent[Any],
    broker_agent: Agent[Any],
) -> str:
    """Bind every live prompt, selected model, output schema, and tool schema."""

    agents = (consumer_agent, provider_agent, broker_agent)
    for agent in agents:
        if not isinstance(agent.instructions, str) or not isinstance(agent.model, str):
            raise TypeError("The durable live graph requires static prompts and model names")
    broker_tools: list[dict[str, Any]] = []
    for tool in broker_agent.tools:
        if not isinstance(tool, FunctionTool):
            raise TypeError("The durable live broker requires function tools only")
        broker_tools.append(
            {
                "description": tool.description,
                "name": tool.name,
                "needsApproval": tool.needs_approval,
                "paramsJsonSchema": tool.params_json_schema,
                "strictJsonSchema": tool.strict_json_schema,
            }
        )
    definition = {
        "agents": [
            {
                "instructions": agent.instructions,
                "model": agent.model,
                "name": agent.name,
            }
            for agent in agents
        ],
        "prompts": {
            "consumerStart": LIVE_CONSUMER_START_PROMPT,
            "providerStart": LIVE_PROVIDER_START_PROMPT,
            "brokerStartTemplate": LIVE_BROKER_START_PROMPT_TEMPLATE,
        },
        "structuredOutputSchemas": {
            schema.__name__: schema.model_json_schema()
            for schema in (
                ConsumerProof,
                ProviderProof,
                BrokerRemedy,
                CommitRemedyArguments,
            )
        },
        "tools": broker_tools,
    }
    return canonical_digest(definition)


def remedy_action_digest(arguments: CommitRemedyArguments) -> str:
    """Create the internal idempotency digest for the deterministic remedy action."""

    return canonical_digest(arguments.remedy.model_dump(mode="json"))


def broker_remedy_action_digest(remedy: BrokerRemedy) -> str:
    """Hash an already validated remedy at the provider-tool boundary."""

    return canonical_digest(remedy.model_dump(mode="json"))


def new_qa_trace_id() -> str:
    """Return a local SDK QA correlation ID, never an OpenAI trace identifier."""

    return f"qa_trace_{uuid4().hex}"
