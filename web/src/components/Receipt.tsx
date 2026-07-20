import type { RecoveryReceipt } from "../domain/recovery";

interface ReceiptProps {
  receipt: RecoveryReceipt;
}

function available(value: string | null | undefined): string {
  return value === null || value === undefined || value.length === 0 ? "None" : value;
}

function modelEvidence(receipt: RecoveryReceipt): string {
  return receipt.modelIds.length === 0
    ? "None — no model call"
    : receipt.modelIds.join(", ");
}

function digestMatch(receipt: RecoveryReceipt): string {
  return receipt.decision === "approved" &&
    receipt.approvedRemedyDigest !== null &&
    receipt.decisionRemedyDigest === receipt.approvedRemedyDigest
    ? "Matched approved digest"
    : "Not applicable";
}

function ReceiptEvidence({
  receipt,
  providerResultInVerdict = false,
}: ReceiptProps & { providerResultInVerdict?: boolean }) {
  return (
    <dl className="evidence-list receipt-evidence">
      <div>
        <dt>Status</dt>
        <dd>{receipt.status}</dd>
      </div>
      <div>
        <dt>Recovery ID</dt>
        <dd className="mono">{receipt.recoveryId}</dd>
      </div>
      <div>
        <dt>Execution mode</dt>
        <dd className="mono">{receipt.executionMode}</dd>
      </div>
      <div>
        <dt>Simulated</dt>
        <dd>{receipt.simulated ? "Yes" : "No"}</dd>
      </div>
      <div>
        <dt>Provider execution</dt>
        <dd>{receipt.providerExecution ? "Yes" : "No"}</dd>
      </div>
      <div>
        <dt>Model IDs</dt>
        <dd className="mono">{modelEvidence(receipt)}</dd>
      </div>
      <div>
        <dt>Root trace ID</dt>
        <dd className="mono">{available(receipt.rootTraceId)}</dd>
      </div>
      <div>
        <dt>Authorization source</dt>
        <dd>{receipt.authorizationSource}</dd>
      </div>
      <div>
        <dt>Decision remedy digest</dt>
        <dd className="mono">{available(receipt.decisionRemedyDigest)}</dd>
      </div>
      <div>
        <dt>Approved remedy digest</dt>
        <dd className="mono">{available(receipt.approvedRemedyDigest)}</dd>
      </div>
      <div>
        <dt>Immediate pre-execution digest match</dt>
        <dd>{digestMatch(receipt)}</dd>
      </div>
      <div>
        <dt>Provider dispatch started</dt>
        <dd>{receipt.providerDispatchStarted ? "Yes" : "No"}</dd>
      </div>
      {!providerResultInVerdict ? (
        <div>
          <dt>Provider result</dt>
          <dd>{receipt.providerResult}</dd>
        </div>
      ) : null}
      <div>
        <dt>Execution count</dt>
        <dd>{receipt.executionCount}</dd>
      </div>
      <div>
        <dt>Permission revoked</dt>
        <dd>{receipt.permissionRevoked ? "Yes" : "No"}</dd>
      </div>
      <div>
        <dt>Exact interruption rejected</dt>
        <dd>{receipt.exactInterruptionRejected ? "Yes" : "No"}</dd>
      </div>
      <div>
        <dt>Scope closed</dt>
        <dd>{receipt.scopeClosed ? "Yes" : "No"}</dd>
      </div>
      <div>
        <dt>Protocol version</dt>
        <dd className="mono">{available(receipt.protocolVersion)}</dd>
      </div>
      <div>
        <dt>Agent graph version</dt>
        <dd className="mono">{available(receipt.agentGraphVersion)}</dd>
      </div>
      <div>
        <dt>SDK version</dt>
        <dd className="mono">{available(receipt.sdkVersion)}</dd>
      </div>
      <div>
        <dt>Prompt-tool schema hash</dt>
        <dd className="mono">{available(receipt.promptToolSchemaHash)}</dd>
      </div>
      <div>
        <dt>Boundary</dt>
        <dd>{receipt.boundary}</dd>
      </div>
    </dl>
  );
}

function VerificationResults({
  results,
  label,
}: {
  results: ReadonlyArray<string>;
  label: string;
}) {
  return (
    <ul className="receipt-checks" aria-label={label}>
      {results.map((result) => (
        <li key={result}>{result}</li>
      ))}
    </ul>
  );
}

