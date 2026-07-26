import { afterEach, describe, expect, it, vi } from "vitest";

import {
  DECISION_CAPACITY_MESSAGE,
  DecisionCapacityError,
  DecisionExpiredError,
  LIVE_ADMISSION_MESSAGES,
  LiveAdmissionError,
  RECOVERY_CREATION_MESSAGES,
  RecoveryCreationError,
  RecoveryLookupError,
  createRecovery,
  getHealth,
  getRecovery,
  getReceipt,
  isRecoverySnapshot,
  postDecision,
  postDecisionResume,
} from "./client";

const CLIENT_REQUEST_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";

const expectedMessages = {
  live_unavailable:
    "Live recovery is unavailable in this demo. A replay fixture is starting automatically; you can rerun it explicitly.",
  live_capacity:
    "Live recovery is currently at capacity. A replay fixture is starting automatically; you can rerun it explicitly.",
  cooldown:
    "Please wait before starting another live recovery. A replay fixture is starting automatically; you can rerun it explicitly.",
  daily_budget:
    "The daily live demo budget is currently reached. A replay fixture is starting automatically; you can rerun it explicitly.",
} as const;

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("getHealth cancellation", () => {
  it("forwards the caller's exact AbortSignal to the health request", async () => {
    const controller = new AbortController();
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          backend: "stub",
          liveReady: false,
          providerBoundary: "demo_adapter_only",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(getHealth(controller.signal)).resolves.toEqual({
      backend: "stub",
      liveReady: false,
      providerBoundary: "demo_adapter_only",
    });
    expect(fetchMock).toHaveBeenCalledWith("/health", {
      headers: { Accept: "application/json" },
      signal: controller.signal,
    });
  });
});

