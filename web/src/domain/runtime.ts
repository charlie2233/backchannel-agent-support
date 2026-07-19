export type ExecutionMode = "openai_live" | "sdk_stub" | "replay_fixture";

export type RuntimeBackend = "openai" | "stub";

export interface HealthStatus {
  backend: RuntimeBackend;
  liveReady: boolean;
  sdkStubReady: boolean;
  providerBoundary: "demo_adapter_only";
}

export interface ExecutionSnapshot {
  executionMode: ExecutionMode;
}

export interface RuntimePresentation {
  label: "OpenAI live" | "SDK stub" | "Replay fixture";
  explanation: string;
  showGpt56Agents: boolean;
}

const presentationByMode: Readonly<
  Record<ExecutionMode, Pick<RuntimePresentation, "label" | "explanation">>
> = {
  openai_live: {
    label: "OpenAI live",
    explanation:
      "Live OpenAI model orchestration against a demo provider adapter. No real hotel booking or payment.",
  },
  sdk_stub: {
    label: "SDK stub",
    explanation:
      "Deterministic transport and approval QA. No OpenAI model call. Demo adapter only.",
  },
  replay_fixture: {
    label: "Replay fixture",
    explanation: "Bundled deterministic protocol trace. No model call or provider execution.",
  },
};

export function deriveRuntimePresentation(
  health: HealthStatus,
  snapshot: ExecutionSnapshot,
): RuntimePresentation {
  const showGpt56Agents =
    health.backend === "openai" &&
    health.liveReady === true &&
    snapshot.executionMode === "openai_live";

  return {
    ...presentationByMode[snapshot.executionMode],
    showGpt56Agents,
  };
}
