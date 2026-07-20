import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  LIVE_ADMISSION_MESSAGES,
  LiveAdmissionError,
  RecoveryLookupError,
  createRecovery,
  getHealth,
  getReceipt,
  getRecovery,
} from "./api/client";
import { AppHeader } from "./components/AppHeader";
import { EventLedger } from "./components/EventLedger";
import { EvidenceInspector } from "./components/EvidenceInspector";
import { Lifecycle } from "./components/Lifecycle";
import { ProvenanceStrip } from "./components/ProvenanceStrip";
import { ScenarioRail } from "./components/ScenarioRail";
import type { HealthStatus } from "./domain/runtime";
import { deriveRuntimePresentation } from "./domain/runtime";
import type {
  RecoveryReceipt,
  RecoveryScenario,
  RecoverySnapshot,
  RecoveryStatus,
  ScenarioId,
} from "./domain/recovery";
import { isTerminalRecoveryStatus } from "./domain/recovery";
import { recoveryScenarios } from "./fixtures/recoveries";
import { useRecoveryEvents } from "./hooks/useRecovery";
import {
  clearHotelRecoveryHint,
  readHotelRecoveryHint,
  writeHotelRecoveryHint,
} from "./recoverySession";

const MOBILE_QUERY = "(max-width: 759px)";
const SDK_QA_NOTICE =
  "Live recovery is unavailable in this demo. You are viewing the deterministic SDK QA trace; replay remains available explicitly.";
const EXPLICIT_REPLAY_NOTICE =
  "Live recovery is unavailable in this demo. You are viewing an explicitly requested replay fixture.";
const EXPLICIT_DEMO_NOTICE =
  "Live recovery is unavailable in this demo. No fallback run has started; choose a replay fixture or SDK QA trace explicitly.";
const AWAITING_DEMO_NOTICE =
  "Awaiting authoritative server evidence for the explicitly requested demo run.";
const RETRYABLE_RESUME_NOTICE =
  "Saved recovery evidence is temporarily unavailable. Its same-tab recovery ID was retained, and no server state or action has been accepted.";

type HotelResumeState =
  | "checking"
  | "none"
  | "invalid"
  | "retryable"
  | "retrying"
  | "restored";

const HOTEL_IDLE_SCENARIO: RecoveryScenario = {
  id: "hotel",
  title: "Hotel booking recovery",
  summary: "No server recovery has been started in this browser session.",
  executionMode: "replay_fixture",
  status: "in_progress",
  currentStep: 0,
  currentStepSummary: "No server run started.",
  lifecycleDetails: {
    Detect: "No server run started.",
    Prove: "No provider evidence has been requested.",
    Negotiate: "No remedy has been prepared.",
    Authorize: "No consent request exists.",
    Execute: "No provider dispatch has been authorized.",
    "Verify & seal": "No server receipt exists.",
  },
  evidence: [],
};

function useMobileLayout(): boolean {
  const [mobile, setMobile] = useState(
    () => typeof window.matchMedia === "function" && window.matchMedia(MOBILE_QUERY).matches,
  );

  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const media = window.matchMedia(MOBILE_QUERY);
    const update = (event: MediaQueryListEvent) => setMobile(event.matches);
    setMobile(media.matches);
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

  return mobile;
}

function serverStatusLabel(status: RecoveryStatus, hasPendingApproval: boolean): string {
  switch (status) {
    case "completed": return "Completed";
    case "closed_without_action": return "Closed without action";
    case "outcome_unknown": return "Outcome unknown";
    case "pending_approval": return hasPendingApproval ? "Awaiting decision" : "Decision in progress";
    case "in_progress": return "Recovery in progress";
  }
}

function workspaceLabel(mode: RecoverySnapshot["executionMode"] | undefined): string {
  switch (mode) {
    case "openai_live": return "Live agent workspace";
    case "sdk_stub": return "SDK QA workspace";
    case "replay_fixture": return "Replay workspace";
    case undefined: return "Recovery workspace";
  }
}

