import type { HealthStatus } from "../domain/runtime";
import type { ExecutionMode } from "../domain/runtime";
import type {
  ApprovalDecisionResponse,
  DecisionRequest,
  DecisionResponse,
  DeclineDecisionResponse,
  HotelRemedyTerms,
  PendingApproval,
  QuotaEvidence,
  RecoveryReceipt,
  RecoverySnapshot,
  ScenarioId,
} from "../domain/recovery";

export class HttpStatusError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "HttpStatusError";
    this.status = status;
  }
}

export type PublicErrorCode =
  | "invalid_request"
  | "method_not_allowed"
  | "not_found"
  | "unsupported_media_type"
  | "request_too_large"
  | "live_unavailable"
  | "live_timeout"
  | "live_cooldown"
  | "live_daily_budget_exceeded"
  | "live_capacity_reached"
  | "creation_daily_budget_exceeded"
  | "stream_capacity_reached"
  | "resume_incompatible"
  | "already_decided"
  | "decision_id_conflict"
  | "remedy_mismatch"
  | "remedy_digest_mismatch"
  | "tool_call_mismatch"
  | "consent_expired"
  | "hard_constraint_denied"
  | "authority_denied"
  | "constraint_denied"
  | "remedy_expired"
  | "decision_unavailable"
  | "decision_in_progress"
  | "model_metadata_conflict"
  | "internal_error";

export interface ReplayFixtureFallback {
  kind: "show_replay_fixture";
  scenarioId: "hotel";
  executionMode: "replay_fixture";
}

const publicMessages: Readonly<Record<PublicErrorCode, string>> = {
  invalid_request: "The request is invalid.",
  method_not_allowed: "The method is not allowed.",
  not_found: "The requested resource was not found.",
  unsupported_media_type: "Content-Type must be application/json.",
  request_too_large: "The request body is too large.",
  live_unavailable: "Live mode is unavailable on this server.",
  live_timeout: "Live processing did not finish before the server deadline.",
  live_cooldown: "Live mode is cooling down for this demo identity.",
  live_daily_budget_exceeded: "The live demo budget is exhausted for today.",
  live_capacity_reached: "The live demo is currently at capacity.",
  creation_daily_budget_exceeded:
    "The public demo recovery creation budget is exhausted for today.",
  stream_capacity_reached: "The event stream is currently at capacity.",
  resume_incompatible: "The saved decision cannot be resumed safely.",
  already_decided: "This recovery already has a terminal decision.",
  decision_id_conflict: "This decision identifier was already used.",
  remedy_mismatch: "The decision does not match the pending remedy.",
  remedy_digest_mismatch: "The decision does not match the displayed terms.",
  tool_call_mismatch: "The decision does not match the pending tool call.",
  consent_expired: "The displayed remedy has expired.",
  hard_constraint_denied: "The remedy no longer satisfies hard constraints.",
  authority_denied: "The remedy exceeds delegated authority.",
  constraint_denied: "The remedy no longer satisfies hard constraints.",
  remedy_expired: "The displayed remedy has expired.",
  decision_unavailable: "The pending decision is unavailable.",
  decision_in_progress: "The durable decision is already being resumed.",
  model_metadata_conflict: "The saved decision cannot be resumed safely.",
  internal_error: "The request could not be completed.",
};

const publicErrorCodes = new Set<string>(Object.keys(publicMessages));
const fallbackCodes = new Set<PublicErrorCode>([
  "live_unavailable",
  "live_timeout",
  "live_cooldown",
  "live_daily_budget_exceeded",
  "live_capacity_reached",
]);

export class PublicApiError extends HttpStatusError {
  readonly code: PublicErrorCode | "unexpected_response";
  readonly requestId: string | null;
  readonly recoveryId: string | null;
  readonly retryAfterSeconds: number | null;
  readonly fallback: ReplayFixtureFallback | null;

