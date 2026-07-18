import type { HealthStatus } from "../domain/runtime";

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
