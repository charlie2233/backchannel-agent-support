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
from agents.tool import FunctionTool

from server.agents.schemas import (
    BrokerRemedy,
    CommitRemedyArguments,
    ConsumerProof,
    ProviderProof,
)
from server.config import APPROVAL_PROTOCOL_VERSION, HOTEL_AGENT_GRAPH_VERSION

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
