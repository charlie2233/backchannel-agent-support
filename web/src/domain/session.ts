import type { DecisionRequest } from "./recovery";
import type { ExecutionMode } from "./runtime";

export const ACTIVE_HOTEL_RECOVERY_KEY = "backchannel.hotelRecovery.v1";
export const ACTIVE_HOTEL_RECOVERY_MODE_KEY = "backchannel.hotelRecoveryMode.v1";
export const PENDING_DECISION_KEY = "backchannel.pendingDecision.v1";

export interface StoredDecisionClaim {
  recoveryId: string;
  request: DecisionRequest;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasExactKeys(value: Record<string, unknown>, expected: string[]): boolean {
  const actual = Object.keys(value).sort();
  const keys = [...expected].sort();
  return actual.length === keys.length && actual.every((key, index) => key === keys[index]);
}

function isRecoveryId(value: unknown): value is string {
  return (
    typeof value === "string" &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value)
  );
}

function isDecisionRequest(value: unknown): value is DecisionRequest {
  return (
    isRecord(value) &&
    hasExactKeys(value, [
      "decision",
      "clientDecisionId",
      "remedyId",
      "remedyDigest",
      "toolCallId",
    ]) &&
    (value.decision === "approve" || value.decision === "decline") &&
    typeof value.clientDecisionId === "string" &&
    value.clientDecisionId.length > 0 &&
    typeof value.remedyId === "string" &&
    value.remedyId.length > 0 &&
    typeof value.remedyDigest === "string" &&
    /^sha256:[0-9a-f]{64}$/.test(value.remedyDigest) &&
    typeof value.toolCallId === "string" &&
    value.toolCallId.length > 0
  );
}

function isStoredDecisionClaim(value: unknown): value is StoredDecisionClaim {
  return (
    isRecord(value) &&
    hasExactKeys(value, ["recoveryId", "request"]) &&
    typeof value.recoveryId === "string" &&
    value.recoveryId.length > 0 &&
    isDecisionRequest(value.request)
  );
}

function storage(): Storage | null {
  try {
    return typeof sessionStorage === "undefined" ? null : sessionStorage;
  } catch {
    return null;
  }
}

export function readActiveHotelRecovery(): string | null {
  try {
    const target = storage();
    const value = target?.getItem(ACTIVE_HOTEL_RECOVERY_KEY) ?? null;
    if (value === null) {
      return null;
    }
    if (!isRecoveryId(value)) {
      clearPendingDecisionForRecovery(value);
      target?.removeItem(ACTIVE_HOTEL_RECOVERY_KEY);
      target?.removeItem(ACTIVE_HOTEL_RECOVERY_MODE_KEY);
      return null;
    }
    return value;
  } catch {
    return null;
  }
}

export function readActiveHotelRecoveryMode(): ExecutionMode | null {
  try {
    const target = storage();
    const activeRecoveryId = target?.getItem(ACTIVE_HOTEL_RECOVERY_KEY);
    const value = target?.getItem(ACTIVE_HOTEL_RECOVERY_MODE_KEY);
    if (!isRecoveryId(activeRecoveryId)) {
      target?.removeItem(ACTIVE_HOTEL_RECOVERY_MODE_KEY);
      return null;
    }
    if (
      value === "openai_live" ||
      value === "sdk_stub" ||
      value === "replay_fixture"
    ) {
      return value;
    }
    target?.removeItem(ACTIVE_HOTEL_RECOVERY_MODE_KEY);
    return null;
  } catch {
    return null;
  }
}

export function persistActiveHotelRecovery(
  recoveryId: string,
  executionMode: ExecutionMode,
): boolean {
  try {
    const target = storage();
    if (target === null || !isRecoveryId(recoveryId)) {
      return false;
    }
    target.setItem(ACTIVE_HOTEL_RECOVERY_KEY, recoveryId);
    target.setItem(ACTIVE_HOTEL_RECOVERY_MODE_KEY, executionMode);
    return true;
  } catch {
    return false;
  }
}

export function clearActiveHotelRecovery(recoveryId: string): boolean {
  try {
    const target = storage();
    if (
      target === null ||
      target.getItem(ACTIVE_HOTEL_RECOVERY_KEY) !== recoveryId
    ) {
      return false;
    }
    target.removeItem(ACTIVE_HOTEL_RECOVERY_KEY);
    target.removeItem(ACTIVE_HOTEL_RECOVERY_MODE_KEY);
    return true;
  } catch {
    return false;
  }
}

export function readPendingDecision(): StoredDecisionClaim | null {
  try {
    const serialized = storage()?.getItem(PENDING_DECISION_KEY);
    if (serialized === null || serialized === undefined) {
      return null;
    }
    const value: unknown = JSON.parse(serialized);
    if (!isStoredDecisionClaim(value)) {
      return null;
    }
    return { recoveryId: value.recoveryId, request: value.request };
  } catch {
    return null;
  }
}

export function persistPendingDecision(claim: StoredDecisionClaim): boolean {
  try {
    const target = storage();
    if (target === null) {
      return false;
    }
    target.setItem(PENDING_DECISION_KEY, JSON.stringify(claim));
    return true;
  } catch {
    return false;
  }
}

export function clearPendingDecisionForRecovery(recoveryId: string): boolean {
  try {
    const target = storage();
    const serialized = target?.getItem(PENDING_DECISION_KEY);
    if (target === null || serialized === null || serialized === undefined) {
      return false;
    }
    const value: unknown = JSON.parse(serialized);
    if (!isStoredDecisionClaim(value) || value.recoveryId !== recoveryId) {
      return false;
    }
    target.removeItem(PENDING_DECISION_KEY);
    return true;
  } catch {
    return false;
  }
}

export function clearPendingDecision(): void {
  try {
    storage()?.removeItem(PENDING_DECISION_KEY);
  } catch {
    // The terminal server state is still authoritative even if browser storage is unavailable.
  }
}
