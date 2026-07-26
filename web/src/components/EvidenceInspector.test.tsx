import { StrictMode, Suspense, startTransition, useLayoutEffect, useState } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DECISION_CAPACITY_MESSAGE } from "../api/client";
import type { RecoveryReceipt, RecoverySnapshot } from "../domain/recovery";
import { recoveryScenarios } from "../fixtures/recoveries";
import { DECISION_REQUEST_TIMEOUT_MS, EvidenceInspector } from "./EvidenceInspector";

const fullDigest = `sha256:${"0123456789abcdef".repeat(4)}` as `sha256:${string}`;

function pendingSnapshot(): RecoverySnapshot {
  return {
    recoveryId: "11111111-2222-4333-8444-555555555555",
    scenarioId: "hotel",
    executionMode: "sdk_stub",
    modelIds: [],
    rootTraceId: "qa_trace_0123456789abcdef0123456789abcdef",
    status: "pending_approval",
    currentStep: 3,
    currentStepSummary: "Server paused at exact consent.",
    createdAt: "2026-07-18T20:00:00Z",
    updatedAt: "2026-07-18T20:00:01Z",
    claimedDecision: null,
    pendingApproval: {
      remedyId: "remedy-server-742",
      remedyDigest: fullDigest,
      terms: {
        bookingId: "booking-server-742",
        action: "replace_room",
        replacement: {
          fromRoomType: "double",
          toRoomType: "ocean-view king",
        },
        stay: {
          checkIn: "2026-09-04",
          checkOut: "2026-09-07",
        },
        currency: "USD",
      },
      costDeltaMinor: 1250,
      changedFields: ["guest_note", "room_type"],
      providerCommitments: ["No additional fees", "Preserve booking dates"],
      expiry: "2026-09-01T18:45:30Z",
      hardConstraintSatisfied: true,
      delegatedAuthoritySatisfied: true,
      toolCallId: "call-server-742",
      executionStarted: false,
    },
  };
}

function claimedSnapshot(
  action: "approve" | "decline" = "approve",
  expiry = "2026-09-01T18:45:30Z",
): RecoverySnapshot {
  return {
    ...pendingSnapshot(),
    currentStepSummary: `Exact ${action} claimed; outcome pending.`,
    pendingApproval: null,
    claimedDecision: {
      action,
      remedyDigest: fullDigest,
      expiry,
    },
  } as RecoverySnapshot;
}

function ActivateDecisionInLayoutEffect({ buttonName }: { buttonName: string }) {
  useLayoutEffect(() => {
    screen.getByRole("button", { name: buttonName }).click();
  }, [buttonName]);
  return null;
}

const suspendedForever = new Promise<never>(() => {});

function AlwaysSuspend(): never {
  throw suspendedForever;
}

