import type { RuntimePresentation } from "../domain/runtime";

interface ProvenanceStripProps {
  presentation: RuntimePresentation | null;
  healthError: boolean;
  awaitingSnapshot?: boolean;
}

export function ProvenanceStrip({
  presentation,
  healthError,
  awaitingSnapshot = false,
}: ProvenanceStripProps) {
  if (presentation === null) {
    return (
      <section className="provenance-strip provenance-strip--pending" aria-live="polite">
        <span className="provenance-dot" aria-hidden="true" />
        <div>
          <strong>
            {healthError
              ? "Runtime unavailable"
              : awaitingSnapshot
                ? "Awaiting run evidence"
                : "Checking runtime"}
          </strong>
          <p>
            {healthError
              ? "The health endpoint could not be verified, so no runtime claim is shown."
              : awaitingSnapshot
                ? "Waiting for a server recovery snapshot before making an execution-mode claim."
              : "Waiting for /health before making a runtime claim."}
          </p>
        </div>
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
