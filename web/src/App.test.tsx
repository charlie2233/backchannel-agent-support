import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "./App";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function stubHealthWithUnavailableRecovery() {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((input: string | URL | Request) => {
      const url = String(input);
      if (url === "/health") {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              backend: "stub",
              liveReady: false,
              providerBoundary: "demo_adapter_only",
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
        );
      }
      if (url === "/api/recoveries") {
        return Promise.resolve(new Response(null, { status: 503 }));
      }
      throw new Error(`Unexpected request: ${url}`);
    }),
  );
}

describe("Backchannel console", () => {
  it("runs API quota only through the zero-approval SDK stub and renders its proof", async () => {
    const requests: Array<{ scenarioId: string; executionMode: string }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/health") {
          return Promise.resolve(
            new Response(
              JSON.stringify({
                backend: "openai",
                liveReady: true,
                providerBoundary: "demo_adapter_only",
              }),
              { status: 200, headers: { "Content-Type": "application/json" } },
            ),
          );
        }
        if (url === "/api/recoveries") {
          const request = JSON.parse(String(init?.body)) as {
            scenarioId: string;
            executionMode: string;
          };
          requests.push(request);
          const quota = request.scenarioId === "api-quota";
          return Promise.resolve(
            new Response(
              JSON.stringify({
                recoveryId: quota
                  ? "22222222-2222-4222-8222-222222222222"
                  : "11111111-1111-4111-8111-111111111111",
                scenarioId: request.scenarioId,
                executionMode: request.executionMode,
                modelIds: quota ? [] : ["gpt-5.6-luna", "gpt-5.6-terra"],
                rootTraceId: quota
                  ? "qa_trace_0123456789abcdef0123456789abcdef"
                  : "trace_0123456789abcdef0123456789abcdef",
                status: quota ? "completed" : "in_progress",
                currentStep: quota ? 5 : 0,
                currentStepSummary: quota
                  ? "Execution verified, temporary permission revoked, and receipt sealed."
                  : "Live hotel recovery started.",
                createdAt: "2026-07-19T12:00:00Z",
                updatedAt: "2026-07-19T12:00:01Z",
                pendingApproval: null,
              }),
              { status: 201, headers: { "Content-Type": "application/json" } },
            ),
          );
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    render(
      <StrictMode>
        <App />
      </StrictMode>,
    );
    expect((await screen.findAllByText("Live hotel recovery started."))[0]).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: /API quota recovery/i }));

    expect(
      await screen.findByText(
        "Execution verified, temporary permission revoked, and receipt sealed.",
      ),
    ).toBeVisible();
    expect(requests).toEqual([
      { scenarioId: "hotel", executionMode: "openai_live" },
      { scenarioId: "api-quota", executionMode: "sdk_stub" },
    ]);
    const lifecycle = screen.getByRole("list", { name: "Recovery lifecycle" });
    expect(within(lifecycle).getByText("Provider proved a 1000-unit baseline ceiling.")).toBeVisible();
    expect(
      within(lifecycle).getByText("A 250-unit us-east-1 burst raises the ceiling to 1250."),
    ).toBeVisible();
    expect(
      within(lifecycle).getByText(
        "Delegated authority covers 300 USD minor units within a 500-unit limit; approval count is zero.",
      ),
    ).toBeVisible();
    expect(
      within(lifecycle).getByText(
        "Execution was verified and temporary permission quota-burst-demo-us-east-1 was revoked.",
      ),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: "Approve remedy" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Decline" })).not.toBeInTheDocument();
    expect(screen.queryByText("GPT-5.6 agents")).not.toBeInTheDocument();
    expect(screen.getByText("SDK stub")).toBeVisible();
  });

  it("keeps the automatic hotel replay fallback out of the API quota workspace", async () => {
    const requests: Array<{ scenarioId: string; executionMode: string }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/health") {
          return Promise.resolve(
            new Response(
              JSON.stringify({
                backend: "stub",
                liveReady: false,
                providerBoundary: "demo_adapter_only",
              }),
              { status: 200, headers: { "Content-Type": "application/json" } },
            ),
          );
        }
        if (url === "/api/recoveries") {
          const request = JSON.parse(String(init?.body)) as {
            scenarioId: string;
            executionMode: string;
          };
          requests.push(request);
          const quota = request.scenarioId === "api-quota";
          return Promise.resolve(
            new Response(
              JSON.stringify({
                recoveryId: quota
                  ? "22222222-2222-4222-8222-222222222222"
                  : "11111111-1111-4111-8111-111111111111",
                scenarioId: request.scenarioId,
                executionMode: request.executionMode,
                modelIds: [],
                rootTraceId: quota
                  ? "qa_trace_0123456789abcdef0123456789abcdef"
                  : null,
                status: quota ? "completed" : "in_progress",
                currentStep: quota ? 5 : 1,
                currentStepSummary: quota
                  ? "Quota SDK proof loaded."
                  : "Hotel replay fallback loaded.",
                createdAt: "2026-07-19T12:00:00Z",
                updatedAt: "2026-07-19T12:00:01Z",
                pendingApproval: null,
              }),
              { status: 201, headers: { "Content-Type": "application/json" } },
            ),
          );
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    render(<App />);
    expect((await screen.findAllByText("Hotel replay fallback loaded."))[0]).toBeVisible();
    expect(screen.getByText(/replay fixture is starting automatically/i)).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: /API quota recovery/i }));

    expect(await screen.findByText("Quota SDK proof loaded.")).toBeVisible();
    expect(screen.queryByText(/replay fixture is starting automatically/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Run replay fixture" })).not.toBeInTheDocument();
    expect(requests).toEqual([
      { scenarioId: "hotel", executionMode: "replay_fixture" },
      { scenarioId: "api-quota", executionMode: "sdk_stub" },
    ]);
  });

  it("coalesces rapid quota clicks, keeps SDK provenance on failure, and permits retry", async () => {
    const quotaRequests: string[] = [];
    let settleFirstQuotaRequest: ((response: Response) => void) | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/health") {
          return Promise.resolve(
            new Response(
              JSON.stringify({
                backend: "openai",
                liveReady: true,
                providerBoundary: "demo_adapter_only",
              }),
              { status: 200, headers: { "Content-Type": "application/json" } },
            ),
          );
        }
        if (url === "/api/recoveries") {
          const request = JSON.parse(String(init?.body)) as {
            scenarioId: string;
            executionMode: string;
          };
          if (request.scenarioId === "api-quota") {
            quotaRequests.push(request.executionMode);
            if (quotaRequests.length === 1) {
              return new Promise<Response>((resolve) => {
                settleFirstQuotaRequest = resolve;
              });
            }
            return Promise.resolve(
              new Response(
                JSON.stringify({
                  recoveryId: "22222222-2222-4222-8222-222222222222",
                  scenarioId: "api-quota",
                  executionMode: "sdk_stub",
                  modelIds: [],
                  rootTraceId: "qa_trace_0123456789abcdef0123456789abcdef",
                  status: "completed",
                  currentStep: 5,
                  currentStepSummary: "Quota retry completed with server evidence.",
                  createdAt: "2026-07-19T12:00:00Z",
                  updatedAt: "2026-07-19T12:00:01Z",
                  pendingApproval: null,
                }),
                { status: 201, headers: { "Content-Type": "application/json" } },
              ),
            );
          }
          return Promise.resolve(
            new Response(
              JSON.stringify({
                recoveryId: "11111111-1111-4111-8111-111111111111",
                scenarioId: "hotel",
                executionMode: "openai_live",
                modelIds: ["gpt-5.6-luna", "gpt-5.6-terra"],
                rootTraceId: "trace_0123456789abcdef0123456789abcdef",
                status: "in_progress",
                currentStep: 0,
                currentStepSummary: "Live hotel recovery started.",
                createdAt: "2026-07-19T12:00:00Z",
                updatedAt: "2026-07-19T12:00:01Z",
                pendingApproval: null,
              }),
              { status: 201, headers: { "Content-Type": "application/json" } },
            ),
          );
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    render(<App />);
    expect((await screen.findAllByText("Live hotel recovery started."))[0]).toBeVisible();
    const quotaButton = screen.getByRole("button", { name: /API quota recovery/i });
    fireEvent.click(quotaButton);
    fireEvent.click(quotaButton);

    expect(quotaRequests).toEqual(["sdk_stub"]);
    expect(screen.getByText("SDK QA workspace")).toBeVisible();
    expect(screen.getByText("Loading deterministic trace")).toBeVisible();
    settleFirstQuotaRequest?.(new Response(null, { status: 500 }));

    expect(
      await screen.findByRole("alert", {
        name: "",
      }),
    ).toHaveTextContent("The deterministic quota trace could not be loaded.");
    expect(quotaRequests).toEqual(["sdk_stub"]);
    expect(screen.getByText("SDK QA workspace")).toBeVisible();
    expect(screen.getByText("Quota proof unavailable")).toBeVisible();
    expect(screen.getByText("No completed quota receipt is being shown.")).toBeVisible();
    expect(screen.queryByText("Completed fixture")).not.toBeInTheDocument();
    expect(screen.queryByText("Replay completed")).not.toBeInTheDocument();
    expect(
      screen.queryByText("Recorded verification and permission revocation are sealed."),
    ).not.toBeInTheDocument();

    fireEvent.click(quotaButton);

    expect(await screen.findByText("Quota retry completed with server evidence.")).toBeVisible();
    expect(quotaRequests).toEqual(["sdk_stub", "sdk_stub"]);
    expect(screen.getByText("SDK QA workspace")).toBeVisible();
  });

  it("retries replay once after a stable live-admission error and discloses it", async () => {
    const requestModes: string[] = [];
    const safeExplanation =
      "Live recovery is currently at capacity. A replay fixture is starting automatically; you can rerun it explicitly.";
    const fetchMock = vi.fn().mockImplementation(
      (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/health") {
          return Promise.resolve(
            new Response(
              JSON.stringify({
                backend: "openai",
                liveReady: true,
                providerBoundary: "demo_adapter_only",
              }),
              { status: 200, headers: { "Content-Type": "application/json" } },
            ),
          );
        }
        if (url === "/api/recoveries") {
          const body = JSON.parse(String(init?.body)) as { executionMode: string };
          requestModes.push(body.executionMode);
          if (body.executionMode === "openai_live") {
            return Promise.resolve(
              new Response(
                JSON.stringify({
                  code: "live_capacity",
                  message: safeExplanation,
                  requestId: "0123456789abcdef0123456789abcdef",
                  fallbackExecutionMode: "replay_fixture",
                }),
                { status: 429, headers: { "Content-Type": "application/json" } },
              ),
            );
          }
          return Promise.resolve(
            new Response(
              JSON.stringify({
                recoveryId: "11111111-2222-4333-8444-555555555555",
                scenarioId: "hotel",
                executionMode: "replay_fixture",
                modelIds: [],
                rootTraceId: null,
                status: "in_progress",
                currentStep: 1,
                currentStepSummary: "Replay server snapshot loaded.",
                createdAt: "2026-07-19T12:00:00Z",
                updatedAt: "2026-07-19T12:00:00Z",
                pendingApproval: null,
              }),
              { status: 201, headers: { "Content-Type": "application/json" } },
            ),
          );
        }
        throw new Error(`Unexpected request: ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    expect(await screen.findByText(safeExplanation)).toBeVisible();
    const replayAction = screen.getByRole("button", { name: "Run replay fixture" });
    expect(replayAction).toBeVisible();
    expect((await screen.findAllByText("Replay server snapshot loaded."))[0]).toBeVisible();
    expect(requestModes).toEqual(["openai_live", "replay_fixture"]);
    expect(screen.getByText("Replay fixture")).toBeVisible();
    expect(screen.queryByText("GPT-5.6 agents")).not.toBeInTheDocument();

    fireEvent.click(replayAction);
    await waitFor(() =>
      expect(requestModes).toEqual(["openai_live", "replay_fixture", "replay_fixture"]),
    );
  });

  it("uses disclosed replay, never SDK stub, when health says live is unavailable", async () => {
    const requestModes: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/health") {
          return Promise.resolve(
            new Response(
              JSON.stringify({
                backend: "stub",
                liveReady: false,
                providerBoundary: "demo_adapter_only",
              }),
              { status: 200, headers: { "Content-Type": "application/json" } },
            ),
          );
        }
        if (url === "/api/recoveries") {
          requestModes.push(
            (JSON.parse(String(init?.body)) as { executionMode: string }).executionMode,
          );
          return Promise.resolve(
            new Response(
              JSON.stringify({
                recoveryId: "11111111-2222-4333-8444-555555555555",
                scenarioId: "hotel",
                executionMode: "replay_fixture",
                modelIds: [],
                rootTraceId: null,
                status: "in_progress",
                currentStep: 1,
                currentStepSummary: "Preflight replay loaded.",
                createdAt: "2026-07-19T12:00:00Z",
                updatedAt: "2026-07-19T12:00:00Z",
                pendingApproval: null,
              }),
              { status: 201, headers: { "Content-Type": "application/json" } },
            ),
          );
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    render(<App />);

    expect(
      await screen.findByText(
        "Live recovery is unavailable in this demo. A replay fixture is starting automatically; you can rerun it explicitly.",
      ),
    ).toBeVisible();
    expect((await screen.findAllByText("Preflight replay loaded."))[0]).toBeVisible();
    expect(requestModes).toEqual(["replay_fixture"]);
    expect(screen.queryByText("SDK stub")).not.toBeInTheDocument();
    expect(screen.queryByText("GPT-5.6 agents")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run replay fixture" })).toBeVisible();
  });

  it("does not offer or start replay for an exact-shaped admission envelope at status 500", async () => {
    const requestModes: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/health") {
          return Promise.resolve(
            new Response(
              JSON.stringify({
                backend: "openai",
                liveReady: true,
                providerBoundary: "demo_adapter_only",
              }),
              { status: 200, headers: { "Content-Type": "application/json" } },
            ),
          );
        }
        if (url === "/api/recoveries") {
          requestModes.push(
            (JSON.parse(String(init?.body)) as { executionMode: string }).executionMode,
          );
          return Promise.resolve(
            new Response(
              JSON.stringify({
                code: "live_capacity",
                message:
                  "Live recovery is currently at capacity. A replay fixture is starting automatically; you can rerun it explicitly.",
                requestId: "0123456789abcdef0123456789abcdef",
                fallbackExecutionMode: "replay_fixture",
              }),
              { status: 500, headers: { "Content-Type": "application/json" } },
            ),
          );
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    render(<App />);

    await waitFor(() => expect(requestModes).toEqual(["openai_live"]));
    expect(screen.queryByRole("button", { name: "Run replay fixture" })).not.toBeInTheDocument();
    expect(screen.queryByText("GPT-5.6 agents")).not.toBeInTheDocument();
  });

  it("renders GPT-5.6 agents only for a verified live backend and live snapshot", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request) => {
        const url = String(input);
        if (url === "/health") {
          return Promise.resolve(
            new Response(
              JSON.stringify({
                backend: "openai",
                liveReady: true,
                providerBoundary: "demo_adapter_only",
              }),
              { status: 200, headers: { "Content-Type": "application/json" } },
            ),
          );
        }
        if (url === "/api/recoveries") {
          return Promise.resolve(
            new Response(
              JSON.stringify({
                recoveryId: "11111111-2222-4333-8444-555555555555",
                scenarioId: "hotel",
                executionMode: "openai_live",
                modelIds: ["gpt-5.6-luna", "gpt-5.6-terra"],
                rootTraceId: "trace_0123456789abcdef0123456789abcdef",
                status: "in_progress",
                currentStep: 0,
                currentStepSummary: "Live recovery started.",
                createdAt: "2026-07-19T12:00:00Z",
                updatedAt: "2026-07-19T12:00:00Z",
                pendingApproval: null,
              }),
              { status: 201, headers: { "Content-Type": "application/json" } },
            ),
          );
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    render(<App />);

    expect(await screen.findByText("GPT-5.6 agents")).toBeVisible();
    expect(screen.queryByText(/12 providers reachable/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/91% less context/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/18 min human support/i)).not.toBeInTheDocument();
  });

  it.each([
    { backend: "stub", liveReady: true, executionMode: "openai_live" },
    { backend: "openai", liveReady: false, executionMode: "openai_live" },
    { backend: "openai", liveReady: true, executionMode: "sdk_stub" },
  ] as const)(
    "does not render GPT-5.6 agents for $backend/$liveReady/$executionMode mismatch",
    async ({ backend, liveReady, executionMode }) => {
      vi.stubGlobal(
        "fetch",
        vi.fn().mockImplementation((input: string | URL | Request) => {
          const url = String(input);
          if (url === "/health") {
            return Promise.resolve(
              new Response(
                JSON.stringify({
                  backend,
                  liveReady,
                  providerBoundary: "demo_adapter_only",
                }),
                { status: 200, headers: { "Content-Type": "application/json" } },
              ),
            );
          }
          if (url === "/api/recoveries") {
            return Promise.resolve(
              new Response(
                JSON.stringify({
                  recoveryId: "11111111-2222-4333-8444-555555555555",
                  scenarioId: "hotel",
                  executionMode,
                  modelIds:
                    executionMode === "openai_live"
                      ? ["gpt-5.6-luna", "gpt-5.6-terra"]
                      : [],
                  rootTraceId:
                    executionMode === "openai_live"
                      ? "trace_0123456789abcdef0123456789abcdef"
                      : "qa_trace_0123456789abcdef0123456789abcdef",
                  status: "in_progress",
                  currentStep: 0,
                  currentStepSummary: "Recovery started.",
                  createdAt: "2026-07-19T12:00:00Z",
                  updatedAt: "2026-07-19T12:00:00Z",
                  pendingApproval: null,
                }),
                { status: 201, headers: { "Content-Type": "application/json" } },
              ),
            );
          }
          throw new Error(`Unexpected request: ${url}`);
        }),
      );

      render(<App />);

      expect(await screen.findByText(executionMode === "sdk_stub" ? "SDK stub" : "OpenAI live"))
        .toBeVisible();
      expect(screen.queryByText("GPT-5.6 agents")).not.toBeInTheDocument();
    },
  );

  it(
    "loads the interactive hotel consent from a real server-shaped SDK snapshot",
    async () => {
      const digest = `sha256:${"a".repeat(64)}`;
      vi.stubGlobal(
        "fetch",
        vi.fn().mockImplementation((input: string | URL | Request) => {
          const url = String(input);
          if (url === "/health") {
            return Promise.resolve(
              new Response(
                JSON.stringify({
                  backend: "stub",
                  liveReady: false,
                  providerBoundary: "demo_adapter_only",
                }),
                { status: 200, headers: { "Content-Type": "application/json" } },
              ),
            );
          }
          if (url === "/api/recoveries") {
            return Promise.resolve(
              new Response(
                JSON.stringify({
                  recoveryId: "11111111-2222-4333-8444-555555555555",
                  scenarioId: "hotel",
                  executionMode: "sdk_stub",
                  modelIds: [],
                  rootTraceId: "qa_trace_0123456789abcdef0123456789abcdef",
                  status: "pending_approval",
                  currentStep: 3,
                  currentStepSummary: "Server pause loaded.",
                  createdAt: "2026-07-18T20:00:00Z",
                  updatedAt: "2026-07-18T20:00:01Z",
                  pendingApproval: {
                    remedyId: "server-remedy",
                    remedyDigest: digest,
                    terms: {
                      bookingId: "server-booking",
                      action: "replace_room",
                      replacement: { fromRoomType: "double", toRoomType: "king" },
                      stay: { checkIn: "2026-08-14", checkOut: "2026-08-16" },
                      currency: "USD",
                    },
                    costDeltaMinor: 0,
                    changedFields: ["room_type"],
                    providerCommitments: [
                      "No additional charge",
                      "Preserve booking dates",
                    ],
                    expiry: "2026-08-01T18:45:30Z",
                    hardConstraintSatisfied: true,
                    delegatedAuthoritySatisfied: true,
                    toolCallId: "server-call",
                    executionStarted: false,
                  },
                }),
                { status: 201, headers: { "Content-Type": "application/json" } },
              ),
            );
          }
          throw new Error(`Unexpected request: ${url}`);
        }),
      );

      render(<App />);

      expect(
        await screen.findByRole("heading", { name: "Decide exact remedy" }),
      ).toBeVisible();
      expect(await screen.findByText("SDK stub")).toBeVisible();
      expect(screen.getByText("server-booking")).toBeVisible();
      expect(screen.queryByText("hotel-consent-v1")).not.toBeInTheDocument();
    },
    10_000,
  );

  it("renders exactly two approved scenarios and the ordered lifecycle", async () => {
    stubHealthWithUnavailableRecovery();

    render(<App />);

    const scenarioList = screen.getByRole("list", { name: "Recovery scenarios" });
    expect(within(scenarioList).getAllByRole("listitem")).toHaveLength(2);
    expect(
      within(scenarioList).getByRole("button", { name: /Hotel booking recovery/i }),
    ).toBeInTheDocument();
    expect(
      within(scenarioList).getByRole("button", { name: /API quota recovery/i }),
    ).toBeInTheDocument();

    const lifecycle = screen.getByRole("list", { name: "Recovery lifecycle" });
    expect(within(lifecycle).getAllByRole("listitem")).toHaveLength(6);
    expect(within(lifecycle).getAllByRole("listitem").map((item) => item.textContent)).toEqual([
      expect.stringContaining("Detect"),
      expect.stringContaining("Prove"),
      expect.stringContaining("Negotiate"),
      expect.stringContaining("Authorize"),
      expect.stringContaining("Execute"),
      expect.stringContaining("Verify & seal"),
    ]);

    expect(await screen.findByText("Replay fixture")).toBeInTheDocument();
    expect(screen.queryByText(/GPT-5\.6 agents/i)).not.toBeInTheDocument();
  });

  it("marks every lifecycle step recorded for an authoritative completed quota", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/health") {
          return Promise.resolve(
            new Response(
              JSON.stringify({
                backend: "stub",
                liveReady: false,
                providerBoundary: "demo_adapter_only",
              }),
              { status: 200, headers: { "Content-Type": "application/json" } },
            ),
          );
        }
        if (url === "/api/recoveries") {
          const request = JSON.parse(String(init?.body)) as { scenarioId: string };
          if (request.scenarioId === "hotel") {
            return Promise.resolve(new Response(null, { status: 503 }));
          }
          return Promise.resolve(
            new Response(
              JSON.stringify({
                recoveryId: "22222222-2222-4222-8222-222222222222",
                scenarioId: "api-quota",
                executionMode: "sdk_stub",
                modelIds: [],
                rootTraceId: "qa_trace_0123456789abcdef0123456789abcdef",
                status: "completed",
                currentStep: 5,
                currentStepSummary: "Authoritative quota receipt sealed.",
                createdAt: "2026-07-19T12:00:00Z",
                updatedAt: "2026-07-19T12:00:01Z",
                pendingApproval: null,
              }),
              { status: 201, headers: { "Content-Type": "application/json" } },
            ),
          );
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );
    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: /API quota recovery/i }));

    expect(await screen.findByText("Authoritative quota receipt sealed.")).toBeVisible();
    const lifecycle = screen.getByRole("list", { name: "Recovery lifecycle" });
    expect(within(lifecycle).getAllByText("Recorded")).toHaveLength(6);
  });

  it.each([
    {
      status: "completed",
      label: "Completed",
      authorization: "Exact remedy approval was accepted by the server.",
      execution: "Approved provider dispatch completed.",
      verification: "Completed server evidence sealed.",
    },
    {
      status: "closed_without_action",
      label: "Closed without action",
      authorization: "The exact remedy was declined and its permission was revoked.",
      execution: "Provider dispatch did not begin.",
      verification: "Closed without action server evidence sealed.",
    },
    {
      status: "outcome_unknown",
      label: "Outcome unknown",
      authorization: "The decline was recorded after dispatch may have begun.",
      execution: "Provider dispatch may have begun; its outcome is unknown.",
      verification: "Outcome unknown server evidence sealed.",
    },
  ] as const)("renders $status as a truthful terminal server outcome", async ({
    status,
    label,
    authorization,
    execution,
    verification,
  }) => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request) => {
        const url = String(input);
        if (url === "/health") {
          return Promise.resolve(
            new Response(
              JSON.stringify({
                backend: "stub",
                liveReady: false,
                providerBoundary: "demo_adapter_only",
              }),
              { status: 200, headers: { "Content-Type": "application/json" } },
            ),
          );
        }
        if (url === "/api/recoveries") {
          return Promise.resolve(
            new Response(
              JSON.stringify({
                recoveryId: "11111111-2222-4333-8444-555555555555",
                scenarioId: "hotel",
                executionMode: "sdk_stub",
                modelIds: [],
                rootTraceId: "qa_trace_0123456789abcdef0123456789abcdef",
                status,
                currentStep: 5,
                currentStepSummary: `${label} server evidence sealed.`,
                createdAt: "2026-07-18T20:00:00Z",
                updatedAt: "2026-07-18T20:00:01Z",
                pendingApproval: null,
              }),
              { status: 201, headers: { "Content-Type": "application/json" } },
            ),
          );
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    render(<App />);

    expect(await screen.findByText(label)).toBeVisible();
    expect(screen.getByText(status)).toBeVisible();
    const lifecycle = screen.getByRole("list", { name: "Recovery lifecycle" });
    expect(within(lifecycle).getAllByText("Recorded")).toHaveLength(6);
    expect(within(lifecycle).getByText(authorization)).toBeVisible();
    expect(within(lifecycle).getByText(execution)).toBeVisible();
    expect(within(lifecycle).getByText(verification)).toBeVisible();
    expect(within(lifecycle).queryByText("Not started.")).not.toBeInTheDocument();
    expect(
      within(lifecycle).queryByText("Waiting for an execution outcome."),
    ).not.toBeInTheDocument();
  });
});
