"""One deterministic Agents SDK graph for the API-quota recovery."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

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

from server.agents.versioning import canonical_digest
from server.providers.quota_simulator import (
    QuotaGrantRequest,
    QuotaGrantResult,
    QuotaSimulator,
)

QUOTA_AGENT_NAME = "Backchannel API quota recovery"
QUOTA_AGENT_INSTRUCTIONS = (
    "Use the one deterministic grant_temporary_quota tool to issue and verify the "
    "canonical delegated temporary quota grant without human approval."
)
QUOTA_START_PROMPT = (
    "Run Detect, Prove, Negotiate, Authorize, Execute, and Verify & seal for the "
    "canonical API quota recovery."
)
_NO_MATCHING_OUTPUT = object()


@dataclass(slots=True)
class QuotaAgentContext:
    recovery_id: str
    quota_provider: QuotaSimulator
    permission_scope_id: str
    dispatch_result: QuotaGrantResult | None = None
    tool_call_id: str | None = None


def canonical_quota_request(recovery_id: str) -> QuotaGrantRequest:
    return QuotaGrantRequest(
        recovery_id=recovery_id,
        recorded_demand_rpm=1200,
        temporary_burst_rpm=1500,
        region="US",
        duration_seconds=900,
        extra_cost_minor=250,
        delegated_authority_max_minor=500,
        currency="USD",
    )


class DeterministicQuotaModel(Model):
    """Emit the graph's sole provider tool call, then a terminal response."""

    def __init__(self, *, call_id: str) -> None:
        self._call_id = call_id

    def _matching_output(self, model_input: str | list[TResponseInputItem]) -> object:
        if not isinstance(model_input, list):
            return _NO_MATCHING_OUTPUT
        for item in model_input:
            if isinstance(item, Mapping):
                item_type = item.get("type")
                call_id = item.get("call_id")
                output = item.get("output")
            else:
                item_type = getattr(item, "type", None)
                call_id = getattr(item, "call_id", None)
                output = getattr(item, "output", None)
            if item_type == "function_call_output" and call_id == self._call_id:
                return output
        return _NO_MATCHING_OUTPUT

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
        function_output = self._matching_output(input)
        del (
            system_instructions,
            input,
            model_settings,
            output_schema,
            handoffs,
            tracing,
            previous_response_id,
            conversation_id,
            prompt,
        )
        if [tool.name for tool in tools] != ["grant_temporary_quota"]:
            raise RuntimeError(
                "Deterministic quota model requires exactly grant_temporary_quota"
            )
        if function_output is _NO_MATCHING_OUTPUT:
            return ModelResponse(
                output=[
                    ResponseFunctionToolCall(
                        type="function_call",
                        name="grant_temporary_quota",
                        call_id=self._call_id,
                        arguments="{}",
                    )
                ],
                usage=Usage(),
                response_id=f"stub-response-{self._call_id}-1",
            )
        if not isinstance(function_output, str):
            raise RuntimeError("Quota provider tool output must be a string")
        try:
            QuotaGrantResult.model_validate_json(function_output)
        except ValueError:
            raise RuntimeError("Quota provider tool output failed strict validation") from None
        return ModelResponse(
            output=[
                ResponseOutputMessage(
                    id=f"stub-message-{self._call_id}",
                    role="assistant",
                    status="completed",
                    type="message",
                    content=[
                        ResponseOutputText(
                            type="output_text",
                            text="Deterministic API quota recovery completed.",
                            annotations=[],
                        )
                    ],
                )
            ],
            usage=Usage(),
            response_id=f"stub-response-{self._call_id}-2",
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
        raise NotImplementedError("Deterministic quota QA uses non-streaming Runner.run")


def build_grant_temporary_quota_tool() -> FunctionTool:
    @function_tool(needs_approval=False, failure_error_function=None)
    def grant_temporary_quota(
        tool_context: ToolContext[QuotaAgentContext],
    ) -> str:
        """Issue and verify the canonical temporary quota grant."""

        context = tool_context.context
        context.tool_call_id = tool_context.tool_call_id
        result = context.quota_provider.dispatch(
            canonical_quota_request(context.recovery_id),
            idempotency_key=f"{context.recovery_id}:{tool_context.tool_call_id}",
            permission_scope_id=context.permission_scope_id,
        )
        context.dispatch_result = result
        return result.model_dump_json()

    return grant_temporary_quota


def build_quota_agent(*, context: QuotaAgentContext) -> Agent[QuotaAgentContext]:
    call_id = f"grant-quota-{context.recovery_id}"
    return Agent[QuotaAgentContext](
        name=QUOTA_AGENT_NAME,
        instructions=QUOTA_AGENT_INSTRUCTIONS,
        model=DeterministicQuotaModel(call_id=call_id),
        tools=[build_grant_temporary_quota_tool()],
    )


def quota_definition_digest(agent: Agent[Any]) -> str:
    if not isinstance(agent.instructions, str):
        raise TypeError("The quota Agent requires static instructions")
    tools: list[dict[str, Any]] = []
    for tool in agent.tools:
        if not isinstance(tool, FunctionTool):
            raise TypeError("The quota Agent requires function tools only")
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
                "QuotaGrantRequest": QuotaGrantRequest.model_json_schema(),
                "QuotaGrantResult": QuotaGrantResult.model_json_schema(),
            },
            "tools": tools,
        }
    )
