import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type FormEvent,
  type RefObject,
} from "react";

import {
  DecisionCapacityError,
  DecisionConflictError,
  DecisionExpiredError,
  postDecision,
  postDecisionResume,
} from "../api/client";
import type { RecoveryEvent } from "../api/events";
import type {
  DecisionAction,
  EvidenceEntry,
  PendingApproval,
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
  retryReceiptButtonRef?: RefObject<HTMLButtonElement | null>;
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

interface DecisionRequestAttempt {
  key: string;
  generation: number;
  action: DecisionAction;
  controller: AbortController;
  deadlineId: ReturnType<typeof setTimeout> | null;
  active: boolean;
}

interface DecisionActionLock {
  key: string;
  generation: number;
  action: DecisionAction;
}

interface AcceptedDecisionContext {
  key: string;
  generation: number;
}

export const DECISION_REQUEST_TIMEOUT_MS = 60_000;

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

function policyEligibleApproval(snapshot: RecoverySnapshot | null): PendingApproval | null {
  const approval = snapshot?.pendingApproval ?? null;
  if (
    approval === null ||
    approval.hardConstraintSatisfied !== true ||
    approval.delegatedAuthoritySatisfied !== true
  ) {
    return null;
  }
  return approval;
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
  if (policyEligibleApproval(snapshot) !== null) {
    return "Approve exact remedy";
  }
  if (snapshot?.claimedDecision !== null && snapshot?.claimedDecision !== undefined) {
    return snapshot.claimedDecision.action === "approve"
      ? "Resume exact approval"
      : "Resume exact decline";
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
  retryReceiptButtonRef,
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
  const [, setSubmittingAction] = useState<DecisionAction | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [decisionAccepted, setDecisionAccepted] = useState(false);
  const [expiredContextKey, setExpiredContextKey] = useState<string | null>(null);
  const [staleDecisionContextKey, setStaleDecisionContextKey] = useState<string | null>(null);
  const decisionIds = useRef<Partial<Record<DecisionAction, string>>>({});
  const decisionRequestInFlight = useRef<DecisionRequestAttempt | null>(null);
  const decisionActionLock = useRef<DecisionActionLock | null>(null);
  const acceptedDecisionContext = useRef<AcceptedDecisionContext | null>(null);
  const conflictedDecisionContexts = useRef(new Set<string>());
  const serverRefreshes = useRef(new Set<string>());
  const fallbackReturnFocusRef = useRef<HTMLElement | null>(null);
  const rawApproval = snapshot?.pendingApproval ?? null;
  const approval = policyEligibleApproval(snapshot);
  const hasIneligibleApproval = rawApproval !== null && approval === null;
  const claimedDecision = snapshot?.claimedDecision ?? null;
  const decisionContextKey = [
    snapshot?.recoveryId ?? "no-recovery",
    approval?.remedyId ?? "no-remedy",
    approval?.remedyDigest ?? "no-remedy-digest",
    approval?.toolCallId ?? "no-tool-call",
    approval?.expiry ?? "no-expiry",
    claimedDecision?.action ?? "no-claimed-action",
    claimedDecision?.remedyDigest ?? "no-claimed-digest",
    claimedDecision?.expiry ?? "no-claimed-expiry",
  ].join(":");
  const decisionContextRef = useRef({ key: decisionContextKey, generation: 0 });
  const currentDecisionRequest =
    decisionRequestInFlight.current?.key === decisionContextKey &&
    decisionRequestInFlight.current.generation === decisionContextRef.current.generation
      ? decisionRequestInFlight.current
      : null;
  const contextSubmittingAction = currentDecisionRequest?.action ?? null;
  const currentDecisionActionLock =
    decisionActionLock.current?.key === decisionContextKey &&
    decisionActionLock.current.generation === decisionContextRef.current.generation
      ? decisionActionLock.current
      : null;
  const lockedDecisionAction = currentDecisionActionLock?.action ?? null;

  const requestIsCurrent = (request: DecisionRequestAttempt): boolean =>
    request.active &&
    decisionRequestInFlight.current === request &&
    decisionContextRef.current.key === request.key &&
    decisionContextRef.current.generation === request.generation;

  const retireDecisionRequest = (
    request: DecisionRequestAttempt,
    abort: boolean,
  ): boolean => {
    if (!requestIsCurrent(request)) return false;
    request.active = false;
    if (request.deadlineId !== null) {
      clearTimeout(request.deadlineId);
      request.deadlineId = null;
    }
    decisionRequestInFlight.current = null;
    if (abort && !request.controller.signal.aborted) request.controller.abort();
    return true;
  };

  const abortDecisionRequest = (request: DecisionRequestAttempt): boolean => {
    if (decisionRequestInFlight.current !== request || !request.active) return false;
    request.active = false;
    if (request.deadlineId !== null) {
      clearTimeout(request.deadlineId);
      request.deadlineId = null;
    }
    decisionRequestInFlight.current = null;
    if (!request.controller.signal.aborted) request.controller.abort();
    return true;
  };

  const abortAnyDecisionRequest = () => {
    const request = decisionRequestInFlight.current;
    if (request === null) return;
    abortDecisionRequest(request);
  };

  const beginDecisionRequest = (
    action: DecisionAction,
    timeoutMessage: string,
  ): DecisionRequestAttempt => {
    const request: DecisionRequestAttempt = {
      key: decisionContextKey,
      generation: decisionContextRef.current.generation,
      action,
      controller: new AbortController(),
      deadlineId: null,
      active: true,
    };
    decisionRequestInFlight.current = request;
    request.deadlineId = setTimeout(() => {
      if (!retireDecisionRequest(request, true)) return;
      setSubmittingAction(null);
      setError(timeoutMessage);
      setStatusMessage(null);
    }, DECISION_REQUEST_TIMEOUT_MS);
    return request;
  };

  useEffect(
    () => () => {
      decisionContextRef.current = {
        key: decisionContextRef.current.key,
        generation: decisionContextRef.current.generation + 1,
      };
      abortAnyDecisionRequest();
    },
    [],
  );

  useLayoutEffect(() => {
    const previousContext = decisionContextRef.current;
    if (previousContext.key === decisionContextKey) return;
    const nextContext = {
      key: decisionContextKey,
      generation: previousContext.generation + 1,
    };
    decisionContextRef.current = nextContext;
    const request = decisionRequestInFlight.current;
    if (
      request !== null &&
      (request.key !== nextContext.key || request.generation !== nextContext.generation)
    ) {
      abortDecisionRequest(request);
    }
    decisionIds.current = {};
    decisionActionLock.current = null;
    acceptedDecisionContext.current = null;
    setError(null);
    setStatusMessage(null);
    setSubmittingAction(null);
    setDecisionAccepted(false);
    setExpiredContextKey(null);
    setStaleDecisionContextKey(
      conflictedDecisionContexts.current.has(nextContext.key)
        ? nextContext.key
        : null,
    );
  }, [decisionContextKey]);

  const decisionExpiry = approval?.expiry ?? claimedDecision?.expiry ?? null;
  const expiryMilliseconds = decisionExpiry === null ? Number.NaN : Date.parse(decisionExpiry);
  const decisionExpired =
    decisionExpiry !== null &&
    (expiredContextKey === decisionContextKey ||
      (Number.isFinite(expiryMilliseconds) && Date.now() >= expiryMilliseconds));
  const decisionConflictBlocked = staleDecisionContextKey === decisionContextKey;

  useEffect(() => {
    if (decisionExpiry === null || !Number.isFinite(expiryMilliseconds)) return;
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
      const activeRequest = decisionRequestInFlight.current;
      if (
        activeRequest !== null &&
        activeRequest.key === decisionContextKey &&
        activeRequest.generation === submittedGeneration &&
        retireDecisionRequest(activeRequest, true)
      ) {
        setSubmittingAction(null);
      }
      setError(null);
      setStatusMessage(null);
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
  }, [decisionContextKey, decisionExpiry, expiryMilliseconds, onServerSuccess]);

  const refreshExpiredDecisionEvidence = async (
    request: DecisionRequestAttempt,
    submittedGeneration: number,
  ) => {
    acceptedDecisionContext.current = {
      key: request.key,
      generation: request.generation,
    };
    setExpiredContextKey(request.key);
    setError(null);
    setStatusMessage(
      "Consent expired. Refreshing authoritative recovery evidence.",
    );
    if (serverRefreshes.current.has(request.key)) return;
    serverRefreshes.current.add(request.key);
    try {
      await onServerSuccess?.();
    } catch {
      if (
        decisionContextRef.current.generation !== submittedGeneration ||
        decisionContextRef.current.key !== request.key
      ) {
        return;
      }
      setError(
        "Consent expired, but refreshed recovery evidence is unavailable.",
      );
    }
  };

  const refreshConflictedDecisionEvidence = async (
    request: DecisionRequestAttempt,
    submittedGeneration: number,
    message: string,
  ) => {
    conflictedDecisionContexts.current.add(request.key);
    setStaleDecisionContextKey(request.key);
    setError(null);
    setStatusMessage(message);
    if (serverRefreshes.current.has(request.key)) return;
    serverRefreshes.current.add(request.key);
    try {
      await onServerSuccess?.();
    } catch {
      if (
        decisionContextRef.current.generation !== submittedGeneration ||
        decisionContextRef.current.key !== request.key
      ) {
        return;
      }
      setError(
        "Decision state changed, but refreshed recovery evidence is unavailable.",
      );
    }
  };

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
    const submittedContext = decisionContextRef.current;
    const actionLock = decisionActionLock.current;
    const acceptedContext = acceptedDecisionContext.current;
    const activeRequest = decisionRequestInFlight.current;
    const matchingActiveRequest =
      activeRequest?.active === true &&
      activeRequest.key === decisionContextKey &&
      activeRequest.generation === decisionContextRef.current.generation;
    if (
      snapshot === null ||
      approval === null ||
      submittedContext.key !== decisionContextKey ||
      (acceptedContext !== null &&
        acceptedContext.key === decisionContextKey &&
        acceptedContext.generation === submittedContext.generation) ||
      conflictedDecisionContexts.current.has(decisionContextKey) ||
      matchingActiveRequest ||
      (actionLock !== null &&
        actionLock.key === decisionContextKey &&
        actionLock.generation === decisionContextRef.current.generation &&
        actionLock.action !== action) ||
      decisionExpired ||
      Date.now() >= Date.parse(approval.expiry)
    ) {
      return;
    }
    if (activeRequest?.active === true) abortAnyDecisionRequest();
    const stableDecisionId = decisionIds.current[action] ?? clientDecisionIdFactory();
    const submittedGeneration = submittedContext.generation;
    const lockedAction = currentDecisionActionLock ?? {
      key: decisionContextKey,
      generation: submittedGeneration,
      action,
    };
    decisionActionLock.current = lockedAction;
    const actionLabel = action === "approve" ? "Approval" : "Decline";
    const exactActionLabel = action === "approve" ? "approval" : "decline";
    const oppositeActionLabel = action === "approve" ? "decline" : "approval";
    const inFlight = beginDecisionRequest(
      action,
      `${actionLabel} request timed out. Retry only this exact ${exactActionLabel}; the ${oppositeActionLabel} action remains disabled.`,
    );
    decisionIds.current[action] = stableDecisionId;
    setSubmittingAction(action);
    setError(null);
    setStatusMessage(null);
    try {
      await postDecision(
        snapshot.recoveryId,
        {
          action,
          clientDecisionId: stableDecisionId,
          remedyId: approval.remedyId,
          remedyDigest: approval.remedyDigest,
          toolCallId: approval.toolCallId,
        },
        inFlight.controller.signal,
      );
      if (!retireDecisionRequest(inFlight, false)) return;
      acceptedDecisionContext.current = {
        key: inFlight.key,
        generation: inFlight.generation,
      };
      setSubmittingAction(null);
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
      if (!retireDecisionRequest(inFlight, false)) return;
      setSubmittingAction(null);
      if (caught instanceof DecisionExpiredError) {
        await refreshExpiredDecisionEvidence(
          inFlight,
          submittedGeneration,
        );
        return;
      }
      if (caught instanceof DecisionConflictError) {
        await refreshConflictedDecisionEvidence(
          inFlight,
          submittedGeneration,
          caught.message,
        );
        return;
      }
      setError(
        caught instanceof DecisionCapacityError
          ? caught.message
          : `${action === "approve" ? "Approval" : "Decline"} could not be recorded. Try again with the same decision.`,
      );
    }
  };

  const resumeDecision = async () => {
    const submittedContext = decisionContextRef.current;
    const acceptedContext = acceptedDecisionContext.current;
    const activeRequest = decisionRequestInFlight.current;
    const matchingActiveRequest =
      activeRequest?.active === true &&
      activeRequest.key === decisionContextKey &&
      activeRequest.generation === decisionContextRef.current.generation;
    if (
      snapshot === null ||
      claimedDecision === null ||
      submittedContext.key !== decisionContextKey ||
      (acceptedContext !== null &&
        acceptedContext.key === decisionContextKey &&
        acceptedContext.generation === submittedContext.generation) ||
      conflictedDecisionContexts.current.has(decisionContextKey) ||
      matchingActiveRequest ||
      decisionExpired ||
      Date.now() >= Date.parse(claimedDecision.expiry)
    ) {
      return;
    }
    if (activeRequest?.active === true) abortAnyDecisionRequest();
    const submittedGeneration = submittedContext.generation;
    decisionActionLock.current = currentDecisionActionLock ?? {
      key: decisionContextKey,
      generation: submittedGeneration,
      action: claimedDecision.action,
    };
    const inFlight = beginDecisionRequest(
      claimedDecision.action,
      `Exact ${claimedDecision.action} resume timed out. Retry only this same server-authored action.`,
    );
    setSubmittingAction(claimedDecision.action);
    setError(null);
    setStatusMessage(null);
    try {
      await postDecisionResume(
        snapshot.recoveryId,
        claimedDecision,
        inFlight.controller.signal,
      );
      if (!retireDecisionRequest(inFlight, false)) return;
      acceptedDecisionContext.current = {
        key: inFlight.key,
        generation: inFlight.generation,
      };
      setSubmittingAction(null);
      setDecisionAccepted(true);
      setStatusMessage(
        `Exact ${claimedDecision.action} resumed by the server. Refreshing recovery evidence.`,
      );
      if (!serverRefreshes.current.has(decisionContextKey)) {
        serverRefreshes.current.add(decisionContextKey);
        try {
          await onServerSuccess?.();
        } catch {
          if (decisionContextRef.current.generation !== submittedGeneration) return;
          setError(
            `Exact ${claimedDecision.action} resumed, but refreshed recovery evidence is unavailable.`,
          );
        }
      }
    } catch (caught: unknown) {
      if (!retireDecisionRequest(inFlight, false)) return;
      setSubmittingAction(null);
      if (caught instanceof DecisionExpiredError) {
        await refreshExpiredDecisionEvidence(
          inFlight,
          submittedGeneration,
        );
        return;
      }
      if (caught instanceof DecisionConflictError) {
        await refreshConflictedDecisionEvidence(
          inFlight,
          submittedGeneration,
          caught.message,
        );
        return;
      }
      setError(
        caught instanceof DecisionCapacityError
          ? caught.message
          : `Exact ${claimedDecision.action} could not be resumed. Try the same resume action again.`,
      );
    }
  };

  const submitDecisionForm = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const submitter = (event.nativeEvent as SubmitEvent).submitter;
    if (!(submitter instanceof HTMLButtonElement)) return;
    if (submitter.value !== "approve" && submitter.value !== "decline") return;
    void submitDecision(submitter.value);
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
          <button ref={retryReceiptButtonRef} type="button" onClick={onRetryReceipt}>
            Retry receipt
          </button>
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
        <div className="execution-boundary" aria-live="polite">
          <strong>
            {decisionConflictBlocked
              ? "Decision state changed — checking authoritative recovery evidence"
              : decisionExpired
              ? "Consent deadline reached — checking the authoritative outcome"
              : "Execution has not begun."}
          </strong>
          <p>
            {decisionConflictBlocked
              ? "Decision controls are disabled until refreshed server evidence replaces this stale context."
              : decisionExpired
              ? "Decision controls are disabled while the server confirms whether a decision claim won."
              : "The server will recheck this exact consent immediately before dispatch."}
          </p>
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
          <div><dt>Hard constraint</dt><dd>Satisfied</dd></div>
          <div><dt>Delegated authority</dt><dd>Satisfied</dd></div>
          <div><dt>Pending tool-call ID</dt><dd className="mono">{approval.toolCallId}</dd></div>
        </dl>
        <TechnicalEvidence defaultOpen={!mobile} entries={technicalEntries} />
        {error === null ? null : <p role="alert">{error}</p>}
        <p className="decision-status" aria-live="polite">{statusMessage}</p>
      </>
    );
    footer = (
      <form className="consent-actions" onSubmit={submitDecisionForm}>
        <button
          className="consent-action consent-action--decline"
          type="submit"
          value="decline"
          disabled={
            contextSubmittingAction !== null ||
            decisionConflictBlocked ||
            decisionExpired ||
            (lockedDecisionAction !== null && lockedDecisionAction !== "decline")
          }
        >
          {contextSubmittingAction === "decline" ? "Submitting decline…" : "Decline"}
        </button>
        <button
          className="consent-action consent-action--approve"
          type="submit"
          value="approve"
          disabled={
            contextSubmittingAction !== null ||
            decisionConflictBlocked ||
            decisionExpired ||
            (lockedDecisionAction !== null && lockedDecisionAction !== "approve")
          }
        >
          {contextSubmittingAction === "approve" ? "Submitting approval…" : "Approve remedy"}
        </button>
      </form>
    );
  } else if (snapshot !== null && claimedDecision !== null) {
    content = (
      <>
        <p className="inspector-summary">{snapshot.currentStepSummary}</p>
        <div className="inspector-status">
          <span className="status-symbol status-symbol--pending_approval" aria-hidden="true" />
          <div><span>Status</span><strong>Decision claimed</strong></div>
        </div>
        <dl className="evidence-list consent-evidence">
          <div><dt>Recovery ID</dt><dd className="mono">{snapshot.recoveryId}</dd></div>
          <div><dt>Claimed action</dt><dd>{claimedDecision.action}</dd></div>
          <div><dt>Remedy digest</dt><dd><code>{visibleDigest(claimedDecision.remedyDigest)}</code></dd></div>
          <div><dt>Expiry</dt><dd><time dateTime={claimedDecision.expiry}>{utcDisplay(claimedDecision.expiry)}</time></dd></div>
        </dl>
        <div className="execution-boundary" aria-live="polite">
          <strong>
            {decisionConflictBlocked
              ? "Decision state changed — checking authoritative recovery evidence"
              : decisionExpired
              ? "Decision deadline reached — checking the authoritative outcome"
              : "A durable exact decision is ready to resume."}
          </strong>
          <p>
            {decisionConflictBlocked
              ? "Resume is disabled until refreshed server evidence replaces this stale context."
              : decisionExpired
              ? "Resume is disabled while the server refreshes claim evidence."
              : "Only the server-authored claimed action can continue; no new consent can be created here."}
          </p>
        </div>
        {error === null ? null : <p role="alert">{error}</p>}
        <p className="decision-status" aria-live="polite">{statusMessage}</p>
      </>
    );
    const resumeLabel = claimedDecision.action === "approve"
      ? "Resume exact approval"
      : "Resume exact decline";
    footer = (
      <form
        className="consent-actions"
        onSubmit={(event) => {
          event.preventDefault();
          void resumeDecision();
        }}
      >
        <button
          className={`consent-action consent-action--${claimedDecision.action}`}
          type="submit"
          disabled={
            contextSubmittingAction !== null ||
            decisionConflictBlocked ||
            decisionExpired
          }
        >
          {contextSubmittingAction === claimedDecision.action ? "Resuming exact decision…" : resumeLabel}
        </button>
      </form>
    );
  } else if (snapshot !== null && hasIneligibleApproval) {
    content = (
      <>
        <p className="inspector-summary">{snapshot.currentStepSummary}</p>
        <div className="execution-boundary" role="alert">
          <strong>Policy-ineligible consent is unavailable.</strong>
          <p>No public decision action can be created from this recovery snapshot.</p>
        </div>
      </>
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
      busy={contextSubmittingAction !== null}
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
