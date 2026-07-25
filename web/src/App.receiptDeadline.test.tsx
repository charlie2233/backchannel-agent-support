import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import App, { RECEIPT_REQUEST_TIMEOUT_MS } from "./App";

const HOTEL_RECOVERY_KEY = "backchannel.hotelRecoveryId";
const RECOVERY_ID = "11111111-2222-4333-8444-555555555555";
const QUOTA_RECOVERY_ID = "22222222-2222-4222-8222-222222222222";
const RECEIPT_URL = `/api/recoveries/${RECOVERY_ID}/receipt`;
const RECEIPT_ERROR = "The authoritative terminal receipt could not be loaded.";

function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function liveHealth(): Response {
  return jsonResponse({
    backend: "openai",
    liveReady: true,
    providerBoundary: "demo_adapter_only",
  });
}

function terminalSnapshot(): Record<string, unknown> {
  return {
    recoveryId: RECOVERY_ID,
    scenarioId: "hotel",
    executionMode: "openai_live",
    modelIds: ["gpt-5.6-luna", "gpt-5.6-terra"],
    rootTraceId: "trace_0123456789abcdef0123456789abcdef",
    status: "completed",
    currentStep: 5,
    currentStepSummary: "Authoritative terminal recovery restored.",
    createdAt: "2026-07-19T12:00:00Z",
    updatedAt: "2026-07-19T12:00:01Z",
    pendingApproval: null,
    claimedDecision: null,
  };
}

function terminalReceipt(providerResult: string): Record<string, unknown> {
  return {
    recoveryId: RECOVERY_ID,
    executionMode: "openai_live",
    status: "completed",
    simulated: true,
    providerExecution: true,
    modelCall: true,
    modelIds: ["gpt-5.6-luna", "gpt-5.6-terra"],
    rootTraceId: "trace_0123456789abcdef0123456789abcdef",
    sdkVersion: "0.18.3",
    protocolVersion: "backchannel.approval.v1",
    agentGraphVersion: "backchannel.hotel-agent.v1",
    definitionDigest: "b".repeat(64),
    boundary:
      "OpenAI agent model calls and demo hotel adapter only; no real booking or payment change.",
    providerResult,
    authorizationSource: "Exact remedy approval accepted by the server.",
    verificationResults: ["Provider result verified before the receipt was sealed."],
    approvalCount: 1,
    approvedRemedyDigest: `sha256:${"a".repeat(64)}`,
  };
}

function sdkTerminalReceipt(providerResult: string): Record<string, unknown> {
  return {
    ...terminalReceipt(providerResult),
    executionMode: "sdk_stub",
    modelCall: false,
    modelIds: [],
    rootTraceId: "qa_trace_0123456789abcdef0123456789abcdef",
    boundary:
      "Deterministic Agents SDK model and demo hotel adapter only; no OpenAI model call, real booking, or payment change.",
  };
}

function pendingSnapshot(): Record<string, unknown> {
  return {
    ...terminalSnapshot(),
    status: "pending_approval",
    currentStep: 3,
    currentStepSummary: "Authoritative decision outcome is still pending.",
  };
}

function quotaSnapshot(
  executionMode: "replay_fixture" | "sdk_stub",
  status: "completed" | "in_progress" = "completed",
): Record<string, unknown> {
  return {
    recoveryId: QUOTA_RECOVERY_ID,
    scenarioId: "api-quota",
    executionMode,
    modelIds: [],
    rootTraceId:
      executionMode === "sdk_stub"
        ? "qa_trace_0123456789abcdef0123456789abcdef"
        : null,
    status,
    currentStep: status === "completed" ? 5 : 1,
    currentStepSummary:
      status === "completed"
        ? "Authoritative quota recovery completed."
        : "Authoritative quota recovery is in progress.",
    createdAt: "2026-07-19T12:00:00Z",
    updatedAt: "2026-07-19T12:00:01Z",
    pendingApproval: null,
    claimedDecision: null,
  };
}

function quotaReplayReceipt(): Record<string, unknown> {
  return {
    recoveryId: QUOTA_RECOVERY_ID,
    executionMode: "replay_fixture",
    status: "simulated_completed",
    simulated: true,
    providerExecution: false,
    modelCall: false,
    modelIds: [],
    rootTraceId: null,
    sdkVersion: null,
    protocolVersion: null,
    agentGraphVersion: null,
    definitionDigest: null,
    boundary: "Simulated quota replay — no model call or provider execution.",
    providerResult: "Recorded quota replay outcome only; no adapter execution occurred.",
    authorizationSource: "Recorded delegated-authority fixture",
    verificationResults: ["No provider dispatch occurred."],
    approvalCount: 0,
    approvedRemedyDigest: null,
  };
}

function quotaSdkReceipt(): Record<string, unknown> {
  return {
    recoveryId: QUOTA_RECOVERY_ID,
    executionMode: "sdk_stub",
    status: "completed",
    simulated: true,
    providerExecution: true,
    modelCall: false,
    modelIds: [],
    rootTraceId: "qa_trace_0123456789abcdef0123456789abcdef",
    sdkVersion: "0.18.3",
    protocolVersion: "backchannel.approval.v1",
    agentGraphVersion: "backchannel.quota-agent.v1",
    definitionDigest: "b".repeat(64),
    boundary:
      "Deterministic Agents SDK stub and demo quota adapter only; no OpenAI model call or real quota change.",
    providerResult:
      "Demo quota adapter verified 1200 units against a temporary 1250-unit US-region ceiling; no real quota was changed.",
    authorizationSource:
      "Predelegated API quota policy: US-only, at most 500 USD minor units, for at most 900 seconds.",
    verificationResults: [
      "Provider proved the baseline quota ceiling at 1000 units.",
      "Temporary US-region burst granted: 250 units for 900 seconds.",
      "All hard constraints remained satisfied.",
      "Extra cost of 300 USD minor units stayed within the delegated 500-unit limit.",
      "Approval count is zero; no human interruption was created.",
      "Execution verified at an effective ceiling of 1250 units.",
      "Temporary quota permission revoked; baseline ceiling restored to 1000 units.",
    ],
    approvalCount: 0,
    approvedRemedyDigest: null,
  };
}

