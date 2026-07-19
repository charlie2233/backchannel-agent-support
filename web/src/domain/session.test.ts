import { afterEach, describe, expect, it } from "vitest";

import {
  ACTIVE_HOTEL_RECOVERY_KEY,
  clearActiveHotelRecovery,
  clearPendingDecisionForRecovery,
  PENDING_DECISION_KEY,
  readActiveHotelRecovery,
} from "./session";

const recoveryId = "11111111-2222-4333-8444-555555555555";
const otherRecoveryId = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
const digest = `sha256:${"a".repeat(64)}`;

function storedClaim(activeRecoveryId: string) {
  return {
    recoveryId: activeRecoveryId,
    request: {
      decision: "decline",
      clientDecisionId: "decision-session-contract",
      remedyId: "remedy-session-contract",
      remedyDigest: digest,
      toolCallId: "call-session-contract",
    },
  };
}

afterEach(() => {
  sessionStorage.clear();
});

describe("recovery session storage", () => {
  it.each([
    "",
    "not-a-recovery-id",
    "../../api/recoveries",
    " 11111111-2222-4333-8444-555555555555 ",
    "11111111-2222-4333-8444-55555555555",
  ])("synchronously evicts malformed active recovery id %j", (malformedId) => {
    sessionStorage.setItem(ACTIVE_HOTEL_RECOVERY_KEY, malformedId);

    expect(readActiveHotelRecovery()).toBeNull();
    expect(sessionStorage.getItem(ACTIVE_HOTEL_RECOVERY_KEY)).toBeNull();
  });

  it("retains a canonical active recovery id", () => {
    sessionStorage.setItem(ACTIVE_HOTEL_RECOVERY_KEY, recoveryId);

    expect(readActiveHotelRecovery()).toBe(recoveryId);
    expect(sessionStorage.getItem(ACTIVE_HOTEL_RECOVERY_KEY)).toBe(recoveryId);
  });

  it("evicts only a valid pending claim matching a malformed active recovery id", () => {
    const malformedId = "not-a-recovery-id";
    const matchingClaim = storedClaim(malformedId);
    sessionStorage.setItem(ACTIVE_HOTEL_RECOVERY_KEY, malformedId);
    sessionStorage.setItem(PENDING_DECISION_KEY, JSON.stringify(matchingClaim));

    expect(readActiveHotelRecovery()).toBeNull();
    expect(sessionStorage.getItem(ACTIVE_HOTEL_RECOVERY_KEY)).toBeNull();
    expect(sessionStorage.getItem(PENDING_DECISION_KEY)).toBeNull();
  });

  it("preserves an unmatched pending claim while evicting a malformed active recovery id", () => {
    const otherClaim = storedClaim(otherRecoveryId);
    sessionStorage.setItem(ACTIVE_HOTEL_RECOVERY_KEY, "not-a-recovery-id");
    sessionStorage.setItem(PENDING_DECISION_KEY, JSON.stringify(otherClaim));

    expect(readActiveHotelRecovery()).toBeNull();
    expect(sessionStorage.getItem(ACTIVE_HOTEL_RECOVERY_KEY)).toBeNull();
    expect(sessionStorage.getItem(PENDING_DECISION_KEY)).toBe(JSON.stringify(otherClaim));
  });

  it("conditionally clears only matching active recovery and pending claim records", () => {
    const otherClaim = storedClaim(otherRecoveryId);
    sessionStorage.setItem(ACTIVE_HOTEL_RECOVERY_KEY, otherRecoveryId);
    sessionStorage.setItem(PENDING_DECISION_KEY, JSON.stringify(otherClaim));

    expect(clearActiveHotelRecovery(recoveryId)).toBe(false);
    expect(clearPendingDecisionForRecovery(recoveryId)).toBe(false);
    expect(sessionStorage.getItem(ACTIVE_HOTEL_RECOVERY_KEY)).toBe(otherRecoveryId);
    expect(sessionStorage.getItem(PENDING_DECISION_KEY)).toBe(JSON.stringify(otherClaim));

    expect(clearActiveHotelRecovery(otherRecoveryId)).toBe(true);
    expect(clearPendingDecisionForRecovery(otherRecoveryId)).toBe(true);
    expect(sessionStorage.getItem(ACTIVE_HOTEL_RECOVERY_KEY)).toBeNull();
    expect(sessionStorage.getItem(PENDING_DECISION_KEY)).toBeNull();
  });
});
