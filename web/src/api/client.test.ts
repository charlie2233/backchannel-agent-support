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
});
