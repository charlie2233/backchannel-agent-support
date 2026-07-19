import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  connectRecoveryEvents,
  type RecoveryEvent,
  type RecoveryEventHandlers,
} from "../api/events";
import type { RecoveryReceipt, RecoverySnapshot } from "../domain/recovery";
import { initialRecoveryState, recoveryReducer, useRecovery } from "./useRecovery";

const recoveryId = "c9f6b65a-0ccf-4ef3-9d12-072e2660b852";

function event(seq: number, type = `event.${seq}`): RecoveryEvent {
  return {
    recoveryId,
    seq,
    type,
    terminal: false,
    data: { summary: type },
    createdAt: "2026-07-18T12:00:00Z",
  };
}

function snapshot(
  status: RecoverySnapshot["status"],
  summary = "Server snapshot.",
): RecoverySnapshot {
  return {
    recoveryId,
    scenarioId: "hotel",
    executionMode: "sdk_stub",
    status,
    currentStep: status === "pending_approval" ? 3 : 5,
    currentStepSummary: summary,
    createdAt: "2026-07-18T12:00:00Z",
    updatedAt: "2026-07-18T12:00:01Z",
    pendingApproval: null,
  };
}

function declinedReceipt(): RecoveryReceipt {
  return {
    recoveryId,
    executionMode: "sdk_stub",
    status: "closed_without_action",
    simulated: true,
    providerExecution: false,
    modelIds: [],
    boundary: "Demo adapter boundary.",
    providerResult: "Exact interruption rejected before provider dispatch.",
    authorizationSource: "Explicit operator decline.",
    verificationResults: ["Exact interruption rejected.", "Permission scope revoked."],
    decision: "declined",
    decisionRemedyDigest: `sha256:${"a".repeat(64)}`,
    executionCount: 0,
    providerDispatchStarted: false,
    exactInterruptionRejected: true,
    permissionRevoked: true,
    scopeClosed: true,
    approvedRemedyDigest: null,
  };
}

afterEach(() => {
  vi.restoreAllMocks();
});

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
      recoveryId,
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

describe("useRecovery authoritative terminal refresh", () => {
  it("loads snapshot and receipt together exactly once after duplicate terminal delivery", async () => {
    const initial = snapshot("pending_approval", "Decision claim is durable.");
    const terminal = snapshot("closed_without_action", "Closed without provider action.");
    const receipt = declinedReceipt();
    const getRecovery = vi
      .fn()
      .mockResolvedValueOnce(initial)
      .mockResolvedValueOnce(terminal);
    const getReceipt = vi.fn().mockResolvedValue(receipt);
    let handlers: RecoveryEventHandlers | undefined;
    const disconnect = vi.fn();
    const connectEvents = vi.fn(
      (_activeRecoveryId: string, nextHandlers: RecoveryEventHandlers) => {
        handlers = nextHandlers;
        return disconnect;
      },
    );

    const { result, unmount } = renderHook(() =>
      useRecovery(recoveryId, { getRecovery, getReceipt, connectEvents }),
    );

    await waitFor(() => expect(result.current.snapshot).toEqual(initial));
    const terminalEvent = {
      ...event(8, "recovery.closed_without_action"),
      terminal: true,
    };
    act(() => {
      handlers?.onEvent(terminalEvent);
      handlers?.onEvent(terminalEvent);
    });

    await waitFor(() => {
      expect(result.current.snapshot).toEqual(terminal);
      expect(result.current.receipt).toEqual(receipt);
    });
    expect(getRecovery).toHaveBeenCalledTimes(2);
    expect(getReceipt).toHaveBeenCalledOnce();
    expect(result.current.events).toEqual([terminalEvent]);

    unmount();
    expect(disconnect).toHaveBeenCalledOnce();
  });

  it("fetches a receipt for an already-terminal recovery on reload", async () => {
    const terminal = snapshot("outcome_unknown", "Execution evidence needs review.");
    const receipt = {
      ...declinedReceipt(),
      status: "outcome_unknown" as const,
      providerExecution: true,
      providerDispatchStarted: true,
      executionCount: 1,
      providerResult: "Provider outcome could not be verified.",
    };
    const getRecovery = vi.fn().mockResolvedValue(terminal);
    const getReceipt = vi.fn().mockResolvedValue(receipt);
    const connectEvents = vi.fn(() => vi.fn());

    const { result } = renderHook(() =>
      useRecovery(recoveryId, { getRecovery, getReceipt, connectEvents }),
    );

    await waitFor(() => {
      expect(result.current.snapshot).toEqual(terminal);
      expect(result.current.receipt).toEqual(receipt);
    });
    expect(getRecovery).toHaveBeenCalledOnce();
    expect(getReceipt).toHaveBeenCalledOnce();
  });

  it.each(["snapshot", "receipt"] as const)(
    "retries the authoritative terminal pair after a transient %s failure",
    async (failedRequest) => {
      const initial = snapshot("pending_approval", "Decision claim is durable.");
      const terminal = snapshot("closed_without_action", "Closed without provider action.");
      const receipt = declinedReceipt();
      const getRecovery = vi
        .fn<(activeRecoveryId: string, signal?: AbortSignal) => Promise<RecoverySnapshot>>()
        .mockResolvedValueOnce(initial);
      const getReceipt = vi
        .fn<(activeRecoveryId: string, signal?: AbortSignal) => Promise<RecoveryReceipt>>();

      if (failedRequest === "snapshot") {
        getRecovery
          .mockRejectedValueOnce(new Error("Transient snapshot failure"))
          .mockResolvedValueOnce(terminal);
        getReceipt.mockResolvedValue(receipt);
      } else {
        getRecovery.mockResolvedValue(terminal);
        getReceipt
          .mockRejectedValueOnce(new Error("Transient receipt failure"))
          .mockResolvedValueOnce(receipt);
      }

      let handlers: RecoveryEventHandlers | undefined;
      const connectEvents = vi.fn(
        (_activeRecoveryId: string, nextHandlers: RecoveryEventHandlers) => {
          handlers = nextHandlers;
          return vi.fn();
        },
      );
      const { result } = renderHook(() =>
        useRecovery(recoveryId, {
          getRecovery,
          getReceipt,
          connectEvents,
          terminalRetryDelayMs: 0,
        }),
      );

      await waitFor(() => expect(result.current.snapshot).toEqual(initial));
      const terminalEvent = {
        ...event(9, "recovery.closed_without_action"),
        terminal: true,
      };
      act(() => handlers?.onEvent(terminalEvent));

      await waitFor(() => {
        expect(result.current.snapshot).toEqual(terminal);
        expect(result.current.receipt).toEqual(receipt);
        expect(result.current.error).toBeNull();
      });
      expect(getRecovery).toHaveBeenCalledTimes(3);
      expect(getReceipt).toHaveBeenCalledTimes(2);
      expect(result.current.events).toEqual([terminalEvent]);
    },
  );
});
