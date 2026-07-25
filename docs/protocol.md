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

## Bounded event-stream admission

After the session-access check and cursor validation, the SSE route runs its targeted expiry
sweep and existence check, then atomically attempts a process-local admission. Defaults are 16
active streams in the application process, four for one recovery, and a five-second retry.
Acquisition never waits on a semaphore and rejected requests never begin durable-ledger
polling.

`Last-Event-ID` is optional and may occur at most once. When present, its lexical form is one or
more ASCII digits (`[0-9]+`); leading zeroes are accepted, but signs, whitespace, separators,
booleans, and Unicode digits are not. Its numeric value must be in SQLite's signed integer cursor
range, `0..9223372036854775807`. An authorized invalid or duplicate cursor receives JSON `400`
with the stable detail `Last-Event-ID must contain only ASCII digits and be between 0 and
9223372036854775807`, `Cache-Control: no-store`, and no SSE buffering headers. This validation
occurs before expiry mutation, event-stream admission, and durable event polling.

At capacity the HTTP response remains `200 text/event-stream`, preserves `no-cache` and
`X-Accel-Buffering: no`, emits no event ID, recovery/session value, count, or limit, and closes
after this control frame (the `requestId` is a fresh 32-character lowercase hexadecimal value):

```text
retry: 5000
event: stream.capacity
data: {"code":"event_stream_capacity","message":"Event streaming is temporarily at capacity; retry is automatic.","requestId":"<request-id>"}

```

The named browser listener accepts only those three exact JSON keys and exact public code and
message. It reports the condition without closing `EventSource`, so native retry continues; the
immediate EOF error does not replace that specific status. A later valid recovery event clears
the transient error and restores ordinary disconnect reporting. Admitted connections keep the
existing durable replay, polling, heartbeat, disconnect, and terminal rules. Their idempotent
lease is also released on cancellation, iterator/store/encoding errors, response construction
failure, and ASGI send failure.

This is not a fleet-wide limit. Every worker or container would have independent counters. The
production launcher uses one Uvicorn worker, so the configured defaults cap polling loops only
in that one process; horizontal scaling requires a separate shared-admission design.

The UI stores no cookie, session correlation, serialized run state, approval payload, or
provenance claim in browser storage. It may retain one syntactically validated hotel recovery
UUID in same-tab `sessionStorage` as a non-authoritative reload hint. The server-authorized GET
must return a contract-valid hotel snapshot before that hint can restore the event stream,
consent, or receipt. A generic authorized 404 or a valid correlated snapshot for another scenario
clears the hint; these cover missing, foreign-session, expired-session, and retention-deleted state
without distinguishing them. Network errors, non-404 responses, malformed JSON, and contract-invalid
or miscorrelated success responses retain the UUID but authorize no snapshot or action. The browser
shows fixed redacted copy and only **Retry saved recovery**; repeated activation coalesces to the same
GET. It never surfaces response text or automatically creates a run, starts a replay, or resumes a
decision. A retry 404 clears the hint, while a valid hotel snapshot restores the ordinary lifecycle.
On a keyless runtime after a terminal lookup, the browser offers replay and SDK QA as explicit
actions; it does not reuse copy that claims an automatic fallback is underway.

## Provenance modes

`openai_live` runs two narrow `gpt-5.6-luna` structured-output agents and one
`gpt-5.6-terra` broker inside one explicit Agents SDK root trace. It proves real model calls
only when the receipt records model IDs, `modelCall=true`, a valid `trace_...` root, and the
live execution mode. The provider is still a simulated hotel adapter. The browser starts this
mode only from the explicit **Start live recovery** action; mount and reload never infer consent
to spend model quota.

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

The `commit_remedy` tool requires Agents SDK approval. After the runner returns the validated
pause, the server may persist the opaque SDK state and a separate public consent view. Its
canonical digest binds exactly:

- `recoveryId` and `remedyId`;
- typed remedy terms;
- integer `costDeltaMinor`;
- sorted `changedFields` and `providerCommitments`;
- timezone-aware UTC `expiry`.

The object is compact JSON with sorted keys and then SHA-256 hashed as
`sha256:<64 lowercase hex>`. A decision must echo `clientDecisionId`, `remedyId`, the full
`remedyDigest`, and the exact `toolCallId`. The shortened digest in the UI is display-only; its
copy action returns the full value.