function installControlledEventSource() {
  class ControlledEventSource {
    static instances: ControlledEventSource[] = [];
    onmessage: ((event: MessageEvent<string>) => void) | null = null;
    onerror: ((event: Event) => void) | null = null;
    addEventListener = vi.fn();
    removeEventListener = vi.fn();
    close = vi.fn();

    constructor(readonly url: string) {
      ControlledEventSource.instances.push(this);
    }
  }
  vi.stubGlobal("EventSource", ControlledEventSource);
  return ControlledEventSource;
}

function emitTerminalEvent(
  source: { onmessage: ((event: MessageEvent<string>) => void) | null } | undefined,
  recoveryId: string,
  summary: string,
) {
  source?.onmessage?.(
    new MessageEvent("message", {
      data: JSON.stringify({
        recoveryId,
        seq: 8,
        type: "recovery.completed",
        terminal: true,
        data: { summary },
        createdAt: "2026-07-19T12:00:02Z",
      }),
    }),
  );
}

async function flushAppRequests() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("terminal receipt snapshot correlation", () => {
  it.each([
    {
      mismatch: "execution mode",
      snapshot: terminalSnapshot(),
      receipt: sdkTerminalReceipt(
        "A same-ID SDK receipt must not replace the live snapshot provenance.",
      ),
      forbiddenResult:
        "A same-ID SDK receipt must not replace the live snapshot provenance.",
    },
    {
      mismatch: "terminal status",
      snapshot: {
        ...terminalSnapshot(),
        status: "closed_without_action",
        currentStepSummary: "Authoritative recovery closed without action.",
      },
      receipt: terminalReceipt(
        "A same-ID completed receipt must not replace the closed snapshot outcome.",
      ),
      forbiddenResult:
        "A same-ID completed receipt must not replace the closed snapshot outcome.",
    },
  ])(
    "fails closed instead of rendering a same-recoveryId receipt with a different $mismatch",
    async ({ snapshot, receipt, forbiddenResult }) => {
      window.sessionStorage.setItem(HOTEL_RECOVERY_KEY, RECOVERY_ID);
      let receiptGets = 0;
      const requestMethods: string[] = [];
      vi.stubGlobal(
        "fetch",
        vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
          const url = String(input);
          requestMethods.push(init?.method ?? "GET");
          if (url === "/health") return Promise.resolve(liveHealth());
          if (url === `/api/recoveries/${RECOVERY_ID}`) {
            return Promise.resolve(jsonResponse(snapshot));
          }
          if (url === RECEIPT_URL) {
            receiptGets += 1;
            return Promise.resolve(jsonResponse(receipt));
          }
          throw new Error(`Unexpected request: ${url}`);
        }),
      );

      render(<App />);
      await flushAppRequests();

      expect(receiptGets).toBe(1);
      expect(screen.getByRole("alert")).toHaveTextContent(RECEIPT_ERROR);
      expect(screen.getByRole("button", { name: "Retry receipt" })).toBeEnabled();
      expect(screen.queryByText(forbiddenResult)).not.toBeInTheDocument();
      expect(requestMethods.filter((method) => method === "POST")).toHaveLength(0);
    },
    15_000,
  );

  it("continues to render a same-mode, same-status authoritative receipt", async () => {
    window.sessionStorage.setItem(HOTEL_RECOVERY_KEY, RECOVERY_ID);
    const providerResult = "Matching terminal receipt remains authoritative.";
    let receiptGets = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request) => {
        const url = String(input);
        if (url === "/health") return Promise.resolve(liveHealth());
        if (url === `/api/recoveries/${RECOVERY_ID}`) {
          return Promise.resolve(jsonResponse(terminalSnapshot()));
        }
        if (url === RECEIPT_URL) {
          receiptGets += 1;
          return Promise.resolve(jsonResponse(terminalReceipt(providerResult)));
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    render(<App />);
    await flushAppRequests();

    expect(receiptGets).toBe(1);
    expect(screen.getByText(providerResult)).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("terminal receipt synchronization ownership", () => {
  it("recovers a terminal event only by refreshing its snapshot before requesting the receipt", async () => {
    window.sessionStorage.setItem(HOTEL_RECOVERY_KEY, RECOVERY_ID);
    const ControlledEventSource = installControlledEventSource();
    let recoveryGets = 0;
    let receiptGets = 0;
    let settleRetrySnapshot: ((response: Response) => void) | undefined;
    const requestMethods: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        requestMethods.push(init?.method ?? "GET");
        if (url === "/health") return Promise.resolve(liveHealth());
        if (url === `/api/recoveries/${RECOVERY_ID}`) {
          recoveryGets += 1;
          if (recoveryGets === 1) {
            return Promise.resolve(jsonResponse(pendingSnapshot()));
          }
          if (recoveryGets === 2) {
            return Promise.reject(new Error("terminal snapshot temporarily unavailable"));
          }
          if (recoveryGets === 3) {
            return new Promise<Response>((resolve) => {
              settleRetrySnapshot = resolve;
            });
          }
          throw new Error("Unexpected extra recovery GET");
        }
        if (url === RECEIPT_URL) {
          receiptGets += 1;
          return Promise.resolve(
            jsonResponse(terminalReceipt("Receipt loaded after the terminal snapshot retry.")),
          );
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    render(<App />);
    await flushAppRequests();
    await waitFor(() => expect(ControlledEventSource.instances).toHaveLength(1));

    act(() => {
      emitTerminalEvent(
        ControlledEventSource.instances[0],
        RECOVERY_ID,
        "Terminal event awaiting correlated evidence.",
      );
    });
    await flushAppRequests();

    expect(recoveryGets).toBe(2);
    expect(receiptGets).toBe(0);
    expect(screen.getByRole("alert")).toHaveTextContent(RECEIPT_ERROR);
    const retry = screen.getByRole("button", { name: "Retry receipt" });

    fireEvent.click(retry);
    await waitFor(() => expect(recoveryGets).toBe(3));
    expect(settleRetrySnapshot).toBeTypeOf("function");
    expect(receiptGets).toBe(0);

    await act(async () => {
      settleRetrySnapshot?.(jsonResponse(terminalSnapshot()));
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    await waitFor(() => expect(receiptGets).toBe(1));

    expect(
      await screen.findByText("Receipt loaded after the terminal snapshot retry."),
    ).toBeVisible();
    expect(requestMethods.filter((method) => method === "POST")).toHaveLength(0);
  });

  it("retires a receipt when the same recovery changes provenance before it settles", async () => {
    window.sessionStorage.setItem(HOTEL_RECOVERY_KEY, RECOVERY_ID);
    const ControlledEventSource = installControlledEventSource();
    const changedSnapshot = {
      ...terminalSnapshot(),
      executionMode: "sdk_stub",
      modelIds: [],
      rootTraceId: "qa_trace_fedcba9876543210fedcba9876543210",
      status: "closed_without_action",
      currentStepSummary: "Same-ID recovery now has different authoritative provenance.",
    };
    const staleProviderResult =
      "The receipt for the superseded live snapshot must remain hidden.";
    let recoveryGets = 0;
    let receiptGets = 0;
    let settleStaleReceipt: ((response: Response) => void) | undefined;
    const requestMethods: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        requestMethods.push(init?.method ?? "GET");
        if (url === "/health") return Promise.resolve(liveHealth());
        if (url === `/api/recoveries/${RECOVERY_ID}`) {
          recoveryGets += 1;
          return Promise.resolve(
            jsonResponse(recoveryGets === 1 ? terminalSnapshot() : changedSnapshot),
          );
        }
        if (url === RECEIPT_URL) {
          receiptGets += 1;
          if (receiptGets === 1) {
            return new Promise<Response>((resolve) => {
              settleStaleReceipt = resolve;
            });
          }
          return Promise.reject(new Error("replacement receipt unavailable"));
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    render(<App />);
    await flushAppRequests();
    await waitFor(() => expect(settleStaleReceipt).toBeTypeOf("function"));
    await waitFor(() => expect(ControlledEventSource.instances).toHaveLength(1));

    act(() => {
      emitTerminalEvent(
        ControlledEventSource.instances[0],
        RECOVERY_ID,
        "Same-ID terminal provenance changed.",
      );
    });
    expect(
      await screen.findByText(
        "Same-ID recovery now has different authoritative provenance.",
      ),
    ).toBeVisible();

    await act(async () => {
      settleStaleReceipt?.(
        jsonResponse(terminalReceipt(staleProviderResult)),
      );
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(screen.queryByText(staleProviderResult)).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(RECEIPT_ERROR);
    expect(screen.getByRole("button", { name: "Retry receipt" })).toBeEnabled();
    expect(requestMethods.filter((method) => method === "POST")).toHaveLength(0);
  });
});

describe("authoritative terminal snapshot request deadline", () => {
  it("bounds a terminal-event snapshot refresh before exposing a receipt retry", async () => {
    vi.useFakeTimers();
    window.sessionStorage.setItem(HOTEL_RECOVERY_KEY, RECOVERY_ID);
    const ControlledEventSource = installControlledEventSource();
    const requestMethods: string[] = [];
    let recoveryGets = 0;
    let receiptGets = 0;
    let terminalRefreshSignal: AbortSignal | undefined;
    const terminalRefreshAbort = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        requestMethods.push(init?.method ?? "GET");
        if (url === "/health") return Promise.resolve(liveHealth());
        if (url === `/api/recoveries/${RECOVERY_ID}`) {
          recoveryGets += 1;
          if (recoveryGets === 1) {
            return Promise.resolve(jsonResponse(pendingSnapshot()));
          }
          if (recoveryGets === 2) {
            terminalRefreshSignal = init?.signal ?? undefined;
            terminalRefreshSignal?.addEventListener("abort", terminalRefreshAbort);
            return new Promise<Response>(() => {});
          }
          throw new Error("Unexpected extra recovery GET");
        }
        if (url === RECEIPT_URL) {
          receiptGets += 1;
          return Promise.resolve(
            jsonResponse(terminalReceipt("No receipt may precede the terminal snapshot.")),
          );
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    render(<App />);
    await flushAppRequests();
    expect(ControlledEventSource.instances).toHaveLength(1);

    act(() => {
      emitTerminalEvent(
        ControlledEventSource.instances[0],
        RECOVERY_ID,
        "Terminal event awaiting a bounded authoritative snapshot.",
      );
    });
    await flushAppRequests();

    expect(recoveryGets).toBe(2);
    expect(receiptGets).toBe(0);
    expect(
      screen.getByText(
        "Terminal server event received. Synchronizing the authoritative snapshot and receipt…",
      ),
    ).toBeVisible();

    await act(async () => {
      vi.advanceTimersByTime(RECEIPT_REQUEST_TIMEOUT_MS - 1);
      await Promise.resolve();
    });
    expect(screen.queryByRole("button", { name: "Retry receipt" })).not.toBeInTheDocument();
    expect(receiptGets).toBe(0);

    await act(async () => {
      vi.advanceTimersByTime(1);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(RECEIPT_REQUEST_TIMEOUT_MS).toBe(12_000);
    expect(terminalRefreshSignal).toBeInstanceOf(AbortSignal);
    expect(terminalRefreshSignal?.aborted).toBe(true);
    expect(terminalRefreshAbort).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("alert")).toHaveTextContent(RECEIPT_ERROR);
    expect(screen.getByRole("button", { name: "Retry receipt" })).toBeEnabled();
    expect(receiptGets).toBe(0);
    expect(requestMethods.filter((method) => method === "POST")).toHaveLength(0);
  });

  it("bounds an explicit retry snapshot refresh and restores its retry action focus", async () => {
    vi.useFakeTimers();
    window.sessionStorage.setItem(HOTEL_RECOVERY_KEY, RECOVERY_ID);
    const requestMethods: string[] = [];
    let recoveryGets = 0;
    let receiptGets = 0;
    let retrySnapshotSignal: AbortSignal | undefined;
    const retrySnapshotAbort = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        requestMethods.push(init?.method ?? "GET");
        if (url === "/health") return Promise.resolve(liveHealth());
        if (url === `/api/recoveries/${RECOVERY_ID}`) {
          recoveryGets += 1;
          if (recoveryGets === 1) {
            return Promise.resolve(jsonResponse(terminalSnapshot()));
          }
          if (recoveryGets === 2) {
            retrySnapshotSignal = init?.signal ?? undefined;
            retrySnapshotSignal?.addEventListener("abort", retrySnapshotAbort);
            return new Promise<Response>(() => {});
          }
          throw new Error("Unexpected extra recovery GET");
        }
        if (url === RECEIPT_URL) {
          receiptGets += 1;
          if (receiptGets === 1) {
            return Promise.reject(new Error("initial receipt unavailable"));
          }
          throw new Error("Receipt GET must wait for the retry snapshot");
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    render(<App />);
    await flushAppRequests();
    const retry = screen.getByRole("button", { name: "Retry receipt" });
    retry.focus();
    expect(retry).toHaveFocus();

    fireEvent.click(retry);
    await flushAppRequests();

    expect(recoveryGets).toBe(2);
    expect(receiptGets).toBe(1);
    expect(screen.getByText("Loading authoritative terminal receipt…")).toBeVisible();
    expect(document.body).toHaveFocus();

    await act(async () => {
      vi.advanceTimersByTime(RECEIPT_REQUEST_TIMEOUT_MS - 1);
      await Promise.resolve();
    });
    expect(screen.getByText("Loading authoritative terminal receipt…")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Retry receipt" })).not.toBeInTheDocument();

    await act(async () => {
      vi.advanceTimersByTime(1);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(retrySnapshotSignal).toBeInstanceOf(AbortSignal);
    expect(retrySnapshotSignal?.aborted).toBe(true);
    expect(retrySnapshotAbort).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("alert")).toHaveTextContent(RECEIPT_ERROR);
    const recreatedRetry = screen.getByRole("button", { name: "Retry receipt" });
    expect(recreatedRetry).not.toBe(retry);
    expect(recreatedRetry).toHaveFocus();
    expect(receiptGets).toBe(1);
    expect(requestMethods.filter((method) => method === "POST")).toHaveLength(0);
  });

  it("aborts an unmounted retry snapshot and ignores its late settlement", async () => {
    window.sessionStorage.setItem(HOTEL_RECOVERY_KEY, RECOVERY_ID);
    const requestMethods: string[] = [];
    let recoveryGets = 0;
    let receiptGets = 0;
    let retrySnapshotSignal: AbortSignal | undefined;
    let settleUnmountedRetry: ((response: Response) => void) | undefined;
    const retrySnapshotAbort = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        requestMethods.push(init?.method ?? "GET");
        if (url === "/health") return Promise.resolve(liveHealth());
        if (url === `/api/recoveries/${RECOVERY_ID}`) {
          recoveryGets += 1;
          if (recoveryGets === 1) {
            return Promise.resolve(jsonResponse(terminalSnapshot()));
          }
          if (recoveryGets === 2) {
            retrySnapshotSignal = init?.signal ?? undefined;
            retrySnapshotSignal?.addEventListener("abort", retrySnapshotAbort);
            return new Promise<Response>((resolve) => {
              settleUnmountedRetry = resolve;
            });
          }
          throw new Error("Unexpected extra recovery GET");
        }
        if (url === RECEIPT_URL) {
          receiptGets += 1;
          if (receiptGets === 1) {
            return Promise.reject(new Error("initial receipt unavailable"));
          }
          return Promise.resolve(
            jsonResponse(terminalReceipt("Late unmounted retry receipt.")),
          );
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    const view = render(<App />);
    await flushAppRequests();
    fireEvent.click(screen.getByRole("button", { name: "Retry receipt" }));
    await flushAppRequests();

    expect(recoveryGets).toBe(2);
    expect(receiptGets).toBe(1);
    expect(settleUnmountedRetry).toBeTypeOf("function");

    view.unmount();

    expect.soft(retrySnapshotSignal).toBeInstanceOf(AbortSignal);
    expect.soft(retrySnapshotSignal?.aborted).toBe(true);
    expect.soft(retrySnapshotAbort).toHaveBeenCalledTimes(1);

    await act(async () => {
      settleUnmountedRetry?.(jsonResponse(terminalSnapshot()));
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect.soft(receiptGets).toBe(1);
    expect.soft(requestMethods.filter((method) => method === "POST")).toHaveLength(0);
  });
});

describe("quota receipt truthfulness by execution mode", () => {
  it("renders a correlated replay receipt without claiming quota-adapter execution", async () => {
    const ControlledEventSource = installControlledEventSource();
    const requestMethods: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        requestMethods.push(init?.method ?? "GET");
        if (url === "/health") return Promise.resolve(liveHealth());
        if (url === "/api/recoveries" && init?.method === "POST") {
          return Promise.resolve(jsonResponse(quotaSnapshot("sdk_stub", "in_progress"), 201));
        }
        if (url === `/api/recoveries/${QUOTA_RECOVERY_ID}`) {
          return Promise.resolve(jsonResponse(quotaSnapshot("replay_fixture")));
        }
        if (url === `/api/recoveries/${QUOTA_RECOVERY_ID}/receipt`) {
          return Promise.resolve(jsonResponse(quotaReplayReceipt()));
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    render(<App />);
    fireEvent.click(
      await screen.findByRole("button", { name: /API quota recovery/i }),
    );
    expect(
      await screen.findByText("Authoritative quota recovery is in progress."),
    ).toBeVisible();
    await waitFor(() => expect(ControlledEventSource.instances).toHaveLength(1));

    act(() => {
      emitTerminalEvent(
        ControlledEventSource.instances[0],
        QUOTA_RECOVERY_ID,
        "Quota replay reached its recorded terminal event.",
      );
    });

    expect(
      await screen.findByText(
        "Recorded quota replay outcome only; no adapter execution occurred.",
      ),
    ).toBeVisible();
    expect(
      screen.queryAllByText(
        "The demo quota adapter verified execution at the temporary 1250-unit ceiling.",
      ),
    ).toHaveLength(0);
    expect(
      screen.queryAllByText(
        "Execution was verified and temporary permission quota-burst-demo-us-east-1 was revoked.",
      ),
    ).toHaveLength(0);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(requestMethods.filter((method) => method === "POST")).toHaveLength(1);
  });

  it("keeps exact SDK quota execution claims when its matching receipt is accepted", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/health") return Promise.resolve(liveHealth());
        if (url === "/api/recoveries" && init?.method === "POST") {
          return Promise.resolve(jsonResponse(quotaSnapshot("sdk_stub"), 201));
        }
        if (url === `/api/recoveries/${QUOTA_RECOVERY_ID}/receipt`) {
          return Promise.resolve(jsonResponse(quotaSdkReceipt()));
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    render(<App />);
    fireEvent.click(
      await screen.findByRole("button", { name: /API quota recovery/i }),
    );

    expect(
      await screen.findByText(
        "Demo quota adapter verified 1200 units against a temporary 1250-unit US-region ceiling; no real quota was changed.",
      ),
    ).toBeVisible();
    expect(
      screen.getAllByText(
        "The demo quota adapter verified execution at the temporary 1250-unit ceiling.",
      ),
    ).not.toHaveLength(0);
    expect(
      screen.getAllByText(
        "Execution was verified and temporary permission quota-burst-demo-us-east-1 was revoked.",
      ),
    ).not.toHaveLength(0);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("terminal receipt request deadline", () => {
  it("times out a never-settling receipt GET into a retryable public state", async () => {
    vi.useFakeTimers();
    window.sessionStorage.setItem(HOTEL_RECOVERY_KEY, RECOVERY_ID);
    const requests: Array<{ url: string; method: string }> = [];
    let receiptSignal: AbortSignal | undefined;
    const abortListener = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method ?? "GET";
        requests.push({ url, method });
        if (url === "/health") return Promise.resolve(liveHealth());
        if (url === `/api/recoveries/${RECOVERY_ID}`) {
          return Promise.resolve(jsonResponse(terminalSnapshot()));
        }
        if (url === RECEIPT_URL) {
          receiptSignal = init?.signal ?? undefined;
          receiptSignal?.addEventListener("abort", abortListener);
          return new Promise<Response>(() => {});
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      }),
    );

    render(<App />);
    await flushAppRequests();

    expect(RECEIPT_REQUEST_TIMEOUT_MS).toBe(12_000);
    expect(receiptSignal).toBeInstanceOf(AbortSignal);
    expect(receiptSignal?.aborted).toBe(false);
    expect(screen.getByText("Loading authoritative terminal receipt…")).toBeVisible();

    await act(async () => {
      vi.advanceTimersByTime(RECEIPT_REQUEST_TIMEOUT_MS - 1);
      await Promise.resolve();
    });
    expect(screen.getByText("Loading authoritative terminal receipt…")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Retry receipt" })).not.toBeInTheDocument();

    await act(async () => {
      vi.advanceTimersByTime(1);
      await Promise.resolve();
    });

    expect(receiptSignal?.aborted).toBe(true);
    expect(abortListener).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("alert")).toHaveTextContent(RECEIPT_ERROR);
    expect(screen.getByRole("button", { name: "Retry receipt" })).toBeEnabled();
    expect(document.body).toHaveFocus();
    expect(requests.filter(({ method }) => method === "POST")).toHaveLength(0);
  });

  it("coalesces a rapid retry and exposes the retry action again after another timeout", async () => {
    vi.useFakeTimers();
    window.sessionStorage.setItem(HOTEL_RECOVERY_KEY, RECOVERY_ID);
    const receiptSignals: AbortSignal[] = [];
    const requestMethods: string[] = [];
    class ControlledEventSource {
      static instances: ControlledEventSource[] = [];
      onmessage: ((event: MessageEvent<string>) => void) | null = null;
      onerror: ((event: Event) => void) | null = null;
      addEventListener = vi.fn();
      removeEventListener = vi.fn();
      close = vi.fn();

      constructor(readonly url: string) {
        ControlledEventSource.instances.push(this);
      }
    }
    vi.stubGlobal("EventSource", ControlledEventSource);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method ?? "GET";
        requestMethods.push(method);
        if (url === "/health") return Promise.resolve(liveHealth());
        if (url === `/api/recoveries/${RECOVERY_ID}`) {
          return Promise.resolve(jsonResponse(terminalSnapshot()));
        }
        if (url === RECEIPT_URL) {
          const signal = init?.signal;
          if (!(signal instanceof AbortSignal)) throw new Error("Receipt GET must be abortable");
          receiptSignals.push(signal);
          return new Promise<Response>(() => {});
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      }),
    );

    render(<App />);
    await flushAppRequests();
    await act(async () => {
      vi.advanceTimersByTime(RECEIPT_REQUEST_TIMEOUT_MS);
      await Promise.resolve();
    });

    const retry = screen.getByRole("button", { name: "Retry receipt" });
    const source = ControlledEventSource.instances[0];
    expect(source?.url).toBe(`/api/recoveries/${RECOVERY_ID}/events`);
    await act(async () => {
      retry.click();
      source?.onmessage?.(
        new MessageEvent("message", {
          data: JSON.stringify({
            recoveryId: RECOVERY_ID,
            seq: 8,
            type: "recovery.completed",
            terminal: true,
            data: { summary: "Receipt retry coalescing event." },
            createdAt: "2026-07-19T12:00:02Z",
          }),
        }),
      );
      await Promise.resolve();
    });
    await flushAppRequests();

    expect(receiptSignals).toHaveLength(2);
    expect(screen.getByText("Receipt retry coalescing event.")).toBeVisible();
    expect(screen.getByText("Loading authoritative terminal receipt…")).toBeVisible();
    expect(requestMethods.filter((method) => method === "POST")).toHaveLength(0);

    await act(async () => {
      vi.advanceTimersByTime(RECEIPT_REQUEST_TIMEOUT_MS);
      await Promise.resolve();
    });

    expect(receiptSignals.map(({ aborted }) => aborted)).toEqual([true, true]);
    const reusableRetry = screen.getByRole("button", { name: "Retry receipt" });
    expect(reusableRetry).toBeEnabled();
    fireEvent.click(reusableRetry);
    await flushAppRequests();
    expect(receiptSignals).toHaveLength(3);
    expect(receiptSignals[2]?.aborted).toBe(false);
    expect(requestMethods.filter((method) => method === "POST")).toHaveLength(0);
  });

  it("clears the exact deadline when a receipt succeeds before it", async () => {
    vi.useFakeTimers();
    const setTimeoutSpy = vi.spyOn(globalThis, "setTimeout");
    const clearTimeoutSpy = vi.spyOn(globalThis, "clearTimeout");
    window.sessionStorage.setItem(HOTEL_RECOVERY_KEY, RECOVERY_ID);
    let settleReceipt: ((response: Response) => void) | undefined;
    let receiptSignal: AbortSignal | undefined;
    const abortListener = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/health") return Promise.resolve(liveHealth());
        if (url === `/api/recoveries/${RECOVERY_ID}`) {
          return Promise.resolve(jsonResponse(terminalSnapshot()));
        }
        if (url === RECEIPT_URL) {
          receiptSignal = init?.signal ?? undefined;
          receiptSignal?.addEventListener("abort", abortListener);
          return new Promise<Response>((resolve) => {
            settleReceipt = resolve;
          });
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    render(<App />);
    await flushAppRequests();
    const deadlineCallIndex = setTimeoutSpy.mock.calls.findIndex(
      ([, delay]) => delay === RECEIPT_REQUEST_TIMEOUT_MS,
    );
    const receiptDeadlineCallIndex = setTimeoutSpy.mock.calls.reduce(
      (latest, [, delay], index) =>
        delay === RECEIPT_REQUEST_TIMEOUT_MS ? index : latest,
      -1,
    );
    expect(deadlineCallIndex).toBeGreaterThanOrEqual(0);
    expect(receiptDeadlineCallIndex).toBeGreaterThan(deadlineCallIndex);
    const receiptDeadlineHandle = setTimeoutSpy.mock.results[receiptDeadlineCallIndex]?.value;
    expect(
      clearTimeoutSpy.mock.calls.filter(([handle]) => handle === receiptDeadlineHandle),
    ).toHaveLength(0);

    await act(async () => {
      vi.advanceTimersByTime(RECEIPT_REQUEST_TIMEOUT_MS - 1);
      settleReceipt?.(jsonResponse(terminalReceipt("Receipt settled before its deadline.")));
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(screen.getByText("Receipt settled before its deadline.")).toBeVisible();
    expect(
      clearTimeoutSpy.mock.calls.filter(([handle]) => handle === receiptDeadlineHandle),
    ).toHaveLength(1);
    expect(receiptSignal?.aborted).toBe(false);

    await act(async () => {
      vi.advanceTimersByTime(RECEIPT_REQUEST_TIMEOUT_MS * 2);
      await Promise.resolve();
    });

    expect(receiptSignal?.aborted).toBe(false);
    expect(abortListener).not.toHaveBeenCalled();
    expect(screen.getByText("Receipt settled before its deadline.")).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(
      clearTimeoutSpy.mock.calls.filter(([handle]) => handle === receiptDeadlineHandle),
    ).toHaveLength(1);
  });

  it.each(["resolve", "reject"] as const)(
    "ignores a late %s from a timed-out attempt after a newer retry succeeds",
    async (lateSettlement) => {
      vi.useFakeTimers();
      window.sessionStorage.setItem(HOTEL_RECOVERY_KEY, RECOVERY_ID);
      let resolveFirst: ((response: Response) => void) | undefined;
      let rejectFirst: ((reason: unknown) => void) | undefined;
      let receiptGets = 0;
      vi.stubGlobal(
        "fetch",
        vi.fn().mockImplementation((input: string | URL | Request) => {
          const url = String(input);
          if (url === "/health") return Promise.resolve(liveHealth());
          if (url === `/api/recoveries/${RECOVERY_ID}`) {
            return Promise.resolve(jsonResponse(terminalSnapshot()));
          }
          if (url === RECEIPT_URL) {
            receiptGets += 1;
            if (receiptGets === 1) {
              return new Promise<Response>((resolve, reject) => {
                resolveFirst = resolve;
                rejectFirst = reject;
              });
            }
            return Promise.resolve(
              jsonResponse(terminalReceipt("Newer retry receipt remains authoritative.")),
            );
          }
          throw new Error(`Unexpected request: ${url}`);
        }),
      );

      render(<App />);
      await flushAppRequests();
      await act(async () => {
        vi.advanceTimersByTime(RECEIPT_REQUEST_TIMEOUT_MS);
        await Promise.resolve();
      });
      fireEvent.click(screen.getByRole("button", { name: "Retry receipt" }));
      await flushAppRequests();

      expect(screen.getByText("Newer retry receipt remains authoritative.")).toBeVisible();
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();

      await act(async () => {
        if (lateSettlement === "resolve") {
          resolveFirst?.(jsonResponse(terminalReceipt("Stale timed-out receipt.")));
        } else {
          rejectFirst?.(new Error("late receipt transport failure"));
        }
        await Promise.resolve();
        await Promise.resolve();
      });

      expect(screen.getByText("Newer retry receipt remains authoritative.")).toBeVisible();
      expect(screen.queryByText("Stale timed-out receipt.")).not.toBeInTheDocument();
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(screen.queryByText("Loading authoritative terminal receipt…")).not.toBeInTheDocument();
      expect(receiptGets).toBe(2);
    },
  );

  it("aborts and clears an unmounted request once, ignores it late, and restarts in StrictMode", async () => {
    vi.useFakeTimers();
    const setTimeoutSpy = vi.spyOn(globalThis, "setTimeout");
    const clearTimeoutSpy = vi.spyOn(globalThis, "clearTimeout");
    window.sessionStorage.setItem(HOTEL_RECOVERY_KEY, RECOVERY_ID);
    let settleUnmounted: ((response: Response) => void) | undefined;
    let firstSignal: AbortSignal | undefined;
    const firstAbortListener = vi.fn();
    let receiptGets = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/health") return Promise.resolve(liveHealth());
        if (url === `/api/recoveries/${RECOVERY_ID}`) {
          return Promise.resolve(jsonResponse(terminalSnapshot()));
        }
        if (url === RECEIPT_URL) {
          receiptGets += 1;
          if (receiptGets === 1) {
            firstSignal = init?.signal ?? undefined;
            firstSignal?.addEventListener("abort", firstAbortListener);
            return new Promise<Response>((resolve) => {
              settleUnmounted = resolve;
            });
          }
          return Promise.resolve(
            jsonResponse(terminalReceipt("StrictMode restarted the receipt request.")),
          );
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    const firstView = render(<App />);
    await flushAppRequests();
    const firstDeadlineCallIndex = setTimeoutSpy.mock.calls.findIndex(
      ([, delay]) => delay === RECEIPT_REQUEST_TIMEOUT_MS,
    );
    const receiptDeadlineCallIndex = setTimeoutSpy.mock.calls.reduce(
      (latest, [, delay], index) =>
        delay === RECEIPT_REQUEST_TIMEOUT_MS ? index : latest,
      -1,
    );
    expect(firstDeadlineCallIndex).toBeGreaterThanOrEqual(0);
    expect(receiptDeadlineCallIndex).toBeGreaterThan(firstDeadlineCallIndex);
    const receiptDeadlineHandle = setTimeoutSpy.mock.results[receiptDeadlineCallIndex]?.value;
    expect(
      clearTimeoutSpy.mock.calls.filter(([handle]) => handle === receiptDeadlineHandle),
    ).toHaveLength(0);

    firstView.unmount();

    expect(firstSignal?.aborted).toBe(true);
    expect(firstAbortListener).toHaveBeenCalledTimes(1);
    expect(
      clearTimeoutSpy.mock.calls.filter(([handle]) => handle === receiptDeadlineHandle),
    ).toHaveLength(1);

    render(
      <StrictMode>
        <App />
      </StrictMode>,
    );
    await flushAppRequests();
    expect(screen.getByText("StrictMode restarted the receipt request.")).toBeVisible();

    await act(async () => {
      settleUnmounted?.(jsonResponse(terminalReceipt("Late unmounted receipt.")));
      vi.advanceTimersByTime(RECEIPT_REQUEST_TIMEOUT_MS * 2);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(firstAbortListener).toHaveBeenCalledTimes(1);
    expect(screen.getByText("StrictMode restarted the receipt request.")).toBeVisible();
    expect(screen.queryByText("Late unmounted receipt.")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(receiptGets).toBe(2);
    expect(
      clearTimeoutSpy.mock.calls.filter(([handle]) => handle === receiptDeadlineHandle),
    ).toHaveLength(1);
  });

  it("turns a current transport AbortError into a retryable receipt error", async () => {
    window.sessionStorage.setItem(HOTEL_RECOVERY_KEY, RECOVERY_ID);
    let receiptSignal: AbortSignal | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/health") return Promise.resolve(liveHealth());
        if (url === `/api/recoveries/${RECOVERY_ID}`) {
          return Promise.resolve(jsonResponse(terminalSnapshot()));
        }
        if (url === RECEIPT_URL) {
          receiptSignal = init?.signal ?? undefined;
          return Promise.reject(new DOMException("transport abort", "AbortError"));
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    render(<App />);

    expect(await screen.findByRole("alert")).toHaveTextContent(RECEIPT_ERROR);
    expect(screen.getByRole("button", { name: "Retry receipt" })).toBeEnabled();
    expect(receiptSignal).toBeInstanceOf(AbortSignal);
    expect(receiptSignal?.aborted).toBe(false);
    expect(document.body).toHaveFocus();
  });

  it.each(["timeout", "AbortError"] as const)(
    "restores body focus to Retry receipt after an explicit %s failure",
    async (failure) => {
      vi.useFakeTimers();
      window.sessionStorage.setItem(HOTEL_RECOVERY_KEY, RECOVERY_ID);
      let receiptGets = 0;
      let rejectRetry: ((reason: unknown) => void) | undefined;
      vi.stubGlobal(
        "fetch",
        vi.fn().mockImplementation((input: string | URL | Request) => {
          const url = String(input);
          if (url === "/health") return Promise.resolve(liveHealth());
          if (url === `/api/recoveries/${RECOVERY_ID}`) {
            return Promise.resolve(jsonResponse(terminalSnapshot()));
          }
          if (url === RECEIPT_URL) {
            receiptGets += 1;
            if (receiptGets === 1) {
              return Promise.reject(new Error("initial receipt failure"));
            }
            return new Promise<Response>((_resolve, reject) => {
              rejectRetry = reject;
            });
          }
          throw new Error(`Unexpected request: ${url}`);
        }),
      );

      render(<App />);
      await flushAppRequests();
      const retry = screen.getByRole("button", { name: "Retry receipt" });
      retry.focus();
      expect(retry).toHaveFocus();

      fireEvent.click(retry);
      await flushAppRequests();

      expect(screen.getByText("Loading authoritative terminal receipt…")).toBeVisible();
      expect(document.body).toHaveFocus();
      await act(async () => {
        if (failure === "timeout") {
          vi.advanceTimersByTime(RECEIPT_REQUEST_TIMEOUT_MS);
        } else {
          rejectRetry?.(new DOMException("current transport abort", "AbortError"));
        }
        await Promise.resolve();
        await Promise.resolve();
      });

      const recreatedRetry = screen.getByRole("button", { name: "Retry receipt" });
      expect(recreatedRetry).not.toBe(retry);
      expect(recreatedRetry).toHaveFocus();
      expect(receiptGets).toBe(2);
    },
  );

  it.each(["timeout", "AbortError"] as const)(
    "preserves deliberate external focus after an explicit receipt %s failure",
    async (failure) => {
      vi.useFakeTimers();
      window.sessionStorage.setItem(HOTEL_RECOVERY_KEY, RECOVERY_ID);
      let receiptGets = 0;
      let rejectRetry: ((reason: unknown) => void) | undefined;
      vi.stubGlobal(
        "fetch",
        vi.fn().mockImplementation((input: string | URL | Request) => {
          const url = String(input);
          if (url === "/health") return Promise.resolve(liveHealth());
          if (url === `/api/recoveries/${RECOVERY_ID}`) {
            return Promise.resolve(jsonResponse(terminalSnapshot()));
          }
          if (url === RECEIPT_URL) {
            receiptGets += 1;
            if (receiptGets === 1) {
              return Promise.reject(new Error("initial receipt failure"));
            }
            return new Promise<Response>((_resolve, reject) => {
              rejectRetry = reject;
            });
          }
          throw new Error(`Unexpected request: ${url}`);
        }),
      );

      render(
        <>
          <button type="button">External focus sentinel</button>
          <App />
        </>,
      );
      await flushAppRequests();
      const retry = screen.getByRole("button", { name: "Retry receipt" });
      retry.focus();
      fireEvent.click(retry);
      await flushAppRequests();
      const focusSentinel = screen.getByRole("button", { name: "External focus sentinel" });
      focusSentinel.focus();
      expect(focusSentinel).toHaveFocus();

      await act(async () => {
        if (failure === "timeout") {
          vi.advanceTimersByTime(RECEIPT_REQUEST_TIMEOUT_MS);
        } else {
          rejectRetry?.(new DOMException("current transport abort", "AbortError"));
        }
        await Promise.resolve();
        await Promise.resolve();
      });

      expect(screen.getByRole("button", { name: "Retry receipt" })).toBeEnabled();
      expect(focusSentinel).toHaveFocus();
      expect(receiptGets).toBe(2);
    },
  );
});
