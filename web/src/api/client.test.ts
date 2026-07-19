import { afterEach, describe, expect, it, vi } from "vitest";

import {
  LIVE_ADMISSION_MESSAGES,
  LiveAdmissionError,
  createRecovery,
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
});
