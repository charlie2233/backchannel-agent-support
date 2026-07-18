import type { RecoveryScenario } from "../domain/recovery";

interface EvidenceInspectorProps {
  scenario: RecoveryScenario;
}

export function EvidenceInspector({ scenario }: EvidenceInspectorProps) {
  return (
    <aside className="evidence-inspector" aria-labelledby="evidence-heading">
      <div className="inspector-heading">
        <p className="eyebrow">Server-shaped snapshot</p>
        <h2 id="evidence-heading">Evidence inspector</h2>
        <p>{scenario.currentStepSummary}</p>
      </div>

      <div className="inspector-status">
        <span className={`status-symbol status-symbol--${scenario.status}`} aria-hidden="true" />
        <div>
          <span>Status</span>
          <strong>{scenario.status === "completed" ? "Replay completed" : "Replay paused"}</strong>
        </div>
      </div>

      <dl className="evidence-list">
        {scenario.evidence.map((entry) => (
          <div key={entry.label}>
            <dt>{entry.label}</dt>
            <dd className={entry.monospace ? "mono" : undefined}>{entry.value}</dd>
          </div>
        ))}
      </dl>

      <div className="simulation-boundary">
        <strong>Simulation boundary</strong>
        <p>This bundled state proves UI behavior only. It is not a provider receipt.</p>
      </div>
    </aside>
  );
}
