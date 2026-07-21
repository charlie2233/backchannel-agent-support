import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import { LIVE_ADMISSION_MESSAGES } from "./api/client";

function healthResponse(backend: "openai" | "stub", liveReady: boolean): Response {
  return new Response(
    JSON.stringify({ backend, liveReady, providerBoundary: "demo_adapter_only" }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );
}

afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("runtime-health recovery integration", () => {
  it("wires exhausted manual recovery back to live-ready without starting a run", async () => {
    vi.useFakeTimers();
    let healthAttempts = 0;
    let resolveManualHealth: ((response: Response) => void) | undefined;
    const recoveryModes: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/health") {
          healthAttempts += 1;
          if (healthAttempts <= 3) return Promise.reject(new Error("offline"));
          return new Promise<Response>((resolve) => {
            resolveManualHealth = resolve;
          });
        }
        if (url === "/api/recoveries") {
          recoveryModes.push(
            (JSON.parse(String(init?.body)) as { executionMode: string }).executionMode,
          );
          return Promise.reject(new Error("must remain idle"));
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    render(<App />);
    await act(async () => vi.advanceTimersByTimeAsync(1_500));
    const retry = screen.getByRole("button", { name: "Retry runtime check" });
    expect(retry).toBeVisible();

    fireEvent.click(retry);
    expect(screen.getByText("Retrying runtime check")).toBeVisible();
    expect(healthAttempts).toBe(4);
    expect(recoveryModes).toEqual([]);

    await act(async () => resolveManualHealth?.(healthResponse("openai", true)));
    expect(screen.getByRole("button", { name: "Start live recovery" })).toBeVisible();
    expect(recoveryModes).toEqual([]);
    expect(screen.queryByText("OpenAI live")).not.toBeInTheDocument();
    expect(screen.queryByText("GPT-5.6 agents")).not.toBeInTheDocument();
  });

  it("reveals an explicit live action after recovery but never auto-starts openai_live", async () => {
    vi.useFakeTimers();
    const recoveryModes: string[] = [];
    let healthAttempts = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/health") {
          healthAttempts += 1;
          if (healthAttempts < 3) return Promise.reject(new Error("temporary outage"));
          return Promise.resolve(healthResponse("openai", true));
        }
        if (url === "/api/recoveries") {
          recoveryModes.push(
            (JSON.parse(String(init?.body)) as { executionMode: string }).executionMode,
          );
          return Promise.reject(new Error("must remain idle"));
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    render(<App />);
    await act(async () => vi.advanceTimersByTimeAsync(1_500));

    expect(screen.getByRole("button", { name: "Start live recovery" })).toBeVisible();
    expect(healthAttempts).toBe(3);
    expect(recoveryModes).toEqual([]);
    expect(screen.queryByText("OpenAI live")).not.toBeInTheDocument();
    expect(screen.queryByText("GPT-5.6 agents")).not.toBeInTheDocument();
  });

  it("starts exactly one disclosed replay fixture after recovered keyless health", async () => {
    vi.useFakeTimers();
    const recoveryModes: string[] = [];
    let healthAttempts = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/health") {
          healthAttempts += 1;
          if (healthAttempts === 1) return Promise.reject(new Error("temporary outage"));
          return Promise.resolve(healthResponse("stub", false));
        }
        if (url === "/api/recoveries") {
          const mode = (JSON.parse(String(init?.body)) as { executionMode: string }).executionMode;
          recoveryModes.push(mode);
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
                currentStepSummary: "Recovered keyless replay loaded.",
                createdAt: "2026-07-20T12:00:00Z",
                updatedAt: "2026-07-20T12:00:01Z",
                pendingApproval: null,
                claimedDecision: null,
              }),
              { status: 201, headers: { "Content-Type": "application/json" } },
            ),
          );
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    render(<App />);
    await act(async () => vi.advanceTimersByTimeAsync(500));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(recoveryModes).toEqual(["replay_fixture"]);
    expect(screen.getByText(LIVE_ADMISSION_MESSAGES.live_unavailable)).toBeVisible();
    expect(screen.getAllByText("Recovered keyless replay loaded.")[0]).toBeVisible();
    expect(recoveryModes).not.toContain("openai_live");
  });
});
