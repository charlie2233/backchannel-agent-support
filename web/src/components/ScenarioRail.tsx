import type { RecoveryScenario, ScenarioId } from "../domain/recovery";

interface ScenarioRailProps {
  scenarios: ReadonlyArray<RecoveryScenario>;
  activeId: ScenarioId;
  disabled?: boolean;
  mobile?: boolean;
  onSelect: (id: ScenarioId) => void;
}

export function ScenarioRail({
  scenarios,
  activeId,
  disabled = false,
  mobile = false,
  onSelect,
}: ScenarioRailProps) {
  if (mobile) {
    return (
      <section className="scenario-selector">
        <label htmlFor="scenario-select">Scenario</label>
        <select
          id="scenario-select"
          aria-label="Scenario"
          disabled={disabled}
          value={activeId}
          onChange={(event) => {
            if (!disabled) onSelect(event.target.value as ScenarioId);
          }}
        >
          {scenarios.map((scenario) => (
            <option key={scenario.id} value={scenario.id}>
              {scenario.title}
            </option>
          ))}
        </select>
      </section>
    );
  }

  return (
    <nav className="scenario-rail" aria-label="Recovery scenarios">
      <div className="rail-heading">
        <p className="eyebrow">Recovery workspace</p>
        <h2>Scenarios</h2>
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
                disabled={disabled}
                onClick={() => {
                  if (!disabled) onSelect(scenario.id);
                }}
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
        <p>Runs stay inside the demo-adapter boundary; replay modes make no live calls.</p>
      </div>
    </nav>
  );
}