  constructor(
    message: string,
    status: number,
    options: {
      code: PublicErrorCode | "unexpected_response";
      requestId: string | null;
      recoveryId: string | null;
      retryAfterSeconds: number | null;
      fallback: ReplayFixtureFallback | null;
    },
  ) {
    super(message, status);
    this.name = "PublicApiError";
    this.code = options.code;
    this.requestId = options.requestId;
    this.recoveryId = options.recoveryId;
    this.retryAfterSeconds = options.retryAfterSeconds;
    this.fallback = options.fallback;
  }
}

function unexpectedResponse(status: number): PublicApiError {
  return new PublicApiError("The server returned an unexpected response.", status, {
    code: "unexpected_response",
    requestId: null,
    recoveryId: null,
    retryAfterSeconds: null,
    fallback: null,
  });
}

const httpTokenPattern = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/;

function trimOptionalWhitespace(value: string): string {
  return value.replace(/^[\t ]+|[\t ]+$/g, "");
}

function isHttpQuotedString(value: string): boolean {
  if (
    value.length < 2 ||
    value[0] !== '"' ||
    value[value.length - 1] !== '"'
  ) {
    return false;
  }
  for (let index = 1; index < value.length - 1; index += 1) {
    const character = value[index] ?? "";
    const codePoint = character.charCodeAt(0);
    if (character === "\\") {
      index += 1;
      if (index >= value.length - 1) {
        return false;
      }
      const escapedCodePoint = (value[index] ?? "").charCodeAt(0);
      if (
        escapedCodePoint !== 0x09 &&
        (escapedCodePoint < 0x20 || escapedCodePoint === 0x7f)
      ) {
        return false;
      }
      continue;
    }
    if (
      character === '"' ||
      (codePoint !== 0x09 && (codePoint < 0x20 || codePoint === 0x7f))
    ) {
      return false;
    }
  }
  return true;
}

function splitContentTypeSections(value: string): string[] | null {
  const sections: string[] = [];
  let sectionStart = 0;
  let inQuotes = false;
  let escaped = false;
  for (let index = 0; index < value.length; index += 1) {
    const character = value[index] ?? "";
    if (character === ",") {
      return null;
    }
    if (inQuotes) {
      if (escaped) {
        escaped = false;
        continue;
      }
      if (character === "\\") {
        escaped = true;
      } else if (character === '"') {
        inQuotes = false;
      }
      continue;
    }
    if (character === '"') {
      inQuotes = true;
    } else if (character === ";") {
      sections.push(value.slice(sectionStart, index));
      sectionStart = index + 1;
    }
  }
  if (inQuotes || escaped) {
    return null;
  }
  sections.push(value.slice(sectionStart));
  return sections;
}

function hasJsonContentType(response: Response): boolean {
  const contentType = response.headers.get("Content-Type");
  if (contentType === null) {
    return false;
  }
  const sections = splitContentTypeSections(contentType);
  if (
    sections === null ||
    trimOptionalWhitespace(sections.shift() ?? "").toLowerCase() !==
      "application/json"
  ) {
    return false;
  }
  const parameterNames = new Set<string>();
  for (const rawParameter of sections) {
    const parameter = trimOptionalWhitespace(rawParameter);
    const equalsIndex = parameter.indexOf("=");
    if (equalsIndex < 1) {
      return false;
    }
    const name = trimOptionalWhitespace(
      parameter.slice(0, equalsIndex),
    );
    const value = trimOptionalWhitespace(
      parameter.slice(equalsIndex + 1),
    );
    const normalizedName = name.toLowerCase();
    if (
      !httpTokenPattern.test(name) ||
      parameterNames.has(normalizedName) ||
      (!httpTokenPattern.test(value) && !isHttpQuotedString(value))
    ) {
      return false;
    }
    parameterNames.add(normalizedName);
  }
  return true;
}

