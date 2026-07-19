import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
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

  it("marks every lifecycle step recorded for a completed scenario", () => {
    stubHealthWithUnavailableRecovery();
    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: /API quota recovery/i }));

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
