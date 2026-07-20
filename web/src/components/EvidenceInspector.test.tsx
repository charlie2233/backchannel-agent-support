import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DECISION_CAPACITY_MESSAGE } from "../api/client";
import type { RecoverySnapshot } from "../domain/recovery";
import { recoveryScenarios } from "../fixtures/recoveries";
import { EvidenceInspector } from "./EvidenceInspector";

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
      delegatedAuthoritySatisfied: false,
      toolCallId: "call-server-742",
      executionStarted: false,
    },
  };
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("EvidenceInspector exact consent", () => {
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
    ).toHaveTextContent("Not satisfied");
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

  it("shares one authoritative refresh with a decision already in flight at the deadline", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-01T18:45:29.000Z"));
    let resolveDecision: ((response: Response) => void) | undefined;
    const fetchMock = vi.fn().mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          resolveDecision = resolve;
        }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const onServerSuccess = vi.fn().mockResolvedValue(undefined);
    render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={pendingSnapshot()}
        clientDecisionIdFactory={() => "decision-at-deadline"}
        onServerSuccess={onServerSuccess}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Approve remedy" }));
    await act(async () => {
      vi.advanceTimersByTime(1_000);
      await Promise.resolve();
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(onServerSuccess).toHaveBeenCalledTimes(1);

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
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(onServerSuccess).toHaveBeenCalledTimes(1);
    expect(
      screen.getByText("Approval accepted by the server. Refreshing recovery evidence."),
    ).toBeVisible();
  });

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
