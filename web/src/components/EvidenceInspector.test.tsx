import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

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
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("EvidenceInspector exact consent", () => {
  it("renders every consent value from the supplied server snapshot", () => {
    render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={pendingSnapshot()}
      />,
    );

    const inspector = screen.getByRole("complementary", {
      name: "Decide exact remedy",
    });
    expect(within(inspector).getByRole("heading", { name: "Decide exact remedy" })).toBeVisible();
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
});
