import type { RecoveryEvent } from "../api/events";
import type { EvidenceEntry, RecoveryReceipt } from "../domain/recovery";
import { TechnicalEvidence } from "./TechnicalEvidence";

interface ReceiptProps {
  events: ReadonlyArray<RecoveryEvent>;
  receipt: RecoveryReceipt;
  technicalOpen?: boolean;
}

const DECLINED_PROOFS = [
  "Human consent requested.",
  "Remedy declined by operator.",
  "Exact interruption rejected.",
  "No replacement action selected.",
  "Temporary permission revoked.",
  "Cancellation receipt sealed.",
] as const;

function modeLabel(mode: RecoveryReceipt["executionMode"]): string {
  if (mode === "openai_live") return "OpenAI live";
  if (mode === "sdk_stub") return "SDK stub";
  return "Replay fixture";
}

function receiptTitle(status: RecoveryReceipt["status"]): string {
  if (status === "closed_without_action") return "Closed without action";
  if (status === "outcome_unknown") return "Outcome unknown";
  if (status === "simulated_completed") return "Simulated replay receipt";
  return "Sealed receipt";
}

function statusLabel(status: RecoveryReceipt["status"]): string {
  if (status === "closed_without_action") return "Closed without action";
  if (status === "outcome_unknown") return "Outcome unknown";
  if (status === "simulated_completed") return "Simulated completed";
  return "Completed";
}

function terminalExecutionCount(
  events: ReadonlyArray<RecoveryEvent>,
  recoveryId: string,
): number | null {
  for (const event of events) {
    if (event.recoveryId !== recoveryId || !event.terminal) continue;
    const count = event.data.executionCount;
    if (typeof count === "number" && Number.isInteger(count) && count >= 0) return count;
  }
  return null;
}

export function Receipt({ events, receipt, technicalOpen = true }: ReceiptProps) {
  const title = receiptTitle(receipt.status);
  const digestMatched = receipt.verificationResults.includes(
    "Immediate pre-execution remedy digest matched the approved digest.",
  );
  const permissionRevoked = receipt.verificationResults.includes(
    "Temporary provider-dispatch permission revoked after the approved execution.",
  ) || receipt.verificationResults.includes(
    "Temporary quota permission revoked; baseline ceiling restored to 1000 units.",
  ) || receipt.verificationResults.includes(
    "Temporary permission revoked.",
  );
  const executionCount = terminalExecutionCount(events, receipt.recoveryId);
  const isDeclined = receipt.status === "closed_without_action";
  const isExecutionFreeReplay =
    receipt.executionMode === "replay_fixture" && receipt.providerExecution === false;
  const isZeroApprovalDelegated =
    receipt.status === "completed" &&
    receipt.approvalCount === 0 &&
    receipt.approvedRemedyDigest === null;
  const approvedDigest = isExecutionFreeReplay
    ? "Not applicable — replay fixture/no provider execution"
    : isZeroApprovalDelegated
      ? "Not applicable — delegated authority required no human approval."
      : receipt.approvedRemedyDigest ?? "None";
  const digestCheck = isZeroApprovalDelegated
    ? "Not applicable — no approved remedy digest was required."
    : digestMatched
      ? "Matched immediately before execution"
      : "Not reported";
  const permissionState = isExecutionFreeReplay
    ? "Not applicable — replay fixture/no provider execution"
    : permissionRevoked
      ? "Revoked"
      : "Not reported";
  const technicalEntries: EvidenceEntry[] = [
    { label: "Root trace ID", value: receipt.rootTraceId ?? "None — replay fixture", monospace: true },
    { label: "Agents SDK version", value: receipt.sdkVersion ?? "None — fixture", monospace: true },
    { label: "Protocol version", value: receipt.protocolVersion ?? "None — fixture", monospace: true },
    { label: "Agent graph version", value: receipt.agentGraphVersion ?? "None — fixture", monospace: true },
    {
      label: "Prompt/tool schema hash",
      value: receipt.definitionDigest === null ? "None — fixture" : `sha256:${receipt.definitionDigest}`,
      monospace: true,
    },
  ];

  return (
    <section className={`receipt receipt--${receipt.status}`} aria-labelledby="receipt-heading">
      <div className="receipt-heading">
        <p className="eyebrow">
          {receipt.status === "simulated_completed" ? "Simulated evidence" : "Terminal evidence"}
        </p>
        <h3 id="receipt-heading">{title}</h3>
        <p>{receipt.boundary}</p>
      </div>

      {isDeclined ? (
        <div className="declined-proof">
          <ol aria-label="Closed-without-action proof">
            {DECLINED_PROOFS.filter((proof) => receipt.verificationResults.includes(proof)).map(
              (proof) => <li key={proof}>{proof}</li>,
            )}
          </ol>
          {executionCount === null ? null : <p className="mono">executionCount = {executionCount}</p>}
          <p>{receipt.providerResult}</p>
        </div>
      ) : null}

      <dl className="evidence-list receipt-evidence">
        <div><dt>Status</dt><dd>{statusLabel(receipt.status)}</dd></div>
        <div><dt>Recovery ID</dt><dd className="mono">{receipt.recoveryId}</dd></div>
        <div><dt>Execution mode</dt><dd>{modeLabel(receipt.executionMode)}</dd></div>
        <div><dt>Models</dt><dd>{receipt.modelIds.length === 0 ? "None — no model call" : receipt.modelIds.join(", ")}</dd></div>
        <div><dt>Authorization source</dt><dd>{receipt.authorizationSource}</dd></div>
        <div><dt>Approval count</dt><dd>{receipt.approvalCount}</dd></div>
        <div><dt>Approved remedy digest</dt><dd className="mono">{approvedDigest}</dd></div>
        {receipt.status === "completed" ? (
          <div><dt>Digest check</dt><dd>{digestCheck}</dd></div>
        ) : null}
        <div><dt>Provider result</dt><dd>{receipt.providerResult}</dd></div>
        <div><dt>Temporary permission</dt><dd>{permissionState}</dd></div>
      </dl>

      <section className="verification-results" aria-labelledby="verification-heading">
        <h4 id="verification-heading">Verification results</h4>
        <ul>
          {receipt.verificationResults.map((result) => <li key={result}>{result}</li>)}
        </ul>
      </section>

      <TechnicalEvidence defaultOpen={technicalOpen} entries={technicalEntries} />
    </section>
  );
}