describe("createRecovery public errors", () => {
  it("keeps the client allowlist aligned with the public server explanations", () => {
    expect(LIVE_ADMISSION_MESSAGES).toEqual(expectedMessages);
  });

  it.each(["live_unavailable", "live_capacity", "cooldown", "daily_budget"] as const)(
    "returns a typed %s admission error with the explicit replay contract",
    async (code) => {
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue(
          new Response(
            JSON.stringify({
              code,
              message: LIVE_ADMISSION_MESSAGES[code],
              requestId: "0123456789abcdef0123456789abcdef",
              fallbackExecutionMode: "replay_fixture",
            }),
            { status: code === "live_unavailable" ? 422 : 429 },
          ),
        ),
      );

      await expect(
        createRecovery("hotel", "openai_live", CLIENT_REQUEST_ID),
      ).rejects.toEqual(
        expect.objectContaining<Partial<LiveAdmissionError>>({
          code,
          message: LIVE_ADMISSION_MESSAGES[code],
          fallbackExecutionMode: "replay_fixture",
        }),
      );
    },
  );

  it("does not surface an unrecognized server message as a live fallback explanation", async () => {
    const unsafeMessage = "pretend this arbitrary server text is safe";
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            code: "live_capacity",
            message: unsafeMessage,
            requestId: "0123456789abcdef0123456789abcdef",
            fallbackExecutionMode: "replay_fixture",
          }),
          { status: 429 },
        ),
      ),
    );

    await expect(
      createRecovery("hotel", "openai_live", CLIENT_REQUEST_ID),
    ).rejects.toThrow(
      "Recovery creation failed with status 429",
    );
    await expect(
      createRecovery("hotel", "openai_live", CLIENT_REQUEST_ID),
    ).rejects.not.toThrow(unsafeMessage);
  });

  it.each([
    { code: "live_unavailable", status: 429 },
    { code: "live_capacity", status: 422 },
    { code: "cooldown", status: 422 },
    { code: "daily_budget", status: 500 },
  ] as const)(
    "does not type $code as live admission when returned with status $status",
    async ({ code, status }) => {
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue(
          new Response(
            JSON.stringify({
              code,
              message: LIVE_ADMISSION_MESSAGES[code],
              requestId: "0123456789abcdef0123456789abcdef",
              fallbackExecutionMode: "replay_fixture",
            }),
            { status },
          ),
        ),
      );

      const caught = await createRecovery(
        "hotel",
        "openai_live",
        CLIENT_REQUEST_ID,
      ).catch((error: unknown) => error);

      expect(caught).toBeInstanceOf(Error);
      expect(caught).not.toBeInstanceOf(LiveAdmissionError);
      expect((caught as Error).message).toBe(
        `Recovery creation failed with status ${status}`,
      );
    },
  );

  it.each([
    {
      code: "idempotency_conflict",
      message:
        "This recovery start no longer matches its original request. No additional run was started.",
    },
    {
      code: "creation_pending",
      message: "Recovery creation is unresolved. Retry the same start shortly.",
    },
    {
      code: "creation_outcome_unknown",
      message:
        "The recovery start outcome could not be confirmed. No replacement run was started.",
    },
  ] as const)(
    "returns a typed exact-shaped $code without exposing arbitrary response text",
    async ({ code, message }) => {
      expect(RECOVERY_CREATION_MESSAGES[code]).toBe(message);
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue(
          new Response(
            JSON.stringify({
              code,
              message,
              requestId: "0123456789abcdef0123456789abcdef",
            }),
            { status: 409 },
          ),
        ),
      );

      await expect(
        createRecovery("hotel", "openai_live", CLIENT_REQUEST_ID),
      ).rejects.toEqual(
        expect.objectContaining<Partial<RecoveryCreationError>>({
          name: "RecoveryCreationError",
          code,
          message,
          requestId: "0123456789abcdef0123456789abcdef",
        }),
      );
    },
  );

  it("returns the exact typed creation-capacity error only at HTTP 429", async () => {
    const message =
      "Recovery creation is temporarily at capacity. Existing starts can still be retried; try a new start later.";
    expect(RECOVERY_CREATION_MESSAGES.creation_capacity).toBe(message);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            code: "creation_capacity",
            message,
            requestId: "0123456789abcdef0123456789abcdef",
          }),
          { status: 429 },
        ),
      ),
    );

    await expect(
      createRecovery("hotel", "sdk_stub", CLIENT_REQUEST_ID),
    ).rejects.toEqual(
      expect.objectContaining<Partial<RecoveryCreationError>>({
        name: "RecoveryCreationError",
        code: "creation_capacity",
        message,
        requestId: "0123456789abcdef0123456789abcdef",
      }),
    );

    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            code: "creation_capacity",
            message,
            requestId: "0123456789abcdef0123456789abcdef",
          }),
          { status: 409 },
        ),
      ),
    );
    const wrongStatus = await createRecovery(
      "hotel",
      "sdk_stub",
      CLIENT_REQUEST_ID,
    ).catch((error: unknown) => error);
    expect(wrongStatus).toBeInstanceOf(Error);
    expect(wrongStatus).not.toBeInstanceOf(RecoveryCreationError);
    expect((wrongStatus as Error).message).toBe(
      "Recovery creation failed with status 409",
    );
  });
});

