"""Deterministic implementation of the installed Agents SDK Model interface."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping

from agents.agent_output import AgentOutputSchemaBase
from agents.handoffs import Handoff
from agents.items import (
    ModelResponse,
    TResponseInputItem,
    TResponseStreamEvent,
)
from agents.model_settings import ModelSettings
from agents.models.interface import Model, ModelTracing
from agents.tool import Tool
from agents.usage import Usage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)
from openai.types.responses.response_prompt_param import ResponsePromptParam

from server.agents.schemas import CommitRemedyArguments

EXACT_REMEDY_REJECTION_MESSAGE = (
    "The user declined this exact remedy. Do not execute it or select an alternative."
)
DECLINED_REMEDY_CLOSURE = (
    "The exact remedy was declined. No provider action was taken and no alternative "
    "remedy was selected."
)


class DeterministicApprovalModel(Model):
    """Emit one approval-gated tool call, then a terminal assistant message."""

    def __init__(self, *, arguments: CommitRemedyArguments, call_id: str) -> None:
        self._arguments = arguments
        self._call_id = call_id

    def _matching_function_output(
        self, model_input: str | list[TResponseInputItem]
    ) -> str | None:
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
            if item_type == "function_call_output" and call_id == self._call_id:
                if not isinstance(output, str):
                    raise RuntimeError("Deterministic tool output must be text")
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
        matching_output = self._matching_function_output(input)
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
        if [tool.name for tool in tools] != ["commit_remedy"]:
            raise RuntimeError("Deterministic model requires exactly the commit_remedy tool")

        if matching_output is None:
            return ModelResponse(
                output=[
                    ResponseFunctionToolCall(
                        type="function_call",
                        name="commit_remedy",
                        call_id=self._call_id,
                        arguments=self._arguments.model_dump_json(),
                    )
                ],
                usage=Usage(),
                response_id=f"stub-response-{self._call_id}-1",
            )

        terminal_text = (
            DECLINED_REMEDY_CLOSURE
            if matching_output == EXACT_REMEDY_REJECTION_MESSAGE
            else "Deterministic demo-provider recovery completed."
        )
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
                            text=terminal_text,
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
        raise NotImplementedError("Deterministic approval smoke uses non-streaming Runner.run")
