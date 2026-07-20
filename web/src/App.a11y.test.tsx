import axe from "axe-core";
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import { ConsentSheet } from "./components/ConsentSheet";
import { Receipt } from "./components/Receipt";
import type { RecoveryReceipt, RecoverySnapshot } from "./domain/recovery";

const digest = `sha256:${"a".repeat(64)}` as `sha256:${string}`;

function pendingSnapshot(): RecoverySnapshot {
  return {
    recoveryId: "11111111-2222-4333-8444-555555555555",
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
  };
}

function terminalReceipt(): RecoveryReceipt {
  return {
    recoveryId: "11111111-2222-4333-8444-555555555555",
    executionMode: "sdk_stub",
    status: "closed_without_action",
    simulated: true,
    providerExecution: false,
    modelIds: [],
    rootTraceId: "qa_trace_11111111111111111111111111111111",
    sdkVersion: "0.18.3",
    protocolVersion: "backchannel.approval.v1",
    agentGraphVersion: "backchannel.hotel-agent.v1",
    promptToolSchemaHash: "b".repeat(64),
    boundary: "Demo provider adapter boundary.",
    providerResult: "Provider dispatch did not begin.",
    authorizationSource: "Operator declined the exact pending remedy.",
    verificationResults: [
      "Human consent requested.",
      "Remedy declined by operator.",
      "Exact interruption rejected.",
      "No replacement action selected.",
      "Temporary permission revoked.",
      "Cancellation receipt sealed.",
    ],
    decision: "declined",
    decisionRemedyDigest: digest,
    executionCount: 0,
    providerDispatchStarted: false,
    exactInterruptionRejected: true,
    permissionRevoked: true,
    scopeClosed: true,
    approvedRemedyDigest: null,
    quotaEvidence: null,
  };
}

async function criticalViolations(root: Element | Document) {
  const result = await axe.run(root, {
    runOnly: { type: "tag", values: ["wcag2a", "wcag2aa"] },
    // jsdom has no canvas implementation, so color contrast remains a browser-only gate.
    rules: { "color-contrast": { enabled: false } },
  });
  return result.violations.filter(({ impact }) => impact === "critical");
}

afterEach(() => {
  cleanup();
  sessionStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function installMatchMedia(matches: boolean) {
  vi.stubGlobal(
    "matchMedia",
    vi.fn().mockImplementation((query: string) => ({
      matches,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  );
}

describe.each([
  ["desktop", false],
  ["mobile", true],
] as const)("App accessibility at %s width", (_label, mobile) => {
  it(
    "has no critical automated accessibility violations",
    async () => {
      installMatchMedia(mobile);
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue(
          new Response(
            JSON.stringify({
              backend: "stub",
              liveReady: false,
              sdkStubReady: false,
              providerBoundary: "demo_adapter_only",
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
        ),
      );
      render(<App />);

      expect(await criticalViolations(document.body)).toEqual([]);
    },
    15_000,
  );
});

describe("critical recovery state accessibility", () => {
  it(
    "scans the actual pending mobile portal",
    async () => {
      installMatchMedia(true);
      render(
        <>
          <main inert>
            <h1>Hotel booking recovery</h1>
          </main>
          <ConsentSheet snapshot={pendingSnapshot()} displayMode="dialog" open />
        </>,
      );

      expect(document.body.querySelector('[role="dialog"]')).not.toBeNull();
      expect(await criticalViolations(document.body)).toEqual([]);
    },
    15_000,
  );

  it(
    "scans an authoritative terminal receipt",
    async () => {
      installMatchMedia(false);
      render(<Receipt receipt={terminalReceipt()} />);

      expect(document.body.querySelector("#closed-heading")).not.toBeNull();
      expect(await criticalViolations(document.body)).toEqual([]);
    },
    15_000,
  );
});
