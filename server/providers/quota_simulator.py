"""Deterministic, idempotent demo boundary for temporary API quota recovery."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from threading import RLock
from typing import Literal

from agents import Agent, function_tool
from agents.agent_output import AgentOutputSchemaBase
from agents.handoffs import Handoff
from agents.items import ModelResponse, TResponseInputItem, TResponseStreamEvent
from agents.model_settings import ModelSettings
from agents.models.interface import Model, ModelTracing
from agents.tool import FunctionTool, Tool
from agents.tool_context import ToolContext
from agents.usage import Usage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)
from openai.types.responses.response_prompt_param import ResponsePromptParam
from pydantic import BaseModel, ConfigDict, model_validator

from server.agents.versioning import canonical_digest

QUOTA_AGENT_NAME = "Backchannel API quota recovery"
QUOTA_AGENT_INSTRUCTIONS = (
    "Run the deterministic demo quota recovery tool once and report completion. "
    "The tool requires no human approval and never changes a real quota."
)
QUOTA_START_PROMPT = "Run the deterministic API quota recovery through verification and revocation."
QUOTA_TOOL_NAME = "recover_api_quota"
QUOTA_TOOL_CALL_ID = "recover-api-quota-demo"
QUOTA_AGENT_GRAPH_VERSION = "backchannel.quota-agent.v1"
QUOTA_PROTOCOL_VERSION = "backchannel.quota.v1"


class QuotaSchema(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class QuotaRecoveryRequest(QuotaSchema):
    account_id: Literal["demo-api-account"]
    region: Literal["us-east-1"]
    baseline_ceiling_units: Literal[1000]
    required_units: Literal[1200]
    burst_units: Literal[250]
    duration_seconds: Literal[900]
    extra_cost_minor: Literal[300]
    currency: Literal["USD"]


class QuotaDelegatedAuthority(QuotaSchema):
    account_id: Literal["demo-api-account"]
    allowed_regions: tuple[Literal["us-east-1"], ...]
    maximum_duration_seconds: Literal[900]
    maximum_extra_cost_minor: Literal[500]
    currency: Literal["USD"]


class QuotaCeilingProof(QuotaSchema):
    baseline_ceiling_units: Literal[1000]
    required_units: Literal[1200]
    shortfall_units: Literal[200]
    provider_evidence_id: Literal["quota-ceiling-proof-demo"]


class QuotaBurstGrant(QuotaSchema):
    permission_id: Literal["quota-burst-demo-us-east-1"]
    region: Literal["us-east-1"]
    burst_units: Literal[250]
    effective_ceiling_units: Literal[1250]
    duration_seconds: Literal[900]
    extra_cost_minor: Literal[300]
    currency: Literal["USD"]


class QuotaRecoveryResult(QuotaSchema):
    ceiling_proof: QuotaCeilingProof
    grant: QuotaBurstGrant
    authority: QuotaDelegatedAuthority
    hard_constraints_satisfied: Literal[True]
    delegated_authority_satisfied: Literal[True]
    approval_count: Literal[0]
    execution_verified: Literal[True]
    permission_revoked: Literal[True]
    restored_ceiling_units: Literal[1000]
    simulated: Literal[True]

    @model_validator(mode="after")
    def require_complementary_policy_proof(self) -> QuotaRecoveryResult:
        if self.ceiling_proof.required_units <= self.ceiling_proof.baseline_ceiling_units:
            raise ValueError("Quota demand must exceed the proven baseline ceiling")
        if (
            self.ceiling_proof.baseline_ceiling_units + self.grant.burst_units
            != self.grant.effective_ceiling_units
        ):
            raise ValueError("Temporary burst must derive the effective ceiling")
        if self.grant.effective_ceiling_units < self.ceiling_proof.required_units:
            raise ValueError("Temporary burst must cover the required demand")
        if self.grant.region not in self.authority.allowed_regions:
            raise ValueError("Temporary burst must remain in an authorized US region")
        if self.grant.duration_seconds > self.authority.maximum_duration_seconds:
            raise ValueError("Temporary burst exceeds delegated duration")
        if self.grant.extra_cost_minor > self.authority.maximum_extra_cost_minor:
            raise ValueError("Temporary burst exceeds delegated cost")
        if self.grant.currency != self.authority.currency:
            raise ValueError("Temporary burst currency differs from delegated authority")
        if self.restored_ceiling_units != self.ceiling_proof.baseline_ceiling_units:
            raise ValueError("Revocation must restore the proven baseline ceiling")
        return self


DETERMINISTIC_QUOTA_REQUEST = QuotaRecoveryRequest(
    account_id="demo-api-account",
    region="us-east-1",
    baseline_ceiling_units=1000,
    required_units=1200,
    burst_units=250,
    duration_seconds=900,
    extra_cost_minor=300,
    currency="USD",
)

DETERMINISTIC_QUOTA_AUTHORITY = QuotaDelegatedAuthority(
    account_id="demo-api-account",
    allowed_regions=("us-east-1",),
    maximum_duration_seconds=900,
    maximum_extra_cost_minor=500,
    currency="USD",
)

DETERMINISTIC_QUOTA_RESULT = QuotaRecoveryResult(
    ceiling_proof=QuotaCeilingProof(
        baseline_ceiling_units=1000,
        required_units=1200,
        shortfall_units=200,
        provider_evidence_id="quota-ceiling-proof-demo",
    ),
    grant=QuotaBurstGrant(
        permission_id="quota-burst-demo-us-east-1",
        region="us-east-1",
        burst_units=250,
        effective_ceiling_units=1250,
        duration_seconds=900,
        extra_cost_minor=300,
        currency="USD",
    ),
    authority=DETERMINISTIC_QUOTA_AUTHORITY,
    hard_constraints_satisfied=True,
    delegated_authority_satisfied=True,
    approval_count=0,
    execution_verified=True,
    permission_revoked=True,
    restored_ceiling_units=1000,
    simulated=True,
)


def quota_request_digest(request: QuotaRecoveryRequest) -> str:
    """Return the canonical durable identity for one exact quota request."""

    serialized = json.dumps(
        request.model_dump(mode="json"),
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


class QuotaVerificationError(RuntimeError):
    """Raised after a temporary grant when deterministic verification is injected to fail."""


class QuotaIdempotencyConflictError(ValueError):
    """Raised when one quota idempotency key is reused for different exact terms."""


class QuotaSimulator:
    """Grant, verify, and always revoke one deterministic demo permission."""

    def __init__(self, *, fail_verification: bool = False) -> None:
        self._lock = RLock()
        self._fail_verification = fail_verification
        self._results: dict[str, tuple[str, QuotaRecoveryResult]] = {}
        self._active_permissions: set[str] = set()
        self._execution_count = 0

    @staticmethod
    def _fingerprint(request: QuotaRecoveryRequest) -> str:
        return quota_request_digest(request)

    @property
    def execution_count(self) -> int:
        with self._lock:
            return self._execution_count

    @property
    def active_permission_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._active_permissions))

    def result_for(self, idempotency_key: str) -> QuotaRecoveryResult:
        with self._lock:
            stored = self._results.get(idempotency_key)
            if stored is None:
                raise KeyError("Quota recovery result was not recorded")
            return stored[1]

    def recover(
        self,
        request: QuotaRecoveryRequest,
        *,
        idempotency_key: str,
    ) -> QuotaRecoveryResult:
        if not idempotency_key.strip():
            raise ValueError("idempotency_key must not be empty")
        fingerprint = self._fingerprint(request)
        with self._lock:
            existing = self._results.get(idempotency_key)
            if existing is not None:
                existing_fingerprint, result = existing
                if existing_fingerprint != fingerprint:
                    raise QuotaIdempotencyConflictError(
                        "Quota idempotency key was reused for different terms"
                    )
                return result

            if request.account_id != DETERMINISTIC_QUOTA_AUTHORITY.account_id:
                raise ValueError("Quota request is outside delegated account authority")
            if request.region not in DETERMINISTIC_QUOTA_AUTHORITY.allowed_regions:
                raise ValueError("Quota request is outside delegated region authority")
            if request.duration_seconds > DETERMINISTIC_QUOTA_AUTHORITY.maximum_duration_seconds:
                raise ValueError("Quota request exceeds delegated duration authority")
            if request.extra_cost_minor > DETERMINISTIC_QUOTA_AUTHORITY.maximum_extra_cost_minor:
                raise ValueError("Quota request exceeds delegated cost authority")

            grant = DETERMINISTIC_QUOTA_RESULT.grant
            self._execution_count += 1
            self._active_permissions.add(grant.permission_id)
            try:
                if self._fail_verification:
                    raise QuotaVerificationError("Injected quota verification failure")
                result = DETERMINISTIC_QUOTA_RESULT
            finally:
                self._active_permissions.discard(grant.permission_id)

            self._results[idempotency_key] = (fingerprint, result)
            return result


@dataclass(frozen=True, slots=True)
class QuotaAgentContext:
    recovery_id: str
    provider: QuotaSimulator
    persist_result: Callable[[QuotaRecoveryResult], None]
    request: QuotaRecoveryRequest = DETERMINISTIC_QUOTA_REQUEST

    @property
    def idempotency_key(self) -> str:
        return f"{self.recovery_id}:quota-burst"


class DeterministicQuotaModel(Model):
    """Emit one non-approval quota tool call and then close deterministically."""

    @staticmethod
    def _matching_tool_output(model_input: str | list[TResponseInputItem]) -> str | None:
        if not isinstance(model_input, list):
            return None
        for item in model_input:
            if isinstance(item, Mapping):
                item_type = item.get("type")
                call_id = item.get("call_id")
                output = item.get("output")
            else:
                item_type = getattr(item, "type", None)
                call_id = getattr(item, "call_id", None)
                output = getattr(item, "output", None)
            if item_type == "function_call_output" and call_id == QUOTA_TOOL_CALL_ID:
                if not isinstance(output, str):
                    raise RuntimeError("Deterministic quota tool output must be text")
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
        matching_output = self._matching_tool_output(input)
        del (
            system_instructions,
            model_settings,
            output_schema,
            handoffs,
            tracing,
            previous_response_id,
            conversation_id,
            prompt,
        )
        if [tool.name for tool in tools] != [QUOTA_TOOL_NAME]:
            raise RuntimeError("Deterministic quota model requires exactly its quota tool")
        if matching_output is None:
            return ModelResponse(
                output=[
                    ResponseFunctionToolCall(
                        type="function_call",
                        name=QUOTA_TOOL_NAME,
                        call_id=QUOTA_TOOL_CALL_ID,
                        arguments="{}",
                    )
                ],
                usage=Usage(),
                response_id="stub-quota-response-1",
            )
        QuotaRecoveryResult.model_validate_json(matching_output)
        return ModelResponse(
            output=[
                ResponseOutputMessage(
                    id="stub-quota-message",
                    role="assistant",
                    status="completed",
                    type="message",
                    content=[
                        ResponseOutputText(
                            type="output_text",
                            text="Deterministic quota recovery completed and permission revoked.",
                            annotations=[],
                        )
                    ],
                )
            ],
            usage=Usage(),
            response_id="stub-quota-response-2",
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
        raise NotImplementedError("Deterministic quota smoke uses non-streaming Runner.run")


def build_quota_agent(*, context: QuotaAgentContext) -> Agent[QuotaAgentContext]:
    @function_tool(needs_approval=False, failure_error_function=None)
    def recover_api_quota(tool_context: ToolContext[QuotaAgentContext]) -> str:
        """Run the bounded demo quota grant, verification, and revocation lifecycle."""

        result = tool_context.context.provider.recover(
            tool_context.context.request,
            idempotency_key=tool_context.context.idempotency_key,
        )
        tool_context.context.persist_result(result)
        return result.model_dump_json()

    return Agent[QuotaAgentContext](
        name=QUOTA_AGENT_NAME,
        instructions=QUOTA_AGENT_INSTRUCTIONS,
        model=DeterministicQuotaModel(),
        tools=[recover_api_quota],
    )


def quota_definition_digest(agent: Agent[QuotaAgentContext]) -> str:
    if not isinstance(agent.instructions, str):
        raise TypeError("Quota agent requires static instructions")
    tools: list[dict[str, object]] = []
    for tool in agent.tools:
        if not isinstance(tool, FunctionTool):
            raise TypeError("Quota agent requires function tools only")
        tools.append(
            {
                "description": tool.description,
                "name": tool.name,
                "needsApproval": tool.needs_approval,
                "paramsJsonSchema": tool.params_json_schema,
                "strictJsonSchema": tool.strict_json_schema,
            }
        )
    return canonical_digest(
        {
            "agent": {
                "instructions": agent.instructions,
                "name": agent.name,
                "startPrompt": QUOTA_START_PROMPT,
            },
            "providerSchemas": {
                schema.__name__: schema.model_json_schema()
                for schema in (
                    QuotaRecoveryRequest,
                    QuotaDelegatedAuthority,
                    QuotaCeilingProof,
                    QuotaBurstGrant,
                    QuotaRecoveryResult,
                )
            },
            "tools": tools,
        }
    )
