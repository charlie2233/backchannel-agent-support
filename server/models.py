"""Public API models shared by the initial runtime endpoints."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ExecutionMode(StrEnum):
    OPENAI_LIVE = "openai_live"
    SDK_STUB = "sdk_stub"
    REPLAY_FIXTURE = "replay_fixture"


class RuntimeBackend(StrEnum):
    OPENAI = "openai"
    STUB = "stub"


class ProviderBoundary(StrEnum):
    DEMO_ADAPTER_ONLY = "demo_adapter_only"


class HealthResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    backend: RuntimeBackend
    live_ready: bool = Field(alias="liveReady")
    provider_boundary: ProviderBoundary = Field(alias="providerBoundary")


class ReadinessResponse(BaseModel):
    status: str
