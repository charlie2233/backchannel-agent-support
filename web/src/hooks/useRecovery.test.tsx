import { describe, expect, it } from "vitest";

import type { RecoveryEvent } from "../api/events";
import { initialRecoveryState, recoveryReducer } from "./useRecovery";

function event(seq: number, type = `event.${seq}`): RecoveryEvent {
  return {
    recoveryId: "c9f6b65a-0ccf-4ef3-9d12-072e2660b852",
    seq,
    type,
    data: { summary: type },
    createdAt: "2026-07-18T12:00:00Z",
  };
}

describe("recoveryReducer", () => {
  it("deduplicates sequence IDs and preserves monotonically increasing server order", () => {
    const withSecond = recoveryReducer(initialRecoveryState, {
      type: "eventReceived",
      event: event(2),
    });
    const withFirst = recoveryReducer(withSecond, {
      type: "eventReceived",
      event: event(1),
    });
    const duplicate = recoveryReducer(withFirst, {
      type: "eventReceived",
      event: event(2, "duplicate.must.be.ignored"),
    });

    expect(duplicate.events.map(({ seq }) => seq)).toEqual([1, 2]);
    expect(duplicate.events[1]?.type).toBe("event.2");
    expect(duplicate.lastSeq).toBe(2);
  });
});
