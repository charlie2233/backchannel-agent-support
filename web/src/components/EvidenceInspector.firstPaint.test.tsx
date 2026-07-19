import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it } from "vitest";

import type { RecoverySnapshot } from "../domain/recovery";
import { recoveryScenarios } from "../fixtures/recoveries";
import { EvidenceInspector } from "./EvidenceInspector";

const recoveryId = "11111111-2222-4333-8444-555555555555";
const digest = `sha256:${"a".repeat(64)}` as const;
const scenario = recoveryScenarios[0];
const snapshot: RecoverySnapshot = {
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
    providerCommitments: ["No additional charge"],
    expiry: "2026-08-01T18:45:30Z",
    hardConstraintSatisfied: true,
    delegatedAuthoritySatisfied: true,
    toolCallId: "server-call",
    executionStarted: false,
  },
};

beforeEach(() => sessionStorage.clear());

describe("stored consent render lock", () => {
  it.each([
    ["approve", "Decline"],
    ["decline", "Approve remedy"],
  ] as const)(
    "disables the conflicting action synchronously for a stored %s",
    (decision, conflictingLabel) => {
      sessionStorage.setItem(
        "backchannel.pendingDecision.v1",
        JSON.stringify({
          recoveryId,
          request: {
            decision,
            clientDecisionId: `decision-${decision}-render-lock`,
            remedyId: "server-remedy",
            remedyDigest: digest,
            toolCallId: "server-call",
          },
        }),
      );

      const markup = renderToStaticMarkup(
        <EvidenceInspector scenario={scenario} snapshot={snapshot} />,
      );
      const document = new DOMParser().parseFromString(markup, "text/html");
      const conflictingButton = Array.from(
        document.querySelectorAll<HTMLButtonElement>(".consent-actions button"),
      ).find((button) => button.textContent === conflictingLabel);

      expect(conflictingButton).toBeDefined();
      expect(conflictingButton?.disabled).toBe(true);
      let clicks = 0;
      conflictingButton?.addEventListener("click", () => {
        clicks += 1;
      });
      conflictingButton?.click();
      expect(clicks).toBe(0);
    },
  );
});