function cancelUnlockedResponseBody(response: Response): void {
  if (response.body === null || response.body.locked) {
    return;
  }
  void response.body.cancel().catch(() => undefined);
}

async function successfulJson(response: Response): Promise<unknown> {
  if (!hasJsonContentType(response)) {
    cancelUnlockedResponseBody(response);
    throw unexpectedResponse(response.status);
  }
  try {
    return await response.json();
  } catch {
    throw unexpectedResponse(response.status);
  }
}

function isUuid(value: unknown): value is string {
  return (
    typeof value === "string" &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value)
  );
}

function isReplayFallback(value: unknown): value is ReplayFixtureFallback {
  return (
    isRecord(value) &&
    hasExactKeys(value, ["kind", "scenarioId", "executionMode"]) &&
    value.kind === "show_replay_fixture" &&
    value.scenarioId === "hotel" &&
    value.executionMode === "replay_fixture"
  );
}

async function failedRequest(
  response: Response,
  options: {
    allowFallback?: boolean;
    allowCreationBudget?: boolean;
    allowRecoveryStoreUnavailable?: boolean;
    allowStreamCapacity?: boolean;
    expectedRecoveryId?: string | null;
  } = {},
): Promise<PublicApiError> {
  if (!hasJsonContentType(response)) {
    cancelUnlockedResponseBody(response);
    return unexpectedResponse(response.status);
  }
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    return unexpectedResponse(response.status);
  }
  if (!isRecord(body) || !hasExactKeys(body, ["error"]) || !isRecord(body.error)) {
    return unexpectedResponse(response.status);
  }
  const error = body.error;
  if (
    !hasExactKeys(error, [
      "code",
      "message",
      "requestId",
      "recoveryId",
      "retryAfterSeconds",
      "fallback",
    ]) ||
    typeof error.code !== "string" ||
    !publicErrorCodes.has(error.code) ||
    typeof error.message !== "string" ||
    error.message !== publicMessages[error.code as PublicErrorCode] ||
    typeof error.requestId !== "string" ||
    !/^req_[0-9a-f]{32}$/.test(error.requestId) ||
    !(error.recoveryId === null || isUuid(error.recoveryId)) ||
    !(
      error.retryAfterSeconds === null ||
      (Number.isInteger(error.retryAfterSeconds) && Number(error.retryAfterSeconds) >= 0)
    )
  ) {
    return unexpectedResponse(response.status);
  }
  const code = error.code as PublicErrorCode;
  const isExactRecoveryStoreUnavailable =
    options.allowRecoveryStoreUnavailable === true &&
    code === "internal_error" &&
    response.status === 503 &&
    isUuid(options.expectedRecoveryId) &&
    error.recoveryId === options.expectedRecoveryId &&
    error.retryAfterSeconds === 1 &&
    error.fallback === null &&
    response.headers.get("Retry-After") === "1" &&
    hasJsonContentType(response);
  if (
    options.allowRecoveryStoreUnavailable === true &&
    code === "internal_error" &&
    response.status === 503 &&
    !isExactRecoveryStoreUnavailable
  ) {
    return unexpectedResponse(response.status);
  }
  if (
    options.expectedRecoveryId !== undefined &&
    error.recoveryId !== options.expectedRecoveryId
  ) {
    return unexpectedResponse(response.status);
  }
  if (
    code === "creation_daily_budget_exceeded" &&
    (options.allowCreationBudget !== true ||
      response.status !== 429 ||
      error.recoveryId !== null ||
      !Number.isInteger(error.retryAfterSeconds) ||
      Number(error.retryAfterSeconds) < 1 ||
      Number(error.retryAfterSeconds) > 86_400 ||
      error.fallback !== null ||
      response.headers.get("Retry-After") !==
        String(error.retryAfterSeconds))
  ) {
    return unexpectedResponse(response.status);
  }
  if (
    code === "stream_capacity_reached" &&
    (options.allowStreamCapacity !== true ||
      response.status !== 429 ||
      options.expectedRecoveryId === undefined ||
      error.recoveryId !== options.expectedRecoveryId ||
      !Number.isInteger(error.retryAfterSeconds) ||
      Number(error.retryAfterSeconds) < 1 ||
      Number(error.retryAfterSeconds) > 300 ||
      error.fallback !== null ||
      response.headers.get("Retry-After") !==
        String(error.retryAfterSeconds))
  ) {
    return unexpectedResponse(response.status);
  }
  if (
    code === "live_timeout" &&
    (response.status !== 504 || error.retryAfterSeconds !== null)
  ) {
    return unexpectedResponse(response.status);
  }
  let fallback: ReplayFixtureFallback | null = null;
  if (error.fallback !== null) {
    if (
      options.allowFallback !== true ||
      !fallbackCodes.has(code) ||
      !isReplayFallback(error.fallback)
    ) {
      return unexpectedResponse(response.status);
    }
    fallback = error.fallback;
  } else if (options.allowFallback === true && fallbackCodes.has(code)) {
    return unexpectedResponse(response.status);
  }
  return new PublicApiError(error.message, response.status, {
    code,
    requestId: error.requestId,
    recoveryId: error.recoveryId,
    retryAfterSeconds:
      error.retryAfterSeconds === null ? null : Number(error.retryAfterSeconds),
    fallback,
  });
}

