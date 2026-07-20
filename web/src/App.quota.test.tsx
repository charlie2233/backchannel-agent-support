import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";

const hotelRecoveryId = "11111111-2222-4333-8444-555555555555";
const quotaRecoveryId = "99999999-2222-4333-8444-555555555555";
const retryQuotaRecoveryId = "88888888-2222-4333-8444-555555555555";

class MockEventSource {
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  close = vi.fn();
}

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function health(sdkStubReady: boolean): Response {
  return response({
    backend: "stub",
    liveReady: false,
    sdkStubReady,
    providerBoundary: "demo_adapter_only",
  });
}

function snapshot(
  scenarioId: "hotel" | "api-quota",
  executionMode: "sdk_stub" | "replay_fixture",
  recoveryId: string,
) {
  const quotaSdk = scenarioId === "api-quota" && executionMode === "sdk_stub";
  return {
    recoveryId,
    scenarioId,
    executionMode,
    status: "completed",
    currentStep: 5,
    currentStepSummary: "Authoritative terminal recovery.",
    createdAt: "2026-07-19T10:00:00Z",
    updatedAt: "2026-07-19T10:00:01Z",
    pendingApproval: null,
    rootTraceId: null,
    modelIds: [],
    sdkVersion: quotaSdk ? "0.18.3" : null,
    protocolVersion: quotaSdk ? "backchannel.quota.v1" : null,
    agentGraphVersion: quotaSdk ? "backchannel.quota-agent.v1" : null,
    promptToolSchemaHash: quotaSdk ? "c".repeat(64) : null,
  };
}

function quotaEvidence(source: "sdk_simulator" | "recorded_fixture") {
  return {
    providerCeilingRpm: 1000,
    recordedDemandRpm: 1200,
    temporaryBurstRpm: 1500,
    region: "US",
    durationSeconds: 900,
    extraCostMinor: 250,
    delegatedAuthorityMaxMinor: 500,
    currency: "USD",
    hardConstraints: {
      regionPreserved: true,
      burstCoversDemand: true,
      durationWithinLimit: true,
      baseQuotaUnchanged: true,
    },
    humanInterruptions: 0,
    approvals: 0,
    providerProofVerified: true,
    grantVerified: true,
    source,
    revocationEvidenceKind:
      source === "sdk_simulator"
        ? "runtime_permission_revoked"
        : "recorded_revocation_only",
    protocolSteps: [
      "Detect",
      "Prove",
      "Negotiate",
      "Authorize",
      "Execute",
      "Verify & seal",
    ],
  };
}

function receipt(
  scenarioId: "hotel" | "api-quota",
  executionMode: "sdk_stub" | "replay_fixture",
  recoveryId: string,
) {
  const quota = scenarioId === "api-quota";
  const runtime = quota && executionMode === "sdk_stub";
  return {
    recoveryId,
    executionMode,
    status: "completed",
    simulated: true,
    providerExecution: runtime,
    modelIds: [],
    rootTraceId: null,
    sdkVersion: runtime ? "0.18.3" : null,
    protocolVersion: runtime ? "backchannel.quota.v1" : null,
    agentGraphVersion: runtime ? "backchannel.quota-agent.v1" : null,
    promptToolSchemaHash: runtime ? "c".repeat(64) : null,
    boundary: "Authoritative test boundary.",
    providerResult: "Authoritative test result.",
    authorizationSource: "Authoritative test source.",
    verificationResults: ["Authoritative test verification."],
    decision: null,
    decisionRemedyDigest: null,
    executionCount: runtime ? 1 : 0,
    providerDispatchStarted: runtime,
    exactInterruptionRejected: false,
    permissionRevoked: runtime,
    scopeClosed: runtime,
    approvedRemedyDigest: null,
    quotaEvidence: quota
      ? quotaEvidence(runtime ? "sdk_simulator" : "recorded_fixture")
      : null,
  };
}

function selectQuota() {
  const title = screen.getByText("API quota recovery", { selector: "strong" });
  const button = title.closest("button");
  if (button === null) {
    throw new Error("API quota scenario button is missing");
  }
  fireEvent.click(button);
}

beforeEach(() => {
  sessionStorage.clear();
  vi.stubGlobal("EventSource", MockEventSource);
});

