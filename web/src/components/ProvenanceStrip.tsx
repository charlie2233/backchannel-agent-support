import type { RuntimePresentation } from "../domain/runtime";
import type { RuntimeHealthPhase } from "../hooks/useRuntimeHealth";

interface ProvenanceStripProps {
  presentation: RuntimePresentation | null;
  healthPhase: RuntimeHealthPhase;
  onRetryRuntime?: () => void;
  awaitingSnapshot?: boolean;
  awaitingServerEvidence?: boolean;
  noRunStarted?: boolean;
}

export function ProvenanceStrip({
  presentation,
  healthPhase,
  onRetryRuntime,
  awaitingSnapshot = false,
  awaitingServerEvidence = false,
  noRunStarted = false,
}: ProvenanceStripProps) {
  if (presentation === null) {
    const title =
      healthPhase === "unavailable"
        ? "Runtime unavailable"
        : healthPhase === "retrying"
          ? "Retrying runtime check"
          : healthPhase === "checking"
            ? "Checking runtime"
            : awaitingServerEvidence
              ? "Awaiting server evidence"
              : noRunStarted
                ? "No server run started"
                : awaitingSnapshot
                  ? "Awaiting run evidence"
                  : "Checking runtime";
    const explanation =
      healthPhase === "unavailable"
        ? "The health endpoint could not be verified after repeated checks, so no runtime claim is shown."
        : healthPhase === "retrying"
          ? "The health endpoint could not be verified. Retrying before making any runtime claim."
          : healthPhase === "checking"
            ? "Waiting for /health before making a runtime claim."
            : awaitingServerEvidence
              ? "Waiting for an authoritative server snapshot before making an execution-mode claim."
              : noRunStarted
                ? "Start a server recovery explicitly before execution-mode provenance is shown."
                : awaitingSnapshot
                  ? "Waiting for a server recovery snapshot before making an execution-mode claim."
                  : "Waiting for /health before making a runtime claim.";

    return (
      <section
        className="provenance-strip provenance-strip--pending"
        aria-atomic="true"
        aria-busy={healthPhase === "checking" || healthPhase === "retrying"}
        aria-live="polite"
      >
        <span className="provenance-dot" aria-hidden="true" />
        <div className="provenance-copy">
          <strong>{title}</strong>
          <p>{explanation}</p>
        </div>
        {healthPhase === "unavailable" && onRetryRuntime !== undefined ? (
          <button className="provenance-retry" type="button" onClick={onRetryRuntime}>
            Retry runtime check
          </button>
        ) : null}
      </section>
    );
  }

  return (
    <section className="provenance-strip" aria-label="Runtime provenance" aria-live="polite">
      <span className="provenance-dot" aria-hidden="true" />
      <div className="provenance-copy">
        <strong>{presentation.label}</strong>
        <p>{presentation.explanation}</p>
      </div>
      {presentation.showGpt56Agents ? (
        <span className="boundary-label">GPT-5.6 agents</span>
      ) : null}
      <span className="boundary-label">Demo adapter boundary</span>
    </section>
  );
}
