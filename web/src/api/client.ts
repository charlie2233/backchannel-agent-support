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
    value.status === "completed";
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

function isDecisionResponse(value: unknown): value is ApprovalDecisionResponse {
  return (
    isRecord(value) &&
    hasExactKeys(value, [
      "clientDecisionId",
      "recoveryId",
      "status",
      "approvedRemedyDigest",
      "executionStarted",
    ]) &&
    typeof value.clientDecisionId === "string" &&
    typeof value.recoveryId === "string" &&
    value.status === "completed" &&
    typeof value.approvedRemedyDigest === "string" &&
    /^sha256:[0-9a-f]{64}$/.test(value.approvedRemedyDigest) &&
    value.executionStarted === true
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
    throw new Error(`Decision request failed with status ${response.status}`);
  }
  const body: unknown = await response.json();
  if (!isDecisionResponse(body)) {
    throw new Error("Decision response did not match the approval contract");
  }
  return body;
}
