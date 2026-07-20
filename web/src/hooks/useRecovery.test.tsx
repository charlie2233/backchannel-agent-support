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

  it("clears a transient stream-capacity error when a later event arrives", () => {
    const saturated = recoveryReducer(initialRecoveryState, {
      type: "failed",
      message: "Event streaming is temporarily at capacity; retry is automatic.",
    });

    const recovered = recoveryReducer(saturated, {
      type: "eventReceived",
      event: event(1),
    });

    expect(saturated.error).toBe(
      "Event streaming is temporarily at capacity; retry is automatic.",
    );
    expect(recovered.error).toBeNull();
    expect(recovered.events).toEqual([event(1)]);
  });

  it("delivers a terminal event once, closes once, and suppresses later disconnect errors", () => {
    const close = vi.fn<() => void>();
    const source: {
      onmessage: ((event: MessageEvent<string>) => void) | null;
      onerror: ((event: Event) => void) | null;
      close: () => void;
      addEventListener: (type: string, listener: EventListener) => void;
      removeEventListener: (type: string, listener: EventListener) => void;
    } = {
      onmessage: null,
      onerror: null,
      close,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
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
