import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import {
  createRecovery,
  getHealth,
  postDecision,
  PublicApiError,
} from "./api/client";
import { AppHeader } from "./components/AppHeader";
import { ConsentSheet } from "./components/ConsentSheet";
import { EvidenceInspector } from "./components/EvidenceInspector";
import { EventLedger } from "./components/EventLedger";
import { Lifecycle } from "./components/Lifecycle";
import { ProvenanceStrip } from "./components/ProvenanceStrip";
import { ScenarioRail } from "./components/ScenarioRail";
import {
  isTerminalRecoveryStatus,
  type DecisionRequest,
  type PendingApproval,
  type RecoveryScenario,
  type RecoverySnapshot,
  type ScenarioId,
} from "./domain/recovery";
import {
  deriveRuntimePresentation,
  type ExecutionMode,
  type HealthStatus,
} from "./domain/runtime";
import {
  clearPendingDecisionForRecovery,
  persistActiveHotelRecovery,
  readActiveHotelRecovery,
  readPendingDecision,
} from "./domain/session";
import {
  hotelReplayCompletedPresentation,
  hotelSdkAuthorizationExpiredPresentation,
  hotelSdkAuthorizationUnknownPresentation,
  hotelSdkClosedPresentation,
  hotelSdkCompletedPresentation,
  quotaReplayCompletedPresentation,
  quotaSdkCompletedPresentation,
  recoveryScenarios,
} from "./fixtures/recoveries";
import { useRecovery } from "./hooks/useRecovery";

let activeHotelCreation: Promise<RecoverySnapshot> | null = null;

function createHotelRecoveryOnce(): Promise<RecoverySnapshot> {
  if (activeHotelCreation === null) {
    activeHotelCreation = createRecovery("hotel", "sdk_stub");
  }
  return activeHotelCreation;
}

function requestMatchesApproval(
  request: DecisionRequest,
  approval: PendingApproval,
): boolean {
  return (
    request.remedyId === approval.remedyId &&
    request.remedyDigest === approval.remedyDigest &&
    request.toolCallId === approval.toolCallId
  );
}

function recoveryStateLabel(snapshot: RecoverySnapshot | null, scenario: RecoveryScenario): string {
  if (snapshot === null) {
    return scenario.status === "completed" ? "Completed fixture" : "Awaiting boundary";
  }
  if (snapshot.status === "completed") {
    return "Completed";
  }
  if (snapshot.status === "closed_without_action") {
    return "Closed without action";
  }
  if (snapshot.status === "outcome_unknown") {
    return "Outcome unknown";
  }
  return snapshot.pendingApproval === null ? "Decision in progress" : "Awaiting approval";
}

export function recoveryStatusAnnouncement(
  stateLabel: string,
  consentReviewAvailable: boolean,
  consentTransitionAnnouncement: string,
): string {
  const transitionCopy =
    consentReviewAvailable && consentTransitionAnnouncement !== ""
      ? ` ${consentTransitionAnnouncement}`
      : "";
  return `Recovery status: ${stateLabel}.${transitionCopy}`;
}

function safeLiveStartError(error: unknown): Error {
  if (error instanceof PublicApiError) {
    return error;
  }
  return new Error("Live recovery could not be started.");
}

function safeQuotaStartError(error: unknown): Error {
  if (error instanceof PublicApiError) {
    return error;
  }
  return new Error("Quota trace could not be started.");
}

function startErrorMessage(error: Error): string {
  const retryGuidance =
    error instanceof PublicApiError && error.retryAfterSeconds !== null
      ? ` Try again in ${error.retryAfterSeconds} seconds.`
      : "";
  return `${error.message}${retryGuidance}`;
}

function isCreationBudgetError(error: Error | null): boolean {
  return (
    error instanceof PublicApiError &&
    error.code === "creation_daily_budget_exceeded"
  );
}

interface AutomaticDecisionRetry {
  recoveryId: string;
  request: DecisionRequest;
  retryKey: string;
}