export async function readRecoveryEventStreamFailure(
  response: Response,
  recoveryId: string,
): Promise<PublicApiError> {
  const contentType = response.headers.get("Content-Type");
  if (
    response.ok ||
    contentType === null ||
    contentType.split(";", 1)[0]?.trim().toLowerCase() !== "application/json"
  ) {
    return unexpectedResponse(response.status);
  }
  return failedRequest(response, {
    allowStreamCapacity: true,
    expectedRecoveryId: recoveryId,
  });
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
    hasExactKeys(candidate, [
      "backend",
      "liveReady",
      "sdkStubReady",
      "providerBoundary",
    ]) &&
    (candidate.backend === "openai" || candidate.backend === "stub") &&
    typeof candidate.liveReady === "boolean" &&
    typeof candidate.sdkStubReady === "boolean" &&
    candidate.providerBoundary === "demo_adapter_only"
  );
}

export async function getHealth(signal?: AbortSignal): Promise<HealthStatus> {
  const response = await fetch("/health", {
    headers: { Accept: "application/json" },
    credentials: "same-origin",
    signal,
    cache: "no-store",
  });

  if (!response.ok) {
    throw await failedRequest(response);
  }

  const body = await successfulJson(response);
  if (!isHealthStatus(body)) {
    throw unexpectedResponse(response.status);
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
      "rootTraceId",
      "modelIds",
      "sdkVersion",
      "protocolVersion",
      "agentGraphVersion",
      "promptToolSchemaHash",
    ])
  ) {
    return false;
  }
  if (!isStringArray(value.modelIds)) {
    return false;
  }
  const modelIds = value.modelIds;
  const statusIsValid =
    value.status === "in_progress" ||
    value.status === "pending_approval" ||
    value.status === "completed" ||
    value.status === "closed_without_action" ||
    value.status === "outcome_unknown";
  const terminalStepIsValid =
    (value.status !== "completed" &&
      value.status !== "closed_without_action" &&
      value.status !== "outcome_unknown") ||
    value.currentStep === 5;
  const approvalIsValid =
    value.pendingApproval === null || isPendingApproval(value.pendingApproval);
  const provenanceIsValid =
    (value.rootTraceId === null || typeof value.rootTraceId === "string") &&
    isStringArray(modelIds) &&
    (value.sdkVersion === null || typeof value.sdkVersion === "string") &&
    (value.protocolVersion === null || typeof value.protocolVersion === "string") &&
    (value.agentGraphVersion === null || typeof value.agentGraphVersion === "string") &&
    (value.promptToolSchemaHash === null ||
      (typeof value.promptToolSchemaHash === "string" &&
        /^[0-9a-f]{64}$/.test(value.promptToolSchemaHash)));
  const liveProvenanceIsValid =
    value.executionMode !== "openai_live" ||
    (typeof value.rootTraceId === "string" &&
      /^trace_[0-9a-f]{32}$/.test(value.rootTraceId) &&
      modelIds.length > 0 &&
      typeof value.sdkVersion === "string" &&
      typeof value.protocolVersion === "string" &&
      typeof value.agentGraphVersion === "string" &&
      typeof value.promptToolSchemaHash === "string");
  return (
    isUuid(value.recoveryId) &&
    (value.scenarioId === "hotel" || value.scenarioId === "api-quota") &&
    (value.executionMode === "openai_live" ||
      value.executionMode === "sdk_stub" ||
      value.executionMode === "replay_fixture") &&
    statusIsValid &&
    Number.isInteger(value.currentStep) &&
    Number(value.currentStep) >= 0 &&
    Number(value.currentStep) <= 5 &&
    terminalStepIsValid &&
    typeof value.currentStepSummary === "string" &&
    isUtcTimestamp(value.createdAt) &&
    isUtcTimestamp(value.updatedAt) &&
    approvalIsValid &&
    provenanceIsValid &&
    liveProvenanceIsValid &&
    (value.executionMode === "openai_live" || modelIds.length === 0) &&
    (value.status === "pending_approval" || value.pendingApproval === null)
  );
}

