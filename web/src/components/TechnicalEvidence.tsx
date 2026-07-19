import type { EvidenceEntry } from "../domain/recovery";

interface TechnicalEvidenceProps {
  defaultOpen?: boolean;
  entries: ReadonlyArray<EvidenceEntry>;
}

export function TechnicalEvidence({ defaultOpen = false, entries }: TechnicalEvidenceProps) {
  return (
    <details className="technical-evidence" open={defaultOpen}>
      <summary>Show technical evidence</summary>
      <dl className="evidence-list">
        {entries.map((entry) => (
          <div key={entry.label}>
            <dt>{entry.label}</dt>
            <dd className={entry.monospace ? "mono" : undefined}>{entry.value}</dd>
          </div>
        ))}
      </dl>
    </details>
  );
}
