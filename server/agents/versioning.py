"""Canonical version markers for durable Agents SDK approval snapshots."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from importlib.metadata import version
from typing import Any
from uuid import uuid4

from agents import Agent
from agents.agent_output import AgentOutputSchema, AgentOutputSchemaBase
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
    HOTEL_LIVE_AGENT_GRAPH_VERSION,
)
from server.models import ExecutionMode

SDK_DISTRIBUTION = "openai-agents"
_QA_TRACE_PATTERN = re.compile(r"qa_trace_[0-9a-f]{32}\Z")
HOTEL_AGENT_NAME = "Backchannel hotel recovery"
HOTEL_AGENT_INSTRUCTIONS = (
    "Use the supplied typed evidence and request approval before commit_remedy."
)
HOTEL_START_PROMPT = (
    "Run the deterministic hotel recovery to its authorization boundary."
)


@dataclass(frozen=True, slots=True)
class ApprovalVersionPolicy:
    """Runtime contract that a serialized pending approval must exactly match."""

    sdk_version: str
    protocol_version: str
    agent_graph_version: str

    @classmethod
    def current(cls) -> ApprovalVersionPolicy:
        return cls(
            sdk_version=version(SDK_DISTRIBUTION),
            protocol_version=APPROVAL_PROTOCOL_VERSION,
            agent_graph_version=HOTEL_AGENT_GRAPH_VERSION,
        )

    @classmethod
    def for_mode(cls, execution_mode: ExecutionMode) -> ApprovalVersionPolicy:
        graph_version = (
            HOTEL_LIVE_AGENT_GRAPH_VERSION
            if execution_mode is ExecutionMode.OPENAI_LIVE
            else HOTEL_AGENT_GRAPH_VERSION
        )
        return cls(
            sdk_version=version(SDK_DISTRIBUTION),
            protocol_version=APPROVAL_PROTOCOL_VERSION,
            agent_graph_version=graph_version,
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


def live_hotel_definition_digest(agents: tuple[Agent[Any], Agent[Any], Agent[Any]]) -> str:
    """Bind live resume to all Agent prompts, models, output schemas, and tools."""

    from server.agents.live_factory import (  # Local import avoids graph construction cycle.
        LIVE_BROKER_INSTRUCTIONS,
        LIVE_CONSUMER_INSTRUCTIONS,
        LIVE_CONSUMER_PROMPT,
        LIVE_PROVIDER_INSTRUCTIONS,
        LIVE_PROVIDER_PROMPT,
    )

    definitions: list[dict[str, Any]] = []
    for agent in agents:
        if not isinstance(agent.instructions, str) or not isinstance(agent.model, str):
            raise TypeError("The live hotel graph requires static prompts and model aliases")
        tools: list[dict[str, Any]] = []
        for tool in agent.tools:
            if not isinstance(tool, FunctionTool):
                raise TypeError("The live hotel graph requires function tools only")
            tools.append(
                {
                    "description": tool.description,
                    "name": tool.name,
                    "needsApproval": tool.needs_approval,
                    "paramsJsonSchema": tool.params_json_schema,
                    "strictJsonSchema": tool.strict_json_schema,
                }
            )
        if isinstance(agent.output_type, AgentOutputSchemaBase):
            output_schema = agent.output_type
        elif agent.output_type is None:
            raise TypeError("Every live hotel Agent requires a strict output schema")
        else:
            output_schema = AgentOutputSchema(agent.output_type)
        definitions.append(
            {
                "instructions": agent.instructions,
                "model": agent.model,
                "name": agent.name,
                "outputSchema": (
                    {
                        "jsonSchema": output_schema.json_schema(),
                        "name": output_schema.name(),
                        "strict": output_schema.is_strict_json_schema(),
                    }
                    if output_schema is not None and not output_schema.is_plain_text()
                    else None
                ),
                "tools": tools,
            }
        )
    return canonical_digest(
        {
            "agents": definitions,
            "prompts": {
                "consumerInstructions": LIVE_CONSUMER_INSTRUCTIONS,
                "consumerStart": LIVE_CONSUMER_PROMPT,
                "providerInstructions": LIVE_PROVIDER_INSTRUCTIONS,
                "providerStart": LIVE_PROVIDER_PROMPT,
                "brokerInstructions": LIVE_BROKER_INSTRUCTIONS,
            },
        }
    )


def remedy_action_digest(arguments: CommitRemedyArguments) -> str:
    """Create the internal idempotency digest for the deterministic remedy action."""

    return canonical_digest(arguments.remedy.model_dump(mode="json"))


def broker_remedy_action_digest(remedy: BrokerRemedy) -> str:
    """Hash an already validated remedy at the provider-tool boundary."""

    return canonical_digest(remedy.model_dump(mode="json"))


def new_qa_trace_id() -> str:
    """Return a local SDK QA correlation ID, never an OpenAI trace identifier."""

    return f"qa_trace_{uuid4().hex}"


def is_valid_qa_trace_id(value: str) -> bool:
    return _QA_TRACE_PATTERN.fullmatch(value) is not None


_OPENAI_TRACE_PATTERN = re.compile(r"trace_[0-9a-f]{32}\Z")


def is_valid_openai_trace_id(value: str) -> bool:
    return _OPENAI_TRACE_PATTERN.fullmatch(value) is not None
