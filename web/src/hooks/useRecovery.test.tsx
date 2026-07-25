import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  type RecoveryEvent,
  type RecoveryEventStreamHandlers,
  type RecoveryEventStreamRequest,
  type RecoveryEventStreamResult,
} from "../api/events";
import { HttpStatusError, PublicApiError } from "../api/client";
import type { RecoveryReceipt, RecoverySnapshot } from "../domain/recovery";
import { initialRecoveryState, recoveryReducer, useRecovery } from "./useRecovery";

const recoveryId = "c9f6b65a-0ccf-4ef3-9d12-072e2660b852";
const sdkRootTraceId = "qa_trace_11111111111111111111111111111111";
const sdkDefinitionDigest = "b".repeat(64);

type OpenEvents = (
  request: RecoveryEventStreamRequest,
  handlers: RecoveryEventStreamHandlers,
) => Promise<RecoveryEventStreamResult>;

function pendingOpenEvents() {
  return vi.fn<OpenEvents>(() => new Promise(() => undefined));
}

function capacityError(): PublicApiError {
  return new PublicApiError(
    "The event stream is currently at capacity.",
    429,
    {
      code: "stream_capacity_reached",
      requestId: "req_11111111111111111111111111111111",
      recoveryId,
      retryAfterSeconds: 1,
      fallback: null,
    },
  );
}

async function flushAsyncWork(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((nextResolve, nextReject) => {
    resolve = nextResolve;
    reject = nextReject;
  });
  return { promise, reject, resolve };
}

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
    rootTraceId: sdkRootTraceId,
    modelIds: [],
    sdkVersion: "0.18.3",
    protocolVersion: "backchannel.approval.v1",
    agentGraphVersion: "backchannel.hotel-agent.v1",
    promptToolSchemaHash: sdkDefinitionDigest,
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
    rootTraceId: sdkRootTraceId,
    sdkVersion: "0.18.3",
    protocolVersion: "backchannel.approval.v1",
    agentGraphVersion: "backchannel.hotel-agent.v1",
    promptToolSchemaHash: sdkDefinitionDigest,
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
    quotaEvidence: null,
  };
}

function quotaTerminalSnapshot(
  executionMode: "sdk_stub" | "replay_fixture" = "sdk_stub",
): RecoverySnapshot {
  return {
    recoveryId,
    scenarioId: "api-quota",
    executionMode,
    status: "completed",
    currentStep: 5,
    currentStepSummary: "Quota receipt sealed.",
    createdAt: "2026-07-19T12:00:00Z",
    updatedAt: "2026-07-19T12:00:01Z",
    pendingApproval: null,
    rootTraceId: null,
    modelIds: [],
    sdkVersion: executionMode === "sdk_stub" ? "0.18.3" : null,
    protocolVersion: executionMode === "sdk_stub" ? "backchannel.quota.v1" : null,
    agentGraphVersion:
      executionMode === "sdk_stub" ? "backchannel.quota-agent.v1" : null,
    promptToolSchemaHash: executionMode === "sdk_stub" ? "c".repeat(64) : null,
  };
}

function quotaTerminalReceipt(
  executionMode: "sdk_stub" | "replay_fixture" = "sdk_stub",
): RecoveryReceipt {
  const runtime = executionMode === "sdk_stub";
  return {
    recoveryId,
    executionMode,
    status: "completed",
    simulated: true,
    providerExecution: runtime,
    modelIds: [],
    rootTraceId: null,
    sdkVersion: runtime ? "0.18.3" : null,
    protocolVersion: runtime ? "backchannel.quota.v1" : null,
    agentGraphVersion: runtime ? "backchannel.quota-agent.v1" : null,
    promptToolSchemaHash: runtime ? "c".repeat(64) : null,
    boundary: "Quota boundary.",
    providerResult: "Quota result.",
    authorizationSource: "Quota authority.",
    verificationResults: ["Quota verification."],
    decision: null,
    decisionRemedyDigest: null,
    executionCount: runtime ? 1 : 0,
    providerDispatchStarted: runtime,
    exactInterruptionRejected: false,
    permissionRevoked: runtime,
    scopeClosed: runtime,
    approvedRemedyDigest: null,
    quotaEvidence: {
      providerCeilingRpm: 1000,
      recordedDemandRpm: 1200,
      temporaryBurstRpm: 1500,
      region: "US",
      durationSeconds: 900,
      extraCostMinor: 250,
      delegatedAuthorityMaxMinor: 500,
      currency: "USD",
      hardConstraints: {
        regionPreserved: true,
        burstCoversDemand: true,
        durationWithinLimit: true,
        baseQuotaUnchanged: true,
      },
      humanInterruptions: 0,
      approvals: 0,
      providerProofVerified: true,
      grantVerified: true,
      source: runtime ? "sdk_simulator" : "recorded_fixture",
      revocationEvidenceKind: runtime
        ? "runtime_permission_revoked"
        : "recorded_revocation_only",
      protocolSteps: [
        "Detect",
        "Prove",
        "Negotiate",
        "Authorize",
        "Execute",
        "Verify & seal",
      ],
    },
  };
}

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("recoveryReducer", () => {
  it("accepts only the next sequence and ignores duplicate, regressed, or gapped dispatches", () => {
    const withFirst = recoveryReducer(initialRecoveryState, {
      type: "eventReceived",
      event: event(1),
    });
    const withSecond = recoveryReducer(withFirst, {
      type: "eventReceived",
      event: event(2),
    });
    const duplicate = recoveryReducer(withSecond, {
      type: "eventReceived",
      event: event(2, "duplicate.must.be.ignored"),
    });
    const regression = recoveryReducer(duplicate, {
      type: "eventReceived",
      event: event(1, "regression.must.be.ignored"),
    });
    const gap = recoveryReducer(regression, {
      type: "eventReceived",
      event: event(4, "gap.must.be.ignored"),
    });

    expect(gap.events.map(({ seq }) => seq)).toEqual([1, 2]);
    expect(gap.events[1]?.type).toBe("event.2");
    expect(gap.lastSeq).toBe(2);
  });

  it("keeps an events-phase error when an authoritative snapshot loads", () => {
    const failed = recoveryReducer(initialRecoveryState, {
      type: "failed",
      message: "The event stream is currently at capacity.",
      status: 429,
      phase: "events",
    });
    const loaded = recoveryReducer(failed, {
      type: "snapshotLoaded",
      snapshot: snapshot("pending_approval"),
    });

    expect(loaded.snapshot).toEqual(snapshot("pending_approval"));
    expect(loaded.loading).toBe(false);
    expect(loaded.error).toBe(
      "The event stream is currently at capacity.",
    );
    expect(loaded.errorStatus).toBe(429);
    expect(loaded.errorPhase).toBe("events");
  });

  it("does not let an events failure replace an initial or terminal evidence failure", () => {
    for (const phase of ["initial", "terminal"] as const) {
      const authoritativeFailure = recoveryReducer(initialRecoveryState, {
        type: "failed",
        message: `${phase} evidence failed`,
        status: 503,
        phase,
      });
      const streamFailure = recoveryReducer(authoritativeFailure, {
        type: "failed",
        message: "Recovery event stream disconnected.",
        status: null,
        phase: "events",
      });

      expect(streamFailure.error).toBe(`${phase} evidence failed`);
      expect(streamFailure.errorPhase).toBe(phase);
      expect(streamFailure.errorStatus).toBe(503);
    }
  });
});

