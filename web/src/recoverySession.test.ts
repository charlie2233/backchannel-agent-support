import { afterEach, describe, expect, it, vi } from "vitest";

import {
  clearHotelRecoveryHint,
  readHotelRecoveryHint,
  writeHotelRecoveryHint,
} from "./recoverySession";

const VALID_ID = "11111111-2222-4333-8444-555555555555";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("hotel recovery session hint", () => {
  it("persists only canonical UUIDs and clears malformed hints", () => {
    const values = new Map<string, string>();
    const storage = {
      getItem: vi.fn((key: string) => values.get(key) ?? null),
      setItem: vi.fn((key: string, value: string) => values.set(key, value)),
      removeItem: vi.fn((key: string) => values.delete(key)),
    } as unknown as Storage;

    expect(writeHotelRecoveryHint(VALID_ID, storage)).toBe(true);
    expect(readHotelRecoveryHint(storage)).toEqual({ hadHint: true, recoveryId: VALID_ID });

    expect(writeHotelRecoveryHint("not-a-uuid", storage)).toBe(false);
    expect(
      writeHotelRecoveryHint(
        JSON.stringify({
          recoveryId: VALID_ID,
          action: "approve",
          remedyDigest: `sha256:${"a".repeat(64)}`,
          clientDecisionId: "must-not-be-stored",
        }),
        storage,
      ),
    ).toBe(false);
    expect(readHotelRecoveryHint(storage)).toEqual({ hadHint: false, recoveryId: null });

    storage.setItem("backchannel.hotelRecoveryId", "bad");
    expect(readHotelRecoveryHint(storage)).toEqual({ hadHint: true, recoveryId: null });
    expect(storage.removeItem).toHaveBeenCalled();
  });

  it.each(["get", "set", "remove"] as const)("fails closed when storage %s throws", (operation) => {
    const storage = {
      getItem: vi.fn(() => {
        if (operation === "get") throw new DOMException("denied", "SecurityError");
        return operation === "remove" ? "bad" : null;
      }),
      setItem: vi.fn(() => {
        if (operation === "set") throw new DOMException("full", "QuotaExceededError");
      }),
      removeItem: vi.fn(() => {
        if (operation === "remove") throw new DOMException("denied", "SecurityError");
      }),
    } as unknown as Storage;

    expect(() => readHotelRecoveryHint(storage)).not.toThrow();
    expect(() => writeHotelRecoveryHint(VALID_ID, storage)).not.toThrow();
    expect(() => clearHotelRecoveryHint(storage)).not.toThrow();
  });
});
