import { useEffect, useRef, useState, type RefObject } from "react";

import { DecisionCapacityError, postDecision } from "../api/client";
import type { RecoveryEvent } from "../api/events";
import type {
  DecisionAction,
  EvidenceEntry,
  RecoveryReceipt,
  RecoveryScenario,
  RecoverySnapshot,
} from "../domain/recovery";
import { ConsentSheet } from "./ConsentSheet";
import { Receipt } from "./Receipt";
import { TechnicalEvidence } from "./TechnicalEvidence";

interface EvidenceInspectorProps {
  scenario: RecoveryScenario;
  snapshot?: RecoverySnapshot | null;
  receipt?: RecoveryReceipt | null;
  events?: ReadonlyArray<RecoveryEvent>;
  receiptLoading?: boolean;
  receiptError?: string | null;
  terminalEventObserved?: boolean;
  emptyState?: {
    title: string;
    summary: string;
    boundaryTitle: string;
    boundaryText: string;
  } | null;
  mobile?: boolean;
  open?: boolean;
  onClose?: () => void;
  onRetryReceipt?: () => void;
  returnFocusRef?: RefObject<HTMLElement | null>;
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

function sheetTitle(
  snapshot: RecoverySnapshot | null,
  receipt: RecoveryReceipt | null,
  terminalEventObserved: boolean,
  decisionAccepted: boolean,
): string {
  if (receipt?.status === "closed_without_action") return "Closed without action";
  if (receipt?.status === "outcome_unknown") return "Outcome unknown";
  if (receipt?.status === "simulated_completed") return "Simulated replay receipt";
  if (receipt?.status === "completed") return "Recovery receipt";
  if (terminalEventObserved) return "Recovery receipt";
  if (decisionAccepted) return "Decision accepted";
  if (snapshot?.pendingApproval !== null && snapshot?.pendingApproval !== undefined) {
    return "Approve exact remedy";
  }
  if (snapshot?.status === "pending_approval") return "Decision in progress";
  return "Evidence inspector";
}

export function EvidenceInspector({
  scenario,
  snapshot = null,
  receipt = null,
  events = [],
  receiptLoading = false,
  receiptError = null,
  terminalEventObserved = false,
  emptyState = null,
  mobile = false,
  open = true,
  onClose = () => {},
  onRetryReceipt,
  returnFocusRef,
  onServerSuccess,
  clientDecisionIdFactory = defaultDecisionId,
}: EvidenceInspectorProps) {
  const [submittingAction, setSubmittingAction] = useState<DecisionAction | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [decisionAccepted, setDecisionAccepted] = useState(false);
  const [expiredContextKey, setExpiredContextKey] = useState<string | null>(null);
  const decisionIds = useRef<Partial<Record<DecisionAction, string>>>({});
  const serverRefreshes = useRef(new Set<string>());
  const fallbackReturnFocusRef = useRef<HTMLElement | null>(null);
  const approval = snapshot?.pendingApproval ?? null;
  const decisionContextKey = [
    snapshot?.recoveryId ?? "no-recovery",
    approval?.remedyId ?? "no-remedy",
    approval?.remedyDigest ?? "no-remedy-digest",
    approval?.toolCallId ?? "no-tool-call",
    approval?.expiry ?? "no-expiry",
  ].join(":");
  const decisionContextRef = useRef({ key: decisionContextKey, generation: 0 });
  if (decisionContextRef.current.key !== decisionContextKey) {
    decisionContextRef.current = {
      key: decisionContextKey,
      generation: decisionContextRef.current.generation + 1,
    };
  }

  useEffect(
    () => () => {
      decisionContextRef.current = {
        key: decisionContextRef.current.key,
        generation: decisionContextRef.current.generation + 1,
      };
    },
    [],
  );

  useEffect(() => {
    decisionIds.current = {};
    setError(null);
    setStatusMessage(null);
    setSubmittingAction(null);
    setDecisionAccepted(false);
    setExpiredContextKey(null);
  }, [decisionContextKey]);

  const expiryMilliseconds = approval === null ? Number.NaN : Date.parse(approval.expiry);
  const consentExpired =
    approval !== null &&
    (expiredContextKey === decisionContextKey ||
      (Number.isFinite(expiryMilliseconds) && Date.now() >= expiryMilliseconds));

  useEffect(() => {
    if (approval === null || !Number.isFinite(expiryMilliseconds)) return;
    const submittedGeneration = decisionContextRef.current.generation;
    let cancelled = false;
    let timeoutId: ReturnType<typeof setTimeout> | undefined;

    const reachExpiry = () => {
      if (
        cancelled ||
        decisionContextRef.current.generation !== submittedGeneration ||
        decisionContextRef.current.key !== decisionContextKey
      ) {
        return;
      }
      setExpiredContextKey(decisionContextKey);
      if (serverRefreshes.current.has(decisionContextKey)) return;
      serverRefreshes.current.add(decisionContextKey);
      void Promise.resolve(onServerSuccess?.()).catch(() => {
        if (
          cancelled ||
          decisionContextRef.current.generation !== submittedGeneration ||
          decisionContextRef.current.key !== decisionContextKey
        ) {
          return;
        }
        setError("Consent expired, but refreshed recovery evidence is unavailable.");
      });
    };

    const scheduleDeadline = () => {
      const remaining = expiryMilliseconds - Date.now();
      if (remaining <= 0) {
        reachExpiry();
        return;
      }
      timeoutId = setTimeout(scheduleDeadline, Math.min(remaining, 2_147_483_647));
    };

    scheduleDeadline();
    return () => {
      cancelled = true;
      if (timeoutId !== undefined) clearTimeout(timeoutId);
    };
  }, [approval, decisionContextKey, expiryMilliseconds, onServerSuccess]);

  const copyDigest = async () => {
    if (approval === null) return;
    const submittedGeneration = decisionContextRef.current.generation;
    setError(null);
    try {
      await navigator.clipboard.writeText(approval.remedyDigest);
      if (decisionContextRef.current.generation !== submittedGeneration) return;
      setStatusMessage("Full remedy digest copied.");
    } catch {
      if (decisionContextRef.current.generation !== submittedGeneration) return;
      setError("The full remedy digest could not be copied.");
    }
  };

  const submitDecision = async (action: DecisionAction) => {
    if (
      snapshot === null ||
      approval === null ||
      submittingAction !== null ||
      consentExpired ||
      Date.now() >= Date.parse(approval.expiry)
    ) {
      return;
    }
    const stableDecisionId = decisionIds.current[action] ?? clientDecisionIdFactory();
    const submittedGeneration = decisionContextRef.current.generation;
    decisionIds.current[action] = stableDecisionId;
    setSubmittingAction(action);
    setError(null);
    setStatusMessage(null);
    try {
      await postDecision(snapshot.recoveryId, {
        action,
        clientDecisionId: stableDecisionId,
        remedyId: approval.remedyId,
        remedyDigest: approval.remedyDigest,
        toolCallId: approval.toolCallId,
      });
      if (decisionContextRef.current.generation !== submittedGeneration) return;
      setDecisionAccepted(true);
      setStatusMessage(
        action === "approve"
          ? "Approval accepted by the server. Refreshing recovery evidence."
          : "Decline accepted by the server. Refreshing recovery evidence.",
      );
      if (!serverRefreshes.current.has(decisionContextKey)) {
        serverRefreshes.current.add(decisionContextKey);
        try {
          await onServerSuccess?.();
        } catch {
          if (decisionContextRef.current.generation !== submittedGeneration) return;
          setError(
            `${action === "approve" ? "Approval" : "Decline"} accepted, but refreshed recovery evidence is unavailable.`,
          );
        }
      }
    } catch (caught: unknown) {
      if (decisionContextRef.current.generation !== submittedGeneration) return;
      setError(
        caught instanceof DecisionCapacityError
          ? caught.message
          : `${action === "approve" ? "Approval" : "Decline"} could not be recorded. Try again with the same decision.`,
      );
    } finally {
      if (decisionContextRef.current.generation === submittedGeneration) {
        setSubmittingAction(null);
      }
    }
  };

  let content;
  let footer;

  if (receipt !== null) {
    content = <Receipt events={events} receipt={receipt} technicalOpen={!mobile} />;
  } else if (receiptLoading) {
    content = <p className="empty-evidence" aria-live="polite">Loading authoritative terminal receipt…</p>;
  } else if (receiptError !== null) {
    content = (
      <div className="receipt-error">
        <p role="alert">{receiptError}</p>
        {onRetryReceipt === undefined ? null : (
          <button type="button" onClick={onRetryReceipt}>Retry receipt</button>
        )}
      </div>
    );
  } else if (terminalEventObserved) {
    content = (
      <p className="empty-evidence" aria-live="polite">
        Terminal server event received. Synchronizing the authoritative snapshot and receipt…
      </p>
    );
  } else if (decisionAccepted) {
    content = (
      <div className="execution-boundary" aria-live="polite">
        <strong>{statusMessage ?? "Decision accepted by the server."}</strong>
        <p>Synchronizing the authoritative recovery snapshot and receipt.</p>
        {error === null ? null : <span role="alert">{error}</span>}
      </div>
    );
  } else if (snapshot !== null && approval !== null) {
    const terms = approval.terms;
    const technicalEntries: EvidenceEntry[] = [
      { label: "Recovery ID", value: snapshot.recoveryId, monospace: true },
      { label: "Remedy ID", value: approval.remedyId, monospace: true },
      { label: "Action", value: terms.action, monospace: true },
      { label: "Changed fields", value: approval.changedFields.join(", "), monospace: true },
      { label: "Provider commitments", value: approval.providerCommitments.join("; ") },
    ];
    content = (
      <>
        <p className="inspector-summary">{snapshot.currentStepSummary}</p>
        <div className="inspector-status">
          <span className="status-symbol status-symbol--pending_approval" aria-hidden="true" />
          <div><span>Status</span><strong>Pending approval</strong></div>
        </div>
        <dl className="evidence-list consent-evidence">
          <div><dt>Booking</dt><dd className="mono">{terms.bookingId}</dd></div>
          <div><dt>Replacement</dt><dd>{terms.replacement.fromRoomType} → {terms.replacement.toRoomType}</dd></div>
          <div><dt>Stay</dt><dd>{terms.stay.checkIn} → {terms.stay.checkOut}</dd></div>
          <div><dt>Cost delta</dt><dd>{formatMinorUsd(approval.costDeltaMinor)}</dd></div>
          <div>
            <dt>Remedy digest</dt>
            <dd className="digest-evidence">
              <code>{visibleDigest(approval.remedyDigest)}</code>
              <button type="button" onClick={() => void copyDigest()}>Copy full remedy digest</button>
            </dd>
          </div>
          <div><dt>Expiry</dt><dd><time dateTime={approval.expiry}>{utcDisplay(approval.expiry)}</time></dd></div>
          <div><dt>Hard constraint</dt><dd>{approval.hardConstraintSatisfied ? "Satisfied" : "Not satisfied"}</dd></div>
          <div><dt>Delegated authority</dt><dd>{approval.delegatedAuthoritySatisfied ? "Satisfied" : "Not satisfied"}</dd></div>
          <div><dt>Pending tool-call ID</dt><dd className="mono">{approval.toolCallId}</dd></div>
        </dl>
        <div className="execution-boundary" aria-live="polite">
          <strong>
            {consentExpired
              ? "Consent deadline reached — checking the authoritative outcome"
              : "Execution has not begun."}
          </strong>
          <p>
            {consentExpired
              ? "Decision controls are disabled while the server confirms whether a decision claim won."
              : "The server will recheck this exact consent immediately before dispatch."}
          </p>
        </div>
        <TechnicalEvidence defaultOpen={!mobile} entries={technicalEntries} />
        {error === null ? null : <p role="alert">{error}</p>}
        <p className="decision-status" aria-live="polite">{statusMessage}</p>
      </>
    );
    footer = (
      <div className="consent-actions">
        <button
          className="consent-action consent-action--decline"
          type="button"
          disabled={submittingAction !== null || consentExpired}
          onClick={() => void submitDecision("decline")}
        >
          {submittingAction === "decline" ? "Submitting decline…" : "Decline"}
        </button>
        <button
          className="consent-action consent-action--approve"
          type="button"
          disabled={submittingAction !== null || consentExpired}
          onClick={() => void submitDecision("approve")}
        >
          {submittingAction === "approve" ? "Submitting approval…" : "Approve remedy"}
        </button>
      </div>
    );
  } else if (snapshot !== null && snapshot.status === "pending_approval") {
    content = (
      <>
        <p className="inspector-summary">{snapshot.currentStepSummary}</p>
        <dl className="evidence-list">
          <div><dt>Recovery ID</dt><dd className="mono">{snapshot.recoveryId}</dd></div>
          <div><dt>Execution mode</dt><dd className="mono">{snapshot.executionMode}</dd></div>
        </dl>
        <div className="execution-boundary">
          <strong>Awaiting the durable execution outcome.</strong>
          <p>The claimed decision cannot be replaced by another decision.</p>
        </div>
      </>
    );
  } else if (
    snapshot !== null &&
    (snapshot.status === "completed" ||
      snapshot.status === "closed_without_action" ||
      snapshot.status === "outcome_unknown")
  ) {
    content = (
      <p className="empty-evidence" aria-live="polite">
        Awaiting the authoritative terminal receipt…
      </p>
    );
  } else if (snapshot !== null) {
    content = (
      <>
        <p className="inspector-summary">{snapshot.currentStepSummary}</p>
        <dl className="evidence-list">
          <div><dt>Recovery ID</dt><dd className="mono">{snapshot.recoveryId}</dd></div>
          <div><dt>Status</dt><dd>{snapshot.status}</dd></div>
          <div><dt>Execution mode</dt><dd className="mono">{snapshot.executionMode}</dd></div>
          <div><dt>Models</dt><dd>{snapshot.modelIds.length === 0 ? "None — no model call" : snapshot.modelIds.join(", ")}</dd></div>
        </dl>
      </>
    );
  } else if (emptyState !== null) {
    content = (
      <>
        <p className="inspector-summary">{emptyState.summary}</p>
        <div className="execution-boundary">
          <strong>{emptyState.boundaryTitle}</strong>
          <p>{emptyState.boundaryText}</p>
        </div>
      </>
    );
  } else {
    content = (
      <>
        <p className="inspector-summary">{scenario.currentStepSummary}</p>
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
      </>
    );
  }

  return (
    <ConsentSheet
      busy={submittingAction !== null}
      footer={footer}
      mobile={mobile}
      onClose={onClose}
      open={open}
      returnFocusRef={returnFocusRef ?? fallbackReturnFocusRef}
      title={
        emptyState?.title ??
        sheetTitle(snapshot, receipt, terminalEventObserved, decisionAccepted)
      }
    >
      {content}
    </ConsentSheet>
  );
}
