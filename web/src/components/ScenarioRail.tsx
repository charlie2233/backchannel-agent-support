import type { RecoveryScenario, ScenarioId } from "../domain/recovery";

interface ScenarioRailProps {
  scenarios: ReadonlyArray<RecoveryScenario>;
  activeId: ScenarioId;
  onSelect: (id: ScenarioId) => void;
}

export function ScenarioRail({ scenarios, activeId, onSelect }: ScenarioRailProps) {
  return (
    <nav className="scenario-rail" aria-labelledby="scenario-heading">
      <div className="rail-heading">
        <p className="eyebrow">Replay library</p>
        <h2 id="scenario-heading">Scenarios</h2>
      </div>
      <ul aria-label="Recovery scenarios">
        {scenarios.map((scenario, index) => {
          const isActive = scenario.id === activeId;
          return (
            <li key={scenario.id}>
              <button
                className="scenario-button"
                type="button"
                aria-current={isActive ? "page" : undefined}
                onClick={() => onSelect(scenario.id)}
              >
                <span className="scenario-index" aria-hidden="true">
                  {String(index + 1).padStart(2, "0")}
                </span>
                <span>
                  <strong>{scenario.title}</strong>
                  <small>{scenario.summary}</small>
                </span>
              </button>
            </li>
          );
        })}
      </ul>
      <div className="rail-note">
        <span className="rail-note-mark" aria-hidden="true" />
        <p>Fixtures are protocol demonstrations, not live executions.</p>
      </div>
    </nav>
  );
}
