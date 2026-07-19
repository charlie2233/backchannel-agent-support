import type { HealthStatus } from "../domain/runtime";
import type { ExecutionMode } from "../domain/runtime";
import type {
  ApprovalDecisionRequest,
  ApprovalDecisionResponse,
  HotelRemedyTerms,
  PendingApproval,
  RecoverySnapshot,
  ScenarioId,
} from "../domain/recovery";

export const LIVE_ADMISSION_MESSAGES = {
  live_unavailable:
    "Live recovery is unavailable in this demo. A replay fixture is starting automatically; you can rerun it explicitly.",
  live_capacity:
    "Live recovery is currently at capacity. A replay fixture is starting automatically; you can rerun it explicitly.",
  cooldown:
    "Please wait before starting another live recovery. A replay fixture is starting automatically; you can rerun it explicitly.",
  daily_budget:
    "The daily live demo budget is currently reached. A replay fixture is starting automatically; you can rerun it explicitly.",
} as const;

export const DECISION_CAPACITY_MESSAGE =
  "Live decision processing is currently at capacity. Retry the same decision shortly.";

export type LiveAdmissionCode = keyof typeof LIVE_ADMISSION_MESSAGES;

export class LiveAdmissionError extends Error {
  readonly name = "LiveAdmissionError";
  readonly fallbackExecutionMode = "replay_fixture" as const;

  constructor(
    readonly code: LiveAdmissionCode,
    readonly requestId: string,
  ) {
    super(LIVE_ADMISSION_MESSAGES[code]);
  }
}

export class DecisionCapacityError extends Error {
  readonly name = "DecisionCapacityError";
  readonly code = "decision_capacity" as const;

  constructor(readonly requestId: string) {
    super(DECISION_CAPACITY_MESSAGE);
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasExactKeys(value: Record<string, unknown>, keys: ReadonlyArray<string>): boolean {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  return actual.length === expected.length && actual.every((key, index) => key === expected[index]);
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function isUtcTimestamp(value: unknown): value is string {
  return (
    typeof value === "string" &&
    (value.endsWith("Z") || value.endsWith("+00:00")) &&
    !Number.isNaN(Date.parse(value))
  );
}

function isHotelRemedyTerms(value: unknown): value is HotelRemedyTerms {
  if (!isRecord(value) || !hasExactKeys(value, ["bookingId", "action", "replacement", "stay", "currency"])) {
    return false;
  }
  if (!isRecord(value.replacement) || !hasExactKeys(value.replacement, ["fromRoomType", "toRoomType"])) {
    return false;
  }
  if (!isRecord(value.stay) || !hasExactKeys(value.stay, ["checkIn", "checkOut"])) {
    return false;
  }
  return (
    typeof value.bookingId === "string" &&
    value.action === "replace_room" &&
    typeof value.replacement.fromRoomType === "string" &&
    typeof value.replacement.toRoomType === "string" &&
    typeof value.stay.checkIn === "string" &&
    typeof value.stay.checkOut === "string" &&
    value.currency === "USD"
  );
}

function isPendingApproval(value: unknown): value is PendingApproval {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, [
      "remedyId",
      "remedyDigest",
      "terms",
      "costDeltaMinor",
      "changedFields",
      "providerCommitments",
      "expiry",
      "hardConstraintSatisfied",
      "delegatedAuthoritySatisfied",
      "toolCallId",
      "executionStarted",
    ])
  ) {
    return false;
  }
  const changedFields = value.changedFields;
  const providerCommitments = value.providerCommitments;
  return (
    typeof value.remedyId === "string" &&
    typeof value.remedyDigest === "string" &&
    /^sha256:[0-9a-f]{64}$/.test(value.remedyDigest) &&
    isHotelRemedyTerms(value.terms) &&
    Number.isInteger(value.costDeltaMinor) &&
    isStringArray(changedFields) &&
    changedFields.length > 0 &&
    changedFields.every((item, index) => item === [...changedFields].sort()[index]) &&
    isStringArray(providerCommitments) &&
    providerCommitments.length > 0 &&
    providerCommitments.every(
      (item, index) => item === [...providerCommitments].sort()[index],
    ) &&
    isUtcTimestamp(value.expiry) &&
    typeof value.hardConstraintSatisfied === "boolean" &&
    typeof value.delegatedAuthoritySatisfied === "boolean" &&
    typeof value.toolCallId === "string" &&
    value.executionStarted === false
  );
}

function isHealthStatus(value: unknown): value is HealthStatus {
  if (typeof value !== "object" || value === null) {
    return false;
  }

  const candidate = value as Record<string, unknown>;
  return (
    (candidate.backend === "openai" || candidate.backend === "stub") &&
    typeof candidate.liveReady === "boolean" &&
    candidate.providerBoundary === "demo_adapter_only"
  );
}

function isLiveAdmissionCode(value: unknown): value is LiveAdmissionCode {
  return typeof value === "string" && value in LIVE_ADMISSION_MESSAGES;
}

function readLiveAdmissionError(
  value: unknown,
  status: number,
): LiveAdmissionError | null {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, [
      "code",
      "message",
      "requestId",
      "fallbackExecutionMode",
    ]) ||
    !isLiveAdmissionCode(value.code) ||
    value.message !== LIVE_ADMISSION_MESSAGES[value.code] ||
    typeof value.requestId !== "string" ||
    !/^[0-9a-f]{32}$/.test(value.requestId) ||
    value.fallbackExecutionMode !== "replay_fixture"
  ) {
    return null;
  }
  const expectedStatus = value.code === "live_unavailable" ? 422 : 429;
  if (status !== expectedStatus) {
    return null;
  }
  return new LiveAdmissionError(value.code, value.requestId);
}

