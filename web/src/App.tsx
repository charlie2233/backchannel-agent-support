import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  LIVE_ADMISSION_MESSAGES,
  LiveAdmissionError,
  RecoveryCreationError,
  RecoveryLookupError,
  createRecovery,
  getReceipt,
  getRecovery,
} from "./api/client";
import type { RecoveryCreationCode } from "./api/client";
import { AppHeader } from "./components/AppHeader";
import { EventLedger } from "./components/EventLedger";
import { EvidenceInspector } from "./components/EvidenceInspector";
import { Lifecycle } from "./components/Lifecycle";
import { ProvenanceStrip } from "./components/ProvenanceStrip";
import { ScenarioRail } from "./components/ScenarioRail";
import { deriveRuntimePresentation } from "./domain/runtime";
import type {
  RecoveryReceipt,
  RecoveryScenario,
  RecoverySnapshot,
  RecoveryStatus,
  ScenarioId,
} from "./domain/recovery";
import { isTerminalRecoveryStatus } from "./domain/recovery";
import {
  acquirePendingRecoveryCreation,
  clearMatchingPendingRecoveryCreation,
  pendingRecoveryCreationsMatch,
  readPendingRecoveryCreation,
} from "./creationSession";
import type { PendingRecoveryCreation } from "./creationSession";
import { recoveryScenarios } from "./fixtures/recoveries";
import { useRecoveryEvents } from "./hooks/useRecovery";
import { useRuntimeHealth } from "./hooks/useRuntimeHealth";
import {
  clearHotelRecoveryHint,
  readHotelRecoveryHint,
  writeHotelRecoveryHint,
} from "./recoverySession";

const MOBILE_QUERY = "(max-width: 759px)";
const SDK_QA_NOTICE =
  "Live recovery is unavailable in this demo. You are viewing the deterministic SDK QA trace; replay remains available explicitly.";
const EXPLICIT_REPLAY_NOTICE =
  "Live recovery is unavailable in this demo. You are viewing an explicitly requested replay fixture.";
const EXPLICIT_DEMO_NOTICE =
  "Live recovery is unavailable in this demo. No fallback run has started; choose a replay fixture or SDK QA trace explicitly.";
const AWAITING_DEMO_NOTICE =
  "Awaiting authoritative server evidence for the explicitly requested demo run.";
const RETRYABLE_RESUME_NOTICE =
  "Saved recovery evidence is temporarily unavailable. Its same-tab recovery ID was retained, and no server state or action has been accepted.";

export const SAVED_RECOVERY_RESTORE_TIMEOUT_MS = 12_000;
export const RECEIPT_REQUEST_TIMEOUT_MS = 12_000;
const RECEIPT_LOAD_ERROR =
  "The authoritative terminal receipt could not be loaded.";

type HotelResumeState =
  | "checking"
  | "none"
  | "invalid"
  | "retryable"
  | "retrying"
  | "restored";

interface HotelResumeRequest {
  controller: AbortController;
  deadlineId: ReturnType<typeof setTimeout> | null;
  generation: number;
  restoreFocusOnRetryable: boolean;
  active: boolean;
}

interface ReceiptRequest {
  controller: AbortController;
  deadlineId: ReturnType<typeof setTimeout> | null;
  bindingKey: string;
  restoreFocusOnFailure: boolean;
  active: boolean;
}

interface TerminalSnapshotRequest {
  controller: AbortController;
  deadlineId: ReturnType<typeof setTimeout> | null;
  refreshKey: string | null;
  restoreFocusOnFailure: boolean;
  active: boolean;
}

const HOTEL_IDLE_SCENARIO: RecoveryScenario = {
  id: "hotel",
  title: "Hotel booking recovery",
  summary: "No server recovery has been started in this browser session.",
  executionMode: "replay_fixture",
  status: "in_progress",
  currentStep: 0,
  currentStepSummary: "No server run started.",
  lifecycleDetails: {
    Detect: "No server run started.",
    Prove: "No provider evidence has been requested.",
    Negotiate: "No remedy has been prepared.",
    Authorize: "No consent request exists.",
    Execute: "No provider dispatch has been authorized.",
    "Verify & seal": "No server receipt exists.",
  },
  evidence: [],
};

function useMobileLayout(): boolean {
  const [mobile, setMobile] = useState(
    () => typeof window.matchMedia === "function" && window.matchMedia(MOBILE_QUERY).matches,
  );

  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const media = window.matchMedia(MOBILE_QUERY);
    const update = (event: MediaQueryListEvent) => setMobile(event.matches);
    setMobile(media.matches);
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

  return mobile;
}

function serverStatusLabel(status: RecoveryStatus, hasPendingApproval: boolean): string {
  switch (status) {
    case "completed": return "Completed";
    case "closed_without_action": return "Closed without action";
    case "outcome_unknown": return "Outcome unknown";
    case "pending_approval": return hasPendingApproval ? "Awaiting decision" : "Decision in progress";
    case "in_progress": return "Recovery in progress";
  }
}

function receiptMatchesSnapshot(
  receipt: RecoveryReceipt,
  snapshot: RecoverySnapshot,
): boolean {
  if (!isTerminalRecoveryStatus(snapshot.status)) return false;
  const expectedStatus =
    snapshot.executionMode === "replay_fixture" && snapshot.status === "completed"
      ? "simulated_completed"
      : snapshot.status;
  const provenanceMatches =
    receipt.recoveryId === snapshot.recoveryId &&
    receipt.executionMode === snapshot.executionMode &&
    receipt.status === expectedStatus &&
    receipt.rootTraceId === snapshot.rootTraceId &&
    receipt.modelIds.length === snapshot.modelIds.length &&
    receipt.modelIds.every((modelId, index) => modelId === snapshot.modelIds[index]);
  if (!provenanceMatches) return false;
  if (snapshot.scenarioId === "api-quota") {
    if (snapshot.status === "completed") {
      return (
        snapshot.executionMode !== "openai_live" &&
        receipt.approvalCount === 0
      );
    }
    return (
      snapshot.status === "outcome_unknown" &&
      snapshot.executionMode === "sdk_stub" &&
      receipt.approvalCount === 0 &&
      receipt.boundary ===
        "Deterministic Agents SDK stub and demo quota adapter only; no OpenAI model call or real quota change."
    );
  }
  return (
    snapshot.scenarioId === "hotel" &&
    (snapshot.status !== "completed" ||
      snapshot.executionMode === "replay_fixture" ||
      receipt.approvalCount === 1)
  );
}

function receiptSnapshotKey(snapshot: RecoverySnapshot): string {
  return JSON.stringify([
    snapshot.recoveryId,
    snapshot.scenarioId,
    snapshot.executionMode,
    snapshot.status,
    snapshot.rootTraceId,
    snapshot.modelIds,
  ]);
}

function workspaceLabel(mode: RecoverySnapshot["executionMode"] | undefined): string {
  switch (mode) {
    case "openai_live": return "Live agent workspace";
    case "sdk_stub": return "SDK QA workspace";
    case "replay_fixture": return "Replay workspace";
    case undefined: return "Recovery workspace";
  }
}

