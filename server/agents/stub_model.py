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
from server.providers.hotel_simulator import HotelDispatchResult

DECLINE_MESSAGE = (
    "The operator declined this exact remedy. Close without action and do not "
    "select a replacement."
)
CLOSED_WITHOUT_ACTION_MESSAGE = (
    "Closed without action. The exact remedy was declined, and no replacement "
    "action was selected."
)
_NO_MATCHING_OUTPUT = object()


class DeterministicApprovalModel(Model):
    """Emit one approval-gated tool call, then a terminal assistant message."""

    def __init__(self, *, arguments: CommitRemedyArguments, call_id: str) -> None:
        self._arguments = arguments
        self._call_id = call_id

    def _matching_function_output(
        self, model_input: str | list[TResponseInputItem]
    ) -> object:
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
        function_output = self._matching_function_output(input)
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

        if function_output is _NO_MATCHING_OUTPUT:
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

        if function_output == DECLINE_MESSAGE:
            terminal_text = CLOSED_WITHOUT_ACTION_MESSAGE
        else:
            if not isinstance(function_output, str):
                raise RuntimeError(
                    "Deterministic commit_remedy output must be a string"
                )
            try:
                HotelDispatchResult.model_validate_json(function_output)
            except ValueError:
                raise RuntimeError(
                    "Deterministic commit_remedy output was neither an approved "
                    "provider result nor the exact decline message"
                ) from None
            terminal_text = "Deterministic demo-provider recovery completed."

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