describe("useRecovery event-stream recovery", () => {
  it("keeps a typed capacity error when a concurrent authoritative snapshot finishes", async () => {
    vi.useFakeTimers();
    let resolveSnapshot: ((value: RecoverySnapshot) => void) | undefined;
    const getRecovery = vi.fn(
      () =>
        new Promise<RecoverySnapshot>((resolve) => {
          resolveSnapshot = resolve;
        }),
    );
    const openEvents = vi
      .fn<OpenEvents>()
      .mockRejectedValue(capacityError());
    const { result } = renderHook(() =>
      useRecovery(recoveryId, { getRecovery, openEvents }),
    );

    await act(flushAsyncWork);
    expect(result.current.errorPhase).toBe("events");
    expect(result.current.error).toBe(
      "The event stream is currently at capacity.",
    );
    expect(result.current.loading).toBe(true);

    await act(async () => {
      resolveSnapshot?.(snapshot("pending_approval"));
      await flushAsyncWork();
    });

    expect(result.current.snapshot).toEqual(snapshot("pending_approval"));
    expect(result.current.loading).toBe(false);
    expect(result.current.errorPhase).toBe("events");
    expect(result.current.eventsRetryAvailable).toBe(false);
    expect(result.current.eventsRetryAfterSeconds).toBe(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(999);
    });
    expect(result.current.eventsRetryAvailable).toBe(false);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(result.current.eventsRetryAvailable).toBe(true);
    expect(result.current.eventsRetryAfterSeconds).toBeNull();
    expect(openEvents).toHaveBeenCalledOnce();
  });

  it("automatically reconnects boundedly after EOF with the accepted cursor", async () => {
    const initial = snapshot("pending_approval");
    const terminal = snapshot(
      "closed_without_action",
      "Closed without provider action.",
    );
    const receipt = declinedReceipt();
    const first = event(1);
    const last = { ...event(2), terminal: true };
    const requests: RecoveryEventStreamRequest[] = [];
    const openEvents = vi
      .fn<OpenEvents>()
      .mockImplementationOnce(async (request, handlers) => {
        requests.push(request);
        handlers.onOpen();
        handlers.onEvent(first);
        return { kind: "eof", lastSeq: 1, lastEvent: first };
      })
      .mockImplementationOnce(async (request, handlers) => {
        requests.push(request);
        handlers.onOpen();
        handlers.onEvent(last);
        return { kind: "terminal", lastSeq: 2, lastEvent: last };
      });
    const getRecovery = vi
      .fn()
      .mockResolvedValueOnce(initial)
      .mockResolvedValueOnce(terminal);
    const getReceipt = vi.fn().mockResolvedValue(receipt);

    const { result } = renderHook(() =>
      useRecovery(recoveryId, {
        getRecovery,
        getReceipt,
        openEvents,
        eventReconnectDelayMs: 0,
        eventReconnectAttempts: 2,
      }),
    );

    await waitFor(() => expect(openEvents).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(result.current.receipt).toEqual(receipt));
    expect(requests[0]).toMatchObject({ afterSeq: 0, lastEvent: null });
    expect(requests[1]).toMatchObject({ afterSeq: 1, lastEvent: first });
    expect(result.current.events).toEqual([first, last]);
    expect(result.current.error).toBeNull();
  });

  it("bounds network retries and redacts the exhausted transport failure", async () => {
    const openEvents = vi
      .fn<OpenEvents>()
      .mockRejectedValue(new TypeError("private loopback reset details"));
    const getRecovery = vi.fn().mockResolvedValue(snapshot("pending_approval"));
    const { result } = renderHook(() =>
      useRecovery(recoveryId, {
        getRecovery,
        openEvents,
        eventReconnectDelayMs: 0,
        eventReconnectAttempts: 2,
      }),
    );

    await waitFor(() => expect(openEvents).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(result.current.errorPhase).toBe("events"));
    expect(result.current.snapshot).toEqual(snapshot("pending_approval"));
    expect(result.current.error).toBe("Recovery event stream disconnected.");
    expect(result.current.error).not.toContain("loopback");
    expect(result.current.eventsRetryAvailable).toBe(true);
  });

  it("honors the capacity hint, coalesces manual retries, refreshes by GET, and retains the cursor until verified open", async () => {
    vi.useFakeTimers();
    const initial = snapshot("pending_approval");
    const first = event(1);
    let retryHandlers: RecoveryEventStreamHandlers | undefined;
    let retryRequest: RecoveryEventStreamRequest | undefined;
    const openEvents = vi
      .fn<OpenEvents>()
      .mockImplementationOnce(async (_request, handlers) => {
        handlers.onOpen();
        handlers.onEvent(first);
        return { kind: "eof", lastSeq: 1, lastEvent: first };
      })
      .mockRejectedValueOnce(capacityError())
      .mockImplementationOnce(
        (request, handlers) =>
          new Promise<RecoveryEventStreamResult>(() => {
            retryRequest = request;
            retryHandlers = handlers;
          }),
      );
    const getRecovery = vi.fn().mockResolvedValue(initial);
    const getReceipt = vi.fn();
    const { result } = renderHook(() =>
      useRecovery(recoveryId, {
        getRecovery,
        getReceipt,
        openEvents,
        eventReconnectDelayMs: 0,
        eventReconnectAttempts: 1,
      }),
    );

    await act(flushAsyncWork);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
      await flushAsyncWork();
    });
    expect(openEvents).toHaveBeenCalledTimes(2);
    expect(result.current.errorPhase).toBe("events");
    expect(result.current.events).toEqual([first]);
    expect(result.current.eventsRetryAvailable).toBe(false);

    act(() => {
      result.current.retryEvents();
      result.current.retryEvents();
    });
    await act(flushAsyncWork);
    expect(getRecovery).toHaveBeenCalledOnce();
    expect(openEvents).toHaveBeenCalledTimes(2);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    expect(result.current.eventsRetryAvailable).toBe(true);

    act(() => {
      result.current.retryEvents();
      result.current.retryEvents();
    });
    await act(flushAsyncWork);

    expect(getRecovery).toHaveBeenCalledTimes(2);
    expect(getReceipt).not.toHaveBeenCalled();
    expect(openEvents).toHaveBeenCalledTimes(3);
    expect(retryRequest).toMatchObject({
      afterSeq: 1,
      lastEvent: first,
    });
    expect(result.current.snapshot).toEqual(initial);
    expect(result.current.events).toEqual([first]);
    expect(result.current.errorPhase).toBe("events");
    expect(result.current.eventsRetrying).toBe(true);

    act(() => retryHandlers?.onOpen());
    expect(result.current.error).toBeNull();
    expect(result.current.errorPhase).toBeNull();
    expect(result.current.eventsRetrying).toBe(false);
  });

  it("loads a terminal snapshot and receipt during manual retry before opening the retained stream", async () => {
    vi.useFakeTimers();
    const initial = snapshot("pending_approval");
    const terminal = snapshot(
      "closed_without_action",
      "Closed without provider action.",
    );
    const receipt = declinedReceipt();
    let retryHandlers: RecoveryEventStreamHandlers | undefined;
    const openEvents = vi
      .fn<OpenEvents>()
      .mockRejectedValueOnce(capacityError())
      .mockImplementationOnce(
        (_request, handlers) =>
          new Promise<RecoveryEventStreamResult>(() => {
            retryHandlers = handlers;
          }),
      );
    const getRecovery = vi
      .fn()
      .mockResolvedValueOnce(initial)
      .mockResolvedValueOnce(terminal);
    const getReceipt = vi.fn().mockResolvedValue(receipt);
    const { result } = renderHook(() =>
      useRecovery(recoveryId, { getRecovery, getReceipt, openEvents }),
    );

    await act(flushAsyncWork);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    act(() => result.current.retryEvents());
    await act(flushAsyncWork);

    expect(getRecovery).toHaveBeenCalledTimes(2);
    expect(getReceipt).toHaveBeenCalledOnce();
    expect(result.current.snapshot).toEqual(terminal);
    expect(result.current.receipt).toEqual(receipt);
    expect(result.current.errorPhase).toBe("events");

    act(() => retryHandlers?.onOpen());
    expect(result.current.error).toBeNull();
    expect(result.current.snapshot).toEqual(terminal);
    expect(result.current.receipt).toEqual(receipt);
  });

  it("keeps the event retry path and retained evidence when the manual snapshot refresh fails", async () => {
    vi.useFakeTimers();
    const initial = snapshot("pending_approval");
    const first = event(1);
    const openEvents = vi
      .fn<OpenEvents>()
      .mockImplementationOnce(async (_request, handlers) => {
        handlers.onOpen();
        handlers.onEvent(first);
        return { kind: "eof", lastSeq: 1, lastEvent: first };
      })
      .mockRejectedValueOnce(capacityError());
    const getRecovery = vi
      .fn()
      .mockRejectedValue(new Error("private snapshot transport detail"));
    const { result } = renderHook(() =>
      useRecovery(recoveryId, {
        initialSnapshot: initial,
        getRecovery,
        openEvents,
        eventReconnectDelayMs: 0,
        eventReconnectAttempts: 1,
      }),
    );

    await act(flushAsyncWork);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
      await flushAsyncWork();
      await vi.advanceTimersByTimeAsync(1_000);
    });
    expect(result.current.eventsRetryAvailable).toBe(true);

    act(() => result.current.retryEvents());
    await act(flushAsyncWork);

    expect(result.current.snapshot).toEqual(initial);
    expect(result.current.events).toEqual([first]);
    expect(result.current.receipt).toBeNull();
    expect(result.current.error).toBe(
      "The event stream is currently at capacity.",
    );
    expect(result.current.errorPhase).toBe("events");
    expect(result.current.eventsRetryAvailable).toBe(true);
    expect(result.current.eventsRetrying).toBe(false);
    expect(result.current.error).not.toContain("private");
    expect(openEvents).toHaveBeenCalledTimes(2);
  });

  it("keeps the event retry path and retained terminal evidence when the manual receipt refresh fails", async () => {
    vi.useFakeTimers();
    const terminal = snapshot(
      "closed_without_action",
      "Closed without provider action.",
    );
    const receipt = declinedReceipt();
    const getRecovery = vi.fn().mockResolvedValue(terminal);
    const getReceipt = vi
      .fn()
      .mockResolvedValueOnce(receipt)
      .mockRejectedValueOnce(
        new Error("private receipt transport detail"),
      );
    const openEvents = vi
      .fn<OpenEvents>()
      .mockRejectedValue(capacityError());
    const { result } = renderHook(() =>
      useRecovery(recoveryId, {
        initialSnapshot: terminal,
        getRecovery,
        getReceipt,
        openEvents,
      }),
    );

    await act(flushAsyncWork);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    expect(result.current.receipt).toEqual(receipt);
    expect(result.current.eventsRetryAvailable).toBe(true);

    act(() => result.current.retryEvents());
    await act(flushAsyncWork);

    expect(result.current.snapshot).toEqual(terminal);
    expect(result.current.receipt).toEqual(receipt);
    expect(result.current.events).toEqual([]);
    expect(result.current.error).toBe(
      "The event stream is currently at capacity.",
    );
    expect(result.current.errorPhase).toBe("events");
    expect(result.current.eventsRetryAvailable).toBe(true);
    expect(result.current.eventsRetrying).toBe(false);
    expect(result.current.error).not.toContain("private");
    expect(openEvents).toHaveBeenCalledOnce();
  });

  it("settles a manual retry and preserves the event failure when refreshed terminal evidence is inconsistent", async () => {
    vi.useFakeTimers();
    const terminal = snapshot(
      "closed_without_action",
      "Closed without provider action.",
    );
    const receipt = declinedReceipt();
    const inconsistentReceipt = {
      ...receipt,
      rootTraceId: "qa_trace_mismatch",
    };
    const getRecovery = vi.fn().mockResolvedValue(terminal);
    const getReceipt = vi
      .fn()
      .mockResolvedValueOnce(receipt)
      .mockResolvedValueOnce(inconsistentReceipt);
    const openEvents = vi
      .fn<OpenEvents>()
      .mockRejectedValue(capacityError());
    const { result } = renderHook(() =>
      useRecovery(recoveryId, {
        initialSnapshot: terminal,
        getRecovery,
        getReceipt,
        openEvents,
      }),
    );

    await act(flushAsyncWork);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    expect(result.current.eventsRetryAvailable).toBe(true);

    act(() => result.current.retryEvents());
    await act(flushAsyncWork);

    expect(result.current.snapshot).toEqual(terminal);
    expect(result.current.receipt).toEqual(receipt);
    expect(result.current.events).toEqual([]);
    expect(result.current.error).toBe(
      "The event stream is currently at capacity.",
    );
    expect(result.current.errorPhase).toBe("events");
    expect(result.current.eventsRetryAvailable).toBe(true);
    expect(result.current.eventsRetrying).toBe(false);
    expect(openEvents).toHaveBeenCalledOnce();
  });

  it("marks the generation stale before abort and fences late StrictMode-style callbacks", async () => {
    let handlers: RecoveryEventStreamHandlers | undefined;
    let streamSignal: AbortSignal | undefined;
    const openEvents = vi.fn<OpenEvents>(
      (request, nextHandlers) =>
        new Promise(() => {
          streamSignal = request.signal;
          handlers = nextHandlers;
        }),
    );
    const getRecovery = vi.fn().mockResolvedValue(snapshot("pending_approval"));
    const getReceipt = vi.fn();
    const view = renderHook(() =>
      useRecovery(recoveryId, { getRecovery, getReceipt, openEvents }),
    );
    await waitFor(() => expect(view.result.current.snapshot).not.toBeNull());
    const before = view.result.current;

    view.unmount();
    act(() => {
      handlers?.onOpen();
      handlers?.onEvent({ ...event(1), terminal: true });
    });

    expect(streamSignal?.aborted).toBe(true);
    expect(view.result.current).toEqual(before);
    expect(getReceipt).not.toHaveBeenCalled();
  });

  it("cancels an owned EOF reconnect timer on cleanup", async () => {
    vi.useFakeTimers();
    const openEvents = vi.fn<OpenEvents>().mockResolvedValue({
      kind: "eof",
      lastSeq: 0,
      lastEvent: null,
    });
    const getRecovery = vi.fn().mockResolvedValue(snapshot("pending_approval"));
    const view = renderHook(() =>
      useRecovery(recoveryId, {
        getRecovery,
        openEvents,
        eventReconnectDelayMs: 100,
        eventReconnectAttempts: 2,
      }),
    );
    await act(flushAsyncWork);
    expect(openEvents).toHaveBeenCalledOnce();

    view.unmount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });

    expect(openEvents).toHaveBeenCalledOnce();
  });
});

