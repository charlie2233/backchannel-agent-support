"""Pinned OpenAI Responses models that retain only safe response provenance."""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Literal, cast, overload

from agents.agent_output import AgentOutputSchemaBase
from agents.handoffs import Handoff
from agents.items import ModelResponse, TResponseInputItem
from agents.model_settings import ModelSettings
from agents.models.interface import Model, ModelProvider, ModelTracing
from agents.models.openai_responses import OpenAIResponsesModel
from agents.tool import Tool
from agents.tracing import SpanError, response_span
from agents.usage import Usage, _response_usage_to_usage, model_usage_to_span_usage
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    BadRequestError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    UnprocessableEntityError,
)
from openai.types.responses import Response, ResponseStreamEvent
from openai.types.responses.response_prompt_param import ResponsePromptParam

LIVE_CONSUMER_MODEL = "gpt-5.6-luna"
LIVE_PROVIDER_MODEL = "gpt-5.6-luna"
LIVE_BROKER_MODEL = "gpt-5.6-terra"
LIVE_MODEL_ALIASES = (
    LIVE_CONSUMER_MODEL,
    LIVE_PROVIDER_MODEL,
    LIVE_BROKER_MODEL,
)
_ALLOWED_MODEL_ALIASES = frozenset(LIVE_MODEL_ALIASES)
_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,191}\Z")
_SAFE_LOGGER = logging.getLogger(__name__)
LiveModelErrorCode = Literal[
    "invalid_api_key",
    "rate_limit_exceeded",
    "model_not_found",
    "authentication_failed",
    "permission_denied",
    "rate_limited",
    "model_unavailable",
    "timeout",
    "connection_error",
    "request_rejected",
    "provider_http_error",
    "external_error",
]
LIVE_MODEL_ERROR_CODES: frozenset[str] = frozenset(
    {
        "invalid_api_key",
        "rate_limit_exceeded",
        "model_not_found",
        "authentication_failed",
        "permission_denied",
        "rate_limited",
        "model_unavailable",
        "timeout",
        "connection_error",
        "request_rejected",
        "provider_http_error",
        "external_error",
    }
)
_PASSTHROUGH_ERROR_CODES: dict[str, LiveModelErrorCode] = {
    "invalid_api_key": "invalid_api_key",
    "rate_limit_exceeded": "rate_limit_exceeded",
    "model_not_found": "model_not_found",
}


def _safe_identifier(value: object, *, label: str, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or _SAFE_IDENTIFIER.fullmatch(value) is None:
        raise RuntimeError(f"OpenAI live response has an unsafe {label}")
    return value


class LiveModelRequestError(RuntimeError):
    """App-owned provider failure that can cross outer logging boundaries safely."""

    def __init__(self, code: LiveModelErrorCode) -> None:
        self.code = code
        super().__init__(code)


def _live_model_error_code(error: Exception) -> LiveModelErrorCode:
    raw_code = getattr(error, "code", None)
    if isinstance(raw_code, str) and raw_code in _PASSTHROUGH_ERROR_CODES:
        return _PASSTHROUGH_ERROR_CODES[raw_code]
    if isinstance(error, AuthenticationError):
        return "authentication_failed"
    if isinstance(error, PermissionDeniedError):
        return "permission_denied"
    if isinstance(error, RateLimitError):
        return "rate_limited"
    if isinstance(error, NotFoundError):
        return "model_unavailable"
    if isinstance(error, APITimeoutError):
        return "timeout"
    if isinstance(error, APIConnectionError):
        return "connection_error"
    if isinstance(error, BadRequestError | UnprocessableEntityError):
        return "request_rejected"
    if isinstance(error, APIStatusError):
        return "provider_http_error"
    return "external_error"


@dataclass(frozen=True, slots=True)
class ModelResponseMetadata:
    """Allowlisted response identifiers; never model input or output payloads."""

    requested_model: str
    returned_model: str
    response_id: str
    request_id: str | None

    def __post_init__(self) -> None:
        if self.requested_model not in _ALLOWED_MODEL_ALIASES:
            raise ValueError("Metadata requested model is not an explicit live alias")
        _safe_identifier(self.returned_model, label="returned model")
        _safe_identifier(self.response_id, label="response ID")
        _safe_identifier(self.request_id, label="request ID", optional=True)

    def to_json_value(self) -> dict[str, str | None]:
        return {
            "requestedModel": self.requested_model,
            "returnedModel": self.returned_model,
            "responseId": self.response_id,
            "requestId": self.request_id,
        }

    @classmethod
    def from_json_value(cls, value: object) -> ModelResponseMetadata:
        if not isinstance(value, dict) or set(value) != {
            "requestedModel",
            "returnedModel",
            "responseId",
            "requestId",
        }:
            raise ValueError("Stored model metadata has an incompatible shape")
        return cls(
            requested_model=cast(str, value["requestedModel"]),
            returned_model=cast(str, value["returnedModel"]),
            response_id=cast(str, value["responseId"]),
            request_id=cast(str | None, value["requestId"]),
        )


class ResponseMetadataRecorder:
    """Append-only safe provenance recorder with an optional durable callback."""

    def __init__(
        self,
        initial: tuple[ModelResponseMetadata, ...] = (),
        *,
        on_record: Callable[[tuple[ModelResponseMetadata, ...]], None] | None = None,
    ) -> None:
        self._records = list(initial)
        self._on_record = on_record

    def record(self, metadata: ModelResponseMetadata) -> None:
        self._records.append(metadata)
        if self._on_record is not None:
            self._on_record(self.snapshot())

    def snapshot(self) -> tuple[ModelResponseMetadata, ...]:
        return tuple(self._records)

    def public_model_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.returned_model for item in self._records))

    def public_summary(self) -> list[dict[str, str | None]]:
        return [item.to_json_value() for item in self._records]


