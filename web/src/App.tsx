import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  LIVE_ADMISSION_MESSAGES,
  LiveAdmissionError,
  createRecovery,
  getHealth,
  getRecovery,
} from "./api/client";
import { EvidenceInspector } from "./components/EvidenceInspector";
import { Lifecycle } from "./components/Lifecycle";
import { ProvenanceStrip } from "./components/ProvenanceStrip";
import { ScenarioRail } from "./components/ScenarioRail";
import { deriveRuntimePresentation, type HealthStatus } from "./domain/runtime";
import type {
  RecoveryScenario,
  RecoverySnapshot,
  RecoveryStatus,
  ScenarioId,
} from "./domain/recovery";
import { recoveryScenarios } from "./fixtures/recoveries";

function serverStatusLabel(status: RecoveryStatus, hasPendingApproval: boolean): string {
  switch (status) {
    case "completed":
      return "Completed";
    case "closed_without_action":
      return "Closed without action";
    case "outcome_unknown":
      return "Outcome unknown";
    case "pending_approval":
      return hasPendingApproval ? "Awaiting decision" : "Decision in progress";
    case "in_progress":
      return "Recovery in progress";
  }
}

function workspaceLabel(mode: RecoverySnapshot["executionMode"] | undefined): string {
  switch (mode) {
    case "openai_live":
      return "Live agent workspace";
    case "sdk_stub":
      return "SDK QA workspace";
    case "replay_fixture":
    case undefined:
      return "Replay workspace";
  }
}

