import type { DecisionRequest } from "./recovery";

export const ACTIVE_HOTEL_RECOVERY_KEY = "backchannel.hotelRecovery.v1";
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

function storage(): Storage | null {
  try {
    return typeof sessionStorage === "undefined" ? null : sessionStorage;
  } catch {
    return null;
  }
}

export function readActiveHotelRecovery(): string | null {
  try {
    const value = storage()?.getItem(ACTIVE_HOTEL_RECOVERY_KEY) ?? null;
    return value !== null && value.length > 0 ? value : null;
  } catch {
    return null;
  }
}

export function persistActiveHotelRecovery(recoveryId: string): boolean {
  try {
    const target = storage();
    if (target === null || recoveryId.length === 0) {
      return false;
    }
    target.setItem(ACTIVE_HOTEL_RECOVERY_KEY, recoveryId);
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
    if (
      !isRecord(value) ||
      !hasExactKeys(value, ["recoveryId", "request"]) ||
      typeof value.recoveryId !== "string" ||
      value.recoveryId.length === 0 ||
      !isDecisionRequest(value.request)
    ) {
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

export function clearPendingDecision(): void {
  try {
    storage()?.removeItem(PENDING_DECISION_KEY);
  } catch {
    // The terminal server state is still authoritative even if browser storage is unavailable.
  }
}
