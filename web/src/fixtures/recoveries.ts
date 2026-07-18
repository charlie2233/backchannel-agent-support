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
    currentStepSummary: "Simulated verification evidence is sealed in the fixture.",
    lifecycleDetails: {
      Detect: "Quota pressure found in the bundled usage trace.",
      Prove: "Recorded allowance and demand compared.",
      Negotiate: "A simulated temporary allocation was selected.",
      Authorize: "Fixture records delegated authority; no human approval.",
      Execute: "Simulated execution event replayed.",
      "Verify & seal": "Simulated receipt replayed.",
    },
    evidence: [
      { label: "Fixture", value: "api-quota-completed-v1", monospace: true },
      { label: "Model call", value: "None — bundled replay" },
      { label: "Provider execution", value: "None — replay only" },
      { label: "Execution mode", value: "replay_fixture", monospace: true },
      { label: "Receipt", value: "Simulated fixture evidence" },
    ],
  },
] as const satisfies ReadonlyArray<RecoveryScenario>;
