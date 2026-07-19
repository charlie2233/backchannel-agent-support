import { afterEach, describe, expect, it, vi } from "vitest";

import {
  DECISION_CAPACITY_MESSAGE,
  DecisionCapacityError,
  LIVE_ADMISSION_MESSAGES,
  LiveAdmissionError,
  createRecovery,
  postDecision,
} from "./client";

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

      await expect(createRecovery("hotel", "openai_live")).rejects.toEqual(
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

    await expect(createRecovery("hotel", "openai_live")).rejects.toThrow(
      "Recovery creation failed with status 429",
    );
    await expect(createRecovery("hotel", "openai_live")).rejects.not.toThrow(unsafeMessage);
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

      const caught = await createRecovery("hotel", "openai_live").catch(
        (error: unknown) => error,
      );

      expect(caught).toBeInstanceOf(Error);
      expect(caught).not.toBeInstanceOf(LiveAdmissionError);
      expect((caught as Error).message).toBe(
        `Recovery creation failed with status ${status}`,
      );
    },
  );
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
});