afterEach(() => {
  cleanup();
  sessionStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("explicit API quota runs", () => {
  it("keeps replay explicit and hides unavailable SDK/live/consent controls", async () => {
    const bodies: Array<Record<string, unknown>> = [];
    const fetchMock = vi.fn().mockImplementation(
      (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/health") {
          return Promise.resolve(health(false));
        }
        if (url === "/api/recoveries") {
          const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
          bodies.push(body);
          return Promise.resolve(
            response(snapshot("api-quota", "replay_fixture", quotaRecoveryId), 201),
          );
        }
        if (url === `/api/recoveries/${quotaRecoveryId}/receipt`) {
          return Promise.resolve(
            response(receipt("api-quota", "replay_fixture", quotaRecoveryId)),
          );
        }
        throw new Error(`Unexpected request: ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);
    await screen.findByText("Live mode is unavailable on this server.");
    selectQuota();

    expect(bodies).toHaveLength(0);
    expect(screen.queryByText("Completed fixture")).not.toBeInTheDocument();
    expect(screen.queryByText("api-quota-completed-v1")).not.toBeInTheDocument();
    expect(screen.queryAllByText("Recorded")).toHaveLength(0);
    expect(
      screen.getByText("No authoritative recovery evidence is available."),
    ).toBeVisible();
    const controls = screen.getByLabelText("Quota execution controls");
    expect(
      within(controls).queryByRole("button", { name: "Run SDK stub trace" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("Run live recovery")).not.toBeInTheDocument();
    expect(screen.queryByText("Approve remedy")).not.toBeInTheDocument();
    expect(screen.queryByText("Decline")).not.toBeInTheDocument();
    const replay = within(controls).getByRole("button", {
      name: "Replay recorded trace",
    });
    fireEvent.click(replay);
    fireEvent.click(replay);

    await waitFor(() => expect(bodies).toEqual([
      { scenarioId: "api-quota", executionMode: "replay_fixture" },
    ]));
    expect(
      await screen.findByRole("heading", { name: "Recorded quota recovery" }),
    ).toBeVisible();
    const lifecycle = document.querySelector<HTMLOListElement>(
      'ol[aria-label="Recovery lifecycle"]',
    );
    if (lifecycle === null) {
      throw new Error("Authoritative quota lifecycle is missing");
    }
    expect(
      Array.from(lifecycle.querySelectorAll("li"), (item) => item.textContent),
    ).toEqual([
      expect.stringContaining("Detect"),
      expect.stringContaining("Prove"),
      expect.stringContaining("Negotiate"),
      expect.stringContaining("Authorize"),
      expect.stringContaining("Execute"),
      expect.stringContaining("Verify & seal"),
    ]);
    expect(within(lifecycle).getAllByText("Recorded")).toHaveLength(6);
  });

  it("shows the local SDK action and renders its authoritative receipt", async () => {
    sessionStorage.setItem("backchannel.hotelRecovery.v1", hotelRecoveryId);
    const bodies: Array<Record<string, unknown>> = [];
    const fetchMock = vi.fn().mockImplementation(
      (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/health") {
          return Promise.resolve(health(true));
        }
        if (url === `/api/recoveries/${hotelRecoveryId}`) {
          return Promise.resolve(
            response(snapshot("hotel", "replay_fixture", hotelRecoveryId)),
          );
        }
        if (url === `/api/recoveries/${hotelRecoveryId}/receipt`) {
          return Promise.resolve(
            response(receipt("hotel", "replay_fixture", hotelRecoveryId)),
          );
        }
        if (url === "/api/recoveries") {
          const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
          bodies.push(body);
          return Promise.resolve(
            response(snapshot("api-quota", "sdk_stub", quotaRecoveryId), 201),
          );
        }
        if (url === `/api/recoveries/${quotaRecoveryId}/receipt`) {
          return Promise.resolve(
            response(receipt("api-quota", "sdk_stub", quotaRecoveryId)),
          );
        }
        throw new Error(`Unexpected request: ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);
    await screen.findByRole("heading", { name: "Completed replay receipt" });
    selectQuota();

    expect(bodies).toHaveLength(0);
    const sdk = screen.getByRole("button", { name: "Run SDK stub trace" });
    expect(screen.getByRole("button", { name: "Replay recorded trace" })).toBeVisible();
    fireEvent.click(sdk);
    fireEvent.click(sdk);

    await waitFor(() => expect(bodies).toEqual([
      { scenarioId: "api-quota", executionMode: "sdk_stub" },
    ]));
    expect(
      await screen.findByRole("heading", { name: "Verified quota recovery" }),
    ).toBeVisible();
    expect(screen.getByText(quotaRecoveryId)).toBeVisible();
    expect(screen.queryByRole("button", { name: /approve/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /decline/i })).not.toBeInTheDocument();
  });

  it("surfaces a failed quota receipt and permits an explicit fresh retry", async () => {
    const bodies: Array<Record<string, unknown>> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(
        (input: string | URL | Request, init?: RequestInit) => {
          const url = String(input);
          if (url === "/health") {
            return Promise.resolve(health(false));
          }
          if (url === "/api/recoveries") {
            const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
            bodies.push(body);
            const recoveryId =
              bodies.length === 1 ? quotaRecoveryId : retryQuotaRecoveryId;
            return Promise.resolve(
              response(snapshot("api-quota", "replay_fixture", recoveryId), 201),
            );
          }
          if (url === `/api/recoveries/${quotaRecoveryId}/receipt`) {
            return Promise.resolve(response({ invalid: "receipt" }));
          }
          if (url === `/api/recoveries/${retryQuotaRecoveryId}/receipt`) {
            return Promise.resolve(
              response(
                receipt(
                  "api-quota",
                  "replay_fixture",
                  retryQuotaRecoveryId,
                ),
              ),
            );
          }
          throw new Error(`Unexpected request: ${url}`);
        },
      ),
    );

    render(<App />);
    await screen.findByText("Live mode is unavailable on this server.");
    selectQuota();
    fireEvent.click(screen.getByRole("button", { name: "Replay recorded trace" }));

    expect(
      await screen.findByText("The server returned an unexpected response."),
    ).toBeVisible();
    const retry = screen.getByRole("button", { name: "Replay recorded trace" });
    expect(retry).toBeEnabled();
    fireEvent.click(retry);

    expect(
      await screen.findByRole("heading", { name: "Recorded quota recovery" }),
    ).toBeVisible();
    expect(screen.getByText(retryQuotaRecoveryId)).toBeVisible();
    expect(bodies).toEqual([
      { scenarioId: "api-quota", executionMode: "replay_fixture" },
      { scenarioId: "api-quota", executionMode: "replay_fixture" },
    ]);
  });

  it("rejects a nonterminal quota creation without completed presentation", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(
        (input: string | URL | Request) => {
          const url = String(input);
          if (url === "/health") {
            return Promise.resolve(health(false));
          }
          if (url === "/api/recoveries") {
            return Promise.resolve(
              response(
                {
                  ...snapshot(
                    "api-quota",
                    "replay_fixture",
                    quotaRecoveryId,
                  ),
                  status: "in_progress",
                  currentStep: 0,
                  currentStepSummary: "Quota trace has not completed.",
                },
                201,
              ),
            );
          }
          throw new Error(`Unexpected request: ${url}`);
        },
      ),
    );

    render(<App />);
    await screen.findByText("Live mode is unavailable on this server.");
    selectQuota();
    fireEvent.click(screen.getByRole("button", { name: "Replay recorded trace" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Quota trace could not be started.",
    );
    expect(
      screen.queryByRole("list", { name: "Recovery lifecycle" }),
    ).not.toBeInTheDocument();
    expect(screen.queryAllByText("Recorded")).toHaveLength(0);
    expect(screen.queryByText("Completed fixture")).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Replay recorded trace" }),
    ).toBeEnabled();
  });

  it("does not replace the selected hotel view with a delayed quota result", async () => {
    let resolveQuota: ((value: Response) => void) | undefined;
    const quotaResult = new Promise<Response>((resolve) => {
      resolveQuota = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(
        (input: string | URL | Request) => {
          const url = String(input);
          if (url === "/health") {
            return Promise.resolve(health(false));
          }
          if (url === "/api/recoveries") {
            return quotaResult;
          }
          throw new Error(`Unexpected request: ${url}`);
        },
      ),
    );

    render(<App />);
    await screen.findByText("Live mode is unavailable on this server.");
    selectQuota();
    fireEvent.click(screen.getByRole("button", { name: "Replay recorded trace" }));
    fireEvent.click(screen.getByRole("button", { name: /Hotel booking recovery/i }));
    resolveQuota?.(
      response(snapshot("api-quota", "replay_fixture", quotaRecoveryId), 201),
    );

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Hotel booking recovery" })).toBeVisible();
    });
    expect(screen.queryByRole("heading", { name: "Recorded quota recovery" })).not.toBeInTheDocument();
  });
});