function storedAutomaticDecisionRetry(
  recoveryId: string | null,
): AutomaticDecisionRetry | null {
  if (recoveryId === null) {
    return null;
  }
  const stored = readPendingDecision();
  if (stored === null || stored.recoveryId !== recoveryId) {
    return null;
  }
  return {
    recoveryId,
    request: stored.request,
    retryKey: `${recoveryId}:${JSON.stringify(stored.request)}`,
  };
}

function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() =>
    typeof window !== "undefined" && typeof window.matchMedia === "function"
      ? window.matchMedia(query).matches
      : false,
  );

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
      return;
    }
    const media = window.matchMedia(query);
    const update = () => setMatches(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, [query]);

  return matches;
}

export default function App() {
  const [activeId, setActiveId] = useState<ScenarioId>("hotel");
  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [healthError, setHealthError] = useState(false);
  const [hotelRecoveryId, setHotelRecoveryId] = useState<string | null>(() =>
    readActiveHotelRecovery(),
  );
  const [createdSnapshot, setCreatedSnapshot] = useState<RecoverySnapshot | null>(null);
  const [quotaRecoveryId, setQuotaRecoveryId] = useState<string | null>(null);
  const [quotaCreatedSnapshot, setQuotaCreatedSnapshot] =
    useState<RecoverySnapshot | null>(null);
  const [defaultCreationPending, setDefaultCreationPending] = useState(
    hotelRecoveryId === null,
  );
  const [runAction, setRunAction] = useState<"live" | "replay" | null>(null);
  const [quotaRunAction, setQuotaRunAction] =
    useState<"sdk" | "replay" | null>(null);
  const [liveStartError, setLiveStartError] = useState<Error | null>(null);
  const [quotaStartError, setQuotaStartError] = useState<Error | null>(null);
  const [consentSheetOpen, setConsentSheetOpen] = useState(false);
  const [, setPendingDecisionRevision] = useState(0);
  const [consentTransitionAnnouncement, setConsentTransitionAnnouncement] =
    useState("");
  const [automaticDecisionRetry, setAutomaticDecisionRetry] =
    useState<AutomaticDecisionRetry | null>(() =>
      storedAutomaticDecisionRetry(hotelRecoveryId),
    );
  const retriedClaims = useRef(new Set<string>());
  const actionInFlight = useRef(false);
  const selectionGeneration = useRef(0);
  const actionController = useRef<AbortController | null>(null);
  const quotaActionInFlight = useRef(false);
  const quotaSelectionGeneration = useRef(0);
  const quotaActionController = useRef<AbortController | null>(null);
  const consentReviewTrigger = useRef<HTMLAnchorElement>(null);
  const recoveryFocusTarget = useRef<HTMLHeadingElement>(null);
  const terminalRetryButton = useRef<HTMLButtonElement>(null);
  const terminalRetryFocusOwner = useRef<ScenarioId | null>(null);
  const compactLayout = useMediaQuery("(max-width: 759px)");
  const previousCompactLayout = useRef(compactLayout);
  const focusPreviousCompactLayout = useRef(compactLayout);
  const previousConsentReviewAvailable = useRef<boolean | null>(null);

  const recovery = useRecovery(hotelRecoveryId, {
    initialSnapshot:
      createdSnapshot?.recoveryId === hotelRecoveryId ? createdSnapshot : null,
  });
  const quotaRecovery = useRecovery(quotaRecoveryId, {
    initialSnapshot:
      quotaCreatedSnapshot?.recoveryId === quotaRecoveryId
        ? quotaCreatedSnapshot
        : null,
  });

  const hotelScenario = recoveryScenarios[0];
  const quotaScenario = recoveryScenarios[1];

  useEffect(() => {
    const controller = new AbortController();
    setHealthError(false);

    void getHealth(controller.signal)
      .then(setHealth)
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") {
          return;
        }
        setHealth(null);
        setHealthError(true);
      });

    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (hotelRecoveryId !== null) {
      setDefaultCreationPending(false);
      return;
    }
    if (health === null) {
      setDefaultCreationPending(!healthError);
      return;
    }
    if (!health.sdkStubReady) {
      setDefaultCreationPending(false);
      return;
    }
    let disposed = false;
    const generation = selectionGeneration.current;
    setDefaultCreationPending(true);
    setLiveStartError(null);
    const creation = createHotelRecoveryOnce();
    void creation
      .then((snapshot) => {
        if (disposed || generation !== selectionGeneration.current) {
          return;
        }
        persistActiveHotelRecovery(snapshot.recoveryId, snapshot.executionMode);
        setCreatedSnapshot(snapshot);
        setHotelRecoveryId(snapshot.recoveryId);
      })
      .catch((error: unknown) => {
        if (!disposed && generation === selectionGeneration.current) {
          setCreatedSnapshot(null);
          setLiveStartError(safeLiveStartError(error));
        }
      })
      .finally(() => {
        if (activeHotelCreation === creation) {
          activeHotelCreation = null;
        }
        if (!disposed && generation === selectionGeneration.current) {
          setDefaultCreationPending(false);
        }
      });
    return () => {
      disposed = true;
    };
  }, [health, healthError, hotelRecoveryId]);

  useEffect(() => {
    const snapshot = recovery.snapshot;
    if (
      hotelRecoveryId === null ||
      snapshot === null ||
      snapshot.recoveryId !== hotelRecoveryId
    ) {
      return;
    }
    persistActiveHotelRecovery(hotelRecoveryId, snapshot.executionMode);
  }, [hotelRecoveryId, recovery.snapshot]);

  useEffect(() => () => {
    selectionGeneration.current += 1;
    actionController.current?.abort();
    quotaSelectionGeneration.current += 1;
    quotaActionController.current?.abort();
  }, []);

  useEffect(() => {
    const snapshot = recovery.snapshot;
    if (
      hotelRecoveryId === null ||
      snapshot === null ||
      snapshot.recoveryId !== hotelRecoveryId ||
      snapshot.status !== "pending_approval"
    ) {
      return;
    }
    const stored = readPendingDecision();
    if (stored === null || stored.recoveryId !== hotelRecoveryId) {
      setAutomaticDecisionRetry((current) =>
        current?.recoveryId === hotelRecoveryId ? null : current,
      );
      return;
    }
    if (
      snapshot.pendingApproval !== null &&
      !requestMatchesApproval(stored.request, snapshot.pendingApproval)
    ) {
      setAutomaticDecisionRetry((current) =>
        current?.recoveryId === hotelRecoveryId ? null : current,
      );
      return;
    }
    const retryKey = `${hotelRecoveryId}:${JSON.stringify(stored.request)}`;
    if (retriedClaims.current.has(retryKey)) {
      return;
    }
    retriedClaims.current.add(retryKey);
    const controller = new AbortController();
    setAutomaticDecisionRetry({
      recoveryId: hotelRecoveryId,
      request: stored.request,
      retryKey,
    });
    void postDecision(hotelRecoveryId, stored.request, controller.signal)
      .catch(() => {
        // Preserve the exact claim for a same-ID retry on the next reload.
      })
      .finally(() => {
        setAutomaticDecisionRetry((current) =>
          current?.retryKey === retryKey ? null : current,
        );
      });
    return () => {
      controller.abort();
      setAutomaticDecisionRetry((current) =>
        current?.retryKey === retryKey ? null : current,
      );
    };
  }, [hotelRecoveryId, recovery.snapshot]);

  useEffect(() => {
    const snapshot = recovery.snapshot;
    const receipt = recovery.receipt;
    if (
      snapshot !== null &&
      receipt !== null &&
      snapshot.recoveryId === receipt.recoveryId &&
      snapshot.status === receipt.status &&
      isTerminalRecoveryStatus(snapshot.status)
    ) {
      if (clearPendingDecisionForRecovery(snapshot.recoveryId)) {
        setPendingDecisionRevision((revision) => revision + 1);
      }
    }
  }, [recovery.receipt, recovery.snapshot]);

  const runRecovery = async (executionMode: "openai_live" | "replay_fixture") => {
    if (
      actionInFlight.current ||
      readPendingDecision() !== null ||
      defaultCreationPending ||
      recovery.loading ||
      (hotelRecoveryId !== null &&
        (recovery.snapshot === null ||
          !isTerminalRecoveryStatus(recovery.snapshot.status)))
    ) {
      return;
    }
    actionInFlight.current = true;
    const generation = selectionGeneration.current + 1;
    selectionGeneration.current = generation;
    const controller = new AbortController();
    actionController.current?.abort();
    actionController.current = controller;
    setRunAction(executionMode === "openai_live" ? "live" : "replay");
    setLiveStartError(null);

    try {
      const snapshot = await createRecovery("hotel", executionMode, controller.signal);
      if (controller.signal.aborted || generation !== selectionGeneration.current) {
        return;
      }
      persistActiveHotelRecovery(snapshot.recoveryId, snapshot.executionMode);
      setCreatedSnapshot(snapshot);
      setHotelRecoveryId(snapshot.recoveryId);
    } catch (error: unknown) {
      if (
        controller.signal.aborted ||
        generation !== selectionGeneration.current ||
        (error instanceof DOMException && error.name === "AbortError")
      ) {
        return;
      }
      setLiveStartError(safeLiveStartError(error));
    } finally {
      if (generation === selectionGeneration.current) {
        actionInFlight.current = false;
        actionController.current = null;
        setRunAction(null);
      }
    }
  };

  const runQuotaRecovery = async (
    executionMode: "sdk_stub" | "replay_fixture",
  ) => {
    if (
      quotaActionInFlight.current ||
      quotaRecovery.loading ||
      (quotaRecovery.error === null &&
        quotaRecoveryId !== null &&
        (quotaRecovery.snapshot === null ||
          !isTerminalRecoveryStatus(quotaRecovery.snapshot.status)))
    ) {
      return;
    }
    quotaActionInFlight.current = true;
    const generation = quotaSelectionGeneration.current + 1;
    quotaSelectionGeneration.current = generation;
    const controller = new AbortController();
    quotaActionController.current?.abort();
    quotaActionController.current = controller;
    setQuotaRunAction(executionMode === "sdk_stub" ? "sdk" : "replay");
    setQuotaStartError(null);
    if (quotaRecovery.error !== null) {
      setQuotaCreatedSnapshot(null);
      setQuotaRecoveryId(null);
    }

    try {
      const snapshot = await createRecovery(
        "api-quota",
        executionMode,
        controller.signal,
      );
      if (
        controller.signal.aborted ||
        generation !== quotaSelectionGeneration.current
      ) {
        return;
      }
      if (
        snapshot.status !== "completed" ||
        snapshot.currentStep !== 5 ||
        snapshot.pendingApproval !== null
      ) {
        throw new Error("Quota creation did not return a terminal recovery");
      }
      setQuotaCreatedSnapshot(snapshot);
      setQuotaRecoveryId(snapshot.recoveryId);
    } catch (error: unknown) {
      if (
        controller.signal.aborted ||
        generation !== quotaSelectionGeneration.current ||
        (error instanceof DOMException && error.name === "AbortError")
      ) {
        return;
      }
      setQuotaStartError(safeQuotaStartError(error));
    } finally {
      if (generation === quotaSelectionGeneration.current) {
        quotaActionInFlight.current = false;
        quotaActionController.current = null;
        setQuotaRunAction(null);
      }
    }
  };

  const selectScenario = (scenarioId: ScenarioId) => {
    if (scenarioId !== "api-quota") {
      quotaSelectionGeneration.current += 1;
      quotaActionController.current?.abort();
      quotaActionController.current = null;
      quotaActionInFlight.current = false;
      setQuotaRunAction(null);
    }
    setActiveId(scenarioId);
  };

  const authoritativeQuotaSnapshot =
    quotaRecovery.snapshot?.recoveryId === quotaRecoveryId
      ? quotaRecovery.snapshot
      : null;
  const authoritativeQuotaReceipt =
    quotaRecovery.receipt?.recoveryId === quotaRecoveryId
      ? quotaRecovery.receipt
      : null;
  const activeSnapshot =
    activeId === "hotel" ? recovery.snapshot : authoritativeQuotaSnapshot;
  const activeReceipt =
    activeId === "hotel" ? recovery.receipt : authoritativeQuotaReceipt;
  const automaticSubmittingAction =
    automaticDecisionRetry !== null &&
    activeSnapshot?.status === "pending_approval" &&
    activeSnapshot.pendingApproval !== null &&
    automaticDecisionRetry.recoveryId === activeSnapshot.recoveryId &&
    requestMatchesApproval(
      automaticDecisionRetry.request,
      activeSnapshot.pendingApproval,
    )
      ? automaticDecisionRetry.request.decision
      : null;
  const hotelScenarioView = useMemo<RecoveryScenario>(
    () =>
      recovery.snapshot === null
        ? hotelScenario
        : {
            ...hotelScenario,
            executionMode: recovery.snapshot.executionMode,
            status: recovery.snapshot.status,
            currentStep: recovery.snapshot.currentStep,
            currentStepSummary: recovery.snapshot.currentStepSummary,
            ...(recovery.snapshot.executionMode === "replay_fixture" &&
            recovery.snapshot.status === "completed"
              ? hotelReplayCompletedPresentation
              : recovery.snapshot.executionMode === "sdk_stub" &&
                  recovery.snapshot.status === "completed"
                ? hotelSdkCompletedPresentation
                : recovery.snapshot.executionMode !== "replay_fixture" &&
                    recovery.receipt?.terminalReason ===
                      "authorization_expired_before_dispatch"
                  ? hotelSdkAuthorizationExpiredPresentation
                  : recovery.snapshot.executionMode !== "replay_fixture" &&
                      recovery.receipt?.terminalReason ===
                        "authorization_expired_with_unresolved_dispatch"
                    ? hotelSdkAuthorizationUnknownPresentation
                    : recovery.snapshot.executionMode === "sdk_stub" &&
                        recovery.snapshot.status === "closed_without_action"
                      ? hotelSdkClosedPresentation
                      : {}),
          },
    [hotelScenario, recovery.receipt?.terminalReason, recovery.snapshot],
  );
  const quotaScenarioView = useMemo<RecoveryScenario>(
    () =>
      authoritativeQuotaSnapshot === null
        ? {
            ...quotaScenario,
            status: "in_progress",
            currentStep: 0,
            currentStepSummary:
              "Choose an explicit SDK stub or recorded replay trace.",
            lifecycleDetails: {
              Detect: "Not run.",
              Prove: "Not run.",
              Negotiate: "Not run.",
              Authorize: "Not run.",
              Execute: "Not run.",
              "Verify & seal": "Not run.",
            },
            evidence: [],
          }
        : {
            ...quotaScenario,
            executionMode: authoritativeQuotaSnapshot.executionMode,
            status: authoritativeQuotaSnapshot.status,
            currentStep: authoritativeQuotaSnapshot.currentStep,
            currentStepSummary: authoritativeQuotaSnapshot.currentStepSummary,
            ...(authoritativeQuotaSnapshot.executionMode === "sdk_stub"
              ? quotaSdkCompletedPresentation
              : quotaReplayCompletedPresentation),
          },
    [authoritativeQuotaSnapshot, quotaScenario],
  );
  const activeScenarioView =
    activeId === "hotel" ? hotelScenarioView : quotaScenarioView;
  const scenarioRailScenarios = useMemo<ReadonlyArray<RecoveryScenario>>(
    () =>
      recoveryScenarios.map((scenario) => {
        if (scenario.id === "hotel") {
          return hotelScenarioView;
        }
        return quotaScenarioView;
      }),
    [hotelScenarioView, quotaScenarioView],
  );

  const activeExecutionMode: ExecutionMode | null =
    activeSnapshot?.executionMode ?? null;

  const presentation = useMemo(
    () =>
      health === null || activeExecutionMode === null
        ? null
        : deriveRuntimePresentation(health, { executionMode: activeExecutionMode }),
    [activeExecutionMode, health],
  );
  const pendingDecision = readPendingDecision();
  const replayFallbackAvailable =
    !isCreationBudgetError(liveStartError) &&
    (healthError ||
      health?.liveReady === false ||
      (liveStartError instanceof PublicApiError &&
        liveStartError.fallback !== null));
  const unresolvedRecoveryActive =
    activeId === "hotel" &&
    (defaultCreationPending ||
      recovery.loading ||
      (hotelRecoveryId !== null &&
        (activeSnapshot === null ||
          !isTerminalRecoveryStatus(activeSnapshot.status))));
  const switchingLocked = pendingDecision !== null || unresolvedRecoveryActive;
  const hotelContentPending =
    activeId === "hotel" &&
    activeSnapshot === null &&
    (defaultCreationPending || recovery.loading || runAction !== null);
  const hotelContentUnavailable =
    activeId === "hotel" && activeSnapshot === null && !hotelContentPending;
  const quotaContentPending =
    activeId === "api-quota" &&
    (quotaRunAction !== null ||
      quotaRecovery.loading ||
      (quotaRecovery.error === null &&
        quotaRecoveryId !== null &&
        (authoritativeQuotaSnapshot === null ||
          (isTerminalRecoveryStatus(authoritativeQuotaSnapshot.status) &&
            authoritativeQuotaReceipt === null))));
  const quotaContentUnavailable =
    activeId === "api-quota" &&
    authoritativeQuotaSnapshot === null &&
    !quotaContentPending;
  const contentPending = hotelContentPending || quotaContentPending;
  const contentUnavailable = hotelContentUnavailable || quotaContentUnavailable;
  const consentReviewAvailable =
    !contentPending &&
    !contentUnavailable &&
    activeId === "hotel" &&
    activeSnapshot?.scenarioId === "hotel" &&
    activeSnapshot.status === "pending_approval" &&
    activeSnapshot.pendingApproval !== null;

  const activeRecovery =
    activeId === "hotel" ? recovery : quotaRecovery;
  const activeEvents = activeRecovery.events;
  const activeEventError =
    activeRecovery.errorPhase === "events" ? activeRecovery.error : null;
  const activeTerminalError =
    activeRecovery.errorPhase === "terminal" ? activeRecovery.error : null;
  const terminalEvidenceVisibilityMessage =
    activeSnapshot !== null && activeEvents.length > 0
      ? "The retained recovery snapshot and event history remain visible while you retry."
      : activeSnapshot !== null
        ? "The retained recovery snapshot remains visible while you retry."
        : activeEvents.length > 0
          ? "The retained recovery event history remains visible while you retry."
          : "No authoritative recovery snapshot or event history is loaded yet.";
  const activeRecoveryStateLabel = recoveryStateLabel(
    activeSnapshot,
    activeScenarioView,
  );
  const activeElement =
    typeof document !== "undefined" &&
    document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
  const consentTransitionOriginHadFocus =
    activeElement !== null &&
    (activeElement === consentReviewTrigger.current ||
      activeElement.closest(".consent-dialog") !== null ||
      activeElement.closest(".evidence-inspector--consent") !== null);
  const shouldFocusRecoveryAfterConsentTransition =
    previousConsentReviewAvailable.current === true &&
    consentTransitionOriginHadFocus &&
    (!consentReviewAvailable ||
      focusPreviousCompactLayout.current !== compactLayout);

  useLayoutEffect(() => {
    if (shouldFocusRecoveryAfterConsentTransition) {
      recoveryFocusTarget.current?.focus();
    }
    previousConsentReviewAvailable.current = consentReviewAvailable;
    focusPreviousCompactLayout.current = compactLayout;
  }, [
    compactLayout,
    consentReviewAvailable,
    shouldFocusRecoveryAfterConsentTransition,
  ]);

  useLayoutEffect(() => {
    if (
      activeTerminalError !== null ||
      activeRecovery.terminalRetryAvailable ||
      activeRecovery.terminalRetrying
    ) {
      return;
    }
    const focusOwner = terminalRetryFocusOwner.current;
    terminalRetryFocusOwner.current = null;
    if (focusOwner !== activeId) {
      return;
    }
    const focusedElement =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    if (
      focusedElement === null ||
      focusedElement === document.body ||
      !focusedElement.isConnected
    ) {
      recoveryFocusTarget.current?.focus();
    }
  }, [
    activeId,
    activeRecovery.terminalRetryAvailable,
    activeRecovery.terminalRetrying,
    activeTerminalError,
  ]);

  useEffect(() => {
    if (
      previousCompactLayout.current !== compactLayout &&
      consentReviewAvailable
    ) {
      setConsentTransitionAnnouncement(
        compactLayout
          ? "Consent review is available from Review exact remedy."
          : "Consent review is available in the evidence inspector.",
      );
    }
    previousCompactLayout.current = compactLayout;
    if (!compactLayout || !consentReviewAvailable) {
      setConsentSheetOpen(false);
    }
    if (!consentReviewAvailable) {
      setConsentTransitionAnnouncement("");
    }
  }, [compactLayout, consentReviewAvailable]);

  return (
    <div className="app-frame">
      <div
        className="app-content"
        inert={compactLayout && consentSheetOpen && consentReviewAvailable}
      >
        <AppHeader executionMode={activeExecutionMode} />

        <div className="console-shell" id="workspace">
        <ScenarioRail
          scenarios={scenarioRailScenarios}
          activeId={activeId}
          onSelect={selectScenario}
        />

        <main className="workspace">
          <ProvenanceStrip presentation={presentation} healthError={healthError} />
          {activeId === "hotel" ? (
            <section className="live-run-controls" aria-label="Recovery execution controls">
              {health?.liveReady === true ? (
                <button
                  type="button"
                  disabled={runAction !== null || switchingLocked}
                  onClick={() => void runRecovery("openai_live")}
                >
                  {runAction === "live" ? "Starting live recovery…" : "Run live recovery"}
                </button>
              ) : null}
              {health?.liveReady === false ? (
                <p>Live mode is unavailable on this server.</p>
              ) : null}
              {liveStartError !== null ? (
                <p role="alert">{startErrorMessage(liveStartError)}</p>
              ) : null}
              {replayFallbackAvailable ? (
                <button
                  type="button"
                  disabled={runAction !== null || switchingLocked}
                  onClick={() => void runRecovery("replay_fixture")}
                >
                  {runAction === "replay" ? "Starting replay fixture…" : "Run replay fixture"}
                </button>
              ) : null}
              {switchingLocked ? (
                <p>
                  {pendingDecision !== null
                    ? "A saved decision must be retried with its original recovery before switching modes."
                    : "Finish or decline the active recovery before switching modes."}
                </p>
              ) : null}
            </section>
          ) : null}
          {activeId === "api-quota" ? (
            <section className="live-run-controls" aria-label="Quota execution controls">
              {health?.sdkStubReady === true ? (
                <button
                  type="button"
                  disabled={quotaRunAction !== null || quotaContentPending}
                  onClick={() => void runQuotaRecovery("sdk_stub")}
                >
                  {quotaRunAction === "sdk"
                    ? "Running SDK stub trace…"
                    : "Run SDK stub trace"}
                </button>
              ) : null}
              <button
                type="button"
                disabled={quotaRunAction !== null || quotaContentPending}
                onClick={() => void runQuotaRecovery("replay_fixture")}
              >
                {quotaRunAction === "replay"
                  ? "Replaying recorded trace…"
                  : "Replay recorded trace"}
              </button>
              {quotaStartError !== null ? (
                <p role="alert">{startErrorMessage(quotaStartError)}</p>
              ) : null}
            </section>
          ) : null}
          <section className="recovery-heading" aria-labelledby="recovery-title">
            <div>
              <p className="eyebrow">Active recovery</p>
              <h1 id="recovery-title" ref={recoveryFocusTarget} tabIndex={-1}>
                {activeScenarioView.title}
              </h1>
              {consentReviewAvailable ? (
                <a
                  className="consent-review-link"
                  href="#approval-heading"
                  ref={consentReviewTrigger}
                  onClick={(event) => {
                    if (!compactLayout) {
                      return;
                    }
                    event.preventDefault();
                    setConsentSheetOpen(true);
                    setConsentTransitionAnnouncement(
                      "Approve exact remedy dialog opened.",
                    );
                  }}
                >
                  Review exact remedy
                </a>
              ) : null}
            </div>
            <span className="recovery-state" aria-hidden="true">
              {activeRecoveryStateLabel}
            </span>
            <span
              className="visually-hidden"
              role="status"
              aria-label="Recovery status updates"
            >
              {recoveryStatusAnnouncement(
                activeRecoveryStateLabel,
                consentReviewAvailable,
                consentTransitionAnnouncement,
              )}
            </span>
          </section>
          {activeSnapshot !== null && activeEventError !== null ? (
            <section
              className="live-run-controls"
              role="alert"
              aria-label="Event update connection"
            >
              <p>
                {activeEventError}
                {activeRecovery.eventsRetryAfterSeconds !== null
                  ? ` Retry is available in ${activeRecovery.eventsRetryAfterSeconds} ${
                      activeRecovery.eventsRetryAfterSeconds === 1
                        ? "second"
                        : "seconds"
                    }.`
                  : ""}
              </p>
              <button
                type="button"
                disabled={
                  !activeRecovery.eventsRetryAvailable ||
                  activeRecovery.eventsRetrying
                }
                onClick={activeRecovery.retryEvents}
              >
                {activeRecovery.eventsRetrying
                  ? "Retrying event updates…"
                  : "Retry event updates"}
              </button>
            </section>
          ) : null}
          {activeTerminalError !== null &&
          (activeRecovery.terminalRetryAvailable ||
            activeRecovery.terminalRetrying) ? (
            <section
              className="live-run-controls"
              role="alert"
              aria-label="Terminal evidence loading"
              aria-busy={activeRecovery.terminalRetrying}
            >
              <p>
                {activeTerminalError}
                {activeRecovery.terminalRetrying
                  ? " Reloading authoritative terminal evidence."
                  : activeRecovery.terminalRetryReason ===
                      "automatic_retries_exhausted"
                    ? " Automatic terminal evidence retries are exhausted."
                    : " Automatic retry is unavailable for this response."}
              </p>
              <p>{terminalEvidenceVisibilityMessage}</p>
              <button
                type="button"
                ref={terminalRetryButton}
                disabled={
                  !activeRecovery.terminalRetryAvailable ||
                  activeRecovery.terminalRetrying
                }
                onClick={() => {
                  terminalRetryFocusOwner.current =
                    document.activeElement === terminalRetryButton.current
                      ? activeId
                      : null;
                  activeRecovery.retryTerminal();
                }}
              >
                {activeRecovery.terminalRetrying
                  ? "Retrying terminal evidence…"
                  : "Retry terminal evidence"}
              </button>
            </section>
          ) : null}
          {contentPending ? (
            <section className="recovery-loading" aria-live="polite" aria-busy="true">
              Loading authoritative recovery…
            </section>
          ) : contentUnavailable ? (
            <section className="recovery-loading" role="status">
              {(activeId === "hotel" ? recovery.error : quotaRecovery.error) ??
                "Choose an available execution mode to start this recovery."}
            </section>
          ) : (
            <>
              <Lifecycle scenario={activeScenarioView} />
              <EventLedger events={activeEvents} compact={compactLayout} />
            </>
          )}
        </main>

        {contentPending ? (
          <aside className="evidence-inspector" aria-busy="true">
            Loading authoritative evidence…
          </aside>
        ) : contentUnavailable ? (
          <aside className="evidence-inspector">
            No authoritative recovery evidence is available.
          </aside>
        ) : consentReviewAvailable && activeSnapshot !== null ? (
          <ConsentSheet
            snapshot={activeSnapshot}
            displayMode={compactLayout ? "dialog" : "inline"}
            open={!compactLayout || consentSheetOpen}
            onClose={(reason) => {
              setConsentSheetOpen(false);
              setConsentTransitionAnnouncement(
                reason === "escape"
                  ? "Approve exact remedy dialog closed with Escape."
                  : "Approve exact remedy dialog closed.",
              );
            }}
            triggerRef={consentReviewTrigger}
            fallbackFocusRef={recoveryFocusTarget}
            scenarioTitle={activeScenarioView.title}
            runtimePresentation={presentation}
            runtimeHealthError={healthError}
            externalSubmittingAction={automaticSubmittingAction}
          />
        ) : (
          <EvidenceInspector
            scenario={activeScenarioView}
            snapshot={activeSnapshot}
            receipt={activeReceipt}
            externalSubmittingAction={automaticSubmittingAction}
          />
        )}
        </div>
      </div>
    </div>
  );
}
