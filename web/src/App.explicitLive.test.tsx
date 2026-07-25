import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import App, { SAVED_RECOVERY_RESTORE_TIMEOUT_MS } from "./App";

const HOTEL_RECOVERY_KEY = "backchannel.hotelRecoveryId";
const HOTEL_CREATION_INTENT_KEY =
  "backchannel.pendingRecoveryCreation.v1.hotel";
const LIVE_RECOVERY_ID = "11111111-2222-4333-8444-555555555555";
const RETRYABLE_RESUME_COPY =
  "Saved recovery evidence is temporarily unavailable. Its same-tab recovery ID was retained, and no server state or action has been accepted.";

function liveHealth(): Response {
  return new Response(
    JSON.stringify({
      backend: "openai",
      liveReady: true,
      providerBoundary: "demo_adapter_only",
    }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );
}

function liveSnapshot(
  overrides: Partial<Record<string, unknown>> = {},
): Record<string, unknown> {
  return {
    recoveryId: LIVE_RECOVERY_ID,
    scenarioId: "hotel",
    executionMode: "openai_live",
    modelIds: ["gpt-5.6-luna", "gpt-5.6-terra"],
    rootTraceId: "trace_0123456789abcdef0123456789abcdef",
    status: "in_progress",
    currentStep: 1,
    currentStepSummary: "Authoritative live recovery loaded.",
    createdAt: "2026-07-19T12:00:00Z",
    updatedAt: "2026-07-19T12:00:01Z",
    pendingApproval: null,
    claimedDecision: null,
    ...overrides,
  };
}

function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function expectNoSnapshotLifecycle(label: "Not started" | "Awaiting server evidence") {
  const lifecycle = screen.getByRole("list", { name: "Recovery lifecycle" });
  expect(within(lifecycle).queryByRole("listitem", { current: "step" })).not.toBeInTheDocument();
  expect(screen.getByText(label, { selector: ".step-count" })).toBeVisible();
  expect(screen.queryByText("Step 1 of 6")).not.toBeInTheDocument();
}

function focusDocumentBody() {
  document.body.tabIndex = -1;
  document.body.focus();
  expect(document.body).toHaveFocus();
}

afterEach(() => {
  cleanup();
  try {
    window.sessionStorage.clear();
  } catch {
    // Storage-denial behavior is exercised explicitly below.
  }
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
  document.body.removeAttribute("tabindex");
});

describe("explicit, reload-safe live recovery", () => {
  it("keeps the saved-recovery deadline above the SQLite busy timeout", () => {
    expect(SAVED_RECOVERY_RESTORE_TIMEOUT_MS).toBe(12_000);
  });

  it("keeps a live-ready StrictMode mount idle until the accessible live action is used", async () => {
    const recoveryPosts: RequestInit[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/health") return Promise.resolve(liveHealth());
        if (url === "/api/recoveries") {
          recoveryPosts.push(init ?? {});
          return Promise.resolve(jsonResponse(liveSnapshot(), 201));
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    render(
      <StrictMode>
        <App />
      </StrictMode>,
    );

    expect(await screen.findByRole("button", { name: "Start live recovery" })).toBeVisible();
    expect(recoveryPosts).toHaveLength(0);
    expect(screen.getAllByText("No server run started.")[0]).toBeVisible();
    expectNoSnapshotLifecycle("Not started");
    expect(screen.queryByText("hotel-consent-v1")).not.toBeInTheDocument();
    expect(screen.queryByText("Replay fixture")).not.toBeInTheDocument();
  });

  it("synchronously coalesces rapid live activation to one POST and stores its UUID", async () => {
    let settleStart: ((response: Response) => void) | undefined;
    const requestModes: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/health") return Promise.resolve(liveHealth());
        if (url === "/api/recoveries") {
          requestModes.push(
            (JSON.parse(String(init?.body)) as { executionMode: string }).executionMode,
          );
          return new Promise<Response>((resolve) => {
            settleStart = resolve;
          });
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    render(
      <StrictMode>
        <App />
      </StrictMode>,
    );
    const start = await screen.findByRole("button", { name: "Start live recovery" });

    fireEvent.click(start);
    fireEvent.click(start);

    expect(requestModes).toEqual(["openai_live"]);
    expectNoSnapshotLifecycle("Awaiting server evidence");
    expect(screen.queryByText("No server run started.")).not.toBeInTheDocument();
    expect(screen.queryByText("Replay fixture")).not.toBeInTheDocument();
    await act(async () => {
      settleStart?.(jsonResponse(liveSnapshot(), 201));
      await Promise.resolve();
    });
    expect((await screen.findAllByText("Authoritative live recovery loaded."))[0]).toBeVisible();
    expect(window.sessionStorage.getItem(HOTEL_RECOVERY_KEY)).toBe(LIVE_RECOVERY_ID);
  });

  it("shows neutral awaiting evidence while a validated resume remains unresolved", async () => {
    let settleResume: ((response: Response) => void) | undefined;
    const requests: Array<{ url: string; method: string }> = [];
    window.sessionStorage.setItem(HOTEL_RECOVERY_KEY, LIVE_RECOVERY_ID);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        requests.push({ url, method: init?.method ?? "GET" });
        if (url === "/health") return Promise.resolve(liveHealth());
        if (url === `/api/recoveries/${LIVE_RECOVERY_ID}`) {
          return new Promise<Response>((resolve) => {
            settleResume = resolve;
          });
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    render(<App />);
    await waitFor(() => expect(settleResume).toBeTypeOf("function"));

    expectNoSnapshotLifecycle("Awaiting server evidence");
    expect(screen.queryByText("No server run started.")).not.toBeInTheDocument();
    expect(screen.queryByText("Replay fixture")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Start live recovery" })).not.toBeInTheDocument();
    const quotaScenario = screen.getByRole("button", { name: /API quota recovery/i });
    expect(quotaScenario).toBeDisabled();
    fireEvent.click(quotaScenario);
    expect(requests.filter(({ method }) => method === "POST")).toHaveLength(0);
    expectNoSnapshotLifecycle("Awaiting server evidence");

    await act(async () => {
      settleResume?.(jsonResponse(liveSnapshot()));
      await Promise.resolve();
    });
    expect((await screen.findAllByText("Authoritative live recovery loaded."))[0]).toBeVisible();
    expect(quotaScenario).toBeEnabled();
  });

  it("resumes a validated pending recovery before fallback without creating a run", async () => {
    class ControlledEventSource {
      static urls: string[] = [];
      onmessage: ((event: MessageEvent<string>) => void) | null = null;
      onerror: ((event: Event) => void) | null = null;
      close = vi.fn();
      addEventListener = vi.fn();
      removeEventListener = vi.fn();

      constructor(url: string) {
        ControlledEventSource.urls.push(url);
      }
    }
    vi.stubGlobal("EventSource", ControlledEventSource);
    window.sessionStorage.setItem(HOTEL_RECOVERY_KEY, LIVE_RECOVERY_ID);
    const requests: Array<{ url: string; method: string }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        requests.push({ url, method: init?.method ?? "GET" });
        if (url === "/health") return Promise.resolve(liveHealth());
        if (url === `/api/recoveries/${LIVE_RECOVERY_ID}`) {
          return Promise.resolve(
            jsonResponse(
              liveSnapshot({
                status: "pending_approval",
                currentStep: 3,
                currentStepSummary: "Restored exact consent.",
                pendingApproval: {
                  remedyId: "remedy-live",
                  remedyDigest: `sha256:${"a".repeat(64)}`,
                  terms: {
                    bookingId: "booking-live",
                    action: "replace_room",
                    replacement: { fromRoomType: "double", toRoomType: "king" },
                    stay: { checkIn: "2026-08-14", checkOut: "2026-08-16" },
                    currency: "USD",
                  },
                  costDeltaMinor: 0,
                  changedFields: ["room_type"],
                  providerCommitments: ["No additional charge"],
                  expiry: "2099-08-01T18:45:30Z",
                  hardConstraintSatisfied: true,
                  delegatedAuthoritySatisfied: true,
                  toolCallId: "server-call-live",
                  executionStarted: false,
                },
              }),
            ),
          );
        }
        if (url === "/api/recoveries") {
          return Promise.reject(new Error("A resume must not create another run"));
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    render(
      <StrictMode>
        <App />
      </StrictMode>,
    );

    expect((await screen.findAllByText("Restored exact consent."))[0]).toBeVisible();
    expect(screen.getByRole("button", { name: "Approve remedy" })).toBeVisible();
    await waitFor(() =>
      expect(ControlledEventSource.urls).toEqual([
        `/api/recoveries/${LIVE_RECOVERY_ID}/events`,
      ]),
    );
    expect(requests).toContainEqual({
      url: `/api/recoveries/${LIVE_RECOVERY_ID}`,
      method: "GET",
    });
    expect(requests.filter(({ url }) => url === "/api/recoveries")).toHaveLength(0);
  });

  it("restores a durable claim without posting until the explicit resume action", async () => {
    class ControlledEventSource {
      onmessage: ((event: MessageEvent<string>) => void) | null = null;
      onerror: ((event: Event) => void) | null = null;
      close = vi.fn();
      addEventListener = vi.fn();
      removeEventListener = vi.fn();
    }
    vi.stubGlobal("EventSource", ControlledEventSource);
    window.sessionStorage.setItem(HOTEL_RECOVERY_KEY, LIVE_RECOVERY_ID);
    const digest = `sha256:${"a".repeat(64)}`;
    let resumePosts = 0;
    let resolveResume: ((response: Response) => void) | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/health") return Promise.resolve(liveHealth());
        if (url === `/api/recoveries/${LIVE_RECOVERY_ID}`) {
          return Promise.resolve(
            jsonResponse(
              liveSnapshot(
                resumePosts === 0
                  ? {
                      status: "pending_approval",
                      currentStep: 3,
                      currentStepSummary: "Exact approve claimed; outcome pending.",
                      claimedDecision: {
                        action: "approve",
                        remedyDigest: digest,
                        expiry: "2099-08-01T18:45:30Z",
                      },
                    }
                  : {
                      status: "completed",
                      currentStep: 5,
                      currentStepSummary: "Resumed execution completed.",
                    },
              ),
            ),
          );
        }
        if (url === `/api/recoveries/${LIVE_RECOVERY_ID}/decisions/resume`) {
          resumePosts += 1;
          expect(init?.method).toBe("POST");
          expect(init?.body).toBe("{}");
          return new Promise<Response>((resolve) => {
            resolveResume = resolve;
          });
        }
        if (url === `/api/recoveries/${LIVE_RECOVERY_ID}/receipt`) {
          return Promise.resolve(
            jsonResponse({
              recoveryId: LIVE_RECOVERY_ID,
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
              providerResult: "Resumed demo-adapter execution completed.",
              authorizationSource: "Previously claimed exact approval.",
              verificationResults: ["Existing durable claim resumed explicitly."],
              approvalCount: 1,
              approvedRemedyDigest: digest,
            }),
          );
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    render(
      <StrictMode>
        <App />
      </StrictMode>,
    );

    const resume = await screen.findByRole("button", { name: "Resume exact approval" });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(resumePosts).toBe(0);
    fireEvent.click(resume);
    fireEvent.click(resume);

    expect(resumePosts).toBe(1);
    resolveResume?.(
      jsonResponse({
        action: "approve",
        recoveryId: LIVE_RECOVERY_ID,
        remedyDigest: digest,
        status: "completed",
        approvedRemedyDigest: digest,
        executionStarted: true,
      }),
    );
    expect(await screen.findByText("Resumed execution completed.")).toBeVisible();
  });

  it("restores terminal snapshot and receipt with zero recovery POSTs", async () => {
    window.sessionStorage.setItem(HOTEL_RECOVERY_KEY, LIVE_RECOVERY_ID);
    const requests: Array<{ url: string; method: string }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        requests.push({ url, method: init?.method ?? "GET" });
        if (url === "/health") return Promise.resolve(liveHealth());
        if (url === `/api/recoveries/${LIVE_RECOVERY_ID}`) {
          return Promise.resolve(
            jsonResponse(
              liveSnapshot({
                status: "closed_without_action",
                currentStep: 5,
                currentStepSummary: "Restored closed recovery.",
              }),
            ),
          );
        }
        if (url === `/api/recoveries/${LIVE_RECOVERY_ID}/receipt`) {
          return Promise.resolve(
            jsonResponse({
              recoveryId: LIVE_RECOVERY_ID,
              executionMode: "openai_live",
              status: "closed_without_action",
              simulated: true,
              providerExecution: false,
              modelCall: true,
              modelIds: ["gpt-5.6-luna", "gpt-5.6-terra"],
              rootTraceId: "trace_0123456789abcdef0123456789abcdef",
              sdkVersion: "0.18.3",
              protocolVersion: "backchannel.approval.v1",
              agentGraphVersion: "backchannel.hotel-agent.v1",
              definitionDigest: "b".repeat(64),
              boundary:
                "OpenAI agent model calls and demo hotel adapter only; no real booking or payment change.",
              providerResult: "Restored receipt with zero dispatch.",
              authorizationSource: "Consent expired without approval.",
              verificationResults: ["No provider dispatch occurred."],
              approvalCount: 0,
              approvedRemedyDigest: null,
            }),
          );
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    render(<App />);

    expect((await screen.findAllByText("Restored receipt with zero dispatch."))[0]).toBeVisible();
    expect(requests.filter(({ url }) => url === "/api/recoveries")).toHaveLength(0);
  });

  it.each([
    ["malformed", "not-a-uuid", null],
    ["stale", LIVE_RECOVERY_ID, 404],
    ["foreign session", LIVE_RECOVERY_ID, 404],
    ["expired session", LIVE_RECOVERY_ID, 404],
    ["retention-deleted", LIVE_RECOVERY_ID, 404],
  ] as const)("clears a %s resume hint and remains explicitly idle", async (_label, hint, status) => {
    window.sessionStorage.setItem(HOTEL_RECOVERY_KEY, hint);
    const requestModes: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/health") return Promise.resolve(liveHealth());
        if (url === `/api/recoveries/${LIVE_RECOVERY_ID}` && status !== null) {
          return Promise.resolve(new Response(null, { status }));
        }
        if (url === "/api/recoveries") {
          requestModes.push(
            (JSON.parse(String(init?.body)) as { executionMode: string }).executionMode,
          );
          return Promise.resolve(jsonResponse(liveSnapshot(), 201));
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    render(<App />);

    expect(await screen.findByRole("button", { name: "Start live recovery" })).toBeVisible();
    expect(window.sessionStorage.getItem(HOTEL_RECOVERY_KEY)).toBeNull();
    expect(requestModes).toHaveLength(0);
    expect(screen.getAllByText("No server run started.")[0]).toBeVisible();
    expectNoSnapshotLifecycle("Not started");
  });

  it("retains an indeterminate hint and coalesces retry before restoring a claimed snapshot", async () => {
    window.sessionStorage.setItem(HOTEL_RECOVERY_KEY, LIVE_RECOVERY_ID);
    let recoveryGets = 0;
    let settleRetry: ((response: Response) => void) | undefined;
    const requests: Array<{ url: string; method: string }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method ?? "GET";
        requests.push({ url, method });
        if (url === "/health") return Promise.resolve(liveHealth());
        if (url === `/api/recoveries/${LIVE_RECOVERY_ID}` && method === "GET") {
          recoveryGets += 1;
          if (recoveryGets === 1) {
            return Promise.resolve(
              new Response("private upstream diagnostics", { status: 503 }),
            );
          }
          return new Promise<Response>((resolve) => {
            settleRetry = resolve;
          });
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      }),
    );

    render(<App />);

    const retry = await screen.findByRole("button", { name: "Retry saved recovery" });
    expect(screen.getByText(RETRYABLE_RESUME_COPY)).toBeVisible();
    expect(screen.queryByText(/private upstream diagnostics/i)).not.toBeInTheDocument();
    expect(window.sessionStorage.getItem(HOTEL_RECOVERY_KEY)).toBe(LIVE_RECOVERY_ID);
    expectNoSnapshotLifecycle("Awaiting server evidence");
    expect(screen.queryByRole("button", { name: "Start live recovery" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Run replay fixture" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Run SDK QA trace" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Resume exact/i })).not.toBeInTheDocument();
    expect(requests.filter(({ method }) => method === "POST")).toHaveLength(0);
    const quotaScenario = screen.getByRole("button", { name: /API quota recovery/i });
    expect(quotaScenario).toBeDisabled();
    fireEvent.click(quotaScenario);
    expect(screen.getByRole("button", { name: "Retry saved recovery" })).toBeVisible();
    expect(requests.filter(({ method }) => method === "POST")).toHaveLength(0);

    retry.focus();
    fireEvent.click(retry);
    fireEvent.click(retry);

    expect(recoveryGets).toBe(2);
    const retrying = screen.getByRole("button", { name: "Retrying saved recovery…" });
    expect(quotaScenario).toBeDisabled();
    fireEvent.click(quotaScenario);
    expect(retrying).toBe(retry);
    expect(retrying).toBeDisabled();
    expect(retrying).toHaveFocus();
    expect(retrying.closest("section")).toHaveAttribute("aria-busy", "true");
    expect(screen.getByText(RETRYABLE_RESUME_COPY)).toBeVisible();
    expect(requests.filter(({ method }) => method === "POST")).toHaveLength(0);
    expectNoSnapshotLifecycle("Awaiting server evidence");
    await act(async () => {
      settleRetry?.(
        jsonResponse(
          liveSnapshot({
            status: "pending_approval",
            currentStep: 3,
            currentStepSummary: "Exact approval remains durably claimed.",
            claimedDecision: {
              action: "approve",
              remedyDigest: `sha256:${"a".repeat(64)}`,
              expiry: "2099-08-01T18:45:30Z",
            },
          }),
        ),
      );
      await Promise.resolve();
    });

    expect(await screen.findByRole("button", { name: "Resume exact approval" })).toBeVisible();
    expect(window.sessionStorage.getItem(HOTEL_RECOVERY_KEY)).toBe(LIVE_RECOVERY_ID);
    expect(quotaScenario).toBeEnabled();
    expect(requests.filter(({ method }) => method === "POST")).toHaveLength(0);
  });

  it("turns a never-settling saved recovery GET into a retryable state at its deadline", async () => {
    vi.useFakeTimers();
    window.sessionStorage.setItem(HOTEL_RECOVERY_KEY, LIVE_RECOVERY_ID);
    const requests: Array<{ url: string; method: string }> = [];
    let recoveryGets = 0;
    let requestSignal: AbortSignal | undefined;
    const abortListener = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method ?? "GET";
        requests.push({ url, method });
        if (url === "/health") return Promise.resolve(liveHealth());
        if (url === `/api/recoveries/${LIVE_RECOVERY_ID}` && method === "GET") {
          recoveryGets += 1;
          requestSignal = init?.signal ?? undefined;
          requestSignal?.addEventListener("abort", abortListener);
          return new Promise<Response>(() => {});
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      }),
    );

    render(
      <>
        <button type="button">External focus sentinel</button>
        <App />
      </>,
    );
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(recoveryGets).toBe(1);
    expect(requestSignal?.aborted).toBe(false);
    const focusSentinel = screen.getByRole("button", { name: "External focus sentinel" });
    focusSentinel.focus();
    expect(focusSentinel).toHaveFocus();

    await act(async () => {
      vi.advanceTimersByTime(SAVED_RECOVERY_RESTORE_TIMEOUT_MS - 1);
      await Promise.resolve();
    });
    expect(screen.queryByRole("button", { name: "Retry saved recovery" })).not.toBeInTheDocument();

    await act(async () => {
      vi.advanceTimersByTime(1);
      await Promise.resolve();
    });

    const retry = screen.getByRole("button", { name: "Retry saved recovery" });
    expect(requestSignal?.aborted).toBe(true);
    expect(abortListener).toHaveBeenCalledTimes(1);
    expect(window.sessionStorage.getItem(HOTEL_RECOVERY_KEY)).toBe(LIVE_RECOVERY_ID);
    expect(screen.getByText(RETRYABLE_RESUME_COPY)).toBeVisible();
    expect(retry).toBeEnabled();
    expect(focusSentinel).toHaveFocus();
    retry.focus();
    expect(retry).toHaveFocus();
    expect(requests.filter(({ method }) => method === "POST")).toHaveLength(0);
    expectNoSnapshotLifecycle("Awaiting server evidence");
  });

  it("coalesces a rapid saved-recovery retry and makes a timed-out retry usable again", async () => {
    vi.useFakeTimers();
    window.sessionStorage.setItem(HOTEL_RECOVERY_KEY, LIVE_RECOVERY_ID);
    const requests: Array<{ url: string; method: string }> = [];
    const requestSignals: AbortSignal[] = [];
    const abortListeners: ReturnType<typeof vi.fn>[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method ?? "GET";
        requests.push({ url, method });
        if (url === "/health") return Promise.resolve(liveHealth());
        if (url === `/api/recoveries/${LIVE_RECOVERY_ID}` && method === "GET") {
          const signal = init?.signal;
          if (!(signal instanceof AbortSignal)) throw new Error("Saved recovery GET must be abortable");
          const listener = vi.fn();
          signal.addEventListener("abort", listener);
          requestSignals.push(signal);
          abortListeners.push(listener);
          return new Promise<Response>(() => {});
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      }),
    );

    render(<App />);
    await act(async () => {
      await Promise.resolve();
      vi.advanceTimersByTime(SAVED_RECOVERY_RESTORE_TIMEOUT_MS);
      await Promise.resolve();
    });
    const retry = screen.getByRole("button", { name: "Retry saved recovery" });
    retry.focus();
    expect(retry).toHaveFocus();

    fireEvent.click(retry);
    fireEvent.click(retry);

    expect(requestSignals).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Retrying saved recovery…" })).toBeDisabled();
    retry.blur();
    focusDocumentBody();
    expect(retry).not.toHaveFocus();
    expect(requests.filter(({ method }) => method === "POST")).toHaveLength(0);

    await act(async () => {
      vi.advanceTimersByTime(SAVED_RECOVERY_RESTORE_TIMEOUT_MS);
      await Promise.resolve();
    });

    const reusableRetry = screen.getByRole("button", { name: "Retry saved recovery" });
    expect(requestSignals.map(({ aborted }) => aborted)).toEqual([true, true]);
    expect(abortListeners.map((listener) => listener.mock.calls.length)).toEqual([1, 1]);
    expect(reusableRetry).toBe(retry);
    expect(reusableRetry).toBeEnabled();
    expect(reusableRetry).toHaveFocus();
    expect(window.sessionStorage.getItem(HOTEL_RECOVERY_KEY)).toBe(LIVE_RECOVERY_ID);
    expect(requests.filter(({ method }) => method === "POST")).toHaveLength(0);

    fireEvent.click(reusableRetry);

    expect(requestSignals).toHaveLength(3);
    expect(requestSignals.map(({ aborted }) => aborted)).toEqual([true, true, false]);
    expect(screen.getByRole("button", { name: "Retrying saved recovery…" })).toBe(retry);
    expect(requests.filter(({ method }) => method === "POST")).toHaveLength(0);
  });

  it("retires a successful saved recovery before its deadline can replace the result", async () => {
    vi.useFakeTimers();
    const setTimeoutSpy = vi.spyOn(globalThis, "setTimeout");
    const clearTimeoutSpy = vi.spyOn(globalThis, "clearTimeout");
    window.sessionStorage.setItem(HOTEL_RECOVERY_KEY, LIVE_RECOVERY_ID);
    let settleResume: ((response: Response) => void) | undefined;
    let requestSignal: AbortSignal | undefined;
    const abortListener = vi.fn();
    const requests: Array<{ url: string; method: string }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method ?? "GET";
        requests.push({ url, method });
        if (url === "/health") return Promise.resolve(liveHealth());
        if (url === `/api/recoveries/${LIVE_RECOVERY_ID}` && method === "GET") {
          requestSignal = init?.signal ?? undefined;
          requestSignal?.addEventListener("abort", abortListener);
          return new Promise<Response>((resolve) => {
            settleResume = resolve;
          });
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      }),
    );

    render(<App />);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    const deadlineCallIndex = setTimeoutSpy.mock.calls.findIndex(
      ([, delay]) => delay === SAVED_RECOVERY_RESTORE_TIMEOUT_MS,
    );
    expect(deadlineCallIndex).toBeGreaterThanOrEqual(0);
    const deadlineHandle = setTimeoutSpy.mock.results[deadlineCallIndex]?.value;

    await act(async () => {
      vi.advanceTimersByTime(SAVED_RECOVERY_RESTORE_TIMEOUT_MS - 1);
      settleResume?.(
        jsonResponse(liveSnapshot({ currentStepSummary: "Restored before deadline." })),
      );
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(screen.getAllByText("Restored before deadline.")[0]).toBeVisible();
    expect(clearTimeoutSpy).toHaveBeenCalledWith(deadlineHandle);
    expect(requestSignal?.aborted).toBe(false);

    await act(async () => {
      vi.advanceTimersByTime(SAVED_RECOVERY_RESTORE_TIMEOUT_MS);
      await Promise.resolve();
    });

    expect(requestSignal?.aborted).toBe(false);
    expect(abortListener).not.toHaveBeenCalled();
    expect(screen.getAllByText("Restored before deadline.")[0]).toBeVisible();
    expect(screen.queryByRole("button", { name: "Retry saved recovery" })).not.toBeInTheDocument();
    expect(requests.filter(({ method }) => method === "POST")).toHaveLength(0);
  });

  it("makes a current initial restoration AbortError retryable", async () => {
    window.sessionStorage.setItem(HOTEL_RECOVERY_KEY, LIVE_RECOVERY_ID);
    const requests: Array<{ url: string; method: string }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method ?? "GET";
        requests.push({ url, method });
        if (url === "/health") return Promise.resolve(liveHealth());
        if (url === `/api/recoveries/${LIVE_RECOVERY_ID}` && method === "GET") {
          return Promise.reject(new DOMException("current transport abort", "AbortError"));
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      }),
    );

    render(<App />);

    const retry = await screen.findByRole("button", { name: "Retry saved recovery" });
    expect(retry).toBeEnabled();
    expect(window.sessionStorage.getItem(HOTEL_RECOVERY_KEY)).toBe(LIVE_RECOVERY_ID);
    expect(requests.filter(({ method }) => method === "POST")).toHaveLength(0);
  });

  it("makes a current explicit-retry AbortError retryable again", async () => {
    window.sessionStorage.setItem(HOTEL_RECOVERY_KEY, LIVE_RECOVERY_ID);
    let recoveryGets = 0;
    let rejectRetry: ((reason: unknown) => void) | undefined;
    const requests: Array<{ url: string; method: string }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method ?? "GET";
        requests.push({ url, method });
        if (url === "/health") return Promise.resolve(liveHealth());
        if (url === `/api/recoveries/${LIVE_RECOVERY_ID}` && method === "GET") {
          recoveryGets += 1;
          return recoveryGets === 1
            ? Promise.resolve(new Response(null, { status: 503 }))
            : new Promise<Response>((_resolve, reject) => {
                rejectRetry = reject;
              });
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      }),
    );

    render(<App />);
    const retry = await screen.findByRole("button", { name: "Retry saved recovery" });
    retry.focus();
    expect(retry).toHaveFocus();

    fireEvent.click(retry);

    expect(screen.getByRole("button", { name: "Retrying saved recovery…" })).toBe(retry);
    retry.blur();
    focusDocumentBody();
    expect(retry).not.toHaveFocus();
    await act(async () => {
      rejectRetry?.(new DOMException("current retry abort", "AbortError"));
      await Promise.resolve();
      await Promise.resolve();
    });
    const reusableRetry = screen.getByRole("button", { name: "Retry saved recovery" });
    expect(reusableRetry).toBe(retry);
    expect(reusableRetry).toBeEnabled();
    expect(reusableRetry).toHaveFocus();
    expect(recoveryGets).toBe(2);
    expect(window.sessionStorage.getItem(HOTEL_RECOVERY_KEY)).toBe(LIVE_RECOVERY_ID);
    expect(requests.filter(({ method }) => method === "POST")).toHaveLength(0);
  });

  it.each(["timeout", "AbortError"] as const)(
    "preserves deliberate focus during an explicit retry %s",
    async (failure) => {
      window.sessionStorage.setItem(HOTEL_RECOVERY_KEY, LIVE_RECOVERY_ID);
      let recoveryGets = 0;
      let rejectRetry: ((reason: unknown) => void) | undefined;
      const requests: Array<{ url: string; method: string }> = [];
      vi.stubGlobal(
        "fetch",
        vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
          const url = String(input);
          const method = init?.method ?? "GET";
          requests.push({ url, method });
          if (url === "/health") return Promise.resolve(liveHealth());
          if (url === `/api/recoveries/${LIVE_RECOVERY_ID}` && method === "GET") {
            recoveryGets += 1;
            if (recoveryGets === 1) {
              return Promise.resolve(new Response(null, { status: 503 }));
            }
            return new Promise<Response>((_resolve, reject) => {
              rejectRetry = reject;
            });
          }
          throw new Error(`Unexpected request: ${method} ${url}`);
        }),
      );

      render(
        <>
          <button type="button">Deliberate request focus</button>
          <App />
        </>,
      );
      const retry = await screen.findByRole("button", { name: "Retry saved recovery" });
      if (failure === "timeout") vi.useFakeTimers();
      retry.focus();
      fireEvent.click(retry);
      expect(screen.getByRole("button", { name: "Retrying saved recovery…" })).toBe(retry);
      retry.blur();
      const deliberateTarget = screen.getByRole("button", { name: "Deliberate request focus" });
      deliberateTarget.focus();
      expect(deliberateTarget).toHaveFocus();

      await act(async () => {
        if (failure === "timeout") {
          vi.advanceTimersByTime(SAVED_RECOVERY_RESTORE_TIMEOUT_MS);
        } else {
          rejectRetry?.(new DOMException("current retry abort", "AbortError"));
        }
        await Promise.resolve();
        await Promise.resolve();
      });

      const reusableRetry = screen.getByRole("button", { name: "Retry saved recovery" });
      expect(reusableRetry).toBe(retry);
      expect(reusableRetry).toBeEnabled();
      expect(deliberateTarget).toHaveFocus();
      expect(recoveryGets).toBe(2);
      expect(window.sessionStorage.getItem(HOTEL_RECOVERY_KEY)).toBe(LIVE_RECOVERY_ID);
      expect(requests.filter(({ method }) => method === "POST")).toHaveLength(0);
    },
  );

  it.each(["resolve", "reject"] as const)(
    "ignores a late %s from a timed-out restoration after a newer retry succeeds",
    async (lateOutcome) => {
      vi.useFakeTimers();
      window.sessionStorage.setItem(HOTEL_RECOVERY_KEY, LIVE_RECOVERY_ID);
      let recoveryGets = 0;
      let resolveFirst: ((response: Response) => void) | undefined;
      let rejectFirst: ((reason: unknown) => void) | undefined;
      const requests: Array<{ url: string; method: string }> = [];
      vi.stubGlobal(
        "fetch",
        vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
          const url = String(input);
          const method = init?.method ?? "GET";
          requests.push({ url, method });
          if (url === "/health") return Promise.resolve(liveHealth());
          if (url === `/api/recoveries/${LIVE_RECOVERY_ID}` && method === "GET") {
            recoveryGets += 1;
            if (recoveryGets === 1) {
              return new Promise<Response>((resolve, reject) => {
                resolveFirst = resolve;
                rejectFirst = reject;
              });
            }
            return Promise.resolve(
              jsonResponse(
                liveSnapshot({ currentStepSummary: "Newer saved recovery evidence restored." }),
              ),
            );
          }
          throw new Error(`Unexpected request: ${method} ${url}`);
        }),
      );

      render(<App />);
      await act(async () => {
        await Promise.resolve();
        vi.advanceTimersByTime(SAVED_RECOVERY_RESTORE_TIMEOUT_MS);
        await Promise.resolve();
      });
      fireEvent.click(screen.getByRole("button", { name: "Retry saved recovery" }));
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(screen.getAllByText("Newer saved recovery evidence restored.")[0]).toBeVisible();

      await act(async () => {
        if (lateOutcome === "resolve") {
          resolveFirst?.(
            jsonResponse(liveSnapshot({ currentStepSummary: "Stale restoration accepted." })),
          );
        } else {
          rejectFirst?.(new Error("late private transport failure"));
        }
        await Promise.resolve();
        await Promise.resolve();
      });

      expect(screen.getAllByText("Newer saved recovery evidence restored.")[0]).toBeVisible();
      expect(screen.queryByText("Stale restoration accepted.")).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Retry saved recovery" })).not.toBeInTheDocument();
      expect(window.sessionStorage.getItem(HOTEL_RECOVERY_KEY)).toBe(LIVE_RECOVERY_ID);
      expect(requests.filter(({ method }) => method === "POST")).toHaveLength(0);
    },
  );

  it("clears the restoration deadline and aborts once on unmount without accepting a late result", async () => {
    vi.useFakeTimers();
    const setTimeoutSpy = vi.spyOn(globalThis, "setTimeout");
    const clearTimeoutSpy = vi.spyOn(globalThis, "clearTimeout");
    window.sessionStorage.setItem(HOTEL_RECOVERY_KEY, LIVE_RECOVERY_ID);
    let settleResume: ((response: Response) => void) | undefined;
    let requestSignal: AbortSignal | undefined;
    const abortListener = vi.fn();
    const requests: Array<{ url: string; method: string }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method ?? "GET";
        requests.push({ url, method });
        if (url === "/health") return Promise.resolve(liveHealth());
        if (url === `/api/recoveries/${LIVE_RECOVERY_ID}` && method === "GET") {
          requestSignal = init?.signal ?? undefined;
          requestSignal?.addEventListener("abort", abortListener);
          return new Promise<Response>((resolve) => {
            settleResume = resolve;
          });
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      }),
    );

    const view = render(<App />);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(requestSignal?.aborted).toBe(false);
    const deadlineCallIndex = setTimeoutSpy.mock.calls.findIndex(
      ([, delay]) => delay === SAVED_RECOVERY_RESTORE_TIMEOUT_MS,
    );
    expect(deadlineCallIndex).toBeGreaterThanOrEqual(0);
    const deadlineHandle = setTimeoutSpy.mock.results[deadlineCallIndex]?.value;

    view.unmount();

    expect(requestSignal?.aborted).toBe(true);
    expect(abortListener).toHaveBeenCalledTimes(1);
    expect(clearTimeoutSpy).toHaveBeenCalledWith(deadlineHandle);
    window.sessionStorage.setItem(
      HOTEL_RECOVERY_KEY,
      "22222222-2222-4222-8222-222222222222",
    );
    await act(async () => {
      settleResume?.(
        jsonResponse(liveSnapshot({ currentStepSummary: "Late unmounted restoration." })),
      );
      vi.advanceTimersByTime(SAVED_RECOVERY_RESTORE_TIMEOUT_MS * 2);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(abortListener).toHaveBeenCalledTimes(1);
    expect(window.sessionStorage.getItem(HOTEL_RECOVERY_KEY)).toBe(
      "22222222-2222-4222-8222-222222222222",
    );
    expect(requests.filter(({ method }) => method === "POST")).toHaveLength(0);
  });

  it("retains a miscorrelated response, then clears the hint when explicit retry returns 404", async () => {
    window.sessionStorage.setItem(HOTEL_RECOVERY_KEY, LIVE_RECOVERY_ID);
    let recoveryGets = 0;
    const requests: Array<{ url: string; method: string }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method ?? "GET";
        requests.push({ url, method });
        if (url === "/health") return Promise.resolve(liveHealth());
        if (url === `/api/recoveries/${LIVE_RECOVERY_ID}` && method === "GET") {
          recoveryGets += 1;
          return Promise.resolve(
            recoveryGets === 1
              ? jsonResponse(
                  liveSnapshot({
                    recoveryId: "99999999-2222-4333-8444-555555555555",
                  }),
                )
              : new Response(null, { status: 404 }),
          );
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      }),
    );

    render(<App />);

    const retry = await screen.findByRole("button", { name: "Retry saved recovery" });
    expect(window.sessionStorage.getItem(HOTEL_RECOVERY_KEY)).toBe(LIVE_RECOVERY_ID);
    expectNoSnapshotLifecycle("Awaiting server evidence");
    expect(requests.filter(({ method }) => method === "POST")).toHaveLength(0);

    fireEvent.click(retry);

    expect(await screen.findByRole("button", { name: "Start live recovery" })).toBeVisible();
    expect(recoveryGets).toBe(2);
    expect(window.sessionStorage.getItem(HOTEL_RECOVERY_KEY)).toBeNull();
    expectNoSnapshotLifecycle("Not started");
    expect(requests.filter(({ method }) => method === "POST")).toHaveLength(0);
  });

  it("clears a correlated wrong-scenario snapshot as terminal without creating a run", async () => {
    window.sessionStorage.setItem(HOTEL_RECOVERY_KEY, LIVE_RECOVERY_ID);
    const requests: Array<{ url: string; method: string }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method ?? "GET";
        requests.push({ url, method });
        if (url === "/health") return Promise.resolve(liveHealth());
        if (url === `/api/recoveries/${LIVE_RECOVERY_ID}`) {
          return Promise.resolve(
            jsonResponse(
              liveSnapshot({
                scenarioId: "api-quota",
                executionMode: "sdk_stub",
                modelIds: [],
                rootTraceId: "qa_trace_0123456789abcdef0123456789abcdef",
              }),
            ),
          );
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      }),
    );

    render(<App />);

    expect(await screen.findByRole("button", { name: "Start live recovery" })).toBeVisible();
    expect(window.sessionStorage.getItem(HOTEL_RECOVERY_KEY)).toBeNull();
    expectNoSnapshotLifecycle("Not started");
    expect(requests.filter(({ method }) => method === "POST")).toHaveLength(0);
  });

  it.each([
    { action: "Run replay fixture", mode: "replay_fixture" },
    { action: "Run SDK QA trace", mode: "sdk_stub" },
  ] as const)(
    "keeps an explicitly requested $mode singular while its start is unresolved",
    async ({ action, mode }) => {
      window.sessionStorage.setItem(HOTEL_RECOVERY_KEY, LIVE_RECOVERY_ID);
      const requestModes: string[] = [];
      vi.stubGlobal(
        "fetch",
        vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
          const url = String(input);
          if (url === "/health") {
            return Promise.resolve(
              jsonResponse({
                backend: "stub",
                liveReady: false,
                providerBoundary: "demo_adapter_only",
              }),
            );
          }
          if (url === `/api/recoveries/${LIVE_RECOVERY_ID}`) {
            return Promise.resolve(new Response(null, { status: 404 }));
          }
          if (url === "/api/recoveries") {
            requestModes.push(
              (JSON.parse(String(init?.body)) as { executionMode: string }).executionMode,
            );
            return new Promise<Response>(() => {});
          }
          throw new Error(`Unexpected request: ${url}`);
        }),
      );

      render(<App />);

      const replay = await screen.findByRole(
        "button",
        { name: "Run replay fixture" },
        { timeout: 10_000 },
      );
      const sdk = screen.getByRole("button", { name: "Run SDK QA trace" });
      expect(replay).toBeVisible();
      expect(sdk).toBeVisible();
      expect(requestModes).toEqual([]);
      expect(window.sessionStorage.getItem(HOTEL_RECOVERY_KEY)).toBeNull();
      expect(screen.getByText(/No fallback run has started/i)).toBeVisible();
      expect(screen.queryByText(/starting automatically/i)).not.toBeInTheDocument();
      expectNoSnapshotLifecycle("Not started");

      fireEvent.click(screen.getByRole("button", { name: action }));

      expect(requestModes).toEqual([mode]);
      expect(screen.queryByText(/No fallback run has started/i)).not.toBeInTheDocument();
      expectNoSnapshotLifecycle("Awaiting server evidence");
      expect(screen.queryByText("No server run started.")).not.toBeInTheDocument();
      expect(screen.queryByText("Replay fixture")).not.toBeInTheDocument();
      expect(screen.queryByText("SDK stub")).not.toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "Retrying recovery start…" }),
      ).toBeDisabled();
      expect(
        screen.queryByRole("button", { name: "Run replay fixture" }),
      ).not.toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: "Run SDK QA trace" }),
      ).not.toBeInTheDocument();
    },
  );

  it.each([
    { action: "Run replay fixture", mode: "replay_fixture" },
    { action: "Run SDK QA trace", mode: "sdk_stub" },
  ] as const)(
    "retains and explicitly retries the same $mode intent after an ambiguous rejection",
    async ({ action, mode }) => {
      window.sessionStorage.setItem(HOTEL_RECOVERY_KEY, LIVE_RECOVERY_ID);
      const requestBodies: Array<{
        clientRequestId: string;
        scenarioId: string;
        executionMode: string;
      }> = [];
      vi.stubGlobal(
        "fetch",
        vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
          const url = String(input);
          if (url === "/health") {
            return Promise.resolve(
              jsonResponse({
                backend: "stub",
                liveReady: false,
                providerBoundary: "demo_adapter_only",
              }),
            );
          }
          if (url === `/api/recoveries/${LIVE_RECOVERY_ID}`) {
            return Promise.resolve(new Response(null, { status: 404 }));
          }
          if (url === "/api/recoveries") {
            requestBodies.push(
              JSON.parse(String(init?.body)) as {
                clientRequestId: string;
                scenarioId: string;
                executionMode: string;
              },
            );
            return Promise.resolve(new Response(null, { status: 503 }));
          }
          throw new Error(`Unexpected request: ${url}`);
        }),
      );

      render(<App />);
      fireEvent.click(
        await screen.findByRole("button", { name: action }, { timeout: 10_000 }),
      );

      await screen.findByRole("alert");
      expect(requestBodies).toHaveLength(1);
      expect(requestBodies[0]).toMatchObject({
        scenarioId: "hotel",
        executionMode: mode,
      });
      expect(
        screen.queryByRole("button", { name: "Run replay fixture" }),
      ).not.toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: "Run SDK QA trace" }),
      ).not.toBeInTheDocument();
      const retry = screen.getByRole("button", {
        name: "Retry recovery start",
      });
      expect(retry).toBeEnabled();
      expectNoSnapshotLifecycle("Awaiting server evidence");

      fireEvent.click(retry);

      await waitFor(() => expect(requestBodies).toHaveLength(2));
      expect(requestBodies[1]).toEqual(requestBodies[0]);
    },
  );

  it("does not accept a live-start result after unmount", async () => {
    let settleStart: ((response: Response) => void) | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request) => {
        const url = String(input);
        if (url === "/health") return Promise.resolve(liveHealth());
        if (url === "/api/recoveries") {
          return new Promise<Response>((resolve) => {
            settleStart = resolve;
          });
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    const view = render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Start live recovery" }));
    view.unmount();

    await act(async () => {
      settleStart?.(jsonResponse(liveSnapshot(), 201));
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(window.sessionStorage.getItem(HOTEL_RECOVERY_KEY)).toBe(
      LIVE_RECOVERY_ID,
    );
    expect(
      window.sessionStorage.getItem(HOTEL_CREATION_INTENT_KEY),
    ).toBeNull();
  });

  it("does not accept a resume result after unmount", async () => {
    let settleResume: ((response: Response) => void) | undefined;
    window.sessionStorage.setItem(HOTEL_RECOVERY_KEY, LIVE_RECOVERY_ID);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request) => {
        const url = String(input);
        if (url === "/health") return Promise.resolve(liveHealth());
        if (url === `/api/recoveries/${LIVE_RECOVERY_ID}`) {
          return new Promise<Response>((resolve) => {
            settleResume = resolve;
          });
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    const view = render(<App />);
    await waitFor(() => expect(settleResume).toBeTypeOf("function"));
    view.unmount();
    window.sessionStorage.setItem(
      HOTEL_RECOVERY_KEY,
      "22222222-2222-4222-8222-222222222222",
    );

    await act(async () => {
      settleResume?.(jsonResponse(liveSnapshot()));
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(window.sessionStorage.getItem(HOTEL_RECOVERY_KEY)).toBe(
      "22222222-2222-4222-8222-222222222222",
    );
  });
});
