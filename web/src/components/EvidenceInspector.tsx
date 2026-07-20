import type {
  DecisionAction,
  DecisionResponse,
  RecoveryReceipt,
  RecoveryScenario,
  RecoverySnapshot,
} from "../domain/recovery";
import { ConsentSheet } from "./ConsentSheet";
import { Receipt } from "./Receipt";

interface EvidenceInspectorProps {
  scenario: RecoveryScenario;
  snapshot?: RecoverySnapshot | null;
  receipt?: RecoveryReceipt | null;
  externalSubmittingAction?: DecisionAction | null;
  onDecisionAccepted?: (response: DecisionResponse) => void;
  clientDecisionIdFactory?: (action: DecisionAction) => string;
}

function DecisionProgress({ snapshot }: { snapshot: RecoverySnapshot }) {
  return (
    <aside className="evidence-inspector" aria-labelledby="decision-progress-heading">
      <div className="inspector-heading">
        <p className="eyebrow">Server decision record</p>
        <h2 id="decision-progress-heading">Decision in progress</h2>
        <p>{snapshot.currentStepSummary}</p>
      </div>
      <dl className="evidence-list">
        <div>
          <dt>Recovery ID</dt>
          <dd className="mono">{snapshot.recoveryId}</dd>
        </div>
        <div>
          <dt>Execution mode</dt>
          <dd className="mono">{snapshot.executionMode}</dd>
        </div>
      </dl>
      <div className="execution-boundary">
        <strong>Awaiting the durable execution outcome.</strong>
        <p>The claimed decision cannot be replaced by another decision.</p>
      </div>
    </aside>
  );
}

function SnapshotEvidence({ snapshot }: { snapshot: RecoverySnapshot }) {
  return (
    <aside className="evidence-inspector" aria-labelledby="evidence-heading">
      <div className="inspector-heading">
        <p className="eyebrow">Server recovery snapshot</p>
        <h2 id="evidence-heading">Evidence inspector</h2>
        <p>{snapshot.currentStepSummary}</p>
      </div>
      <dl className="evidence-list">
        <div>
          <dt>Recovery ID</dt>
          <dd className="mono">{snapshot.recoveryId}</dd>
        </div>
        <div>
          <dt>Status</dt>
          <dd>{snapshot.status}</dd>
        </div>
        <div>
          <dt>Execution mode</dt>
          <dd className="mono">{snapshot.executionMode}</dd>
        </div>
      </dl>
    </aside>
  );
}

function FixtureEvidence({ scenario }: { scenario: RecoveryScenario }) {
  return (
    <aside className="evidence-inspector" aria-labelledby="evidence-heading">
      <div className="inspector-heading">
        <p className="eyebrow">Bundled fixture snapshot</p>
        <h2 id="evidence-heading">Evidence inspector</h2>
        <p>{scenario.currentStepSummary}</p>
      </div>

      <div className="inspector-status" aria-live="polite">
        <span
          className={`status-symbol status-symbol--${scenario.status}`}
          aria-hidden="true"
        />
        <div>
          <span>Status</span>
          <strong>
            {scenario.status === "completed" ? "Replay completed" : "Replay paused"}
          </strong>
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

export function EvidenceInspector({
  scenario,
  snapshot = null,
  receipt = null,
  externalSubmittingAction = null,
  onDecisionAccepted,
  clientDecisionIdFactory,
}: EvidenceInspectorProps) {
  if (
    snapshot !== null &&
    receipt !== null &&
    snapshot.recoveryId === receipt.recoveryId &&
    snapshot.status === receipt.status
  ) {
    return <Receipt receipt={receipt} />;
  }

  if (snapshot !== null && snapshot.pendingApproval !== null) {
    return (
      <ConsentSheet
        snapshot={snapshot}
        displayMode="inline"
        externalSubmittingAction={externalSubmittingAction}
        onDecisionAccepted={onDecisionAccepted}
        clientDecisionIdFactory={clientDecisionIdFactory}
      />
    );
  }

  if (snapshot !== null && snapshot.status === "pending_approval") {
    return <DecisionProgress snapshot={snapshot} />;
  }

  if (snapshot !== null) {
    return <SnapshotEvidence snapshot={snapshot} />;
  }

  return <FixtureEvidence scenario={scenario} />;
}
