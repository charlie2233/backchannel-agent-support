import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  connectRecoveryEvents,
  type RecoveryEvent,
  type RecoveryEventHandlers,
} from "../api/events";
import { HttpStatusError } from "../api/client";
import type { RecoveryReceipt, RecoverySnapshot } from "../domain/recovery";
import { initialRecoveryState, recoveryReducer, useRecovery } from "./useRecovery";

const recoveryId = "c9f6b65a-0ccf-4ef3-9d12-072e2660b852";
const sdkRootTraceId = "qa_trace_11111111111111111111111111111111";
const sdkDefinitionDigest = "b".repeat(64);

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
  it("preserves the typed status and initial-load phase of a recovery lookup failure", async () => {
    const getRecovery = vi
      .fn()
      .mockRejectedValue(new HttpStatusError("Recovery request failed with status 404", 404));
    const connectEvents = vi.fn(() => vi.fn());

    const { result } = renderHook(() =>
      useRecovery(recoveryId, { getRecovery, connectEvents }),
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
    const connectEvents = vi.fn(() => vi.fn());

    const { result, unmount } = renderHook(() =>
      useRecovery(recoveryId, {
        getRecovery,
        getReceipt,
        connectEvents,
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
    const connectEvents = vi.fn(() => vi.fn());

    const { result, unmount } = renderHook(() =>
      useRecovery(recoveryId, {
        getRecovery,
        getReceipt,
        connectEvents,
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