async function readRecovery(response: Response): Promise<RecoverySnapshot> {
  const body = await successfulJson(response);
  if (!isRecoverySnapshot(body)) {
    throw unexpectedResponse(response.status);
  }
  return body;
}

export async function getRecovery(
  recoveryId: string,
  signal?: AbortSignal,
): Promise<RecoverySnapshot> {
  const response = await fetch(`/api/recoveries/${encodeURIComponent(recoveryId)}`, {
    headers: { Accept: "application/json" },
    credentials: "same-origin",
    signal,
    cache: "no-store",
  });
  if (!response.ok) {
    throw await failedRequest(response, {
      allowRecoveryStoreUnavailable: true,
      expectedRecoveryId: recoveryId,
    });
  }
  const recovery = await readRecovery(response);
  if (recovery.recoveryId !== recoveryId) {
    throw unexpectedResponse(response.status);
  }
  return recovery;
}

function isSupportedCreationPair(
  scenarioId: ScenarioId,
  executionMode: ExecutionMode,
): boolean {
  return (
    (scenarioId === "hotel" &&
      (executionMode === "sdk_stub" ||
        executionMode === "replay_fixture" ||
        executionMode === "openai_live")) ||
    (scenarioId === "api-quota" &&
      (executionMode === "sdk_stub" ||
        executionMode === "replay_fixture"))
  );
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
    credentials: "same-origin",
    signal,
  });
  if (!response.ok) {
    throw await failedRequest(response, {
      allowFallback: executionMode === "openai_live",
      allowCreationBudget: isSupportedCreationPair(
        scenarioId,
        executionMode,
      ),
      expectedRecoveryId: null,
    });
  }
  const recovery = await readRecovery(response);
  if (
    recovery.scenarioId !== scenarioId ||
    recovery.executionMode !== executionMode
  ) {
    throw unexpectedResponse(response.status);
  }
  return recovery;
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
    isUuid(value.recoveryId) &&
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
    isUuid(value.recoveryId) &&
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
      credentials: "same-origin",
      signal,
    },
  );
  if (!response.ok) {
    throw await failedRequest(response, { expectedRecoveryId: recoveryId });
  }
  const body = await successfulJson(response);
  if (!isDecisionResponse(body)) {
    throw unexpectedResponse(response.status);
  }
  const responseDigest =
    body.decision === "approve"
      ? body.approvedRemedyDigest
      : body.decisionRemedyDigest;
  if (
    body.recoveryId !== recoveryId ||
    body.clientDecisionId !== decision.clientDecisionId ||
    body.decision !== decision.decision ||
    responseDigest !== decision.remedyDigest
  ) {
    throw unexpectedResponse(response.status);
  }
  return body;
}

