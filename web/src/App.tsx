import { useEffect, useMemo, useState } from "react";

import { getHealth } from "./api/client";
import { EvidenceInspector } from "./components/EvidenceInspector";
import { Lifecycle } from "./components/Lifecycle";
import { ProvenanceStrip } from "./components/ProvenanceStrip";
import { ScenarioRail } from "./components/ScenarioRail";
import { deriveRuntimePresentation, type HealthStatus } from "./domain/runtime";
import type { ScenarioId } from "./domain/recovery";
import { recoveryScenarios } from "./fixtures/recoveries";

export default function App() {
  const [activeId, setActiveId] = useState<ScenarioId>("hotel");
  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [healthError, setHealthError] = useState(false);

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

  const presentation = useMemo(
    () => (health === null ? null : deriveRuntimePresentation(health, activeScenario)),
    [activeScenario, health],
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
          <span className="environment-badge">Replay workspace</span>
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
              <h1 id="recovery-title">{activeScenario.title}</h1>
            </div>
            <span className="recovery-state">
              {activeScenario.status === "completed" ? "Completed fixture" : "Awaiting boundary"}
            </span>
          </section>
          <Lifecycle scenario={activeScenario} />
        </main>

        <EvidenceInspector scenario={activeScenario} />
      </div>
    </div>
  );
}
