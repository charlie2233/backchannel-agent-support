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

from server.trace_ids import is_valid_live_trace_id, is_valid_qa_trace_id


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


SDK_STUB_BOUNDARY = (
    "Deterministic Agents SDK model and demo hotel adapter only; "
    "no OpenAI model call, real booking, or payment change."
)
QUOTA_SDK_STUB_BOUNDARY = (
    "Deterministic Agents SDK stub and demo quota adapter only; "
    "no OpenAI model call or real quota change."
)
QUOTA_SDK_PROVIDER_RESULT = (
    "Demo quota adapter verified 1200 units against a temporary "
    "1250-unit US-region ceiling; no real quota was changed."
)
QUOTA_SDK_AUTHORIZATION_SOURCE = (
    "Predelegated API quota policy: US-only, at most 500 USD minor "
    "units, for at most 900 seconds."
)
QUOTA_SDK_VERIFICATION_RESULTS = (
    "Provider proved the baseline quota ceiling at 1000 units.",
    "Temporary US-region burst granted: 250 units for 900 seconds.",
    "All hard constraints remained satisfied.",
    "Extra cost of 300 USD minor units stayed within the delegated 500-unit limit.",
    "Approval count is zero; no human interruption was created.",
    "Execution verified at an effective ceiling of 1250 units.",
    "Temporary quota permission revoked; baseline ceiling restored to 1000 units.",
)
OPENAI_LIVE_BOUNDARY = (
    "OpenAI agent model calls and demo hotel adapter only; no real booking or payment change."
)


class ScenarioId(StrEnum):
    HOTEL = "hotel"
    API_QUOTA = "api-quota"


class DecisionAction(StrEnum):
    APPROVE = "approve"
    DECLINE = "decline"


class RecoveryStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    PENDING_APPROVAL = "pending_approval"
    COMPLETED = "completed"
    CLOSED_WITHOUT_ACTION = "closed_without_action"
    OUTCOME_UNKNOWN = "outcome_unknown"

    @property
    def terminal(self) -> bool:
        return self in {
            RecoveryStatus.COMPLETED,
            RecoveryStatus.CLOSED_WITHOUT_ACTION,
            RecoveryStatus.OUTCOME_UNKNOWN,
        }


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
    status: Literal["simulated_completed"]
    simulated: Literal[True]
    provider_execution: Literal[False] = Field(alias="providerExecution")
    model_ids: list[str] = Field(alias="modelIds", max_length=0)
    boundary: str
    provider_result: str = Field(alias="providerResult")
    authorization_source: str = Field(alias="authorizationSource")
    verification_results: list[str] = Field(alias="verificationResults")
    approval_count: Literal[0] = Field(alias="approvalCount")


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
    provider_commitments: list[str] = Field(alias="providerCommitments", min_length=1)
    expiry: datetime
    hard_constraint_satisfied: bool = Field(alias="hardConstraintSatisfied")
    delegated_authority_satisfied: bool = Field(alias="delegatedAuthoritySatisfied")
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

    action: DecisionAction
    client_decision_id: str = Field(alias="clientDecisionId", min_length=1, max_length=128)
    remedy_id: str = Field(alias="remedyId", min_length=1, max_length=128)
    remedy_digest: str = Field(
        alias="remedyDigest",
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    tool_call_id: str = Field(alias="toolCallId", min_length=1, max_length=256)


class ApprovalDecisionResponse(ApiModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)

    action: DecisionAction
    client_decision_id: str = Field(alias="clientDecisionId")
    recovery_id: str = Field(alias="recoveryId")
    status: Literal["completed", "closed_without_action", "outcome_unknown"]
    approved_remedy_digest: str | None = Field(
        default=None,
        alias="approvedRemedyDigest",
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    execution_started: bool | None = Field(alias="executionStarted")

    @model_validator(mode="after")
    def enforce_action_outcome(self) -> Self:
        if self.action is DecisionAction.APPROVE:
            if (
                self.status != "completed"
                or not self.execution_started
                or self.approved_remedy_digest is None
            ):
                raise ValueError("Approve decisions require completed execution evidence")
            return self
        if self.status == "completed" or self.approved_remedy_digest is not None:
            raise ValueError("Decline decisions cannot claim approved execution evidence")
        if self.status == "closed_without_action" and self.execution_started is not False:
            raise ValueError("Closed declines require zero execution evidence")
        if self.status == "outcome_unknown" and self.execution_started is not None:
            raise ValueError("Unknown outcomes cannot claim whether execution started")
        return self


class DecisionResumeRequest(ApiModel):
    """An explicit resume signal with no client-authored decision fields."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)


class DecisionResumeResponse(ApiModel):
    """Minimal exact-claim result returned to a browser that lacks the claim token."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)

    action: DecisionAction
    recovery_id: str = Field(alias="recoveryId")
    remedy_digest: str = Field(
        alias="remedyDigest",
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    status: Literal["completed", "closed_without_action", "outcome_unknown"]
    approved_remedy_digest: str | None = Field(
        default=None,
        alias="approvedRemedyDigest",
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    execution_started: bool | None = Field(alias="executionStarted")

    @classmethod
    def from_decision(
        cls,
        response: ApprovalDecisionResponse,
        *,
        remedy_digest: str,
    ) -> Self:
        return cls(
            action=response.action,
            recoveryId=response.recovery_id,
            remedyDigest=remedy_digest,
            status=response.status,
            approvedRemedyDigest=response.approved_remedy_digest,
            executionStarted=response.execution_started,
        )

    @model_validator(mode="after")
    def enforce_action_outcome(self) -> Self:
        if self.action is DecisionAction.APPROVE:
            if (
                self.status != "completed"
                or self.execution_started is not True
                or self.approved_remedy_digest != self.remedy_digest
            ):
                raise ValueError("Resumed approvals require exact completed evidence")
            return self
        if self.status == "completed" or self.approved_remedy_digest is not None:
            raise ValueError("Resumed declines cannot claim approved execution evidence")
        if self.status == "closed_without_action" and self.execution_started is not False:
            raise ValueError("Closed resumed declines require zero execution evidence")
        if self.status == "outcome_unknown" and self.execution_started is not None:
            raise ValueError("Unknown resumed outcomes cannot claim execution state")
        return self


class ClaimedDecisionView(ApiModel):
    """Minimal public evidence for one unfinished durable decision claim."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)

    action: DecisionAction
    remedy_digest: str = Field(
        alias="remedyDigest",
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    expiry: datetime

    @field_validator("expiry")
    @classmethod
    def require_utc_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("Claimed decision expiry must be timezone-aware UTC")
        return value


class RecoverySnapshot(ApiModel):
    recovery_id: str = Field(alias="recoveryId")
    scenario_id: ScenarioId = Field(alias="scenarioId")
    execution_mode: ExecutionMode = Field(alias="executionMode")
    model_ids: list[str] = Field(alias="modelIds")
    root_trace_id: str | None = Field(alias="rootTraceId")
    status: RecoveryStatus
    current_step: int = Field(alias="currentStep", ge=0, le=5)
    current_step_summary: str = Field(alias="currentStepSummary")
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")
    pending_approval: PendingApprovalView | None = Field(default=None, alias="pendingApproval")
    claimed_decision: ClaimedDecisionView | None = Field(
        default=None,
        alias="claimedDecision",
    )

    @model_validator(mode="after")
    def enforce_execution_mode_trace_provenance(self) -> Self:
        if self.pending_approval is not None and self.claimed_decision is not None:
            raise ValueError("Pending consent and a durable decision claim are mutually exclusive")
        if self.claimed_decision is not None and (
            self.status is not RecoveryStatus.PENDING_APPROVAL
            or self.scenario_id is not ScenarioId.HOTEL
            or self.execution_mode
            not in {ExecutionMode.SDK_STUB, ExecutionMode.OPENAI_LIVE}
            or self.pending_approval is not None
        ):
            raise ValueError(
                "Claimed decisions require one resumable pending hotel recovery"
            )
        if self.execution_mode is ExecutionMode.REPLAY_FIXTURE:
            if self.model_ids or self.root_trace_id is not None:
                raise ValueError("Replay snapshots require no model IDs or root trace ID")
            return self
        if self.execution_mode is ExecutionMode.SDK_STUB:
            if (
                self.model_ids
                or self.root_trace_id is None
                or not is_valid_qa_trace_id(self.root_trace_id)
            ):
                raise ValueError("SDK stub snapshots require one QA root and no model IDs")
            return self
        if (
            not self.model_ids
            or self.root_trace_id is None
            or not is_valid_live_trace_id(self.root_trace_id)
        ):
            raise ValueError("OpenAI live snapshots require model IDs and an SDK root trace ID")
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
    status: Literal[
        "completed",
        "simulated_completed",
        "closed_without_action",
        "outcome_unknown",
    ]
    simulated: bool
    provider_execution: bool | None = Field(alias="providerExecution")
    model_call: bool = Field(default=False, alias="modelCall")
    model_ids: list[str] = Field(alias="modelIds")
    root_trace_id: str | None = Field(default=None, alias="rootTraceId")
    sdk_version: str | None = Field(default=None, alias="sdkVersion")
    protocol_version: str | None = Field(default=None, alias="protocolVersion")
    agent_graph_version: str | None = Field(default=None, alias="agentGraphVersion")
    definition_digest: str | None = Field(
        default=None,
        alias="definitionDigest",
        pattern=r"^[0-9a-f]{64}$",
    )
    boundary: str
    provider_result: str = Field(alias="providerResult")
    authorization_source: str = Field(alias="authorizationSource")
    verification_results: list[str] = Field(alias="verificationResults")
    approval_count: int = Field(default=0, alias="approvalCount", ge=0)
    approved_remedy_digest: str | None = Field(
        default=None,
        alias="approvedRemedyDigest",
        pattern=r"^sha256:[0-9a-f]{64}$",
    )

    @property
    def has_canonical_quota_evidence(self) -> bool:
        """Return whether all delegated-quota evidence matches the executed demo facts."""

        return (
            self.provider_result == QUOTA_SDK_PROVIDER_RESULT
            and self.authorization_source == QUOTA_SDK_AUTHORIZATION_SOURCE
            and tuple(self.verification_results) == QUOTA_SDK_VERIFICATION_RESULTS
        )

    @model_validator(mode="before")
    @classmethod
    def infer_legacy_approval_count(cls, value: object) -> object:
        """Upgrade stored pre-Task-9 receipts without weakening new quota evidence."""

        if not isinstance(value, dict):
            return value
        if "approvalCount" in value or "approval_count" in value:
            return value
        migrated = dict(value)
        execution_mode = migrated.get("executionMode", migrated.get("execution_mode"))
        status = migrated.get("status")
        migrated["approvalCount"] = int(
            status == "completed"
            and execution_mode in {
                ExecutionMode.SDK_STUB,
                ExecutionMode.SDK_STUB.value,
                ExecutionMode.OPENAI_LIVE,
                ExecutionMode.OPENAI_LIVE.value,
            }
        )
        return migrated

    @model_validator(mode="after")
    def enforce_execution_mode_provenance(self) -> Self:
        if self.execution_mode is ExecutionMode.REPLAY_FIXTURE:
            if (
                self.status != "simulated_completed"
                or not self.simulated
                or self.provider_execution is not False
                or self.model_call
                or self.model_ids
                or self.root_trace_id is not None
                or self.sdk_version is not None
                or self.protocol_version is not None
                or self.agent_graph_version is not None
                or self.definition_digest is not None
                or self.approved_remedy_digest is not None
                or self.approval_count != 0
            ):
                raise ValueError(
                    "Replay receipts require simulated-completed evidence, no provider "
                    "execution, no approved remedy digest, and no model IDs"
                )
            return self
        if self.status == "simulated_completed":
            raise ValueError("Only replay fixtures may use simulated-completed receipts")
        version_markers = (
            self.sdk_version,
            self.protocol_version,
            self.agent_graph_version,
            self.definition_digest,
        )
        if self.execution_mode is ExecutionMode.SDK_STUB:
            quota_completion = self.status == "completed" and self.approval_count == 0
            if (
                not self.simulated
                or self.model_call
                or self.model_ids
                or self.root_trace_id is None
                or not is_valid_qa_trace_id(self.root_trace_id)
                or any(marker is None for marker in version_markers)
                or self.boundary
                != (QUOTA_SDK_STUB_BOUNDARY if quota_completion else SDK_STUB_BOUNDARY)
            ):
                raise ValueError(
                    "SDK stub receipts require versioned QA provenance and no model call"
                )
            if quota_completion and self.approved_remedy_digest is not None:
                raise ValueError("Delegated quota completion cannot claim a human-approved digest")
            if quota_completion and not self.has_canonical_quota_evidence:
                raise ValueError(
                    "Delegated quota completion requires canonical provider, authorization, "
                    "and verification evidence"
                )
        if self.execution_mode is ExecutionMode.OPENAI_LIVE:
            if (
                not self.simulated
                or not self.model_call
                or self.model_ids != ["gpt-5.6-luna", "gpt-5.6-terra"]
                or self.root_trace_id is None
                or not is_valid_live_trace_id(self.root_trace_id)
                or any(marker is None for marker in version_markers)
                or self.boundary != OPENAI_LIVE_BOUNDARY
                or (self.status == "completed" and self.approval_count != 1)
            ):
                raise ValueError(
                    "OpenAI live receipts require real model-call, root-trace, version, "
                    "and demo-provider evidence"
                )
        if self.status == "completed" and self.provider_execution is not True:
            raise ValueError("Completed receipts require provider execution evidence")
        if (
            self.execution_mode in {ExecutionMode.SDK_STUB, ExecutionMode.OPENAI_LIVE}
            and self.status == "completed"
            and self.approval_count > 0
            and self.approved_remedy_digest is None
        ):
            raise ValueError("Completed SDK receipts require an approved remedy digest")
        if (
            self.execution_mode is ExecutionMode.SDK_STUB
            and self.status == "completed"
            and self.approval_count not in {0, 1}
        ):
            raise ValueError("SDK completion approval count must be zero or one")
        if self.status != "completed" and self.approval_count != 0:
            raise ValueError("Non-completed receipts cannot claim an approval")
        if self.status == RecoveryStatus.CLOSED_WITHOUT_ACTION.value and (
            self.provider_execution is not False or self.approved_remedy_digest is not None
        ):
            raise ValueError(
                "Closed-without-action receipts require zero provider execution and no "
                "approved digest"
            )
        if self.status == RecoveryStatus.OUTCOME_UNKNOWN.value and (
            self.provider_execution is not None or self.approved_remedy_digest is not None
        ):
            raise ValueError(
                "Unknown-outcome receipts cannot claim provider execution or an approved digest"
            )
        return self


class DemoResetResponse(ApiModel):
    reset: Literal[True]