describe("useRecovery authoritative evidence deadlines", () => {
  it("times out a never-settling initial snapshot at the default 12 second deadline", async () => {
    vi.useFakeTimers();
    let requestSignal: AbortSignal | undefined;
    const getRecovery = vi.fn(
      (_activeRecoveryId: string, signal?: AbortSignal) =>
        new Promise<RecoverySnapshot>(() => {
          requestSignal = signal;
        }),
    );
    const openEvents = pendingOpenEvents();
    const view = renderHook(() =>
      useRecovery(recoveryId, {
        getRecovery,
        openEvents,
      }),
    );

    await act(flushAsyncWork);
    act(() => vi.advanceTimersByTime(11_999));
    await act(flushAsyncWork);
    expect(view.result.current.error).toBeNull();
    expect(view.result.current.loading).toBe(true);

    act(() => vi.advanceTimersByTime(1));
    await act(flushAsyncWork);

    expect(requestSignal?.aborted).toBe(true);
    expect(view.result.current.loading).toBe(false);
    expect(view.result.current.error).toBe(
      "Recovery evidence request timed out.",
    );
    expect(view.result.current.errorPhase).toBe("initial");
    expect(view.result.current.errorStatus).toBeNull();
  });

  it("bounds a terminal snapshot and receipt pair with one shared deadline", async () => {
    vi.useFakeTimers();
    const readSignals: AbortSignal[] = [];
    const getRecovery = vi.fn(
      (_activeRecoveryId: string, signal?: AbortSignal) =>
        new Promise<RecoverySnapshot>(() => {
          if (signal !== undefined) {
            readSignals.push(signal);
          }
        }),
    );
    const getReceipt = vi.fn(
      (_activeRecoveryId: string, signal?: AbortSignal) =>
        new Promise<RecoveryReceipt>(() => {
          if (signal !== undefined) {
            readSignals.push(signal);
          }
        }),
    );
    const terminalEvent = {
      ...event(1, "recovery.closed_without_action"),
      terminal: true,
    };
    const initial = snapshot("pending_approval");
    const openEvents = vi.fn<OpenEvents>(
      (_request, handlers) =>
        new Promise(() => {
          handlers.onOpen();
          handlers.onEvent(terminalEvent);
        }),
    );
    const view = renderHook(() =>
      useRecovery(recoveryId, {
        initialSnapshot: initial,
        getRecovery,
        getReceipt,
        openEvents,
        authoritativeReadDeadlineMs: 100,
      }),
    );

    await act(flushAsyncWork);
    expect(getRecovery).toHaveBeenCalledOnce();
    expect(getReceipt).toHaveBeenCalledOnce();
    act(() => vi.advanceTimersByTime(100));
    await act(flushAsyncWork);

    expect(readSignals).toHaveLength(2);
    expect(readSignals.every((signal) => signal.aborted)).toBe(true);
    expect(view.result.current.snapshot).toEqual(initial);
    expect(view.result.current.receipt).toBeNull();
    expect(view.result.current.error).toBe(
      "Recovery evidence request timed out.",
    );
    expect(view.result.current.errorPhase).toBe("terminal");
  });

  it("does not stack terminal retries when one paired read fails and the other hangs", async () => {
    vi.useFakeTimers();
    const receiptSignals: AbortSignal[] = [];
    const getRecovery = vi
      .fn()
      .mockRejectedValue(new Error("private snapshot transport detail"));
    const getReceipt = vi.fn(
      (_activeRecoveryId: string, signal?: AbortSignal) =>
        new Promise<RecoveryReceipt>(() => {
          if (signal !== undefined) {
            receiptSignals.push(signal);
          }
        }),
    );
    const terminalEvent = {
      ...event(1, "recovery.closed_without_action"),
      terminal: true,
    };
    const initial = snapshot("pending_approval");
    const openEvents = vi.fn<OpenEvents>(
      (_request, handlers) =>
        new Promise(() => {
          handlers.onOpen();
          handlers.onEvent(terminalEvent);
        }),
    );
    const view = renderHook(() =>
      useRecovery(recoveryId, {
        initialSnapshot: initial,
        getRecovery,
        getReceipt,
        openEvents,
        terminalRetryDelayMs: 0,
        authoritativeReadDeadlineMs: 100,
      }),
    );

    await act(flushAsyncWork);
    act(() => vi.advanceTimersByTime(0));
    await act(flushAsyncWork);
    expect(getRecovery).toHaveBeenCalledOnce();
    expect(getReceipt).toHaveBeenCalledOnce();
    expect(view.result.current.error).toBeNull();

    act(() => vi.advanceTimersByTime(99));
    await act(flushAsyncWork);
    expect(getRecovery).toHaveBeenCalledOnce();
    expect(getReceipt).toHaveBeenCalledOnce();
    expect(receiptSignals[0]?.aborted).toBe(false);

    act(() => vi.advanceTimersByTime(1));
    await act(flushAsyncWork);
    expect(receiptSignals[0]?.aborted).toBe(true);
    expect(view.result.current.error).toBe(
      "Recovery evidence request timed out.",
    );
    expect(view.result.current.errorPhase).toBe("terminal");

    act(() => vi.advanceTimersByTime(0));
    await act(flushAsyncWork);
    expect(getRecovery).toHaveBeenCalledTimes(2);
    expect(getReceipt).toHaveBeenCalledTimes(2);
  });

  it("restores one manual event retry after a hung snapshot read and ignores its late resolution", async () => {
    vi.useFakeTimers();
    const initial = snapshot("pending_approval");
    const first = event(1);
    const lateSnapshot = deferred<RecoverySnapshot>();
    let requestSignal: AbortSignal | undefined;
    const getRecovery = vi
      .fn()
      .mockImplementationOnce(
        (_activeRecoveryId: string, signal?: AbortSignal) => {
          requestSignal = signal;
          return lateSnapshot.promise;
        },
      )
      .mockResolvedValueOnce(initial);
    let resumedRequest: RecoveryEventStreamRequest | undefined;
    let resumedHandlers: RecoveryEventStreamHandlers | undefined;
    const openEvents = vi
      .fn<OpenEvents>()
      .mockImplementationOnce(async (_request, handlers) => {
        handlers.onOpen();
        handlers.onEvent(first);
        throw capacityError();
      })
      .mockImplementationOnce(
        (request, handlers) =>
          new Promise<RecoveryEventStreamResult>(() => {
            resumedRequest = request;
            resumedHandlers = handlers;
          }),
      );
    const view = renderHook(() =>
      useRecovery(recoveryId, {
        initialSnapshot: initial,
        getRecovery,
        openEvents,
        authoritativeReadDeadlineMs: 100,
      }),
    );

    await act(flushAsyncWork);
    act(() => vi.advanceTimersByTime(1_000));
    await act(flushAsyncWork);
    expect(view.result.current.eventsRetryAvailable).toBe(true);

    act(() => {
      view.result.current.retryEvents();
      view.result.current.retryEvents();
    });
    await act(flushAsyncWork);
    expect(getRecovery).toHaveBeenCalledOnce();
    expect(view.result.current.eventsRetrying).toBe(true);

    act(() => vi.advanceTimersByTime(100));
    await act(flushAsyncWork);

    expect(requestSignal?.aborted).toBe(true);
    expect(view.result.current.snapshot).toEqual(initial);
    expect(view.result.current.events).toEqual([first]);
    expect(view.result.current.lastSeq).toBe(1);
    expect(view.result.current.error).toBe(
      "The event stream is currently at capacity.",
    );
    expect(view.result.current.errorPhase).toBe("events");
    expect(view.result.current.eventsRetryAvailable).toBe(true);
    expect(view.result.current.eventsRetrying).toBe(false);
    expect(openEvents).toHaveBeenCalledOnce();

    act(() => {
      view.result.current.retryEvents();
      view.result.current.retryEvents();
    });
    await act(flushAsyncWork);
    expect(getRecovery).toHaveBeenCalledTimes(2);
    expect(openEvents).toHaveBeenCalledTimes(2);
    expect(resumedRequest).toMatchObject({
      afterSeq: 1,
      lastEvent: first,
    });
    expect(view.result.current.eventsRetryAvailable).toBe(false);
    expect(view.result.current.eventsRetrying).toBe(true);

    await act(async () => {
      lateSnapshot.resolve(
        snapshot("closed_without_action", "Late terminal snapshot."),
      );
      await flushAsyncWork();
    });
    expect(view.result.current.snapshot).toEqual(initial);
    expect(view.result.current.receipt).toBeNull();
    expect(openEvents).toHaveBeenCalledTimes(2);

    act(() => resumedHandlers?.onOpen());
    expect(view.result.current.error).toBeNull();
    expect(view.result.current.errorPhase).toBeNull();
    expect(view.result.current.eventsRetrying).toBe(false);
  });

  it("retains terminal evidence and one retry after a hung receipt read", async () => {
    vi.useFakeTimers();
    const terminal = snapshot(
      "closed_without_action",
      "Closed without provider action.",
    );
    const receipt = declinedReceipt();
    const lateReceipt = deferred<RecoveryReceipt>();
    let retryReceiptSignal: AbortSignal | undefined;
    const getRecovery = vi.fn().mockResolvedValue(terminal);
    const getReceipt = vi
      .fn()
      .mockResolvedValueOnce(receipt)
      .mockImplementationOnce(
        (_activeRecoveryId: string, signal?: AbortSignal) => {
          retryReceiptSignal = signal;
          return lateReceipt.promise;
        },
      );
    const openEvents = vi
      .fn<OpenEvents>()
      .mockRejectedValue(capacityError());
    const view = renderHook(() =>
      useRecovery(recoveryId, {
        initialSnapshot: terminal,
        getRecovery,
        getReceipt,
        openEvents,
        authoritativeReadDeadlineMs: 100,
      }),
    );

    await act(flushAsyncWork);
    expect(view.result.current.receipt).toEqual(receipt);
    act(() => vi.advanceTimersByTime(1_000));
    await act(flushAsyncWork);
    expect(view.result.current.eventsRetryAvailable).toBe(true);

    act(() => view.result.current.retryEvents());
    await act(flushAsyncWork);
    expect(getRecovery).toHaveBeenCalledOnce();
    expect(getReceipt).toHaveBeenCalledTimes(2);
    expect(view.result.current.eventsRetrying).toBe(true);

    act(() => vi.advanceTimersByTime(100));
    await act(flushAsyncWork);

    expect(retryReceiptSignal?.aborted).toBe(true);
    expect(view.result.current.snapshot).toEqual(terminal);
    expect(view.result.current.receipt).toEqual(receipt);
    expect(view.result.current.error).toBe(
      "The event stream is currently at capacity.",
    );
    expect(view.result.current.errorPhase).toBe("events");
    expect(view.result.current.eventsRetryAvailable).toBe(true);
    expect(view.result.current.eventsRetrying).toBe(false);
    expect(openEvents).toHaveBeenCalledOnce();

    await act(async () => {
      lateReceipt.reject(new Error("late private receipt detail"));
      await flushAsyncWork();
    });
    expect(view.result.current.receipt).toEqual(receipt);
    expect(view.result.current.error).not.toContain("private");
    expect(openEvents).toHaveBeenCalledOnce();
  });

  it("caps an oversized injected evidence deadline at 60 seconds", async () => {
    vi.useFakeTimers();
    let requestSignal: AbortSignal | undefined;
    const getRecovery = vi.fn(
      (_activeRecoveryId: string, signal?: AbortSignal) =>
        new Promise<RecoverySnapshot>(() => {
          requestSignal = signal;
        }),
    );
    const openEvents = pendingOpenEvents();
    const view = renderHook(() =>
      useRecovery(recoveryId, {
        getRecovery,
        openEvents,
        authoritativeReadDeadlineMs: 600_000,
      }),
    );

    await act(flushAsyncWork);
    act(() => vi.advanceTimersByTime(59_999));
    await act(flushAsyncWork);
    expect(requestSignal?.aborted).toBe(false);
    expect(view.result.current.error).toBeNull();

    act(() => vi.advanceTimersByTime(1));
    await act(flushAsyncWork);
    expect(requestSignal?.aborted).toBe(true);
    expect(view.result.current.error).toBe(
      "Recovery evidence request timed out.",
    );
  });

  it.each([0, -1, 1.5, Number.NaN])(
    "defaults an invalid evidence deadline (%s) to exactly 12 seconds",
    async (authoritativeReadDeadlineMs) => {
      vi.useFakeTimers();
      let requestSignal: AbortSignal | undefined;
      const getRecovery = vi.fn(
        (_activeRecoveryId: string, signal?: AbortSignal) =>
          new Promise<RecoverySnapshot>(() => {
            requestSignal = signal;
          }),
      );
      const openEvents = pendingOpenEvents();
      const view = renderHook(() =>
        useRecovery(recoveryId, {
          getRecovery,
          openEvents,
          authoritativeReadDeadlineMs,
        }),
      );

      await act(flushAsyncWork);
      act(() => vi.advanceTimersByTime(11_999));
      await act(flushAsyncWork);
      expect(requestSignal?.aborted).toBe(false);
      expect(view.result.current.error).toBeNull();

      act(() => vi.advanceTimersByTime(1));
      await act(flushAsyncWork);
      expect(requestSignal?.aborted).toBe(true);
      expect(view.result.current.error).toBe(
        "Recovery evidence request timed out.",
      );
    },
  );

  it("fences the first authoritative read across a real StrictMode effect restart", async () => {
    vi.useFakeTimers();
    const first = deferred<RecoverySnapshot>();
    const second = deferred<RecoverySnapshot>();
    const readSignals: AbortSignal[] = [];
    const getRecovery = vi
      .fn()
      .mockImplementationOnce(
        (_activeRecoveryId: string, signal?: AbortSignal) => {
          if (signal !== undefined) {
            readSignals.push(signal);
          }
          return first.promise;
        },
      )
      .mockImplementationOnce(
        (_activeRecoveryId: string, signal?: AbortSignal) => {
          if (signal !== undefined) {
            readSignals.push(signal);
          }
          return second.promise;
        },
      );
    const openEvents = pendingOpenEvents();
    const view = renderHook(
      () =>
        useRecovery(recoveryId, {
          getRecovery,
          openEvents,
          authoritativeReadDeadlineMs: 100,
        }),
      { reactStrictMode: true },
    );
    await act(flushAsyncWork);

    expect(getRecovery).toHaveBeenCalledTimes(2);
    expect(readSignals).toHaveLength(2);
    expect(readSignals[0]?.aborted).toBe(true);
    expect(readSignals[1]?.aborted).toBe(false);
    expect(vi.getTimerCount()).toBe(1);

    await act(async () => {
      first.resolve(
        snapshot("closed_without_action", "Stale StrictMode snapshot."),
      );
      await flushAsyncWork();
    });
    expect(view.result.current.snapshot).toBeNull();
    expect(view.result.current.error).toBeNull();

    await act(async () => {
      second.resolve(snapshot("pending_approval"));
      await flushAsyncWork();
    });
    expect(view.result.current.snapshot).toEqual(
      snapshot("pending_approval"),
    );
    expect(view.result.current.error).toBeNull();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("aborts and clears an owned evidence deadline on unmount without a stale failure", async () => {
    vi.useFakeTimers();
    let requestSignal: AbortSignal | undefined;
    const getRecovery = vi.fn(
      (_activeRecoveryId: string, signal?: AbortSignal) =>
        new Promise<RecoverySnapshot>(() => {
          requestSignal = signal;
        }),
    );
    const openEvents = pendingOpenEvents();
    const view = renderHook(() =>
      useRecovery(recoveryId, {
        getRecovery,
        openEvents,
        authoritativeReadDeadlineMs: 100,
      }),
    );
    await act(flushAsyncWork);
    const beforeUnmount = view.result.current;

    view.unmount();
    expect(requestSignal?.aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
    act(() => vi.advanceTimersByTime(1_000));
    await act(flushAsyncWork);

    expect(view.result.current).toEqual(beforeUnmount);
    expect(view.result.current.error).toBeNull();
  });
});

describe("useRecovery authoritative terminal refresh", () => {
  it("preserves the typed status and initial-load phase of a recovery lookup failure", async () => {
    const getRecovery = vi
      .fn()
      .mockRejectedValue(new HttpStatusError("Recovery request failed with status 404", 404));
    const openEvents = pendingOpenEvents();

    const { result } = renderHook(() =>
      useRecovery(recoveryId, { getRecovery, openEvents }),
    );

    await waitFor(() => {
      expect(result.current.error).toBe("Recovery request failed with status 404");
      expect(result.current.errorStatus).toBe(404);
      expect(result.current.errorPhase).toBe("initial");
    });
  });

  it("loads snapshot and receipt together exactly once after duplicate terminal delivery", async () => {
    const initial = snapshot("pending_approval", "Decision claim is durable.");
    const terminal = snapshot("closed_without_action", "Closed without provider action.");
    const receipt = declinedReceipt();
    const getRecovery = vi
      .fn()
      .mockResolvedValueOnce(initial)
      .mockResolvedValueOnce(terminal);
    const getReceipt = vi.fn().mockResolvedValue(receipt);
    let handlers: RecoveryEventStreamHandlers | undefined;
    let streamSignal: AbortSignal | undefined;
    const openEvents = vi.fn<OpenEvents>(
      (request, nextHandlers) => {
        handlers = nextHandlers;
        streamSignal = request.signal;
        return new Promise(() => undefined);
      },
    );

    const { result, unmount } = renderHook(() =>
      useRecovery(recoveryId, { getRecovery, getReceipt, openEvents }),
    );

    await waitFor(() => expect(result.current.snapshot).toEqual(initial));
    const terminalEvent = {
      ...event(1, "recovery.closed_without_action"),
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
    expect(streamSignal?.aborted).toBe(true);
  });

  it("fences a late initial lookup failure after terminal evidence wins", async () => {
    let rejectInitial: ((error: Error) => void) | undefined;
    const initialLookup = new Promise<RecoverySnapshot>((_resolve, reject) => {
      rejectInitial = reject;
    });
    const terminal = snapshot(
      "closed_without_action",
      "Closed without provider action.",
    );
    const receipt = declinedReceipt();
    const terminalEvent = {
      ...event(1, "recovery.closed_without_action"),
      terminal: true,
    };
    const getRecovery = vi
      .fn()
      .mockReturnValueOnce(initialLookup)
      .mockResolvedValueOnce(terminal);
    const getReceipt = vi.fn().mockResolvedValue(receipt);
    const openEvents = vi.fn<OpenEvents>(async (_request, handlers) => {
      handlers.onOpen();
      handlers.onEvent(terminalEvent);
      return {
        kind: "terminal",
        lastSeq: terminalEvent.seq,
        lastEvent: terminalEvent,
      };
    });
    const { result } = renderHook(() =>
      useRecovery(recoveryId, { getRecovery, getReceipt, openEvents }),
    );

    await waitFor(() => expect(result.current.receipt).toEqual(receipt));
    await act(async () => {
      rejectInitial?.(new Error("late initial failure"));
      await flushAsyncWork();
    });

    expect(result.current.snapshot).toEqual(terminal);
    expect(result.current.receipt).toEqual(receipt);
    expect(result.current.error).toBeNull();
    expect(result.current.errorPhase).toBeNull();
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
    const openEvents = pendingOpenEvents();

    const { result } = renderHook(() =>
      useRecovery(recoveryId, { getRecovery, getReceipt, openEvents }),
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

      let handlers: RecoveryEventStreamHandlers | undefined;
      const openEvents = vi.fn<OpenEvents>(
        (_request, nextHandlers) => {
          handlers = nextHandlers;
          return new Promise(() => undefined);
        },
      );
      const { result } = renderHook(() =>
        useRecovery(recoveryId, {
          getRecovery,
          getReceipt,
          openEvents,
          terminalRetryDelayMs: 0,
        }),
      );

      await waitFor(() => expect(result.current.snapshot).toEqual(initial));
      const terminalEvent = {
        ...event(1, "recovery.closed_without_action"),
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

  it.each([
    {
      label: "quota snapshot without quota evidence",
      snapshot: quotaTerminalSnapshot(),
      receipt: { ...quotaTerminalReceipt(), quotaEvidence: null },
    },
    {
      label: "hotel snapshot with quota evidence",
      snapshot: {
        ...quotaTerminalSnapshot(),
        scenarioId: "hotel" as const,
      },
      receipt: quotaTerminalReceipt(),
    },
    {
      label: "quota execution-mode mismatch",
      snapshot: quotaTerminalSnapshot("sdk_stub"),
      receipt: quotaTerminalReceipt("replay_fixture"),
    },
  ])("rejects a terminal $label", async ({ snapshot, receipt }) => {
    const getRecovery = vi.fn().mockResolvedValue(snapshot);
    const getReceipt = vi.fn().mockResolvedValue(receipt);
    const openEvents = pendingOpenEvents();

    const { result, unmount } = renderHook(() =>
      useRecovery(recoveryId, {
        getRecovery,
        getReceipt,
        openEvents,
        terminalRetryDelayMs: 60_000,
      }),
    );

    await waitFor(() => {
      expect(result.current.errorPhase).toBe("terminal");
      expect(result.current.error).toMatch(/one recovery outcome/i);
    });
    expect(result.current.receipt).toBeNull();
    unmount();
  });

  it.each([
    {
      label: "root trace ID",
      receipt: { ...declinedReceipt(), rootTraceId: "qa_trace_mismatch" },
    },
    {
      label: "model IDs",
      receipt: { ...declinedReceipt(), modelIds: ["unexpected-model"] },
    },
    {
      label: "SDK version",
      receipt: { ...declinedReceipt(), sdkVersion: "0.18.4" },
    },
    {
      label: "protocol version",
      receipt: {
        ...declinedReceipt(),
        protocolVersion: "backchannel.approval.v2",
      },
    },
    {
      label: "agent graph version",
      receipt: {
        ...declinedReceipt(),
        agentGraphVersion: "backchannel.hotel-agent.v2",
      },
    },
    {
      label: "prompt/tool schema hash",
      receipt: { ...declinedReceipt(), promptToolSchemaHash: "c".repeat(64) },
    },
  ])("rejects a terminal pair with mismatched $label provenance", async ({ receipt }) => {
    const terminal = snapshot(
      "closed_without_action",
      "Closed without provider action.",
    );
    const getRecovery = vi.fn().mockResolvedValue(terminal);
    const getReceipt = vi.fn().mockResolvedValue(receipt);
    const openEvents = pendingOpenEvents();

    const { result, unmount } = renderHook(() =>
      useRecovery(recoveryId, {
        getRecovery,
        getReceipt,
        openEvents,
        terminalRetryDelayMs: 60_000,
      }),
    );

    await waitFor(() => {
      expect(result.current.errorPhase).toBe("terminal");
      expect(result.current.error).toMatch(/one recovery outcome/i);
    });
    expect(result.current.receipt).toBeNull();
    unmount();
  });
});
