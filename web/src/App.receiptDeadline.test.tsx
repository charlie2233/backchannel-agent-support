import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import App, { RECEIPT_REQUEST_TIMEOUT_MS } from "./App";

const HOTEL_RECOVERY_KEY = "backchannel.hotelRecoveryId";
const RECOVERY_ID = "11111111-2222-4333-8444-555555555555";
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
