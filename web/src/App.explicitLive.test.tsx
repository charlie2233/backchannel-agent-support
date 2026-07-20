import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "./App";

const HOTEL_RECOVERY_KEY = "backchannel.hotelRecoveryId";
const LIVE_RECOVERY_ID = "11111111-2222-4333-8444-555555555555";
const AWAITING_DEMO_COPY =
  "Awaiting authoritative server evidence for the explicitly requested demo run.";

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

afterEach(() => {
  cleanup();
  try {
    window.sessionStorage.clear();
  } catch {
    // Storage-denial behavior is exercised explicitly below.
  }
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("explicit, reload-safe live recovery", () => {
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

    render(<App />);
    await waitFor(() => expect(settleResume).toBeTypeOf("function"));

    expectNoSnapshotLifecycle("Awaiting server evidence");
    expect(screen.queryByText("No server run started.")).not.toBeInTheDocument();
    expect(screen.queryByText("Replay fixture")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Start live recovery" })).not.toBeInTheDocument();

    await act(async () => {
      settleResume?.(jsonResponse(liveSnapshot()));
      await Promise.resolve();
    });
    expect((await screen.findAllByText("Authoritative live recovery loaded."))[0]).toBeVisible();
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

  it.each([
    { action: "Run replay fixture", mode: "replay_fixture" },
    { action: "Run SDK QA trace", mode: "sdk_stub" },
  ] as const)(
    "offers an explicit $mode after a failed keyless resume without auto-starting",
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
      if (mode === "replay_fixture") {
        const sdkWhileReplayPending = screen.getByRole("button", {
          name: "Run SDK QA trace",
        });
        expect(sdkWhileReplayPending).toBeDisabled();
        fireEvent.click(sdkWhileReplayPending);
        expect(requestModes).toEqual(["replay_fixture"]);
      }
    },
  );

  it.each([
    { action: "Run replay fixture", mode: "replay_fixture" },
    { action: "Run SDK QA trace", mode: "sdk_stub" },
  ] as const)(
    "restores explicit demo choices when a requested $mode is rejected",
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
            return Promise.resolve(new Response(null, { status: 503 }));
          }
          throw new Error(`Unexpected request: ${url}`);
        }),
      );

      render(<App />);
      fireEvent.click(
        await screen.findByRole("button", { name: action }, { timeout: 10_000 }),
      );

      expect(requestModes).toEqual([mode]);
      expect(
        await screen.findByText(/No fallback run has started/i, {}, { timeout: 10_000 }),
      ).toBeVisible();
      expect(screen.queryByText(AWAITING_DEMO_COPY)).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Run replay fixture" })).toBeEnabled();
      expect(screen.getByRole("button", { name: "Run SDK QA trace" })).toBeEnabled();
      expectNoSnapshotLifecycle("Not started");
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
    expect(window.sessionStorage.getItem(HOTEL_RECOVERY_KEY)).toBeNull();
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