class SafeOpenAIResponsesModel(OpenAIResponsesModel):
    """Capture returned model/response IDs at the only raw Response seam."""

    def __init__(
        self,
        model: str,
        openai_client: AsyncOpenAI,
        *,
        recorder: ResponseMetadataRecorder,
    ) -> None:
        if model not in _ALLOWED_MODEL_ALIASES:
            raise ValueError("OpenAI live model must be an explicit live model alias")
        super().__init__(model, openai_client, model_is_explicit=True)
        self._metadata_recorder = recorder

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        previous_response_id: str | None = None,
        conversation_id: str | None = None,
        prompt: ResponsePromptParam | None = None,
    ) -> ModelResponse:
        """Mirror the pinned nonstream path without ever logging exception payloads."""

        with response_span(disabled=tracing.is_disabled()) as span_response:
            try:
                response = await self._fetch_response(
                    system_instructions,
                    input,
                    model_settings,
                    tools,
                    output_schema,
                    handoffs,
                    previous_response_id=previous_response_id,
                    conversation_id=conversation_id,
                    stream=False,
                    prompt=prompt,
                )
                usage = (
                    _response_usage_to_usage(response.usage)
                    if response.usage
                    else Usage()
                )
                if response.usage:
                    span_response.span_data.usage = model_usage_to_span_usage(usage)
                _SAFE_LOGGER.debug("OpenAI model response completed")
            except Exception as error:
                error_code = _live_model_error_code(error)
                span_response.set_error(
                    SpanError(
                        message="Error getting response",
                        data={
                            "errorClass": "LiveModelRequestError",
                            "errorCode": error_code,
                        },
                    )
                )
                _SAFE_LOGGER.error(
                    "OpenAI response failed error_class=LiveModelRequestError "
                    "error_code=%s",
                    error_code,
                )
                raise LiveModelRequestError(error_code) from None

        return ModelResponse(
            output=response.output,
            usage=usage,
            response_id=response.id,
            request_id=getattr(response, "_request_id", None),
        )

    @overload
    async def _fetch_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        previous_response_id: str | None,
        conversation_id: str | None,
        stream: Literal[True],
        prompt: ResponsePromptParam | None = None,
    ) -> AsyncIterator[ResponseStreamEvent]: ...

    @overload
    async def _fetch_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        previous_response_id: str | None,
        conversation_id: str | None,
        stream: Literal[False],
        prompt: ResponsePromptParam | None = None,
    ) -> Response: ...

    async def _fetch_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        previous_response_id: str | None = None,
        conversation_id: str | None = None,
        stream: Literal[True] | Literal[False] = False,
        prompt: ResponsePromptParam | None = None,
    ) -> Response | AsyncIterator[ResponseStreamEvent]:
        if stream:
            raise RuntimeError("Backchannel live validation disables streaming model calls")
        raw = await super()._fetch_response(
            system_instructions=system_instructions,
            input=input,
            model_settings=model_settings,
            tools=tools,
            output_schema=output_schema,
            handoffs=handoffs,
            previous_response_id=previous_response_id,
            conversation_id=conversation_id,
            stream=False,
            prompt=prompt,
        )
        if not isinstance(raw, Response):
            raise RuntimeError("Backchannel live validation requires a non-streaming Response")
        requested_model = self.model
        request_id = getattr(raw, "_request_id", None)
        returned_model = _safe_identifier(raw.model, label="returned model")
        response_id = _safe_identifier(raw.id, label="response ID")
        safe_request_id = _safe_identifier(
            request_id,
            label="request ID",
            optional=True,
        )
        if returned_model is None or response_id is None:
            raise RuntimeError("OpenAI live response identifiers are missing")
        self._metadata_recorder.record(
            ModelResponseMetadata(
                requested_model=requested_model,
                returned_model=returned_model,
                response_id=response_id,
                request_id=safe_request_id,
            )
        )
        return raw


class SafeOpenAIResponsesProvider(ModelProvider):
    """Resolve only the three explicit Agent model aliases for this live graph."""

    def __init__(
        self,
        *,
        client: AsyncOpenAI,
        recorder: ResponseMetadataRecorder,
    ) -> None:
        self._client = client
        self._recorder = recorder
        self._models: dict[str, SafeOpenAIResponsesModel] = {}

    def get_model(self, model_name: str | None) -> Model:
        if model_name not in _ALLOWED_MODEL_ALIASES:
            raise ValueError("OpenAI live Agent requires an explicit live model alias")
        model_key = model_name
        if model_key not in self._models:
            self._models[model_key] = SafeOpenAIResponsesModel(
                model_key,
                self._client,
                recorder=self._recorder,
            )
        return self._models[model_key]
