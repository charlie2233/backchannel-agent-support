import { useEffect, useRef, useState } from "react";

import { postDecision } from "../api/client";
import type {
  DecisionAction,
  DecisionRequest,
  DecisionResponse,
  PendingApproval,
  RecoveryReceipt,
  RecoveryScenario,
  RecoverySnapshot,
} from "../domain/recovery";
import {
  persistPendingDecision,
  readPendingDecision,
} from "../domain/session";

interface EvidenceInspectorProps {
  scenario: RecoveryScenario;
  snapshot?: RecoverySnapshot | null;
  receipt?: RecoveryReceipt | null;
  externalSubmittingAction?: DecisionAction | null;
  onDecisionAccepted?: (response: DecisionResponse) => void;
  clientDecisionIdFactory?: (action: DecisionAction) => string;
}

function defaultDecisionId(action: DecisionAction): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return `decision-${action}-${crypto.randomUUID()}`;
  }
  return `decision-${action}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
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

function requestMatchesApproval(
  request: DecisionRequest,
  approval: PendingApproval,
): boolean {
  return (
    request.remedyId === approval.remedyId &&
    request.remedyDigest === approval.remedyDigest &&
    request.toolCallId === approval.toolCallId
  );
}

function responseMatchesRequest(
  response: DecisionResponse,
  request: DecisionRequest,
  recoveryId: string,
): boolean {
  if (
    response.clientDecisionId !== request.clientDecisionId ||
    response.recoveryId !== recoveryId ||
    response.decision !== request.decision
  ) {
    return false;
  }
  return response.decision === "approve"
    ? response.approvedRemedyDigest === request.remedyDigest
    : response.decisionRemedyDigest === request.remedyDigest;
}

function ReceiptInspector({ receipt }: { receipt: RecoveryReceipt }) {
  if (receipt.status === "closed_without_action") {
    const closureProof = [
      "Human consent requested.",
      "Remedy declined by operator.",
      "Exact interruption rejected.",
      "No replacement action selected.",
      "Temporary permission revoked.",
      "Cancellation receipt sealed.",
    ];
    const fixedVerification = new Set(closureProof);
    return (
      <aside className="evidence-inspector receipt-inspector" aria-labelledby="closed-heading">
        <div className="inspector-heading">
          <p className="eyebrow">Authoritative server receipt</p>
          <h2 id="closed-heading">Closed without action</h2>
          <p>The exact remedy was declined and its permission scope is closed.</p>
        </div>
        <div className="receipt-verdict receipt-verdict--closed">
          <strong>Provider dispatch did not begin.</strong>
          <p>This is cancellation evidence for the exact rejected interruption.</p>
        </div>
        <ul className="receipt-checks" aria-label="Closure verification">
          {closureProof.map((result) => <li key={result}>{result}</li>)}
          <li>executionCount = {receipt.executionCount}</li>
          {receipt.verificationResults
            .filter((result) => !fixedVerification.has(result))
            .map((result) => <li key={result}>{result}</li>)}
        </ul>
        <dl className="evidence-list">
          <div>
            <dt>Decision remedy digest</dt>
            <dd className="mono">{receipt.decisionRemedyDigest}</dd>
          </div>
          <div>
            <dt>Authorization source</dt>
            <dd>{receipt.authorizationSource}</dd>
          </div>
          <div>
            <dt>Boundary</dt>
            <dd>{receipt.boundary}</dd>
          </div>
        </dl>
      </aside>
    );
  }

  if (receipt.status === "outcome_unknown") {
    const reconciliationResults = receipt.verificationResults.filter(
      (result) => result !== "Manual reconciliation required.",
    );
    return (
      <aside className="evidence-inspector receipt-inspector" aria-labelledby="unknown-heading">
        <div className="inspector-heading">
          <p className="eyebrow">Authoritative server receipt</p>
          <h2 id="unknown-heading">Outcome unknown</h2>
          <p>Execution evidence exists, so cancellation cannot be claimed.</p>
        </div>
        <div className="receipt-verdict receipt-verdict--unknown">
          <strong>Manual reconciliation required.</strong>
          <p>{receipt.providerResult}</p>
        </div>
        <ul className="receipt-checks" aria-label="Reconciliation evidence">
          {reconciliationResults.map((result) => <li key={result}>{result}</li>)}
        </ul>
        <dl className="evidence-list">
          <div>
            <dt>Execution count</dt>
            <dd>{receipt.executionCount}</dd>
          </div>
          <div>
            <dt>Provider dispatch started</dt>
            <dd>{receipt.providerDispatchStarted ? "Yes" : "No"}</dd>
          </div>
          <div>
            <dt>Decision remedy digest</dt>
            <dd className="mono">{receipt.decisionRemedyDigest}</dd>
          </div>
        </dl>
      </aside>
    );
  }

  return (
    <aside className="evidence-inspector receipt-inspector" aria-labelledby="completed-heading">
      <div className="inspector-heading">
        <p className="eyebrow">Authoritative server receipt</p>
        <h2 id="completed-heading">Completed receipt</h2>
        <p>{receipt.providerResult}</p>
      </div>
      <dl className="evidence-list">
        <div>
          <dt>Recovery ID</dt>
          <dd className="mono">{receipt.recoveryId}</dd>
        </div>
        <div>
          <dt>Execution count</dt>
          <dd>{receipt.executionCount}</dd>
        </div>
        <div>
          <dt>Approved remedy digest</dt>
          <dd className="mono">{receipt.approvedRemedyDigest}</dd>
        </div>
      </dl>
    </aside>
  );
}

export function EvidenceInspector({
  scenario,
  snapshot = null,
  receipt = null,
  externalSubmittingAction = null,
  onDecisionAccepted,
  clientDecisionIdFactory = defaultDecisionId,
}: EvidenceInspectorProps) {
  const [submittingAction, setSubmittingAction] = useState<DecisionAction | null>(null);
  const [lockedAction, setLockedAction] = useState<DecisionAction | null>(null);
  const [acceptedAction, setAcceptedAction] = useState<DecisionAction | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const decisionIds = useRef<Partial<Record<DecisionAction, string>>>({});
  const activeRequest = useRef<DecisionRequest | null>(null);
  const approval = snapshot?.pendingApproval ?? null;

  useEffect(() => {
    activeRequest.current = null;
    decisionIds.current = {};
    setSubmittingAction(null);
    setLockedAction(null);
    setAcceptedAction(null);
    setError(null);
    setStatusMessage(null);
    if (snapshot === null || approval === null) {
      return;
    }
    const stored = readPendingDecision();
    if (
      stored !== null &&
      stored.recoveryId === snapshot.recoveryId &&
      requestMatchesApproval(stored.request, approval)
    ) {
      activeRequest.current = stored.request;
      decisionIds.current[stored.request.decision] = stored.request.clientDecisionId;
      setLockedAction(stored.request.decision);
    }
  }, [
    approval?.remedyDigest,
    approval?.remedyId,
    approval?.toolCallId,
    snapshot?.recoveryId,
  ]);

  if (
    snapshot !== null &&
    receipt !== null &&
    snapshot.recoveryId === receipt.recoveryId &&
    snapshot.status === receipt.status
  ) {
    return <ReceiptInspector receipt={receipt} />;
  }

  if (snapshot !== null && approval !== null) {
    const terms = approval.terms;
    const effectiveSubmittingAction =
      externalSubmittingAction ?? submittingAction;

    const copyDigest = async () => {
      setError(null);
      try {
        await navigator.clipboard.writeText(approval.remedyDigest);
        setStatusMessage("Full remedy digest copied.");
      } catch {
        setError("The full remedy digest could not be copied.");
      }
    };

    const submitDecision = async (action: DecisionAction) => {
      if (
        effectiveSubmittingAction !== null ||
        acceptedAction !== null ||
        (lockedAction !== null && lockedAction !== action)
      ) {
        return;
      }
      const existingRequest = activeRequest.current;
      const stableDecisionId =
        existingRequest?.decision === action
          ? existingRequest.clientDecisionId
          : decisionIds.current[action] ?? clientDecisionIdFactory(action);
      decisionIds.current[action] = stableDecisionId;
      const request: DecisionRequest =
        existingRequest?.decision === action && requestMatchesApproval(existingRequest, approval)
          ? existingRequest
          : {
              decision: action,
              clientDecisionId: stableDecisionId,
              remedyId: approval.remedyId,
              remedyDigest: approval.remedyDigest,
              toolCallId: approval.toolCallId,
            };
      if (
        !persistPendingDecision({ recoveryId: snapshot.recoveryId, request })
      ) {
        setError("Decision could not be saved for safe retry. No request was sent.");
        return;
      }
      activeRequest.current = request;
      setLockedAction(action);
      setSubmittingAction(action);
      setError(null);
      setStatusMessage(null);
      try {
        const response = await postDecision(snapshot.recoveryId, request);
        if (!responseMatchesRequest(response, request, snapshot.recoveryId)) {
          throw new Error("Decision acknowledgement did not match the durable request");
        }
        setAcceptedAction(action);
        setStatusMessage(
          action === "approve"
            ? "Approval accepted. Waiting for terminal evidence."
            : "Decline accepted. Waiting for terminal evidence.",
        );
        onDecisionAccepted?.(response);
      } catch {
        setError(
          action === "approve"
            ? "Approval could not be recorded. Retry the same decision."
            : "Decline could not be recorded. Retry the same decision.",
        );
      } finally {
        setSubmittingAction(null);
      }
    };

    const actionsDisabled = effectiveSubmittingAction !== null || acceptedAction !== null;
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
          <button
            className="consent-action consent-action--decline"
            type="button"
            disabled={actionsDisabled || (lockedAction !== null && lockedAction !== "decline")}
            onClick={() => void submitDecision("decline")}
          >
            {effectiveSubmittingAction === "decline" ? "Declining…" : "Decline"}
          </button>
          <button
            className="consent-action consent-action--approve"
            type="button"
            disabled={actionsDisabled || (lockedAction !== null && lockedAction !== "approve")}
            onClick={() => void submitDecision("approve")}
          >
            {effectiveSubmittingAction === "approve" ? "Approving…" : "Approve remedy"}
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
          <p>The claimed decision cannot be replaced by another decision.</p>
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