describe("postDecision public errors", () => {
  const decision = {
    action: "approve" as const,
    clientDecisionId: "decision-retry-742",
    remedyId: "remedy-server-742",
    remedyDigest: `sha256:${"0".repeat(64)}` as `sha256:${string}`,
    toolCallId: "call-server-742",
  };

  it("returns a typed endpoint-specific capacity error with the safe retry message", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            code: "decision_capacity",
            message: DECISION_CAPACITY_MESSAGE,
            requestId: "0123456789abcdef0123456789abcdef",
          }),
          { status: 429 },
        ),
      ),
    );

    await expect(
      postDecision("11111111-2222-4333-8444-555555555555", decision),
    ).rejects.toEqual(
      expect.objectContaining<Partial<DecisionCapacityError>>({
        code: "decision_capacity",
        message: DECISION_CAPACITY_MESSAGE,
        requestId: "0123456789abcdef0123456789abcdef",
      }),
    );
  });

  it.each([
    { status: 429, extraFallback: true },
    { status: 500, extraFallback: false },
  ])(
    "rejects a non-contract decision-capacity envelope at status $status as generic",
    async ({ status, extraFallback }) => {
      const body: Record<string, string> = {
        code: "decision_capacity",
        message: DECISION_CAPACITY_MESSAGE,
        requestId: "0123456789abcdef0123456789abcdef",
      };
      if (extraFallback) {
        body.fallbackExecutionMode = "replay_fixture";
      }
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue(
          new Response(JSON.stringify(body), { status }),
        ),
      );

      await expect(
        postDecision("11111111-2222-4333-8444-555555555555", decision),
      ).rejects.toThrow(`Decision request failed with status ${status}`);
    },
  );

  it("returns a typed exact-expiry error only for the requested recovery", async () => {
    const recoveryId = "11111111-2222-4333-8444-555555555555";
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            detail: {
              code: "remedy_expired",
              recoveryId,
            },
          }),
          { status: 422 },
        ),
      ),
    );

    await expect(postDecision(recoveryId, decision)).rejects.toEqual(
      expect.objectContaining<Partial<DecisionExpiredError>>({
        name: "DecisionExpiredError",
        code: "remedy_expired",
        recoveryId,
      }),
    );
  });

  it("rejects an uncorrelated expiry envelope as a generic error", async () => {
    const recoveryId = "11111111-2222-4333-8444-555555555555";
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            detail: {
              code: "remedy_expired",
              recoveryId: "99999999-2222-4333-8444-555555555555",
            },
          }),
          { status: 422 },
        ),
      ),
    );

    const error = await postDecision(recoveryId, decision).catch(
      (caught: unknown) => caught,
    );
    expect(error).toBeInstanceOf(Error);
    expect(error).not.toBeInstanceOf(DecisionExpiredError);
    expect((error as Error).message).toBe(
      "Decision request failed with status 422",
    );
  });
});

