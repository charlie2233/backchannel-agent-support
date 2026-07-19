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
export type DecisionAction = "approve" | "decline";
export type RecoveryStatus =
  | "in_progress"
  | "pending_approval"
  | "completed"
  | "closed_without_action"
  | "outcome_unknown";

export function isTerminalRecoveryStatus(status: RecoveryStatus): boolean {
  return (
    status === "completed" ||
    status === "closed_without_action" ||
    status === "outcome_unknown"
  );
}

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
  modelIds: string[];
  rootTraceId: string | null;
  status: RecoveryStatus;
  currentStep: number;
  currentStepSummary: string;
  createdAt: string;
  updatedAt: string;
  pendingApproval: PendingApproval | null;
}

export interface ApprovalDecisionRequest {
  action: DecisionAction;
  clientDecisionId: string;
  remedyId: string;
  remedyDigest: `sha256:${string}`;
  toolCallId: string;
}

export interface ApprovedDecisionResponse {
  action: "approve";
  clientDecisionId: string;
  recoveryId: string;
  status: "completed";
  approvedRemedyDigest: `sha256:${string}`;
  executionStarted: true;
}

export interface ClosedDecisionResponse {
  action: "decline";
  clientDecisionId: string;
  recoveryId: string;
  status: "closed_without_action";
  approvedRemedyDigest: null;
  executionStarted: false;
}

export interface UnknownDecisionResponse {
  action: "decline";
  clientDecisionId: string;
  recoveryId: string;
  status: "outcome_unknown";
  approvedRemedyDigest: null;
  executionStarted: null;
}

export type ApprovalDecisionResponse =
  | ApprovedDecisionResponse
  | ClosedDecisionResponse
  | UnknownDecisionResponse;

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
