import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const useRecoveryMock = vi.hoisted(() => vi.fn());

vi.mock("./hooks/useRecovery", () => ({
  useRecovery: useRecoveryMock,
}));

import App from "./App";

const recoveryId = "11111111-2222-4333-8444-555555555555";
const digest = `sha256:${"a".repeat(64)}`;
const retryEvents = vi.fn();

const pendingSnapshot = {
  recoveryId,
  scenarioId: "hotel",
  executionMode: "sdk_stub",
  status: "pending_approval",
  currentStep: 3,
  currentStepSummary: "Server pause loaded.",
  createdAt: "2026-07-24T20:00:00Z",
  updatedAt: "2026-07-24T20:00:01Z",
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
    providerCommitments: [
      "No additional charge",
      "Preserve booking dates",
    ],
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

const emptyRecovery = {
  snapshot: null,
  receipt: null,
  events: [],
  lastSeq: 0,
  loading: false,
  error: null,
  errorStatus: null,
  errorPhase: null,
  retryEvents: vi.fn(),
  eventsRetryAvailable: false,
  eventsRetryAfterSeconds: null,
  eventsRetrying: false,
};

let hotelRecovery = {
  ...emptyRecovery,
  snapshot: pendingSnapshot,
  error: "The event stream is currently at capacity." as string | null,
  errorStatus: 429 as number | null,
  errorPhase: "events" as "events" | null,
  retryEvents,
  eventsRetryAfterSeconds: 1 as number | null,
};

beforeEach(() => {
  sessionStorage.clear();
  sessionStorage.setItem("backchannel.hotelRecovery.v1", recoveryId);
  retryEvents.mockClear();
  hotelRecovery = {
    ...hotelRecovery,
    error: "The event stream is currently at capacity.",
    errorStatus: 429,
    errorPhase: "events",
    eventsRetryAvailable: false,
    eventsRetryAfterSeconds: 1,
    eventsRetrying: false,
  };
  useRecoveryMock.mockImplementation((activeRecoveryId: string | null) =>
    activeRecoveryId === recoveryId ? hotelRecovery : emptyRecovery,
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
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
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

describe("event-stream recovery alert", () => {
  it("keeps authoritative pending evidence visible and disables retry until the hint expires", () => {
    const view = render(<App />);

    const alert = screen.getByRole("alert", {
      name: "Event update connection",
    });
    expect(alert).toHaveTextContent(
      "The event stream is currently at capacity.",
    );
    expect(alert).toHaveTextContent("Retry is available in 1 second.");
    expect(
      screen.getByRole("heading", { name: "Approve exact remedy" }),
    ).toBeVisible();
    expect(screen.getByText("Server pause loaded.")).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Retry event updates" }),
    ).toBeDisabled();

    hotelRecovery = {
      ...hotelRecovery,
      eventsRetryAvailable: true,
      eventsRetryAfterSeconds: null,
    };
    view.rerender(<App />);

    const retry = screen.getByRole("button", {
      name: "Retry event updates",
    });
    expect(retry).toBeEnabled();
    fireEvent.click(retry);
    expect(retryEvents).toHaveBeenCalledOnce();
  });

  it("exposes one disabled retry control while an authoritative retry is in progress", () => {
    hotelRecovery = {
      ...hotelRecovery,
      eventsRetryAvailable: false,
      eventsRetryAfterSeconds: null,
      eventsRetrying: true,
    };

    render(<App />);

    expect(
      screen.getByRole("button", { name: "Retrying event updates…" }),
    ).toBeDisabled();
    expect(retryEvents).not.toHaveBeenCalled();
  });
});