function QuotaReceipt({ receipt }: ReceiptProps) {
  const evidence = receipt.quotaEvidence;
  if (evidence === null) {
    return null;
  }
  const runtime = evidence.source === "sdk_simulator";
  const heading = runtime ? "Verified quota recovery" : "Recorded quota recovery";
  return (
    <aside
      className="evidence-inspector receipt-inspector"
      aria-labelledby="quota-receipt-heading"
    >
      <div className="inspector-heading">
        <p className="eyebrow">
          {runtime ? "Authoritative simulator receipt" : "Recorded fixture receipt"}
        </p>
        <h2 id="quota-receipt-heading">{heading}</h2>
      </div>
      <div className="receipt-verdict">
        <strong>
          {runtime ? "Runtime permission revoked" : "Recorded revocation evidence only"}
        </strong>
        <p>
          {runtime
            ? "One deterministic demo-provider dispatch completed with no human approval."
            : "No runtime model call, provider dispatch, or permission revocation occurred."}
        </p>
      </div>
      <dl className="evidence-list quota-evidence">
        <div>
          <dt>Provider ceiling</dt>
          <dd>{evidence.providerCeilingRpm} rpm</dd>
        </div>
        <div>
          <dt>Recorded demand</dt>
          <dd>{evidence.recordedDemandRpm} rpm</dd>
        </div>
        <div>
          <dt>Temporary burst</dt>
          <dd>{evidence.temporaryBurstRpm} rpm</dd>
        </div>
        <div>
          <dt>Region</dt>
          <dd>{evidence.region}</dd>
        </div>
        <div>
          <dt>Duration</dt>
          <dd>{evidence.durationSeconds} seconds</dd>
        </div>
        <div>
          <dt>Extra cost</dt>
          <dd>{evidence.extraCostMinor} {evidence.currency} minor units</dd>
        </div>
        <div>
          <dt>Delegated maximum</dt>
          <dd>{evidence.delegatedAuthorityMaxMinor} {evidence.currency} minor units</dd>
        </div>
        <div>
          <dt>Currency</dt>
          <dd>{evidence.currency}</dd>
        </div>
        <div>
          <dt>Human interruptions</dt>
          <dd>{evidence.humanInterruptions} human interruptions</dd>
        </div>
        <div>
          <dt>Approvals</dt>
          <dd>{evidence.approvals} approvals</dd>
        </div>
        <div>
          <dt>Protocol</dt>
          <dd>{evidence.protocolSteps.join(" → ")}</dd>
        </div>
        <div>
          <dt>Evidence source</dt>
          <dd className="mono">{evidence.source}</dd>
        </div>
        <div>
          <dt>Revocation evidence kind</dt>
          <dd className="mono">{evidence.revocationEvidenceKind}</dd>
        </div>
        <div>
          <dt>Provider proof</dt>
          <dd>{evidence.providerProofVerified ? "Verified" : "Not verified"}</dd>
        </div>
        <div>
          <dt>Grant verification</dt>
          <dd>{evidence.grantVerified ? "Verified" : "Not verified"}</dd>
        </div>
        <div>
          <dt>Provider dispatch</dt>
          <dd>{runtime ? "Yes — one local demo dispatch" : "No — recorded outcome only"}</dd>
        </div>
        <div>
          <dt>Hard constraints</dt>
          <dd>
            Region preserved; burst covers demand; duration within limit; base quota unchanged
          </dd>
        </div>
        <div>
          <dt>Region preserved</dt>
          <dd>{evidence.hardConstraints.regionPreserved ? "Yes" : "No"}</dd>
        </div>
        <div>
          <dt>Burst covers demand</dt>
          <dd>{evidence.hardConstraints.burstCoversDemand ? "Yes" : "No"}</dd>
        </div>
        <div>
          <dt>Duration within limit</dt>
          <dd>{evidence.hardConstraints.durationWithinLimit ? "Yes" : "No"}</dd>
        </div>
        <div>
          <dt>Base quota unchanged</dt>
          <dd>{evidence.hardConstraints.baseQuotaUnchanged ? "Yes" : "No"}</dd>
        </div>
      </dl>
      <VerificationResults
        results={receipt.verificationResults}
        label="Quota verification evidence"
      />
      <ReceiptEvidence receipt={receipt} />
    </aside>
  );
}

