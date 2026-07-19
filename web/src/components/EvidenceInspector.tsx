import { useRef, useState } from "react";

import { postDecision } from "../api/client";
import type { RecoveryScenario, RecoverySnapshot } from "../domain/recovery";

interface EvidenceInspectorProps {
  scenario: RecoveryScenario;
  snapshot?: RecoverySnapshot | null;
  onServerSuccess?: () => void | Promise<void>;
  clientDecisionIdFactory?: () => string;
}

function defaultDecisionId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return `decision-${crypto.randomUUID()}`;
  }
  return `decision-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function visibleDigest(digest: string): string {
  return `${digest.slice(0, 19)}…${digest.slice(-8)}`;
}

function utcDisplay(timestamp: string): string {
  return timestamp.replace("T", " ").replace(/(?:Z|\+00:00)$/, " UTC");
}

function formatMinorUsd(minorUnits: number): string {
  return `${(minorUnits / 100).toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })} USD`;
}

export function EvidenceInspector({
  scenario,
  snapshot = null,
  onServerSuccess,
  clientDecisionIdFactory = defaultDecisionId,
}: EvidenceInspectorProps) {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const decisionId = useRef<string | null>(null);
  const approval = snapshot?.pendingApproval ?? null;

  if (snapshot !== null && approval !== null) {
    const terms = approval.terms;

    const copyDigest = async () => {
      setError(null);
      try {
        await navigator.clipboard.writeText(approval.remedyDigest);
        setStatusMessage("Full remedy digest copied.");
      } catch {
        setError("The full remedy digest could not be copied.");
      }
    };

    const approve = async () => {
      if (submitting) {
        return;
      }
      const stableDecisionId = decisionId.current ?? clientDecisionIdFactory();
      decisionId.current = stableDecisionId;
      setSubmitting(true);
      setError(null);
      setStatusMessage(null);
      try {
        await postDecision(snapshot.recoveryId, {
          clientDecisionId: stableDecisionId,
          remedyId: approval.remedyId,
          remedyDigest: approval.remedyDigest,
          toolCallId: approval.toolCallId,
        });
        setStatusMessage("Decision accepted by the server. Refreshing recovery evidence.");
        try {
          await onServerSuccess?.();
        } catch {
          setError("Decision accepted, but refreshed recovery evidence is unavailable.");
        }
      } catch {
        setError("Approval could not be recorded. Try again with the same decision.");
      } finally {
        setSubmitting(false);
      }
    };

    return (
      <aside className="evidence-inspector" aria-labelledby="approval-heading">
        <div className="inspector-heading">
          <p className="eyebrow">Server consent record</p>
          <h2 id="approval-heading">Approve exact remedy</h2>
          <p>{snapshot.currentStepSummary}</p>
        </div>

        <div className="inspector-status">
          <span className="status-symbol status-symbol--pending_approval" aria-hidden="true" />
          <div>
            <span>Status</span>
            <strong>Pending approval</strong>
          </div>
        </div>

        <dl className="evidence-list consent-evidence">
          <div>
            <dt>Recovery ID</dt>
            <dd className="mono">{snapshot.recoveryId}</dd>
          </div>
          <div>
            <dt>Remedy ID</dt>
            <dd className="mono">{approval.remedyId}</dd>
          </div>
          <div>
            <dt>Booking</dt>
            <dd className="mono">{terms.bookingId}</dd>
          </div>
          <div>
            <dt>Action</dt>
            <dd className="mono">{terms.action}</dd>
          </div>
          <div>
            <dt>Replacement</dt>
            <dd>
              {terms.replacement.fromRoomType} → {terms.replacement.toRoomType}
            </dd>
          </div>
          <div>
            <dt>Stay</dt>
            <dd>
              {terms.stay.checkIn} → {terms.stay.checkOut}
            </dd>
          </div>
          <div>
            <dt>Cost delta</dt>
            <dd>{formatMinorUsd(approval.costDeltaMinor)}</dd>
          </div>
          <div>
            <dt>Changed fields</dt>
            <dd className="mono">{approval.changedFields.join(", ")}</dd>
          </div>
          <div>
            <dt>Provider commitments</dt>
            <dd>{approval.providerCommitments.join("; ")}</dd>
          </div>
          <div>
            <dt>Remedy digest</dt>
            <dd className="digest-evidence">
              <code>{visibleDigest(approval.remedyDigest)}</code>
              <button type="button" onClick={() => void copyDigest()}>
                Copy full remedy digest
              </button>
            </dd>
          </div>
          <div>
            <dt>Expiry</dt>
            <dd>
              <time dateTime={approval.expiry}>{utcDisplay(approval.expiry)}</time>
            </dd>
          </div>
          <div>
            <dt>Hard constraint</dt>
            <dd>{approval.hardConstraintSatisfied ? "Satisfied" : "Not satisfied"}</dd>
          </div>
          <div>
            <dt>Delegated authority</dt>
            <dd>{approval.delegatedAuthoritySatisfied ? "Satisfied" : "Not satisfied"}</dd>
          </div>
          <div>
            <dt>Tool call ID</dt>
            <dd className="mono">{approval.toolCallId}</dd>
          </div>
        </dl>

        <div className="execution-boundary">
          <strong>Execution has not begun.</strong>
          <p>The server will recheck this exact consent immediately before dispatch.</p>
        </div>

        {error !== null ? <p role="alert">{error}</p> : null}
        <p className="decision-status" aria-live="polite">
          {statusMessage}
        </p>
        <div className="consent-actions">
          <button type="button" disabled={submitting} onClick={() => void approve()}>
            {submitting ? "Submitting…" : "Approve remedy"}
          </button>
        </div>
      </aside>
    );
  }

  if (
    snapshot !== null &&
    snapshot.status === "pending_approval" &&
    approval === null
  ) {
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
          <p>The claimed decision cannot be replaced by another approval.</p>
        </div>
      </aside>
    );
  }

  if (snapshot !== null) {
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

  return (
    <aside className="evidence-inspector" aria-labelledby="evidence-heading">
      <div className="inspector-heading">
        <p className="eyebrow">Bundled fixture snapshot</p>
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
