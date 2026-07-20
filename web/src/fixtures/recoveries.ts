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

export const hotelReplayCompletedPresentation = {
  summary: "A completed recorded hotel replay sealed simulated evidence with no provider dispatch.",
  lifecycleDetails: {
    Detect: "Conflict found in the bundled booking trace.",
    Prove: "Consumer and demo-provider fixture records compared.",
    Negotiate: "One simulated replacement remedy replayed.",
    Authorize: "Recorded consent boundary replayed without runtime authority.",
    Execute: "Recorded simulated outcome without provider dispatch.",
    "Verify & seal": "Completed replay receipt sealed as simulated fixture evidence.",
  },
  evidence: [
    { label: "Fixture", value: "hotel-completed-v1", monospace: true },
    { label: "Model call", value: "None — recorded replay" },
    { label: "Provider dispatch", value: "None — replay only" },
    { label: "Execution mode", value: "replay_fixture", monospace: true },
    { label: "Receipt", value: "Completed simulated fixture evidence" },
  ],
} as const satisfies Pick<RecoveryScenario, "summary" | "lifecycleDetails" | "evidence">;

export const quotaSdkCompletedPresentation = {
  summary: "One local deterministic SDK trace verified a temporary quota grant.",
  lifecycleDetails: {
    Detect: "Recorded demand reached 1200 rpm.",
    Prove: "The simulator proved the provider ceiling was 1000 rpm in US.",
    Negotiate: "A 1500 rpm temporary burst was selected for 900 seconds.",
    Authorize: "All constraints fit delegated authority; no human approval occurred.",
    Execute: "One local demo quota-adapter dispatch completed.",
    "Verify & seal": "The grant was verified and runtime permission was revoked.",
  },
  evidence: [
    { label: "Source", value: "sdk_simulator", monospace: true },
    { label: "Model call", value: "None — deterministic SDK model" },
    { label: "Provider dispatch", value: "One — local demo adapter" },
    { label: "Human approvals", value: "0" },
    { label: "Permission", value: "Revoked after verification" },
  ],
} as const satisfies Pick<RecoveryScenario, "summary" | "lifecycleDetails" | "evidence">;

export const quotaReplayCompletedPresentation = {
  summary: "A recorded quota trace was replayed without runtime execution.",
  lifecycleDetails: {
    Detect: "Recorded demand of 1200 rpm was replayed.",
    Prove: "Recorded proof states a 1000 rpm provider ceiling in US.",
    Negotiate: "Recorded terms contain a 1500 rpm burst for 900 seconds.",
    Authorize: "Recorded constraints fit delegated authority; no approval occurred.",
    Execute: "Recorded outcome only; no runtime provider dispatch occurred.",
    "Verify & seal": "Recorded verification and revocation evidence was sealed.",
  },
  evidence: [
    { label: "Source", value: "recorded_fixture", monospace: true },
    { label: "Model call", value: "None — recorded replay" },
    { label: "Provider dispatch", value: "None — recorded replay" },
    { label: "Human approvals", value: "0" },
    { label: "Revocation", value: "Recorded evidence only" },
  ],
} as const satisfies Pick<RecoveryScenario, "summary" | "lifecycleDetails" | "evidence">;
