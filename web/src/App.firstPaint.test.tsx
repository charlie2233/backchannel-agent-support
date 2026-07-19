import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

const useRecoveryMock = vi.hoisted(() => vi.fn());

vi.mock("./hooks/useRecovery", () => ({
  useRecovery: useRecoveryMock,
}));

import App from "./App";

const recoveryId = "11111111-2222-4333-8444-555555555555";
const digest = `sha256:${"a".repeat(64)}`;

const pendingSnapshot = {
  recoveryId,
  scenarioId: "hotel",
  executionMode: "sdk_stub",
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
    providerCommitments: ["No additional charge", "Preserve booking dates"],
    expiry: "2026-08-01T18:45:30Z",
    hardConstraintSatisfied: true,
    delegatedAuthoritySatisfied: true,
    toolCallId: "server-call",
    executionStarted: false,
  },
};

beforeEach(() => {
  sessionStorage.clear();
  useRecoveryMock.mockReturnValue({
    snapshot: pendingSnapshot,
    receipt: null,
    events: [],
    lastSeq: 0,
    loading: false,
    error: null,
  });
});

describe("persisted consent first paint", () => {
  it.each([
    ["approve", "Approving…"],
    ["decline", "Declining…"],
  ] as const)(
    "renders a stored %s as submitting before passive effects",
    (decision, submittingLabel) => {
      const storedRequest = {
        decision,
        clientDecisionId: `decision-${decision}-first-paint`,
        remedyId: "server-remedy",
        remedyDigest: digest,
        toolCallId: "server-call",
      };
      sessionStorage.setItem("backchannel.hotelRecovery.v1", recoveryId);
      sessionStorage.setItem(
        "backchannel.pendingDecision.v1",
        JSON.stringify({ recoveryId, request: storedRequest }),
      );

      const markup = renderToStaticMarkup(<App />);
      const document = new DOMParser().parseFromString(markup, "text/html");
      const buttons = Array.from(
        document.querySelectorAll<HTMLButtonElement>(".consent-actions button"),
      );

      expect(buttons).toHaveLength(2);
      expect(buttons.every((button) => button.disabled)).toBe(true);
      expect(buttons.map((button) => button.textContent)).toContain(submittingLabel);

      let alternateClicks = 0;
      const alternate = buttons.find((button) => button.textContent !== submittingLabel);
      alternate?.addEventListener("click", () => {
        alternateClicks += 1;
      });
      alternate?.click();
      expect(alternateClicks).toBe(0);
    },
  );
});
