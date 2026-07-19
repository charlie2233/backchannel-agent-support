import { describe, expect, it, vi } from "vitest";

import { connectRecoveryEvents, parseRecoveryEvent } from "./events";

const recoveryId = "11111111-2222-4333-8444-555555555555";

function serializedEvent(overrides: Record<string, unknown> = {}): string {
  return JSON.stringify({
    recoveryId,
    seq: 1,
    type: "recovery.detected",
    terminal: false,
    data: { phase: "Detect", summary: "Booking conflict detected." },
    createdAt: "2026-07-19T12:00:00Z",
    ...overrides,
  });
}

describe("recovery event API validation", () => {
  it("accepts only the complete public event envelope", () => {
    expect(parseRecoveryEvent(serializedEvent())).toEqual({
      recoveryId,
      seq: 1,
      type: "recovery.detected",
      terminal: false,
      data: { phase: "Detect", summary: "Booking conflict detected." },
      createdAt: "2026-07-19T12:00:00Z",
    });
  });

  it.each([
    { leakedState: "serialized" },
    { createdAt: "July 19 at noon" },
    { data: [] },
    { seq: 0 },
  ])("rejects invalid or expanded event evidence %#", (override) => {
    expect(() => parseRecoveryEvent(serializedEvent(override))).toThrow(
      "Recovery event did not match the stream contract",
    );
  });

  it("keeps native reconnect active after a transient error and closes on cleanup", () => {
    const close = vi.fn();
    const source = { onmessage: null, onerror: null, close } as {
      onmessage: ((event: MessageEvent<string>) => void) | null;
      onerror: ((event: Event) => void) | null;
      close: () => void;
    };
    const onError = vi.fn();
    const disconnect = connectRecoveryEvents(recoveryId, { onEvent: vi.fn(), onError }, () => source);

    source.onerror?.(new Event("error"));
    expect(onError).toHaveBeenCalledOnce();
    expect(close).not.toHaveBeenCalled();

    disconnect();
    expect(close).toHaveBeenCalledOnce();
  });
});
