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
export type RecoveryStatus =
  | "in_progress"
  | "pending_approval"
  | "completed"
  | "closed_without_action"
  | "outcome_unknown";

export type TerminalRecoveryStatus = Extract<
  RecoveryStatus,
  "completed" | "closed_without_action" | "outcome_unknown"
>;
export type DecisionAction = "approve" | "decline";

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
  rootTraceId?: string | null;
  modelIds?: string[];
  sdkVersion?: string | null;
  protocolVersion?: string | null;
  agentGraphVersion?: string | null;
  promptToolSchemaHash?: string | null;
}

export interface DecisionRequest {
  decision: DecisionAction;
  clientDecisionId: string;
  remedyId: string;
  remedyDigest: `sha256:${string}`;
  toolCallId: string;
}

export interface ApprovalDecisionResponse {
  clientDecisionId: string;
  recoveryId: string;
  decision: "approve";
  status: "completed";
  approvedRemedyDigest: `sha256:${string}`;
  executionStarted: true;
}

export interface DeclineDecisionResponse {
  clientDecisionId: string;
  recoveryId: string;
  decision: "decline";
  status: "closed_without_action" | "outcome_unknown";
  decisionRemedyDigest: `sha256:${string}`;
  executionStarted: boolean;
}

export type DecisionResponse =
  | ApprovalDecisionResponse
  | DeclineDecisionResponse;

export interface RecoveryReceipt {
  recoveryId: string;
  executionMode: ExecutionMode;
  status: TerminalRecoveryStatus;
  simulated: boolean;
  providerExecution: boolean;
  modelIds: string[];
  rootTraceId?: string | null;
  sdkVersion?: string | null;
  protocolVersion?: string | null;
  agentGraphVersion?: string | null;
  promptToolSchemaHash?: string | null;
  boundary: string;
  providerResult: string;
  authorizationSource: string;
  verificationResults: string[];
  decision: "approved" | "declined" | null;
  decisionRemedyDigest: `sha256:${string}` | null;
  executionCount: number;
  providerDispatchStarted: boolean;
  exactInterruptionRejected: boolean;
  permissionRevoked: boolean;
  scopeClosed: boolean;
  approvedRemedyDigest: `sha256:${string}` | null;
  quotaEvidence: QuotaEvidence | null;
}

export interface QuotaHardConstraints {
  regionPreserved: true;
  burstCoversDemand: true;
  durationWithinLimit: true;
  baseQuotaUnchanged: true;
}

export interface QuotaEvidence {
  providerCeilingRpm: 1000;
  recordedDemandRpm: 1200;
  temporaryBurstRpm: 1500;
  region: "US";
  durationSeconds: 900;
  extraCostMinor: 250;
  delegatedAuthorityMaxMinor: 500;
  currency: "USD";
  hardConstraints: QuotaHardConstraints;
  humanInterruptions: 0;
  approvals: 0;
  providerProofVerified: true;
  grantVerified: true;
  source: "sdk_simulator" | "recorded_fixture";
  revocationEvidenceKind:
    | "runtime_permission_revoked"
    | "recorded_revocation_only";
  protocolSteps: typeof lifecycleSteps;
}

export function isTerminalRecoveryStatus(
  status: RecoveryStatus,
): status is TerminalRecoveryStatus {
  return (
    status === "completed" ||
    status === "closed_without_action" ||
    status === "outcome_unknown"
  );
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
