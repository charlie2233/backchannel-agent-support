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

    model_config = ConfigDict(populate_by_name=True, extra="forbid")


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
    CLOSED_WITHOUT_ACTION = "closed_without_action"
    OUTCOME_UNKNOWN = "outcome_unknown"


class HealthResponse(ApiModel):

    backend: RuntimeBackend
    live_ready: bool = Field(alias="liveReady")
    sdk_stub_ready: bool = Field(alias="sdkStubReady")
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
    status: Literal["completed"]
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
    model_config = ConfigDict(populate_by_name=False, extra="forbid", frozen=True)

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
    model_config = ConfigDict(populate_by_name=False, extra="forbid", frozen=True)

    decision: Literal["approve", "decline"]
    client_decision_id: str = Field(
        alias="clientDecisionId",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    remedy_id: str = Field(
        alias="remedyId",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    remedy_digest: str = Field(
        alias="remedyDigest",
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    tool_call_id: str = Field(
        alias="toolCallId",
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )


class ApprovalDecisionResponse(ApiModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)

    client_decision_id: str = Field(alias="clientDecisionId")
    recovery_id: str = Field(alias="recoveryId")
    decision: Literal["approve"]
    status: Literal["completed"]
    approved_remedy_digest: str = Field(
        alias="approvedRemedyDigest",
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    execution_started: Literal[True] = Field(alias="executionStarted")


class DeclineDecisionResponse(ApiModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)

    client_decision_id: str = Field(alias="clientDecisionId")
    recovery_id: str = Field(alias="recoveryId")
    decision: Literal["decline"]
    status: Literal["closed_without_action", "outcome_unknown"]
    decision_remedy_digest: str = Field(
        alias="decisionRemedyDigest",
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    execution_started: bool = Field(alias="executionStarted")


DecisionResponse = ApprovalDecisionResponse | DeclineDecisionResponse


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
    root_trace_id: str | None = Field(default=None, alias="rootTraceId")
    model_ids: list[str] = Field(default_factory=list, alias="modelIds")
    sdk_version: str | None = Field(default=None, alias="sdkVersion")
    protocol_version: str | None = Field(default=None, alias="protocolVersion")
    agent_graph_version: str | None = Field(default=None, alias="agentGraphVersion")
    prompt_tool_schema_hash: str | None = Field(
        default=None,
        alias="promptToolSchemaHash",
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def enforce_snapshot_provenance(self) -> Self:
        if self.execution_mode is ExecutionMode.OPENAI_LIVE:
            if (
                self.status is RecoveryStatus.IN_PROGRESS
                and self.root_trace_id is None
                and not self.model_ids
            ):
                return self
            if (
                self.root_trace_id is None
                or not self.root_trace_id.startswith("trace_")
                or not self.model_ids
                or self.sdk_version is None
                or self.protocol_version is None
                or self.agent_graph_version is None
                or self.prompt_tool_schema_hash is None
            ):
                raise ValueError("OpenAI live snapshots require complete live provenance")
        elif self.model_ids:
            raise ValueError("Stub and replay snapshots cannot claim returned model IDs")
        return self


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
    root_trace_id: str | None = Field(default=None, alias="rootTraceId")
    sdk_version: str | None = Field(default=None, alias="sdkVersion")
    protocol_version: str | None = Field(default=None, alias="protocolVersion")
    agent_graph_version: str | None = Field(default=None, alias="agentGraphVersion")
    prompt_tool_schema_hash: str | None = Field(
        default=None,
        alias="promptToolSchemaHash",
        pattern=r"^[0-9a-f]{64}$",
    )
    boundary: str
    provider_result: str = Field(alias="providerResult")
    authorization_source: str = Field(alias="authorizationSource")
    verification_results: list[str] = Field(alias="verificationResults")
    decision: Literal["approved", "declined"] | None = None
    decision_remedy_digest: str | None = Field(
        default=None,
        alias="decisionRemedyDigest",
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    execution_count: int = Field(default=0, alias="executionCount", ge=0)
    provider_dispatch_started: bool = Field(
        default=False,
        alias="providerDispatchStarted",
    )
    exact_interruption_rejected: bool = Field(
        default=False,
        alias="exactInterruptionRejected",
    )
    permission_revoked: bool = Field(default=False, alias="permissionRevoked")
    scope_closed: bool = Field(default=False, alias="scopeClosed")
    approved_remedy_digest: str | None = Field(
        default=None,
        alias="approvedRemedyDigest",
        pattern=r"^sha256:[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def enforce_execution_mode_provenance(self) -> Self:
        if self.execution_mode is ExecutionMode.REPLAY_FIXTURE and (
            self.status != RecoveryStatus.COMPLETED.value
            or not self.simulated
            or self.provider_execution
            or self.model_ids
            or self.root_trace_id is not None
            or self.sdk_version is not None
            or self.protocol_version is not None
            or self.agent_graph_version is not None
            or self.prompt_tool_schema_hash is not None
            or self.decision is not None
            or self.decision_remedy_digest is not None
            or self.execution_count != 0
            or self.provider_dispatch_started
            or self.exact_interruption_rejected
            or self.permission_revoked
            or self.scope_closed
            or self.approved_remedy_digest is not None
        ):
            raise ValueError(
                "Replay receipts require completed simulated evidence with no model, "
                "provider, decision, or permission-scope claims"
            )
        if self.execution_mode is ExecutionMode.OPENAI_LIVE:
            if (
                not self.simulated
                or not self.model_ids
                or self.root_trace_id is None
                or not self.root_trace_id.startswith("trace_")
                or self.sdk_version is None
                or self.protocol_version is None
                or self.agent_graph_version is None
                or self.prompt_tool_schema_hash is None
                or self.decision is None
                or self.decision_remedy_digest is None
                or not self.permission_revoked
                or not self.scope_closed
            ):
                raise ValueError(
                    "OpenAI live terminal receipts require model and closed-scope provenance"
                )
            if self.decision == "approved":
                if (
                    self.status != RecoveryStatus.COMPLETED.value
                    or not self.provider_execution
                    or self.execution_count != 1
                    or not self.provider_dispatch_started
                    or self.exact_interruption_rejected
                    or self.approved_remedy_digest != self.decision_remedy_digest
                ):
                    raise ValueError(
                        "Approved live receipts require one demo-provider dispatch and digest"
                    )
            elif self.status == RecoveryStatus.CLOSED_WITHOUT_ACTION.value:
                if (
                    self.provider_execution
                    or self.execution_count != 0
                    or self.provider_dispatch_started
                    or not self.exact_interruption_rejected
                    or self.approved_remedy_digest is not None
                ):
                    raise ValueError(
                        "Closed live receipts require exact rejection and zero dispatch"
                    )
            elif self.status == RecoveryStatus.OUTCOME_UNKNOWN.value:
                if (
                    self.execution_count < 1
                    or not self.provider_dispatch_started
                    or not self.exact_interruption_rejected
                    or self.approved_remedy_digest is not None
                ):
                    raise ValueError(
                        "Outcome-unknown live receipts require dispatch evidence"
                    )
            else:
                raise ValueError("Declined live receipt has an invalid terminal status")
        if self.execution_mode is ExecutionMode.SDK_STUB:
            if not self.simulated or self.model_ids:
                raise ValueError(
                    "SDK stub receipts require simulated evidence and no model IDs"
                )
            if (
                self.decision is None
                or self.decision_remedy_digest is None
                or not self.permission_revoked
                or not self.scope_closed
            ):
                raise ValueError(
                    "SDK stub terminal receipts require a decision-bound closed scope"
                )
            if self.decision == "approved":
                if (
                    self.status != RecoveryStatus.COMPLETED.value
                    or not self.provider_execution
                    or self.execution_count != 1
                    or not self.provider_dispatch_started
                    or self.exact_interruption_rejected
                    or self.approved_remedy_digest != self.decision_remedy_digest
                ):
                    raise ValueError(
                        "Approved SDK receipts require one dispatched execution and "
                        "matching approved digest"
                    )
            elif self.status == RecoveryStatus.CLOSED_WITHOUT_ACTION.value:
                if (
                    self.provider_execution
                    or self.execution_count != 0
                    or self.provider_dispatch_started
                    or not self.exact_interruption_rejected
                    or self.approved_remedy_digest is not None
                ):
                    raise ValueError(
                        "Closed-without-action receipts require exact rejection and "
                        "zero dispatch evidence"
                    )
            elif self.status == RecoveryStatus.OUTCOME_UNKNOWN.value:
                if (
                    self.execution_count < 1
                    or not self.provider_dispatch_started
                    or not self.exact_interruption_rejected
                    or self.approved_remedy_digest is not None
                ):
                    raise ValueError(
                        "Outcome-unknown decline receipts require execution or dispatch "
                        "evidence and exact rejection"
                    )
            else:
                raise ValueError("Declined SDK receipt has an invalid terminal status")
        return self


class DemoResetResponse(ApiModel):
    reset: Literal[True]
