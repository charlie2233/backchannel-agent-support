import type { HealthStatus } from "../domain/runtime";
import type { ExecutionMode } from "../domain/runtime";
import type {
  ApprovalDecisionResponse,
  DecisionRequest,
  DecisionResponse,
  DeclineDecisionResponse,
  HotelRemedyTerms,
  PendingApproval,
  RecoveryReceipt,
  RecoverySnapshot,
  ScenarioId,
} from "../domain/recovery";

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

function isSha256Digest(value: unknown): value is `sha256:${string}` {
  return typeof value === "string" && /^sha256:[0-9a-f]{64}$/.test(value);
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
    isSha256Digest(value.remedyDigest) &&
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
    throw new Error(`Recovery creation failed with status ${response.status}`);
  }
  return readRecovery(response);
}

function isApprovalDecisionResponse(
  value: Record<string, unknown>,
): value is Record<string, unknown> & ApprovalDecisionResponse {
  return (
    hasExactKeys(value, [
      "clientDecisionId",
      "recoveryId",
      "decision",
      "status",
      "approvedRemedyDigest",
      "executionStarted",
    ]) &&
    typeof value.clientDecisionId === "string" &&
    typeof value.recoveryId === "string" &&
    value.decision === "approve" &&
    value.status === "completed" &&
    isSha256Digest(value.approvedRemedyDigest) &&
    value.executionStarted === true
  );
}

function isDeclineDecisionResponse(
  value: Record<string, unknown>,
): value is Record<string, unknown> & DeclineDecisionResponse {
  return (
    hasExactKeys(value, [
      "clientDecisionId",
      "recoveryId",
      "decision",
      "status",
      "decisionRemedyDigest",
      "executionStarted",
    ]) &&
    typeof value.clientDecisionId === "string" &&
    typeof value.recoveryId === "string" &&
    value.decision === "decline" &&
    (value.status === "closed_without_action" || value.status === "outcome_unknown") &&
    isSha256Digest(value.decisionRemedyDigest) &&
    typeof value.executionStarted === "boolean"
  );
}

function isDecisionResponse(value: unknown): value is DecisionResponse {
  return (
    isRecord(value) &&
    (isApprovalDecisionResponse(value) || isDeclineDecisionResponse(value))
  );
}

export async function postDecision(
  recoveryId: string,
  decision: DecisionRequest,
  signal?: AbortSignal,
): Promise<DecisionResponse> {
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
    throw new Error(`Decision request failed with status ${response.status}`);
  }
  const body: unknown = await response.json();
  if (!isDecisionResponse(body)) {
    throw new Error("Decision response did not match the terminal action contract");
  }
  return body;
}

function nullableDigest(value: unknown): value is `sha256:${string}` | null {
  return value === null || isSha256Digest(value);
}

function isRecoveryReceipt(value: unknown): value is RecoveryReceipt {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, [
      "recoveryId",
      "executionMode",
      "status",
      "simulated",
      "providerExecution",
      "modelIds",
      "boundary",
      "providerResult",
      "authorizationSource",
      "verificationResults",
      "decision",
      "decisionRemedyDigest",
      "executionCount",
      "providerDispatchStarted",
      "exactInterruptionRejected",
      "permissionRevoked",
      "scopeClosed",
      "approvedRemedyDigest",
    ])
  ) {
    return false;
  }
  const executionModeIsValid =
    value.executionMode === "openai_live" ||
    value.executionMode === "sdk_stub" ||
    value.executionMode === "replay_fixture";
  const statusIsTerminal =
    value.status === "completed" ||
    value.status === "closed_without_action" ||
    value.status === "outcome_unknown";
  const commonFieldsAreValid =
    typeof value.recoveryId === "string" &&
    executionModeIsValid &&
    statusIsTerminal &&
    typeof value.simulated === "boolean" &&
    typeof value.providerExecution === "boolean" &&
    isStringArray(value.modelIds) &&
    typeof value.boundary === "string" &&
    typeof value.providerResult === "string" &&
    typeof value.authorizationSource === "string" &&
    isStringArray(value.verificationResults) &&
    (value.decision === "approved" || value.decision === "declined" || value.decision === null) &&
    nullableDigest(value.decisionRemedyDigest) &&
    typeof value.executionCount === "number" &&
    Number.isInteger(value.executionCount) &&
    Number(value.executionCount) >= 0 &&
    typeof value.providerDispatchStarted === "boolean" &&
    typeof value.exactInterruptionRejected === "boolean" &&
    typeof value.permissionRevoked === "boolean" &&
    typeof value.scopeClosed === "boolean" &&
    nullableDigest(value.approvedRemedyDigest);
  if (!commonFieldsAreValid) {
    return false;
  }

  if (value.executionMode !== "sdk_stub") {
    return true;
  }
  if (!value.permissionRevoked || !value.scopeClosed || value.decisionRemedyDigest === null) {
    return false;
  }
  if (value.status === "completed") {
    return (
      value.decision === "approved" &&
      value.providerExecution === true &&
      value.executionCount === 1 &&
      value.providerDispatchStarted === true &&
      value.exactInterruptionRejected === false &&
      value.approvedRemedyDigest === value.decisionRemedyDigest
    );
  }
  if (value.status === "closed_without_action") {
    return (
      value.decision === "declined" &&
      value.providerExecution === false &&
      value.executionCount === 0 &&
      value.providerDispatchStarted === false &&
      value.exactInterruptionRejected === true &&
      value.approvedRemedyDigest === null
    );
  }
  return (
    value.decision === "declined" &&
    Number(value.executionCount) >= 1 &&
    value.providerDispatchStarted === true &&
    value.exactInterruptionRejected === true &&
    value.approvedRemedyDigest === null
  );
}

export async function getReceipt(
  recoveryId: string,
  signal?: AbortSignal,
): Promise<RecoveryReceipt> {
  const response = await fetch(
    `/api/recoveries/${encodeURIComponent(recoveryId)}/receipt`,
    { headers: { Accept: "application/json" }, signal },
  );
  if (!response.ok) {
    throw new Error(`Receipt request failed with status ${response.status}`);
  }
  const body: unknown = await response.json();
  if (!isRecoveryReceipt(body)) {
    throw new Error("Receipt response did not match the terminal evidence contract");
  }
  return body;
}
