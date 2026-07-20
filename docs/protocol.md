# Recovery protocol

Backchannel uses the same visible lifecycle for every scenario:

1. **Detect** — record the failed outcome and affected intent.
2. **Prove** — obtain typed consumer and provider evidence.
3. **Negotiate** — select exact remedy terms that satisfy the evidence.
4. **Authorize** — evaluate hard constraints and delegated authority; interrupt for a human
   only when the proposed action is outside predelegated authority.
5. **Execute** — dispatch only the authorized, digest-matched action to a demo adapter.
6. **Verify & seal** — verify the result, revoke temporary permission, and persist a receipt.

The integer `currentStep` is zero-based in the API and maps to this fixed order.

## Signed session access

Creating a recovery also binds it to the signed, HttpOnly opaque demo session in the request.
That access association is committed in the same SQLite transaction as a new SDK/live recovery
or, for replay, only after the canonical snapshot, events, and receipt have passed integrity
validation. The durable association contains only `recoveryId` and a keyed session correlation
value; the raw cookie, nonce, client address, API key, and authorization headers are not stored.

The recovery snapshot, SSE stream, receipt, and decision endpoint require the same valid
session. Authorization runs before SSE cursor parsing, live-lease renewal, consent validation,
decision claiming, or provider dispatch. An unknown UUID and a UUID owned by another, expired,
missing, or tampered session produce the same generic 404, without disclosing whether recovery
state exists. A valid cookie and unchanged identity secret preserve access across process
restart; rotating the secret intentionally fails closed.

## Provenance modes

`openai_live` runs two narrow `gpt-5.6-luna` structured-output agents and one
`gpt-5.6-terra` broker inside one explicit Agents SDK root trace. It proves real model calls
only when the receipt records model IDs, `modelCall=true`, a valid `trace_...` root, and the
live execution mode. The provider is still a simulated hotel adapter.

`sdk_stub` uses the actual Agents SDK runner, tool interruption, serialized state, and resume
path with deterministic local model responses. Its `qa_trace_...` value is a local correlation
ID, not an OpenAI trace, and its model ID list is empty.

`replay_fixture` persists and streams bundled events. Its receipt is
`simulated_completed`, records `providerExecution=false`, and has no root trace, model IDs,
SDK version, protocol version, graph version, definition digest, or approved remedy digest.

The live consumer, provider, and broker calls share one explicit root trace in a running
process. A restarted approval supplies the durable root ID again for correlation, but the
current Agents SDK does not publicly guarantee that a new operating-system process appends to
the same already-exported backend trace object. Restart tests therefore prove durable
correlation and safe resume, not externally verified cross-process trace continuity.

## Exact hotel consent

The `commit_remedy` tool requires Agents SDK approval. Before pausing, the server persists the
opaque SDK state and a separate public consent view. Its canonical digest binds exactly:

- `recoveryId` and `remedyId`;
- typed remedy terms;
- integer `costDeltaMinor`;
- sorted `changedFields` and `providerCommitments`;
- timezone-aware UTC `expiry`.

The object is compact JSON with sorted keys and then SHA-256 hashed as
`sha256:<64 lowercase hex>`. A decision must echo `clientDecisionId`, `remedyId`, the full
`remedyDigest`, and the exact `toolCallId`. The shortened digest in the UI is display-only; its
copy action returns the full value.

Approval is claimed in a SQLite `BEGIN IMMEDIATE` transaction. Immediately before creating an
execution, the server recomputes the consent digest and rechecks expiry, hard constraints,
delegated authority, and the stored SDK interruption. The provider execution key is bound to
the recovery, interruption, and action digest. Retrying the same decision ID returns its stored
result; a different loser receives `409 already_decided`. These controls and tests establish an
at-most-one dispatch property for this demo adapter, not a universal exactly-once guarantee.
An unrelated session cannot participate in that race: it receives 404 before a decision claim
and cannot win, renew a live lease, or trigger provider dispatch.

If the authoritative UTC expiry is reached with the recovery and envelope still pending and no
decision, execution, receipt, or terminal event exists, a bounded `BEGIN IMMEDIATE` lifecycle
sweep closes the recovery instead. A decision claim that commits first wins and is left for the
normal resume path; an expiration that commits first makes later claims unavailable. The
browser's local timer only disables both controls and asks for fresh server evidence. It does
not infer whether another tab already committed a claim or synthesize a terminal outcome.
Once exact durable expiration evidence exists, every later authorized decision attempt returns
the stable `422 remedy_expired` code without changing the receipt or event ledger; unrelated
sessions still receive the generic 404 before that evidence is inspected.

Pending state also records `sdkVersion`, `protocolVersion`, `agentGraphVersion`, and a
`definitionDigest` over prompts, model selections, structured-output schemas, and tool schema.
Any mismatch is rejected as `409 resume_incompatible` before dispatch.

## Terminal semantics

An approved hotel decision is `completed` only after one demo-adapter dispatch is durably
recorded. Its receipt includes the approved remedy digest, immediate pre-execution digest match,
provider result, verification results, revoked temporary permission, execution mode, model IDs,
root trace ID, and version/digest markers.

A declined interruption is `closed_without_action` only when the server can prove
`executionStarted=false`, `providerExecution=false`, and an event-ledger
`executionCount = 0`. The receipt proves human consent was requested, the remedy and exact
interruption were rejected, no replacement was selected, provider dispatch did not begin,
temporary permission was revoked, and a cancellation receipt was sealed.

An untouched expired interruption is also `closed_without_action`, but uses the distinct
terminal event `recovery.expired`. Its receipt preserves SDK/live provenance and proves consent
was requested, the window expired without a decision claim, decision and execution counts are
zero, provider dispatch did not begin, temporary permission was revoked, and expiration
evidence was sealed. A repeated sweep or process restart does not rewrite that evidence. Any
claimed-but-incomplete decision is deliberately excluded because provider dispatch may be in
flight.

If dispatch might have begun, the terminal state is `outcome_unknown`. It deliberately carries
`providerExecution=null`, no approved digest, and no zero-execution or cancellation claim.

## API quota complement

API quota recovery is deterministic stub/replay only; it is not a second live domain. The SDK
stub proves a 1000-unit baseline, a 1200-unit requirement, and a 200-unit shortfall. A temporary
250-unit `us-east-1` burst for 900 seconds and 300 USD minor units is within predelegated limits
of 900 seconds and 500 minor units. It therefore records `approvalCount=0`, verifies the
effective 1250-unit ceiling, revokes the temporary permission, restores the baseline, and seals
a simulated receipt. The replay fixture presents recorded evidence without executing the quota
demo adapter.
