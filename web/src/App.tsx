import { useEffect, useMemo, useRef, useState } from "react";

import { createRecovery, getHealth, postDecision } from "./api/client";
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
import { deriveRuntimePresentation, type HealthStatus } from "./domain/runtime";
import {
  clearActiveHotelRecovery,
  clearPendingDecisionForRecovery,
  persistActiveHotelRecovery,
  readActiveHotelRecovery,
  readPendingDecision,
} from "./domain/session";
import { recoveryScenarios } from "./fixtures/recoveries";
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
  const [automaticDecisionRetry, setAutomaticDecisionRetry] =
    useState<AutomaticDecisionRetry | null>(() =>
      storedAutomaticDecisionRetry(hotelRecoveryId),
    );
  const retriedClaims = useRef(new Set<string>());
  const replacedRecoveries = useRef(new Set<string>());

  const recovery = useRecovery(hotelRecoveryId, {
    initialSnapshot:
      createdSnapshot?.recoveryId === hotelRecoveryId ? createdSnapshot : null,
  });

  const activeScenario =
    recoveryScenarios.find((scenario) => scenario.id === activeId) ?? recoveryScenarios[0];

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
      return;
    }
    let disposed = false;
    const creation = createHotelRecoveryOnce();
    void creation
      .then((snapshot) => {
        if (disposed) {
          return;
        }
        persistActiveHotelRecovery(snapshot.recoveryId);
        setCreatedSnapshot(snapshot);
        setHotelRecoveryId(snapshot.recoveryId);
        if (activeHotelCreation === creation) {
          activeHotelCreation = null;
        }
      })
      .catch(() => {
        if (activeHotelCreation === creation) {
          activeHotelCreation = null;
        }
        if (!disposed) {
          setCreatedSnapshot(null);
        }
      });
    return () => {
      disposed = true;
    };
  }, [hotelRecoveryId]);

  useEffect(() => {
    if (
      hotelRecoveryId === null ||
      recovery.errorPhase !== "initial" ||
      (recovery.errorStatus !== 404 && recovery.errorStatus !== 422) ||
      replacedRecoveries.current.has(hotelRecoveryId)
    ) {
      return;
    }

    const storedRecoveryId = readActiveHotelRecovery();
    clearPendingDecisionForRecovery(hotelRecoveryId);
    setAutomaticDecisionRetry((current) =>
      current?.recoveryId === hotelRecoveryId ? null : current,
    );
    if (storedRecoveryId !== null && storedRecoveryId !== hotelRecoveryId) {
      setCreatedSnapshot(null);
      setHotelRecoveryId(storedRecoveryId);
      return;
    }

    replacedRecoveries.current.add(hotelRecoveryId);
    clearActiveHotelRecovery(hotelRecoveryId);
    setCreatedSnapshot(null);
    setHotelRecoveryId((current) =>
      current === hotelRecoveryId ? null : current,
    );
  }, [hotelRecoveryId, recovery.errorPhase, recovery.errorStatus]);

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

  const activeSnapshot = activeId === "hotel" ? recovery.snapshot : null;
  const activeReceipt = activeId === "hotel" ? recovery.receipt : null;
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
  const activeScenarioView = useMemo<RecoveryScenario>(
    () =>
      activeSnapshot === null
        ? activeScenario
        : {
            ...activeScenario,
            executionMode: activeSnapshot.executionMode,
            status: activeSnapshot.status,
            currentStep: activeSnapshot.currentStep,
            currentStepSummary: activeSnapshot.currentStepSummary,
          },
    [activeScenario, activeSnapshot],
  );

  const presentation = useMemo(
    () => (health === null ? null : deriveRuntimePresentation(health, activeScenarioView)),
    [activeScenarioView, health],
  );

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
            {activeSnapshot?.executionMode === "sdk_stub" ? "SDK QA workspace" : "Replay workspace"}
          </span>
        </div>
      </header>

      <div className="console-shell" id="workspace">
        <ScenarioRail
          scenarios={recoveryScenarios}
          activeId={activeId}
          onSelect={setActiveId}
        />

        <main className="workspace">
          <ProvenanceStrip presentation={presentation} healthError={healthError} />
          <section className="recovery-heading" aria-labelledby="recovery-title">
            <div>
              <p className="eyebrow">Active recovery</p>
              <h1 id="recovery-title">{activeScenarioView.title}</h1>
            </div>
            <span className="recovery-state">
              {recoveryStateLabel(activeSnapshot, activeScenarioView)}
            </span>
          </section>
          <Lifecycle scenario={activeScenarioView} />
        </main>

        <EvidenceInspector
          scenario={activeScenarioView}
          snapshot={activeSnapshot}
          receipt={activeReceipt}
          externalSubmittingAction={automaticSubmittingAction}
        />
      </div>
    </div>
  );
}
