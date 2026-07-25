import type { HealthStatus } from "../domain/runtime";
import type { ExecutionMode } from "../domain/runtime";
import type {
  ApprovalDecisionRequest,
  ApprovalDecisionResponse,
  ClaimedDecision,
  DecisionResumeResponse,
  HotelRemedyTerms,
  PendingApproval,
  RecoveryReceipt,
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

export const RECOVERY_CREATION_MESSAGES = {
  idempotency_conflict:
    "This recovery start no longer matches its original request. No additional run was started.",
  creation_pending:
    "Recovery creation is still in progress. Retry the same start shortly.",
  creation_outcome_unknown:
    "The recovery start outcome could not be confirmed. No replacement run was started.",
  creation_capacity:
    "Recovery creation is temporarily at capacity. Existing starts can still be retried; try a new start later.",
} as const;

export type LiveAdmissionCode = keyof typeof LIVE_ADMISSION_MESSAGES;
export type RecoveryCreationCode = keyof typeof RECOVERY_CREATION_MESSAGES;

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

export class DecisionExpiredError extends Error {
  readonly name = "DecisionExpiredError";
  readonly code = "remedy_expired" as const;

  constructor(readonly recoveryId: string) {
    super("Consent expired. Refresh authoritative recovery evidence.");
  }
}

export class RecoveryCreationError extends Error {
  readonly name = "RecoveryCreationError";

  constructor(
    readonly code: RecoveryCreationCode,
    readonly requestId: string,
  ) {
    super(RECOVERY_CREATION_MESSAGES[code]);
  }
}

export type RecoveryLookupDisposition = "terminal" | "retryable";

export class RecoveryLookupError extends Error {
  readonly name = "RecoveryLookupError";

  constructor(readonly disposition: RecoveryLookupDisposition) {
    super(
      disposition === "terminal"
        ? "Saved recovery is unavailable."
        : "Saved recovery evidence is temporarily unavailable.",
    );
  }
}

function isAbortError(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "name" in error &&
    error.name === "AbortError"
  );
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
    value.hardConstraintSatisfied === true &&
    value.delegatedAuthoritySatisfied === true &&
    typeof value.toolCallId === "string" &&
    value.executionStarted === false
  );
}

function isClaimedDecision(value: unknown): value is ClaimedDecision {
  return (
    isRecord(value) &&
    hasExactKeys(value, ["action", "remedyDigest", "expiry"]) &&
    (value.action === "approve" || value.action === "decline") &&
    typeof value.remedyDigest === "string" &&
    /^sha256:[0-9a-f]{64}$/.test(value.remedyDigest) &&
    isUtcTimestamp(value.expiry)
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

function readDecisionExpiredError(
  value: unknown,
  status: number,
  recoveryId: string,
): DecisionExpiredError | null {
  if (
    status !== 422 ||
    !isRecord(value) ||
    !hasExactKeys(value, ["detail"]) ||
    !isRecord(value.detail) ||
    !hasExactKeys(value.detail, ["code", "recoveryId"]) ||
    value.detail.code !== "remedy_expired" ||
    value.detail.recoveryId !== recoveryId
  ) {
    return null;
  }
  return new DecisionExpiredError(recoveryId);
}

function isRecoveryCreationCode(
  value: unknown,
): value is RecoveryCreationCode {
  return typeof value === "string" && value in RECOVERY_CREATION_MESSAGES;
}

function readRecoveryCreationError(
  value: unknown,
  status: number,
): RecoveryCreationError | null {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, ["code", "message", "requestId"]) ||
    !isRecoveryCreationCode(value.code) ||
    value.message !== RECOVERY_CREATION_MESSAGES[value.code] ||
    typeof value.requestId !== "string" ||
    !/^[0-9a-f]{32}$/.test(value.requestId)
  ) {
    return null;
  }
  const expectedStatus = value.code === "creation_capacity" ? 429 : 409;
  if (status !== expectedStatus) {
    return null;
  }
  return new RecoveryCreationError(value.code, value.requestId);
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
      "claimedDecision",
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
  const claimIsValid =
    value.claimedDecision === null || isClaimedDecision(value.claimedDecision);
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
    claimIsValid &&
    !(value.pendingApproval !== null && value.claimedDecision !== null) &&
    (value.claimedDecision === null ||
      (value.status === "pending_approval" &&
        value.scenarioId === "hotel" &&
        (value.executionMode === "sdk_stub" || value.executionMode === "openai_live"))) &&
    (value.status === "pending_approval" ||
      (value.pendingApproval === null && value.claimedDecision === null))
  );
}