function readDecisionCapacityError(
  value: unknown,
  status: number,
): DecisionCapacityError | null {
  if (
    status !== 429 ||
    !isRecord(value) ||
    !hasExactKeys(value, ["code", "message", "requestId"]) ||
    value.code !== "decision_capacity" ||
    value.message !== DECISION_CAPACITY_MESSAGE ||
    typeof value.requestId !== "string" ||
    !/^[0-9a-f]{32}$/.test(value.requestId)
  ) {
    return null;
  }
  return new DecisionCapacityError(value.requestId);
}

export async function getHealth(signal?: AbortSignal): Promise<HealthStatus> {
  const response = await fetch("/health", {
    headers: { Accept: "application/json" },
    signal,
  });

  if (!response.ok) {
    throw new Error(`Health request failed with status ${response.status}`);
  }

  const body: unknown = await response.json();
  if (!isHealthStatus(body)) {
    throw new Error("Health response did not match the runtime contract");
  }

  return body;
}

export function isRecoverySnapshot(value: unknown): value is RecoverySnapshot {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, [
      "recoveryId",
      "scenarioId",
      "executionMode",
      "modelIds",
      "rootTraceId",
      "status",
      "currentStep",
      "currentStepSummary",
      "createdAt",
      "updatedAt",
      "pendingApproval",
    ])
  ) {
    return false;
  }
  const statusIsValid =
    value.status === "in_progress" ||
    value.status === "pending_approval" ||
    value.status === "completed" ||
    value.status === "closed_without_action" ||
    value.status === "outcome_unknown";
  const approvalIsValid =
    value.pendingApproval === null || isPendingApproval(value.pendingApproval);
  const modeProvenanceIsValid =
    isStringArray(value.modelIds) &&
    ((value.executionMode === "replay_fixture" &&
      value.modelIds.length === 0 &&
      value.rootTraceId === null) ||
      (value.executionMode === "sdk_stub" &&
        value.modelIds.length === 0 &&
        typeof value.rootTraceId === "string" &&
        /^qa_trace_[0-9a-f]{32}$/.test(value.rootTraceId)) ||
      (value.executionMode === "openai_live" &&
        value.modelIds.length > 0 &&
        value.modelIds.every((modelId) => modelId.startsWith("gpt-5.6-")) &&
        typeof value.rootTraceId === "string" &&
        /^trace_[0-9a-f]{32}$/.test(value.rootTraceId)));
  return (
    typeof value.recoveryId === "string" &&
    (value.scenarioId === "hotel" || value.scenarioId === "api-quota") &&
    (value.executionMode === "openai_live" ||
      value.executionMode === "sdk_stub" ||
      value.executionMode === "replay_fixture") &&
    statusIsValid &&
    Number.isInteger(value.currentStep) &&
    Number(value.currentStep) >= 0 &&
    Number(value.currentStep) <= 5 &&
    typeof value.currentStepSummary === "string" &&
    isUtcTimestamp(value.createdAt) &&
    isUtcTimestamp(value.updatedAt) &&
    modeProvenanceIsValid &&
    approvalIsValid &&
    (value.status === "pending_approval" || value.pendingApproval === null)
  );
}

