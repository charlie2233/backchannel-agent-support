import { createRef, useRef, useState } from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { RecoverySnapshot } from "../domain/recovery";
import type { RuntimePresentation } from "../domain/runtime";
import { ConsentSheet } from "./ConsentSheet";

const digest = `sha256:${"0123456789abcdef".repeat(4)}` as `sha256:${string}`;
const sdkPresentation: RuntimePresentation = {
  label: "SDK stub",
  explanation:
    "Deterministic transport and approval QA. No OpenAI model call. Demo adapter only.",
  showGpt56Agents: false,
};

function pendingSnapshot(): RecoverySnapshot {
  return {
    recoveryId: "11111111-2222-4333-8444-555555555555",
    scenarioId: "hotel",
    executionMode: "sdk_stub",
    status: "pending_approval",
    currentStep: 3,
    currentStepSummary: "Server paused at exact consent.",
    createdAt: "2026-07-18T20:00:00Z",
    updatedAt: "2026-07-18T20:00:01Z",
    pendingApproval: {
      remedyId: "remedy-server-742",
      remedyDigest: digest,
      terms: {
        bookingId: "booking-server-742",
        action: "replace_room",
        replacement: { fromRoomType: "double", toRoomType: "ocean-view king" },
        stay: { checkIn: "2026-09-04", checkOut: "2026-09-07" },
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
  sessionStorage.clear();
  vi.restoreAllMocks();
});

function DialogHarness() {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  return (
    <>
      <button ref={triggerRef} type="button" onClick={() => setOpen(true)}>
        Review exact remedy
      </button>
      <ConsentSheet
        snapshot={pendingSnapshot()}
        displayMode="dialog"
        open={open}
        onClose={() => setOpen(false)}
        triggerRef={triggerRef}
      />
    </>
  );
}

function TerminalHarness() {
  const [terminal, setTerminal] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const fallbackFocusRef = useRef<HTMLHeadingElement>(null);
  return (
    <>
      <h1 ref={fallbackFocusRef} tabIndex={-1}>Hotel booking recovery</h1>
      <div>
        {terminal ? null : <button ref={triggerRef} type="button">Review exact remedy</button>}
      </div>
      <button type="button" onClick={() => setTerminal(true)}>Seal terminal evidence</button>
      {terminal ? (
        <p role="status" aria-label="Recovery status updates">
          Recovery status: Completed.
        </p>
      ) : (
        <ConsentSheet
          snapshot={pendingSnapshot()}
          displayMode="dialog"
          open
          triggerRef={triggerRef}
          fallbackFocusRef={fallbackFocusRef}
        />
      )}
    </>
  );
}

describe("ConsentSheet", () => {
  it("opens as a modal dialog, traps focus, closes on Escape, and restores trigger focus", () => {
    render(<DialogHarness />);
    const trigger = screen.getByRole("button", { name: "Review exact remedy" });
    trigger.focus();
    fireEvent.click(trigger);

    const dialog = screen.getByRole("dialog", { name: "Approve exact remedy" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    const close = within(dialog).getByRole("button", { name: "Close" });
    const decline = within(dialog).getByRole("button", { name: "Decline" });
    const approve = within(dialog).getByRole("button", { name: "Approve remedy" });
    expect(close).toHaveFocus();
    expect(decline).not.toHaveFocus();
    expect(approve).not.toHaveFocus();

    fireEvent.keyDown(dialog, { key: "Tab", shiftKey: true });
    expect(approve).toHaveFocus();
    fireEvent.keyDown(dialog, { key: "Tab" });
    expect(close).toHaveFocus();

    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("keeps essential consent evidence visible and collapses only technical evidence on mobile", () => {
    render(
      <ConsentSheet
        snapshot={pendingSnapshot()}
        displayMode="dialog"
        open
        onClose={() => undefined}
      />,
    );

    const dialog = screen.getByRole("dialog", { name: "Approve exact remedy" });
    for (const label of [
      "Cost delta",
      "Remedy digest",
      "Expiry",
      "Hard constraint",
      "Delegated authority",
      "Tool call ID",
    ]) {
      expect(within(dialog).getByText(label).closest("details")).toBeNull();
    }
    expect(within(dialog).getByText("Execution has not begun.")).toBeVisible();
    const disclosure = within(dialog).getByText("Technical evidence").closest("details");
    expect(disclosure).not.toHaveAttribute("open");
    expect(within(dialog).getAllByRole("button", { name: /Decline|Approve remedy/ })).toHaveLength(2);
  });

  it("keeps desktop consent inline and technical evidence expanded", () => {
    render(<ConsentSheet snapshot={pendingSnapshot()} displayMode="inline" />);

    const inspector = screen.getByRole("complementary", { name: "Approve exact remedy" });
    expect(within(inspector).queryByRole("button", { name: "Close" })).not.toBeInTheDocument();
    expect(within(inspector).getByText("Technical evidence").closest("details")).toHaveAttribute("open");
  });

  it("shows authoritative compact recovery context only in the dialog", () => {
    const view = render(
      <ConsentSheet
        snapshot={pendingSnapshot()}
        displayMode="dialog"
        open
        scenarioTitle="Hotel booking recovery"
        runtimePresentation={sdkPresentation}
      />,
    );

    const context = screen.getByRole("region", { name: "Recovery context" });
    expect(context).toHaveTextContent("Hotel booking recovery");
    expect(context).toHaveTextContent("SDK stub");
    expect(context).toHaveTextContent(sdkPresentation.explanation);
    expect(context).toHaveTextContent("Step 4 of 6");
    expect(context).toHaveTextContent("Authorize");
    expect(context).toHaveTextContent("Server paused at exact consent.");

    view.rerender(
      <ConsentSheet
        snapshot={pendingSnapshot()}
        displayMode="inline"
        scenarioTitle="Hotel booking recovery"
        runtimePresentation={sdkPresentation}
      />,
    );
    expect(screen.queryByRole("region", { name: "Recovery context" })).not.toBeInTheDocument();
  });

  it("places the execution-not-begun warning directly beneath the consent heading", () => {
    render(
      <ConsentSheet snapshot={pendingSnapshot()} displayMode="dialog" open />,
    );

    const headingGroup = screen
      .getByRole("heading", { name: "Approve exact remedy" })
      .closest(".inspector-heading");
    const warning = screen
      .getByText("Execution has not begun.")
      .closest(".execution-boundary");
    expect(headingGroup?.nextElementSibling).toBe(warning);
  });

  it("renders approval expiry in UTC at whole-second precision", () => {
    const snapshot = pendingSnapshot();
    if (snapshot.pendingApproval === null) {
      throw new Error("Pending approval fixture is missing");
    }
    snapshot.pendingApproval = {
      ...snapshot.pendingApproval,
      expiry: "2026-09-01T18:45:30.987654Z",
    };
    render(<ConsentSheet snapshot={snapshot} displayMode="dialog" open />);

    const expiry = screen.getByText("2026-09-01 18:45:30 UTC");
    expect(expiry).toHaveAttribute("dateTime", "2026-09-01T18:45:30.987654Z");
  });

  it("describes Technical evidence inside its native summary control", () => {
    render(<ConsentSheet snapshot={pendingSnapshot()} displayMode="dialog" open />);

    const summary = screen.getByText("Technical evidence").closest("summary");
    expect(summary).not.toBeNull();
    expect(summary).toHaveTextContent("IDs, runtime, and trace provenance.");
  });

  it("focuses a persistent visible target when terminal evidence replaces the dialog", async () => {
    render(<TerminalHarness />);
    expect(screen.getByRole("button", { name: "Close" })).toHaveFocus();

    fireEvent.click(screen.getByRole("button", { name: "Seal terminal evidence" }));

    await waitFor(() =>
      expect(screen.getByRole("heading", { name: "Hotel booking recovery" })).toHaveFocus(),
    );
    expect(screen.getByRole("status", { name: "Recovery status updates" })).toHaveTextContent(
      "Recovery status: Completed.",
    );
  });

  it("moves focus to the persistent target in both breakpoint directions", () => {
    const triggerRef = createRef<HTMLButtonElement>();
    const fallbackFocusRef = createRef<HTMLHeadingElement>();
    const view = render(
      <>
        <button ref={triggerRef} type="button">Review exact remedy</button>
        <h1 ref={fallbackFocusRef} tabIndex={-1}>Hotel booking recovery</h1>
        <ConsentSheet
          snapshot={pendingSnapshot()}
          displayMode="dialog"
          open
          triggerRef={triggerRef}
          fallbackFocusRef={fallbackFocusRef}
        />
      </>,
    );

    view.rerender(
      <>
        <button ref={triggerRef} type="button">Review exact remedy</button>
        <h1 ref={fallbackFocusRef} tabIndex={-1}>Hotel booking recovery</h1>
        <ConsentSheet
          snapshot={pendingSnapshot()}
          displayMode="inline"
          triggerRef={triggerRef}
          fallbackFocusRef={fallbackFocusRef}
        />
      </>,
    );
    expect(fallbackFocusRef.current).toHaveFocus();

    screen.getByRole("button", { name: "Approve remedy" }).focus();
    view.rerender(
      <>
        <button ref={triggerRef} type="button">Review exact remedy</button>
        <h1 ref={fallbackFocusRef} tabIndex={-1}>Hotel booking recovery</h1>
        <ConsentSheet
          snapshot={pendingSnapshot()}
          displayMode="dialog"
          open={false}
          triggerRef={triggerRef}
          fallbackFocusRef={fallbackFocusRef}
        />
      </>,
    );
    expect(fallbackFocusRef.current).toHaveFocus();
  });

  it("preserves a deliberate mobile technical-evidence toggle after a desktop round trip", () => {
    const view = render(
      <ConsentSheet
        snapshot={pendingSnapshot()}
        displayMode="dialog"
        open
        onClose={() => undefined}
      />,
    );

    let disclosure = screen.getByText("Technical evidence").closest("details");
    expect(disclosure).not.toHaveAttribute("open");
    fireEvent.click(screen.getByText("Technical evidence"));
    expect(disclosure).toHaveAttribute("open");

    view.rerender(
      <ConsentSheet snapshot={pendingSnapshot()} displayMode="inline" />,
    );
    expect(screen.getByText("Technical evidence").closest("details")).toHaveAttribute(
      "open",
    );

    view.rerender(
      <ConsentSheet
        snapshot={pendingSnapshot()}
        displayMode="dialog"
        open
        onClose={() => undefined}
      />,
    );
    disclosure = screen.getByText("Technical evidence").closest("details");
    expect(disclosure).toHaveAttribute("open");
  });

  it("preserves a deliberate desktop technical-evidence toggle after a mobile round trip", () => {
    const view = render(
      <ConsentSheet snapshot={pendingSnapshot()} displayMode="inline" />,
    );

    let disclosure = screen.getByText("Technical evidence").closest("details");
    expect(disclosure).toHaveAttribute("open");
    fireEvent.click(screen.getByText("Technical evidence"));
    expect(disclosure).not.toHaveAttribute("open");

    view.rerender(
      <ConsentSheet
        snapshot={pendingSnapshot()}
        displayMode="dialog"
        open
        onClose={() => undefined}
      />,
    );
    expect(screen.getByText("Technical evidence").closest("details")).not.toHaveAttribute(
      "open",
    );

    view.rerender(
      <ConsentSheet snapshot={pendingSnapshot()} displayMode="inline" />,
    );
    disclosure = screen.getByText("Technical evidence").closest("details");
    expect(disclosure).not.toHaveAttribute("open");
  });
});
