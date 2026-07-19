import { afterEach, describe, expect, it, vi } from "vitest";

import {
  getReceipt,
  getRecovery,
  HttpStatusError,
  isRecoverySnapshot,
  postDecision,
} from "./client";

const recoveryId = "11111111-2222-4333-8444-555555555555";
const digest = `sha256:${"a".repeat(64)}` as `sha256:${string}`;

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function cancellationReceipt(executionCount = 0) {
  return {
    recoveryId,
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
    boundary: "Demo adapter boundary.",
    providerResult: "Exact interruption rejected before provider dispatch.",
    authorizationSource: "Explicit operator decline.",
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
    executionCount,
    providerDispatchStarted: false,
    exactInterruptionRejected: true,
    permissionRevoked: true,
    scopeClosed: true,
    approvedRemedyDigest: null,
  };
}

function liveApprovedReceipt() {
  return {
    recoveryId,
    executionMode: "openai_live",
    status: "completed",
    simulated: true,
    providerExecution: true,
    modelIds: [
      "gpt-5.6-luna-2026-07-15-returned",
      "gpt-5.6-terra-2026-07-15-returned",
    ],
    rootTraceId: "trace_11111111111111111111111111111111",
    sdkVersion: "0.18.3",
    protocolVersion: "backchannel.approval.v1",
    agentGraphVersion: "backchannel.hotel-live-agent.v1",
    promptToolSchemaHash: "c".repeat(64),
    boundary: "Live models and demo adapter only.",
    providerResult: "Demo adapter confirmed.",
    authorizationSource: "Exact approved interruption.",
    verificationResults: ["Temporary permission revoked."],
    decision: "approved",
    decisionRemedyDigest: digest,
    executionCount: 1,
    providerDispatchStarted: true,
    exactInterruptionRejected: false,
    permissionRevoked: true,
    scopeClosed: true,
    approvedRemedyDigest: digest,
  };
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("decision and receipt contracts", () => {
  it("preserves a non-success recovery HTTP status as typed client evidence", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "Not found" }), {
          status: 404,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    const failure = await getRecovery(recoveryId).catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(HttpStatusError);
    expect(failure).toMatchObject({ status: 404 });
    expect((failure as Error).message).toBe("Recovery request failed with status 404");
  });

  it("posts an explicit decline discriminator and accepts only the decline union", async () => {
    const request = {
      decision: "decline" as const,
      clientDecisionId: "decision-decline-contract",
      remedyId: "remedy-contract",
      remedyDigest: digest,
      toolCallId: "call-contract",
    };
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        clientDecisionId: request.clientDecisionId,
        recoveryId,
        decision: "decline",
        status: "closed_without_action",
        decisionRemedyDigest: digest,
        executionStarted: false,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(postDecision(recoveryId, request)).resolves.toMatchObject({
      decision: "decline",
      status: "closed_without_action",
    });
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toEqual(request);
  });

  it("rejects a response whose discriminator and status belong to different union arms", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({
          clientDecisionId: "decision-invalid",
          recoveryId,
          decision: "approve",
          status: "closed_without_action",
          decisionRemedyDigest: digest,
          executionStarted: false,
        }),
      ),
    );

    await expect(
      postDecision(recoveryId, {
        decision: "approve",
        clientDecisionId: "decision-invalid",
        remedyId: "remedy-contract",
        remedyDigest: digest,
        toolCallId: "call-contract",
      }),
    ).rejects.toThrow("terminal action contract");
  });

  it("loads a concrete cancellation receipt from the server", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(cancellationReceipt())));

    await expect(getReceipt(recoveryId)).resolves.toMatchObject({
      status: "closed_without_action",
      decision: "declined",
      executionCount: 0,
      providerDispatchStarted: false,
    });
  });

  it("rejects impossible cancellation execution evidence", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse(cancellationReceipt(1))),
    );

    await expect(getReceipt(recoveryId)).rejects.toThrow(
      "terminal evidence contract",
    );
  });

  it.each([
    { executionCount: 0 },
    { providerDispatchStarted: false },
    { providerExecution: false },
    { exactInterruptionRejected: true },
    { permissionRevoked: false },
    { approvedRemedyDigest: null },
    { decision: "declined" },
  ])("rejects impossible live approved evidence %#", async (invalidUpdate) => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({ ...liveApprovedReceipt(), ...invalidUpdate }),
      ),
    );

    await expect(getReceipt(recoveryId)).rejects.toThrow(
      "terminal evidence contract",
    );
  });

  it("recognizes both terminal snapshot statuses without accepting pending consent", () => {
    const base = {
      recoveryId,
      scenarioId: "hotel",
      executionMode: "sdk_stub",
      currentStep: 5,
      currentStepSummary: "Terminal server state.",
      createdAt: "2026-07-18T20:00:00Z",
      updatedAt: "2026-07-18T20:00:03Z",
      pendingApproval: null,
      rootTraceId: "qa_trace_11111111111111111111111111111111",
      modelIds: [],
      sdkVersion: "0.18.3",
      protocolVersion: "backchannel.approval.v1",
      agentGraphVersion: "backchannel.hotel-agent.v1",
      promptToolSchemaHash: "b".repeat(64),
    };
    expect(isRecoverySnapshot({ ...base, status: "closed_without_action" })).toBe(true);
    expect(isRecoverySnapshot({ ...base, status: "outcome_unknown" })).toBe(true);
    expect(
      isRecoverySnapshot({
        ...base,
        status: "outcome_unknown",
        pendingApproval: { impossible: true },
      }),
    ).toBe(false);
  });

  it("accepts server-returned live provenance without hard-coded model aliases", () => {
    expect(
      isRecoverySnapshot({
        recoveryId,
        scenarioId: "hotel",
        executionMode: "openai_live",
        status: "pending_approval",
        currentStep: 3,
        currentStepSummary: "Exact approval pending.",
        createdAt: "2026-07-18T20:00:00Z",
        updatedAt: "2026-07-18T20:00:03Z",
        pendingApproval: null,
        rootTraceId: "trace_11111111111111111111111111111111",
        modelIds: [
          "gpt-5.6-luna-2026-07-15-returned",
          "gpt-5.6-terra-2026-07-15-returned",
        ],
        sdkVersion: "0.18.3",
        protocolVersion: "backchannel.approval.v1",
        agentGraphVersion: "backchannel.hotel-live-agent.v1",
        promptToolSchemaHash: "c".repeat(64),
      }),
    ).toBe(true);
  });
});
