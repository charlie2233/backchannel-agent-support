import type { ScenarioId } from "./domain/recovery";
import type { ExecutionMode } from "./domain/runtime";

export const HOTEL_PENDING_CREATION_KEY =
  "backchannel.pendingRecoveryCreation.v1.hotel";
export const API_QUOTA_PENDING_CREATION_KEY =
  "backchannel.pendingRecoveryCreation.v1.api-quota";

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const INTENT_FIELDS = [
  "clientRequestId",
  "executionMode",
  "scenarioId",
] as const;

export interface PendingRecoveryCreation {
  clientRequestId: string;
  scenarioId: ScenarioId;
  executionMode: ExecutionMode;
}

function browserSessionStorage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.sessionStorage;
  } catch {
    return null;
  }
}

function storageKey(scenarioId: ScenarioId): string {
  return scenarioId === "hotel"
    ? HOTEL_PENDING_CREATION_KEY
    : API_QUOTA_PENDING_CREATION_KEY;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isSupportedPair(
  scenarioId: ScenarioId,
  executionMode: ExecutionMode,
): boolean {
  return scenarioId === "hotel"
    ? executionMode === "openai_live" ||
        executionMode === "sdk_stub" ||
        executionMode === "replay_fixture"
    : executionMode === "sdk_stub";
}

function parseIntent(
  value: unknown,
  expectedScenarioId: ScenarioId,
): PendingRecoveryCreation | null {
  if (!isRecord(value)) return null;
  const keys = Object.keys(value).sort();
  if (
    keys.length !== INTENT_FIELDS.length ||
    !keys.every((key, index) => key === INTENT_FIELDS[index])
  ) {
    return null;
  }
  if (
    typeof value.clientRequestId !== "string" ||
    !UUID_PATTERN.test(value.clientRequestId) ||
    value.scenarioId !== expectedScenarioId ||
    (value.executionMode !== "openai_live" &&
      value.executionMode !== "sdk_stub" &&
      value.executionMode !== "replay_fixture") ||
    !isSupportedPair(expectedScenarioId, value.executionMode)
  ) {
    return null;
  }
  return {
    clientRequestId: value.clientRequestId,
    scenarioId: expectedScenarioId,
    executionMode: value.executionMode,
  };
}

export function pendingRecoveryCreationsMatch(
  left: PendingRecoveryCreation,
  right: PendingRecoveryCreation,
): boolean {
  return (
    left.clientRequestId === right.clientRequestId &&
    left.scenarioId === right.scenarioId &&
    left.executionMode === right.executionMode
  );
}

export function readPendingRecoveryCreation(
  scenarioId: ScenarioId,
  storage: Storage | null = browserSessionStorage(),
): PendingRecoveryCreation | null {
  if (storage === null) return null;
  const key = storageKey(scenarioId);
  let raw: string | null;
  try {
    raw = storage.getItem(key);
  } catch {
    return null;
  }
  if (raw === null) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    parsed = null;
  }
  const intent = parseIntent(parsed, scenarioId);
  if (intent !== null) return intent;
  try {
    storage.removeItem(key);
  } catch {
    // Invalid storage is never trusted even when the browser denies cleanup.
  }
  return null;
}

export function acquirePendingRecoveryCreation(
  scenarioId: ScenarioId,
  executionMode: ExecutionMode,
  storage: Storage | null = browserSessionStorage(),
): PendingRecoveryCreation | null {
  if (storage === null || !isSupportedPair(scenarioId, executionMode)) {
    return null;
  }
  const existing = readPendingRecoveryCreation(scenarioId, storage);
  if (existing !== null) {
    return existing.executionMode === executionMode ? existing : null;
  }

  let clientRequestId: string;
  try {
    clientRequestId = globalThis.crypto.randomUUID();
  } catch {
    return null;
  }
  if (!UUID_PATTERN.test(clientRequestId)) return null;
  const intent: PendingRecoveryCreation = {
    clientRequestId,
    scenarioId,
    executionMode,
  };
  try {
    storage.setItem(storageKey(scenarioId), JSON.stringify(intent));
  } catch {
    return null;
  }
  return intent;
}

export function clearMatchingPendingRecoveryCreation(
  intent: PendingRecoveryCreation,
  storage: Storage | null = browserSessionStorage(),
): boolean {
  if (storage === null) return false;
  const current = readPendingRecoveryCreation(intent.scenarioId, storage);
  if (
    current === null ||
    !pendingRecoveryCreationsMatch(current, intent)
  ) {
    return false;
  }
  try {
    storage.removeItem(storageKey(intent.scenarioId));
    return true;
  } catch {
    return false;
  }
}
