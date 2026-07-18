import type { HealthStatus } from "../domain/runtime";
import type { RecoverySnapshot } from "../domain/recovery";

function isHealthStatus(value: unknown): value is HealthStatus {
  if (typeof value !== "object" || value === null) {
    return false;
  }

  const candidate = value as Record<string, unknown>;
  return (
    (candidate.backend === "openai" || candidate.backend === "stub") &&
    typeof candidate.liveReady === "boolean" &&
    candidate.providerBoundary === "demo_adapter_only"
  );
}

export async function getHealth(signal?: AbortSignal): Promise<HealthStatus> {
  const response = await fetch("/health", {
    headers: { Accept: "application/json" },
    signal,
  });

  if (!response.ok) {
    throw new Error(`Health request failed with status ${response.status}`);
  }

  const body: unknown = await response.json();
  if (!isHealthStatus(body)) {
    throw new Error("Health response did not match the runtime contract");
  }

  return body;
}

function isRecoverySnapshot(value: unknown): value is RecoverySnapshot {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate.recoveryId === "string" &&
    (candidate.scenarioId === "hotel" || candidate.scenarioId === "api-quota") &&
    (candidate.executionMode === "openai_live" ||
      candidate.executionMode === "sdk_stub" ||
      candidate.executionMode === "replay_fixture") &&
    (candidate.status === "in_progress" || candidate.status === "completed") &&
    typeof candidate.currentStep === "number" &&
    typeof candidate.currentStepSummary === "string" &&
    typeof candidate.createdAt === "string" &&
    typeof candidate.updatedAt === "string"
  );
}

export async function getRecovery(
  recoveryId: string,
  signal?: AbortSignal,
): Promise<RecoverySnapshot> {
  const response = await fetch(`/api/recoveries/${encodeURIComponent(recoveryId)}`, {
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    throw new Error(`Recovery request failed with status ${response.status}`);
  }
  const body: unknown = await response.json();
  if (!isRecoverySnapshot(body)) {
    throw new Error("Recovery response did not match the snapshot contract");
  }
  return body;
}
