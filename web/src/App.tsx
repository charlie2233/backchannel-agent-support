import { useCallback, useEffect, useMemo, useState } from "react";

import { createRecovery, getHealth, getRecovery } from "./api/client";
import { EvidenceInspector } from "./components/EvidenceInspector";
import { Lifecycle } from "./components/Lifecycle";
import { ProvenanceStrip } from "./components/ProvenanceStrip";
import { ScenarioRail } from "./components/ScenarioRail";
import { deriveRuntimePresentation, type HealthStatus } from "./domain/runtime";
import type { RecoveryScenario, RecoverySnapshot, ScenarioId } from "./domain/recovery";
import { recoveryScenarios } from "./fixtures/recoveries";

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
    const controller = new AbortController();

    void createRecovery("hotel", "sdk_stub", controller.signal)
      .then(setHotelSnapshot)
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") {
          return;
        }
        setHotelSnapshot(null);
      });

    return () => controller.abort();
  }, []);

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
              {activeSnapshot !== null
                ? activeScenarioView.status === "completed"
                  ? "Completed"
                  : activeSnapshot.pendingApproval === null
                    ? "Decision in progress"
                    : "Awaiting approval"
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
