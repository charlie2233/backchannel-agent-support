import { useCallback, useEffect, useMemo, useState } from "react";

import { createRecovery, getHealth, getRecovery } from "./api/client";
import { EvidenceInspector } from "./components/EvidenceInspector";
import { Lifecycle } from "./components/Lifecycle";
import { ProvenanceStrip } from "./components/ProvenanceStrip";
import { ScenarioRail } from "./components/ScenarioRail";
import { deriveRuntimePresentation, type HealthStatus } from "./domain/runtime";
import type {
  RecoveryScenario,
  RecoverySnapshot,
  RecoveryStatus,
  ScenarioId,
} from "./domain/recovery";
import { recoveryScenarios } from "./fixtures/recoveries";

function serverStatusLabel(status: RecoveryStatus, hasPendingApproval: boolean): string {
  switch (status) {
    case "completed":
      return "Completed";
    case "closed_without_action":
      return "Closed without action";
    case "outcome_unknown":
      return "Outcome unknown";
    case "pending_approval":
      return hasPendingApproval ? "Awaiting decision" : "Decision in progress";
    case "in_progress":
      return "Recovery in progress";
  }
}

function workspaceLabel(mode: RecoverySnapshot["executionMode"] | undefined): string {
  switch (mode) {
    case "openai_live":
      return "Live agent workspace";
    case "sdk_stub":
      return "SDK QA workspace";
    case "replay_fixture":
    case undefined:
      return "Replay workspace";
  }
}

function serverLifecycleDetails(
  snapshot: RecoverySnapshot,
): RecoveryScenario["lifecycleDetails"] {
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
        "Verify & seal": snapshot.currentStepSummary,
      };
    case "closed_without_action":
      return {
        ...recorded,
        Authorize: "The exact remedy was declined and its permission was revoked.",
        Execute: "Provider dispatch did not begin.",
        "Verify & seal": snapshot.currentStepSummary,
      };
    case "outcome_unknown":
      return {
        ...recorded,
        Authorize: "The decline was recorded after dispatch may have begun.",
        Execute: "Provider dispatch may have begun; its outcome is unknown.",
        "Verify & seal": snapshot.currentStepSummary,
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
  const [activeId, setActiveId] = useState<ScenarioId>("hotel");
  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [healthError, setHealthError] = useState(false);
  const [hotelSnapshot, setHotelSnapshot] = useState<RecoverySnapshot | null>(null);

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
    if (health === null) {
      return;
    }
    const controller = new AbortController();
    const executionMode =
      health.backend === "openai" && health.liveReady ? "openai_live" : "sdk_stub";

    void createRecovery("hotel", executionMode, controller.signal)
      .then(setHotelSnapshot)
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") {
          return;
        }
        setHotelSnapshot(null);
      });

    return () => controller.abort();
  }, [health]);

  const activeSnapshot = activeId === "hotel" ? hotelSnapshot : null;
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
            lifecycleDetails: serverLifecycleDetails(activeSnapshot),
          },
    [activeScenario, activeSnapshot],
  );

  const refreshHotelSnapshot = useCallback(async () => {
    if (hotelSnapshot === null) {
      return;
    }
    setHotelSnapshot(await getRecovery(hotelSnapshot.recoveryId));
  }, [hotelSnapshot]);

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
            {workspaceLabel(activeSnapshot?.executionMode)}
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
              {activeSnapshot !== null
                ? serverStatusLabel(
                    activeSnapshot.status,
                    activeSnapshot.pendingApproval !== null,
                  )
                : activeScenarioView.status === "completed"
                  ? "Completed fixture"
                  : "Awaiting boundary"}
            </span>
          </section>
          <Lifecycle scenario={activeScenarioView} />
        </main>

        <EvidenceInspector
          scenario={activeScenarioView}
          snapshot={activeSnapshot}
          onServerSuccess={refreshHotelSnapshot}
        />
      </div>
    </div>
  );
}
