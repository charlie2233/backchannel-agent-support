import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { RecoveryReceipt } from "../domain/recovery";
import { Receipt } from "./Receipt";

const digest = `sha256:${"a".repeat(64)}` as `sha256:${string}`;

function receipt(overrides: Partial<RecoveryReceipt> = {}): RecoveryReceipt {
  return {
    recoveryId: "11111111-2222-4333-8444-555555555555",
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
    authorizationSource: "Approved Agents SDK commit_remedy interruption.",
    verificationResults: [
      "Demo provider dispatch returned confirmed.",
      "Temporary permission revoked after terminal completion.",
    ],
    decision: "approved",
    decisionRemedyDigest: digest,
    executionCount: 1,
    providerDispatchStarted: true,
    exactInterruptionRejected: false,
    permissionRevoked: true,
    scopeClosed: true,
    approvedRemedyDigest: digest,
    quotaEvidence: null,
    ...overrides,
  };
}

afterEach(cleanup);

describe("Receipt", () => {
  it("renders completed provenance and the immediate digest comparison from the receipt", () => {
    render(<Receipt receipt={receipt()} />);

    const region = screen.getByRole("complementary", { name: "Completed receipt" });
    expect(within(region).getByText("sdk_stub")).toBeVisible();
    expect(within(region).getByText("None — no model call")).toBeVisible();
    expect(within(region).getByText("qa_trace_11111111111111111111111111111111")).toBeVisible();
    expect(within(region).getByText("0.18.3")).toBeVisible();
    expect(within(region).getByText("backchannel.approval.v1")).toBeVisible();
    expect(within(region).getByText("backchannel.hotel-agent.v1")).toBeVisible();
    expect(within(region).getByText("b".repeat(64))).toBeVisible();
    expect(within(region).getByText("Matched approved digest")).toBeVisible();
    expect(within(region).getByText("Temporary permission revoked after terminal completion.")).toBeVisible();
  });

  it("proves a declined remedy executed zero times and never began provider dispatch", () => {
    render(
      <Receipt
        receipt={receipt({
          status: "closed_without_action",
          providerExecution: false,
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
          executionCount: 0,
          providerDispatchStarted: false,
          exactInterruptionRejected: true,
          approvedRemedyDigest: null,
        })}
      />,
    );

    const region = screen.getByRole("complementary", { name: "Closed without action" });
    expect(within(region).getByText("Provider dispatch did not begin.")).toBeVisible();
    expect(within(region).getByText("executionCount = 0")).toBeVisible();
    expect(within(region).getByText("Exact interruption rejected.")).toBeVisible();
    expect(within(region).getByText("Cancellation receipt sealed.")).toBeVisible();
  });

  it("renders the authoritative quota currency and revocation evidence kind", () => {
    render(
      <Receipt
        receipt={receipt({
          authorizationSource: "Delegated quota authority.",
          decision: null,
          decisionRemedyDigest: null,
          approvedRemedyDigest: null,
          rootTraceId: null,
          providerResult: "Temporary quota grant verified.",
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
            source: "sdk_simulator",
            revocationEvidenceKind: "runtime_permission_revoked",
            protocolSteps: [
              "Detect",
              "Prove",
              "Negotiate",
              "Authorize",
              "Execute",
              "Verify & seal",
            ],
          },
        })}
      />,
    );

    const region = screen.getByRole("complementary", { name: "Verified quota recovery" });
    expect(within(region).getByText("Currency").nextSibling).toHaveTextContent("USD");
    expect(within(region).getByText("Revocation evidence kind").nextSibling).toHaveTextContent(
      "runtime_permission_revoked",
    );
  });
});
