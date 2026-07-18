import { describe, expect, it, vi } from "vitest";

import { connectRecoveryEvents, type RecoveryEvent } from "../api/events";
import { initialRecoveryState, recoveryReducer } from "./useRecovery";

function event(seq: number, type = `event.${seq}`): RecoveryEvent {
  return {
    recoveryId: "c9f6b65a-0ccf-4ef3-9d12-072e2660b852",
    seq,
    type,
    terminal: false,
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

  it("delivers a terminal event once, closes once, and suppresses later disconnect errors", () => {
    const close = vi.fn<() => void>();
    const source: {
      onmessage: ((event: MessageEvent<string>) => void) | null;
      onerror: ((event: Event) => void) | null;
      close: () => void;
    } = {
      onmessage: null,
      onerror: null,
      close,
    };
    const onEvent = vi.fn();
    const onError = vi.fn();
    const disconnect = connectRecoveryEvents(
      "c9f6b65a-0ccf-4ef3-9d12-072e2660b852",
      { onEvent, onError },
      () => source,
    );
    const terminalEvent = { ...event(7), terminal: true };

    source.onmessage?.(
      new MessageEvent("message", { data: JSON.stringify(terminalEvent) }),
    );
    source.onerror?.(new Event("error"));
    disconnect();

    expect(onEvent).toHaveBeenCalledOnce();
    expect(onEvent).toHaveBeenCalledWith(terminalEvent);
    expect(close).toHaveBeenCalledOnce();
    expect(onError).not.toHaveBeenCalled();
  });
});
