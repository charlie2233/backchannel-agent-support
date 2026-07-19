import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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

function healthResponse(): Response {
  return jsonResponse({
    backend: "stub",
    liveReady: false,
    providerBoundary: "demo_adapter_only",
  });
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

function declinedReceipt(status: "closed_without_action" | "outcome_unknown") {
  const uncertain = status === "outcome_unknown";
  return {
    recoveryId,
    executionMode: "sdk_stub",
    status,
    simulated: true,
    providerExecution: uncertain,
    modelIds: [],
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
  it.each([404, 422])(
    "replaces a stored recovery after definitive status %s exactly once and clears its matching claim",
    async (status) => {
      const replacementRecoveryId = "99999999-8888-4777-8666-555555555555";
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
          return Promise.resolve(jsonResponse({ detail: "Stored recovery unavailable" }, status));
        }
        if (url === "/api/recoveries") {
          return Promise.resolve(
            jsonResponse(pendingSnapshot(undefined, replacementRecoveryId), 201),
          );
        }
        throw new Error(`Unexpected request: ${url}`);
      });
      vi.stubGlobal("fetch", fetchMock);

      render(<App />);

      await waitFor(() => {
        expect(sessionStorage.getItem("backchannel.hotelRecovery.v1")).toBe(
          replacementRecoveryId,
        );
      }, { timeout: 5_000 });
      expect(
        await screen.findByRole(
          "heading",
          { name: "Approve exact remedy" },
          { timeout: 5_000 },
        ),
      ).toBeVisible();
      await waitFor(() => {
        expect(sessionStorage.getItem("backchannel.pendingDecision.v1")).toBeNull();
      }, { timeout: 5_000 });

      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(
        fetchMock.mock.calls.filter(([input]) => String(input) === "/api/recoveries"),
      ).toHaveLength(1);
      expect(
        fetchMock.mock.calls.filter(
          ([input]) => String(input) === `/api/recoveries/${recoveryId}`,
        ),
      ).toHaveLength(1);
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

      expect(await screen.findByText("Replay fixture")).toBeVisible();
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

  it("renders exactly two approved scenarios and the ordered lifecycle", async () => {
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

    const lifecycle = screen.getByRole("list", { name: "Recovery lifecycle" });
    expect(within(lifecycle).getAllByRole("listitem")).toHaveLength(6);
    expect(within(lifecycle).getAllByRole("listitem").map((item) => item.textContent)).toEqual([
      expect.stringContaining("Detect"),
      expect.stringContaining("Prove"),
      expect.stringContaining("Negotiate"),
      expect.stringContaining("Authorize"),
      expect.stringContaining("Execute"),
      expect.stringContaining("Verify & seal"),
    ]);

    expect(await screen.findByText("Replay fixture")).toBeInTheDocument();
    expect(screen.queryByText(/GPT-5\.6 agents/i)).not.toBeInTheDocument();
  });

  it("marks every lifecycle step recorded for a completed scenario", () => {
    stubHealthWithUnavailableRecovery();
    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: /API quota recovery/i }));

    const lifecycle = screen.getByRole("list", { name: "Recovery lifecycle" });
    expect(within(lifecycle).getAllByText("Recorded")).toHaveLength(6);
  });
});