function serverLifecycleDetails(
  snapshot: RecoverySnapshot,
): RecoveryScenario["lifecycleDetails"] {
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
        "Verify & seal": snapshot.currentStepSummary,
      };
    case "closed_without_action":
      return {
        ...recorded,
        Authorize: "The exact remedy was declined and its permission was revoked.",
        Execute: "Provider dispatch did not begin.",
        "Verify & seal": snapshot.currentStepSummary,
      };
    case "outcome_unknown":
      return {
        ...recorded,
        Authorize: "The decline was recorded after dispatch may have begun.",
        Execute: "Provider dispatch may have begun; its outcome is unknown.",
        "Verify & seal": snapshot.currentStepSummary,
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
  const [activeId, setActiveId] = useState<ScenarioId>("hotel");
  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [healthError, setHealthError] = useState(false);
  const [hotelSnapshot, setHotelSnapshot] = useState<RecoverySnapshot | null>(null);
  const [quotaSnapshot, setQuotaSnapshot] = useState<RecoverySnapshot | null>(null);
  const [quotaLoading, setQuotaLoading] = useState(false);
  const [quotaError, setQuotaError] = useState<string | null>(null);
  const quotaStartedRef = useRef(false);
  const [replayFallback, setReplayFallback] = useState<string | null>(null);
  const [replayLoading, setReplayLoading] = useState(false);
  const [replayError, setReplayError] = useState<string | null>(null);

  const activeScenario =
    recoveryScenarios.find((scenario) => scenario.id === activeId) ?? recoveryScenarios[0];

  const startQuota = useCallback(async () => {
    if (quotaStartedRef.current) {
      return;
    }
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
      setActiveId(scenarioId);
      if (scenarioId === "api-quota" && quotaSnapshot === null && !quotaLoading) {
        void startQuota();
      }
    },
    [quotaLoading, quotaSnapshot, startQuota],
  );

  const startReplay = useCallback(async (signal?: AbortSignal) => {
    setReplayLoading(true);
    setReplayError(null);
    try {
      const snapshot = await createRecovery("hotel", "replay_fixture", signal);
      setHotelSnapshot(snapshot);
    } catch (error: unknown) {
      if (error instanceof DOMException && error.name === "AbortError") {
        return;
      }
      setReplayError("The replay fixture could not be started. You can retry explicitly.");
    } finally {
      setReplayLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setHealthError(false);

    void getHealth(controller.signal)
      .then(setHealth)
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") {
          return;
        }
        setHealth(null);
        setHealthError(true);
      });

    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (health === null) {
      return;
    }
    const controller = new AbortController();
    const liveAvailable = health.backend === "openai" && health.liveReady;
    if (!liveAvailable) {
      setHotelSnapshot(null);
      setReplayFallback(LIVE_ADMISSION_MESSAGES.live_unavailable);
      void startReplay(controller.signal);
      return () => controller.abort();
    }

    setReplayFallback(null);
    setReplayError(null);

    void createRecovery("hotel", "openai_live", controller.signal)
      .then(setHotelSnapshot)
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") {
          return;
        }
        setHotelSnapshot(null);
        if (error instanceof LiveAdmissionError) {
          setReplayFallback(error.message);
          void startReplay(controller.signal);
        }
      });

    return () => controller.abort();
  }, [health, startReplay]);

  const activeSnapshot = activeId === "hotel" ? hotelSnapshot : quotaSnapshot;
  const activeScenarioView = useMemo<RecoveryScenario>(
    () => {
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
      if (activeId !== "api-quota") {
        return activeScenario;
      }
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
    },
    [activeId, activeScenario, activeSnapshot, quotaLoading],
  );

  const refreshHotelSnapshot = useCallback(async () => {
    if (hotelSnapshot === null) {
      return;
    }
    setHotelSnapshot(await getRecovery(hotelSnapshot.recoveryId));
  }, [hotelSnapshot]);

  const presentation = useMemo(
    () => (health === null ? null : deriveRuntimePresentation(health, activeScenarioView)),
    [activeScenarioView, health],
  );

  return (
    <div className="app-frame">
      <header className="top-bar">
        <a className="brand" href="#workspace" aria-label="Backchannel console home">
          <span className="brand-mark" aria-hidden="true">
            <span />
            <span />
          </span>
          <span>Backchannel</span>
        </a>
        <div className="top-context">
          <span>Operational recovery console</span>
          <span className="environment-badge">
            {workspaceLabel(activeScenarioView.executionMode)}
          </span>
        </div>
      </header>

      <div className="console-shell" id="workspace">
        <ScenarioRail
          scenarios={recoveryScenarios}
          activeId={activeId}
          onSelect={selectScenario}
        />

        <main className="workspace">
          <ProvenanceStrip presentation={presentation} healthError={healthError} />
          {activeId === "hotel" && replayFallback !== null ? (
            <section className="replay-fallback" aria-live="polite">
              <div>
                <p className="eyebrow">Replay fallback</p>
                <p>{replayFallback}</p>
                {replayError !== null ? <p role="alert">{replayError}</p> : null}
              </div>
              <button
                type="button"
                aria-label="Run replay fixture"
                disabled={replayLoading}
                onClick={() => void startReplay()}
              >
                {replayLoading ? "Loading replay fixture…" : "Run replay fixture"}
              </button>
            </section>
          ) : null}
          {activeId === "api-quota" && quotaError !== null ? (
            <p role="alert">{quotaError}</p>
          ) : null}
          <section className="recovery-heading" aria-labelledby="recovery-title">
            <div>
              <p className="eyebrow">Active recovery</p>
              <h1 id="recovery-title">{activeScenarioView.title}</h1>
            </div>
            <span className="recovery-state">
              {activeSnapshot !== null
                ? serverStatusLabel(
                    activeSnapshot.status,
                    activeSnapshot.pendingApproval !== null,
                  )
                : activeScenarioView.status === "completed"
                  ? "Completed fixture"
                  : "Awaiting boundary"}
            </span>
          </section>
          <Lifecycle scenario={activeScenarioView} />
        </main>

        {activeId === "api-quota" && activeSnapshot === null ? (
          <aside className="evidence-inspector" aria-labelledby="quota-evidence-heading">
            <div className="inspector-heading">
              <p className="eyebrow">Server quota proof</p>
              <h2 id="quota-evidence-heading">
                {quotaLoading ? "Loading deterministic trace" : "Quota proof unavailable"}
              </h2>
              <p>{activeScenarioView.currentStepSummary}</p>
            </div>
            <div className="execution-boundary">
              <strong>No completed quota receipt is being shown.</strong>
              <p>Only server-returned SDK evidence can complete this scenario.</p>
            </div>
          </aside>
        ) : (
          <EvidenceInspector
            scenario={activeScenarioView}
            snapshot={activeSnapshot}
            onServerSuccess={refreshHotelSnapshot}
          />
        )}
      </div>
    </div>
  );
}
