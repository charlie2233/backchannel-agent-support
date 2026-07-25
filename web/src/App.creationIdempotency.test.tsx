import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import { LIVE_ADMISSION_MESSAGES } from "./api/client";

const HOTEL_RECOVERY_KEY = "backchannel.hotelRecoveryId";
const HOTEL_CREATION_INTENT_KEY =
  "backchannel.pendingRecoveryCreation.v1.hotel";
const API_QUOTA_CREATION_INTENT_KEY =
  "backchannel.pendingRecoveryCreation.v1.api-quota";
const LIVE_RECOVERY_ID = "11111111-2222-4333-8444-555555555555";
const REQUEST_ID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

type ExecutionMode = "openai_live" | "sdk_stub" | "replay_fixture";

interface CreationIntent {
  clientRequestId: string;
  scenarioId: "hotel" | "api-quota";
  executionMode: ExecutionMode;
}

function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function liveHealth(): Response {
  return jsonResponse({
    backend: "openai",
    liveReady: true,
    providerBoundary: "demo_adapter_only",
  });
}

function hotelSnapshot(
  executionMode: "openai_live" | "replay_fixture" = "openai_live",
): Record<string, unknown> {
  return {
    recoveryId: LIVE_RECOVERY_ID,
    scenarioId: "hotel",
    executionMode,
    modelIds:
      executionMode === "openai_live"
        ? ["gpt-5.6-luna", "gpt-5.6-terra"]
        : [],
    rootTraceId:
      executionMode === "openai_live"
        ? "trace_0123456789abcdef0123456789abcdef"
        : null,
    status: "in_progress",
    currentStep: 1,
    currentStepSummary:
      executionMode === "openai_live"
        ? "Idempotent live recovery accepted."
        : "Idempotent replay recovery accepted.",
    createdAt: "2026-07-24T12:00:00Z",
    updatedAt: "2026-07-24T12:00:01Z",
    pendingApproval: null,
    claimedDecision: null,
  };
}

function quotaSnapshot(): Record<string, unknown> {
  return {
    recoveryId: "22222222-2222-4222-8222-222222222222",
    scenarioId: "api-quota",
    executionMode: "sdk_stub",
    modelIds: [],
    rootTraceId: "qa_trace_0123456789abcdef0123456789abcdef",
    status: "completed",
    currentStep: 5,
    currentStepSummary: "Idempotent quota recovery accepted.",
    createdAt: "2026-07-24T12:00:00Z",
    updatedAt: "2026-07-24T12:00:01Z",
    pendingApproval: null,
    claimedDecision: null,
  };
}

function readHotelCreationIntent(): CreationIntent | null {
  const stored = window.sessionStorage.getItem(HOTEL_CREATION_INTENT_KEY);
  return stored === null ? null : JSON.parse(stored) as CreationIntent;
}

function readPostBody(init: RequestInit | undefined): CreationIntent {
  return JSON.parse(String(init?.body)) as CreationIntent;
}

afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("browser recovery-creation idempotency", () => {
  it("persists the exact intent before POST, then promotes a validated snapshot", async () => {
    const postBodies: CreationIntent[] = [];
    const intentsAtPost: Array<CreationIntent | null> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/health") return Promise.resolve(liveHealth());
        if (url === "/api/recoveries") {
          postBodies.push(readPostBody(init));
          intentsAtPost.push(readHotelCreationIntent());
          return Promise.resolve(jsonResponse(hotelSnapshot(), 201));
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    render(<App />);
    fireEvent.click(
      await screen.findByRole(
        "button",
        { name: "Start live recovery" },
        { timeout: 10_000 },
      ),
    );

    expect(
      (await screen.findAllByText("Idempotent live recovery accepted."))[0],
    ).toBeVisible();
    expect(postBodies).toHaveLength(1);
    expect(postBodies[0]).toEqual({
      clientRequestId: expect.stringMatching(REQUEST_ID_PATTERN),
      scenarioId: "hotel",
      executionMode: "openai_live",
    });
    expect(intentsAtPost[0]).toEqual(postBodies[0]);
    expect(window.sessionStorage.getItem(HOTEL_RECOVERY_KEY)).toBe(
      LIVE_RECOVERY_ID,
    );
    expect(window.sessionStorage.getItem(HOTEL_CREATION_INTENT_KEY)).toBeNull();
  });

  it(
    "retains and reuses one client request ID after an external AbortError and reload",
    async () => {
      const postBodies: CreationIntent[] = [];
      let rejectFirstStart: ((reason?: unknown) => void) | undefined;
      let liveReady = true;
      vi.stubGlobal(
        "fetch",
        vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
          const url = String(input);
          if (url === "/health") {
            return Promise.resolve(
              liveReady
                ? liveHealth()
                : jsonResponse({
                    backend: "stub",
                    liveReady: false,
                    providerBoundary: "demo_adapter_only",
                  }),
            );
          }
          if (url === "/api/recoveries") {
            postBodies.push(readPostBody(init));
            if (postBodies.length > 1) return new Promise<Response>(() => {});
            return new Promise<Response>((_resolve, reject) => {
              rejectFirstStart = reject;
            });
          }
          throw new Error(`Unexpected request: ${url}`);
        }),
      );

      const firstView = render(<App />);
      const start = await screen.findByRole(
        "button",
        { name: "Start live recovery" },
        { timeout: 10_000 },
      );
      fireEvent.click(start);
      expect(postBodies).toHaveLength(1);
      expect(rejectFirstStart).toBeTypeOf("function");

      await act(async () => {
        rejectFirstStart?.(
          new DOMException("private transport detail", "AbortError"),
        );
        await Promise.resolve();
        await Promise.resolve();
      });

      const firstBody = postBodies[0];
      expect(firstBody.clientRequestId).toMatch(REQUEST_ID_PATTERN);
      expect(readHotelCreationIntent()).toEqual(firstBody);

      expect(readHotelCreationIntent()).toEqual(firstBody);
      firstView.unmount();
      liveReady = false;

      render(<App />);
      const retry = await screen.findByRole(
        "button",
        { name: "Retry recovery start" },
        { timeout: 10_000 },
      );
      expect(postBodies).toHaveLength(1);
      expect(screen.queryByText(/private transport detail/i)).not.toBeInTheDocument();

      fireEvent.click(retry);
      await waitFor(() => expect(postBodies).toHaveLength(2));
      expect(postBodies[1]).toEqual(firstBody);
      expect(readHotelCreationIntent()).toEqual(firstBody);
    },
  );

  it("retains a stale quota success across unmount, then reuses and clears it after mounted acceptance", async () => {
    const postBodies: CreationIntent[] = [];
    let settleFirstStart: ((response: Response) => void) | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/health") return Promise.resolve(liveHealth());
        if (url === "/api/recoveries") {
          const body = readPostBody(init);
          postBodies.push(body);
          if (postBodies.length === 1) {
            return new Promise<Response>((resolve) => {
              settleFirstStart = resolve;
            });
          }
          return Promise.resolve(jsonResponse(quotaSnapshot(), 201));
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    const firstView = render(<App />);
    const firstQuotaAction = await screen.findByRole(
      "button",
      { name: /API quota recovery/i },
      { timeout: 10_000 },
    );
    await waitFor(() => expect(firstQuotaAction).toBeEnabled());
    fireEvent.click(firstQuotaAction);
    await waitFor(() => expect(settleFirstStart).toBeTypeOf("function"));
    expect(postBodies).toHaveLength(1);
    expect(postBodies[0]).toEqual({
      clientRequestId: expect.stringMatching(REQUEST_ID_PATTERN),
      scenarioId: "api-quota",
      executionMode: "sdk_stub",
    });
    expect(
      JSON.parse(
        window.sessionStorage.getItem(API_QUOTA_CREATION_INTENT_KEY) ?? "null",
      ),
    ).toEqual(postBodies[0]);

    firstView.unmount();
    await act(async () => {
      settleFirstStart?.(jsonResponse(quotaSnapshot(), 201));
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(
      JSON.parse(
        window.sessionStorage.getItem(API_QUOTA_CREATION_INTENT_KEY) ?? "null",
      ),
    ).toEqual(postBodies[0]);

    render(<App />);
    expect(postBodies).toHaveLength(1);
    const secondQuotaAction = await screen.findByRole(
      "button",
      { name: /API quota recovery/i },
      { timeout: 10_000 },
    );
    await waitFor(() => expect(secondQuotaAction).toBeEnabled());
    fireEvent.click(secondQuotaAction);

    expect(
      await screen.findByText("Idempotent quota recovery accepted."),
    ).toBeVisible();
    expect(postBodies).toHaveLength(2);
    expect(postBodies[1]).toEqual(postBodies[0]);
    expect(
      window.sessionStorage.getItem(API_QUOTA_CREATION_INTENT_KEY),
    ).toBeNull();
  });

  it("shows the safe quota capacity message and explicitly retries the same intent", async () => {
    const message =
      "Recovery creation is temporarily at capacity. Existing starts can still be retried; try a new start later.";
    const postBodies: CreationIntent[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/health") return Promise.resolve(liveHealth());
        if (url === "/api/recoveries") {
          const body = readPostBody(init);
          postBodies.push(body);
          return Promise.resolve(
            postBodies.length === 1
              ? jsonResponse(
                  {
                    code: "creation_capacity",
                    message,
                    requestId: "0123456789abcdef0123456789abcdef",
                  },
                  429,
                )
              : jsonResponse(quotaSnapshot(), 201),
          );
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    render(<App />);
    const quotaAction = await screen.findByRole(
      "button",
      { name: /API quota recovery/i },
      { timeout: 10_000 },
    );
    await waitFor(() => expect(quotaAction).toBeEnabled());
    fireEvent.click(quotaAction);

    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(postBodies).toHaveLength(1);
    const storedIntent = JSON.parse(
      window.sessionStorage.getItem(API_QUOTA_CREATION_INTENT_KEY) ?? "null",
    ) as CreationIntent;
    expect(storedIntent).toEqual(postBodies[0]);
    expect(document.body).not.toHaveTextContent(storedIntent.clientRequestId);

    fireEvent.click(
      screen.getByRole("button", { name: "Retry quota recovery" }),
    );
    expect(
      await screen.findByText("Idempotent quota recovery accepted."),
    ).toBeVisible();
    expect(postBodies).toHaveLength(2);
    expect(postBodies[1]).toEqual(storedIntent);
    expect(
      window.sessionStorage.getItem(API_QUOTA_CREATION_INTENT_KEY),
    ).toBeNull();
  });

  it("abandons a conflicting quota intent only on explicit fresh start", async () => {
    const message =
      "This recovery start no longer matches its original request. No additional run was started.";
    const postBodies: CreationIntent[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/health") return Promise.resolve(liveHealth());
        if (url === "/api/recoveries") {
          const body = readPostBody(init);
          postBodies.push(body);
          return Promise.resolve(
            postBodies.length === 1
              ? jsonResponse(
                  {
                    code: "idempotency_conflict",
                    message,
                    requestId: "0123456789abcdef0123456789abcdef",
                  },
                  409,
                )
              : jsonResponse(quotaSnapshot(), 201),
          );
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    render(<App />);
    const quotaAction = await screen.findByRole(
      "button",
      { name: /API quota recovery/i },
      { timeout: 10_000 },
    );
    await waitFor(() => expect(quotaAction).toBeEnabled());
    fireEvent.click(quotaAction);

    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(postBodies).toHaveLength(1);
    const conflicted = JSON.parse(
      window.sessionStorage.getItem(API_QUOTA_CREATION_INTENT_KEY) ?? "null",
    ) as CreationIntent;
    expect(conflicted).toEqual(postBodies[0]);
    expect(
      screen.queryByRole("button", { name: "Retry quota recovery" }),
    ).not.toBeInTheDocument();
    const startNew = screen.getByRole("button", {
      name: "Start a new quota recovery",
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(postBodies).toHaveLength(1);
    expect(document.body).not.toHaveTextContent(conflicted.clientRequestId);

    fireEvent.click(startNew);
    expect(
      await screen.findByText("Idempotent quota recovery accepted."),
    ).toBeVisible();
    expect(postBodies).toHaveLength(2);
    expect(postBodies[1]).toMatchObject({
      scenarioId: "api-quota",
      executionMode: "sdk_stub",
      clientRequestId: expect.stringMatching(REQUEST_ID_PATTERN),
    });
    expect(postBodies[1].clientRequestId).not.toBe(
      conflicted.clientRequestId,
    );
    expect(
      window.sessionStorage.getItem(API_QUOTA_CREATION_INTENT_KEY),
    ).toBeNull();
  });

  it("rotates the ID when a definitive live admission rejection falls back to replay", async () => {
    const postBodies: CreationIntent[] = [];
    const intentsAtPost: Array<CreationIntent | null> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/health") return Promise.resolve(liveHealth());
        if (url === "/api/recoveries") {
          const body = readPostBody(init);
          postBodies.push(body);
          intentsAtPost.push(readHotelCreationIntent());
          if (body.executionMode === "openai_live") {
            return Promise.resolve(
              jsonResponse(
                {
                  code: "live_capacity",
                  message: LIVE_ADMISSION_MESSAGES.live_capacity,
                  requestId: "0123456789abcdef0123456789abcdef",
                  fallbackExecutionMode: "replay_fixture",
                },
                429,
              ),
            );
          }
          return Promise.resolve(
            jsonResponse(hotelSnapshot("replay_fixture"), 201),
          );
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    render(<App />);
    fireEvent.click(
      await screen.findByRole(
        "button",
        { name: "Start live recovery" },
        { timeout: 10_000 },
      ),
    );

    expect(
      (await screen.findAllByText("Idempotent replay recovery accepted."))[0],
    ).toBeVisible();
    expect(postBodies).toHaveLength(2);
    expect(postBodies.map(({ executionMode }) => executionMode)).toEqual([
      "openai_live",
      "replay_fixture",
    ]);
    expect(postBodies[0].clientRequestId).toMatch(REQUEST_ID_PATTERN);
    expect(postBodies[1].clientRequestId).toMatch(REQUEST_ID_PATTERN);
    expect(postBodies[1].clientRequestId).not.toBe(
      postBodies[0].clientRequestId,
    );
    expect(intentsAtPost).toEqual(postBodies);
    expect(window.sessionStorage.getItem(HOTEL_CREATION_INTENT_KEY)).toBeNull();
    expect(window.sessionStorage.getItem(HOTEL_RECOVERY_KEY)).toBe(
      LIVE_RECOVERY_ID,
    );
    expect(document.body).not.toHaveTextContent(postBodies[0].clientRequestId);
    expect(document.body).not.toHaveTextContent(postBodies[1].clientRequestId);
  });

  it.each([
    {
      code: "idempotency_conflict",
      message:
        "This recovery start no longer matches its original request. No additional run was started.",
      status: 409,
    },
    {
      code: "creation_pending",
      message:
        "Recovery creation is still in progress. Retry the same start shortly.",
      status: 409,
    },
    {
      code: "creation_outcome_unknown",
      message:
        "The recovery start outcome could not be confirmed. No replacement run was started.",
      status: 409,
    },
    {
      code: "creation_capacity",
      message:
        "Recovery creation is temporarily at capacity. Existing starts can still be retried; try a new start later.",
      status: 429,
    },
  ] as const)(
    "surfaces $code without automatic retry, fallback, or key leakage",
    async ({ code, message, status }) => {
      const postBodies: CreationIntent[] = [];
      vi.stubGlobal(
        "fetch",
        vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
          const url = String(input);
          if (url === "/health") return Promise.resolve(liveHealth());
          if (url === "/api/recoveries") {
            postBodies.push(readPostBody(init));
            return Promise.resolve(
              jsonResponse(
                {
                  code,
                  message,
                  requestId: "0123456789abcdef0123456789abcdef",
                },
                status,
              ),
            );
          }
          throw new Error(`Unexpected request: ${url}`);
        }),
      );

      render(<App />);
      fireEvent.click(
        await screen.findByRole(
          "button",
          { name: "Start live recovery" },
          { timeout: 10_000 },
        ),
      );

      const alert = await screen.findByRole("alert");
      expect(alert).toHaveTextContent(message);
      expect(postBodies).toHaveLength(1);
      expect(postBodies[0]).toEqual({
        clientRequestId: expect.stringMatching(REQUEST_ID_PATTERN),
        scenarioId: "hotel",
        executionMode: "openai_live",
      });
      expect(readHotelCreationIntent()).toEqual(postBodies[0]);
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(postBodies).toHaveLength(1);
      expect(postBodies.map(({ executionMode }) => executionMode)).toEqual([
        "openai_live",
      ]);
      expect(alert).not.toHaveTextContent(postBodies[0].clientRequestId);
      expect(document.body).not.toHaveTextContent(postBodies[0].clientRequestId);
      if (code !== "idempotency_conflict") {
        expect(
          screen.queryByRole("button", { name: "Start a new recovery" }),
        ).not.toBeInTheDocument();
        expect(
          screen.getByRole("button", { name: "Retry recovery start" }),
        ).toBeVisible();
        fireEvent.click(
          screen.getByRole("button", { name: "Retry recovery start" }),
        );
        await waitFor(() => expect(postBodies).toHaveLength(2));
        expect(postBodies[1]).toEqual(postBodies[0]);
        expect(readHotelCreationIntent()).toEqual(postBodies[0]);
      }
    },
  );

  it("abandons only a conflicting local intent after explicit confirmation and starts nothing", async () => {
    const message =
      "This recovery start no longer matches its original request. No additional run was started.";
    const postBodies: CreationIntent[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/health") return Promise.resolve(liveHealth());
        if (url === "/api/recoveries") {
          postBodies.push(readPostBody(init));
          return Promise.resolve(
            jsonResponse(
              {
                code: "idempotency_conflict",
                message,
                requestId: "0123456789abcdef0123456789abcdef",
              },
              409,
            ),
          );
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    render(<App />);
    fireEvent.click(
      await screen.findByRole(
        "button",
        { name: "Start live recovery" },
        { timeout: 10_000 },
      ),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(postBodies).toHaveLength(1);
    expect(readHotelCreationIntent()).toEqual(postBodies[0]);
    expect(
      screen.queryByRole("button", { name: "Retry recovery start" }),
    ).not.toBeInTheDocument();
    const abandon = screen.getByRole("button", {
      name: "Start a new recovery",
    });
    expect(abandon).toBeVisible();
    expect(document.body).not.toHaveTextContent(postBodies[0].clientRequestId);

    fireEvent.click(abandon);

    await waitFor(() =>
      expect(
        window.sessionStorage.getItem(HOTEL_CREATION_INTENT_KEY),
      ).toBeNull(),
    );
    expect(
      await screen.findByRole("button", { name: "Start live recovery" }),
    ).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(postBodies).toHaveLength(1);
    expect(document.body).not.toHaveTextContent(postBodies[0].clientRequestId);
  });

  it("returns a replay conflict to explicit demo choices without auto-starting", async () => {
    const intent: CreationIntent = {
      clientRequestId: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
      scenarioId: "hotel",
      executionMode: "replay_fixture",
    };
    const message =
      "This recovery start no longer matches its original request. No additional run was started.";
    const postBodies: CreationIntent[] = [];
    window.sessionStorage.setItem(
      HOTEL_CREATION_INTENT_KEY,
      JSON.stringify(intent),
    );
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/health") {
          return Promise.resolve(
            jsonResponse({
              backend: "stub",
              liveReady: false,
              providerBoundary: "demo_adapter_only",
            }),
          );
        }
        if (url === "/api/recoveries") {
          postBodies.push(readPostBody(init));
          return Promise.resolve(
            jsonResponse(
              {
                code: "idempotency_conflict",
                message,
                requestId: "0123456789abcdef0123456789abcdef",
              },
              409,
            ),
          );
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    render(<App />);
    const retry = await screen.findByRole(
      "button",
      { name: "Retry recovery start" },
      { timeout: 10_000 },
    );
    expect(postBodies).toHaveLength(0);
    fireEvent.click(retry);

    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(postBodies).toEqual([intent]);
    fireEvent.click(
      screen.getByRole("button", { name: "Start a new recovery" }),
    );

    expect(
      await screen.findByRole("button", { name: "Run replay fixture" }),
    ).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Run SDK QA trace" }),
    ).toBeVisible();
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(postBodies).toEqual([intent]);
    expect(
      window.sessionStorage.getItem(HOTEL_CREATION_INTENT_KEY),
    ).toBeNull();
    expect(document.body).not.toHaveTextContent(intent.clientRequestId);
  });

  it("promotes a validated stale-generation success so reload can recover it", async () => {
    let settleStart: ((response: Response) => void) | undefined;
    let recoveryGets = 0;
    const postBodies: CreationIntent[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/health") return Promise.resolve(liveHealth());
        if (url === "/api/recoveries") {
          postBodies.push(readPostBody(init));
          return new Promise<Response>((resolve) => {
            settleStart = resolve;
          });
        }
        if (url === `/api/recoveries/${LIVE_RECOVERY_ID}`) {
          recoveryGets += 1;
          return Promise.resolve(jsonResponse(hotelSnapshot()));
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    const firstView = render(<App />);
    fireEvent.click(
      await screen.findByRole(
        "button",
        { name: "Start live recovery" },
        { timeout: 10_000 },
      ),
    );
    await waitFor(() => expect(settleStart).toBeTypeOf("function"));
    expect(postBodies[0].clientRequestId).toMatch(REQUEST_ID_PATTERN);
    expect(readHotelCreationIntent()).toEqual(postBodies[0]);

    firstView.unmount();
    await act(async () => {
      settleStart?.(jsonResponse(hotelSnapshot(), 201));
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(window.sessionStorage.getItem(HOTEL_RECOVERY_KEY)).toBe(
      LIVE_RECOVERY_ID,
    );
    expect(window.sessionStorage.getItem(HOTEL_CREATION_INTENT_KEY)).toBeNull();

    render(<App />);
    expect(
      (await screen.findAllByText("Idempotent live recovery accepted."))[0],
    ).toBeVisible();
    expect(recoveryGets).toBeGreaterThanOrEqual(1);
    expect(postBodies).toHaveLength(1);
  });
});
