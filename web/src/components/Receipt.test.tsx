import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { RecoveryEvent } from "../api/events";
import type { RecoveryReceipt } from "../domain/recovery";
import { Receipt } from "./Receipt";

afterEach(cleanup);

const recoveryId = "11111111-2222-4333-8444-555555555555";
const approvedDigest = `sha256:${"a".repeat(64)}` as const;

function completedReceipt(): RecoveryReceipt {
  return {
    recoveryId,
    executionMode: "sdk_stub",
    status: "completed",
    simulated: true,
    providerExecution: true,
    modelCall: false,
    modelIds: [],
    rootTraceId: "qa_trace_0123456789abcdef0123456789abcdef",
    sdkVersion: "0.18.3",
    protocolVersion: "backchannel.approval.v1",
    agentGraphVersion: "backchannel.hotel-agent.v1",
    definitionDigest: "b".repeat(64),
    boundary:
      "Deterministic Agents SDK model and demo hotel adapter only; no OpenAI model call, real booking, or payment change.",
    providerResult: "Demo adapter confirmed the authorized action.",
    authorizationSource: "Approved Agents SDK commit_remedy interruption.",
    verificationResults: [
      "Immediate pre-execution remedy digest matched the approved digest.",
      "Demo provider dispatch returned confirmed.",
      "Temporary provider-dispatch permission revoked after the approved execution.",
    ],
    approvalCount: 1,
    approvedRemedyDigest: approvedDigest,
  };
}

function declinedReceipt(): RecoveryReceipt {
  return {
    ...completedReceipt(),
    status: "closed_without_action",
    providerExecution: false,
    providerResult: "Provider dispatch did not begin.",
    authorizationSource: "User declined the exact Agents SDK commit_remedy interruption.",
    verificationResults: [
      "Human consent requested.",
      "Remedy declined by operator.",
      "Exact interruption rejected.",
      "No replacement action selected.",
      "Execution count is zero.",
      "Provider dispatch did not begin.",
      "Temporary permission revoked.",
      "Cancellation receipt sealed.",
    ],
    approvedRemedyDigest: null,
    approvalCount: 0,
  };
}

const approvalRequested: RecoveryEvent = {
  recoveryId,
  seq: 4,
  type: "approval.requested",
  terminal: false,
  data: { phase: "Authorize", summary: "Human consent requested." },
  createdAt: "2026-07-19T12:00:00Z",
};

const closedEvent: RecoveryEvent = {
  recoveryId,
  seq: 5,
  type: "recovery.closed_without_action",
  terminal: true,
  data: {
    executionCount: 0,
    providerExecution: false,
    phase: "Verify & seal",
    summary: "Exact remedy declined before provider dispatch.",
  },
  createdAt: "2026-07-19T12:00:01Z",
};

