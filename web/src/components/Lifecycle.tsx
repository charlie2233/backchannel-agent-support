import {
  lifecycleSteps,
  type RecoveryScenario,
} from "../domain/recovery";

interface LifecycleProps {
  scenario: RecoveryScenario;
}

function stepState(
  scenario: RecoveryScenario,
  index: number,
): { className: string; label: string; current: boolean } {
  if (scenario.status === "completed") {
    return { className: "complete", label: "Recorded", current: false };
  }
  if (scenario.status === "closed_without_action") {
    if (index < 3) return { className: "complete", label: "Recorded", current: false };
    if (index === 3) return { className: "declined", label: "Declined", current: false };
    if (index === 4) return { className: "not-run", label: "Not run", current: false };
    return { className: "closed", label: "Closed", current: false };
  }
  if (scenario.status === "outcome_unknown") {
    if (index < 3) return { className: "complete", label: "Recorded", current: false };
    if (index === 3) return { className: "declined", label: "Declined", current: false };
    if (index === 4) return { className: "unknown", label: "Unknown", current: false };
    return { className: "unknown", label: "Outcome unknown", current: false };
  }
  if (index < scenario.currentStep) {
    return { className: "complete", label: "Recorded", current: false };
  }
  if (index === scenario.currentStep) {
    return { className: "current", label: "Current", current: true };
  }
  return { className: "upcoming", label: "Upcoming", current: false };
}

export function Lifecycle({ scenario }: LifecycleProps) {
  const currentStep = lifecycleSteps[scenario.currentStep];

  return (
    <section className="lifecycle-panel" aria-labelledby="lifecycle-heading">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Protocol trace</p>
          <h2 id="lifecycle-heading">Recovery lifecycle</h2>
        </div>
        <span className="step-count">
          Step {scenario.currentStep + 1} of {lifecycleSteps.length}
        </span>
      </div>

      <ol className="lifecycle" aria-label="Recovery lifecycle">
        {lifecycleSteps.map((step, index) => {
          const state = stepState(scenario, index);
          return (
            <li
              className={`lifecycle-item lifecycle-item--${state.className}`}
              key={step}
              aria-current={state.current ? "step" : undefined}
            >
              <div className="step-marker" aria-hidden="true">
                {index + 1}
              </div>
              <div className="step-body">
                <div className="step-title-row">
                  <h3>{step}</h3>
                  <span>{state.label}</span>
                </div>
                <p className="step-description">{scenario.lifecycleDetails[step]}</p>
              </div>
            </li>
          );
        })}
      </ol>
      <div className="current-step-detail" role="note" aria-label="Current step detail">
        <strong>{currentStep}</strong>
        <p>{scenario.lifecycleDetails[currentStep]}</p>
      </div>
    </section>
  );
}
