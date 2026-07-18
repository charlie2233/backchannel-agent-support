import { lifecycleSteps, type RecoveryScenario } from "../domain/recovery";

interface LifecycleProps {
  scenario: RecoveryScenario;
}

export function Lifecycle({ scenario }: LifecycleProps) {
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
          const state =
            scenario.status === "completed" || index < scenario.currentStep
              ? "complete"
              : index === scenario.currentStep
                ? "current"
                : "upcoming";
          return (
            <li className={`lifecycle-item lifecycle-item--${state}`} key={step}>
              <div className="step-marker" aria-hidden="true">
                {index + 1}
              </div>
              <div className="step-body">
                <div className="step-title-row">
                  <h3>{step}</h3>
                  <span>{state === "complete" ? "Recorded" : state}</span>
                </div>
                <p>{scenario.lifecycleDetails[step]}</p>
              </div>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
