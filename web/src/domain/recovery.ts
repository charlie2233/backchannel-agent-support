import type { ExecutionMode } from "./runtime";

export const lifecycleSteps = [
  "Detect",
  "Prove",
  "Negotiate",
  "Authorize",
  "Execute",
  "Verify & seal",
] as const;

export type LifecycleStep = (typeof lifecycleSteps)[number];
export type ScenarioId = "hotel" | "api-quota";
export type RecoveryStatus = "in_progress" | "completed";

export interface EvidenceEntry {
  label: string;
  value: string;
  monospace?: boolean;
}

export interface RecoveryScenario {
  id: ScenarioId;
  title: string;
  summary: string;
  executionMode: ExecutionMode;
  status: RecoveryStatus;
  currentStep: number;
  currentStepSummary: string;
  lifecycleDetails: Readonly<Record<LifecycleStep, string>>;
  evidence: ReadonlyArray<EvidenceEntry>;
}
