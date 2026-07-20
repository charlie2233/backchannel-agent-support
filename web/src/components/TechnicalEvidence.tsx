import { type ReactNode, useState } from "react";

export type TechnicalEvidencePreferenceScope = "desktop" | "mobile";
export type TechnicalEvidencePreferences = Partial<
  Record<TechnicalEvidencePreferenceScope, boolean>
>;

interface TechnicalEvidenceProps {
  children: ReactNode;
  expanded: boolean;
  preferenceScope: TechnicalEvidencePreferenceScope;
  preferences?: TechnicalEvidencePreferences;
  onPreferenceChange?: (
    scope: TechnicalEvidencePreferenceScope,
    expanded: boolean,
  ) => void;
}

export function TechnicalEvidence({
  children,
  expanded,
  preferenceScope,
  preferences,
  onPreferenceChange,
}: TechnicalEvidenceProps) {
  const [localPreferences, setLocalPreferences] =
    useState<TechnicalEvidencePreferences>({});
  const effectivePreferences = preferences ?? localPreferences;
  const isOpen = effectivePreferences[preferenceScope] ?? expanded;
  const toggleDisclosure = () => {
    const nextOpen = !isOpen;
    if (preferences === undefined) {
      setLocalPreferences((current) => ({
        ...current,
        [preferenceScope]: nextOpen,
      }));
    }
    onPreferenceChange?.(preferenceScope, nextOpen);
  };
  return (
    <details className="technical-evidence" open={isOpen}>
      <summary
        onClick={(event) => {
          event.preventDefault();
          toggleDisclosure();
        }}
        onKeyDown={(event) => {
          if ((event.key === "Enter" || event.key === " ") && !event.repeat) {
            event.preventDefault();
            toggleDisclosure();
          }
        }}
      >
        <span className="technical-evidence__summary-title">
          Technical evidence
        </span>
        <span className="technical-evidence__summary-description">
          IDs, runtime, and trace provenance.
        </span>
      </summary>
      <div className="technical-evidence__content">{children}</div>
    </details>
  );
}