function nullableDigest(value: unknown): value is `sha256:${string}` | null {
  return value === null || isSha256Digest(value);
}

function isQuotaEvidence(value: unknown): value is QuotaEvidence {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, [
      "providerCeilingRpm",
      "recordedDemandRpm",
      "temporaryBurstRpm",
      "region",
      "durationSeconds",
      "extraCostMinor",
      "delegatedAuthorityMaxMinor",
      "currency",
      "hardConstraints",
      "humanInterruptions",
      "approvals",
      "providerProofVerified",
      "grantVerified",
      "source",
      "revocationEvidenceKind",
      "protocolSteps",
    ]) ||
    !isRecord(value.hardConstraints) ||
    !hasExactKeys(value.hardConstraints, [
      "regionPreserved",
      "burstCoversDemand",
      "durationWithinLimit",
      "baseQuotaUnchanged",
    ])
  ) {
    return false;
  }
  const sourceIsValid =
    value.source === "sdk_simulator" || value.source === "recorded_fixture";
  const revocationMatchesSource =
    (value.source === "sdk_simulator" &&
      value.revocationEvidenceKind === "runtime_permission_revoked") ||
    (value.source === "recorded_fixture" &&
      value.revocationEvidenceKind === "recorded_revocation_only");
  return (
    value.providerCeilingRpm === 1000 &&
    value.recordedDemandRpm === 1200 &&
    value.temporaryBurstRpm === 1500 &&
    value.region === "US" &&
    value.durationSeconds === 900 &&
    value.extraCostMinor === 250 &&
    value.delegatedAuthorityMaxMinor === 500 &&
    value.currency === "USD" &&
    value.hardConstraints.regionPreserved === true &&
    value.hardConstraints.burstCoversDemand === true &&
    value.hardConstraints.durationWithinLimit === true &&
    value.hardConstraints.baseQuotaUnchanged === true &&
    value.humanInterruptions === 0 &&
    value.approvals === 0 &&
    value.providerProofVerified === true &&
    value.grantVerified === true &&
    sourceIsValid &&
    revocationMatchesSource &&
    Array.isArray(value.protocolSteps) &&
    value.protocolSteps.length === 6 &&
    value.protocolSteps[0] === "Detect" &&
    value.protocolSteps[1] === "Prove" &&
    value.protocolSteps[2] === "Negotiate" &&
    value.protocolSteps[3] === "Authorize" &&
    value.protocolSteps[4] === "Execute" &&
    value.protocolSteps[5] === "Verify & seal"
  );
}

