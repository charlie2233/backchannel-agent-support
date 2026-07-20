import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { RecoverySnapshot } from "../domain/recovery";
import { recoveryScenarios } from "../fixtures/recoveries";
import { EvidenceInspector } from "./EvidenceInspector";

const fullDigest = `sha256:${"0123456789abcdef".repeat(4)}` as `sha256:${string}`;

function pendingSnapshot(
  recoveryId = "11111111-2222-4333-8444-555555555555",
): RecoverySnapshot {
  return {
    recoveryId,
    scenarioId: "hotel",
    executionMode: "sdk_stub",
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

function approvedResponse(
  clientDecisionId: string,
  recoveryId = "11111111-2222-4333-8444-555555555555",
): Response {
  return new Response(
    JSON.stringify({
      clientDecisionId,
      recoveryId,
      decision: "approve",
      status: "completed",
      approvedRemedyDigest: fullDigest,
      executionStarted: true,
    }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );
}

afterEach(() => {
  cleanup();
  sessionStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("EvidenceInspector exact consent", () => {
  it(
    "renders every consent value and both accessible actions without autofocus",
    () => {
      render(
        <EvidenceInspector
          scenario={recoveryScenarios[0]}
          snapshot={pendingSnapshot()}
        />,
      );

      const inspector = screen.getByRole("complementary", {
        name: "Approve exact remedy",
      });
      expect(inspector).toHaveClass("evidence-inspector--consent");
      const approvalHeading = within(inspector).getByRole("heading", {
        name: "Approve exact remedy",
      });
      expect(approvalHeading).toBeVisible();
      expect(approvalHeading).toHaveAttribute("id", "approval-heading");
      expect(approvalHeading).toHaveAttribute("tabindex", "-1");
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
      const decline = within(inspector).getByRole("button", { name: "Decline" });
      const approve = within(inspector).getByRole("button", { name: "Approve remedy" });
      const actionGroups = inspector.querySelectorAll(".consent-actions");
      expect(actionGroups).toHaveLength(1);
      expect(within(actionGroups[0] as HTMLElement).getAllByRole("button")).toEqual([
        decline,
        approve,
      ]);
      expect(decline).toBeVisible();
      expect(approve).toBeVisible();
      expect(decline).not.toHaveFocus();
      expect(approve).not.toHaveFocus();
    },
    10_000,
  );

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

  it("posts exact approval, disables both actions, persists, and waits for terminal evidence", async () => {
    let resolveFetch: ((response: Response) => void) | undefined;
    const fetchMock = vi.fn().mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          resolveFetch = resolve;
        }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const onDecisionAccepted = vi.fn();
    render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={pendingSnapshot()}
        clientDecisionIdFactory={(action) => `decision-${action}-stable-742`}
        onDecisionAccepted={onDecisionAccepted}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Approve remedy" }));

    expect(screen.getByRole("button", { name: "Approving…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Decline" })).toBeDisabled();
    expect(screen.getByText("Execution has not begun.")).toBeVisible();
    expect(screen.queryByText(/completed receipt/i)).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(
      "/api/recoveries/11111111-2222-4333-8444-555555555555/decisions",
    );
    expect(JSON.parse(String(init.body))).toEqual({
      decision: "approve",
      clientDecisionId: "decision-approve-stable-742",
      remedyId: "remedy-server-742",
      remedyDigest: fullDigest,
      toolCallId: "call-server-742",
    });
    expect(JSON.parse(String(sessionStorage.getItem("backchannel.pendingDecision.v1")))).toEqual({
      recoveryId: "11111111-2222-4333-8444-555555555555",
      request: JSON.parse(String(init.body)),
    });

    resolveFetch?.(approvedResponse("decision-approve-stable-742"));
    await waitFor(() => expect(onDecisionAccepted).toHaveBeenCalledTimes(1));
    expect(screen.getByText(/Approval accepted.*terminal evidence/i)).toBeVisible();
    expect(screen.queryByText(/completed receipt/i)).not.toBeInTheDocument();
    expect(sessionStorage.getItem("backchannel.pendingDecision.v1")).not.toBeNull();
  });

  it("posts exact decline with its own stable ID and action-specific submitting state", async () => {
    let resolveFetch: ((response: Response) => void) | undefined;
    const fetchMock = vi.fn().mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          resolveFetch = resolve;
        }),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={pendingSnapshot()}
        clientDecisionIdFactory={(action) => `decision-${action}-stable-742`}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Decline" }));

    expect(screen.getByRole("button", { name: "Declining…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Approve remedy" })).toBeDisabled();
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toEqual({
      decision: "decline",
      clientDecisionId: "decision-decline-stable-742",
      remedyId: "remedy-server-742",
      remedyDigest: fullDigest,
      toolCallId: "call-server-742",
    });

    resolveFetch?.(
      new Response(
        JSON.stringify({
          clientDecisionId: "decision-decline-stable-742",
          recoveryId: "11111111-2222-4333-8444-555555555555",
          decision: "decline",
          status: "closed_without_action",
          decisionRemedyDigest: fullDigest,
          executionStarted: false,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    expect(await screen.findByText(/Decline accepted.*terminal evidence/i)).toBeVisible();
    expect(screen.queryByText("Closed without action")).not.toBeInTheDocument();
  });

  it("shows an alert, locks the alternate action, and reuses one ID on retry", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response("failure", { status: 503 }))
      .mockResolvedValueOnce(approvedResponse("decision-retry-742"));
    vi.stubGlobal("fetch", fetchMock);
    const onDecisionAccepted = vi.fn();
    render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={pendingSnapshot()}
        clientDecisionIdFactory={() => "decision-retry-742"}
        onDecisionAccepted={onDecisionAccepted}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Approve remedy" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The server returned an unexpected response. Retry the same decision.",
    );
    expect(screen.getByRole("button", { name: "Decline" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Approve remedy" }));

    await waitFor(() => expect(onDecisionAccepted).toHaveBeenCalledTimes(1));
    expect(fetchMock).toHaveBeenCalledTimes(2);
    const postedBodies = fetchMock.mock.calls.map(([, init]) =>
      JSON.parse(String((init as RequestInit).body)),
    );
    expect(postedBodies[0].clientDecisionId).toBe("decision-retry-742");
    expect(postedBodies[1].clientDecisionId).toBe("decision-retry-742");
    expect(postedBodies[0].decision).toBe("approve");
    expect(postedBodies[1].decision).toBe("approve");
  });

  it.each([
    {
      label: "capacity",
      failure: new Response(
        JSON.stringify({
          error: {
            code: "live_capacity_reached",
            message: "The live demo is currently at capacity.",
            requestId: "req_11111111111111111111111111111111",
            recoveryId: "11111111-2222-4333-8444-555555555555",
            retryAfterSeconds: 1,
            fallback: null,
          },
        }),
        { status: 429, headers: { "Content-Type": "application/json" } },
      ),
      message: "The live demo is currently at capacity. Retry the same decision.",
    },
    {
      label: "unavailable",
      failure: new Response(
        JSON.stringify({
          error: {
            code: "live_unavailable",
            message: "Live mode is unavailable on this server.",
            requestId: "req_22222222222222222222222222222222",
            recoveryId: "11111111-2222-4333-8444-555555555555",
            retryAfterSeconds: null,
            fallback: null,
          },
        }),
        { status: 503, headers: { "Content-Type": "application/json" } },
      ),
      message: "Live mode is unavailable on this server. Retry the same decision.",
    },
    {
      label: "network",
      failure: new TypeError("network-canary-must-not-render"),
      message: "Approval could not be recorded. Retry the same decision.",
    },
  ])(
    "retains the exact recovery and decision ID after a $label decision failure",
    async ({ failure, message }) => {
      const fetchMock = vi
        .fn()
        .mockImplementationOnce(() =>
          failure instanceof Response
            ? Promise.resolve(failure)
            : Promise.reject(failure),
        )
        .mockResolvedValueOnce(approvedResponse("decision-live-retry-742"));
      vi.stubGlobal("fetch", fetchMock);
      render(
        <EvidenceInspector
          scenario={recoveryScenarios[0]}
          snapshot={{ ...pendingSnapshot(), executionMode: "openai_live" }}
          clientDecisionIdFactory={() => "decision-live-retry-742"}
        />,
      );

      fireEvent.click(screen.getByRole("button", { name: "Approve remedy" }));

      expect(await screen.findByRole("alert")).toHaveTextContent(message);
      expect(screen.queryByText("network-canary-must-not-render")).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /replay/i })).not.toBeInTheDocument();
      const stored = JSON.parse(
        String(sessionStorage.getItem("backchannel.pendingDecision.v1")),
      ) as { recoveryId: string; request: { clientDecisionId: string } };
      expect(stored).toMatchObject({
        recoveryId: "11111111-2222-4333-8444-555555555555",
        request: { clientDecisionId: "decision-live-retry-742" },
      });

      fireEvent.click(screen.getByRole("button", { name: "Approve remedy" }));
      await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
      const requests = fetchMock.mock.calls.map(([, init]) =>
        JSON.parse(String((init as RequestInit).body)),
      );
      expect(requests[1]).toEqual(requests[0]);
    },
  );

  it("ignores a delayed decision acknowledgement after the displayed recovery changes", async () => {
    let resolveFirst: ((response: Response) => void) | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(
        () =>
          new Promise<Response>((resolve) => {
            resolveFirst = resolve;
          }),
      ),
    );
    const onDecisionAccepted = vi.fn();
    const view = render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={pendingSnapshot()}
        clientDecisionIdFactory={() => "decision-delayed-742"}
        onDecisionAccepted={onDecisionAccepted}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Approve remedy" }));

    const nextRecoveryId = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
    view.rerender(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={pendingSnapshot(nextRecoveryId)}
        clientDecisionIdFactory={() => "decision-next-742"}
        onDecisionAccepted={onDecisionAccepted}
      />,
    );
    resolveFirst?.(approvedResponse("decision-delayed-742"));

    await waitFor(() => {
      expect(screen.getByText(nextRecoveryId)).toBeVisible();
      expect(screen.getByRole("button", { name: "Approve remedy" })).toBeEnabled();
    });
    expect(onDecisionAccepted).not.toHaveBeenCalled();
    expect(screen.queryByText(/Approval accepted/i)).not.toBeInTheDocument();
  });

  it("does not expose either action after the durable decision claim", () => {
    const claimedSnapshot: RecoverySnapshot = {
      ...pendingSnapshot(),
      currentStepSummary: "Exact decline claimed; closure outcome pending.",
      pendingApproval: null,
    };

    render(
      <EvidenceInspector
        scenario={recoveryScenarios[0]}
        snapshot={claimedSnapshot}
      />,
    );

    const inspector = screen.getByRole("complementary", {
      name: "Decision in progress",
    });
    expect(inspector).not.toHaveClass("evidence-inspector--consent");
    expect(
      within(inspector).getByRole("heading", { name: "Decision in progress" }),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: "Approve remedy" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Decline" })).not.toBeInTheDocument();
    expect(screen.queryByText("Execution has not begun.")).not.toBeInTheDocument();
  });
});