function ReplayReceipt({ receipt }: ReceiptProps) {
  return (
    <aside className="evidence-inspector receipt-inspector" aria-labelledby="replay-heading">
      <div className="inspector-heading">
        <p className="eyebrow">Recorded simulated fixture evidence</p>
        <h2 id="replay-heading">Completed replay receipt</h2>
        <p>No model call or provider dispatch occurred.</p>
      </div>
      <div className="receipt-verdict">
        <strong>Simulated replay only.</strong>
        <p>{receipt.providerResult}</p>
      </div>
      <VerificationResults
        results={receipt.verificationResults}
        label="Recorded replay verification"
      />
      <ReceiptEvidence receipt={receipt} providerResultInVerdict />
    </aside>
  );
}

function ClosedReceipt({ receipt }: ReceiptProps) {
  const closedBeforeDispatch =
    !receipt.providerExecution &&
    !receipt.providerDispatchStarted &&
    receipt.executionCount === 0;
  const dispatchProof = closedBeforeDispatch
    ? "Provider dispatch did not begin."
    : receipt.providerResult;
  return (
    <aside className="evidence-inspector receipt-inspector" aria-labelledby="closed-heading">
      <div className="inspector-heading">
        <p className="eyebrow">Authoritative server receipt</p>
        <h2 id="closed-heading">Closed without action</h2>
        <p>The exact remedy was declined and its permission scope is closed.</p>
      </div>
      <div className="receipt-verdict receipt-verdict--closed">
        <strong>{dispatchProof}</strong>
        <p>This is cancellation evidence for the exact rejected interruption.</p>
      </div>
      <ul className="receipt-checks" aria-label="Closure verification">
        {receipt.verificationResults.map((result) => (
          <li key={result}>{result}</li>
        ))}
        <li>executionCount = {receipt.executionCount}</li>
      </ul>
      <ReceiptEvidence
        receipt={receipt}
        providerResultInVerdict={dispatchProof === receipt.providerResult}
      />
    </aside>
  );
}

function UnknownReceipt({ receipt }: ReceiptProps) {
  const reconciliationResults = receipt.verificationResults.filter(
    (result) => result !== "Manual reconciliation required.",
  );
  return (
    <aside className="evidence-inspector receipt-inspector" aria-labelledby="unknown-heading">
      <div className="inspector-heading">
        <p className="eyebrow">Authoritative server receipt</p>
        <h2 id="unknown-heading">Outcome unknown</h2>
        <p>Execution evidence exists, so cancellation cannot be claimed.</p>
      </div>
      <div className="receipt-verdict receipt-verdict--unknown">
        <strong>Manual reconciliation required.</strong>
        <p>{receipt.providerResult}</p>
      </div>
      <VerificationResults
        results={reconciliationResults}
        label="Reconciliation evidence"
      />
      <ReceiptEvidence receipt={receipt} providerResultInVerdict />
    </aside>
  );
}

function CompletedReceipt({ receipt }: ReceiptProps) {
  return (
    <aside className="evidence-inspector receipt-inspector" aria-labelledby="completed-heading">
      <div className="inspector-heading">
        <p className="eyebrow">Authoritative server receipt</p>
        <h2 id="completed-heading">Completed receipt</h2>
        <p>{receipt.providerResult}</p>
      </div>
      <VerificationResults
        results={receipt.verificationResults}
        label="Completion verification evidence"
      />
      <ReceiptEvidence receipt={receipt} providerResultInVerdict />
    </aside>
  );
}

export function Receipt({ receipt }: ReceiptProps) {
  if (receipt.quotaEvidence !== null) {
    return <QuotaReceipt receipt={receipt} />;
  }
  if (receipt.executionMode === "replay_fixture") {
    return <ReplayReceipt receipt={receipt} />;
  }
  if (receipt.status === "closed_without_action") {
    return <ClosedReceipt receipt={receipt} />;
  }
  if (receipt.status === "outcome_unknown") {
    return <UnknownReceipt receipt={receipt} />;
  }
  return <CompletedReceipt receipt={receipt} />;
}