function isRecoveryReceipt(value: unknown): value is RecoveryReceipt {
  const baseKeys = [
    "recoveryId",
    "executionMode",
    "status",
    "simulated",
    "providerExecution",
    "modelIds",
    "rootTraceId",
    "sdkVersion",
    "protocolVersion",
    "agentGraphVersion",
    "promptToolSchemaHash",
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
  ];
  if (
    !isRecord(value) ||
    !hasExactKeys(value, [...baseKeys, "quotaEvidence"])
  ) {
    return false;
  }
  if (!isStringArray(value.modelIds)) {
    return false;
  }
  const modelIds = value.modelIds;
  const executionModeIsValid =
    value.executionMode === "openai_live" ||
    value.executionMode === "sdk_stub" ||
      value.executionMode === "replay_fixture";
  const quotaEvidence = value.quotaEvidence;
  const statusIsTerminal =
    value.status === "completed" ||
    value.status === "closed_without_action" ||
    value.status === "outcome_unknown";
  const commonFieldsAreValid =
    isUuid(value.recoveryId) &&
    executionModeIsValid &&
    statusIsTerminal &&
    typeof value.simulated === "boolean" &&
    typeof value.providerExecution === "boolean" &&
    isStringArray(modelIds) &&
    (value.rootTraceId === null || typeof value.rootTraceId === "string") &&
    (value.sdkVersion === null || typeof value.sdkVersion === "string") &&
    (value.protocolVersion === null || typeof value.protocolVersion === "string") &&
    (value.agentGraphVersion === null || typeof value.agentGraphVersion === "string") &&
    (value.promptToolSchemaHash === null ||
      (typeof value.promptToolSchemaHash === "string" &&
        /^[0-9a-f]{64}$/.test(value.promptToolSchemaHash))) &&
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
  const quotaEvidenceIsValid =
    quotaEvidence === null || isQuotaEvidence(quotaEvidence);
  if (!commonFieldsAreValid || !quotaEvidenceIsValid) {
    return false;
  }

  if (value.executionMode === "openai_live") {
    if (
      typeof value.rootTraceId !== "string" ||
      !/^trace_[0-9a-f]{32}$/.test(value.rootTraceId) ||
      modelIds.length === 0 ||
      typeof value.sdkVersion !== "string" ||
      typeof value.protocolVersion !== "string" ||
      typeof value.agentGraphVersion !== "string" ||
      typeof value.promptToolSchemaHash !== "string" ||
      !value.permissionRevoked ||
      !value.scopeClosed ||
      value.decisionRemedyDigest === null ||
      quotaEvidence !== null
    ) {
      return false;
    }
  } else if (modelIds.length > 0) {
    return false;
  }

  if (value.executionMode === "replay_fixture") {
    return (
      value.status === "completed" &&
      value.simulated === true &&
      value.providerExecution === false &&
      modelIds.length === 0 &&
      value.rootTraceId === null &&
      value.sdkVersion === null &&
      value.protocolVersion === null &&
      value.agentGraphVersion === null &&
      value.promptToolSchemaHash === null &&
      value.decision === null &&
      value.decisionRemedyDigest === null &&
      value.executionCount === 0 &&
      value.providerDispatchStarted === false &&
      value.exactInterruptionRejected === false &&
      value.permissionRevoked === false &&
      value.scopeClosed === false &&
      value.approvedRemedyDigest === null &&
      (quotaEvidence === null || quotaEvidence.source === "recorded_fixture")
    );
  }
  if (quotaEvidence !== null) {
    return (
      quotaEvidence.source === "sdk_simulator" &&
      value.status === "completed" &&
      value.simulated === true &&
      value.providerExecution === true &&
      value.rootTraceId === null &&
      typeof value.sdkVersion === "string" &&
      value.sdkVersion.length > 0 &&
      value.protocolVersion === "backchannel.quota.v1" &&
      value.agentGraphVersion === "backchannel.quota-agent.v1" &&
      typeof value.promptToolSchemaHash === "string" &&
      value.decision === null &&
      value.decisionRemedyDigest === null &&
      value.executionCount === 1 &&
      value.providerDispatchStarted === true &&
      value.exactInterruptionRejected === false &&
      value.permissionRevoked === true &&
      value.scopeClosed === true &&
      value.approvedRemedyDigest === null
    );
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
    {
      headers: { Accept: "application/json" },
      credentials: "same-origin",
      signal,
      cache: "no-store",
    },
  );
  if (!response.ok) {
    throw await failedRequest(response, {
      allowRecoveryStoreUnavailable: true,
      expectedRecoveryId: recoveryId,
    });
  }
  const body = await successfulJson(response);
  if (!isRecoveryReceipt(body)) {
    throw unexpectedResponse(response.status);
  }
  if (body.recoveryId !== recoveryId) {
    throw unexpectedResponse(response.status);
  }
  return body;
}
