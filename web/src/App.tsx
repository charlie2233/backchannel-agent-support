import { useEffect, useMemo, useRef, useState } from "react";

import {
  createRecovery,
  getHealth,
  postDecision,
  PublicApiError,
} from "./api/client";
import { EvidenceInspector } from "./components/EvidenceInspector";
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

function environmentLabel(mode: ExecutionMode | null): string {
  switch (mode) {
    case "openai_live":
      return "OpenAI live workspace";
    case "sdk_stub":
      return "SDK QA workspace";
    case "replay_fixture":
      return "Replay workspace";
    case null:
      return "Runtime pending";
  }
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
      .catch(() => {
        if (!disposed && generation === selectionGeneration.current) {
          setCreatedSnapshot(null);
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
      clearPendingDecisionForRecovery(snapshot.recoveryId);
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
              : {}),
          },
    [hotelScenario, recovery.snapshot],
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
    healthError ||
    health?.liveReady === false ||
    (liveStartError instanceof PublicApiError && liveStartError.fallback !== null);
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

  return (
    <div className="app-frame">
      <header className="top-bar">
        <a className="brand" href="#workspace" aria-label="Backchannel console home">
          <span className="brand-mark" aria-hidden="true">
            <span />
            <span />
          </span>
          <span>Backchannel</span>
        </a>
        <div className="top-context">
          <span>Operational recovery console</span>
          <span className="environment-badge">
            {environmentLabel(activeExecutionMode)}
          </span>
        </div>
      </header>

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
                <p role="alert">
                  {liveStartError.message}
                  {liveStartError instanceof PublicApiError &&
                  liveStartError.retryAfterSeconds !== null
                    ? ` Try again in ${liveStartError.retryAfterSeconds} seconds.`
                    : ""}
                </p>
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
                <p role="alert">{quotaStartError.message}</p>
              ) : null}
            </section>
          ) : null}
          <section className="recovery-heading" aria-labelledby="recovery-title">
            <div>
              <p className="eyebrow">Active recovery</p>
              <h1 id="recovery-title">{activeScenarioView.title}</h1>
              {consentReviewAvailable ? (
                <a className="consent-review-link" href="#approval-heading">
                  Review exact remedy
                </a>
              ) : null}
            </div>
            <span className="recovery-state">
              {recoveryStateLabel(activeSnapshot, activeScenarioView)}
            </span>
          </section>
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
            <Lifecycle scenario={activeScenarioView} />
          )}
        </main>

        {contentPending ? (
          <aside className="evidence-inspector" aria-live="polite" aria-busy="true">
            Loading authoritative evidence…
          </aside>
        ) : contentUnavailable ? (
          <aside className="evidence-inspector" aria-live="polite">
            No authoritative recovery evidence is available.
          </aside>
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
  );
}