export function isRecoveryReceipt(value: unknown): value is RecoveryReceipt {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, [
      "recoveryId",
      "executionMode",
      "status",
      "simulated",
      "providerExecution",
      "modelCall",
      "modelIds",
      "rootTraceId",
      "sdkVersion",
      "protocolVersion",
      "agentGraphVersion",
      "definitionDigest",
      "boundary",
      "providerResult",
      "authorizationSource",
      "verificationResults",
      "approvalCount",
      "approvedRemedyDigest",
    ])
  ) {
    return false;
  }

  const modeIsValid =
    value.executionMode === "openai_live" ||
    value.executionMode === "sdk_stub" ||
    value.executionMode === "replay_fixture";
  const statusIsValid =
    value.status === "completed" ||
    value.status === "simulated_completed" ||
    value.status === "closed_without_action" ||
    value.status === "outcome_unknown";
  const versionFieldsAreValid = [
    value.sdkVersion,
    value.protocolVersion,
    value.agentGraphVersion,
  ].every((field) => field === null || typeof field === "string");
  const digestFieldsAreValid =
    (value.definitionDigest === null ||
      (typeof value.definitionDigest === "string" && /^[0-9a-f]{64}$/.test(value.definitionDigest))) &&
    (value.approvedRemedyDigest === null ||
      (typeof value.approvedRemedyDigest === "string" &&
        /^sha256:[0-9a-f]{64}$/.test(value.approvedRemedyDigest)));
  const commonFieldsAreValid =
    typeof value.recoveryId === "string" &&
    modeIsValid &&
    statusIsValid &&
    typeof value.simulated === "boolean" &&
    (typeof value.providerExecution === "boolean" || value.providerExecution === null) &&
    typeof value.modelCall === "boolean" &&
    isStringArray(value.modelIds) &&
    (value.rootTraceId === null || typeof value.rootTraceId === "string") &&
    versionFieldsAreValid &&
    digestFieldsAreValid &&
    typeof value.boundary === "string" &&
    typeof value.providerResult === "string" &&
    typeof value.authorizationSource === "string" &&
    isStringArray(value.verificationResults) &&
    Number.isInteger(value.approvalCount) &&
    Number(value.approvalCount) >= 0;
  if (!commonFieldsAreValid) {
    return false;
  }
  if (!isStringArray(value.modelIds) || !isStringArray(value.verificationResults)) {
    return false;
  }

  if (value.executionMode === "replay_fixture") {
    return (
      value.status === "simulated_completed" &&
      value.simulated === true &&
      value.providerExecution === false &&
      value.modelCall === false &&
      value.modelIds.length === 0 &&
      value.rootTraceId === null &&
      value.sdkVersion === null &&
      value.protocolVersion === null &&
      value.agentGraphVersion === null &&
      value.definitionDigest === null &&
      value.approvalCount === 0 &&
      value.approvedRemedyDigest === null
    );
  }

  const versioned =
    typeof value.sdkVersion === "string" &&
    typeof value.protocolVersion === "string" &&
    typeof value.agentGraphVersion === "string" &&
    typeof value.definitionDigest === "string";
  const modeProvenance =
    value.executionMode === "sdk_stub"
      ? value.modelCall === false &&
        value.modelIds.length === 0 &&
        typeof value.rootTraceId === "string" &&
        /^qa_trace_[0-9a-f]{32}$/.test(value.rootTraceId)
      : value.modelCall === true &&
        value.modelIds.length === 2 &&
        value.modelIds[0] === "gpt-5.6-luna" &&
        value.modelIds[1] === "gpt-5.6-terra" &&
        typeof value.rootTraceId === "string" &&
        /^trace_[0-9a-f]{32}$/.test(value.rootTraceId);
  if (!versioned || !modeProvenance || value.simulated !== true) {
    return false;
  }
  const sdkBoundary =
    "Deterministic Agents SDK model and demo hotel adapter only; no OpenAI model call, real booking, or payment change.";
  const quotaBoundary =
    "Deterministic Agents SDK stub and demo quota adapter only; no OpenAI model call or real quota change.";
  const liveBoundary =
    "OpenAI agent model calls and demo hotel adapter only; no real booking or payment change.";
  if (value.executionMode === "openai_live" && value.boundary !== liveBoundary) {
    return false;
  }
  if (value.status === "completed") {
    if (value.providerExecution !== true) {
      return false;
    }
    if (value.executionMode === "openai_live") {
      return value.approvalCount === 1 && value.approvedRemedyDigest !== null;
    }
    if (value.approvalCount === 1) {
      return value.boundary === sdkBoundary && value.approvedRemedyDigest !== null;
    }
    const quotaVerificationResults = [
      "Provider proved the baseline quota ceiling at 1000 units.",
      "Temporary US-region burst granted: 250 units for 900 seconds.",
      "All hard constraints remained satisfied.",
      "Extra cost of 300 USD minor units stayed within the delegated 500-unit limit.",
      "Approval count is zero; no human interruption was created.",
      "Execution verified at an effective ceiling of 1250 units.",
      "Temporary quota permission revoked; baseline ceiling restored to 1000 units.",
    ];
    return (
      value.approvalCount === 0 &&
      value.approvedRemedyDigest === null &&
      value.boundary === quotaBoundary &&
      value.providerResult ===
        "Demo quota adapter verified 1200 units against a temporary 1250-unit US-region ceiling; no real quota was changed." &&
      value.authorizationSource ===
        "Predelegated API quota policy: US-only, at most 500 USD minor units, for at most 900 seconds." &&
      value.verificationResults.length === quotaVerificationResults.length &&
      value.verificationResults.every(
        (result, index) => result === quotaVerificationResults[index],
      )
    );
  }
  if (value.status === "closed_without_action") {
    return (
      value.approvalCount === 0 &&
      value.providerExecution === false &&
      value.approvedRemedyDigest === null &&
      (value.executionMode !== "sdk_stub" || value.boundary === sdkBoundary)
    );
  }
  return (
    value.status === "outcome_unknown" &&
    (value.approvalCount === 0 || value.approvalCount === 1) &&
    value.providerExecution === null &&
    value.approvedRemedyDigest === null &&
    (value.executionMode !== "sdk_stub" || value.boundary === sdkBoundary)
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
  try {
    const response = await fetch(`/api/recoveries/${encodeURIComponent(recoveryId)}`, {
      headers: { Accept: "application/json" },
      signal,
    });
    if (!response.ok) {
      throw new RecoveryLookupError(response.status === 404 ? "terminal" : "retryable");
    }
    const snapshot = await readRecovery(response);
    if (snapshot.recoveryId !== recoveryId) {
      throw new RecoveryLookupError("retryable");
    }
    return snapshot;
  } catch (error: unknown) {
    if (isAbortError(error) || error instanceof RecoveryLookupError) throw error;
    throw new RecoveryLookupError("retryable");
  }
}