function serverLifecycleDetails(snapshot: RecoverySnapshot): RecoveryScenario["lifecycleDetails"] {
  if (snapshot.scenarioId === "api-quota") {
    if (snapshot.executionMode === "replay_fixture") {
      return {
        Detect: "Recorded quota demand exceeded the recorded baseline ceiling.",
        Prove: "Recorded provider evidence proved the fixture's quota ceiling.",
        Negotiate: "Recorded temporary US-region burst terms were replayed.",
        Authorize: "Recorded delegated authority required zero human approvals.",
        Execute: "No quota adapter execution occurred; this is a replay fixture.",
        "Verify & seal":
          "Recorded verification and permission-revocation evidence was replayed.",
      };
    }
    if (snapshot.status === "outcome_unknown") {
      return {
        Detect: "Recovery creation was durably recorded before the interruption.",
        Prove: "No complete provider proof was committed before restart.",
        Negotiate: "No temporary burst terms are asserted from incomplete evidence.",
        Authorize:
          "Delegated authority existed; no human approval was recorded, and an unrecorded request remains possible.",
        Execute: "Provider dispatch may have begun; its outcome remains unknown.",
        "Verify & seal":
          "Verification and permission revocation are not asserted; an unknown-outcome receipt was sealed.",
      };
    }
    return {
      Detect: "Server detected demand of 1200 units above the 1000-unit baseline ceiling.",
      Prove: "Provider proved a 1000-unit baseline ceiling.",
      Negotiate: "A 250-unit us-east-1 burst raises the ceiling to 1250.",
      Authorize:
        "Delegated authority covers 300 USD minor units within a 500-unit limit; approval count is zero.",
      Execute: "The demo quota adapter verified execution at the temporary 1250-unit ceiling.",
      "Verify & seal":
        "Execution was verified and temporary permission quota-burst-demo-us-east-1 was revoked.",
    };
  }
  const recorded = {
    Detect: "Server recovery detected the hotel booking conflict.",
    Prove: "Server-side recovery evidence was recorded.",
    Negotiate: "One exact replacement remedy was prepared.",
  } as const;

  switch (snapshot.status) {
    case "completed":
      return {
        ...recorded,
        Authorize: "Exact remedy approval was accepted by the server.",
        Execute: "Approved provider dispatch completed.",
        "Verify & seal": "Provider verification and receipt sealing were recorded.",
      };
    case "closed_without_action":
      return {
        ...recorded,
        Authorize: "The exact remedy was declined and its permission was revoked.",
        Execute: "Provider dispatch did not begin.",
        "Verify & seal": "Cancellation evidence was sealed by the server.",
      };
    case "outcome_unknown":
      return {
        ...recorded,
        Authorize: "An exact decision was claimed; its provider outcome remains unresolved.",
        Execute: "Provider dispatch may have begun; its outcome is unknown.",
        "Verify & seal": "Uncertain-outcome evidence was sealed by the server.",
      };
    case "pending_approval":
      return {
        ...recorded,
        Authorize:
          snapshot.pendingApproval === null
            ? "A durable decision claim is being resolved."
            : snapshot.currentStepSummary,
        Execute:
          snapshot.pendingApproval === null
            ? "The execution outcome is not yet available."
            : "Provider dispatch has not begun.",
        "Verify & seal": "Waiting for the durable decision outcome.",
      };
    case "in_progress":
      return {
        ...recorded,
        Authorize: snapshot.currentStepSummary,
        Execute: "No provider dispatch has been recorded.",
        "Verify & seal": "No terminal evidence is available.",
      };
  }
}

