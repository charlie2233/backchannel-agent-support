import { afterEach, describe, expect, it, vi } from "vitest";

import { getReceipt } from "./client";

const recoveryId = "99999999-2222-4333-8444-555555555555";

function quotaEvidence(source: "sdk_simulator" | "recorded_fixture") {
  return {
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
    source,
    revocationEvidenceKind:
      source === "sdk_simulator"
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
  };
}

function quotaReceipt(mode: "sdk_stub" | "replay_fixture") {
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
    authorizationSource: "Exact quota authority.",
    verificationResults: ["Exact quota verification."],
    decision: null,
    decisionRemedyDigest: null,
    executionCount: runtime ? 1 : 0,
    providerDispatchStarted: runtime,
    exactInterruptionRejected: false,
    permissionRevoked: runtime,
    scopeClosed: runtime,
    approvedRemedyDigest: null,
    quotaEvidence: quotaEvidence(runtime ? "sdk_simulator" : "recorded_fixture"),
  };
}

function response(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("strict API quota receipt transport", () => {
  it.each(["sdk_stub", "replay_fixture"] as const)(
    "accepts the canonical %s receipt",
    async (mode) => {
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(quotaReceipt(mode))));

      await expect(getReceipt(recoveryId)).resolves.toMatchObject({
        executionMode: mode,
        terminalReason: null,
        quotaEvidence: quotaEvidence(
          mode === "sdk_stub" ? "sdk_simulator" : "recorded_fixture",
        ),
      });
    },
  );

  it.each([
    { providerCeilingRpm: 1001 },
    { recordedDemandRpm: 1199 },
    { temporaryBurstRpm: 1200 },
    { region: "EU" },
    { durationSeconds: 901 },
    { extraCostMinor: 251 },
    { delegatedAuthorityMaxMinor: 499 },
    { currency: "EUR" },
    {
      hardConstraints: {
        ...quotaEvidence("sdk_simulator").hardConstraints,
        regionPreserved: false,
      },
    },
    {
      hardConstraints: {
        ...quotaEvidence("sdk_simulator").hardConstraints,
        burstCoversDemand: false,
      },
    },
    {
      hardConstraints: {
        ...quotaEvidence("sdk_simulator").hardConstraints,
        durationWithinLimit: false,
      },
    },
    {
      hardConstraints: {
        ...quotaEvidence("sdk_simulator").hardConstraints,
        baseQuotaUnchanged: false,
      },
    },
    {
      hardConstraints: {
        ...quotaEvidence("sdk_simulator").hardConstraints,
        unexpected: true,
      },
    },
    { humanInterruptions: 1 },
    { approvals: 1 },
    { providerProofVerified: false },
    { grantVerified: false },
    { source: "recorded_fixture" },
    { revocationEvidenceKind: "recorded_revocation_only" },
    { protocolSteps: ["Prove", "Detect", "Negotiate", "Authorize", "Execute", "Verify & seal"] },
    { protocolSteps: ["Detect", "Prove", "Negotiate", "Authorize", "Execute"] },
    { protocolSteps: ["Detect", "Prove", "Negotiate", "Authorize", "Execute", "Execute"] },
    { unexpected: true },
  ])("rejects a mutated SDK quota fact %#", async (mutation) => {
    const canonical = quotaReceipt("sdk_stub");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        response({
          ...canonical,
          quotaEvidence: { ...canonical.quotaEvidence, ...mutation },
        }),
      ),
    );

    await expect(getReceipt(recoveryId)).rejects.toMatchObject({
      code: "unexpected_response",
      status: 200,
    });
  });

  it("rejects replay evidence that claims a runtime revocation", async () => {
    const canonical = quotaReceipt("replay_fixture");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        response({
          ...canonical,
          quotaEvidence: {
            ...canonical.quotaEvidence,
            revocationEvidenceKind: "runtime_permission_revoked",
          },
        }),
      ),
    );

    await expect(getReceipt(recoveryId)).rejects.toMatchObject({
      code: "unexpected_response",
      status: 200,
    });
  });

  it.each([
    { rootTraceId: "qa_trace_11111111111111111111111111111111" },
    { modelIds: ["impossible-model"] },
    { protocolVersion: "backchannel.approval.v1" },
    { agentGraphVersion: "backchannel.hotel-agent.v1" },
    { decision: "approved" },
    { decisionRemedyDigest: `sha256:${"a".repeat(64)}` },
    { executionCount: 0 },
    { providerDispatchStarted: false },
    { permissionRevoked: false },
    { scopeClosed: false },
    { quotaEvidence: null },
  ])("rejects an outer SDK quota contradiction %#", async (mutation) => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(response({ ...quotaReceipt("sdk_stub"), ...mutation })),
    );

    await expect(getReceipt(recoveryId)).rejects.toMatchObject({
      code: "unexpected_response",
      status: 200,
    });
  });

  it.each([
    { providerExecution: true },
    { executionCount: 1 },
    { providerDispatchStarted: true },
    { permissionRevoked: true },
    { scopeClosed: true },
  ])("rejects a replay quota runtime claim %#", async (mutation) => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        response({ ...quotaReceipt("replay_fixture"), ...mutation }),
      ),
    );

    await expect(getReceipt(recoveryId)).rejects.toMatchObject({
      code: "unexpected_response",
      status: 200,
    });
  });

  it("rejects live mode carrying quota evidence", async () => {
    const canonical = quotaReceipt("sdk_stub");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        response({
          ...canonical,
          executionMode: "openai_live",
          rootTraceId: "trace_11111111111111111111111111111111",
          modelIds: ["gpt-5.6-luna-returned"],
          decision: "approved",
          decisionRemedyDigest: `sha256:${"a".repeat(64)}`,
          approvedRemedyDigest: `sha256:${"a".repeat(64)}`,
        }),
      ),
    );

    await expect(getReceipt(recoveryId)).rejects.toMatchObject({
      code: "unexpected_response",
      status: 200,
    });
  });
});
