"""Deterministic implementation of the installed Agents SDK Model interface."""

from __future__ import annotations

from collections.abc import AsyncIterator

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


class DeterministicApprovalModel(Model):
    """Emit one approval-gated tool call, then a terminal assistant message."""

    def __init__(self, *, arguments: CommitRemedyArguments, call_id: str) -> None:
        self._arguments = arguments
        self._call_id = call_id
        self._response_number = 0

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

        self._response_number += 1
        if self._response_number == 1:
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
                            text="Deterministic demo-provider recovery completed.",
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
