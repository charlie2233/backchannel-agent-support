import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { RecoveryReceipt, RecoverySnapshot } from "../domain/recovery";
import { recoveryScenarios } from "../fixtures/recoveries";
import { EvidenceInspector } from "./EvidenceInspector";

const recoveryId = "99999999-2222-4333-8444-555555555555";

afterEach(cleanup);

function quotaReceipt(mode: "sdk_stub" | "replay_fixture"): RecoveryReceipt {
  const runtime = mode === "sdk_stub";
  return {
    recoveryId,
    executionMode: mode,
    status: "completed",
    simulated: true,
    providerExecution: runtime,
    modelIds: [],
    rootTraceId: null,
    sdkVersion: runtime ? "0.18.3" : null,
    protocolVersion: runtime ? "backchannel.quota.v1" : null,
    agentGraphVersion: runtime ? "backchannel.quota-agent.v1" : null,
    promptToolSchemaHash: runtime ? "c".repeat(64) : null,
    boundary: "Exact quota boundary.",
    providerResult: "Exact quota result.",
    authorizationSource: "Exact delegated quota authority.",
    verificationResults: ["Exact quota verification."],
    decision: null,
    decisionRemedyDigest: null,
    executionCount: runtime ? 1 : 0,
    providerDispatchStarted: runtime,
    exactInterruptionRejected: false,
    permissionRevoked: runtime,
    scopeClosed: runtime,
    approvedRemedyDigest: null,
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
      source: runtime ? "sdk_simulator" : "recorded_fixture",
      revocationEvidenceKind: runtime
        ? "runtime_permission_revoked"
        : "recorded_revocation_only",
      protocolSteps: [
        "Detect",
        "Prove",
        "Negotiate",
        "Authorize",
        "Execute",
        "Verify & seal",
      ],
    },
  };
}

function quotaSnapshot(mode: "sdk_stub" | "replay_fixture"): RecoverySnapshot {
  return {
    recoveryId,
    scenarioId: "api-quota",
    executionMode: mode,
    status: "completed",
    currentStep: 5,
    currentStepSummary: "Quota receipt sealed.",
    createdAt: "2026-07-19T10:00:00Z",
    updatedAt: "2026-07-19T10:00:01Z",
    pendingApproval: null,
    rootTraceId: null,
    modelIds: [],
    sdkVersion: mode === "sdk_stub" ? "0.18.3" : null,
    protocolVersion: mode === "sdk_stub" ? "backchannel.quota.v1" : null,
    agentGraphVersion: mode === "sdk_stub" ? "backchannel.quota-agent.v1" : null,
    promptToolSchemaHash: mode === "sdk_stub" ? "c".repeat(64) : null,
  };
}

describe("API quota evidence inspector", () => {
  it.each([
    ["sdk_stub", "Verified quota recovery", "Runtime permission revoked"],
    ["replay_fixture", "Recorded quota recovery", "Recorded revocation evidence only"],
  ] as const)("renders canonical %s evidence without consent", (mode, heading, revocation) => {
    render(
      <EvidenceInspector
        scenario={recoveryScenarios[1]}
        snapshot={quotaSnapshot(mode)}
        receipt={quotaReceipt(mode)}
      />,
    );

    const inspector = screen.getByRole("complementary", { name: heading });
    expect(within(inspector).getByText("1000 rpm")).toBeVisible();
    expect(within(inspector).getByText("1200 rpm")).toBeVisible();
    expect(within(inspector).getByText("1500 rpm")).toBeVisible();
    expect(within(inspector).getByText("US")).toBeVisible();
    expect(within(inspector).getByText("900 seconds")).toBeVisible();
    expect(within(inspector).getByText("250 USD minor units")).toBeVisible();
    expect(within(inspector).getByText("500 USD minor units")).toBeVisible();
    expect(within(inspector).getByText("0 human interruptions")).toBeVisible();
    expect(within(inspector).getByText("0 approvals")).toBeVisible();
    expect(within(inspector).getByText(revocation)).toBeVisible();
    expect(
      within(inspector).getByText(
        mode === "sdk_stub" ? "sdk_simulator" : "recorded_fixture",
      ),
    ).toBeVisible();
    expect(within(inspector).getAllByText("Verified")).toHaveLength(2);
    expect(
      within(inspector).getByText(
        "Region preserved; burst covers demand; duration within limit; base quota unchanged",
      ),
    ).toBeVisible();
    expect(within(inspector).queryByRole("button", { name: /approve/i })).not.toBeInTheDocument();
    expect(within(inspector).queryByRole("button", { name: /decline/i })).not.toBeInTheDocument();
  });
});
