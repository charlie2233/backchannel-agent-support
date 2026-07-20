import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import axe from "axe-core";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";

const recoveryId = "11111111-2222-4333-8444-555555555555";

class QuietEventSource {
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  close = vi.fn();
  addEventListener = vi.fn();
  removeEventListener = vi.fn();

  constructor(_url: string) {}
}

function setViewport(mobile: boolean) {
  vi.stubGlobal(
    "matchMedia",
    vi.fn().mockImplementation((query: string) => ({
      matches: query === "(max-width: 759px)" ? mobile : false,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  );
}

function stubPendingRecovery() {
  vi.stubGlobal("EventSource", QuietEventSource);
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
              recoveryId,
              scenarioId: "hotel",
              executionMode: "openai_live",
              modelIds: ["gpt-5.6-luna", "gpt-5.6-terra"],
              rootTraceId: "trace_0123456789abcdef0123456789abcdef",
              status: "pending_approval",
              currentStep: 3,
              currentStepSummary: "Approval required before demo-provider dispatch.",
              createdAt: "2026-07-19T12:00:00Z",
              updatedAt: "2026-07-19T12:00:01Z",
              claimedDecision: null,
              pendingApproval: {
                remedyId: "remedy-server",
                remedyDigest: `sha256:${"a".repeat(64)}`,
                terms: {
                  bookingId: "booking-server",
                  action: "replace_room",
                  replacement: { fromRoomType: "double", toRoomType: "king" },
                  stay: { checkIn: "2026-08-14", checkOut: "2026-08-16" },
                  currency: "USD",
                },
                costDeltaMinor: 0,
                changedFields: ["room_type"],
                providerCommitments: ["No additional charge"],
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
  window.sessionStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("responsive operational console accessibility", () => {
  it.each([
    { mobile: false, label: "desktop" },
    { mobile: true, label: "mobile" },
  ])(
    "has no critical axe violations at $label",
    async ({ mobile }) => {
      setViewport(mobile);
      stubPendingRecovery();
      const { container } = render(<App />);
      fireEvent.click(await screen.findByRole("button", { name: "Start live recovery" }));
      await screen.findAllByText("Approval required before demo-provider dispatch.");

      if (mobile) {
        fireEvent.click(screen.getByRole("button", { name: "Review exact remedy" }));
        await screen.findByRole("dialog", { name: "Approve exact remedy" });
      }

      const result = await axe.run(container);
      expect(result.violations.filter(({ impact }) => impact === "critical")).toEqual([]);
    },
    15_000,
  );

  it("uses a semantic two-option selector and focus-managed evidence dialog on mobile", async () => {
    setViewport(true);
    stubPendingRecovery();
    render(<App />);

    const selector = await screen.findByRole("combobox", { name: "Scenario" });
    expect(within(selector).getAllByRole("option")).toHaveLength(2);
    expect(screen.queryByRole("navigation", { name: "Recovery scenarios" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Start live recovery" }));
    await screen.findAllByText("Approval required before demo-provider dispatch.");
    expect(screen.getByText("Step 4 of 6")).toBeVisible();

    const trigger = screen.getByRole("button", { name: "Review exact remedy" });
    fireEvent.click(trigger);
    const dialog = await screen.findByRole("dialog", { name: "Approve exact remedy" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(within(dialog).getByText("Execution has not begun.")).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "Approve remedy" })).not.toHaveFocus();
  });

  it("keeps the six lifecycle stages ordered with an explicit current step", async () => {
    setViewport(false);
    stubPendingRecovery();
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Start live recovery" }));
    await screen.findAllByText("Approval required before demo-provider dispatch.");

    const lifecycle = screen.getByRole("list", { name: "Recovery lifecycle" });
    expect(within(lifecycle).getAllByRole("listitem")).toHaveLength(6);
    expect(within(lifecycle).getByRole("listitem", { current: "step" })).toHaveTextContent(
      "Authorize",
    );
    await waitFor(() => expect(screen.getByText("GPT-5.6 agents")).toBeVisible());
  });
});
