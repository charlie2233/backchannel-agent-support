import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createRecovery,
  getHealth,
  getReceipt,
  getRecovery,
  HttpStatusError,
  isRecoverySnapshot,
  postDecision,
  PublicApiError,
} from "./client";

const recoveryId = "11111111-2222-4333-8444-555555555555";
const digest = `sha256:${"a".repeat(64)}` as `sha256:${string}`;

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function publicErrorResponse(
  code: string,
  message: string,
  status = 429,
  fallback: object | null = null,
  errorRecoveryId: string | null = null,
): Response {
  return new Response(
    JSON.stringify({
      error: {
        code,
        message,
        requestId: "req_11111111111111111111111111111111",
        recoveryId: errorRecoveryId,
        retryAfterSeconds: status === 429 ? 12 : null,
        fallback,
      },
    }),
    { status, headers: { "Content-Type": "application/json" } },
  );
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
    quotaEvidence: null,
  };
}

function replayRecoverySnapshot(activeRecoveryId = recoveryId) {
  return {
    recoveryId: activeRecoveryId,
    scenarioId: "hotel",
    executionMode: "replay_fixture",
    status: "completed",
    currentStep: 5,
    currentStepSummary: "Bundled replay completed.",
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

function replayRecoveryReceipt() {
  return {
    recoveryId,
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
    boundary: "Recorded simulated replay; no model call or provider execution.",
    providerResult: "No provider dispatch — recorded fixture outcome only.",
    authorizationSource: "Recorded fixture.",
    verificationResults: ["Recorded fixture verified."],
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
    quotaEvidence: null,
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
        publicErrorResponse(
          "not_found",
          "The requested resource was not found.",
          404,
          null,
          recoveryId,
        ),
      ),
    );

    const failure = await getRecovery(recoveryId).catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(HttpStatusError);
    expect(failure).toBeInstanceOf(PublicApiError);
    expect(failure).toMatchObject({ status: 404, code: "not_found" });
    expect((failure as Error).message).toBe(
      "The requested resource was not found.",
    );
  });

  it("rejects null or mismatched recovery correlation on resource failures", async () => {
    const request = {
      decision: "decline" as const,
      clientDecisionId: "decision-error-correlation",
      remedyId: "remedy-error-correlation",
      remedyDigest: digest,
      toolCallId: "call-error-correlation",
    };
    const resourceCalls: ReadonlyArray<() => Promise<unknown>> = [
      () => getRecovery(recoveryId),
      () => postDecision(recoveryId, request),
      () => getReceipt(recoveryId),
    ];

    for (const errorRecoveryId of [
      null,
      "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
    ]) {
      for (const resourceCall of resourceCalls) {
        vi.stubGlobal(
          "fetch",
          vi.fn().mockResolvedValue(
            publicErrorResponse(
              "not_found",
              "The requested resource was not found.",
              404,
              null,
              errorRecoveryId,
            ),
          ),
        );

        const failure = await resourceCall().catch((error: unknown) => error);

        expect(failure).toBeInstanceOf(PublicApiError);
        expect(failure).toMatchObject({
          code: "unexpected_response",
          status: 404,
          recoveryId: null,
        });
      }
    }
  });

  it("rejects successful recovery and receipt substitution by recovery ID", async () => {
    const substitutedRecoveryId = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
    for (const [request, body] of [
      [
        () => getRecovery(recoveryId),
        replayRecoverySnapshot(substitutedRecoveryId),
      ],
      [
        () => getReceipt(recoveryId),
        { ...cancellationReceipt(), recoveryId: substitutedRecoveryId },
      ],
    ] as const) {
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(body)));

      const failure = await request().catch((error: unknown) => error);

      expect(failure).toBeInstanceOf(PublicApiError);
      expect(failure).toMatchObject({
        code: "unexpected_response",
        status: 200,
        recoveryId: null,
      });
    }
  });

  it("binds a successful decision acknowledgement to the exact submitted claim", async () => {
    const request = {
      decision: "decline" as const,
      clientDecisionId: "decision-success-correlation",
      remedyId: "remedy-success-correlation",
      remedyDigest: digest,
      toolCallId: "call-success-correlation",
    };
    const valid = {
      clientDecisionId: request.clientDecisionId,
      recoveryId,
      decision: "decline",
      status: "closed_without_action",
      decisionRemedyDigest: digest,
      executionStarted: false,
    };
    const substitutions = [
      {
        ...valid,
        recoveryId: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
      },
      { ...valid, clientDecisionId: "decision-substituted" },
      { ...valid, decisionRemedyDigest: `sha256:${"f".repeat(64)}` },
      {
        clientDecisionId: request.clientDecisionId,
        recoveryId,
        decision: "approve",
        status: "completed",
        approvedRemedyDigest: digest,
        executionStarted: true,
      },
    ];

    for (const body of substitutions) {
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(body)));

      const failure = await postDecision(recoveryId, request).catch(
        (error: unknown) => error,
      );

      expect(failure).toBeInstanceOf(PublicApiError);
      expect(failure).toMatchObject({
        code: "unexpected_response",
        status: 200,
        recoveryId: null,
      });
    }
  });

  it("accepts only the exact typed live-start fallback envelope", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        publicErrorResponse(
          "live_cooldown",
          "Live mode is cooling down for this demo identity.",
          429,
          {
            kind: "show_replay_fixture",
            scenarioId: "hotel",
            executionMode: "replay_fixture",
          },
        ),
      ),
    );

    const failure = await createRecovery("hotel", "openai_live").catch(
      (error: unknown) => error,
    );

    expect(failure).toBeInstanceOf(PublicApiError);
    expect(failure).toMatchObject({
      code: "live_cooldown",
      retryAfterSeconds: 12,
      fallback: {
        kind: "show_replay_fixture",
        scenarioId: "hotel",
        executionMode: "replay_fixture",
      },
    });
  });

  it("turns an unrecognized error body into a local generic error without canaries", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            error: {
              code: "prompt-canary-code",
              message: "prompt-canary-message",
              requestId: "attacker-request-id",
              recoveryId: null,
              retryAfterSeconds: null,
              fallback: null,
              payload: "tool-payload-canary",
            },
          }),
          { status: 500, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );

    const failure = await getRecovery(recoveryId).catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(PublicApiError);
    expect(failure).toMatchObject({ code: "unexpected_response", status: 500 });
    expect((failure as Error).message).toBe("The server returned an unexpected response.");
    expect(JSON.stringify(failure)).not.toContain("canary");
    expect((failure as Error).stack).not.toContain("canary");
  });

  it("normalizes malformed successful JSON without exposing response canaries", async () => {
    const decision = {
      decision: "decline" as const,
      clientDecisionId: "decision-malformed-success",
      remedyId: "remedy-malformed-success",
      remedyDigest: digest,
      toolCallId: "call-malformed-success",
    };
    const requests: ReadonlyArray<() => Promise<unknown>> = [
      () => getHealth(),
      () => getRecovery(recoveryId),
      () => postDecision(recoveryId, decision),
      () => getReceipt(recoveryId),
    ];

    for (const request of requests) {
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue(
          new Response("2xx-json-response-canary{", {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        ),
      );

      const failure = await request().catch((error: unknown) => error);

      expect(failure).toBeInstanceOf(PublicApiError);
      expect(failure).toMatchObject({ code: "unexpected_response", status: 200 });
      expect((failure as Error).message).toBe(
        "The server returned an unexpected response.",
      );
      expect(JSON.stringify(failure)).not.toContain("canary");
      expect((failure as Error).stack).not.toContain("canary");
    }
  });

  it("rejects a recovery snapshot that silently changes the requested mode", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({
          recoveryId,
          scenarioId: "hotel",
          executionMode: "replay_fixture",
          status: "completed",
          currentStep: 5,
          currentStepSummary: "Bundled replay completed.",
          createdAt: "2026-07-18T20:00:00Z",
          updatedAt: "2026-07-18T20:00:03Z",
          pendingApproval: null,
          rootTraceId: null,
          modelIds: [],
          sdkVersion: null,
          protocolVersion: null,
          agentGraphVersion: null,
          promptToolSchemaHash: null,
        }),
      ),
    );

    await expect(createRecovery("hotel", "openai_live")).rejects.toMatchObject({
      code: "unexpected_response",
      status: 200,
    });
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
    ).rejects.toMatchObject({ code: "unexpected_response", status: 200 });
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

    await expect(getReceipt(recoveryId)).rejects.toMatchObject({
      code: "unexpected_response",
      status: 200,
    });
  });

  it.each([
    { status: "closed_without_action" },
    { simulated: false },
    { providerExecution: true },
    { modelIds: ["impossible-replay-model"] },
    { rootTraceId: "trace_11111111111111111111111111111111" },
    { sdkVersion: "0.18.3" },
    { protocolVersion: "backchannel.approval.v1" },
    { agentGraphVersion: "backchannel.hotel-agent.v1" },
    { promptToolSchemaHash: "b".repeat(64) },
    { decision: "approved" },
    { decisionRemedyDigest: digest },
    { executionCount: 1 },
    { providerDispatchStarted: true },
    { exactInterruptionRejected: true },
    { permissionRevoked: true },
    { scopeClosed: true },
    { approvedRemedyDigest: digest },
  ])("rejects impossible replay receipt evidence %#", async (invalidUpdate) => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({ ...replayRecoveryReceipt(), ...invalidUpdate }),
      ),
    );

    await expect(getReceipt(recoveryId)).rejects.toMatchObject({
      code: "unexpected_response",
      status: 200,
      recoveryId: null,
    });
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

    await expect(getReceipt(recoveryId)).rejects.toMatchObject({
      code: "unexpected_response",
      status: 200,
    });
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
