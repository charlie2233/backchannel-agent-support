import {
  type KeyboardEvent as ReactKeyboardEvent,
  type RefObject,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";

import { postDecision, PublicApiError } from "../api/client";
import type {
  DecisionAction,
  DecisionRequest,
  DecisionResponse,
  PendingApproval,
  RecoverySnapshot,
} from "../domain/recovery";
import { lifecycleSteps } from "../domain/recovery";
import type { RuntimePresentation } from "../domain/runtime";
import {
  persistPendingDecision,
  readPendingDecision,
  type StoredDecisionClaim,
} from "../domain/session";
import {
  TechnicalEvidence,
  type TechnicalEvidencePreferences,
  type TechnicalEvidencePreferenceScope,
} from "./TechnicalEvidence";

export interface ConsentSheetProps {
  snapshot: RecoverySnapshot;
  displayMode: "inline" | "dialog";
  open?: boolean;
  onClose?: (reason: "close_button" | "escape") => void;
  triggerRef?: RefObject<HTMLElement | null>;
  fallbackFocusRef?: RefObject<HTMLElement | null>;
  scenarioTitle?: string;
  runtimePresentation?: RuntimePresentation | null;
  runtimeHealthError?: boolean;
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
  const instant = new Date(timestamp);
  if (Number.isNaN(instant.getTime())) {
    return timestamp;
  }
  return `${instant.toISOString().slice(0, 19).replace("T", " ")} UTC`;
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

function matchingStoredClaim(
  snapshot: RecoverySnapshot,
  approval: PendingApproval,
): StoredDecisionClaim | null {
  const stored = readPendingDecision();
  return stored !== null &&
    stored.recoveryId === snapshot.recoveryId &&
    requestMatchesApproval(stored.request, approval)
    ? stored
    : null;
}

function focusableElements(container: HTMLElement): HTMLElement[] {
  return Array.from(
    container.querySelectorAll<HTMLElement>(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), summary, textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ),
  ).filter((element) => !element.hasAttribute("hidden"));
}

function isValidFocusTarget(target: HTMLElement | null): target is HTMLElement {
  if (target === null || !target.isConnected || target.closest("[hidden], [inert]") !== null) {
    return false;
  }
  const style = window.getComputedStyle(target);
  return style.display !== "none" && style.visibility !== "hidden";
}

export function ConsentSheet({
  snapshot,
  displayMode,
  open = displayMode === "inline",
  onClose,
  triggerRef,
  fallbackFocusRef,
  scenarioTitle,
  runtimePresentation = null,
  runtimeHealthError = false,
  externalSubmittingAction = null,
  onDecisionAccepted,
  clientDecisionIdFactory = defaultDecisionId,
}: ConsentSheetProps) {
  const approval = snapshot.pendingApproval;
  if (approval === null) {
    throw new Error("ConsentSheet requires a pending approval");
  }
  const synchronousStoredClaim = matchingStoredClaim(snapshot, approval);
  const [submittingAction, setSubmittingAction] = useState<DecisionAction | null>(null);
  const [lockedDecision, setLockedDecision] = useState<StoredDecisionClaim | null>(
    synchronousStoredClaim,
  );
  const [acceptedAction, setAcceptedAction] = useState<DecisionAction | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [technicalEvidencePreferenceState, setTechnicalEvidencePreferenceState] =
    useState<{
      recoveryId: string;
      preferences: TechnicalEvidencePreferences;
    }>(() => ({ recoveryId: snapshot.recoveryId, preferences: {} }));
  const decisionIds = useRef<Partial<Record<DecisionAction, string>>>(
    synchronousStoredClaim === null
      ? {}
      : {
          [synchronousStoredClaim.request.decision]:
            synchronousStoredClaim.request.clientDecisionId,
        },
  );
  const activeRequest = useRef<DecisionRequest | null>(
    synchronousStoredClaim?.request ?? null,
  );
  const renderedRecoveryId = useRef(snapshot.recoveryId);
  const dialogRef = useRef<HTMLDivElement>(null);
  const inlineRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const latestDisplayMode = useRef(displayMode);
  const breakpointFocusPending = useRef(false);
  const previousSurface =
    latestDisplayMode.current === "dialog" ? dialogRef.current : inlineRef.current;
  if (
    latestDisplayMode.current !== displayMode &&
    previousSurface?.contains(document.activeElement)
  ) {
    breakpointFocusPending.current = true;
  }
  latestDisplayMode.current = displayMode;
  renderedRecoveryId.current = snapshot.recoveryId;

  const stateLockedDecision =
    lockedDecision !== null &&
    lockedDecision.recoveryId === snapshot.recoveryId &&
    requestMatchesApproval(lockedDecision.request, approval)
      ? lockedDecision
      : null;
  const effectiveLockedDecision = synchronousStoredClaim ?? stateLockedDecision;
  const lockedAction = effectiveLockedDecision?.request.decision ?? null;
  const technicalEvidencePreferences =
    technicalEvidencePreferenceState.recoveryId === snapshot.recoveryId
      ? technicalEvidencePreferenceState.preferences
      : {};
  const updateTechnicalEvidencePreference = (
    scope: TechnicalEvidencePreferenceScope,
    expanded: boolean,
  ) => {
    setTechnicalEvidencePreferenceState((current) => ({
      recoveryId: snapshot.recoveryId,
      preferences: {
        ...(current.recoveryId === snapshot.recoveryId
          ? current.preferences
          : {}),
        [scope]: expanded,
      },
    }));
  };

  if (
    synchronousStoredClaim !== null &&
    activeRequest.current?.clientDecisionId !==
      synchronousStoredClaim.request.clientDecisionId
  ) {
    activeRequest.current = synchronousStoredClaim.request;
    decisionIds.current[synchronousStoredClaim.request.decision] =
      synchronousStoredClaim.request.clientDecisionId;
  }

  useEffect(() => {
    activeRequest.current = null;
    decisionIds.current = {};
    setSubmittingAction(null);
    setLockedDecision(null);
    setAcceptedAction(null);
    setError(null);
    setStatusMessage(null);
    const stored = readPendingDecision();
    if (
      stored !== null &&
      stored.recoveryId === snapshot.recoveryId &&
      requestMatchesApproval(stored.request, approval)
    ) {
      activeRequest.current = stored.request;
      decisionIds.current[stored.request.decision] = stored.request.clientDecisionId;
      setLockedDecision(stored);
    }
  }, [
    approval.remedyDigest,
    approval.remedyId,
    approval.toolCallId,
    snapshot.recoveryId,
  ]);

  useEffect(() => {
    if (displayMode !== "dialog" || !open) {
      return;
    }
    const fallbackTrigger =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const fallbackTargetAtOpen = fallbackFocusRef?.current ?? null;
    const priorOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeRef.current?.focus();
    return () => {
      document.body.style.overflow = priorOverflow;
      const restoreFocus = () => {
        const trigger =
          triggerRef === undefined ? fallbackTrigger : triggerRef.current;
        const target =
          latestDisplayMode.current === "dialog" && isValidFocusTarget(trigger)
            ? trigger
            : isValidFocusTarget(fallbackFocusRef?.current ?? null)
              ? fallbackFocusRef?.current ?? null
              : fallbackTargetAtOpen;
        if (!isValidFocusTarget(target)) {
          return false;
        }
        target.focus();
        return document.activeElement === target;
      };
      if (!restoreFocus()) {
        queueMicrotask(restoreFocus);
      }
    };
  }, [displayMode, fallbackFocusRef, open, triggerRef]);

  useLayoutEffect(() => {
    if (!breakpointFocusPending.current) {
      return;
    }
    breakpointFocusPending.current = false;
    const target = fallbackFocusRef?.current ?? null;
    if (isValidFocusTarget(target)) {
      target.focus();
    }
  }, [displayMode, fallbackFocusRef]);

  if (displayMode === "dialog" && !open) {
    return null;
  }

  const effectiveSubmittingAction = externalSubmittingAction ?? submittingAction;

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
      existingRequest?.decision === action &&
      requestMatchesApproval(existingRequest, approval)
        ? existingRequest
        : {
            decision: action,
            clientDecisionId: stableDecisionId,
            remedyId: approval.remedyId,
            remedyDigest: approval.remedyDigest,
            toolCallId: approval.toolCallId,
          };
    const submittedRecoveryId = snapshot.recoveryId;
    if (!persistPendingDecision({ recoveryId: snapshot.recoveryId, request })) {
      setError("Decision could not be saved for safe retry. No request was sent.");
      return;
    }
    activeRequest.current = request;
    setLockedDecision({ recoveryId: snapshot.recoveryId, request });
    setSubmittingAction(action);
    setError(null);
    setStatusMessage(null);
    try {
      const response = await postDecision(snapshot.recoveryId, request);
      if (renderedRecoveryId.current !== submittedRecoveryId) {
        return;
      }
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
    } catch (caught: unknown) {
      if (renderedRecoveryId.current !== submittedRecoveryId) {
        return;
      }
      const prefix =
        caught instanceof PublicApiError
          ? caught.message
          : action === "approve"
            ? "Approval could not be recorded."
            : "Decline could not be recorded.";
      setError(`${prefix} Retry the same decision.`);
    } finally {
      if (renderedRecoveryId.current === submittedRecoveryId) {
        setSubmittingAction(null);
      }
    }
  };

  const handleDialogKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (displayMode !== "dialog") {
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      onClose?.("escape");
      return;
    }
    if (event.key !== "Tab" || dialogRef.current === null) {
      return;
    }
    const focusable = focusableElements(dialogRef.current);
    if (focusable.length === 0) {
      event.preventDefault();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  const actionsDisabled = effectiveSubmittingAction !== null || acceptedAction !== null;
  const runtimeLabel =
    runtimePresentation?.label ??
    (runtimeHealthError ? "Runtime unavailable" : "Checking runtime");
  const runtimeExplanation =
    runtimePresentation?.explanation ??
    (runtimeHealthError
      ? "The health endpoint could not be verified, so no runtime claim is shown."
      : "Waiting for verified health and recovery evidence before making a runtime claim.");
  const currentLifecycleStep = lifecycleSteps[snapshot.currentStep];
  const content = (
    <>
      {displayMode === "dialog" ? (
        <div className="consent-dialog__header">
          {scenarioTitle !== undefined ? (
            <section
              className="consent-runtime-context"
              aria-label="Recovery context"
            >
              <div className="consent-runtime-context__identity">
                <p className="eyebrow">Active recovery</p>
                <strong>{scenarioTitle}</strong>
              </div>
              <div className="consent-runtime-context__facts">
                <span>{runtimeLabel}</span>
                <span>
                  Step {snapshot.currentStep + 1} of {lifecycleSteps.length}
                  {currentLifecycleStep === undefined
                    ? ""
                    : ` · ${currentLifecycleStep}`}
                </span>
              </div>
              <p className="consent-runtime-context__summary">
                {snapshot.currentStepSummary}
              </p>
              <p className="consent-runtime-context__provenance">
                {runtimeExplanation}
              </p>
            </section>
          ) : null}
          <button
            className="consent-close"
            ref={closeRef}
            type="button"
            onClick={() => onClose?.("close_button")}
          >
            Close
          </button>
        </div>
      ) : null}
      <div className="inspector-heading">
        <p className="eyebrow">Server consent record</p>
        <h2 id="approval-heading" tabIndex={-1}>
          Approve exact remedy
        </h2>
        <p>{snapshot.currentStepSummary}</p>
      </div>

      <div className="execution-boundary">
        <strong>Execution has not begun.</strong>
        <p>The server will recheck this exact consent immediately before dispatch.</p>
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
          <dt>Booking</dt>
          <dd className="mono">{approval.terms.bookingId}</dd>
        </div>
        <div>
          <dt>Action</dt>
          <dd className="mono">{approval.terms.action}</dd>
        </div>
        <div>
          <dt>Replacement</dt>
          <dd>
            {approval.terms.replacement.fromRoomType} → {approval.terms.replacement.toRoomType}
          </dd>
        </div>
        <div>
          <dt>Stay</dt>
          <dd>
            {approval.terms.stay.checkIn} → {approval.terms.stay.checkOut}
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
        <div>
          <dt>Authorization scope</dt>
          <dd>One-time authorization for this exact remedy and tool call.</dd>
        </div>
      </dl>

      <TechnicalEvidence
        expanded={displayMode === "inline"}
        preferenceScope={displayMode === "inline" ? "desktop" : "mobile"}
        preferences={technicalEvidencePreferences}
        onPreferenceChange={updateTechnicalEvidencePreference}
      >
        <dl className="evidence-list">
          <div>
            <dt>Recovery ID</dt>
            <dd className="mono">{snapshot.recoveryId}</dd>
          </div>
          <div>
            <dt>Remedy ID</dt>
            <dd className="mono">{approval.remedyId}</dd>
          </div>
          <div>
            <dt>Execution mode</dt>
            <dd className="mono">{snapshot.executionMode}</dd>
          </div>
          {snapshot.rootTraceId !== undefined ? (
            <div>
              <dt>Root trace ID</dt>
              <dd className="mono">{snapshot.rootTraceId ?? "None"}</dd>
            </div>
          ) : null}
          {snapshot.modelIds !== undefined ? (
            <div>
              <dt>Model IDs</dt>
              <dd className="mono">
                {snapshot.modelIds.length === 0
                  ? "None — no model call"
                  : snapshot.modelIds.join(", ")}
              </dd>
            </div>
          ) : null}
        </dl>
      </TechnicalEvidence>

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
    </>
  );

  if (displayMode === "dialog") {
    return createPortal(
      <div
        className="consent-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="approval-heading"
        ref={dialogRef}
        onKeyDown={handleDialogKeyDown}
      >
        <div className="consent-dialog__surface">{content}</div>
      </div>,
      document.body,
    );
  }
  return (
    <aside
      className="evidence-inspector evidence-inspector--consent"
      aria-labelledby="approval-heading"
      ref={inlineRef}
    >
      {content}
    </aside>
  );
}