function serverLifecycleDetails(snapshot: RecoverySnapshot): RecoveryScenario["lifecycleDetails"] {
  if (snapshot.scenarioId === "api-quota") {
    return {
      Detect: "Server detected demand of 1200 units above the 1000-unit baseline ceiling.",
      Prove: "Provider proved a 1000-unit baseline ceiling.",
      Negotiate: "A 250-unit us-east-1 burst raises the ceiling to 1250.",
      Authorize:
        "Delegated authority covers 300 USD minor units within a 500-unit limit; approval count is zero.",
      Execute: "The demo quota adapter verified execution at the temporary 1250-unit ceiling.",
      "Verify & seal":
        "Execution was verified and temporary permission quota-burst-demo-us-east-1 was revoked.",
    };
  }
  const recorded = {
    Detect: "Server recovery detected the hotel booking conflict.",
    Prove: "Server-side recovery evidence was recorded.",
    Negotiate: "One exact replacement remedy was prepared.",
  } as const;

  switch (snapshot.status) {
    case "completed":
      return {
        ...recorded,
        Authorize: "Exact remedy approval was accepted by the server.",
        Execute: "Approved provider dispatch completed.",
        "Verify & seal": "Provider verification and receipt sealing were recorded.",
      };
    case "closed_without_action":
      return {
        ...recorded,
        Authorize: "The exact remedy was declined and its permission was revoked.",
        Execute: "Provider dispatch did not begin.",
        "Verify & seal": "Cancellation evidence was sealed by the server.",
      };
    case "outcome_unknown":
      return {
        ...recorded,
        Authorize: "The decline was recorded after dispatch may have begun.",
        Execute: "Provider dispatch may have begun; its outcome is unknown.",
        "Verify & seal": "Uncertain-outcome evidence was sealed by the server.",
      };
    case "pending_approval":
      return {
        ...recorded,
        Authorize:
          snapshot.pendingApproval === null
            ? "A durable decision claim is being resolved."
            : snapshot.currentStepSummary,
        Execute:
          snapshot.pendingApproval === null
            ? "The execution outcome is not yet available."
            : "Provider dispatch has not begun.",
        "Verify & seal": "Waiting for the durable decision outcome.",
      };
    case "in_progress":
      return {
        ...recorded,
        Authorize: snapshot.currentStepSummary,
        Execute: "No provider dispatch has been recorded.",
        "Verify & seal": "No terminal evidence is available.",
      };
  }
}

