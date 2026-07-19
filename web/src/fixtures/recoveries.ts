import type { RecoveryScenario } from "../domain/recovery";

export const recoveryScenarios = [
  {
    id: "hotel",
    title: "Hotel booking recovery",
    summary: "A conflicting room assignment is paused at operator authorization.",
    executionMode: "replay_fixture",
    status: "in_progress",
    currentStep: 3,
    currentStepSummary: "Replay paused before any provider dispatch.",
    lifecycleDetails: {
      Detect: "Conflict found in the bundled booking trace.",
      Prove: "Consumer and demo-provider records compared.",
      Negotiate: "One simulated replacement remedy prepared.",
      Authorize: "Recorded trace is paused at the consent boundary.",
      Execute: "Not started.",
      "Verify & seal": "Waiting for an execution outcome.",
    },
    evidence: [
      { label: "Fixture", value: "hotel-consent-v1", monospace: true },
      { label: "Model call", value: "None — bundled replay" },
      { label: "Provider execution", value: "None — replay only" },
      { label: "Execution mode", value: "replay_fixture", monospace: true },
      { label: "Current boundary", value: "Authorization not submitted" },
    ],
  },
  {
    id: "api-quota",
    title: "API quota recovery",
    summary: "A deterministic quota trace resolves inside delegated authority.",
    executionMode: "replay_fixture",
    status: "completed",
    currentStep: 5,
    currentStepSummary: "Recorded verification and permission revocation are sealed.",
    lifecycleDetails: {
      Detect: "Recorded demand of 1200 units exceeds the 1000-unit baseline ceiling.",
      Prove: "Recorded provider proof establishes a 200-unit shortfall.",
      Negotiate: "A 250-unit us-east-1 burst raises the ceiling to 1250 for 900 seconds.",
      Authorize:
        "Recorded 300 USD minor-unit cost is within the delegated 500-unit limit; approval count is zero.",
      Execute: "Recorded SDK-stub execution was verified at the temporary ceiling.",
      "Verify & seal":
        "Recorded permission quota-burst-demo-us-east-1 was revoked and the 1000-unit baseline restored.",
    },
    evidence: [
      { label: "Fixture", value: "api-quota-completed-v1", monospace: true },
      { label: "Model call", value: "None — bundled replay" },
      { label: "Provider execution", value: "None — replay only" },
      { label: "Execution mode", value: "replay_fixture", monospace: true },
      { label: "Approval count", value: "0 — delegated authority" },
      { label: "Receipt", value: "Recorded, simulated fixture evidence" },
    ],
  },
] as const satisfies ReadonlyArray<RecoveryScenario>;
