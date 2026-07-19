"""Typed public and replay-fixture contracts for Backchannel."""

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)


class ApiModel(BaseModel):
    """Base model that accepts Python names and emits explicit API aliases."""

    model_config = ConfigDict(populate_by_name=True)


class ExecutionMode(StrEnum):
    OPENAI_LIVE = "openai_live"
    SDK_STUB = "sdk_stub"
    REPLAY_FIXTURE = "replay_fixture"


class RuntimeBackend(StrEnum):
    OPENAI = "openai"
    STUB = "stub"


class ProviderBoundary(StrEnum):
    DEMO_ADAPTER_ONLY = "demo_adapter_only"


class ScenarioId(StrEnum):
    HOTEL = "hotel"
    API_QUOTA = "api-quota"


class RecoveryStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    PENDING_APPROVAL = "pending_approval"
    COMPLETED = "completed"


class HealthResponse(ApiModel):

    backend: RuntimeBackend
    live_ready: bool = Field(alias="liveReady")
    provider_boundary: ProviderBoundary = Field(alias="providerBoundary")


class ReadinessResponse(ApiModel):
    status: str


class ReplayEventTemplate(ApiModel):
    type: str
    status: RecoveryStatus
    current_step: int = Field(alias="currentStep", ge=0, le=5)
    summary: str
    data: dict[str, JsonValue]


class ReplayReceiptTemplate(ApiModel):
    status: str
    simulated: Literal[True]
    provider_execution: Literal[False] = Field(alias="providerExecution")
    model_ids: list[str] = Field(alias="modelIds", max_length=0)
    boundary: str
    provider_result: str = Field(alias="providerResult")
    authorization_source: str = Field(alias="authorizationSource")
    verification_results: list[str] = Field(alias="verificationResults")


class ReplayScenarioDefinition(ApiModel):
    id: ScenarioId
    title: str
    summary: str
    execution_mode: Literal[ExecutionMode.REPLAY_FIXTURE] = Field(alias="executionMode")
    initial_step: int = Field(alias="initialStep", ge=0, le=5)
    initial_summary: str = Field(alias="initialSummary")
    events: list[ReplayEventTemplate] = Field(min_length=1)
    receipt: ReplayReceiptTemplate | None


class ScenarioResponse(ApiModel):
    id: ScenarioId
    title: str
    summary: str
    execution_mode: ExecutionMode = Field(alias="executionMode")


class CreateRecoveryRequest(ApiModel):
    scenario_id: ScenarioId = Field(alias="scenarioId")
    execution_mode: ExecutionMode = Field(alias="executionMode")


class HotelReplacementTerms(ApiModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)

    from_room_type: str = Field(alias="fromRoomType")
    to_room_type: str = Field(alias="toRoomType")


class HotelStayTerms(ApiModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)

    check_in: str = Field(alias="checkIn")
    check_out: str = Field(alias="checkOut")


class HotelRemedyTerms(ApiModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)

    booking_id: str = Field(alias="bookingId")
    action: Literal["replace_room"]
    replacement: HotelReplacementTerms
    stay: HotelStayTerms
    currency: Literal["USD"]


class PendingApprovalView(ApiModel):
    """Strict public consent record; opaque SDK state never enters this model."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)

    remedy_id: str = Field(alias="remedyId")
    remedy_digest: str = Field(
        alias="remedyDigest",
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    terms: HotelRemedyTerms
    cost_delta_minor: int = Field(alias="costDeltaMinor", strict=True)
    changed_fields: list[str] = Field(alias="changedFields", min_length=1)
    provider_commitments: list[str] = Field(
        alias="providerCommitments", min_length=1
    )
    expiry: datetime
    hard_constraint_satisfied: bool = Field(alias="hardConstraintSatisfied")
    delegated_authority_satisfied: bool = Field(
        alias="delegatedAuthoritySatisfied"
    )
    tool_call_id: str = Field(alias="toolCallId")
    execution_started: Literal[False] = Field(alias="executionStarted")

    @field_validator("changed_fields", "provider_commitments")
    @classmethod
    def require_canonical_set_order(cls, value: list[str]) -> list[str]:
        if value != sorted(value):
            raise ValueError("Set-like consent fields must use canonical sorted order")
        return value

    @field_validator("expiry")
    @classmethod
    def require_utc_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("Consent expiry must be timezone-aware UTC")
        return value


class ApprovalDecisionRequest(ApiModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)

    client_decision_id: str = Field(alias="clientDecisionId", min_length=1, max_length=128)
    remedy_id: str = Field(alias="remedyId", min_length=1)
    remedy_digest: str = Field(
        alias="remedyDigest",
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    tool_call_id: str = Field(alias="toolCallId", min_length=1)


class ApprovalDecisionResponse(ApiModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)

    client_decision_id: str = Field(alias="clientDecisionId")
    recovery_id: str = Field(alias="recoveryId")
    status: Literal["completed"]
    approved_remedy_digest: str = Field(
        alias="approvedRemedyDigest",
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    execution_started: Literal[True] = Field(alias="executionStarted")


class RecoverySnapshot(ApiModel):
    recovery_id: str = Field(alias="recoveryId")
    scenario_id: ScenarioId = Field(alias="scenarioId")
    execution_mode: ExecutionMode = Field(alias="executionMode")
    status: RecoveryStatus
    current_step: int = Field(alias="currentStep", ge=0, le=5)
    current_step_summary: str = Field(alias="currentStepSummary")
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")
    pending_approval: PendingApprovalView | None = Field(
        default=None, alias="pendingApproval"
    )


class RecoveryEvent(ApiModel):
    recovery_id: str = Field(alias="recoveryId")
    seq: int = Field(ge=1)
    type: str
    terminal: bool
    data: dict[str, JsonValue]
    created_at: datetime = Field(alias="createdAt")


class RecoveryReceipt(ApiModel):
    recovery_id: str = Field(alias="recoveryId")
    execution_mode: ExecutionMode = Field(alias="executionMode")
    status: str
    simulated: bool
    provider_execution: bool = Field(alias="providerExecution")
    model_ids: list[str] = Field(alias="modelIds")
    boundary: str
    provider_result: str = Field(alias="providerResult")
    authorization_source: str = Field(alias="authorizationSource")
    verification_results: list[str] = Field(alias="verificationResults")
    approved_remedy_digest: str | None = Field(
        default=None,
        alias="approvedRemedyDigest",
        pattern=r"^sha256:[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def enforce_execution_mode_provenance(self) -> Self:
        if self.execution_mode is ExecutionMode.REPLAY_FIXTURE and (
            not self.simulated or self.provider_execution or self.model_ids
        ):
            raise ValueError(
                "Replay receipts require simulated evidence, no provider execution, "
                "and no model IDs"
            )
        if self.execution_mode is ExecutionMode.SDK_STUB and (
            not self.simulated or self.model_ids
        ):
            raise ValueError("SDK stub receipts require simulated evidence and no model IDs")
        return self


class DemoResetResponse(ApiModel):
    reset: Literal[True]