async function readRecovery(response: Response): Promise<RecoverySnapshot> {
  const body: unknown = await response.json();
  if (!isRecoverySnapshot(body)) {
    throw new Error("Recovery response did not match the snapshot contract");
  }
  return body;
}

export async function getRecovery(
  recoveryId: string,
  signal?: AbortSignal,
): Promise<RecoverySnapshot> {
  const response = await fetch(`/api/recoveries/${encodeURIComponent(recoveryId)}`, {
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    throw new Error(`Recovery request failed with status ${response.status}`);
  }
  return readRecovery(response);
}

export async function createRecovery(
  scenarioId: ScenarioId,
  executionMode: ExecutionMode,
  signal?: AbortSignal,
): Promise<RecoverySnapshot> {
  const response = await fetch("/api/recoveries", {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ scenarioId, executionMode }),
    signal,
  });
  if (!response.ok) {
    let body: unknown = null;
    try {
      body = await response.json();
    } catch {
      // A malformed error response is handled by the generic status-only path.
    }
    const admissionError = readLiveAdmissionError(body, response.status);
    if (admissionError !== null) {
      throw admissionError;
    }
    throw new Error(`Recovery creation failed with status ${response.status}`);
  }
  return readRecovery(response);
}

function isDecisionResponse(value: unknown): value is ApprovalDecisionResponse {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, [
      "action",
      "clientDecisionId",
      "recoveryId",
      "status",
      "approvedRemedyDigest",
      "executionStarted",
    ]) ||
    typeof value.clientDecisionId !== "string" ||
    typeof value.recoveryId !== "string"
  ) {
    return false;
  }
  if (value.action === "approve") {
    return (
      value.status === "completed" &&
      typeof value.approvedRemedyDigest === "string" &&
      /^sha256:[0-9a-f]{64}$/.test(value.approvedRemedyDigest) &&
      value.executionStarted === true
    );
  }
  if (value.action !== "decline" || value.approvedRemedyDigest !== null) {
    return false;
  }
  return (
    (value.status === "closed_without_action" && value.executionStarted === false) ||
    (value.status === "outcome_unknown" && value.executionStarted === null)
  );
}

export async function postDecision(
  recoveryId: string,
  decision: ApprovalDecisionRequest,
  signal?: AbortSignal,
): Promise<ApprovalDecisionResponse> {
  const response = await fetch(
    `/api/recoveries/${encodeURIComponent(recoveryId)}/decisions`,
    {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify(decision),
      signal,
    },
  );
  if (!response.ok) {
    let body: unknown = null;
    try {
      body = await response.json();
    } catch {
      // A malformed error response is handled by the generic status-only path.
    }
    const capacityError = readDecisionCapacityError(body, response.status);
    if (capacityError !== null) {
      throw capacityError;
    }
    throw new Error(`Decision request failed with status ${response.status}`);
  }
  const body: unknown = await response.json();
  if (!isDecisionResponse(body)) {
    throw new Error("Decision response did not match the decision contract");
  }
  if (
    body.action !== decision.action ||
    body.clientDecisionId !== decision.clientDecisionId ||
    body.recoveryId !== recoveryId ||
    (body.action === "approve" &&
      body.approvedRemedyDigest !== decision.remedyDigest)
  ) {
    throw new Error("Decision response did not match the requested decision");
  }
  return body;
}
