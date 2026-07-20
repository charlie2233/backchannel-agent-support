import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RecoveryReceipt, RecoverySnapshot } from "./domain/recovery";
import type { RecoveryState } from "./hooks/useRecovery";

const useRecoveryMock = vi.hoisted(() => vi.fn());

vi.mock("./hooks/useRecovery", () => ({
  useRecovery: useRecoveryMock,
}));

import App, { recoveryStatusAnnouncement } from "./App";

const recoveryId = "11111111-2222-4333-8444-555555555555";
const digest = `sha256:${"a".repeat(64)}` as `sha256:${string}`;

function pendingSnapshot(): RecoverySnapshot {
  return {
    recoveryId,
    scenarioId: "hotel",
    executionMode: "sdk_stub",
    status: "pending_approval",
    currentStep: 3,
    currentStepSummary: "Server pause loaded.",
    createdAt: "2026-07-18T20:00:00Z",
    updatedAt: "2026-07-18T20:00:01Z",
    pendingApproval: {
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
      providerCommitments: ["No additional charge"],
      expiry: "2026-08-01T18:45:30Z",
      hardConstraintSatisfied: true,
      delegatedAuthoritySatisfied: true,
      toolCallId: "server-call",
      executionStarted: false,
    },
    rootTraceId: "qa_trace_11111111111111111111111111111111",
    modelIds: [],
    sdkVersion: "0.18.3",
    protocolVersion: "backchannel.approval.v1",
    agentGraphVersion: "backchannel.hotel-agent.v1",
    promptToolSchemaHash: "b".repeat(64),
  };
}

function completedReceipt(): RecoveryReceipt {
  return {
    recoveryId,
    executionMode: "sdk_stub",
    status: "completed",
    simulated: true,
    providerExecution: true,
    modelIds: [],
    rootTraceId: "qa_trace_11111111111111111111111111111111",
    sdkVersion: "0.18.3",
    protocolVersion: "backchannel.approval.v1",
    agentGraphVersion: "backchannel.hotel-agent.v1",
    promptToolSchemaHash: "b".repeat(64),
    boundary: "Demo provider adapter boundary.",
    providerResult: "Demo provider dispatch returned confirmed.",
    authorizationSource: "Approved Agents SDK interruption.",
    verificationResults: ["Temporary permission revoked after terminal completion."],
    decision: "approved",
    decisionRemedyDigest: digest,
    executionCount: 1,
    providerDispatchStarted: true,
    exactInterruptionRejected: false,
    permissionRevoked: true,
    scopeClosed: true,
    approvedRemedyDigest: digest,
    quotaEvidence: null,
  };
}

function state(snapshot: RecoverySnapshot, receipt: RecoveryReceipt | null): RecoveryState {
  return {
    snapshot,
    receipt,
    events: [],
    lastSeq: 0,
    loading: false,
    error: null,
    errorStatus: null,
    errorPhase: null,
  };
}

const emptyState: RecoveryState = {
  snapshot: null,
  receipt: null,
  events: [],
  lastSeq: 0,
  loading: false,
  error: null,
  errorStatus: null,
  errorPhase: null,
};

function installControllableMatchMedia(initialMatches: boolean) {
  let matches = initialMatches;
  const listeners = new Set<() => void>();
  const media = {
    get matches() {
      return matches;
    },
    media: "(max-width: 759px)",
    onchange: null,
    addEventListener: (_type: string, listener: () => void) => listeners.add(listener),
    removeEventListener: (_type: string, listener: () => void) => listeners.delete(listener),
    addListener: (listener: () => void) => listeners.add(listener),
    removeListener: (listener: () => void) => listeners.delete(listener),
    dispatchEvent: () => true,
  };
  vi.stubGlobal("matchMedia", vi.fn(() => media));
  return (nextMatches: boolean) => {
    matches = nextMatches;
    act(() => listeners.forEach((listener) => listener()));
  };
}

let hotelState: RecoveryState;

beforeEach(() => {
  sessionStorage.clear();
  sessionStorage.setItem("backchannel.hotelRecovery.v1", recoveryId);
  hotelState = state(pendingSnapshot(), null);
  useRecoveryMock.mockImplementation((activeRecoveryId: string | null) =>
    activeRecoveryId === recoveryId ? hotelState : emptyState,
  );
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          backend: "stub",
          liveReady: false,
          sdkStubReady: true,
          providerBoundary: "demo_adapter_only",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    ),
  );
});

