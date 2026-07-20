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
    const onError = vi.fn();
    const disconnect = connectRecoveryEvents(recoveryId, { onEvent: vi.fn(), onError }, () => source);

    source.onerror?.(new Event("error"));
    expect(onError).toHaveBeenCalledOnce();
    expect(close).not.toHaveBeenCalled();

    disconnect();
    expect(close).toHaveBeenCalledOnce();
  });

  it("treats an exact stream.capacity control as transient and keeps reconnect active", () => {
    const namedListeners = new Map<string, EventListener>();
    const close = vi.fn();
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
      addEventListener: vi.fn((type: string, listener: EventListener) => {
        namedListeners.set(type, listener);
      }),
      removeEventListener: vi.fn((type: string, listener: EventListener) => {
        if (namedListeners.get(type) === listener) {
          namedListeners.delete(type);
        }
      }),
    };
    const onEvent = vi.fn();
    const onError = vi.fn();
    const disconnect = connectRecoveryEvents(recoveryId, { onEvent, onError }, () => source);
    const capacityListener = namedListeners.get("stream.capacity");
    expect(capacityListener).toBeDefined();

    capacityListener?.(
      new MessageEvent("stream.capacity", {
        data: JSON.stringify({
          code: "event_stream_capacity",
          message: "Event streaming is temporarily at capacity; retry is automatic.",
          requestId: "0123456789abcdef0123456789abcdef",
        }),
      }),
    );

    expect(onError).toHaveBeenCalledOnce();
    expect(onError.mock.calls[0]?.[0]).toEqual(
      new Error("Event streaming is temporarily at capacity; retry is automatic."),
    );
    expect(close).not.toHaveBeenCalled();

    source.onerror?.(new Event("error"));
    expect(onError).toHaveBeenCalledOnce();

    source.onmessage?.(new MessageEvent("message", { data: serializedEvent() }));
    expect(onEvent).toHaveBeenCalledOnce();
    expect(close).not.toHaveBeenCalled();

    source.onerror?.(new Event("error"));
    expect(onError).toHaveBeenCalledTimes(2);
    expect(onError.mock.calls[1]?.[0]).toEqual(
      new Error("Recovery event stream disconnected"),
    );

    disconnect();
    expect(source.removeEventListener).toHaveBeenCalledWith(
      "stream.capacity",
      capacityListener,
    );
    expect(close).toHaveBeenCalledOnce();
  });

  it.each([
    { requestId: "NOT-LOWERCASE-HEX" },
    { recoveryId },
    { message: "different" },
  ])("rejects invalid or expanded stream.capacity controls %#", (override) => {
    const namedListeners = new Map<string, EventListener>();
    const source = {
      onmessage: null,
      onerror: null,
      close: vi.fn(),
      addEventListener: (type: string, listener: EventListener) => {
        namedListeners.set(type, listener);
      },
      removeEventListener: vi.fn(),
    };
    const onError = vi.fn();
    connectRecoveryEvents(recoveryId, { onEvent: vi.fn(), onError }, () => source);

    namedListeners.get("stream.capacity")?.(
      new MessageEvent("stream.capacity", {
        data: JSON.stringify({
          code: "event_stream_capacity",
          message: "Event streaming is temporarily at capacity; retry is automatic.",
          requestId: "0123456789abcdef0123456789abcdef",
          ...override,
        }),
      }),
    );

    expect(onError).toHaveBeenCalledOnce();
    expect(onError.mock.calls[0]?.[0]).toEqual(
      new Error("Stream capacity control did not match the public contract"),
    );
    expect(source.close).not.toHaveBeenCalled();
  });
});