describe("Receipt", () => {
  it("renders all required completed provenance from the authoritative receipt", () => {
    render(<Receipt events={[]} receipt={completedReceipt()} />);

    const receipt = screen.getByRole("region", { name: "Sealed receipt" });
    expect(within(receipt).getByText("Completed")).toBeVisible();
    expect(within(receipt).getByText(recoveryId)).toBeVisible();
    expect(within(receipt).getByText("SDK stub")).toBeVisible();
    expect(within(receipt).getByText("None — no model call")).toBeVisible();
    expect(within(receipt).getByText("qa_trace_0123456789abcdef0123456789abcdef")).toBeVisible();
    expect(within(receipt).getByText("Approved Agents SDK commit_remedy interruption.")).toBeVisible();
    expect(within(receipt).getByText(approvedDigest)).toBeVisible();
    expect(within(receipt).getByText("Matched immediately before execution")).toBeVisible();
    expect(within(receipt).getByText("Demo adapter confirmed the authorized action.")).toBeVisible();
    expect(within(receipt).getByText("Revoked")).toBeVisible();
    expect(within(receipt).getByText("0.18.3")).toBeVisible();
    expect(within(receipt).getByText("backchannel.approval.v1")).toBeVisible();
    expect(within(receipt).getByText("backchannel.hotel-agent.v1")).toBeVisible();
    expect(within(receipt).getByText(`sha256:${"b".repeat(64)}`)).toBeVisible();
  });

  it("renders the exact closed-without-action proof from receipt and event evidence", () => {
    render(<Receipt events={[approvalRequested, closedEvent]} receipt={declinedReceipt()} />);

    const receipt = screen.getByRole("region", { name: "Closed without action" });
    const proofList = within(receipt).getByRole("list", {
      name: "Closed-without-action proof",
    });
    for (const proof of [
      "Human consent requested.",
      "Remedy declined by operator.",
      "Exact interruption rejected.",
      "No replacement action selected.",
      "Temporary permission revoked.",
      "Cancellation receipt sealed.",
    ]) {
      expect(within(proofList).getByText(proof)).toBeVisible();
    }
    expect(within(receipt).getByText("executionCount = 0")).toBeVisible();
    expect(within(receipt).getAllByText("Provider dispatch did not begin.")).not.toHaveLength(0);
  });

  it("never turns an unknown outcome into cancellation evidence", () => {
    const unknown: RecoveryReceipt = {
      ...declinedReceipt(),
      status: "outcome_unknown",
      providerExecution: null,
      providerResult: "Provider dispatch may have begun; its outcome is unknown.",
      authorizationSource:
        "Decline arrived after execution or dispatch may have begun; cancellation was not claimed.",
      verificationResults: [
        "Exact interruption rejected.",
        "Prior execution or dispatch evidence detected.",
        "Outcome marked unknown instead of cancelled.",
        "Temporary permission revoked.",
        "Uncertain-outcome receipt sealed.",
      ],
      approvalCount: 0,
    };
    render(<Receipt events={[]} receipt={unknown} />);

    const receipt = screen.getByRole("region", { name: "Outcome unknown" });
    expect(within(receipt).getByText("Provider dispatch may have begun; its outcome is unknown.")).toBeVisible();
    expect(within(receipt).queryByText("executionCount = 0")).not.toBeInTheDocument();
    expect(within(receipt).queryByText("Closed without action")).not.toBeInTheDocument();
    expect(within(receipt).queryByText("Cancellation receipt sealed.")).not.toBeInTheDocument();
  });

  it("keeps replay receipts visibly simulated and execution-free", () => {
    const replay: RecoveryReceipt = {
      recoveryId,
      executionMode: "replay_fixture",
      status: "simulated_completed",
      simulated: true,
      providerExecution: false,
      modelCall: false,
      modelIds: [],
      rootTraceId: null,
      sdkVersion: null,
      protocolVersion: null,
      agentGraphVersion: null,
      definitionDigest: null,
      boundary: "Simulated replay receipt — no model call or provider execution.",
      providerResult: "None — recorded fixture outcome only.",
      authorizationSource: "Recorded delegated-authority fixture",
      verificationResults: ["No provider dispatch occurred"],
      approvalCount: 0,
      approvedRemedyDigest: null,
    };
    render(<Receipt events={[]} receipt={replay} />);

    const receipt = screen.getByRole("region", { name: "Simulated replay receipt" });
    expect(within(receipt).getByText("Replay fixture")).toBeVisible();
    expect(within(receipt).getByText("Simulated replay receipt — no model call or provider execution.")).toBeVisible();
    expect(within(receipt).getByText("None — no model call")).toBeVisible();
    expect(
      within(receipt).getAllByText("Not applicable — replay fixture/no provider execution"),
    ).toHaveLength(2);
    expect(within(receipt).queryByText("Not reported")).not.toBeInTheDocument();
  });

  it("labels zero-approval delegated authority without implying a missing digest check", () => {
    const quota: RecoveryReceipt = {
      ...completedReceipt(),
      authorizationSource: "Delegated authority policy; no human approval required.",
      approvalCount: 0,
      approvedRemedyDigest: null,
      providerResult: "Quota adapter verified the temporary 1250-unit ceiling.",
      verificationResults: [
        "Quota remedy remained inside delegated authority; approval count stayed zero.",
        "Temporary quota permission revoked; baseline ceiling restored to 1000 units.",
      ],
    };
    render(<Receipt events={[]} receipt={quota} />);

    const receipt = screen.getByRole("region", { name: "Sealed receipt" });
    expect(
      within(receipt).getByText(
        "Not applicable — delegated authority required no human approval.",
      ),
    ).toBeVisible();
    expect(
      within(receipt).getByText("Not applicable — no approved remedy digest was required."),
    ).toBeVisible();
    expect(within(receipt).getByText("Revoked")).toBeVisible();
    expect(within(receipt).queryByText("Not reported")).not.toBeInTheDocument();
  });
});
