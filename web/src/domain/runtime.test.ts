import { describe, expect, it } from "vitest";

import {
  deriveRuntimePresentation,
  type ExecutionMode,
  type HealthStatus,
} from "./runtime";

const provenanceCases: ReadonlyArray<{
  executionMode: ExecutionMode;
  label: string;
  explanation: string;
}> = [
  {
    executionMode: "openai_live",
    label: "OpenAI live",
    explanation:
      "Live OpenAI model orchestration against a demo provider adapter. No real hotel booking or payment.",
  },
  {
    executionMode: "sdk_stub",
    label: "SDK stub",
    explanation:
      "Deterministic transport and approval QA. No OpenAI model call. Demo adapter only.",
  },
  {
    executionMode: "replay_fixture",
    label: "Replay fixture",
    explanation: "Bundled deterministic protocol trace. No model call or provider execution.",
  },
];

const healthCases: ReadonlyArray<HealthStatus> = [
  {
    backend: "openai",
    liveReady: true,
    sdkStubReady: true,
    providerBoundary: "demo_adapter_only",
  },
  {
    backend: "openai",
    liveReady: false,
    sdkStubReady: true,
    providerBoundary: "demo_adapter_only",
  },
  {
    backend: "stub",
    liveReady: true,
    sdkStubReady: true,
    providerBoundary: "demo_adapter_only",
  },
  {
    backend: "stub",
    liveReady: false,
    sdkStubReady: true,
    providerBoundary: "demo_adapter_only",
  },
];

describe("deriveRuntimePresentation", () => {
  for (const provenance of provenanceCases) {
    for (const health of healthCases) {
      it(`${provenance.executionMode} with ${health.backend}/${String(health.liveReady)}`, () => {
        const result = deriveRuntimePresentation(health, {
          executionMode: provenance.executionMode,
        });

        expect(result.label).toBe(provenance.label);
        expect(result.explanation).toBe(provenance.explanation);
        expect(result.showGpt56Agents).toBe(
          health.backend === "openai" &&
            health.liveReady === true &&
            provenance.executionMode === "openai_live",
        );
      });
    }
  }
});
