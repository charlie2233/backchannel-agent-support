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
export type RecoveryStatus = "in_progress" | "pending_approval" | "completed";

export interface HotelRemedyTerms {
  bookingId: string;
  action: "replace_room";
  replacement: {
    fromRoomType: string;
    toRoomType: string;
  };
  stay: {
    checkIn: string;
    checkOut: string;
  };
  currency: "USD";
}

export interface PendingApproval {
  remedyId: string;
  remedyDigest: `sha256:${string}`;
  terms: HotelRemedyTerms;
  costDeltaMinor: number;
  changedFields: string[];
  providerCommitments: string[];
  expiry: string;
  hardConstraintSatisfied: boolean;
  delegatedAuthoritySatisfied: boolean;
  toolCallId: string;
  executionStarted: false;
}

export interface RecoverySnapshot {
  recoveryId: string;
  scenarioId: ScenarioId;
  executionMode: ExecutionMode;
  status: RecoveryStatus;
  currentStep: number;
  currentStepSummary: string;
  createdAt: string;
  updatedAt: string;
  pendingApproval: PendingApproval | null;
}

export interface ApprovalDecisionRequest {
  clientDecisionId: string;
  remedyId: string;
  remedyDigest: `sha256:${string}`;
  toolCallId: string;
}

export interface ApprovalDecisionResponse {
  clientDecisionId: string;
  recoveryId: string;
  status: "completed";
  approvedRemedyDigest: `sha256:${string}`;
  executionStarted: true;
}

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
