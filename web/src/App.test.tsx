import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";

const recoveryId = "11111111-2222-4333-8444-555555555555";
const digest = `sha256:${"a".repeat(64)}`;

class MockEventSource {
  static instances: MockEventSource[] = [];

  readonly url: string;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  close = vi.fn();

  constructor(url: string | URL) {
    this.url = String(url);
    MockEventSource.instances.push(this);
  }

  emit(body: unknown) {
    this.onmessage?.(
      new MessageEvent("message", { data: JSON.stringify(body) }),
    );
  }
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function healthResponse(liveReady = false, sdkStubReady = true): Response {
  return jsonResponse({
    backend: liveReady ? "openai" : "stub",
    liveReady,
    sdkStubReady,
    providerBoundary: "demo_adapter_only",
  });
}

function publicErrorResponse(
  code:
    | "live_unavailable"
    | "live_timeout"
    | "live_capacity_reached"
    | "live_cooldown"
    | "live_daily_budget_exceeded",
  message: string,
  status: 429 | 503 | 504,
  recoveryId: string | null = null,
  fallback: object | null = {
    kind: "show_replay_fixture",
    scenarioId: "hotel",
    executionMode: "replay_fixture",
  },
): Response {
  return jsonResponse(
    {
      error: {
        code,
        message,
        requestId: "req_11111111111111111111111111111111",
        recoveryId,
        retryAfterSeconds: status === 429 ? 1 : null,
        fallback,
      },
    },
    status,
  );
}

function creationBudgetResponse(retryAfterSeconds = 27): Response {
  return new Response(
    JSON.stringify({
      error: {
        code: "creation_daily_budget_exceeded",
        message:
          "The public demo recovery creation budget is exhausted for today.",
        requestId: "req_11111111111111111111111111111111",
        recoveryId: null,
        retryAfterSeconds,
        fallback: null,
      },
    }),
    {
      status: 429,
      headers: {
        "Content-Type": "application/json",
        "Retry-After": String(retryAfterSeconds),
      },
    },
  );
}

function pendingSnapshot(pendingApproval: object | null = {
  remedyId: "server-remedy",
  remedyDigest: digest,
  terms: {
    bookingId: "server-booking",
    action: "replace_room",
    replacement: { fromRoomType: "double", toRoomType: "king" },
    stay: { checkIn: "2026-08-14", checkOut: "2026-08-16" },
    currency: "USD",
  },
  costDeltaMinor: 0,
  changedFields: ["room_type"],
  providerCommitments: ["No additional charge", "Preserve booking dates"],
  expiry: "2026-08-01T18:45:30Z",
  hardConstraintSatisfied: true,
  delegatedAuthoritySatisfied: true,
  toolCallId: "server-call",
  executionStarted: false,
}, activeRecoveryId = recoveryId) {
  return {
    recoveryId: activeRecoveryId,
    scenarioId: "hotel",
    executionMode: "sdk_stub",
    status: "pending_approval",
    currentStep: 3,
    currentStepSummary:
      pendingApproval === null ? "Exact decline claimed; closure outcome pending." : "Server pause loaded.",
    createdAt: "2026-07-18T20:00:00Z",
    updatedAt: "2026-07-18T20:00:01Z",
    pendingApproval,
    rootTraceId: "qa_trace_11111111111111111111111111111111",
    modelIds: [],
    sdkVersion: "0.18.3",
    protocolVersion: "backchannel.approval.v1",
    agentGraphVersion: "backchannel.hotel-agent.v1",
    promptToolSchemaHash: "b".repeat(64),
  };
}

function terminalSnapshot(status: "closed_without_action" | "outcome_unknown") {
  return {
    ...pendingSnapshot(null),
    status,
    currentStep: 5,
    currentStepSummary:
      status === "closed_without_action"
        ? "Declined remedy closed without provider action."
        : "Provider outcome requires manual reconciliation.",
    updatedAt: "2026-07-18T20:00:03Z",
  };
}

function replaySnapshot(activeRecoveryId = "77777777-2222-4333-8444-555555555555") {
  return {
    recoveryId: activeRecoveryId,
    scenarioId: "hotel",
    executionMode: "replay_fixture",
    status: "completed",
    currentStep: 5,
    currentStepSummary: "Bundled deterministic replay completed.",
    createdAt: "2026-07-18T20:00:00Z",
    updatedAt: "2026-07-18T20:00:03Z",
    pendingApproval: null,
    rootTraceId: null,
    modelIds: [],
    sdkVersion: null,
    protocolVersion: null,
    agentGraphVersion: null,
    promptToolSchemaHash: null,
  };
}

function replayReceipt(activeRecoveryId = "77777777-2222-4333-8444-555555555555") {
  return {
    recoveryId: activeRecoveryId,
    executionMode: "replay_fixture",
    status: "completed",
    simulated: true,
    providerExecution: false,
    modelIds: [],
    rootTraceId: null,
    sdkVersion: null,
    protocolVersion: null,
    agentGraphVersion: null,
    promptToolSchemaHash: null,
    boundary: "Bundled replay; no model call or provider execution.",
    providerResult: "No provider dispatch.",
    authorizationSource: "Bundled deterministic fixture.",
    verificationResults: ["Fixture loaded."],
    decision: null,
    decisionRemedyDigest: null,
    executionCount: 0,
    providerDispatchStarted: false,
    exactInterruptionRejected: false,
    permissionRevoked: false,
    scopeClosed: false,
    approvedRemedyDigest: null,
    quotaEvidence: null,
  };
}

function livePendingSnapshot(activeRecoveryId = recoveryId) {
  return {
    ...pendingSnapshot(undefined, activeRecoveryId),
    executionMode: "openai_live",
    rootTraceId: "trace_11111111111111111111111111111111",
    modelIds: ["gpt-5.6-luna-returned", "gpt-5.6-terra-returned"],
    agentGraphVersion: "backchannel.hotel-live-agent.v1",
  };
}

function declinedReceipt(status: "closed_without_action" | "outcome_unknown") {
  const uncertain = status === "outcome_unknown";
  return {
    recoveryId,
    executionMode: "sdk_stub",
    status,
    simulated: true,
    providerExecution: uncertain,
    modelIds: [],
    rootTraceId: "qa_trace_11111111111111111111111111111111",
    sdkVersion: "0.18.3",
    protocolVersion: "backchannel.approval.v1",
    agentGraphVersion: "backchannel.hotel-agent.v1",
    promptToolSchemaHash: "b".repeat(64),
    boundary: "Demo provider adapter boundary.",
    providerResult: uncertain
      ? "Dispatch evidence exists; provider result could not be verified."
      : "Exact SDK interruption rejected before provider dispatch.",
    authorizationSource: "Explicit operator decline.",
    verificationResults: uncertain
      ? ["Manual reconciliation required."]
      : [
          "Human consent requested.",
          "Remedy declined by operator.",
          "Exact interruption rejected.",
          "No replacement action selected.",
          "Temporary permission revoked.",
          "Cancellation receipt sealed.",
        ],
    decision: "declined",
    decisionRemedyDigest: digest,
    executionCount: uncertain ? 1 : 0,
    providerDispatchStarted: uncertain,
    exactInterruptionRejected: true,
    permissionRevoked: true,
    scopeClosed: true,
    approvedRemedyDigest: null,
    quotaEvidence: null,
  };
}

beforeEach(() => {
  MockEventSource.instances = [];
  vi.stubGlobal("EventSource", MockEventSource);
  sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  sessionStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function stubHealthWithUnavailableRecovery() {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((input: string | URL | Request) => {
      const url = String(input);
      if (url === "/health") {
        return Promise.resolve(healthResponse());
      }
      if (url === "/api/recoveries") {
        return Promise.resolve(new Response(null, { status: 503 }));
      }
      throw new Error(`Unexpected request: ${url}`);
    }),
  );
}

describe("Backchannel console", () => {
  it("surfaces a bounded automatic hotel creation budget failure without replay or evidence", async () => {
    vi.useFakeTimers();
    let resolveCreation: ((response: Response) => void) | undefined;
    const creation = new Promise<Response>((resolve) => {
      resolveCreation = resolve;
    });
    const createBodies: Array<Record<string, unknown>> = [];
    const fetchMock = vi.fn().mockImplementation(
      (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/health") {
          return Promise.resolve(healthResponse(false, true));
        }
        if (url === "/api/recoveries") {
          createBodies.push(
            JSON.parse(String(init?.body)) as Record<string, unknown>,
          );
          return creation;
        }
        throw new Error(`Unexpected request: ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);

    try {
      render(
        <StrictMode>
          <App />
        </StrictMode>,
      );
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(createBodies).toEqual([
        { scenarioId: "hotel", executionMode: "sdk_stub" },
      ]);
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();

      await act(async () => {
        resolveCreation?.(creationBudgetResponse());
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });

      expect(screen.getByRole("alert")).toHaveTextContent(
        "The public demo recovery creation budget is exhausted for today. Try again in 27 seconds.",
      );
      expect(sessionStorage.getItem("backchannel.hotelRecovery.v1")).toBeNull();
      expect(
        screen.queryByText("Loading authoritative recovery…"),
      ).not.toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: "Run replay fixture" }),
      ).not.toBeInTheDocument();
      expect(
        screen.getByText("No authoritative recovery evidence is available."),
      ).toBeVisible();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(86_401_000);
      });
      expect(createBodies).toEqual([
        { scenarioId: "hotel", executionMode: "sdk_stub" },
      ]);
      expect(sessionStorage.getItem("backchannel.hotelRecovery.v1")).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it.each([
    {
      label: "live",
      liveReady: true,
      buttonName: "Run live recovery",
      executionMode: "openai_live",
    },
    {
      label: "replay",
      liveReady: false,
      buttonName: "Run replay fixture",
      executionMode: "replay_fixture",
    },
  ] as const)(
    "shows creation budget retry guidance for an explicit hotel $label attempt",
    async ({ liveReady, buttonName, executionMode }) => {
      const createBodies: Array<Record<string, unknown>> = [];
      const fetchMock = vi.fn().mockImplementation(
        (input: string | URL | Request, init?: RequestInit) => {
          const url = String(input);
          if (url === "/health") {
            return Promise.resolve(healthResponse(liveReady, true));
          }
          if (url === "/api/recoveries") {
            const body = JSON.parse(String(init?.body)) as Record<
              string,
              unknown
            >;
            createBodies.push(body);
            return Promise.resolve(
              body.executionMode === "sdk_stub"
                ? jsonResponse(
                    terminalSnapshot("closed_without_action"),
                    201,
                  )
                : creationBudgetResponse(31),
            );
          }
          if (url === `/api/recoveries/${recoveryId}/receipt`) {
            return Promise.resolve(
              jsonResponse(declinedReceipt("closed_without_action")),
            );
          }
          throw new Error(`Unexpected request: ${url}`);
        },
      );
      vi.stubGlobal("fetch", fetchMock);

      render(<App />);
      const button = await screen.findByRole("button", { name: buttonName });
      await waitFor(() => expect(button).toBeEnabled());
      fireEvent.click(button);

      expect(await screen.findByRole("alert")).toHaveTextContent(
        "The public demo recovery creation budget is exhausted for today. Try again in 31 seconds.",
      );
      expect(
        createBodies.filter(
          (body) => body.executionMode === executionMode,
        ),
      ).toHaveLength(1);
      expect(
        screen.queryByRole("button", { name: "Run replay fixture" }),
      ).not.toBeInTheDocument();
      expect(sessionStorage.getItem("backchannel.hotelRecovery.v1")).toBe(
        recoveryId,
      );
    },
  );

  it.each([404, 422])(
    "does not replace a stored recovery or matching claim after status %s",
    async (status) => {
      const storedRequest = {
        decision: "decline",
        clientDecisionId: `decision-stale-${status}`,
        remedyId: "server-remedy",
        remedyDigest: digest,
        toolCallId: "server-call",
      };
      sessionStorage.setItem("backchannel.hotelRecovery.v1", recoveryId);
      sessionStorage.setItem(
        "backchannel.pendingDecision.v1",
        JSON.stringify({ recoveryId, request: storedRequest }),
      );
      const fetchMock = vi.fn().mockImplementation((input: string | URL | Request) => {
        const url = String(input);
        if (url === "/health") {
          return Promise.resolve(healthResponse());
        }
        if (url === `/api/recoveries/${recoveryId}`) {
          return Promise.resolve(
            jsonResponse(
              {
                error: {
                  code: status === 404 ? "not_found" : "invalid_request",
                  message:
                    status === 404
                      ? "The requested resource was not found."
                      : "The request is invalid.",
                  requestId: "req_11111111111111111111111111111111",
                  recoveryId,
                  retryAfterSeconds: null,
                  fallback: null,
                },
              },
              status,
            ),
          );
        }
        if (url === "/api/recoveries") {
          throw new Error("Stored durable identity must not be replaced");
        }
        throw new Error(`Unexpected request: ${url}`);
      });
      vi.stubGlobal("fetch", fetchMock);

      render(<App />);

      await waitFor(() => {
        expect(
          fetchMock.mock.calls.filter(
            ([input]) => String(input) === `/api/recoveries/${recoveryId}`,
          ),
        ).toHaveLength(1);
      }, { timeout: 5_000 });

      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(
        fetchMock.mock.calls.filter(([input]) => String(input) === "/api/recoveries"),
      ).toHaveLength(0);
      expect(sessionStorage.getItem("backchannel.hotelRecovery.v1")).toBe(recoveryId);
      expect(sessionStorage.getItem("backchannel.pendingDecision.v1")).toBe(
        JSON.stringify({ recoveryId, request: storedRequest }),
      );
    },
    15_000,
  );

  it.each([
    { label: "network", failure: new TypeError("Network unavailable") },
    { label: "server", failure: 503 },
  ])(
    "retains the stored recovery and pending claim after a transient $label failure",
    async ({ failure }) => {
      const storedRequest = {
        decision: "decline",
        clientDecisionId: "decision-transient-retained",
        remedyId: "server-remedy",
        remedyDigest: digest,
        toolCallId: "server-call",
      };
      const storedClaim = JSON.stringify({ recoveryId, request: storedRequest });
      sessionStorage.setItem("backchannel.hotelRecovery.v1", recoveryId);
      sessionStorage.setItem("backchannel.pendingDecision.v1", storedClaim);
      const fetchMock = vi.fn().mockImplementation((input: string | URL | Request) => {
        const url = String(input);
        if (url === "/health") {
          return Promise.resolve(healthResponse());
        }
        if (url === `/api/recoveries/${recoveryId}`) {
          return failure instanceof Error
            ? Promise.reject(failure)
            : Promise.resolve(jsonResponse({ detail: "Unavailable" }, failure));
        }
        if (url === "/api/recoveries") {
          throw new Error("Transient failure must not create a replacement recovery");
        }
        throw new Error(`Unexpected request: ${url}`);
      });
      vi.stubGlobal("fetch", fetchMock);

      render(<App />);

      expect(await screen.findByText("Runtime pending")).toBeVisible();
      expect(screen.queryByText("Replay fixture")).not.toBeInTheDocument();
      await waitFor(() => {
        expect(
          fetchMock.mock.calls.filter(
            ([input]) => String(input) === `/api/recoveries/${recoveryId}`,
          ),
        ).toHaveLength(1);
      });
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });

      expect(sessionStorage.getItem("backchannel.hotelRecovery.v1")).toBe(recoveryId);
      expect(sessionStorage.getItem("backchannel.pendingDecision.v1")).toBe(storedClaim);
      expect(
        fetchMock.mock.calls.filter(([input]) => String(input) === "/api/recoveries"),
      ).toHaveLength(0);
    },
  );

  it(
    "loads the interactive hotel consent from a real server-shaped SDK snapshot",
    async () => {
      const fetchMock = vi.fn().mockImplementation(
        (input: string | URL | Request) => {
          const url = String(input);
          if (url === "/health") {
            return Promise.resolve(healthResponse());
          }
          if (url === "/api/recoveries") {
            return Promise.resolve(jsonResponse(pendingSnapshot(), 201));
          }
          throw new Error(`Unexpected request: ${url}`);
        },
      );
      vi.stubGlobal("fetch", fetchMock);

      render(<App />);

      await waitFor(() => {
        expect(
          fetchMock.mock.calls.filter(([input]) => String(input) === "/api/recoveries"),
        ).toHaveLength(1);
      });
      await waitFor(() => {
        expect(sessionStorage.getItem("backchannel.hotelRecovery.v1")).toBe(recoveryId);
      });
      expect(
        await screen.findByRole("heading", { name: "Approve exact remedy" }),
      ).toBeVisible();
      expect(await screen.findByText("SDK stub")).toBeVisible();
      expect(screen.getByText("server-booking")).toBeVisible();
      expect(screen.getByRole("button", { name: "Decline" })).toBeVisible();
      expect(screen.queryByText("hotel-consent-v1")).not.toBeInTheDocument();
    },
    10_000,
  );

  it("renders a decline receipt only after terminal SSE and authoritative refetch", async () => {
    const fetchMock = vi.fn().mockImplementation(
      (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/health") {
          return Promise.resolve(healthResponse());
        }
        if (url === "/api/recoveries") {
          return Promise.resolve(jsonResponse(pendingSnapshot(), 201));
        }
        if (url.endsWith("/decisions")) {
          const request = JSON.parse(String(init?.body)) as Record<string, unknown>;
          return Promise.resolve(
            jsonResponse({
              clientDecisionId: request.clientDecisionId,
              recoveryId,
              decision: "decline",
              status: "closed_without_action",
              decisionRemedyDigest: digest,
              executionStarted: false,
            }),
          );
        }
        if (url === `/api/recoveries/${recoveryId}`) {
          return Promise.resolve(jsonResponse(terminalSnapshot("closed_without_action")));
        }
        if (url === `/api/recoveries/${recoveryId}/receipt`) {
          return Promise.resolve(jsonResponse(declinedReceipt("closed_without_action")));
        }
        throw new Error(`Unexpected request: ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Decline" }, { timeout: 5_000 }),
    );

    expect(
      await screen.findByText(
        /Decline accepted.*terminal evidence/i,
        undefined,
        { timeout: 5_000 },
      ),
    ).toBeVisible();
    expect(screen.queryByRole("heading", { name: "Closed without action" })).not.toBeInTheDocument();
    await waitFor(() => expect(MockEventSource.instances).toHaveLength(1), {
      timeout: 5_000,
    });
    MockEventSource.instances[0]?.emit({
      recoveryId,
      seq: 8,
      type: "recovery.closed_without_action",
      terminal: true,
      data: { status: "closed_without_action" },
      createdAt: "2026-07-18T20:00:03Z",
    });

    expect(
      await screen.findByRole(
        "heading",
        { name: "Closed without action" },
        { timeout: 5_000 },
      ),
    ).toBeVisible();
    expect(screen.getByText("Provider dispatch did not begin.")).toBeVisible();
    expect(screen.getByText("Human consent requested.")).toBeVisible();
    expect(screen.getByText("Remedy declined by operator.")).toBeVisible();
    expect(screen.getByText("Exact interruption rejected.")).toBeVisible();
    expect(screen.getByText("No replacement action selected.")).toBeVisible();
    expect(screen.getByText("Temporary permission revoked.")).toBeVisible();
    expect(screen.getByText("Cancellation receipt sealed.")).toBeVisible();
    expect(screen.getByText("executionCount = 0")).toBeVisible();
    expect(screen.getByText(digest)).toBeVisible();
    await waitFor(() => {
      expect(sessionStorage.getItem("backchannel.pendingDecision.v1")).toBeNull();
    }, { timeout: 5_000 });
    expect(sessionStorage.getItem("backchannel.hotelRecovery.v1")).toBe(recoveryId);
  }, 20_000);

  it("retries the same hidden durable decision claim after reload without creating an orphan", async () => {
    const storedRequest = {
      decision: "decline",
      clientDecisionId: "decision-decline-reload-stable",
      remedyId: "server-remedy",
      remedyDigest: digest,
      toolCallId: "server-call",
    };
    sessionStorage.setItem("backchannel.hotelRecovery.v1", recoveryId);
    sessionStorage.setItem(
      "backchannel.pendingDecision.v1",
      JSON.stringify({ recoveryId, request: storedRequest }),
    );
    const fetchMock = vi.fn().mockImplementation(
      (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/health") {
          return Promise.resolve(healthResponse());
        }
        if (url === `/api/recoveries/${recoveryId}`) {
          return Promise.resolve(jsonResponse(pendingSnapshot(null)));
        }
        if (url.endsWith("/decisions")) {
          expect(JSON.parse(String(init?.body))).toEqual(storedRequest);
          return Promise.resolve(
            jsonResponse({
              clientDecisionId: storedRequest.clientDecisionId,
              recoveryId,
              decision: "decline",
              status: "closed_without_action",
              decisionRemedyDigest: digest,
              executionStarted: false,
            }),
          );
        }
        throw new Error(`Unexpected request: ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);

    const first = render(<App />);
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.filter(([input]) => String(input).endsWith("/decisions")),
      ).toHaveLength(1);
    });
    first.unmount();
    render(<App />);
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.filter(([input]) => String(input).endsWith("/decisions")),
      ).toHaveLength(2);
    });

    const creates = fetchMock.mock.calls.filter(
      ([input]) => String(input) === "/api/recoveries",
    );
    const decisionBodies = fetchMock.mock.calls
      .filter(([input]) => String(input).endsWith("/decisions"))
      .map(([, init]) => JSON.parse(String((init as RequestInit).body)));
    expect(creates).toHaveLength(0);
    expect(decisionBodies).toEqual([storedRequest, storedRequest]);
    expect(sessionStorage.getItem("backchannel.pendingDecision.v1")).not.toBeNull();
  });

  it.each([
    {
      action: "approve" as const,
      submittingLabel: "Approving…",
      originalLabel: "Approve remedy",
      alternateLabel: "Decline",
    },
    {
      action: "decline" as const,
      submittingLabel: "Declining…",
      originalLabel: "Decline",
      alternateLabel: "Approve remedy",
    },
  ])(
    "locks both actions while automatically retrying a stored $action with the same ID",
    async ({ action, submittingLabel, originalLabel, alternateLabel }) => {
      const storedRequest = {
        decision: action,
        clientDecisionId: `decision-${action}-reload-visible`,
        remedyId: "server-remedy",
        remedyDigest: digest,
        toolCallId: "server-call",
      };
      sessionStorage.setItem("backchannel.hotelRecovery.v1", recoveryId);
      sessionStorage.setItem(
        "backchannel.pendingDecision.v1",
        JSON.stringify({ recoveryId, request: storedRequest }),
      );
      let rejectAutomaticRetry: (reason?: unknown) => void = () => undefined;
      const unresolvedAutomaticRetry = new Promise<Response>((_resolve, reject) => {
        rejectAutomaticRetry = reject;
      });
      let decisionCalls = 0;
      const fetchMock = vi.fn().mockImplementation(
        (input: string | URL | Request, init?: RequestInit) => {
          const url = String(input);
          if (url === "/health") {
            return Promise.resolve(healthResponse());
          }
          if (url === `/api/recoveries/${recoveryId}`) {
            return Promise.resolve(jsonResponse(pendingSnapshot()));
          }
          if (url.endsWith("/decisions")) {
            expect(JSON.parse(String(init?.body))).toEqual(storedRequest);
            decisionCalls += 1;
            if (decisionCalls === 1) {
              return unresolvedAutomaticRetry;
            }
            return Promise.resolve(
              jsonResponse(
                action === "approve"
                  ? {
                      clientDecisionId: storedRequest.clientDecisionId,
                      recoveryId,
                      decision: "approve",
                      status: "completed",
                      approvedRemedyDigest: digest,
                      executionStarted: true,
                    }
                  : {
                      clientDecisionId: storedRequest.clientDecisionId,
                      recoveryId,
                      decision: "decline",
                      status: "closed_without_action",
                      decisionRemedyDigest: digest,
                      executionStarted: false,
                    },
              ),
            );
          }
          throw new Error(`Unexpected request: ${url}`);
        },
      );
      vi.stubGlobal("fetch", fetchMock);

      render(<App />);

      const submittingButton = await screen.findByRole("button", {
        name: submittingLabel,
      });
      const alternateButton = screen.getByRole("button", { name: alternateLabel });
      expect(submittingButton).toBeDisabled();
      expect(alternateButton).toBeDisabled();
      await waitFor(() => {
        expect(
          fetchMock.mock.calls.filter(([input]) => String(input).endsWith("/decisions")),
        ).toHaveLength(1);
      }, { timeout: 5_000 });
      fireEvent.click(submittingButton);
      fireEvent.click(alternateButton);
      expect(
        fetchMock.mock.calls.filter(([input]) => String(input).endsWith("/decisions")),
      ).toHaveLength(1);

      await act(async () => {
        rejectAutomaticRetry(new Error("Transient automatic retry failure"));
        await Promise.resolve();
      });

      const retryButton = await screen.findByRole("button", { name: originalLabel });
      await waitFor(() => expect(retryButton).toBeEnabled());
      expect(screen.getByRole("button", { name: alternateLabel })).toBeDisabled();
      fireEvent.click(retryButton);

      await waitFor(() => {
        expect(
          fetchMock.mock.calls.filter(([input]) => String(input).endsWith("/decisions")),
        ).toHaveLength(2);
      });
      const bodies = fetchMock.mock.calls
        .filter(([input]) => String(input).endsWith("/decisions"))
        .map(([, init]) => JSON.parse(String((init as RequestInit).body)));
      expect(bodies).toEqual([storedRequest, storedRequest]);
      expect(sessionStorage.getItem("backchannel.pendingDecision.v1")).toBe(
        JSON.stringify({ recoveryId, request: storedRequest }),
      );
    },
    15_000,
  );

  it("renders outcome unknown truthfully from the authoritative receipt on reload", async () => {
    sessionStorage.setItem("backchannel.hotelRecovery.v1", recoveryId);
    const fetchMock = vi.fn().mockImplementation((input: string | URL | Request) => {
      const url = String(input);
      if (url === "/health") {
        return Promise.resolve(healthResponse());
      }
      if (url === `/api/recoveries/${recoveryId}`) {
        return Promise.resolve(jsonResponse(terminalSnapshot("outcome_unknown")));
      }
      if (url === `/api/recoveries/${recoveryId}/receipt`) {
        return Promise.resolve(jsonResponse(declinedReceipt("outcome_unknown")));
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Outcome unknown" })).toBeVisible();
    expect(
      screen.getByText("Dispatch evidence exists; provider result could not be verified."),
    ).toBeVisible();
    expect(screen.getByText("Manual reconciliation required.")).toBeVisible();
    expect(screen.queryByText("Provider dispatch did not begin.")).not.toBeInTheDocument();
    expect(
      fetchMock.mock.calls.filter(([input]) => String(input) === "/api/recoveries"),
    ).toHaveLength(0);
  });

  it("keeps persisted mode claims neutral until authoritative recovery evidence arrives", async () => {
    sessionStorage.setItem("backchannel.hotelRecovery.v1", recoveryId);
    sessionStorage.setItem("backchannel.hotelRecoveryMode.v1", "openai_live");
    let resolveRecovery: ((response: Response) => void) | undefined;
    const fetchMock = vi.fn().mockImplementation((input: string | URL | Request) => {
      const url = String(input);
      if (url === "/health") {
        return Promise.resolve(healthResponse(true));
      }
      if (url === `/api/recoveries/${recoveryId}`) {
        return new Promise<Response>((resolve) => {
          resolveRecovery = resolve;
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    expect(screen.getByText("Runtime pending")).toBeVisible();
    expect(screen.getByText("Loading authoritative recovery…")).toBeVisible();
    expect(screen.getByText("Checking runtime")).toBeVisible();
    expect(screen.queryByText("OpenAI live workspace")).not.toBeInTheDocument();
    expect(screen.queryByText("OpenAI live")).not.toBeInTheDocument();
    expect(screen.queryByText("Replay workspace")).not.toBeInTheDocument();
    expect(screen.queryByText("Bundled fixture snapshot")).not.toBeInTheDocument();
    expect(screen.queryByText("replay_fixture")).not.toBeInTheDocument();

    await act(async () => {
      resolveRecovery?.(jsonResponse(livePendingSnapshot()));
      await Promise.resolve();
    });
    expect(await screen.findByText("OpenAI live")).toBeVisible();
    expect(screen.getByText("OpenAI live workspace")).toBeVisible();
    expect(screen.queryByText("Replay workspace")).not.toBeInTheDocument();
    expect(
      fetchMock.mock.calls.filter(([input]) => String(input) === "/api/recoveries"),
    ).toHaveLength(0);
  });

  it("does not abandon a nonterminal recovery when another mode is requested", async () => {
    const createBodies: Array<Record<string, unknown>> = [];
    const fetchMock = vi.fn().mockImplementation(
      (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/health") {
          return Promise.resolve(healthResponse(true));
        }
        if (url === "/api/recoveries") {
          const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
          createBodies.push(body);
          return Promise.resolve(jsonResponse(pendingSnapshot(), 201));
        }
        throw new Error(`Unexpected request: ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    const live = await screen.findByRole("button", { name: "Run live recovery" });
    expect(live).toBeDisabled();
    expect(
      screen.getByText("Finish or decline the active recovery before switching modes."),
    ).toBeVisible();
    fireEvent.click(live);
    expect(
      createBodies.filter(({ executionMode }) => executionMode === "openai_live"),
    ).toHaveLength(0);
  });

  it("does not switch away from a persisted recovery after a transient load failure", async () => {
    sessionStorage.setItem("backchannel.hotelRecovery.v1", recoveryId);
    sessionStorage.setItem("backchannel.hotelRecoveryMode.v1", "sdk_stub");
    const fetchMock = vi.fn().mockImplementation((input: string | URL | Request) => {
      const url = String(input);
      if (url === "/health") {
        return Promise.resolve(healthResponse(true));
      }
      if (url === `/api/recoveries/${recoveryId}`) {
        return Promise.resolve(
          jsonResponse(
            {
              error: {
                code: "internal_error",
                message: "The request could not be completed.",
                requestId: "req_11111111111111111111111111111111",
                recoveryId,
                retryAfterSeconds: null,
                fallback: null,
              },
            },
            500,
          ),
        );
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    expect(
      await screen.findByText("The request could not be completed."),
    ).toBeVisible();
    const live = screen.getByRole("button", { name: "Run live recovery" });
    expect(live).toBeDisabled();
    fireEvent.click(live);
    expect(
      fetchMock.mock.calls.filter(([input]) => String(input) === "/api/recoveries"),
    ).toHaveLength(0);
  });

  it("issues only one live start for rapid repeated clicks", async () => {
    let resolveLive: ((response: Response) => void) | undefined;
    const createBodies: Array<Record<string, unknown>> = [];
    const fetchMock = vi.fn().mockImplementation(
      (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/health") {
          return Promise.resolve(healthResponse(true));
        }
        if (url === "/api/recoveries") {
          const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
          createBodies.push(body);
          if (body.executionMode === "sdk_stub") {
            return Promise.resolve(
              jsonResponse(terminalSnapshot("closed_without_action"), 201),
            );
          }
          return new Promise<Response>((resolve) => {
            resolveLive = resolve;
          });
        }
        if (url === `/api/recoveries/${recoveryId}/receipt`) {
          return Promise.resolve(
            jsonResponse(declinedReceipt("closed_without_action")),
          );
        }
        throw new Error(`Unexpected request: ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);
    const live = await screen.findByRole("button", { name: "Run live recovery" });
    await waitFor(() => expect(live).toBeEnabled());
    fireEvent.click(live);
    fireEvent.click(live);

    await waitFor(() => {
      expect(
        createBodies.filter(({ executionMode }) => executionMode === "openai_live"),
      ).toHaveLength(1);
    });
    await act(async () => {
      resolveLive?.(jsonResponse(livePendingSnapshot(), 201));
      await Promise.resolve();
    });
  });

  it("renders exactly two approved scenarios without inventing a quota run", async () => {
    stubHealthWithUnavailableRecovery();

    render(<App />);

    const scenarioList = screen.getByRole("list", { name: "Recovery scenarios" });
    expect(within(scenarioList).getAllByRole("listitem")).toHaveLength(2);
    expect(
      within(scenarioList).getByRole("button", { name: /Hotel booking recovery/i }),
    ).toBeInTheDocument();
    expect(
      within(scenarioList).getByRole("button", { name: /API quota recovery/i }),
    ).toBeInTheDocument();

    fireEvent.click(
      within(scenarioList).getByRole("button", { name: /API quota recovery/i }),
    );

    expect(
      screen.queryByRole("list", { name: "Recovery lifecycle" }),
    ).not.toBeInTheDocument();
    expect(screen.queryAllByText("Recorded")).toHaveLength(0);
    expect(
      screen.getByText("No authoritative recovery evidence is available."),
    ).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Replay recorded trace" }),
    ).toBeVisible();
    expect(screen.queryByText(/GPT-5\.6 agents/i)).not.toBeInTheDocument();
  });

  it("does not mark quota lifecycle steps recorded before an explicit run", () => {
    stubHealthWithUnavailableRecovery();
    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: /API quota recovery/i }));

    expect(
      screen.queryByRole("list", { name: "Recovery lifecycle" }),
    ).not.toBeInTheDocument();
    expect(screen.queryAllByText("Recorded")).toHaveLength(0);
    expect(
      screen.getByText("No authoritative recovery evidence is available."),
    ).toBeVisible();
  });

  it.each([
    ["live_unavailable", "Live mode is unavailable on this server.", 503],
    [
      "live_timeout",
      "Live processing did not finish before the server deadline.",
      504,
    ],
    ["live_capacity_reached", "The live demo is currently at capacity.", 429],
    ["live_cooldown", "Live mode is cooling down for this demo identity.", 429],
    [
      "live_daily_budget_exceeded",
      "The live demo budget is exhausted for today.",
      429,
    ],
  ] as const)(
    "requires an explicit replay click after live start code %s",
    async (code, message, status) => {
      const replayRecoveryId = "77777777-2222-4333-8444-555555555555";
      const createBodies: Array<Record<string, unknown>> = [];
      const fetchMock = vi.fn().mockImplementation(
        (input: string | URL | Request, init?: RequestInit) => {
          const url = String(input);
          if (url === "/health") {
            return Promise.resolve(healthResponse(true));
          }
          if (url === "/api/recoveries") {
            const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
            createBodies.push(body);
            if (body.executionMode === "sdk_stub") {
              return Promise.resolve(
                jsonResponse(terminalSnapshot("closed_without_action"), 201),
              );
            }
            if (body.executionMode === "openai_live") {
              return Promise.resolve(
                publicErrorResponse(code, message, status, null),
              );
            }
            if (body.executionMode === "replay_fixture") {
              return Promise.resolve(
                jsonResponse(replaySnapshot(replayRecoveryId), 201),
              );
            }
          }
          if (url === `/api/recoveries/${recoveryId}/receipt`) {
            return Promise.resolve(
              jsonResponse(declinedReceipt("closed_without_action")),
            );
          }
          if (url === `/api/recoveries/${replayRecoveryId}/receipt`) {
            return Promise.resolve(jsonResponse(replayReceipt(replayRecoveryId)));
          }
          throw new Error(`Unexpected request: ${url}`);
        },
      );
      vi.stubGlobal("fetch", fetchMock);

      render(<App />);
      const live = await screen.findByRole("button", {
        name: "Run live recovery",
      });
      await waitFor(() => expect(live).toBeEnabled());
      fireEvent.click(live);

      expect(await screen.findByRole("alert")).toHaveTextContent(message);
      expect(
        createBodies.filter(({ executionMode }) => executionMode === "replay_fixture"),
      ).toHaveLength(0);
      const fallback = screen.getByRole("button", { name: "Run replay fixture" });
      fireEvent.click(fallback);
      fireEvent.click(fallback);

      await waitFor(() => {
        expect(
          createBodies.filter(({ executionMode }) => executionMode === "replay_fixture"),
        ).toHaveLength(1);
      });
      expect(
        createBodies.filter(({ executionMode }) => executionMode === "openai_live"),
      ).toHaveLength(1);
      expect(
        await screen.findByRole("heading", { name: "Completed replay receipt" }),
      ).toBeVisible();
      expect(screen.getByText("Simulated replay only.")).toBeVisible();
      expect(screen.getByText("No model call or provider dispatch occurred.")).toBeVisible();
      const scenarioList = screen.getByRole("list", { name: "Recovery scenarios" });
      expect(scenarioList).toHaveTextContent("completed recorded hotel replay");
      expect(scenarioList).not.toHaveTextContent("paused at operator authorization");
      expect(screen.queryByText("Not started.")).not.toBeInTheDocument();
      expect(
        screen.queryByText("Waiting for an execution outcome."),
      ).not.toBeInTheDocument();
      expect(screen.queryByText("Authorization not submitted")).not.toBeInTheDocument();
      fireEvent.click(
        within(scenarioList).getByRole("button", { name: /API quota recovery/i }),
      );
      expect(scenarioList).toHaveTextContent("completed recorded hotel replay");
      expect(scenarioList).not.toHaveTextContent("paused at operator authorization");
    },
  );

  it("offers replay when health says live is unavailable without running it automatically", async () => {
    const createBodies: Array<Record<string, unknown>> = [];
    const fetchMock = vi.fn().mockImplementation(
      (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/health") {
          return Promise.resolve(healthResponse(false));
        }
        if (url === "/api/recoveries") {
          const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
          createBodies.push(body);
          return Promise.resolve(
            body.executionMode === "sdk_stub"
              ? jsonResponse(terminalSnapshot("closed_without_action"), 201)
              : jsonResponse(replaySnapshot(), 201),
          );
        }
        if (url === `/api/recoveries/${recoveryId}/receipt`) {
          return Promise.resolve(
            jsonResponse(declinedReceipt("closed_without_action")),
          );
        }
        if (url.endsWith("/receipt")) {
          return Promise.resolve(jsonResponse(replayReceipt()));
        }
        throw new Error(`Unexpected request: ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    expect(
      await screen.findByText("Live mode is unavailable on this server."),
    ).toBeVisible();
    expect(
      createBodies.filter(({ executionMode }) => executionMode === "replay_fixture"),
    ).toHaveLength(0);
    const replay = screen.getByRole("button", { name: "Run replay fixture" });
    await waitFor(() => expect(replay).toBeEnabled());
    fireEvent.click(replay);
    await waitFor(() => {
      expect(
        createBodies.filter(({ executionMode }) => executionMode === "replay_fixture"),
      ).toHaveLength(1);
    });
  });

  it("does not auto-start the local SDK lane when health marks it unavailable", async () => {
    const createBodies: Array<Record<string, unknown>> = [];
    const fetchMock = vi.fn().mockImplementation(
      (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/health") {
          return Promise.resolve(healthResponse(false, false));
        }
        if (url === "/api/recoveries") {
          const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
          createBodies.push(body);
          return Promise.resolve(jsonResponse(replaySnapshot(), 201));
        }
        if (url.endsWith("/receipt")) {
          return Promise.resolve(jsonResponse(replayReceipt()));
        }
        throw new Error(`Unexpected request: ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    expect(
      await screen.findByText("Live mode is unavailable on this server."),
    ).toBeVisible();
    expect(
      createBodies.filter(({ executionMode }) => executionMode === "sdk_stub"),
    ).toHaveLength(0);
    const replay = screen.getByRole("button", { name: "Run replay fixture" });
    await waitFor(() => expect(replay).toBeEnabled());
    fireEvent.click(replay);
    await waitFor(() => {
      expect(
        createBodies.filter(({ executionMode }) => executionMode === "replay_fixture"),
      ).toHaveLength(1);
    });
    expect(
      await screen.findByRole("heading", { name: "Completed replay receipt" }),
    ).toBeVisible();
  });

  it("offers an explicit replay attempt when health cannot be verified", async () => {
    const createBodies: Array<Record<string, unknown>> = [];
    const fetchMock = vi.fn().mockImplementation(
      (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/health") {
          return Promise.reject(new Error("health-response-canary"));
        }
        if (url === "/api/recoveries") {
          const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
          createBodies.push(body);
          if (body.executionMode === "sdk_stub") {
            return Promise.resolve(new Response(null, { status: 503 }));
          }
          return Promise.resolve(jsonResponse(replaySnapshot(), 201));
        }
        if (url.endsWith("/receipt")) {
          return Promise.resolve(jsonResponse(replayReceipt()));
        }
        throw new Error(`Unexpected request: ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    expect(await screen.findByText("Runtime unavailable")).toBeVisible();
    expect(
      createBodies.filter(({ executionMode }) => executionMode === "replay_fixture"),
    ).toHaveLength(0);
    const replay = screen.getByRole("button", { name: "Run replay fixture" });
    await waitFor(() => expect(replay).toBeEnabled());
    fireEvent.click(replay);
    await waitFor(() => {
      expect(
        createBodies.filter(({ executionMode }) => executionMode === "replay_fixture"),
      ).toHaveLength(1);
    });
    expect(screen.queryByText("health-response-canary")).not.toBeInTheDocument();
  });
});