afterEach(() => {
  cleanup();
  sessionStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("consent surface focus transitions", () => {
  it(
    "focuses the persistent recovery target and announces terminal replacement",
    async () => {
      installControllableMatchMedia(true);
      const view = render(<App />);
      fireEvent.click(screen.getByRole("link", { name: "Review exact remedy" }));
      expect(screen.getByRole("button", { name: "Close" })).toHaveFocus();

      const terminalSnapshot: RecoverySnapshot = {
        ...pendingSnapshot(),
        status: "completed",
        currentStep: 5,
        currentStepSummary: "Receipt sealed.",
        pendingApproval: null,
      };
      hotelState = state(terminalSnapshot, completedReceipt());
      view.rerender(<App />);

      const recoveryTarget = screen.getByRole("heading", {
        name: "Hotel booking recovery",
      });
      await waitFor(() => expect(recoveryTarget).toHaveFocus());
      expect(
        screen.getByRole("status", { name: "Recovery status updates" }),
      ).toHaveTextContent("Recovery status: Completed.");
      expect(
        screen.getByRole("status", { name: "Recovery status updates" }),
      ).not.toHaveTextContent("dialog opened");
      expect(screen.getByRole("heading", { name: "Completed receipt" })).toBeVisible();
    },
    15_000,
  );

  it(
    "focuses the persistent recovery target across both breakpoint directions",
    async () => {
      const setMobile = installControllableMatchMedia(true);
      render(<App />);
      fireEvent.click(screen.getByRole("link", { name: "Review exact remedy" }));
      expect(screen.getByRole("button", { name: "Close" })).toHaveFocus();

      setMobile(false);
      const recoveryTarget = screen.getByRole("heading", {
        name: "Hotel booking recovery",
      });
      await waitFor(() => expect(recoveryTarget).toHaveFocus());
      expect(
        screen.getByRole("complementary", { name: "Approve exact remedy" }),
      ).toBeVisible();

      screen.getByRole("button", { name: "Approve remedy" }).focus();
      setMobile(true);
      await waitFor(() => expect(recoveryTarget).toHaveFocus());
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    },
    15_000,
  );

  it(
    "moves focus from the restored mobile trigger before desktop hides it",
    async () => {
      const setMobile = installControllableMatchMedia(true);
      render(<App />);
      const trigger = screen.getByRole("link", { name: "Review exact remedy" });
      fireEvent.click(trigger);
      fireEvent.click(screen.getByRole("button", { name: "Close" }));
      await waitFor(() => expect(trigger).toHaveFocus());

      setMobile(false);

      const recoveryTarget = screen.getByRole("heading", {
        name: "Hotel booking recovery",
      });
      await waitFor(() => expect(recoveryTarget).toHaveFocus());
      expect(
        screen.getByRole("complementary", { name: "Approve exact remedy" }),
      ).toBeVisible();
    },
    15_000,
  );

  it(
    "reactively releases the saved decision lock after terminal evidence clears it",
    async () => {
      installControllableMatchMedia(false);
      sessionStorage.setItem(
        "backchannel.pendingDecision.v1",
        JSON.stringify({
          recoveryId,
          request: {
            decision: "approve",
            clientDecisionId: "decision-approve-terminal",
            remedyId: "server-remedy",
            remedyDigest: digest,
            toolCallId: "server-call",
          },
        }),
      );
      const terminalSnapshot: RecoverySnapshot = {
        ...pendingSnapshot(),
        status: "completed",
        currentStep: 5,
        currentStepSummary: "Receipt sealed.",
        pendingApproval: null,
      };
      hotelState = state(terminalSnapshot, completedReceipt());

      render(<App />);

      const replay = await screen.findByRole(
        "button",
        { name: "Run replay fixture" },
        { timeout: 10_000 },
      );
      await waitFor(
        () => {
          expect(sessionStorage.getItem("backchannel.pendingDecision.v1")).toBeNull();
        },
        { timeout: 10_000 },
      );
      expect(replay).toBeEnabled();
      expect(
        screen.queryByText(/A saved decision must be retried/i),
      ).not.toBeInTheDocument();
      expect(screen.getByRole("heading", { name: "Completed receipt" })).toBeVisible();
    },
    15_000,
  );

  it(
    "passes real scenario, runtime, and lifecycle context into the mobile dialog",
    async () => {
      installControllableMatchMedia(true);
      render(<App />);
      await screen.findByRole(
        "region",
        { name: "Runtime provenance" },
        { timeout: 10_000 },
      );

      fireEvent.click(screen.getByRole("link", { name: "Review exact remedy" }));

      const context = screen.getByRole("region", { name: "Recovery context" });
      expect(context).toHaveTextContent("Hotel booking recovery");
      expect(context).toHaveTextContent("SDK stub");
      expect(context).toHaveTextContent("Step 4 of 6");
      expect(context).toHaveTextContent("Authorize");
      expect(context).toHaveTextContent("Server pause loaded.");
    },
    15_000,
  );
});

describe("recovery status announcements", () => {
  it("omits stale dialog transition copy when consent is no longer available", () => {
    expect(
      recoveryStatusAnnouncement(
        "Completed",
        false,
        "Approve exact remedy dialog opened.",
      ),
    ).toBe("Recovery status: Completed.");
  });
});
