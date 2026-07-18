import type { RuntimePresentation } from "../domain/runtime";

interface ProvenanceStripProps {
  presentation: RuntimePresentation | null;
  healthError: boolean;
}

export function ProvenanceStrip({ presentation, healthError }: ProvenanceStripProps) {
  if (presentation === null) {
    return (
      <section className="provenance-strip provenance-strip--pending" aria-live="polite">
        <span className="provenance-dot" aria-hidden="true" />
        <div>
          <strong>{healthError ? "Runtime unavailable" : "Checking runtime"}</strong>
          <p>
            {healthError
              ? "The health endpoint could not be verified, so no runtime claim is shown."
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
      <span className="boundary-label">Demo adapter boundary</span>
    </section>
  );
}