export async function getReceipt(
  recoveryId: string,
  signal?: AbortSignal,
): Promise<RecoveryReceipt> {
  const response = await fetch(
    `/api/recoveries/${encodeURIComponent(recoveryId)}/receipt`,
    {
      headers: { Accept: "application/json" },
      signal,
    },
  );
  if (!response.ok) {
    throw new Error(`Receipt request failed with status ${response.status}`);
  }
  const body: unknown = await response.json();
  if (!isRecoveryReceipt(body) || body.recoveryId !== recoveryId) {
    throw new Error("Receipt response did not match the receipt contract");
  }
  return body;
}

export async function createRecovery(
  scenarioId: ScenarioId,
  executionMode: ExecutionMode,
  clientRequestId: string,
  signal?: AbortSignal,
): Promise<RecoverySnapshot> {
  const response = await fetch("/api/recoveries", {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ clientRequestId, scenarioId, executionMode }),
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
    const creationError = readRecoveryCreationError(body, response.status);
    if (creationError !== null) {
      throw creationError;
    }
    throw new Error(`Recovery creation failed with status ${response.status}`);
  }
  const snapshot = await readRecovery(response);
  if (snapshot.scenarioId !== scenarioId || snapshot.executionMode !== executionMode) {
    throw new Error("Recovery response did not match the requested creation");
  }
  return snapshot;
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
    const expiredError = readDecisionExpiredError(
      body,
      response.status,
      recoveryId,
    );
    if (expiredError !== null) {
      throw expiredError;
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

function isDecisionResumeResponse(value: unknown): value is DecisionResumeResponse {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, [
      "action",
      "recoveryId",
      "remedyDigest",
      "status",
      "approvedRemedyDigest",
      "executionStarted",
    ]) ||
    typeof value.recoveryId !== "string" ||
    typeof value.remedyDigest !== "string" ||
    !/^sha256:[0-9a-f]{64}$/.test(value.remedyDigest)
  ) {
    return false;
  }
  if (value.action === "approve") {
    return (
      value.status === "completed" &&
      value.approvedRemedyDigest === value.remedyDigest &&
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

export async function postDecisionResume(
  recoveryId: string,
  claimedDecision: ClaimedDecision,
  signal?: AbortSignal,
): Promise<DecisionResumeResponse> {
  const response = await fetch(
    `/api/recoveries/${encodeURIComponent(recoveryId)}/decisions/resume`,
    {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({}),
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
    const expiredError = readDecisionExpiredError(
      body,
      response.status,
      recoveryId,
    );
    if (expiredError !== null) throw expiredError;
    const capacityError = readDecisionCapacityError(body, response.status);
    if (capacityError !== null) throw capacityError;
    throw new Error(`Decision resume failed with status ${response.status}`);
  }
  const body: unknown = await response.json();
  if (!isDecisionResumeResponse(body)) {
    throw new Error("Decision resume response did not match the resume contract");
  }
  if (
    body.recoveryId !== recoveryId ||
    body.action !== claimedDecision.action ||
    body.remedyDigest !== claimedDecision.remedyDigest
  ) {
    throw new Error("Decision resume response did not match the claimed decision");
  }
  return body;
}