export default function App() {
  const mobile = useMobileLayout();
  const [activeId, setActiveId] = useState<ScenarioId>("hotel");
  const { health, phase: healthPhase, retry: retryRuntimeHealth } = useRuntimeHealth();
  const [hotelSnapshot, setHotelSnapshot] = useState<RecoverySnapshot | null>(null);
  const [hotelResumeState, setHotelResumeState] = useState<HotelResumeState>("checking");
  const [hotelCreationIntent, setHotelCreationIntent] =
    useState<PendingRecoveryCreation | null>(
      () => readPendingRecoveryCreation("hotel"),
    );
  const [hotelCreationErrorCode, setHotelCreationErrorCode] =
    useState<RecoveryCreationCode | null>(null);
  const [hotelRequiresExplicitRestart, setHotelRequiresExplicitRestart] =
    useState(false);
  const [hotelLiveLoading, setHotelLiveLoading] = useState(false);
  const [quotaSnapshot, setQuotaSnapshot] = useState<RecoverySnapshot | null>(null);
  const [quotaLoading, setQuotaLoading] = useState(false);
  const [quotaError, setQuotaError] = useState<string | null>(null);
  const [quotaCreationErrorCode, setQuotaCreationErrorCode] =
    useState<RecoveryCreationCode | null>(null);
  const [quotaConflictingIntent, setQuotaConflictingIntent] =
    useState<PendingRecoveryCreation | null>(null);
  const quotaStartedRef = useRef(false);
  const appMountedRef = useRef(false);
  const hotelStartGenerationRef = useRef(0);
  const hotelLiveStartInFlightRef = useRef(false);
  const hotelDemoStartInFlightRef = useRef(false);
  const hotelLiveControllerRef = useRef<AbortController | null>(null);
  const hotelResumeRequestRef = useRef<HotelResumeRequest | null>(null);
  const hotelResumeRecoveryIdRef = useRef<string | null>(null);
  const hotelResumeFocusGenerationRef = useRef<number | null>(null);
  const hotelResumeRetryButtonRef = useRef<HTMLButtonElement>(null);
  const invalidResumeObservedRef = useRef(false);
  const [replayFallback, setReplayFallback] = useState<string | null>(null);
  const [replayLoading, setReplayLoading] = useState(false);
  const [replayError, setReplayError] = useState<string | null>(null);
  const [sdkLoading, setSdkLoading] = useState(false);
  const [sdkError, setSdkError] = useState<string | null>(null);
  const [hotelStartError, setHotelStartError] = useState<string | null>(null);
  const [receipts, setReceipts] = useState<Record<string, RecoveryReceipt>>({});
  const [receiptLoading, setReceiptLoading] = useState<Record<string, boolean>>({});
  const [receiptErrors, setReceiptErrors] = useState<Record<string, string>>({});
  const receiptRequestsRef = useRef(new Map<string, ReceiptRequest>());
  const completedReceiptBindingsRef = useRef(new Map<string, string>());
  const hotelSnapshotRef = useRef<RecoverySnapshot | null>(null);
  const quotaSnapshotRef = useRef<RecoverySnapshot | null>(null);
  const receiptRetryFocusRecoveryIdRef = useRef<string | null>(null);
  const receiptRetryButtonRef = useRef<HTMLButtonElement>(null);
  const terminalSnapshotRequestsRef = useRef(
    new Map<string, TerminalSnapshotRequest>(),
  );
  const terminalRefreshCompletedRef = useRef(new Set<string>());
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  const evidenceTriggerRef = useRef<HTMLButtonElement>(null);
  const hotelRestorationUnresolved =
    hotelResumeState === "checking" ||
    hotelResumeState === "retryable" ||
    hotelResumeState === "retrying";
  hotelSnapshotRef.current = hotelSnapshot;
  quotaSnapshotRef.current = quotaSnapshot;

  const activeScenario =
    recoveryScenarios.find((scenario) => scenario.id === activeId) ?? recoveryScenarios[0];

  const acceptHotelSnapshot = useCallback((snapshot: RecoverySnapshot) => {
    if (snapshot.scenarioId !== "hotel") return;
    writeHotelRecoveryHint(snapshot.recoveryId);
    setHotelSnapshot(snapshot);
  }, []);

  const beginPendingCreation = useCallback((
    scenarioId: ScenarioId,
    executionMode: RecoverySnapshot["executionMode"],
  ): PendingRecoveryCreation | null => {
    const intent = acquirePendingRecoveryCreation(scenarioId, executionMode);
    if (scenarioId === "hotel") {
      setHotelCreationErrorCode(null);
      if (intent === null) {
        setHotelStartError(
          "This browser could not preserve a safe recovery retry. No run was started.",
        );
      } else {
        setHotelRequiresExplicitRestart(false);
        setHotelCreationIntent(intent);
      }
    }
    return intent;
  }, []);

  const promoteHotelCreation = useCallback((
    intent: PendingRecoveryCreation,
    snapshot: RecoverySnapshot,
  ): boolean => {
    const current = readPendingRecoveryCreation("hotel");
    if (
      snapshot.scenarioId !== "hotel" ||
      snapshot.executionMode !== intent.executionMode ||
      current === null ||
      !pendingRecoveryCreationsMatch(current, intent) ||
      !writeHotelRecoveryHint(snapshot.recoveryId)
    ) {
      return false;
    }
    return clearMatchingPendingRecoveryCreation(intent);
  }, []);

  const clearHotelCreationState = useCallback((
    intent: PendingRecoveryCreation,
  ) => {
    setHotelCreationIntent((current) =>
      current !== null && pendingRecoveryCreationsMatch(current, intent)
        ? null
        : current,
    );
  }, []);

  const hotelResumeRequestIsCurrent = useCallback((request: HotelResumeRequest): boolean => (
    request.active &&
    hotelResumeRequestRef.current === request &&
    hotelStartGenerationRef.current === request.generation
  ), []);

  const retireHotelResumeRequest = useCallback((
    request: HotelResumeRequest,
    abort: boolean,
  ): boolean => {
    if (!request.active || hotelResumeRequestRef.current !== request) return false;
    request.active = false;
    if (request.deadlineId !== null) {
      clearTimeout(request.deadlineId);
      request.deadlineId = null;
    }
    hotelResumeRequestRef.current = null;
    if (abort && !request.controller.signal.aborted) request.controller.abort();
    return true;
  }, []);

  const abortHotelResumeRequest = useCallback(() => {
    const request = hotelResumeRequestRef.current;
    if (request === null) return;
    retireHotelResumeRequest(request, true);
  }, [retireHotelResumeRequest]);

  const makeHotelResumeRequestRetryable = useCallback((request: HotelResumeRequest) => {
    hotelResumeFocusGenerationRef.current = request.restoreFocusOnRetryable
      ? request.generation
      : null;
    setHotelSnapshot(null);
    setHotelResumeState("retryable");
  }, []);

  const restoreSavedHotelRecovery = useCallback((
    recoveryId: string,
    retry = false,
  ): HotelResumeRequest | null => {
    if (hotelResumeRequestRef.current?.active === true) return null;
    const controller = new AbortController();
    hotelResumeRecoveryIdRef.current = recoveryId;
    const generation = ++hotelStartGenerationRef.current;
    const request: HotelResumeRequest = {
      controller,
      deadlineId: null,
      generation,
      restoreFocusOnRetryable: retry,
      active: true,
    };
    hotelResumeRequestRef.current = request;
    hotelResumeFocusGenerationRef.current = null;
    setHotelSnapshot(null);
    setHotelResumeState(retry ? "retrying" : "checking");
    setReplayFallback(null);
    setReplayError(null);
    setSdkError(null);
    setHotelStartError(null);
    setHotelCreationErrorCode(null);

    request.deadlineId = setTimeout(() => {
      if (
        !hotelResumeRequestIsCurrent(request) ||
        !retireHotelResumeRequest(request, true)
      ) {
        return;
      }
      makeHotelResumeRequestRetryable(request);
    }, SAVED_RECOVERY_RESTORE_TIMEOUT_MS);

    void getRecovery(recoveryId, controller.signal)
      .then((snapshot) => {
        if (!hotelResumeRequestIsCurrent(request)) return;
        if (snapshot.scenarioId !== "hotel") {
          hotelResumeFocusGenerationRef.current = null;
          invalidResumeObservedRef.current = true;
          hotelResumeRecoveryIdRef.current = null;
          clearHotelRecoveryHint();
          setHotelResumeState("invalid");
          return;
        }
        hotelResumeFocusGenerationRef.current = null;
        const pendingCreation = readPendingRecoveryCreation("hotel");
        if (
          pendingCreation !== null &&
          pendingCreation.executionMode === snapshot.executionMode &&
          clearMatchingPendingRecoveryCreation(pendingCreation)
        ) {
          clearHotelCreationState(pendingCreation);
        }
        acceptHotelSnapshot(snapshot);
        setHotelResumeState("restored");
      })
      .catch((error: unknown) => {
        if (!hotelResumeRequestIsCurrent(request)) return;
        if (error instanceof DOMException && error.name === "AbortError") {
          makeHotelResumeRequestRetryable(request);
          return;
        }
        if (
          error instanceof RecoveryLookupError &&
          error.disposition === "terminal"
        ) {
          hotelResumeFocusGenerationRef.current = null;
          invalidResumeObservedRef.current = true;
          hotelResumeRecoveryIdRef.current = null;
          clearHotelRecoveryHint();
          setHotelResumeState("invalid");
          return;
        }
        makeHotelResumeRequestRetryable(request);
      })
      .finally(() => {
        retireHotelResumeRequest(request, false);
      });

    return request;
  }, [
    acceptHotelSnapshot,
    clearHotelCreationState,
    hotelResumeRequestIsCurrent,
    makeHotelResumeRequestRetryable,
    retireHotelResumeRequest,
  ]);

  useEffect(() => {
    if (hotelResumeState !== "retryable") return;
    const focusGeneration = hotelResumeFocusGenerationRef.current;
    if (focusGeneration === null) return;
    hotelResumeFocusGenerationRef.current = null;
    if (hotelStartGenerationRef.current !== focusGeneration) return;
    if (document.activeElement !== document.body) return;
    hotelResumeRetryButtonRef.current?.focus();
  }, [hotelResumeState]);

  const retireReceiptRequest = useCallback((
    recoveryId: string,
    request: ReceiptRequest,
    abort: boolean,
  ): boolean => {
    if (
      !request.active ||
      receiptRequestsRef.current.get(recoveryId) !== request
    ) {
      return false;
    }
    request.active = false;
    if (request.deadlineId !== null) {
      clearTimeout(request.deadlineId);
      request.deadlineId = null;
    }
    receiptRequestsRef.current.delete(recoveryId);
    if (abort && !request.controller.signal.aborted) request.controller.abort();
    return true;
  }, []);

  const abortReceiptRequests = useCallback(() => {
    for (const [recoveryId, request] of receiptRequestsRef.current) {
      retireReceiptRequest(recoveryId, request, true);
    }
  }, [retireReceiptRequest]);

  const loadReceipt = useCallback((snapshot: RecoverySnapshot, explicitRetry = false) => {
    const { recoveryId } = snapshot;
    const bindingKey = receiptSnapshotKey(snapshot);
    const existingRequest = receiptRequestsRef.current.get(recoveryId);
    if (existingRequest?.active === true) {
      if (existingRequest.bindingKey === bindingKey) return;
      retireReceiptRequest(recoveryId, existingRequest, true);
    }
    const completedBinding = completedReceiptBindingsRef.current.get(recoveryId);
    if (completedBinding === bindingKey) return;
    if (completedBinding !== undefined) {
      completedReceiptBindingsRef.current.delete(recoveryId);
      setReceipts((current) => {
        const next = { ...current };
        delete next[recoveryId];
        return next;
      });
    }
    const request: ReceiptRequest = {
      controller: new AbortController(),
      deadlineId: null,
      bindingKey,
      restoreFocusOnFailure: explicitRetry,
      active: true,
    };
    receiptRequestsRef.current.set(recoveryId, request);
    setReceiptLoading((current) => ({ ...current, [recoveryId]: true }));
    setReceiptErrors((current) => {
      const next = { ...current };
      delete next[recoveryId];
      return next;
    });

    request.deadlineId = setTimeout(() => {
      if (!retireReceiptRequest(recoveryId, request, true)) return;
      if (request.restoreFocusOnFailure) {
        receiptRetryFocusRecoveryIdRef.current = recoveryId;
      }
      setReceiptErrors((current) => ({
        ...current,
        [recoveryId]: RECEIPT_LOAD_ERROR,
      }));
      setReceiptLoading((current) => ({ ...current, [recoveryId]: false }));
    }, RECEIPT_REQUEST_TIMEOUT_MS);

    void getReceipt(recoveryId, request.controller.signal)
      .then((receipt) => {
        const currentSnapshot =
          snapshot.scenarioId === "hotel"
            ? hotelSnapshotRef.current
            : quotaSnapshotRef.current;
        if (
          currentSnapshot === null ||
          currentSnapshot.recoveryId !== recoveryId ||
          receiptSnapshotKey(currentSnapshot) !== request.bindingKey ||
          !receiptMatchesSnapshot(receipt, currentSnapshot)
        ) {
          throw new Error("Receipt does not match the authoritative recovery snapshot");
        }
        if (!retireReceiptRequest(recoveryId, request, false)) return;
        completedReceiptBindingsRef.current.set(recoveryId, request.bindingKey);
        setReceipts((current) => ({ ...current, [recoveryId]: receipt }));
        setReceiptLoading((current) => ({ ...current, [recoveryId]: false }));
      })
      .catch(() => {
        if (!retireReceiptRequest(recoveryId, request, false)) return;
        if (request.restoreFocusOnFailure) {
          receiptRetryFocusRecoveryIdRef.current = recoveryId;
        }
        setReceiptErrors((current) => ({
          ...current,
          [recoveryId]: RECEIPT_LOAD_ERROR,
        }));
        setReceiptLoading((current) => ({ ...current, [recoveryId]: false }));
      });
  }, [retireReceiptRequest]);

  const retireTerminalSnapshotRequest = useCallback((
    recoveryId: string,
    request: TerminalSnapshotRequest,
    abort: boolean,
  ): boolean => {
    if (
      !request.active ||
      terminalSnapshotRequestsRef.current.get(recoveryId) !== request
    ) {
      return false;
    }
    request.active = false;
    if (request.deadlineId !== null) {
      clearTimeout(request.deadlineId);
      request.deadlineId = null;
    }
    terminalSnapshotRequestsRef.current.delete(recoveryId);
    if (abort && !request.controller.signal.aborted) request.controller.abort();
    return true;
  }, []);

  const abortTerminalSnapshotRequests = useCallback(() => {
    for (const [recoveryId, request] of terminalSnapshotRequestsRef.current) {
      retireTerminalSnapshotRequest(recoveryId, request, true);
    }
  }, [retireTerminalSnapshotRequest]);

  const loadTerminalSnapshot = useCallback((
    sourceSnapshot: RecoverySnapshot,
    {
      explicitRetry = false,
      refreshKey = null,
    }: {
      explicitRetry?: boolean;
      refreshKey?: string | null;
    } = {},
  ) => {
    const { recoveryId, scenarioId } = sourceSnapshot;
    if (terminalSnapshotRequestsRef.current.get(recoveryId)?.active === true) {
      return;
    }
    const request: TerminalSnapshotRequest = {
      controller: new AbortController(),
      deadlineId: null,
      refreshKey,
      restoreFocusOnFailure: explicitRetry,
      active: true,
    };
    terminalSnapshotRequestsRef.current.set(recoveryId, request);
    if (explicitRetry) {
      setReceiptLoading((current) => ({ ...current, [recoveryId]: true }));
      setReceiptErrors((current) => {
        const next = { ...current };
        delete next[recoveryId];
        return next;
      });
    }

    const failCurrentRequest = () => {
      if (!retireTerminalSnapshotRequest(recoveryId, request, true)) return;
      if (request.restoreFocusOnFailure) {
        receiptRetryFocusRecoveryIdRef.current = recoveryId;
      }
      setReceiptErrors((current) => ({
        ...current,
        [recoveryId]: RECEIPT_LOAD_ERROR,
      }));
      setReceiptLoading((current) => ({
        ...current,
        [recoveryId]: false,
      }));
    };

    request.deadlineId = setTimeout(
      failCurrentRequest,
      RECEIPT_REQUEST_TIMEOUT_MS,
    );
    void getRecovery(recoveryId, request.controller.signal)
      .then((snapshot) => {
        if (
          !request.active ||
          terminalSnapshotRequestsRef.current.get(recoveryId) !== request
        ) {
          return;
        }
        const currentSnapshot =
          scenarioId === "hotel"
            ? hotelSnapshotRef.current
            : quotaSnapshotRef.current;
        if (
          !appMountedRef.current ||
          currentSnapshot?.recoveryId !== recoveryId ||
          snapshot.scenarioId !== scenarioId ||
          !isTerminalRecoveryStatus(snapshot.status)
        ) {
          throw new Error(
            "Terminal evidence refresh did not match the current recovery",
          );
        }
        if (!retireTerminalSnapshotRequest(recoveryId, request, false)) return;
        if (request.refreshKey !== null) {
          terminalRefreshCompletedRef.current.add(request.refreshKey);
        }
        if (scenarioId === "hotel") {
          hotelSnapshotRef.current = snapshot;
          setHotelSnapshot((current) =>
            current?.recoveryId === recoveryId ? snapshot : current,
          );
        } else {
          quotaSnapshotRef.current = snapshot;
          setQuotaSnapshot((current) =>
            current?.recoveryId === recoveryId ? snapshot : current,
          );
        }
        void loadReceipt(snapshot, explicitRetry);
      })
      .catch(() => {
        failCurrentRequest();
      });
  }, [loadReceipt, retireTerminalSnapshotRequest]);

  const startQuota = useCallback(async () => {
    if (quotaStartedRef.current) return;
    const intent = beginPendingCreation("api-quota", "sdk_stub");
    if (intent === null) {
      setQuotaError(
        "This browser could not preserve a safe quota retry. No run was started.",
      );
      return;
    }
    quotaStartedRef.current = true;
    setQuotaLoading(true);
    setQuotaError(null);
    setQuotaCreationErrorCode(null);
    try {
      const snapshot = await createRecovery(
        "api-quota",
        "sdk_stub",
        intent.clientRequestId,
      );
      if (!appMountedRef.current) return;
      setQuotaSnapshot(snapshot);
      clearMatchingPendingRecoveryCreation(intent);
      setQuotaConflictingIntent(null);
    } catch (error: unknown) {
      if (!appMountedRef.current) return;
      quotaStartedRef.current = false;
      const creationCode =
        error instanceof RecoveryCreationError ? error.code : null;
      setQuotaCreationErrorCode(creationCode);
      setQuotaConflictingIntent(
        creationCode === "idempotency_conflict" ? intent : null,
      );
      setQuotaError(
        error instanceof RecoveryCreationError
          ? error.message
          : "The deterministic quota trace could not be loaded.",
      );
    } finally {
      if (appMountedRef.current) setQuotaLoading(false);
    }
  }, [beginPendingCreation]);

  const startNewQuotaAfterConflict = useCallback(() => {
    if (
      quotaCreationErrorCode !== "idempotency_conflict" ||
      quotaConflictingIntent === null ||
      !clearMatchingPendingRecoveryCreation(quotaConflictingIntent)
    ) {
      return;
    }
    setQuotaConflictingIntent(null);
    setQuotaCreationErrorCode(null);
    setQuotaError(null);
    void startQuota();
  }, [
    quotaConflictingIntent,
    quotaCreationErrorCode,
    startQuota,
  ]);

  const selectScenario = useCallback(
    (scenarioId: ScenarioId) => {
      if (hotelRestorationUnresolved || hotelCreationIntent !== null) return;
      setActiveId(scenarioId);
      setEvidenceOpen(false);
      if (scenarioId === "api-quota" && quotaSnapshot === null && !quotaLoading) {
        void startQuota();
      }
    },
    [
      hotelCreationIntent,
      hotelRestorationUnresolved,
      quotaLoading,
      quotaSnapshot,
      startQuota,
    ],
  );

  const startReplay = useCallback(async (
    signal?: AbortSignal,
    explicit = false,
    fromExplicitDemoChoice = false,
  ) => {
    if (hotelRestorationUnresolved || hotelDemoStartInFlightRef.current) return;
    const intent = beginPendingCreation("hotel", "replay_fixture");
    if (intent === null) return;
    hotelDemoStartInFlightRef.current = true;
    const generation = ++hotelStartGenerationRef.current;
    setSdkLoading(false);
    setSdkError(null);
    setReplayLoading(true);
    setReplayError(null);
    setHotelStartError(null);
    if (fromExplicitDemoChoice) setReplayFallback(AWAITING_DEMO_NOTICE);
    try {
      const snapshot = await createRecovery(
        "hotel",
        "replay_fixture",
        intent.clientRequestId,
        signal,
      );
      const promoted = promoteHotelCreation(intent, snapshot);
      if (hotelStartGenerationRef.current !== generation) return;
      if (promoted) clearHotelCreationState(intent);
      acceptHotelSnapshot(snapshot);
      if (explicit) setReplayFallback(EXPLICIT_REPLAY_NOTICE);
    } catch (error: unknown) {
      if (hotelStartGenerationRef.current !== generation) return;
      if (error instanceof DOMException && error.name === "AbortError") return;
      if (fromExplicitDemoChoice) setReplayFallback(EXPLICIT_DEMO_NOTICE);
      const message =
        error instanceof RecoveryCreationError
          ? error.message
          : "The replay fixture could not be started. You can retry explicitly.";
      setHotelCreationErrorCode(
        error instanceof RecoveryCreationError ? error.code : null,
      );
      setReplayError(message);
      setHotelStartError(message);
    } finally {
      if (hotelStartGenerationRef.current === generation) {
        hotelDemoStartInFlightRef.current = false;
        setReplayLoading(false);
      }
    }
  }, [
    acceptHotelSnapshot,
    beginPendingCreation,
    clearHotelCreationState,
    hotelRestorationUnresolved,
    promoteHotelCreation,
  ]);

  const startSdkQa = useCallback(async () => {
    if (
      sdkLoading ||
      hotelRestorationUnresolved ||
      hotelDemoStartInFlightRef.current
    ) {
      return;
    }
    const intent = beginPendingCreation("hotel", "sdk_stub");
    if (intent === null) return;
    hotelDemoStartInFlightRef.current = true;
    const fromExplicitDemoChoice = replayFallback === EXPLICIT_DEMO_NOTICE;
    const generation = ++hotelStartGenerationRef.current;
    setReplayLoading(false);
    setReplayError(null);
    setSdkLoading(true);
    setSdkError(null);
    setHotelStartError(null);
    if (fromExplicitDemoChoice) setReplayFallback(AWAITING_DEMO_NOTICE);
    try {
      const snapshot = await createRecovery(
        "hotel",
        "sdk_stub",
        intent.clientRequestId,
      );
      const promoted = promoteHotelCreation(intent, snapshot);
      if (hotelStartGenerationRef.current !== generation) return;
      if (promoted) clearHotelCreationState(intent);
      acceptHotelSnapshot(snapshot);
      if (replayFallback !== null) setReplayFallback(SDK_QA_NOTICE);
      setEvidenceOpen(false);
    } catch (error: unknown) {
      if (hotelStartGenerationRef.current !== generation) return;
      if (fromExplicitDemoChoice) setReplayFallback(EXPLICIT_DEMO_NOTICE);
      const message =
        error instanceof RecoveryCreationError
          ? error.message
          : "The deterministic SDK QA trace could not be started.";
      setHotelCreationErrorCode(
        error instanceof RecoveryCreationError ? error.code : null,
      );
      setSdkError(message);
      setHotelStartError(message);
    } finally {
      if (hotelStartGenerationRef.current === generation) {
        hotelDemoStartInFlightRef.current = false;
        setSdkLoading(false);
      }
    }
  }, [
    acceptHotelSnapshot,
    beginPendingCreation,
    clearHotelCreationState,
    hotelRestorationUnresolved,
    promoteHotelCreation,
    replayFallback,
    sdkLoading,
  ]);

  const startLive = useCallback(() => {
    const retryingPendingLive =
      hotelCreationIntent?.executionMode === "openai_live";
    if (
      hotelLiveStartInFlightRef.current ||
      hotelRestorationUnresolved ||
      (!retryingPendingLive &&
        (health?.backend !== "openai" || !health.liveReady))
    ) {
      return;
    }
    const intent = beginPendingCreation("hotel", "openai_live");
    if (intent === null) return;
    hotelLiveStartInFlightRef.current = true;
    const generation = ++hotelStartGenerationRef.current;
    const controller = new AbortController();
    hotelLiveControllerRef.current = controller;
    setHotelLiveLoading(true);
    setHotelStartError(null);
    setReplayError(null);
    setReplayFallback(null);

    void createRecovery(
      "hotel",
      "openai_live",
      intent.clientRequestId,
      controller.signal,
    )
      .then((snapshot) => {
        const promoted = promoteHotelCreation(intent, snapshot);
        if (hotelStartGenerationRef.current !== generation) return;
        if (promoted) clearHotelCreationState(intent);
        acceptHotelSnapshot(snapshot);
        setEvidenceOpen(false);
      })
      .catch((error: unknown) => {
        if (hotelStartGenerationRef.current !== generation) return;
        if (error instanceof DOMException && error.name === "AbortError") return;
        if (error instanceof LiveAdmissionError) {
          clearMatchingPendingRecoveryCreation(intent);
          clearHotelCreationState(intent);
          setReplayFallback(error.message);
          hotelLiveStartInFlightRef.current = false;
          hotelLiveControllerRef.current = null;
          setHotelLiveLoading(false);
          void startReplay();
          return;
        }
        setHotelStartError(
          error instanceof RecoveryCreationError
            ? error.message
            : "The live recovery could not be started. No fallback run was created.",
        );
        setHotelCreationErrorCode(
          error instanceof RecoveryCreationError ? error.code : null,
        );
      })
      .finally(() => {
        if (hotelLiveControllerRef.current !== controller) return;
        hotelLiveControllerRef.current = null;
        hotelLiveStartInFlightRef.current = false;
        if (hotelStartGenerationRef.current === generation) setHotelLiveLoading(false);
      });
  }, [
    acceptHotelSnapshot,
    beginPendingCreation,
    clearHotelCreationState,
    health,
    hotelCreationIntent,
    hotelRestorationUnresolved,
    promoteHotelCreation,
    startReplay,
  ]);

  const retryPendingHotelCreation = useCallback(() => {
    if (hotelCreationIntent === null) return;
    switch (hotelCreationIntent.executionMode) {
      case "openai_live":
        startLive();
        return;
      case "replay_fixture":
        void startReplay(undefined, true);
        return;
      case "sdk_stub":
        void startSdkQa();
    }
  }, [hotelCreationIntent, startLive, startReplay, startSdkQa]);

  const abandonConflictingHotelCreation = useCallback(() => {
    if (
      hotelCreationErrorCode !== "idempotency_conflict" ||
      hotelCreationIntent === null ||
      !clearMatchingPendingRecoveryCreation(hotelCreationIntent)
    ) {
      return;
    }
    clearHotelCreationState(hotelCreationIntent);
    setHotelCreationErrorCode(null);
    setHotelRequiresExplicitRestart(true);
    setHotelStartError(null);
    setReplayError(null);
    setSdkError(null);
    setReplayFallback(
      health?.backend === "openai" && health.liveReady
        ? null
        : EXPLICIT_DEMO_NOTICE,
    );
  }, [
    clearHotelCreationState,
    health,
    hotelCreationErrorCode,
    hotelCreationIntent,
  ]);

  useEffect(() => {
    const { hadHint, recoveryId } = readHotelRecoveryHint();
    if (!hadHint) {
      hotelResumeFocusGenerationRef.current = null;
      setHotelResumeState(invalidResumeObservedRef.current ? "invalid" : "none");
      return;
    }
    if (recoveryId === null) {
      hotelResumeFocusGenerationRef.current = null;
      invalidResumeObservedRef.current = true;
      setHotelResumeState("invalid");
      return;
    }

    const request = restoreSavedHotelRecovery(recoveryId);
    return () => {
      if (request !== null) retireHotelResumeRequest(request, true);
    };
  }, [restoreSavedHotelRecovery, retireHotelResumeRequest]);

  useEffect(() => {
    if (
      health === null ||
      hotelRestorationUnresolved ||
      hotelCreationIntent !== null ||
      hotelResumeState === "restored" ||
      hotelSnapshot !== null
    ) {
      return;
    }
    const controller = new AbortController();
    const liveAvailable = health.backend === "openai" && health.liveReady;
    if (hotelResumeState === "invalid") {
      setHotelSnapshot(null);
      setReplayError(null);
      setHotelStartError(null);
      setReplayFallback(liveAvailable ? null : EXPLICIT_DEMO_NOTICE);
      return () => controller.abort();
    }
    if (hotelRequiresExplicitRestart) {
      setHotelSnapshot(null);
      setReplayError(null);
      setHotelStartError(null);
      setReplayFallback(liveAvailable ? null : EXPLICIT_DEMO_NOTICE);
      return () => controller.abort();
    }
    if (!liveAvailable) {
      setHotelSnapshot(null);
      setReplayFallback(LIVE_ADMISSION_MESSAGES.live_unavailable);
      void startReplay(controller.signal);
      return () => controller.abort();
    }

    setReplayFallback(null);
    setReplayError(null);
    setHotelStartError(null);
    return () => controller.abort();
  }, [
    health,
    hotelCreationIntent,
    hotelRequiresExplicitRestart,
    hotelSnapshot,
    hotelRestorationUnresolved,
    hotelResumeState,
    startReplay,
  ]);

  useEffect(() => {
    appMountedRef.current = true;
    return () => {
      appMountedRef.current = false;
      hotelStartGenerationRef.current += 1;
      hotelLiveStartInFlightRef.current = false;
      hotelDemoStartInFlightRef.current = false;
      hotelLiveControllerRef.current?.abort();
      hotelLiveControllerRef.current = null;
      hotelResumeFocusGenerationRef.current = null;
      receiptRetryFocusRecoveryIdRef.current = null;
      abortHotelResumeRequest();
      abortReceiptRequests();
      abortTerminalSnapshotRequests();
    };
  }, [
    abortHotelResumeRequest,
    abortReceiptRequests,
    abortTerminalSnapshotRequests,
  ]);

  useEffect(() => {
    const terminalSnapshots = [hotelSnapshot, quotaSnapshot];
    for (const snapshot of terminalSnapshots) {
      if (snapshot !== null && isTerminalRecoveryStatus(snapshot.status)) {
        void loadReceipt(snapshot);
      }
    }
  }, [hotelSnapshot, loadReceipt, quotaSnapshot]);

  const activeSnapshot = activeId === "hotel" ? hotelSnapshot : quotaSnapshot;
  const hotelLifecyclePhase =
    activeId !== "hotel" || activeSnapshot !== null
      ? "active"
      : hotelRestorationUnresolved ||
          hotelCreationIntent !== null ||
          hotelLiveLoading ||
          replayLoading ||
          sdkLoading
        ? "awaiting"
        : "idle";
  const activeRecoveryId = activeSnapshot?.recoveryId ?? null;
  const eventState = useRecoveryEvents(activeRecoveryId);
  const activeEvents = useMemo(
    () => eventState.events.filter((event) => event.recoveryId === activeRecoveryId),
    [activeRecoveryId, eventState.events],
  );
  const terminalEvent = [...activeEvents].reverse().find((event) => event.terminal);

  useEffect(() => {
    if (terminalEvent === undefined || activeSnapshot === null) return;
    const refreshKey = `${terminalEvent.recoveryId}:${terminalEvent.seq}`;
    if (terminalRefreshCompletedRef.current.has(refreshKey)) return;
    if (isTerminalRecoveryStatus(activeSnapshot.status)) {
      void loadReceipt(activeSnapshot);
    }
    loadTerminalSnapshot(activeSnapshot, { refreshKey });
  }, [activeSnapshot, loadReceipt, loadTerminalSnapshot, terminalEvent]);

  const activeScenarioView = useMemo<RecoveryScenario>(() => {
    if (activeSnapshot !== null) {
      return {
        ...activeScenario,
        executionMode: activeSnapshot.executionMode,
        status: activeSnapshot.status,
        currentStep: activeSnapshot.currentStep,
        currentStepSummary: activeSnapshot.currentStepSummary,
        lifecycleDetails: serverLifecycleDetails(activeSnapshot),
      };
    }
    if (activeId === "hotel") return HOTEL_IDLE_SCENARIO;
    const waiting = quotaLoading
      ? "Starting the deterministic quota SDK trace."
      : "No server quota proof is available.";
    return {
      ...activeScenario,
      executionMode: "sdk_stub",
      status: "in_progress",
      currentStep: 0,
      currentStepSummary: waiting,
      lifecycleDetails: {
        Detect: waiting,
        Prove: "Waiting for provider ceiling evidence.",
        Negotiate: "Waiting for exact temporary burst terms.",
        Authorize: "Waiting for delegated-authority evidence.",
        Execute: "No quota adapter execution has been recorded.",
        "Verify & seal": "No terminal quota receipt is available.",
      },
      evidence: [
        { label: "Execution mode", value: "sdk_stub", monospace: true },
        { label: "Server evidence", value: quotaLoading ? "Loading" : "Unavailable" },
      ],
    };
  }, [activeId, activeScenario, activeSnapshot, quotaLoading]);

  const refreshHotelSnapshot = useCallback(async () => {
    if (hotelSnapshot === null) return;
    const requestedRecoveryId = hotelSnapshot.recoveryId;
    const snapshot = await getRecovery(requestedRecoveryId);
    if (snapshot.scenarioId !== "hotel") return;
    setHotelSnapshot((current) =>
      current?.recoveryId === requestedRecoveryId ? snapshot : current,
    );
    if (isTerminalRecoveryStatus(snapshot.status)) {
      void loadReceipt(snapshot);
    }
  }, [hotelSnapshot, loadReceipt]);

  const presentation = useMemo(
    () => health === null || activeSnapshot === null
      ? null
      : deriveRuntimePresentation(health, activeSnapshot),
    [activeSnapshot, health],
  );
  const storedActiveReceipt =
    activeSnapshot === null ? null : receipts[activeSnapshot.recoveryId] ?? null;
  const activeReceipt =
    activeSnapshot !== null &&
    storedActiveReceipt !== null &&
    receiptMatchesSnapshot(storedActiveReceipt, activeSnapshot)
      ? storedActiveReceipt
      : null;
  const activeReceiptLoading =
    activeSnapshot === null ? false : receiptLoading[activeSnapshot.recoveryId] === true;
  const activeReceiptError =
    activeSnapshot === null ? null : receiptErrors[activeSnapshot.recoveryId] ?? null;

  useEffect(() => {
    const recoveryId = receiptRetryFocusRecoveryIdRef.current;
    if (
      recoveryId === null ||
      receiptLoading[recoveryId] === true ||
      receiptErrors[recoveryId] === undefined
    ) {
      return;
    }
    receiptRetryFocusRecoveryIdRef.current = null;
    if (activeRecoveryId !== recoveryId) return;
    if (document.activeElement !== document.body) return;
    receiptRetryButtonRef.current?.focus();
  }, [activeRecoveryId, receiptErrors, receiptLoading]);

  const evidenceLabel =
    terminalEvent !== undefined
      ? "Review recovery receipt"
      : activeSnapshot?.pendingApproval !== null && activeSnapshot?.pendingApproval !== undefined
      ? "Review exact remedy"
      : activeReceipt !== null || (activeSnapshot !== null && isTerminalRecoveryStatus(activeSnapshot.status))
        ? "Review recovery receipt"
        : "Review recovery evidence";
  const quotaEmptyState =
    activeId === "api-quota" && activeSnapshot === null
      ? {
          title: quotaLoading ? "Loading deterministic trace" : "Quota proof unavailable",
          summary: activeScenarioView.currentStepSummary,
          boundaryTitle: "No completed quota receipt is being shown.",
          boundaryText: "Only server-returned SDK evidence can complete this scenario.",
        }
      : null;
  const hotelEmptyState =
    activeId === "hotel" && activeSnapshot === null
      ? {
          title:
            hotelLifecyclePhase === "awaiting"
              ? "Awaiting server evidence"
              : "No recovery started",
          summary:
            hotelLifecyclePhase === "awaiting"
              ? "Awaiting server evidence."
              : "No server run started.",
          boundaryTitle:
            hotelLifecyclePhase === "awaiting"
              ? "No execution-mode claim is available yet."
              : "No active recovery evidence is being shown.",
          boundaryText:
            hotelLifecyclePhase === "awaiting"
              ? "Waiting for an authoritative server recovery snapshot."
              : "Start a live recovery explicitly, or use a disclosed replay or SDK QA action when available.",
        }
      : null;
  const liveAvailable = health?.backend === "openai" && health.liveReady;

  return (
    <div className="app-frame">
      <AppHeader workspace={workspaceLabel(activeSnapshot?.executionMode)} />
      <div className="console-shell" id="workspace">
        <ScenarioRail
          scenarios={recoveryScenarios}
          activeId={activeId}
          disabled={
            hotelRestorationUnresolved || hotelCreationIntent !== null
          }
          mobile={mobile}
          onSelect={selectScenario}
        />

        <main className="workspace">
          <ProvenanceStrip
            presentation={presentation}
            healthPhase={healthPhase}
            onRetryRuntime={retryRuntimeHealth}
            awaitingSnapshot={
              activeId === "api-quota" && health !== null && activeSnapshot === null
            }
            awaitingServerEvidence={
              activeId === "hotel" && hotelLifecyclePhase === "awaiting"
            }
            noRunStarted={activeId === "hotel" && hotelLifecyclePhase === "idle"}
          />
          {activeId === "hotel" &&
          hotelCreationIntent !== null &&
          !hotelRestorationUnresolved &&
          activeSnapshot === null ? (
            <section className="live-start" aria-live="polite">
              <div>
                <p className="eyebrow">Unresolved recovery start</p>
                <p>
                  {hotelCreationErrorCode === "idempotency_conflict"
                    ? "This local recovery start cannot be reused. You can abandon it and return to the available start choices."
                    : "No accepted server snapshot is available yet. Retry the same recovery start explicitly."}
                </p>
              </div>
              {hotelCreationErrorCode === "idempotency_conflict" ? (
                <button
                  type="button"
                  onClick={abandonConflictingHotelCreation}
                >
                  Start a new recovery
                </button>
              ) : (
                <button
                  type="button"
                  disabled={hotelLiveLoading || replayLoading || sdkLoading}
                  onClick={retryPendingHotelCreation}
                >
                  {hotelLiveLoading || replayLoading || sdkLoading
                    ? "Retrying recovery start…"
                    : "Retry recovery start"}
                </button>
              )}
            </section>
          ) : null}
          {activeId === "hotel" &&
          liveAvailable &&
          !hotelRestorationUnresolved &&
          hotelCreationIntent === null &&
          activeSnapshot === null &&
          replayFallback === null ? (
            <section className="live-start" aria-live="polite">
              <div>
                <p className="eyebrow">Live recovery</p>
                <p>
                  {hotelLiveLoading
                    ? "Waiting for an authoritative live recovery snapshot."
                    : "No server run starts until you explicitly request one."}
                </p>
              </div>
              <button
                type="button"
                disabled={hotelLiveLoading}
                onClick={startLive}
              >
                {hotelLiveLoading ? "Starting live recovery…" : "Start live recovery"}
              </button>
            </section>
          ) : null}
          {activeId === "hotel" &&
          (hotelResumeState === "retryable" || hotelResumeState === "retrying") ? (
            <section
              className="live-start"
              aria-busy={hotelResumeState === "retrying"}
              aria-live="polite"
            >
              <div>
                <p className="eyebrow">Saved recovery</p>
                <p>{RETRYABLE_RESUME_NOTICE}</p>
              </div>
              <button
                ref={hotelResumeRetryButtonRef}
                type="button"
                disabled={hotelResumeState === "retrying"}
                onClick={() => {
                  const recoveryId = hotelResumeRecoveryIdRef.current;
                  if (recoveryId !== null) restoreSavedHotelRecovery(recoveryId, true);
                }}
              >
                {hotelResumeState === "retrying"
                  ? "Retrying saved recovery…"
                  : "Retry saved recovery"}
              </button>
            </section>
          ) : null}
          {activeId === "hotel" &&
          replayFallback !== null &&
          hotelCreationIntent === null ? (
            <section className="replay-fallback" aria-live="polite">
              <div>
                <p className="eyebrow">
                  {replayFallback === EXPLICIT_DEMO_NOTICE ||
                  replayFallback === AWAITING_DEMO_NOTICE
                    ? "Available demo modes"
                    : "Replay fallback"}
                </p>
                <p>{replayFallback}</p>
                {replayError === null ? null : <p role="alert">{replayError}</p>}
                {sdkError === null ? null : <p role="alert">{sdkError}</p>}
              </div>
              <div className="fallback-actions">
                <button
                  type="button"
                  aria-label="Run replay fixture"
                  disabled={replayLoading || sdkLoading}
                  onClick={() =>
                    void startReplay(
                      undefined,
                      true,
                      replayFallback === EXPLICIT_DEMO_NOTICE,
                    )
                  }
                >
                  {replayLoading ? "Loading replay fixture…" : "Run replay fixture"}
                </button>
                <button
                  type="button"
                  disabled={
                    sdkLoading ||
                    (replayLoading && replayFallback === AWAITING_DEMO_NOTICE)
                  }
                  onClick={() => void startSdkQa()}
                >
                  {sdkLoading ? "Starting SDK QA trace…" : "Run SDK QA trace"}
                </button>
              </div>
            </section>
          ) : null}
          {activeId === "api-quota" && quotaError !== null ? (
            <section className="live-start" aria-live="polite">
              <p role="alert">{quotaError}</p>
              <button
                type="button"
                disabled={quotaLoading}
                onClick={
                  quotaCreationErrorCode === "idempotency_conflict"
                    ? startNewQuotaAfterConflict
                    : () => void startQuota()
                }
              >
                {quotaCreationErrorCode === "idempotency_conflict"
                  ? "Start a new quota recovery"
                  : quotaLoading
                    ? "Retrying quota recovery…"
                    : "Retry quota recovery"}
              </button>
            </section>
          ) : null}
          {activeId === "hotel" && hotelStartError !== null ? <p role="alert">{hotelStartError}</p> : null}
          <section className="recovery-heading" aria-labelledby="recovery-title">
            <div>
              <p className="eyebrow">Active recovery</p>
              <h1 id="recovery-title">{activeScenarioView.title}</h1>
              {activeSnapshot !== null && isTerminalRecoveryStatus(activeSnapshot.status) ? (
                <p className="terminal-summary">{activeSnapshot.currentStepSummary}</p>
              ) : null}
            </div>
            <span
              className={`recovery-state recovery-state--${
                activeSnapshot?.status ?? activeScenarioView.status
              }`}
            >
              {activeSnapshot !== null
                ? serverStatusLabel(activeSnapshot.status, activeSnapshot.pendingApproval !== null)
                : activeId === "hotel"
                  ? hotelLifecyclePhase === "awaiting"
                    ? "Awaiting evidence"
                    : "Not started"
                : activeScenarioView.status === "completed"
                  ? "Completed fixture"
                  : "Awaiting boundary"}
            </span>
          </section>
          <Lifecycle phase={hotelLifecyclePhase} scenario={activeScenarioView} />
        </main>

        {mobile ? (
          <button
            ref={evidenceTriggerRef}
            className="evidence-trigger"
            type="button"
            onClick={() => setEvidenceOpen(true)}
          >
            {evidenceLabel}
          </button>
        ) : null}

        <EvidenceInspector
          scenario={activeScenarioView}
          snapshot={activeSnapshot}
          receipt={activeReceipt}
          events={activeEvents}
          receiptLoading={activeReceiptLoading}
          receiptError={activeReceiptError}
          retryReceiptButtonRef={receiptRetryButtonRef}
          terminalEventObserved={terminalEvent !== undefined}
          emptyState={hotelEmptyState ?? quotaEmptyState}
          mobile={mobile}
          open={mobile ? evidenceOpen : true}
          onClose={() => setEvidenceOpen(false)}
          onRetryReceipt={
            activeSnapshot === null
              ? undefined
              : () => {
                  const refreshKey =
                    terminalEvent?.recoveryId === activeSnapshot.recoveryId
                      ? `${terminalEvent.recoveryId}:${terminalEvent.seq}`
                      : null;
                  loadTerminalSnapshot(activeSnapshot, {
                    explicitRetry: true,
                    refreshKey,
                  });
                }
          }
          returnFocusRef={evidenceTriggerRef}
          onServerSuccess={refreshHotelSnapshot}
        />

        <EventLedger
          events={activeEvents}
          error={eventState.error}
          mobile={mobile}
          rootTraceId={activeSnapshot?.rootTraceId ?? null}
        />
      </div>
    </div>
  );
}