export default function App() {
  const mobile = useMobileLayout();
  const [activeId, setActiveId] = useState<ScenarioId>("hotel");
  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [healthError, setHealthError] = useState(false);
  const [hotelSnapshot, setHotelSnapshot] = useState<RecoverySnapshot | null>(null);
  const [hotelResumeState, setHotelResumeState] = useState<HotelResumeState>("checking");
  const [hotelLiveLoading, setHotelLiveLoading] = useState(false);
  const [quotaSnapshot, setQuotaSnapshot] = useState<RecoverySnapshot | null>(null);
  const [quotaLoading, setQuotaLoading] = useState(false);
  const [quotaError, setQuotaError] = useState<string | null>(null);
  const quotaStartedRef = useRef(false);
  const hotelStartGenerationRef = useRef(0);
  const hotelLiveStartInFlightRef = useRef(false);
  const hotelLiveControllerRef = useRef<AbortController | null>(null);
  const hotelResumeInFlightRef = useRef(false);
  const hotelResumeControllerRef = useRef<AbortController | null>(null);
  const hotelResumeRecoveryIdRef = useRef<string | null>(null);
  const invalidResumeObservedRef = useRef(false);
  const [replayFallback, setReplayFallback] = useState<string | null>(null);
  const [replayLoading, setReplayLoading] = useState(false);
  const [replayError, setReplayError] = useState<string | null>(null);
  const [sdkLoading, setSdkLoading] = useState(false);
  const [sdkError, setSdkError] = useState<string | null>(null);
  const [hotelStartError, setHotelStartError] = useState<string | null>(null);
  const [receipts, setReceipts] = useState<Record<string, RecoveryReceipt>>({});
  const [receiptLoading, setReceiptLoading] = useState<Record<string, boolean>>({});
  const [receiptErrors, setReceiptErrors] = useState<Record<string, string>>({});
  const receiptRequestsRef = useRef(new Set<string>());
  const completedReceiptIdsRef = useRef(new Set<string>());
  const terminalRefreshesRef = useRef(new Set<string>());
  const terminalRefreshCompletedRef = useRef(new Set<string>());
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  const evidenceTriggerRef = useRef<HTMLButtonElement>(null);
  const hotelRestorationUnresolved =
    hotelResumeState === "checking" ||
    hotelResumeState === "retryable" ||
    hotelResumeState === "retrying";

  const activeScenario =
    recoveryScenarios.find((scenario) => scenario.id === activeId) ?? recoveryScenarios[0];

  const acceptHotelSnapshot = useCallback((snapshot: RecoverySnapshot) => {
    if (snapshot.scenarioId !== "hotel") return;
    writeHotelRecoveryHint(snapshot.recoveryId);
    setHotelSnapshot(snapshot);
  }, []);

  const restoreSavedHotelRecovery = useCallback((
    recoveryId: string,
    retry = false,
  ): AbortController | null => {
    if (hotelResumeInFlightRef.current) return null;
    const controller = new AbortController();
    hotelResumeInFlightRef.current = true;
    hotelResumeControllerRef.current = controller;
    hotelResumeRecoveryIdRef.current = recoveryId;
    const generation = ++hotelStartGenerationRef.current;
    setHotelSnapshot(null);
    setHotelResumeState(retry ? "retrying" : "checking");
    setReplayFallback(null);
    setReplayError(null);
    setSdkError(null);
    setHotelStartError(null);

    void getRecovery(recoveryId, controller.signal)
      .then((snapshot) => {
        if (
          controller.signal.aborted ||
          hotelStartGenerationRef.current !== generation ||
          hotelResumeControllerRef.current !== controller
        ) {
          return;
        }
        if (snapshot.scenarioId !== "hotel") {
          invalidResumeObservedRef.current = true;
          hotelResumeRecoveryIdRef.current = null;
          clearHotelRecoveryHint();
          setHotelResumeState("invalid");
          return;
        }
        acceptHotelSnapshot(snapshot);
        setHotelResumeState("restored");
      })
      .catch((error: unknown) => {
        if (
          controller.signal.aborted ||
          hotelStartGenerationRef.current !== generation ||
          hotelResumeControllerRef.current !== controller
        ) {
          return;
        }
        if (error instanceof DOMException && error.name === "AbortError") return;
        if (
          error instanceof RecoveryLookupError &&
          error.disposition === "terminal"
        ) {
          invalidResumeObservedRef.current = true;
          hotelResumeRecoveryIdRef.current = null;
          clearHotelRecoveryHint();
          setHotelResumeState("invalid");
          return;
        }
        setHotelSnapshot(null);
        setHotelResumeState("retryable");
      })
      .finally(() => {
        if (hotelResumeControllerRef.current !== controller) return;
        hotelResumeControllerRef.current = null;
        hotelResumeInFlightRef.current = false;
      });

    return controller;
  }, [acceptHotelSnapshot]);

  const loadReceipt = useCallback(async (recoveryId: string) => {
    if (
      receiptRequestsRef.current.has(recoveryId) ||
      completedReceiptIdsRef.current.has(recoveryId)
    ) return;
    receiptRequestsRef.current.add(recoveryId);
    setReceiptLoading((current) => ({ ...current, [recoveryId]: true }));
    setReceiptErrors((current) => {
      const next = { ...current };
      delete next[recoveryId];
      return next;
    });
    try {
      const receipt = await getReceipt(recoveryId);
      completedReceiptIdsRef.current.add(recoveryId);
      setReceipts((current) => ({ ...current, [recoveryId]: receipt }));
    } catch {
      setReceiptErrors((current) => ({
        ...current,
        [recoveryId]: "The authoritative terminal receipt could not be loaded.",
      }));
    } finally {
      receiptRequestsRef.current.delete(recoveryId);
      setReceiptLoading((current) => ({ ...current, [recoveryId]: false }));
    }
  }, []);

  const startQuota = useCallback(async () => {
    if (quotaStartedRef.current) return;
    quotaStartedRef.current = true;
    setQuotaLoading(true);
    setQuotaError(null);
    try {
      setQuotaSnapshot(await createRecovery("api-quota", "sdk_stub"));
    } catch {
      quotaStartedRef.current = false;
      setQuotaError("The deterministic quota trace could not be loaded.");
    } finally {
      setQuotaLoading(false);
    }
  }, []);

  const selectScenario = useCallback(
    (scenarioId: ScenarioId) => {
      if (hotelRestorationUnresolved) return;
      setActiveId(scenarioId);
      setEvidenceOpen(false);
      if (scenarioId === "api-quota" && quotaSnapshot === null && !quotaLoading) {
        void startQuota();
      }
    },
    [hotelRestorationUnresolved, quotaLoading, quotaSnapshot, startQuota],
  );

  const startReplay = useCallback(async (
    signal?: AbortSignal,
    explicit = false,
    fromExplicitDemoChoice = false,
  ) => {
    if (hotelRestorationUnresolved) return;
    const generation = ++hotelStartGenerationRef.current;
    setSdkLoading(false);
    setSdkError(null);
    setReplayLoading(true);
    setReplayError(null);
    if (fromExplicitDemoChoice) setReplayFallback(AWAITING_DEMO_NOTICE);
    try {
      const snapshot = await createRecovery("hotel", "replay_fixture", signal);
      if (hotelStartGenerationRef.current !== generation) return;
      acceptHotelSnapshot(snapshot);
      if (explicit) setReplayFallback(EXPLICIT_REPLAY_NOTICE);
    } catch (error: unknown) {
      if (hotelStartGenerationRef.current !== generation) return;
      if (error instanceof DOMException && error.name === "AbortError") return;
      if (fromExplicitDemoChoice) setReplayFallback(EXPLICIT_DEMO_NOTICE);
      setReplayError("The replay fixture could not be started. You can retry explicitly.");
    } finally {
      if (hotelStartGenerationRef.current === generation) setReplayLoading(false);
    }
  }, [acceptHotelSnapshot, hotelRestorationUnresolved]);

  const startSdkQa = useCallback(async () => {
    if (sdkLoading || hotelRestorationUnresolved) return;
    const fromExplicitDemoChoice = replayFallback === EXPLICIT_DEMO_NOTICE;
    const generation = ++hotelStartGenerationRef.current;
    setReplayLoading(false);
    setReplayError(null);
    setSdkLoading(true);
    setSdkError(null);
    if (fromExplicitDemoChoice) setReplayFallback(AWAITING_DEMO_NOTICE);
    try {
      const snapshot = await createRecovery("hotel", "sdk_stub");
      if (hotelStartGenerationRef.current !== generation) return;
      acceptHotelSnapshot(snapshot);
      if (replayFallback !== null) setReplayFallback(SDK_QA_NOTICE);
      setEvidenceOpen(false);
    } catch {
      if (hotelStartGenerationRef.current !== generation) return;
      if (fromExplicitDemoChoice) setReplayFallback(EXPLICIT_DEMO_NOTICE);
      setSdkError("The deterministic SDK QA trace could not be started.");
    } finally {
      if (hotelStartGenerationRef.current === generation) setSdkLoading(false);
    }
  }, [acceptHotelSnapshot, hotelRestorationUnresolved, replayFallback, sdkLoading]);

  const startLive = useCallback(() => {
    if (
      hotelLiveStartInFlightRef.current ||
      health?.backend !== "openai" ||
      !health.liveReady ||
      hotelRestorationUnresolved
    ) {
      return;
    }
    hotelLiveStartInFlightRef.current = true;
    const generation = ++hotelStartGenerationRef.current;
    const controller = new AbortController();
    hotelLiveControllerRef.current = controller;
    setHotelLiveLoading(true);
    setHotelStartError(null);
    setReplayError(null);
    setReplayFallback(null);

    void createRecovery("hotel", "openai_live", controller.signal)
      .then((snapshot) => {
        if (hotelStartGenerationRef.current !== generation) return;
        acceptHotelSnapshot(snapshot);
        setEvidenceOpen(false);
      })
      .catch((error: unknown) => {
        if (hotelStartGenerationRef.current !== generation) return;
        if (error instanceof DOMException && error.name === "AbortError") return;
        if (error instanceof LiveAdmissionError) {
          setReplayFallback(error.message);
          hotelLiveStartInFlightRef.current = false;
          hotelLiveControllerRef.current = null;
          setHotelLiveLoading(false);
          void startReplay();
          return;
        }
        setHotelStartError("The live recovery could not be started. No fallback run was created.");
      })
      .finally(() => {
        if (hotelLiveControllerRef.current !== controller) return;
        hotelLiveControllerRef.current = null;
        hotelLiveStartInFlightRef.current = false;
        if (hotelStartGenerationRef.current === generation) setHotelLiveLoading(false);
      });
  }, [acceptHotelSnapshot, health, hotelRestorationUnresolved, startReplay]);

  useEffect(() => {
    const controller = new AbortController();
    setHealthError(false);
    void getHealth(controller.signal)
      .then(setHealth)
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setHealth(null);
        setHealthError(true);
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const { hadHint, recoveryId } = readHotelRecoveryHint();
    if (!hadHint) {
      setHotelResumeState(invalidResumeObservedRef.current ? "invalid" : "none");
      return;
    }
    if (recoveryId === null) {
      invalidResumeObservedRef.current = true;
      setHotelResumeState("invalid");
      return;
    }

    const controller = restoreSavedHotelRecovery(recoveryId);
    return () => {
      controller?.abort();
      if (hotelResumeControllerRef.current !== controller) return;
      hotelResumeControllerRef.current = null;
      hotelResumeInFlightRef.current = false;
    };
  }, [restoreSavedHotelRecovery]);

  useEffect(() => {
    if (
      health === null ||
      hotelRestorationUnresolved ||
      hotelResumeState === "restored"
    ) {
      return;
    }
    const controller = new AbortController();
    const liveAvailable = health.backend === "openai" && health.liveReady;
    if (hotelResumeState === "invalid") {
      setHotelSnapshot(null);
      setReplayError(null);
      setHotelStartError(null);
      setReplayFallback(liveAvailable ? null : EXPLICIT_DEMO_NOTICE);
      return () => controller.abort();
    }
    if (!liveAvailable) {
      setHotelSnapshot(null);
      setReplayFallback(LIVE_ADMISSION_MESSAGES.live_unavailable);
      void startReplay(controller.signal);
      return () => controller.abort();
    }

    setReplayFallback(null);
    setReplayError(null);
    setHotelStartError(null);
    return () => controller.abort();
  }, [health, hotelRestorationUnresolved, hotelResumeState, startReplay]);

  useEffect(
    () => () => {
      hotelStartGenerationRef.current += 1;
      hotelLiveStartInFlightRef.current = false;
      hotelLiveControllerRef.current?.abort();
      hotelLiveControllerRef.current = null;
      hotelResumeControllerRef.current?.abort();
      hotelResumeControllerRef.current = null;
      hotelResumeInFlightRef.current = false;
    },
    [],
  );

  useEffect(() => {
    const terminalSnapshots = [hotelSnapshot, quotaSnapshot];
    for (const snapshot of terminalSnapshots) {
      if (snapshot !== null && isTerminalRecoveryStatus(snapshot.status)) {
        void loadReceipt(snapshot.recoveryId);
      }
    }
  }, [hotelSnapshot, loadReceipt, quotaSnapshot]);

  const activeSnapshot = activeId === "hotel" ? hotelSnapshot : quotaSnapshot;
  const hotelLifecyclePhase =
    activeId !== "hotel" || activeSnapshot !== null
      ? "active"
      : hotelRestorationUnresolved ||
          hotelLiveLoading ||
          replayLoading ||
          sdkLoading
        ? "awaiting"
        : "idle";
  const activeRecoveryId = activeSnapshot?.recoveryId ?? null;
  const eventState = useRecoveryEvents(activeRecoveryId);
  const activeEvents = useMemo(
    () => eventState.events.filter((event) => event.recoveryId === activeRecoveryId),
    [activeRecoveryId, eventState.events],
  );
  const terminalEvent = [...activeEvents].reverse().find((event) => event.terminal);

  useEffect(() => {
    if (terminalEvent === undefined || activeSnapshot === null) return;
    const refreshKey = `${terminalEvent.recoveryId}:${terminalEvent.seq}`;
    if (
      terminalRefreshesRef.current.has(refreshKey) ||
      terminalRefreshCompletedRef.current.has(refreshKey)
    ) return;
    terminalRefreshesRef.current.add(refreshKey);
    const scenarioId = activeSnapshot.scenarioId;
    const requestedRecoveryId = terminalEvent.recoveryId;
    void getRecovery(requestedRecoveryId)
      .then((snapshot) => {
        if (snapshot.scenarioId !== scenarioId) return;
        terminalRefreshCompletedRef.current.add(refreshKey);
        if (scenarioId === "hotel") {
          setHotelSnapshot((current) =>
            current?.recoveryId === requestedRecoveryId ? snapshot : current,
          );
        } else {
          setQuotaSnapshot((current) =>
            current?.recoveryId === requestedRecoveryId ? snapshot : current,
          );
        }
      })
      .catch(() => {
        // The event remains visible; no terminal snapshot claim is synthesized.
      })
      .finally(() => terminalRefreshesRef.current.delete(refreshKey));
    void loadReceipt(requestedRecoveryId);
  }, [activeSnapshot, loadReceipt, terminalEvent]);

  const activeScenarioView = useMemo<RecoveryScenario>(() => {
    if (activeSnapshot !== null) {
      return {
        ...activeScenario,
        executionMode: activeSnapshot.executionMode,
        status: activeSnapshot.status,
        currentStep: activeSnapshot.currentStep,
        currentStepSummary: activeSnapshot.currentStepSummary,
        lifecycleDetails: serverLifecycleDetails(activeSnapshot),
      };
    }
    if (activeId === "hotel") return HOTEL_IDLE_SCENARIO;
    const waiting = quotaLoading
      ? "Starting the deterministic quota SDK trace."
      : "No server quota proof is available.";
    return {
      ...activeScenario,
      executionMode: "sdk_stub",
      status: "in_progress",
      currentStep: 0,
      currentStepSummary: waiting,
      lifecycleDetails: {
        Detect: waiting,
        Prove: "Waiting for provider ceiling evidence.",
        Negotiate: "Waiting for exact temporary burst terms.",
        Authorize: "Waiting for delegated-authority evidence.",
        Execute: "No quota adapter execution has been recorded.",
        "Verify & seal": "No terminal quota receipt is available.",
      },
      evidence: [
        { label: "Execution mode", value: "sdk_stub", monospace: true },
        { label: "Server evidence", value: quotaLoading ? "Loading" : "Unavailable" },
      ],
    };
  }, [activeId, activeScenario, activeSnapshot, quotaLoading]);

  const refreshHotelSnapshot = useCallback(async () => {
    if (hotelSnapshot === null) return;
    const requestedRecoveryId = hotelSnapshot.recoveryId;
    const snapshot = await getRecovery(requestedRecoveryId);
    if (snapshot.scenarioId !== "hotel") return;
    setHotelSnapshot((current) =>
      current?.recoveryId === requestedRecoveryId ? snapshot : current,
    );
    if (isTerminalRecoveryStatus(snapshot.status)) {
      void loadReceipt(snapshot.recoveryId);
    }
  }, [hotelSnapshot, loadReceipt]);

  const presentation = useMemo(
    () => health === null || activeSnapshot === null
      ? null
      : deriveRuntimePresentation(health, activeSnapshot),
    [activeSnapshot, health],
  );
  const activeReceipt =
    activeSnapshot === null ? null : receipts[activeSnapshot.recoveryId] ?? null;
  const activeReceiptLoading =
    activeSnapshot === null ? false : receiptLoading[activeSnapshot.recoveryId] === true;
  const activeReceiptError =
    activeSnapshot === null ? null : receiptErrors[activeSnapshot.recoveryId] ?? null;
  const evidenceLabel =
    terminalEvent !== undefined
      ? "Review recovery receipt"
      : activeSnapshot?.pendingApproval !== null && activeSnapshot?.pendingApproval !== undefined
      ? "Review exact remedy"
      : activeReceipt !== null || (activeSnapshot !== null && isTerminalRecoveryStatus(activeSnapshot.status))
        ? "Review recovery receipt"
        : "Review recovery evidence";
  const quotaEmptyState =
    activeId === "api-quota" && activeSnapshot === null
      ? {
          title: quotaLoading ? "Loading deterministic trace" : "Quota proof unavailable",
          summary: activeScenarioView.currentStepSummary,
          boundaryTitle: "No completed quota receipt is being shown.",
          boundaryText: "Only server-returned SDK evidence can complete this scenario.",
        }
      : null;
  const hotelEmptyState =
    activeId === "hotel" && activeSnapshot === null
      ? {
          title:
            hotelLifecyclePhase === "awaiting"
              ? "Awaiting server evidence"
              : "No recovery started",
          summary:
            hotelLifecyclePhase === "awaiting"
              ? "Awaiting server evidence."
              : "No server run started.",
          boundaryTitle:
            hotelLifecyclePhase === "awaiting"
              ? "No execution-mode claim is available yet."
              : "No active recovery evidence is being shown.",
          boundaryText:
            hotelLifecyclePhase === "awaiting"
              ? "Waiting for an authoritative server recovery snapshot."
              : "Start a live recovery explicitly, or use a disclosed replay or SDK QA action when available.",
        }
      : null;
  const liveAvailable = health?.backend === "openai" && health.liveReady;

  return (
    <div className="app-frame">
      <AppHeader workspace={workspaceLabel(activeSnapshot?.executionMode)} />
      <div className="console-shell" id="workspace">
        <ScenarioRail
          scenarios={recoveryScenarios}
          activeId={activeId}
          disabled={hotelRestorationUnresolved}
          mobile={mobile}
          onSelect={selectScenario}
        />

        <main className="workspace">
          <ProvenanceStrip
            presentation={presentation}
            healthError={healthError}
            awaitingSnapshot={
              activeId === "api-quota" && health !== null && activeSnapshot === null
            }
            awaitingServerEvidence={
              activeId === "hotel" && hotelLifecyclePhase === "awaiting"
            }
            noRunStarted={activeId === "hotel" && hotelLifecyclePhase === "idle"}
          />
          {activeId === "hotel" &&
          liveAvailable &&
          !hotelRestorationUnresolved &&
          activeSnapshot === null &&
          replayFallback === null ? (
            <section className="live-start" aria-live="polite">
              <div>
                <p className="eyebrow">Live recovery</p>
                <p>
                  {hotelLiveLoading
                    ? "Waiting for an authoritative live recovery snapshot."
                    : "No server run starts until you explicitly request one."}
                </p>
              </div>
              <button
                type="button"
                disabled={hotelLiveLoading}
                onClick={startLive}
              >
                {hotelLiveLoading ? "Starting live recovery…" : "Start live recovery"}
              </button>
            </section>
          ) : null}
          {activeId === "hotel" &&
          (hotelResumeState === "retryable" || hotelResumeState === "retrying") ? (
            <section
              className="live-start"
              aria-busy={hotelResumeState === "retrying"}
              aria-live="polite"
            >
              <div>
                <p className="eyebrow">Saved recovery</p>
                <p>{RETRYABLE_RESUME_NOTICE}</p>
              </div>
              <button
                type="button"
                disabled={hotelResumeState === "retrying"}
                onClick={() => {
                  const recoveryId = hotelResumeRecoveryIdRef.current;
                  if (recoveryId !== null) restoreSavedHotelRecovery(recoveryId, true);
                }}
              >
                {hotelResumeState === "retrying"
                  ? "Retrying saved recovery…"
                  : "Retry saved recovery"}
              </button>
            </section>
          ) : null}
          {activeId === "hotel" && replayFallback !== null ? (
            <section className="replay-fallback" aria-live="polite">
              <div>
                <p className="eyebrow">
                  {replayFallback === EXPLICIT_DEMO_NOTICE ||
                  replayFallback === AWAITING_DEMO_NOTICE
                    ? "Available demo modes"
                    : "Replay fallback"}
                </p>
                <p>{replayFallback}</p>
                {replayError === null ? null : <p role="alert">{replayError}</p>}
                {sdkError === null ? null : <p role="alert">{sdkError}</p>}
              </div>
              <div className="fallback-actions">
                <button
                  type="button"
                  aria-label="Run replay fixture"
                  disabled={replayLoading || sdkLoading}
                  onClick={() =>
                    void startReplay(
                      undefined,
                      true,
                      replayFallback === EXPLICIT_DEMO_NOTICE,
                    )
                  }
                >
                  {replayLoading ? "Loading replay fixture…" : "Run replay fixture"}
                </button>
                <button
                  type="button"
                  disabled={
                    sdkLoading ||
                    (replayLoading && replayFallback === AWAITING_DEMO_NOTICE)
                  }
                  onClick={() => void startSdkQa()}
                >
                  {sdkLoading ? "Starting SDK QA trace…" : "Run SDK QA trace"}
                </button>
              </div>
            </section>
          ) : null}
          {activeId === "api-quota" && quotaError !== null ? <p role="alert">{quotaError}</p> : null}
          {activeId === "hotel" && hotelStartError !== null ? <p role="alert">{hotelStartError}</p> : null}
          <section className="recovery-heading" aria-labelledby="recovery-title">
            <div>
              <p className="eyebrow">Active recovery</p>
              <h1 id="recovery-title">{activeScenarioView.title}</h1>
              {activeSnapshot !== null && isTerminalRecoveryStatus(activeSnapshot.status) ? (
                <p className="terminal-summary">{activeSnapshot.currentStepSummary}</p>
              ) : null}
            </div>
            <span
              className={`recovery-state recovery-state--${
                activeSnapshot?.status ?? activeScenarioView.status
              }`}
            >
              {activeSnapshot !== null
                ? serverStatusLabel(activeSnapshot.status, activeSnapshot.pendingApproval !== null)
                : activeId === "hotel"
                  ? hotelLifecyclePhase === "awaiting"
                    ? "Awaiting evidence"
                    : "Not started"
                : activeScenarioView.status === "completed"
                  ? "Completed fixture"
                  : "Awaiting boundary"}
            </span>
          </section>
          <Lifecycle phase={hotelLifecyclePhase} scenario={activeScenarioView} />
        </main>

        {mobile ? (
          <button
            ref={evidenceTriggerRef}
            className="evidence-trigger"
            type="button"
            onClick={() => setEvidenceOpen(true)}
          >
            {evidenceLabel}
          </button>
        ) : null}

        <EvidenceInspector
          scenario={activeScenarioView}
          snapshot={activeSnapshot}
          receipt={activeReceipt}
          events={activeEvents}
          receiptLoading={activeReceiptLoading}
          receiptError={activeReceiptError}
          terminalEventObserved={terminalEvent !== undefined}
          emptyState={hotelEmptyState ?? quotaEmptyState}
          mobile={mobile}
          open={mobile ? evidenceOpen : true}
          onClose={() => setEvidenceOpen(false)}
          onRetryReceipt={
            activeSnapshot === null
              ? undefined
              : () => {
                  const { recoveryId, scenarioId } = activeSnapshot;
                  void getRecovery(recoveryId)
                    .then((snapshot) => {
                      if (snapshot.scenarioId !== scenarioId) return;
                      if (scenarioId === "hotel") {
                        setHotelSnapshot((current) =>
                          current?.recoveryId === recoveryId ? snapshot : current,
                        );
                      } else {
                        setQuotaSnapshot((current) =>
                          current?.recoveryId === recoveryId ? snapshot : current,
                        );
                      }
                    })
                    .catch(() => {});
                  void loadReceipt(recoveryId);
                }
          }
          returnFocusRef={evidenceTriggerRef}
          onServerSuccess={refreshHotelSnapshot}
        />

        <EventLedger
          events={activeEvents}
          error={eventState.error}
          mobile={mobile}
          rootTraceId={activeSnapshot?.rootTraceId ?? null}
        />
      </div>
    </div>
  );
}