describe("claimed decision and explicit resume contracts", () => {
  const recoveryId = "11111111-2222-4333-8444-555555555555";
  const claimedDecision = {
    action: "approve" as const,
    remedyDigest: `sha256:${"a".repeat(64)}` as `sha256:${string}`,
    expiry: "2026-09-01T18:45:30Z",
  };
  const snapshot = {
    recoveryId,
    scenarioId: "hotel",
    executionMode: "sdk_stub",
    modelIds: [],
    rootTraceId: "qa_trace_0123456789abcdef0123456789abcdef",
    status: "pending_approval",
    currentStep: 3,
    currentStepSummary: "Exact approve claimed; outcome pending.",
    createdAt: "2026-07-20T01:00:00Z",
    updatedAt: "2026-07-20T01:00:01Z",
    pendingApproval: null,
    claimedDecision,
  };

  it("strictly validates the minimal claimedDecision snapshot", () => {
    expect(isRecoverySnapshot(snapshot)).toBe(true);
    expect(
      isRecoverySnapshot({
        ...snapshot,
        claimedDecision: { ...claimedDecision, clientDecisionId: "hidden" },
      }),
    ).toBe(false);
    expect(
      isRecoverySnapshot({
        ...snapshot,
        claimedDecision: { ...claimedDecision, remedyDigest: `sha256:${"A".repeat(64)}` },
      }),
    ).toBe(false);
    expect(
      isRecoverySnapshot({
        ...snapshot,
        claimedDecision: { ...claimedDecision, expiry: "2026-09-01T11:45:30-07:00" },
      }),
    ).toBe(false);
    expect(
      isRecoverySnapshot({ ...snapshot, pendingApproval: {} }),
    ).toBe(false);
    expect(
      isRecoverySnapshot({
        ...snapshot,
        executionMode: "replay_fixture",
        modelIds: [],
        rootTraceId: null,
      }),
    ).toBe(false);
  });

  it("posts exact empty JSON and accepts the matching strict response", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          action: "approve",
          recoveryId,
          remedyDigest: claimedDecision.remedyDigest,
          status: "completed",
          approvedRemedyDigest: claimedDecision.remedyDigest,
          executionStarted: true,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(postDecisionResume(recoveryId, claimedDecision)).resolves.toEqual(
      expect.objectContaining({ action: "approve", remedyDigest: claimedDecision.remedyDigest }),
    );
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/recoveries/${recoveryId}/decisions/resume`,
      expect.objectContaining({ method: "POST", body: "{}" }),
    );
  });

  it("returns a typed exact-expiry error for the correlated resume", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            detail: {
              code: "remedy_expired",
              recoveryId,
            },
          }),
          { status: 422 },
        ),
      ),
    );

    await expect(
      postDecisionResume(recoveryId, claimedDecision),
    ).rejects.toBeInstanceOf(DecisionExpiredError);
  });

  it.each([
    { changed: { action: "decline" }, label: "action mismatch" },
    { changed: { recoveryId: "99999999-2222-4333-8444-555555555555" }, label: "recovery mismatch" },
    { changed: { remedyDigest: `sha256:${"b".repeat(64)}` }, label: "digest mismatch" },
    { changed: { clientDecisionId: "must-not-expand" }, label: "extra key" },
  ])("rejects a resume response with $label", async ({ changed }) => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            action: "approve",
            recoveryId,
            remedyDigest: claimedDecision.remedyDigest,
            status: "completed",
            approvedRemedyDigest: claimedDecision.remedyDigest,
            executionStarted: true,
            ...changed,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );

    await expect(postDecisionResume(recoveryId, claimedDecision)).rejects.toThrow(
      /resume response/i,
    );
  });
});

describe("pending approval policy eligibility contract", () => {
  const approval = {
    remedyId: "remedy-policy-test",
    remedyDigest: `sha256:${"a".repeat(64)}`,
    terms: {
      bookingId: "booking-policy-test",
      action: "replace_room",
      replacement: { fromRoomType: "double", toRoomType: "king" },
      stay: { checkIn: "2026-08-14", checkOut: "2026-08-16" },
      currency: "USD",
    },
    costDeltaMinor: 0,
    changedFields: ["room_type"],
    providerCommitments: ["Preserve booking dates"],
    expiry: "2026-08-14T00:00:00Z",
    hardConstraintSatisfied: true,
    delegatedAuthoritySatisfied: true,
    toolCallId: "tool-policy-test",
    executionStarted: false,
  };
  const snapshot = {
    recoveryId: "11111111-2222-4333-8444-555555555555",
    scenarioId: "hotel",
    executionMode: "sdk_stub",
    modelIds: [],
    rootTraceId: "qa_trace_0123456789abcdef0123456789abcdef",
    status: "pending_approval",
    currentStep: 3,
    currentStepSummary: "Policy-eligible consent is pending.",
    createdAt: "2026-07-20T01:00:00Z",
    updatedAt: "2026-07-20T01:00:01Z",
    pendingApproval: approval,
    claimedDecision: null,
  };

  it("accepts pending consent only when both policy checks are exactly true", () => {
    expect(isRecoverySnapshot(snapshot)).toBe(true);
    expect(isRecoverySnapshot({
      ...snapshot,
      pendingApproval: { ...approval, hardConstraintSatisfied: false },
    })).toBe(false);
    expect(isRecoverySnapshot({
      ...snapshot,
      pendingApproval: { ...approval, delegatedAuthoritySatisfied: false },
    })).toBe(false);
  });
});

describe("getReceipt runtime validation", () => {
  const validReceipt = {
    recoveryId: "11111111-2222-4333-8444-555555555555",
    executionMode: "sdk_stub",
    status: "completed",
    simulated: true,
    providerExecution: true,
    modelCall: false,
    modelIds: [],
    rootTraceId: "qa_trace_0123456789abcdef0123456789abcdef",
    sdkVersion: "0.18.3",
    protocolVersion: "backchannel.approval.v1",
    agentGraphVersion: "backchannel.hotel-agent.v1",
    definitionDigest: "b".repeat(64),
    boundary:
      "Deterministic Agents SDK model and demo hotel adapter only; no OpenAI model call, real booking, or payment change.",
    providerResult: "Demo adapter confirmed the authorized action.",
    authorizationSource: "Approved Agents SDK commit_remedy interruption.",
    verificationResults: [
      "Immediate pre-execution remedy digest matched the approved digest.",
      "Temporary provider-dispatch permission revoked after the approved execution.",
    ],
    approvalCount: 1,
    approvedRemedyDigest: `sha256:${"a".repeat(64)}`,
  };

  it("accepts a complete mode-bound terminal receipt", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify(validReceipt), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    await expect(getReceipt(validReceipt.recoveryId)).resolves.toEqual(validReceipt);
  });

  it("accepts recorded live snapshot model IDs without hard-coding a model family", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            recoveryId: validReceipt.recoveryId,
            scenarioId: "hotel",
            executionMode: "openai_live",
            modelIds: ["recorded-model-a", "recorded-model-b"],
            rootTraceId: "trace_0123456789abcdef0123456789abcdef",
            status: "in_progress",
            currentStep: 0,
            currentStepSummary: "Live recovery started.",
            createdAt: "2026-07-19T12:00:00Z",
            updatedAt: "2026-07-19T12:00:01Z",
            pendingApproval: null,
            claimedDecision: null,
          }),
          { status: 201, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );

    await expect(
      createRecovery("hotel", "openai_live", CLIENT_REQUEST_ID),
    ).resolves.toEqual(
      expect.objectContaining({ modelIds: ["recorded-model-a", "recorded-model-b"] }),
    );
  });

  it("rejects a receipt returned for another recovery", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            ...validReceipt,
            recoveryId: "99999999-2222-4333-8444-555555555555",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );

    await expect(getReceipt(validReceipt.recoveryId)).rejects.toThrow(
      "Receipt response did not match the receipt contract",
    );
  });

  it("accepts a canonical zero-approval SDK quota receipt", async () => {
    const quotaReceipt = {
      ...validReceipt,
      approvalCount: 0,
      approvedRemedyDigest: null,
      boundary:
        "Deterministic Agents SDK stub and demo quota adapter only; no OpenAI model call or real quota change.",
      providerResult:
        "Demo quota adapter verified 1200 units against a temporary 1250-unit US-region ceiling; no real quota was changed.",
      authorizationSource:
        "Predelegated API quota policy: US-only, at most 500 USD minor units, for at most 900 seconds.",
      verificationResults: [
        "Provider proved the baseline quota ceiling at 1000 units.",
        "Temporary US-region burst granted: 250 units for 900 seconds.",
        "All hard constraints remained satisfied.",
        "Extra cost of 300 USD minor units stayed within the delegated 500-unit limit.",
        "Approval count is zero; no human interruption was created.",
        "Execution verified at an effective ceiling of 1250 units.",
        "Temporary quota permission revoked; baseline ceiling restored to 1000 units.",
      ],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify(quotaReceipt), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    await expect(getReceipt(quotaReceipt.recoveryId)).resolves.toEqual(quotaReceipt);
  });

  it("accepts canonical zero-approval SDK quota uncertainty evidence", async () => {
    const unknownQuotaReceipt = {
      ...validReceipt,
      status: "outcome_unknown",
      providerExecution: null,
      approvalCount: 0,
      approvedRemedyDigest: null,
      boundary:
        "Deterministic Agents SDK stub and demo quota adapter only; no OpenAI model call or real quota change.",
      providerResult:
        "Demo quota dispatch may have begun; its outcome could not be confirmed.",
      authorizationSource:
        "Predelegated quota authority existed, but restart evidence cannot confirm execution.",
      verificationResults: [
        "Recovery creation was durably marked started.",
        "No human approval was recorded.",
        "No complete quota receipt was committed before restart.",
        "Provider execution, verification, and permission revocation are not asserted.",
        "Startup reconciliation did not redispatch the quota operation.",
        "Uncertain quota outcome receipt sealed.",
      ],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify(unknownQuotaReceipt), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    await expect(getReceipt(unknownQuotaReceipt.recoveryId)).resolves.toEqual(
      unknownQuotaReceipt,
    );
  });

  it("rejects drift in canonical SDK quota uncertainty evidence", async () => {
    const changedUnknownQuotaReceipt = {
      ...validReceipt,
      status: "outcome_unknown",
      providerExecution: null,
      approvalCount: 0,
      approvedRemedyDigest: null,
      boundary:
        "Deterministic Agents SDK stub and demo quota adapter only; no OpenAI model call or real quota change.",
      providerResult: "Quota outcome was probably fine.",
      authorizationSource:
        "Predelegated quota authority existed, but restart evidence cannot confirm execution.",
      verificationResults: [
        "Recovery creation was durably marked started.",
        "No human approval was recorded.",
        "No complete quota receipt was committed before restart.",
        "Provider execution, verification, and permission revocation are not asserted.",
        "Startup reconciliation did not redispatch the quota operation.",
        "Uncertain quota outcome receipt sealed.",
      ],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify(changedUnknownQuotaReceipt), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    await expect(
      getReceipt(changedUnknownQuotaReceipt.recoveryId),
    ).rejects.toThrow("Receipt response did not match the receipt contract");
  });

  it("accepts a canonical unknown-outcome receipt for one claimed approval", async () => {
    const unknownReceipt = {
      ...validReceipt,
      status: "outcome_unknown",
      providerExecution: null,
      providerResult: "Claimed decision expired; provider outcome remains unknown.",
      authorizationSource:
        "A durable exact decision was claimed before consent expiry; no provider outcome is asserted.",
      verificationResults: [
        "Human consent requested.",
        "An exact decision was claimed before consent expiry.",
        "Durable evidence cannot prove that provider dispatch did not begin.",
        "Cancellation and zero execution are not claimed.",
        "Temporary permission revoked.",
        "Uncertain claim-expiry receipt sealed.",
      ],
      approvalCount: 1,
      approvedRemedyDigest: null,
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify(unknownReceipt), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    await expect(getReceipt(unknownReceipt.recoveryId)).resolves.toEqual(unknownReceipt);
  });

  it("preserves zero-approval SDK hotel uncertainty evidence", async () => {
    const unknownHotelReceipt = {
      ...validReceipt,
      status: "outcome_unknown",
      providerExecution: null,
      providerResult: "Hotel provider outcome remains unknown.",
      authorizationSource: "No provider outcome is asserted.",
      verificationResults: [
        "No human approval was recorded.",
        "Provider execution is not asserted.",
      ],
      approvalCount: 0,
      approvedRemedyDigest: null,
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify(unknownHotelReceipt), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    await expect(getReceipt(unknownHotelReceipt.recoveryId)).resolves.toEqual(
      unknownHotelReceipt,
    );
  });

  it.each([
    { name: "approval count two", update: { approvalCount: 2 } },
    { name: "provider execution false", update: { providerExecution: false } },
    { name: "provider execution true", update: { providerExecution: true } },
    {
      name: "an approved remedy digest",
      update: { approvedRemedyDigest: `sha256:${"a".repeat(64)}` },
    },
  ])("rejects an unknown-outcome receipt with $name", async ({ update }) => {
    const unknownReceipt = {
      ...validReceipt,
      status: "outcome_unknown",
      providerExecution: null,
      approvalCount: 1,
      approvedRemedyDigest: null,
      ...update,
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify(unknownReceipt), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    await expect(getReceipt(unknownReceipt.recoveryId)).rejects.toThrow(
      "Receipt response did not match the receipt contract",
    );
  });

  it.each([
    { name: "an extra public field", update: { leakedState: "serialized" } },
    { name: "a replay model claim", update: { executionMode: "replay_fixture", rootTraceId: null } },
    { name: "an invalid definition digest", update: { definitionDigest: "not-a-digest" } },
  ])("rejects $name instead of rendering unvalidated evidence", async ({ update }) => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ ...validReceipt, ...update }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    await expect(getReceipt(validReceipt.recoveryId)).rejects.toThrow(
      "Receipt response did not match the receipt contract",
    );
  });
});

describe("recovery response correlation", () => {
  const requestedRecoveryId = "11111111-2222-4333-8444-555555555555";
  const validSnapshot = {
    recoveryId: requestedRecoveryId,
    scenarioId: "hotel",
    executionMode: "sdk_stub",
    modelIds: [],
    rootTraceId: "qa_trace_0123456789abcdef0123456789abcdef",
    status: "in_progress",
    currentStep: 1,
    currentStepSummary: "Server recovery is active.",
    createdAt: "2026-07-19T12:00:00Z",
    updatedAt: "2026-07-19T12:00:01Z",
    pendingApproval: null,
    claimedDecision: null,
  };

  it("classifies a generic 404 as a terminal saved-recovery lookup", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(null, { status: 404 })),
    );

    await expect(getRecovery(requestedRecoveryId)).rejects.toEqual(
      expect.objectContaining<Partial<RecoveryLookupError>>({
        name: "RecoveryLookupError",
        disposition: "terminal",
      }),
    );
  });

  it.each([
    {
      name: "network failure",
      fetchResult: () => Promise.reject(new Error("private network details")),
    },
    {
      name: "503 response",
      fetchResult: () =>
        Promise.resolve(new Response("private upstream details", { status: 503 })),
    },
    {
      name: "malformed JSON response",
      fetchResult: () =>
        Promise.resolve(
          new Response("not-json", {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        ),
    },
    {
      name: "contract-invalid response",
      fetchResult: () =>
        Promise.resolve(
          new Response(JSON.stringify({
            ...validSnapshot,
            unexpected: "private contract detail",
          }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        ),
    },
  ])("classifies a $name as a retryable saved-recovery lookup", async ({ fetchResult }) => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation(fetchResult));

    const error = await getRecovery(requestedRecoveryId).catch(
      (caught: unknown) => caught,
    );

    expect(error).toEqual(
      expect.objectContaining<Partial<RecoveryLookupError>>({
        name: "RecoveryLookupError",
        disposition: "retryable",
      }),
    );
    expect((error as Error).message).toBe(
      "Saved recovery evidence is temporarily unavailable.",
    );
  });

  it("passes an abort error through unchanged", async () => {
    const abortError = Object.assign(new Error("request aborted"), {
      name: "AbortError",
    });
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(abortError));

    await expect(getRecovery(requestedRecoveryId)).rejects.toBe(abortError);
  });

  it("classifies a shape-valid GET response for a different recovery as retryable", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            ...validSnapshot,
            recoveryId: "99999999-2222-4333-8444-555555555555",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );

    await expect(getRecovery(requestedRecoveryId)).rejects.toEqual(
      expect.objectContaining<Partial<RecoveryLookupError>>({
        name: "RecoveryLookupError",
        disposition: "retryable",
      }),
    );
  });

  it.each([
    {
      name: "scenario",
      response: { ...validSnapshot, scenarioId: "api-quota" },
    },
    {
      name: "execution mode",
      response: {
        ...validSnapshot,
        executionMode: "replay_fixture",
        rootTraceId: null,
      },
    },
  ])("rejects a shape-valid create response for another $name", async ({ response }) => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify(response), {
          status: 201,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    await expect(
      createRecovery("hotel", "sdk_stub", CLIENT_REQUEST_ID),
    ).rejects.toThrow("Recovery response did not match the requested creation");
  });
});
