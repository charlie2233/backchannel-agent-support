import asyncio
from types import SimpleNamespace
from typing import cast

from agents import function_tool
from agents.items import TResponseInputItem
from agents.model_settings import ModelSettings
from agents.models.interface import ModelTracing
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage

from server.agents.schemas import deterministic_hotel_arguments
from server.agents.stub_model import DeterministicApprovalModel

CALL_ID = "commit-remedy-focused-model-test"


@function_tool
def commit_remedy() -> str:
    """Focused model test tool."""

    return "unused"


def get_response(
    model: DeterministicApprovalModel,
    model_input: list[TResponseInputItem],
):
    return asyncio.run(
        model.get_response(
            system_instructions=None,
            input=model_input,
            model_settings=ModelSettings(),
            tools=[commit_remedy],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        )
    )


def make_model() -> DeterministicApprovalModel:
    return DeterministicApprovalModel(
        arguments=deterministic_hotel_arguments(),
        call_id=CALL_ID,
    )


def test_model_finishes_when_matching_function_output_is_in_mapping_history() -> None:
    response = get_response(
        make_model(),
        cast(
            list[TResponseInputItem],
            [
                {"role": "user", "content": "run"},
                {
                    "type": "function_call_output",
                    "call_id": CALL_ID,
                    "output": "{}",
                },
            ],
        ),
    )

    assert isinstance(response.output[0], ResponseOutputMessage)


def test_model_finishes_when_matching_function_output_is_an_object() -> None:
    response = get_response(
        make_model(),
        cast(
            list[TResponseInputItem],
            [SimpleNamespace(type="function_call_output", call_id=CALL_ID, output="{}")],
        ),
    )

    assert isinstance(response.output[0], ResponseOutputMessage)


def test_model_requests_commit_when_matching_output_is_absent() -> None:
    response = get_response(
        make_model(),
        cast(
            list[TResponseInputItem],
            [
                {
                    "type": "function_call_output",
                    "call_id": "some-other-call",
                    "output": "{}",
                }
            ],
        ),
    )

    assert isinstance(response.output[0], ResponseFunctionToolCall)