The authoritative typed evidence must independently match the stored remedy identifier, exact
terms, minor-unit cost, sorted changed fields, and sorted provider commitments. The server
recomputes the consent digest over those fields plus the UTC expiry and separately requires the
pending envelope's action digest to match the typed remedy. These checks run before persistence,
for public pending and claimed views, and during decision validation. Consequently a broken
binding exposes neither `pendingApproval` nor `claimedDecision`; there is no public approve,
decline, or resume capability to render.

The structured SDK argument models reject unknown top-level and nested fields. Their JSON Schemas
are definition-digest inputs, so this stricter schema creates a new canonical definition digest
and deliberately makes older serialized pending SDK state resume-incompatible.

The runner must first return exactly one typed `commit_remedy` interruption whose arguments
match the validated consumer/provider proof. The server then recomputes current hotel policy and
requires both the hard-constraint and delegated-authority results to be exactly true before it
creates the recovery, access binding, event, remedy, pending envelope, or public consent. A
policy denial raises only the generic correlated internal-error boundary; it does not create an
alternative, replay, or provider dispatch. A denied mocked-live request releases its exact live
admission but retains the truthfully consumed budget unit. This scripted test is not a real
OpenAI run.

The public pending-consent schema represents both policy results as literal `true`, and the
browser validates them as such before accepting a snapshot or rendering decision controls.
SQLite repeats the complete binding and current-policy checks before beginning a consent
transition. If an older or tampered row contains a false stored result, mismatched consent field,
changed evidence, digest mismatch, or stale action digest, its public snapshot exposes no
actionable pending consent or resumable claim. Decision-time digest, expiry, policy, and
interruption revalidation remains mandatory even after these admission checks.

The SHA-256 values are unkeyed integrity bindings, not database authentication. A malicious
writer with authority to replace all bound rows and recompute both digests remains outside this
application-level threat model; SQLite storage is an explicit trust boundary.

Approval is claimed in a SQLite `BEGIN IMMEDIATE` transaction. Immediately before creating an
execution, the server recomputes the consent digest and rechecks expiry, hard constraints,
delegated authority, and the stored SDK interruption. The provider execution key is bound to
the recovery, interruption, and action digest. Retrying the same decision ID returns its stored
result; a different loser receives `409 already_decided`. These controls and tests establish an
at-most-one dispatch property for this demo adapter, not a universal exactly-once guarantee.
An unrelated session cannot participate in that race: it receives 404 before a decision claim
and cannot win, renew a live lease, or trigger provider dispatch.

### Explicit durable-claim resume

If a process or response is interrupted after the decision claim commits, an authorized
snapshot keeps `pendingApproval=null` and may expose only `claimedDecision.action`,
`claimedDecision.remedyDigest`, and `claimedDecision.expiry`. It never exposes the durable
`clientDecisionId`, remedy ID, tool-call ID, request fingerprint, or serialized SDK state. The
browser still stores only the recovery UUID and never submits a decision on mount or reload.

The user-controlled **Resume exact approval** or **Resume exact decline** action calls
`POST /api/recoveries/{recoveryId}/decisions/resume` with exact `{}` JSON. Session access is
checked before content type, body, or claim inspection. A foreign, missing, expired, or tampered
session therefore keeps the generic 404 boundary; an authorized recovery without a claim gets
`409 decision_resume_unavailable`, and no decision is created. The server recomputes the stored
full-request fingerprint, performs consent/expiry/version preflight before live capacity, and
repeats those checks immediately inside the shared continuation pipeline. A completed claim
returns a minimal stored result without acquiring live capacity or rerunning model/provider work.

This is a signed-session capability, not proof of the original tab: another tab with the same
HttpOnly session and recovery UUID may explicitly continue the already-fixed claim. Concurrent
attempts remain bounded by the durable execution/idempotency ledger to at most one demo-adapter
dispatch, but may duplicate live model work across workers. Capacity returns the existing
`decision_capacity` response and leaves the explicit action available; it never starts replay or
retries automatically. At or after the original consent expiry, resume returns
`422 remedy_expired` before capacity or dispatch and does not invent a cancellation or receipt.

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