beforeEach(() => {
  Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
    configurable: true,
    value: vi.fn(function showModal(this: HTMLDialogElement) {
      this.open = true;
    }),
  });
  Object.defineProperty(HTMLDialogElement.prototype, "close", {
    configurable: true,
    value: vi.fn(function close(this: HTMLDialogElement) {
      this.open = false;
      this.dispatchEvent(new Event("close"));
    }),
  });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it.each(["hardConstraintSatisfied", "delegatedAuthoritySatisfied"] as const)(
  "does not render public decision actions when %s is false",
  (field) => {
    const validApproval = pendingSnapshot().pendingApproval!;
    const snapshot = {
      ...pendingSnapshot(),
      pendingApproval: {
        ...validApproval,
        [field]: false,
      },
    } as unknown as RecoverySnapshot;

    render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={snapshot}
      />,
    );

    expect(screen.queryByRole("button", { name: "Approve remedy" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Decline" })).not.toBeInTheDocument();
    expect(screen.getByText("Policy-ineligible consent is unavailable.")).toBeInTheDocument();
  },
);

it("renders no decision or resume action when the server suppresses a tampered claim", () => {
  render(
    <EvidenceInspector
      scenario={recoveryScenarios[0]}
      snapshot={{
        ...claimedSnapshot(),
        pendingApproval: null,
        claimedDecision: null,
      }}
    />,
  );

  expect(screen.queryByRole("button", { name: "Approve remedy" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Decline" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Resume exact/ })).not.toBeInTheDocument();
});

describe("EvidenceInspector exact consent", () => {
  it.each([
    ["approve", "Resume exact approval", "approval"],
    ["decline", "Resume exact decline", "decline"],
  ] as const)(
    "renders only the server-authored %s resume action and evidence",
    (action, buttonName, oppositeWord) => {
      render(
        <EvidenceInspector
          scenario={recoveryScenarios[0]}
          snapshot={claimedSnapshot(action)}
        />,
      );

      expect(screen.getByText(`Exact ${action} claimed; outcome pending.`)).toBeVisible();
      expect(screen.getByText("sha256:0123456789ab…89abcdef")).toBeVisible();
      expect(screen.getByText("2026-09-01 18:45:30 UTC")).toBeVisible();
      expect(screen.getByRole("button", { name: buttonName })).toBeEnabled();
      expect(
        screen.queryByRole("button", {
          name: oppositeWord === "approval" ? "Resume exact decline" : "Resume exact approval",
        }),
      ).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Approve remedy" })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Decline" })).not.toBeInTheDocument();
    },
  );

  it("never resumes on mount, reload rendering, or StrictMode effects", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const { rerender } = render(
      <StrictMode>
        <EvidenceInspector
          scenario={recoveryScenarios[0]}
          snapshot={claimedSnapshot()}
        />
      </StrictMode>,
    );

    rerender(
      <StrictMode>
        <EvidenceInspector
          scenario={recoveryScenarios[0]}
          snapshot={claimedSnapshot()}
        />
      </StrictMode>,
    );

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("sends one exact empty resume request under rapid clicks", async () => {
    let resolveResume: ((response: Response) => void) | undefined;
    const fetchMock = vi.fn().mockImplementation(
      () => new Promise<Response>((resolve) => { resolveResume = resolve; }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const onServerSuccess = vi.fn().mockResolvedValue(undefined);
    render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={claimedSnapshot()}
        onServerSuccess={onServerSuccess}
      />,
    );
    const button = screen.getByRole("button", { name: "Resume exact approval" });

    fireEvent.click(button);
    fireEvent.click(button);
    fireEvent.click(button);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/recoveries/11111111-2222-4333-8444-555555555555/decisions/resume",
      expect.objectContaining({ method: "POST", body: "{}" }),
    );

    resolveResume?.(
      new Response(
        JSON.stringify({
          action: "approve",
          recoveryId: "11111111-2222-4333-8444-555555555555",
          remedyDigest: fullDigest,
          status: "completed",
          approvedRemedyDigest: fullDigest,
          executionStarted: true,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    await waitFor(() => expect(onServerSuccess).toHaveBeenCalledTimes(1));
  });

  it("keeps a successful claimed resume latched before the accepted render commits", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          action: "approve",
          recoveryId: "11111111-2222-4333-8444-555555555555",
          remedyDigest: fullDigest,
          status: "completed",
          approvedRemedyDigest: fullDigest,
          executionStarted: true,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    let capturedButton: HTMLButtonElement | null = null;
    const onServerSuccess = vi.fn(() => {
      if (capturedButton === null) return;
      capturedButton.disabled = false;
      fireEvent.click(capturedButton);
    });
    render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={claimedSnapshot()}
        onServerSuccess={onServerSuccess}
      />,
    );
    const button = screen.getByRole<HTMLButtonElement>("button", {
      name: "Resume exact approval",
    });
    const observedActivations = vi.fn();
    button.addEventListener("click", observedActivations);
    capturedButton = button;

    fireEvent.click(button);

    await waitFor(() => expect(onServerSuccess).toHaveBeenCalledTimes(1));
    expect(observedActivations).toHaveBeenCalledTimes(2);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("retains and retries the exact resume affordance after network failure", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new Error("offline"));
    vi.stubGlobal("fetch", fetchMock);
    render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={claimedSnapshot("decline")}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Resume exact decline" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Exact decline could not be resumed",
    );
    expect(screen.getByRole("button", { name: "Resume exact decline" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Resume exact approval" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Resume exact decline" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  });

  it("refreshes authoritative evidence instead of retrying an expired resume", async () => {
    const recoveryId = "11111111-2222-4333-8444-555555555555";
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          detail: {
            code: "remedy_expired",
            recoveryId,
          },
        }),
        { status: 422, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const onServerSuccess = vi.fn().mockResolvedValue(undefined);
    render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={claimedSnapshot("approve")}
        onServerSuccess={onServerSuccess}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Resume exact approval" }));

    await waitFor(() => expect(onServerSuccess).toHaveBeenCalledTimes(1));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(
      screen.getByText("Consent expired. Refreshing authoritative recovery evidence."),
    ).toBeVisible();
    expect(screen.queryByText(/could not be resumed/i)).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Resume exact approval" }),
    ).toBeDisabled();
  });

  it("blocks a stale resume and refreshes once on an exact state conflict", async () => {
    const recoveryId = "11111111-2222-4333-8444-555555555555";
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          detail: {
            code: "decision_resume_unavailable",
            recoveryId,
          },
        }),
        { status: 409, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    let resolveRefresh: (() => void) | undefined;
    const onServerSuccess = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          resolveRefresh = resolve;
        }),
    );
    render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={claimedSnapshot("approve")}
        onServerSuccess={onServerSuccess}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Resume exact approval" }));

    await waitFor(() => expect(onServerSuccess).toHaveBeenCalledTimes(1));
    const resume = screen.getByRole("button", { name: "Resume exact approval" });
    expect(resume).toBeDisabled();
    expect(
      screen.getByText(
        "Decision state changed or could not be safely continued. Refreshing authoritative recovery evidence.",
      ),
    ).toBeVisible();
    expect(screen.queryByText(/could not be resumed/i)).not.toBeInTheDocument();
    fireEvent.click(resume);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(onServerSuccess).toHaveBeenCalledTimes(1);

    resolveRefresh?.();
  });

  it("disables an expired claimed decision and refreshes evidence without posting", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-01T18:45:31.000Z"));
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const onServerSuccess = vi.fn().mockResolvedValue(undefined);
    render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={claimedSnapshot("approve")}
        onServerSuccess={onServerSuccess}
      />,
    );
    await act(async () => { await Promise.resolve(); });

    const button = screen.getByRole("button", { name: "Resume exact approval" });
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(onServerSuccess).toHaveBeenCalledTimes(1);
  });

  it("refreshes an expired claimed approval to canonical unknown evidence without posting", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-01T18:45:31.000Z"));
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const refreshObserved = vi.fn();
    const terminalSnapshot: RecoverySnapshot = {
      ...claimedSnapshot("approve"),
      status: "outcome_unknown",
      currentStep: 5,
      currentStepSummary: "Claimed decision expired; provider outcome remains unknown.",
      claimedDecision: null,
    };
    const terminalReceipt: RecoveryReceipt = {
      recoveryId: terminalSnapshot.recoveryId,
      executionMode: "sdk_stub",
      status: "outcome_unknown",
      simulated: true,
      providerExecution: null,
      modelCall: false,
      modelIds: [],
      rootTraceId: "qa_trace_0123456789abcdef0123456789abcdef",
      sdkVersion: "0.18.3",
      protocolVersion: "backchannel.approval.v1",
      agentGraphVersion: "backchannel.hotel-agent.v1",
      definitionDigest: "b".repeat(64),
      boundary:
        "Deterministic Agents SDK model and demo hotel adapter only; no OpenAI model call, real booking, or payment change.",
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

    function ExpiredClaimRefreshHarness() {
      const [terminal, setTerminal] = useState(false);
      return (
        <EvidenceInspector
          scenario={recoveryScenarios[0]}
          snapshot={terminal ? terminalSnapshot : claimedSnapshot("approve")}
          receipt={terminal ? terminalReceipt : null}
          onServerSuccess={() => {
            refreshObserved();
            setTerminal(true);
          }}
        />
      );
    }

    render(<ExpiredClaimRefreshHarness />);
    await act(async () => { await Promise.resolve(); });

    expect(refreshObserved).toHaveBeenCalledTimes(1);
    expect(fetchMock).not.toHaveBeenCalled();
    const receipt = screen.getByRole("region", { name: "Outcome unknown" });
    expect(within(receipt).getByText("Approval count").parentElement).toHaveTextContent(
      "Approval count1",
    );
    expect(
      within(receipt).getByText("Approved remedy digest").parentElement,
    ).toHaveTextContent("Approved remedy digestNone");
    expect(screen.queryByRole("button", { name: /Resume exact/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve remedy" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Decline" })).not.toBeInTheDocument();
  });

  it("closes the fresh-decision rapid-click window before React state commits", () => {
    const fetchMock = vi.fn().mockImplementation(() => new Promise<Response>(() => {}));
    vi.stubGlobal("fetch", fetchMock);
    render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={pendingSnapshot()}
      />,
    );
    const button = screen.getByRole("button", { name: "Approve remedy" });

    fireEvent.click(button);
    fireEvent.click(button);
    fireEvent.click(button);

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("keeps a successful fresh decision latched before the accepted render commits", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          action: "approve",
          clientDecisionId: "decision-success-latch",
          recoveryId: "11111111-2222-4333-8444-555555555555",
          status: "completed",
          approvedRemedyDigest: fullDigest,
          executionStarted: true,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    let capturedButton: HTMLButtonElement | null = null;
    const onServerSuccess = vi.fn(() => {
      if (capturedButton === null) return;
      capturedButton.disabled = false;
      fireEvent.click(capturedButton);
    });
    render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={pendingSnapshot()}
        clientDecisionIdFactory={() => "decision-success-latch"}
        onServerSuccess={onServerSuccess}
      />,
    );
    const button = screen.getByRole<HTMLButtonElement>("button", { name: "Approve remedy" });
    const observedActivations = vi.fn();
    button.addEventListener("click", observedActivations);
    capturedButton = button;

    fireEvent.click(button);

    await waitFor(() => expect(onServerSuccess).toHaveBeenCalledTimes(1));
    expect(observedActivations).toHaveBeenCalledTimes(2);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("scopes an accepted decision latch to its committed recovery context", async () => {
    const snapshotB: RecoverySnapshot = {
      ...pendingSnapshot(),
      recoveryId: "bbbbbbbb-2222-4333-8444-555555555555",
      pendingApproval: {
        ...pendingSnapshot().pendingApproval!,
        remedyId: "remedy-server-b",
        toolCallId: "call-server-b",
      },
    };
    const idFactory = vi
      .fn<() => string>()
      .mockReturnValueOnce("decision-accepted-a")
      .mockReturnValueOnce("decision-new-b");
    const fetchMock = vi.fn().mockImplementation(
      (input: string | URL | Request, init?: RequestInit) => {
        const body = JSON.parse(String(init?.body)) as {
          action: "approve" | "decline";
          clientDecisionId: string;
          remedyDigest: string;
        };
        const recoveryId = String(input).split("/").at(-2);
        return Promise.resolve(
          new Response(
            JSON.stringify({
              action: body.action,
              clientDecisionId: body.clientDecisionId,
              recoveryId,
              status: "completed",
              approvedRemedyDigest: body.remedyDigest,
              executionStarted: true,
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
        );
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const { rerender } = render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={pendingSnapshot()}
        clientDecisionIdFactory={idFactory}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Approve remedy" }));
    expect(await screen.findByText(/Approval accepted by the server/)).toBeVisible();

    rerender(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={snapshotB}
        clientDecisionIdFactory={idFactory}
      />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Approve remedy" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(idFactory).toHaveBeenCalledTimes(2);
  });

  it("aborts a timed-out approval once, releases the mobile sheet, and ignores a late success", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-20T12:00:00.000Z"));
    let resolveDecision: ((response: Response) => void) | undefined;
    let requestSignal: AbortSignal | undefined;
    const fetchMock = vi.fn().mockImplementation(
      (_input: string | URL | Request, init?: RequestInit) =>
        new Promise<Response>((resolve) => {
          resolveDecision = resolve;
          requestSignal = init?.signal ?? undefined;
        }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const onClose = vi.fn();
    const onServerSuccess = vi.fn().mockResolvedValue(undefined);
    const renderInspector = (open: boolean) => (
      <EvidenceInspector
        mobile
        open={open}
        scenario={recoveryScenarios[0]}
        snapshot={pendingSnapshot()}
        clientDecisionIdFactory={() => "decision-timeout-mobile"}
        onClose={onClose}
        onServerSuccess={onServerSuccess}
      />
    );
    const { rerender } = render(renderInspector(true));

    fireEvent.click(screen.getByRole("button", { name: "Approve remedy" }));
    const closeButton = screen.getByRole("button", { name: "Close evidence sheet" });
    expect(closeButton).toBeDisabled();
    expect(requestSignal).toBeDefined();
    const abortListener = vi.fn();
    requestSignal?.addEventListener("abort", abortListener);

    await act(async () => {
      vi.advanceTimersByTime(DECISION_REQUEST_TIMEOUT_MS);
      await Promise.resolve();
    });

    expect(requestSignal?.aborted).toBe(true);
    expect(abortListener).toHaveBeenCalledTimes(1);
    expect(closeButton).toBeEnabled();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Approval request timed out. Retry only this exact approval; the decline action remains disabled.",
    );
    const cancelEvent = new Event("cancel", { cancelable: true });
    fireEvent(screen.getByRole("dialog", { name: "Approve exact remedy" }), cancelEvent);
    expect(cancelEvent.defaultPrevented).toBe(false);
    fireEvent.click(closeButton);
    expect(onClose).toHaveBeenCalledTimes(1);
    rerender(renderInspector(false));
    rerender(renderInspector(true));
    expect(screen.getByRole("button", { name: "Approve remedy" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Decline" })).toBeDisabled();

    resolveDecision?.(
      new Response(
        JSON.stringify({
          action: "approve",
          clientDecisionId: "decision-timeout-mobile",
          recoveryId: "11111111-2222-4333-8444-555555555555",
          status: "completed",
          approvedRemedyDigest: fullDigest,
          executionStarted: true,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      vi.advanceTimersByTime(DECISION_REQUEST_TIMEOUT_MS);
    });

    expect(abortListener).toHaveBeenCalledTimes(1);
    expect(onServerSuccess).not.toHaveBeenCalled();
    expect(screen.queryByText(/accepted by the server/i)).not.toBeInTheDocument();
  });

  it("retires the transport deadline before awaiting a hanging server refresh", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-20T12:00:00.000Z"));
    let requestSignal: AbortSignal | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((_input: string | URL | Request, init?: RequestInit) => {
        requestSignal = init?.signal ?? undefined;
        return Promise.resolve(
          new Response(
            JSON.stringify({
              action: "approve",
              clientDecisionId: "decision-refresh-hangs",
              recoveryId: "11111111-2222-4333-8444-555555555555",
              status: "completed",
              approvedRemedyDigest: fullDigest,
              executionStarted: true,
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
        );
      }),
    );
    const onClose = vi.fn();
    const onServerSuccess = vi.fn(() => new Promise<void>(() => {}));
    render(
      <EvidenceInspector
        mobile
        open
        scenario={recoveryScenarios[0]}
        snapshot={pendingSnapshot()}
        clientDecisionIdFactory={() => "decision-refresh-hangs"}
        onClose={onClose}
        onServerSuccess={onServerSuccess}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Approve remedy" }));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(onServerSuccess).toHaveBeenCalledTimes(1);
    expect(
      screen.getByText("Approval accepted by the server. Refreshing recovery evidence."),
    ).toBeVisible();
    const closeButton = screen.getByRole("button", { name: "Close evidence sheet" });
    expect(closeButton).toBeEnabled();
    await act(async () => {
      vi.advanceTimersByTime(DECISION_REQUEST_TIMEOUT_MS * 2);
      await Promise.resolve();
    });
    expect(requestSignal?.aborted).toBe(false);
    expect(screen.queryByText(/request timed out/i)).not.toBeInTheDocument();
    fireEvent.click(closeButton);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("locks an ambiguous decision to the original action and ID across deadline retries", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-20T12:00:00.000Z"));
    let resolveFirst: ((response: Response) => void) | undefined;
    const fetchMock = vi.fn().mockImplementation(
      (_input: string | URL | Request, init?: RequestInit) =>
        new Promise<Response>((resolve) => {
          if (fetchMock.mock.calls.length === 1) resolveFirst = resolve;
          expect(init?.signal).toBeInstanceOf(AbortSignal);
        }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const idFactory = vi.fn(() => "decision-timeout-retry");
    const onServerSuccess = vi.fn().mockResolvedValue(undefined);
    render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={pendingSnapshot()}
        clientDecisionIdFactory={idFactory}
        onServerSuccess={onServerSuccess}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Approve remedy" }));
    await act(async () => {
      vi.advanceTimersByTime(DECISION_REQUEST_TIMEOUT_MS);
      await Promise.resolve();
    });

    const declineButton = screen.getByRole("button", { name: "Decline" });
    expect(declineButton).toBeDisabled();
    expect(screen.getByRole("button", { name: "Approve remedy" })).toBeEnabled();
    fireEvent.click(declineButton);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Approve remedy" }));
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(idFactory).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls.map(([, init]) => JSON.parse(String(init?.body)))).toEqual([
      expect.objectContaining({
        action: "approve",
        clientDecisionId: "decision-timeout-retry",
      }),
      expect.objectContaining({
        action: "approve",
        clientDecisionId: "decision-timeout-retry",
      }),
    ]);

    resolveFirst?.(
      new Response(
        JSON.stringify({
          action: "approve",
          clientDecisionId: "decision-timeout-retry",
          recoveryId: "11111111-2222-4333-8444-555555555555",
          status: "completed",
          approvedRemedyDigest: fullDigest,
          executionStarted: true,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(screen.getByRole("button", { name: "Submitting approval…" })).toBeDisabled();
    expect(onServerSuccess).not.toHaveBeenCalled();
    expect(screen.queryByText(/accepted by the server/i)).not.toBeInTheDocument();
  });

  it("bounds claimed-decision resume and ignores a late rejection", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-20T12:00:00.000Z"));
    let rejectResume: ((reason: Error) => void) | undefined;
    let requestSignal: AbortSignal | undefined;
    const fetchMock = vi.fn().mockImplementation(
      (_input: string | URL | Request, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          rejectResume = reject;
          requestSignal = init?.signal ?? undefined;
        }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const onServerSuccess = vi.fn().mockResolvedValue(undefined);
    render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={claimedSnapshot("decline")}
        onServerSuccess={onServerSuccess}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Resume exact decline" }));
    const abortListener = vi.fn();
    requestSignal?.addEventListener("abort", abortListener);
    await act(async () => {
      vi.advanceTimersByTime(DECISION_REQUEST_TIMEOUT_MS);
      await Promise.resolve();
    });

    expect(abortListener).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Resume exact decline" })).toBeEnabled();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Exact decline resume timed out. Retry only this same server-authored action.",
    );
    rejectResume?.(new Error("late transport failure"));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByRole("alert")).toHaveTextContent("resume timed out");
    expect(onServerSuccess).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Resume exact decline" }));
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("aborts active decision deadlines on context change and unmount, then clears the action lock", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-20T12:00:00.000Z"));
    const requestSignals: AbortSignal[] = [];
    const fetchMock = vi.fn().mockImplementation(
      (_input: string | URL | Request, init?: RequestInit) => {
        if (init?.signal !== undefined && init.signal !== null) requestSignals.push(init.signal);
        return new Promise<Response>(() => {});
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const snapshotB: RecoverySnapshot = {
      ...pendingSnapshot(),
      recoveryId: "bbbbbbbb-2222-4333-8444-555555555555",
      pendingApproval: {
        ...pendingSnapshot().pendingApproval!,
        remedyId: "remedy-server-b",
        toolCallId: "call-server-b",
      },
    };
    const { rerender, unmount } = render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={pendingSnapshot()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Approve remedy" }));
    const firstAbortListener = vi.fn();
    requestSignals[0]?.addEventListener("abort", firstAbortListener);
    rerender(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={snapshotB}
      />,
    );
    await act(async () => { await Promise.resolve(); });

    expect(firstAbortListener).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Approve remedy" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Decline" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Decline" }));
    const secondAbortListener = vi.fn();
    requestSignals[1]?.addEventListener("abort", secondAbortListener);
    unmount();

    expect(secondAbortListener).toHaveBeenCalledTimes(1);
    await act(async () => {
      vi.advanceTimersByTime(DECISION_REQUEST_TIMEOUT_MS * 2);
      await Promise.resolve();
    });
    expect(firstAbortListener).toHaveBeenCalledTimes(1);
    expect(secondAbortListener).toHaveBeenCalledTimes(1);
  });

  it("does not let a late context-reset effect abort or unlock the new context request", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-20T12:00:00.000Z"));
    const requestSignals: AbortSignal[] = [];
    const postedBodies: Array<{ action: string; clientDecisionId: string }> = [];
    const fetchMock = vi.fn().mockImplementation(
      (_input: string | URL | Request, init?: RequestInit) => {
        if (init?.signal !== undefined && init.signal !== null) requestSignals.push(init.signal);
        postedBodies.push(JSON.parse(String(init?.body)) as {
          action: string;
          clientDecisionId: string;
        });
        return new Promise<Response>(() => {});
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const idFactory = vi
      .fn<() => string>()
      .mockReturnValueOnce("decision-new-context")
      .mockReturnValueOnce("decision-unexpected-replacement");
    const snapshotB: RecoverySnapshot = {
      ...pendingSnapshot(),
      recoveryId: "bbbbbbbb-2222-4333-8444-555555555555",
      pendingApproval: {
        ...pendingSnapshot().pendingApproval!,
        remedyId: "remedy-server-b",
        toolCallId: "call-server-b",
      },
    };
    const { rerender } = render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={pendingSnapshot()}
      />,
    );

    rerender(
      <>
        <EvidenceInspector
          scenario={recoveryScenarios[0]}
          snapshot={snapshotB}
          clientDecisionIdFactory={idFactory}
        />
        <ActivateDecisionInLayoutEffect buttonName="Approve remedy" />
      </>,
    );

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(requestSignals).toHaveLength(1);
    expect(requestSignals[0]?.aborted).toBe(false);
    fireEvent.click(
      screen.getByRole("button", { name: /approve remedy|submitting approval/i }),
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      vi.advanceTimersByTime(DECISION_REQUEST_TIMEOUT_MS);
      await Promise.resolve();
    });

    expect(screen.getByRole("button", { name: "Decline" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Approve remedy" }));
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(idFactory).toHaveBeenCalledTimes(1);
    expect(postedBodies).toEqual([
      expect.objectContaining({ action: "approve", clientDecisionId: "decision-new-context" }),
      expect.objectContaining({ action: "approve", clientDecisionId: "decision-new-context" }),
    ]);
  });

  it("does not let an abandoned context render corrupt the committed decision lock", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-20T12:00:00.000Z"));
    const postedBodies: Array<{ action: string; clientDecisionId: string }> = [];
    const fetchMock = vi.fn().mockImplementation(
      (_input: string | URL | Request, init?: RequestInit) => {
        postedBodies.push(JSON.parse(String(init?.body)) as {
          action: string;
          clientDecisionId: string;
        });
        return new Promise<Response>(() => {});
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const idFactory = vi
      .fn<() => string>()
      .mockReturnValueOnce("decision-committed-a")
      .mockReturnValueOnce("decision-corrupted-a");
    const snapshotB: RecoverySnapshot = {
      ...pendingSnapshot(),
      recoveryId: "bbbbbbbb-2222-4333-8444-555555555555",
      pendingApproval: {
        ...pendingSnapshot().pendingApproval!,
        remedyId: "remedy-server-b",
        toolCallId: "call-server-b",
      },
    };
    function SuspendedContextHarness() {
      const [renderB, setRenderB] = useState(false);
      return (
        <>
          <button
            type="button"
            onClick={() => startTransition(() => setRenderB(true))}
          >
            Attempt suspended context
          </button>
          <Suspense fallback={<p>Suspended context fallback</p>}>
            <EvidenceInspector
              scenario={recoveryScenarios[0]}
              snapshot={renderB ? snapshotB : pendingSnapshot()}
              clientDecisionIdFactory={idFactory}
            />
            {renderB ? <AlwaysSuspend /> : null}
          </Suspense>
        </>
      );
    }
    render(<SuspendedContextHarness />);

    fireEvent.click(screen.getByRole("button", { name: "Approve remedy" }));
    await act(async () => {
      vi.advanceTimersByTime(DECISION_REQUEST_TIMEOUT_MS);
      await Promise.resolve();
    });
    expect(screen.getByRole("button", { name: "Decline" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Attempt suspended context" }));
    expect(screen.queryByText("Suspended context fallback")).not.toBeInTheDocument();
    expect(screen.getByText("11111111-2222-4333-8444-555555555555")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Approve remedy" }));

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(idFactory).toHaveBeenCalledTimes(1);
    expect(postedBodies).toEqual([
      expect.objectContaining({ action: "approve", clientDecisionId: "decision-committed-a" }),
      expect.objectContaining({ action: "approve", clientDecisionId: "decision-committed-a" }),
    ]);
    expect(screen.getByRole("button", { name: "Decline" })).toBeDisabled();
  });

  it("keeps the pending tool-call ID visible before collapsed mobile technical evidence", () => {
    const { container } = render(
      <EvidenceInspector
        mobile
        open={false}
        scenario={recoveryScenarios[0]}
        snapshot={pendingSnapshot()}
      />,
    );

    const technical = container.querySelector("details");
    expect(technical).not.toBeNull();
    expect(technical).not.toHaveAttribute("open");
    expect(within(technical as HTMLElement).queryByText("call-server-742")).not.toBeInTheDocument();
    expect(screen.getByText("call-server-742")).toBeInTheDocument();
  });

  it("places the mobile non-execution boundary before secondary consent evidence", () => {
    render(
      <EvidenceInspector
        mobile
        open
        scenario={recoveryScenarios[0]}
        snapshot={pendingSnapshot()}
      />,
    );

    const boundary = screen.getByText("Execution has not begun.").closest(
      ".execution-boundary",
    );
    const evidence = screen.getByText("Booking").closest(".consent-evidence");

    expect(boundary).not.toBeNull();
    expect(evidence).not.toBeNull();
    expect(
      boundary!.compareDocumentPosition(evidence!) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("renders every consent value from the supplied server snapshot", () => {
    render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={pendingSnapshot()}
      />,
    );

    const inspector = screen.getByRole("complementary", {
      name: "Approve exact remedy",
    });
    expect(within(inspector).getByRole("heading", { name: "Approve exact remedy" })).toBeVisible();
    expect(
      within(inspector).getByText("11111111-2222-4333-8444-555555555555"),
    ).toBeVisible();
    expect(within(inspector).getByText("booking-server-742")).toBeVisible();
    expect(within(inspector).getByText("replace_room")).toBeVisible();
    expect(within(inspector).getByText("double → ocean-view king")).toBeVisible();
    expect(within(inspector).getByText("2026-09-04 → 2026-09-07")).toBeVisible();
    expect(within(inspector).getByText("$12.50 USD")).toBeVisible();
    expect(within(inspector).getByText("guest_note, room_type")).toBeVisible();
    expect(
      within(inspector).getByText("No additional fees; Preserve booking dates"),
    ).toBeVisible();
    expect(within(inspector).getByText("2026-09-01 18:45:30 UTC")).toBeVisible();
    expect(within(inspector).getByText("Hard constraint").nextSibling).toHaveTextContent(
      "Satisfied",
    );
    expect(
      within(inspector).getByText("Delegated authority").nextSibling,
    ).toHaveTextContent("Satisfied");
    expect(within(inspector).getByText("call-server-742")).toBeVisible();
    expect(within(inspector).getByText("Execution has not begun.")).toBeVisible();
    expect(within(inspector).getByText("sha256:0123456789ab…89abcdef")).toBeVisible();
    expect(within(inspector).queryByText(fullDigest)).not.toBeInTheDocument();
    expect(within(inspector).getByRole("button", { name: "Approve remedy" })).toBeVisible();
    expect(within(inspector).getByRole("button", { name: "Decline" })).toBeVisible();
  });

  it("copies the full digest while keeping only a shortened digest visible", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={pendingSnapshot()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Copy full remedy digest" }));

    await waitFor(() => expect(writeText).toHaveBeenCalledWith(fullDigest));
  });

  it("posts the exact tuple, submits without optimistic completion, then refetches", async () => {
    let resolveFetch: ((response: Response) => void) | undefined;
    const fetchMock = vi.fn().mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          resolveFetch = resolve;
        }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const onServerSuccess = vi.fn().mockResolvedValue(undefined);
    render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={pendingSnapshot()}
        clientDecisionIdFactory={() => "decision-stable-742"}
        onServerSuccess={onServerSuccess}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Approve remedy" }));

    expect(screen.getByRole("button", { name: "Submitting approval…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Decline" })).toBeDisabled();
    expect(screen.getByText("Execution has not begun.")).toBeVisible();
    expect(screen.queryByText(/completed receipt/i)).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(
      "/api/recoveries/11111111-2222-4333-8444-555555555555/decisions",
    );
    expect(JSON.parse(String(init.body))).toEqual({
      action: "approve",
      clientDecisionId: "decision-stable-742",
      remedyId: "remedy-server-742",
      remedyDigest: fullDigest,
      toolCallId: "call-server-742",
    });

    resolveFetch?.(
      new Response(
        JSON.stringify({
          action: "approve",
          clientDecisionId: "decision-stable-742",
          recoveryId: "11111111-2222-4333-8444-555555555555",
          status: "completed",
          approvedRemedyDigest: fullDigest,
          executionStarted: true,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    await waitFor(() => expect(onServerSuccess).toHaveBeenCalledTimes(1));
    expect(screen.getByText("Approval accepted by the server. Refreshing recovery evidence.")).toBeVisible();
    expect(screen.queryByText(/completed receipt/i)).not.toBeInTheDocument();
  });

  it("rejects an approval response for a different remedy digest", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          action: "approve",
          clientDecisionId: "decision-digest-mismatch-742",
          recoveryId: "11111111-2222-4333-8444-555555555555",
          status: "completed",
          approvedRemedyDigest: `sha256:${"f".repeat(64)}`,
          executionStarted: true,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const onServerSuccess = vi.fn().mockResolvedValue(undefined);
    render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={pendingSnapshot()}
        clientDecisionIdFactory={() => "decision-digest-mismatch-742"}
        onServerSuccess={onServerSuccess}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Approve remedy" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Approval could not be recorded. Try again with the same decision.",
    );
    expect(onServerSuccess).not.toHaveBeenCalled();
  });

  it("refreshes authoritative evidence instead of retrying an expired decision", async () => {
    const recoveryId = "11111111-2222-4333-8444-555555555555";
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          detail: {
            code: "remedy_expired",
            recoveryId,
          },
        }),
        { status: 422, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const onServerSuccess = vi.fn().mockResolvedValue(undefined);
    render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={pendingSnapshot()}
        clientDecisionIdFactory={() => "decision-expired-742"}
        onServerSuccess={onServerSuccess}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Approve remedy" }));

    await waitFor(() => expect(onServerSuccess).toHaveBeenCalledTimes(1));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(
      screen.getByText("Consent expired. Refreshing authoritative recovery evidence."),
    ).toBeVisible();
    expect(screen.queryByText(/could not be recorded/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve remedy" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Decline" })).toBeDisabled();
  });

  it("blocks both stale actions and refreshes once when another tab wins", async () => {
    const recoveryId = "11111111-2222-4333-8444-555555555555";
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          detail: {
            code: "already_decided",
            recoveryId,
          },
        }),
        { status: 409, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    let rejectRefresh: ((reason: Error) => void) | undefined;
    const onServerSuccess = vi.fn(
      () =>
        new Promise<void>((_resolve, reject) => {
          rejectRefresh = reject;
        }),
    );
    render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={pendingSnapshot()}
        clientDecisionIdFactory={() => "decision-race-loser-742"}
        onServerSuccess={onServerSuccess}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Approve remedy" }));

    await waitFor(() => expect(onServerSuccess).toHaveBeenCalledTimes(1));
    const approve = screen.getByRole("button", { name: "Approve remedy" });
    const decline = screen.getByRole("button", { name: "Decline" });
    expect(approve).toBeDisabled();
    expect(decline).toBeDisabled();
    expect(
      screen.getByText(
        "Decision state changed or could not be safely continued. Refreshing authoritative recovery evidence.",
      ),
    ).toBeVisible();
    expect(screen.queryByText(/accepted by the server/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/try again with the same decision/i)).not.toBeInTheDocument();
    fireEvent.click(approve);
    fireEvent.click(decline);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(onServerSuccess).toHaveBeenCalledTimes(1);

    rejectRefresh?.(new Error("snapshot unavailable"));
    expect(
      await screen.findByText(
        "Decision state changed, but refreshed recovery evidence is unavailable.",
      ),
    ).toBeVisible();
    expect(approve).toBeDisabled();
    expect(decline).toBeDisabled();
  });

  it("deduplicates a conflict refresh against the consent-expiry timer", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-01T18:45:29.999Z"));
    const recoveryId = "11111111-2222-4333-8444-555555555555";
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          detail: {
            code: "already_decided",
            recoveryId,
          },
        }),
        { status: 409, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const onServerSuccess = vi.fn(() => new Promise<void>(() => {}));
    render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={pendingSnapshot()}
        clientDecisionIdFactory={() => "decision-race-expiry-742"}
        onServerSuccess={onServerSuccess}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Approve remedy" }));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(onServerSuccess).toHaveBeenCalledTimes(1);
    await act(async () => {
      vi.advanceTimersByTime(2);
      await Promise.resolve();
    });
    expect(onServerSuccess).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Approve remedy" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Decline" })).toBeDisabled();
  });

  it("renders only the refreshed server-authored resume action after a race conflict", async () => {
    const recoveryId = "11111111-2222-4333-8444-555555555555";
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          detail: {
            code: "already_decided",
            recoveryId,
          },
        }),
        { status: 409, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    let rerenderInspector: ReturnType<typeof render>["rerender"] | null = null;
    const onServerSuccess = vi.fn(() => {
      if (rerenderInspector === null) throw new Error("Inspector was not rendered");
      rerenderInspector(
        <EvidenceInspector
          scenario={recoveryScenarios[0]}
          snapshot={claimedSnapshot("decline")}
          onServerSuccess={() => {}}
        />,
      );
    });
    const rendered = render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={pendingSnapshot()}
        clientDecisionIdFactory={() => "decision-race-refresh-742"}
        onServerSuccess={onServerSuccess}
      />,
    );
    rerenderInspector = rendered.rerender;

    fireEvent.click(screen.getByRole("button", { name: "Approve remedy" }));

    await waitFor(() => expect(onServerSuccess).toHaveBeenCalledTimes(1));
    expect(
      screen.getByRole("button", { name: "Resume exact decline" }),
    ).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Resume exact approval" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve remedy" })).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("clears the conflict latch for a new context and ignores the old refresh rejection", async () => {
    const recoveryId = "11111111-2222-4333-8444-555555555555";
    const originalSnapshot = pendingSnapshot();
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          detail: {
            code: "already_decided",
            recoveryId,
          },
        }),
        { status: 409, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    let rejectRefresh: ((reason: Error) => void) | undefined;
    const onServerSuccess = vi.fn(
      () =>
        new Promise<void>((_resolve, reject) => {
          rejectRefresh = reject;
        }),
    );
    const rendered = render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={originalSnapshot}
        clientDecisionIdFactory={() => "decision-old-context-742"}
        onServerSuccess={onServerSuccess}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Approve remedy" }));
    await waitFor(() => expect(onServerSuccess).toHaveBeenCalledTimes(1));

    const nextSnapshot = {
      ...pendingSnapshot(),
      recoveryId: "22222222-3333-4444-8555-666666666666",
      pendingApproval: {
        ...pendingSnapshot().pendingApproval!,
        remedyId: "remedy-next-context-742",
        remedyDigest: `sha256:${"b".repeat(64)}` as `sha256:${string}`,
        toolCallId: "call-next-context-742",
      },
    };
    rendered.rerender(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={nextSnapshot}
        clientDecisionIdFactory={() => "decision-new-context-742"}
        onServerSuccess={onServerSuccess}
      />,
    );

    expect(screen.getByRole("button", { name: "Approve remedy" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Decline" })).toBeEnabled();
    rejectRefresh?.(new Error("old refresh failed"));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(
      screen.queryByText(
        "Decision state changed, but refreshed recovery evidence is unavailable.",
      ),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve remedy" })).toBeEnabled();

    rendered.rerender(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={originalSnapshot}
        clientDecisionIdFactory={() => "decision-old-context-revisited-742"}
        onServerSuccess={onServerSuccess}
      />,
    );
    const revisitedApprove = screen.getByRole("button", { name: "Approve remedy" });
    expect(revisitedApprove).toBeDisabled();
    expect(screen.getByRole("button", { name: "Decline" })).toBeDisabled();
    fireEvent.click(revisitedApprove);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(onServerSuccess).toHaveBeenCalledTimes(1);
  });

  it("ignores a late conflict response after expiry already retired its request", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-01T18:45:29.999Z"));
    const recoveryId = "11111111-2222-4333-8444-555555555555";
    let resolveFetch: ((response: Response) => void) | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        () =>
          new Promise<Response>((resolve) => {
            resolveFetch = resolve;
          }),
      ),
    );
    const onServerSuccess = vi.fn().mockRejectedValue(
      new Error("expiry refresh failed"),
    );
    render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={pendingSnapshot()}
        clientDecisionIdFactory={() => "decision-late-conflict-742"}
        onServerSuccess={onServerSuccess}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Approve remedy" }));
    await act(async () => {
      vi.advanceTimersByTime(2);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(onServerSuccess).toHaveBeenCalledTimes(1);
    expect(
      screen.getByText(
        "Consent expired, but refreshed recovery evidence is unavailable.",
      ),
    ).toBeVisible();

    resolveFetch?.(
      new Response(
        JSON.stringify({
          detail: {
            code: "already_decided",
            recoveryId,
          },
        }),
        { status: 409, headers: { "Content-Type": "application/json" } },
      ),
    );
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(onServerSuccess).toHaveBeenCalledTimes(1);
    expect(
      screen.queryByText(
        "Decision state changed or could not be safely continued. Refreshing authoritative recovery evidence.",
      ),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText(
        "Consent expired, but refreshed recovery evidence is unavailable.",
      ),
    ).toBeVisible();
  });

  it("posts an exact decline, disables both actions, and refreshes only after acceptance", async () => {
    let resolveFetch: ((response: Response) => void) | undefined;
    const fetchMock = vi.fn().mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          resolveFetch = resolve;
        }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const onServerSuccess = vi.fn().mockResolvedValue(undefined);
    render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={pendingSnapshot()}
        clientDecisionIdFactory={() => "decision-decline-742"}
        onServerSuccess={onServerSuccess}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Decline" }));

    expect(screen.getByRole("button", { name: "Approve remedy" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Submitting decline…" })).toBeDisabled();
    expect(screen.getByText("Execution has not begun.")).toBeVisible();
    expect(onServerSuccess).not.toHaveBeenCalled();
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toEqual({
      action: "decline",
      clientDecisionId: "decision-decline-742",
      remedyId: "remedy-server-742",
      remedyDigest: fullDigest,
      toolCallId: "call-server-742",
    });

    resolveFetch?.(
      new Response(
        JSON.stringify({
          action: "decline",
          clientDecisionId: "decision-decline-742",
          recoveryId: "11111111-2222-4333-8444-555555555555",
          status: "closed_without_action",
          approvedRemedyDigest: null,
          executionStarted: false,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    await waitFor(() => expect(onServerSuccess).toHaveBeenCalledTimes(1));
    expect(
      screen.getByText("Decline accepted by the server. Refreshing recovery evidence."),
    ).toBeVisible();
  });

  it("keeps a server-accepted decision locked when refreshed evidence is unavailable", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            action: "approve",
            clientDecisionId: "decision-refresh-failure-742",
            recoveryId: "11111111-2222-4333-8444-555555555555",
            status: "completed",
            approvedRemedyDigest: fullDigest,
            executionStarted: true,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );
    render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={pendingSnapshot()}
        clientDecisionIdFactory={() => "decision-refresh-failure-742"}
        onServerSuccess={vi.fn().mockRejectedValue(new Error("refresh unavailable"))}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Approve remedy" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Approval accepted, but refreshed recovery evidence is unavailable.",
    );
    expect(
      screen.getByText("Approval accepted by the server. Refreshing recovery evidence."),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: "Approve remedy" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Decline" })).not.toBeInTheDocument();
  });

  it("ignores a late decision from recovery A while recovery B is submitting", async () => {
    let resolveA: ((response: Response) => void) | undefined;
    let resolveB: ((response: Response) => void) | undefined;
    const fetchMock = vi.fn().mockImplementation((_input: string | URL | Request, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body)) as { clientDecisionId: string };
      return new Promise<Response>((resolve) => {
        if (body.clientDecisionId === "decision-a") resolveA = resolve;
        else resolveB = resolve;
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    const idFactory = vi
      .fn<() => string>()
      .mockReturnValueOnce("decision-a")
      .mockReturnValueOnce("decision-b");
    const snapshotA = pendingSnapshot();
    const snapshotB: RecoverySnapshot = {
      ...pendingSnapshot(),
      recoveryId: "bbbbbbbb-2222-4333-8444-555555555555",
      currentStepSummary: "Recovery B awaits its own decision.",
      pendingApproval: {
        ...pendingSnapshot().pendingApproval!,
        remedyId: "remedy-server-b",
        toolCallId: "call-server-b",
      },
    };
    const onServerSuccess = vi.fn().mockResolvedValue(undefined);
    const { rerender } = render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={snapshotA}
        clientDecisionIdFactory={idFactory}
        onServerSuccess={onServerSuccess}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Approve remedy" }));
    rerender(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={snapshotB}
        clientDecisionIdFactory={idFactory}
        onServerSuccess={onServerSuccess}
      />,
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Approve remedy" })).not.toBeDisabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Approve remedy" }));
    expect(screen.getByRole("button", { name: "Submitting approval…" })).toBeDisabled();

    resolveA?.(
      new Response(
        JSON.stringify({
          action: "approve",
          clientDecisionId: "decision-a",
          recoveryId: snapshotA.recoveryId,
          status: "completed",
          approvedRemedyDigest: fullDigest,
          executionStarted: true,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(screen.getByRole("button", { name: "Submitting approval…" })).toBeDisabled();
    expect(screen.queryByText(/accepted by the server/i)).not.toBeInTheDocument();
    expect(onServerSuccess).not.toHaveBeenCalled();

    resolveB?.(
      new Response(
        JSON.stringify({
          action: "approve",
          clientDecisionId: "decision-b",
          recoveryId: snapshotB.recoveryId,
          status: "completed",
          approvedRemedyDigest: fullDigest,
          executionStarted: true,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    expect(
      await screen.findByText("Approval accepted by the server. Refreshing recovery evidence."),
    ).toBeVisible();
    expect(onServerSuccess).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: "Approve remedy" })).not.toBeInTheDocument();
  });

  it("ignores an original recovery A decision after switching A to B to A", async () => {
    let resolveOriginalA: ((response: Response) => void) | undefined;
    let resolveNewA: ((response: Response) => void) | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((_input: string | URL | Request, init?: RequestInit) => {
        const body = JSON.parse(String(init?.body)) as { clientDecisionId: string };
        return new Promise<Response>((resolve) => {
          if (body.clientDecisionId === "decision-original-a") resolveOriginalA = resolve;
          else resolveNewA = resolve;
        });
      }),
    );
    const idFactory = vi
      .fn<() => string>()
      .mockReturnValueOnce("decision-original-a")
      .mockReturnValueOnce("decision-new-a");
    const snapshotA = pendingSnapshot();
    const snapshotB: RecoverySnapshot = {
      ...pendingSnapshot(),
      recoveryId: "bbbbbbbb-2222-4333-8444-555555555555",
      currentStepSummary: "Recovery B transition.",
      pendingApproval: {
        ...pendingSnapshot().pendingApproval!,
        remedyId: "remedy-server-b",
        toolCallId: "call-server-b",
      },
    };
    const { rerender } = render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={snapshotA}
        clientDecisionIdFactory={idFactory}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Approve remedy" }));
    rerender(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={snapshotB}
        clientDecisionIdFactory={idFactory}
      />,
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Approve remedy" })).not.toBeDisabled(),
    );
    rerender(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={snapshotA}
        clientDecisionIdFactory={idFactory}
      />,
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Approve remedy" })).not.toBeDisabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Approve remedy" }));

    resolveOriginalA?.(
      new Response(
        JSON.stringify({
          action: "approve",
          clientDecisionId: "decision-original-a",
          recoveryId: snapshotA.recoveryId,
          status: "completed",
          approvedRemedyDigest: fullDigest,
          executionStarted: true,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByRole("button", { name: "Submitting approval…" })).toBeDisabled();
    expect(screen.queryByText(/accepted by the server/i)).not.toBeInTheDocument();

    resolveNewA?.(
      new Response(
        JSON.stringify({
          action: "approve",
          clientDecisionId: "decision-new-a",
          recoveryId: snapshotA.recoveryId,
          status: "completed",
          approvedRemedyDigest: fullDigest,
          executionStarted: true,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    expect(
      await screen.findByText("Approval accepted by the server. Refreshing recovery evidence."),
    ).toBeVisible();
  });

  it("shows an alert and reuses the same decision ID on retry", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response("failure", { status: 503 }))
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            action: "approve",
            clientDecisionId: "decision-retry-742",
            recoveryId: "11111111-2222-4333-8444-555555555555",
            status: "completed",
            approvedRemedyDigest: fullDigest,
            executionStarted: true,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);
    const onServerSuccess = vi.fn().mockResolvedValue(undefined);
    render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={pendingSnapshot()}
        clientDecisionIdFactory={() => "decision-retry-742"}
        onServerSuccess={onServerSuccess}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Approve remedy" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Approval could not be recorded. Try again with the same decision.",
    );
    const declineButton = screen.getByRole("button", { name: "Decline" });
    expect(declineButton).toBeDisabled();
    fireEvent.click(declineButton);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Approve remedy" }));

    await waitFor(() => expect(onServerSuccess).toHaveBeenCalledTimes(1));
    expect(fetchMock).toHaveBeenCalledTimes(2);
    const postedBodies = fetchMock.mock.calls.map(([, init]) =>
      JSON.parse(String((init as RequestInit).body)),
    );
    expect(postedBodies[0].clientDecisionId).toBe("decision-retry-742");
    expect(postedBodies[1].clientDecisionId).toBe("decision-retry-742");
    expect(postedBodies[0].action).toBe("approve");
    expect(postedBodies[1].action).toBe("approve");
  });

  it("shows the safe decision-capacity retry message without starting replay", async () => {
    const decisionUrl =
      "/api/recoveries/11111111-2222-4333-8444-555555555555/decisions";
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            code: "decision_capacity",
            message: DECISION_CAPACITY_MESSAGE,
            requestId: "0123456789abcdef0123456789abcdef",
          }),
          { status: 429, headers: { "Content-Type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            action: "approve",
            clientDecisionId: "decision-capacity-742",
            recoveryId: "11111111-2222-4333-8444-555555555555",
            status: "completed",
            approvedRemedyDigest: fullDigest,
            executionStarted: true,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);
    const onServerSuccess = vi.fn().mockResolvedValue(undefined);
    render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={pendingSnapshot()}
        clientDecisionIdFactory={() => "decision-capacity-742"}
        onServerSuccess={onServerSuccess}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Approve remedy" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      DECISION_CAPACITY_MESSAGE,
    );
    fireEvent.click(screen.getByRole("button", { name: "Approve remedy" }));

    await waitFor(() => expect(onServerSuccess).toHaveBeenCalledTimes(1));
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([decisionUrl, decisionUrl]);
    const postedBodies = fetchMock.mock.calls.map(([, init]) =>
      JSON.parse(String((init as RequestInit).body)),
    );
    expect(postedBodies.map((body) => body.clientDecisionId)).toEqual([
      "decision-capacity-742",
      "decision-capacity-742",
    ]);
    expect(postedBodies).not.toContainEqual(
      expect.objectContaining({ executionMode: "replay_fixture" }),
    );
  });

  it("shows a decline-specific error, does not refresh, and reuses its decision ID", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response("failure", { status: 503 }))
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            action: "decline",
            clientDecisionId: "decision-decline-retry-742",
            recoveryId: "11111111-2222-4333-8444-555555555555",
            status: "closed_without_action",
            approvedRemedyDigest: null,
            executionStarted: false,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);
    const onServerSuccess = vi.fn().mockResolvedValue(undefined);
    render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={pendingSnapshot()}
        clientDecisionIdFactory={() => "decision-decline-retry-742"}
        onServerSuccess={onServerSuccess}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Decline" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Decline could not be recorded. Try again with the same decision.",
    );
    expect(onServerSuccess).not.toHaveBeenCalled();
    const approveButton = screen.getByRole("button", { name: "Approve remedy" });
    expect(approveButton).toBeDisabled();
    fireEvent.click(approveButton);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Decline" }));

    await waitFor(() => expect(onServerSuccess).toHaveBeenCalledTimes(1));
    const postedBodies = fetchMock.mock.calls.map(([, init]) =>
      JSON.parse(String((init as RequestInit).body)),
    );
    expect(postedBodies).toEqual([
      expect.objectContaining({
        action: "decline",
        clientDecisionId: "decision-decline-retry-742",
      }),
      expect.objectContaining({
        action: "decline",
        clientDecisionId: "decision-decline-retry-742",
      }),
    ]);
  });

  it("does not expose an approval action after the durable decision claim", () => {
    const claimedSnapshot: RecoverySnapshot = {
      ...pendingSnapshot(),
      currentStepSummary: "Exact approval claimed; execution outcome pending.",
      pendingApproval: null,
    };

    render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={claimedSnapshot}
      />,
    );

    expect(screen.getByRole("heading", { name: "Decision in progress" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Approve remedy" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Decline" })).not.toBeInTheDocument();
    expect(screen.queryByText("Execution has not begun.")).not.toBeInTheDocument();
  });

  it("disables both decisions at the server deadline and refreshes exactly once", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-01T18:45:29.000Z"));
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const onServerSuccess = vi.fn().mockResolvedValue(undefined);
    render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={pendingSnapshot()}
        onServerSuccess={onServerSuccess}
      />,
    );

    expect(screen.getByRole("button", { name: "Approve remedy" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Decline" })).toBeEnabled();

    await act(async () => {
      vi.advanceTimersByTime(1_000);
      await Promise.resolve();
    });

    expect(
      screen.getByText("Consent deadline reached — checking the authoritative outcome"),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Approve remedy" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Decline" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Approve remedy" }));
    fireEvent.click(screen.getByRole("button", { name: "Decline" }));
    expect(fetchMock).not.toHaveBeenCalled();
    expect(onServerSuccess).toHaveBeenCalledTimes(1);

    await act(async () => {
      vi.advanceTimersByTime(60_000);
      await Promise.resolve();
    });
    expect(onServerSuccess).toHaveBeenCalledTimes(1);
  });

  it.each(["resolve", "reject"] as const)(
    "lets authoritative expiry retire an in-flight decision and ignores its late %s",
    async (outcome) => {
      vi.useFakeTimers();
      vi.setSystemTime(new Date("2026-09-01T18:45:29.000Z"));
      let resolveDecision: ((response: Response) => void) | undefined;
      let rejectDecision: ((reason: Error) => void) | undefined;
      let requestSignal: AbortSignal | undefined;
      const fetchMock = vi.fn().mockImplementation(
        (_input: string | URL | Request, init?: RequestInit) =>
          new Promise<Response>((resolve, reject) => {
            resolveDecision = resolve;
            rejectDecision = reject;
            requestSignal = init?.signal ?? undefined;
          }),
      );
      vi.stubGlobal("fetch", fetchMock);
      const onClose = vi.fn();
      const onServerSuccess = vi.fn().mockResolvedValue(undefined);
      render(
        <EvidenceInspector
          mobile
          open
          scenario={recoveryScenarios[0]}
          snapshot={pendingSnapshot()}
          clientDecisionIdFactory={() => "decision-at-deadline"}
          onClose={onClose}
          onServerSuccess={onServerSuccess}
        />,
      );

      fireEvent.click(screen.getByRole("button", { name: "Approve remedy" }));
      const abortListener = vi.fn();
      requestSignal?.addEventListener("abort", abortListener);
      await act(async () => {
        vi.advanceTimersByTime(1_000);
        await Promise.resolve();
      });

      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(requestSignal?.aborted).toBe(true);
      expect(abortListener).toHaveBeenCalledTimes(1);
      expect(onServerSuccess).toHaveBeenCalledTimes(1);
      expect(
        screen.getByText("Consent deadline reached — checking the authoritative outcome"),
      ).toBeVisible();
      expect(screen.getByRole("button", { name: "Approve remedy" })).toBeDisabled();
      expect(screen.getByRole("button", { name: "Decline" })).toBeDisabled();
      const closeButton = screen.getByRole("button", { name: "Close evidence sheet" });
      expect(closeButton).toBeEnabled();

      await act(async () => {
        if (outcome === "resolve") {
          resolveDecision?.(
            new Response(
              JSON.stringify({
                action: "approve",
                clientDecisionId: "decision-at-deadline",
                recoveryId: "11111111-2222-4333-8444-555555555555",
                status: "completed",
                approvedRemedyDigest: fullDigest,
                executionStarted: true,
              }),
              { status: 200, headers: { "Content-Type": "application/json" } },
            ),
          );
        } else {
          rejectDecision?.(new Error("late decision transport failure"));
        }
        await Promise.resolve();
        await Promise.resolve();
        vi.advanceTimersByTime(DECISION_REQUEST_TIMEOUT_MS + 1);
        await Promise.resolve();
      });

      expect(abortListener).toHaveBeenCalledTimes(1);
      expect(onServerSuccess).toHaveBeenCalledTimes(1);
      expect(screen.queryByText(/request timed out/i)).not.toBeInTheDocument();
      expect(screen.queryByText(/accepted by the server/i)).not.toBeInTheDocument();
      fireEvent.click(closeButton);
      expect(onClose).toHaveBeenCalledTimes(1);
    },
  );

  it("cancels an old deadline across context changes and refreshes only the active recovery", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-01T18:45:29.000Z"));
    const onServerSuccess = vi.fn().mockResolvedValue(undefined);
    const snapshotB: RecoverySnapshot = {
      ...pendingSnapshot(),
      recoveryId: "bbbbbbbb-2222-4333-8444-555555555555",
      pendingApproval: {
        ...pendingSnapshot().pendingApproval!,
        remedyId: "remedy-server-b",
        toolCallId: "call-server-b",
        expiry: "2026-09-01T18:45:40Z",
      },
    };
    const { rerender } = render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={pendingSnapshot()}
        onServerSuccess={onServerSuccess}
      />,
    );

    await act(async () => {
      vi.advanceTimersByTime(500);
    });
    rerender(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={snapshotB}
        onServerSuccess={onServerSuccess}
      />,
    );
    await act(async () => {
      vi.advanceTimersByTime(1_000);
      await Promise.resolve();
    });

    expect(onServerSuccess).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Approve remedy" })).toBeEnabled();
    expect(
      screen.queryByText("Consent deadline reached — checking the authoritative outcome"),
    ).not.toBeInTheDocument();

    await act(async () => {
      vi.advanceTimersByTime(9_500);
      await Promise.resolve();
    });
    expect(onServerSuccess).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Approve remedy" })).toBeDisabled();
  });

  it("does not refresh after the consent surface unmounts", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-01T18:45:29.000Z"));
    const onServerSuccess = vi.fn().mockResolvedValue(undefined);
    const { unmount } = render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={pendingSnapshot()}
        onServerSuccess={onServerSuccess}
      />,
    );

    unmount();
    await act(async () => {
      vi.advanceTimersByTime(2_000);
      await Promise.resolve();
    });

    expect(onServerSuccess).not.toHaveBeenCalled();
  });

  it.each(["resolve", "reject"] as const)(
    "ignores an in-flight decision that %s after unmount",
    async (outcome) => {
      let resolveFetch: ((response: Response) => void) | undefined;
      let rejectFetch: ((reason: Error) => void) | undefined;
      vi.stubGlobal(
        "fetch",
        vi.fn().mockImplementation(
          () =>
            new Promise<Response>((resolve, reject) => {
              resolveFetch = resolve;
              rejectFetch = reject;
            }),
        ),
      );
      const onServerSuccess = vi.fn().mockResolvedValue(undefined);
      const { unmount } = render(
        <EvidenceInspector
          scenario={recoveryScenarios[0]}
          snapshot={pendingSnapshot()}
          clientDecisionIdFactory={() => "decision-unmounted"}
          onServerSuccess={onServerSuccess}
        />,
      );
      fireEvent.click(screen.getByRole("button", { name: "Approve remedy" }));

      unmount();
      await act(async () => {
        if (outcome === "resolve") {
          resolveFetch?.(
            new Response(
              JSON.stringify({
                action: "approve",
                clientDecisionId: "decision-unmounted",
                recoveryId: "11111111-2222-4333-8444-555555555555",
                status: "completed",
                approvedRemedyDigest: fullDigest,
                executionStarted: true,
              }),
              { status: 200, headers: { "Content-Type": "application/json" } },
            ),
          );
        } else {
          rejectFetch?.(new Error("request ended after unmount"));
        }
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });

      expect(onServerSuccess).not.toHaveBeenCalled();
    },
  );
});
