import { afterEach, describe, expect, it } from "vitest";

import {
  HOTEL_PENDING_CREATION_KEY,
  acquirePendingRecoveryCreation,
  clearMatchingPendingRecoveryCreation,
  readPendingRecoveryCreation,
} from "./creationSession";

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

afterEach(() => {
  window.sessionStorage.clear();
});

describe("pending recovery creation session contract", () => {
  it("persists an exact UUID intent, reuses its tuple, and refuses to overwrite it", () => {
    const first = acquirePendingRecoveryCreation(
      "hotel",
      "openai_live",
      window.sessionStorage,
    );

    expect(first).toEqual({
      clientRequestId: expect.stringMatching(UUID_PATTERN),
      scenarioId: "hotel",
      executionMode: "openai_live",
    });
    expect(Object.keys(first ?? {}).sort()).toEqual([
      "clientRequestId",
      "executionMode",
      "scenarioId",
    ]);
    expect(JSON.parse(window.sessionStorage.getItem(HOTEL_PENDING_CREATION_KEY) ?? "null"))
      .toEqual(first);

    expect(
      acquirePendingRecoveryCreation(
        "hotel",
        "openai_live",
        window.sessionStorage,
      ),
    ).toEqual(first);

    expect(
      acquirePendingRecoveryCreation(
        "hotel",
        "replay_fixture",
        window.sessionStorage,
      ),
    ).toBeNull();
    expect(
      readPendingRecoveryCreation("hotel", window.sessionStorage),
    ).toEqual(first);
  });

  it.each([
    ["extra field", {
      clientRequestId: "11111111-2222-4333-8444-555555555555",
      scenarioId: "hotel",
      executionMode: "openai_live",
      secret: "must-not-survive",
    }],
    ["missing field", {
      clientRequestId: "11111111-2222-4333-8444-555555555555",
      scenarioId: "hotel",
    }],
    ["uppercase UUID", {
      clientRequestId: "AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE",
      scenarioId: "hotel",
      executionMode: "openai_live",
    }],
    ["scenario mismatch", {
      clientRequestId: "11111111-2222-4333-8444-555555555555",
      scenarioId: "api-quota",
      executionMode: "sdk_stub",
    }],
    ["unsupported mode pairing", {
      clientRequestId: "11111111-2222-4333-8444-555555555555",
      scenarioId: "hotel",
      executionMode: "unsupported",
    }],
  ])("clears a stored intent with a %s", (_label, value) => {
    window.sessionStorage.setItem(
      HOTEL_PENDING_CREATION_KEY,
      JSON.stringify(value),
    );

    expect(
      readPendingRecoveryCreation("hotel", window.sessionStorage),
    ).toBeNull();
    expect(window.sessionStorage.getItem(HOTEL_PENDING_CREATION_KEY)).toBeNull();
  });

  it("clears only the exact intent that completed", () => {
    const first = acquirePendingRecoveryCreation(
      "hotel",
      "openai_live",
      window.sessionStorage,
    );
    const newer = {
      clientRequestId: "11111111-2222-4333-8444-555555555555",
      scenarioId: "hotel" as const,
      executionMode: "sdk_stub" as const,
    };
    expect(first).not.toBeNull();
    window.sessionStorage.setItem(
      HOTEL_PENDING_CREATION_KEY,
      JSON.stringify(newer),
    );

    expect(
      clearMatchingPendingRecoveryCreation(first!, window.sessionStorage),
    ).toBe(false);
    expect(
      readPendingRecoveryCreation("hotel", window.sessionStorage),
    ).toEqual(newer);
    expect(
      clearMatchingPendingRecoveryCreation(newer, window.sessionStorage),
    ).toBe(true);
    expect(
      readPendingRecoveryCreation("hotel", window.sessionStorage),
    ).toBeNull();
  });
});
