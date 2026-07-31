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
value; the raw cookie, nonce, client address, API key, and authorization headers are not stored
in the application SQLite tables. HTTP server and access logs are outside this persistence
boundary and may contain client addresses according to the Uvicorn/operator logging setup.

The recovery snapshot, SSE stream, receipt, and decision endpoint require the same valid
session. Authorization runs before SSE cursor parsing, live-lease renewal, consent validation,
decision claiming, or provider dispatch. An unknown UUID and a UUID owned by another, expired,
missing, or tampered session produce the same generic 404, without disclosing whether recovery
state exists. A valid cookie and unchanged identity secret preserve access across process
restart; rotating the secret intentionally fails closed.

## Durable recovery creation

`POST /api/recoveries` requires `clientRequestId`, an ASCII token matching
`[A-Za-z0-9][A-Za-z0-9._~-]{0,127}`. A client creates a fresh token for each new start
intent and reuses it only to retry the exact same scenario and execution mode. The same-tab UI
keeps the pending raw UUID token with that scenario/mode tuple in `sessionStorage` until a
matching snapshot is accepted or the user explicitly abandons a conflicting start. This is a
client retry hint, not a durable or public server identifier: the server never persists, logs,
or responds with the raw token and stores only a session-scoped HMAC plus a SHA-256 fingerprint
of canonical scenario/mode JSON. The same surviving signed cookie and unchanged identity
secret are part of that scope; a lost or expired cookie, or a rotated secret, produces a new
scope that cannot recover the earlier binding.

The additive `recovery_creations` ledger reserves the eventual recovery UUID before work
starts. A short SQLite `BEGIN IMMEDIATE` transaction returns exactly one of owner, ready,
pending, conflict, unknown, or capacity; no transaction remains open across replay, Agents SDK, live
model, or provider work. SDK and live owners pass the reserved UUID into orchestration.
Replay owners reserve the canonical fixture UUID, preserving one shared fixture while access
remains separately bound to each signed session. Each claim also stores the opaque session
correlation, opaque HMAC IP correlation observed when the key is first reserved, and exact
signed-cookie expiry. The creation ledger stores no raw address; transport and access logs
remain outside that SQLite claim.

An exact ready retry returns `201` with the current authoritative snapshot and consumes no
additional orchestration, live admission, or budget unit. A changed scenario or mode returns
`409 idempotency_conflict` before accounting. A concurrent active owner returns
`409 creation_pending` with `Retry-After: 2`. A handled error after work starts marks the claim
`creation_outcome_unknown`. A hard-loss start remains `creation_pending` until the five-minute
stale bound, then becomes `creation_outcome_unknown`; unknown claims are never rerun. A complete
matching recovery already persisted for the same session is reconciled first. A known live
admission denial deletes only its untouched
reservation, so an explicit later retry can safely attempt admission again. Demo reset returns
`409 reset_creation_pending` without mutation while the caller session has a reserved, started,
or unknown claim; reset can be retried after the owner resolves it or the signed session
expires. Ready claims remain resettable.

The exact request-key lookup and all of those retry semantics run before capacity accounting.
It does not compare or rewrite the stored IP correlation, so the same signed session and key
remain retryable after IP mobility even when the new IP is saturated. Only a row-absent key
counts rows whose `expires_at` is later than the transaction time, then atomically inserts or
refuses. The defaults are 32 unexpired claims per signed session, 128 per opaque IP
correlation, and 2,048 globally; the bounded environment settings are
`BACKCHANNEL_MAX_RECOVERY_CREATIONS_PER_SESSION`,
`BACKCHANNEL_MAX_RECOVERY_CREATIONS_PER_IP`, and
`BACKCHANNEL_MAX_RECOVERY_CREATIONS_GLOBAL`. The IP gate is independent; only the session
limit cannot exceed the global limit, and whichever independent gate is lower wins. Every
unexpired reserved, started, ready, and unknown row counts. Expired rows do not count even if
bounded cleanup has not deleted them yet.

A new key that would exceed any bound returns `429` with only this fixed envelope and
per-request correlation; it has no `Retry-After` header:

```json
{"code":"creation_capacity","message":"Recovery creation is temporarily at capacity. Existing starts can still be retried; try a new start later.","requestId":"<32-lowercase-hex>"}
```

The denied start itself stores no request digest or reserved UUID and performs no
orchestration, live admission, budget accounting, or recovery mutation. Ordinary bounded
pre-claim maintenance still runs before capacity is evaluated and may delete unrelated
expired creation rows, expire unrelated pending approvals, or clean unrelated terminal
recoveries. The capacity envelope discloses no raw client token, keyed digest, reserved UUID,
fallback, count, or limit. The endpoint's exact 429 schema is a `oneOf` containing this
no-fallback envelope plus the reachable `live_capacity`, `cooldown`, and `daily_budget`
live-admission envelopes, each with `fallbackExecutionMode: "replay_fixture"`.
`live_unavailable` remains a 422 outcome and is not a 429 alternative.

The per-IP gate is defense-in-depth abuse resistance, not authentication, a unique-user
limit, or a fairness guarantee. Shared NATs can group unrelated users. The direct network peer
is canonical unless trusted-proxy mode and its explicit CIDR allowlist are enabled; only then
can a validated forwarded chain select the correlated address. Every `X-Forwarded-For` field
is combined in wire order before comma-separated hops are normalized; the right-to-left walk
stops at the nearest untrusted hop, and any malformed hop falls back to the direct peer. This
prevents an attacker-controlled first duplicate field from replacing a later proxy-appended
client address. The identity secret HMACs that value before creation-ledger persistence. Legacy
creation rows are backfilled with their
already-opaque session correlation as `ip_key`, preserving exact-key claim semantics without
introducing a raw address into SQLite or invalidating a claim. This privacy boundary does not
claim that Uvicorn or infrastructure access logs omit client addresses.

The browser retains the same-tab tuple and offers an explicit same-key retry for capacity,
pending, and unknown outcomes; the deterministic quota scenario uses the fixed safe message
instead of server-authored text. An idempotency conflict is permanent for that tuple, so quota
offers **Start a new quota recovery** instead. Nothing is cleared or posted automatically;
that explicit action clears only the matching conflicted tuple and creates a fresh UUID intent.

Evidence, not status alone, controls ready and reconciliation responses. Exact replay retries
call `ReplayEngine.start` and its canonical snapshot/event/receipt validator; partial or
tampered fixtures become `creation_outcome_unknown`. SDK/live hotel creation requires either
one valid pending/claimed consent binding or a terminal receipt whose mode, status, model/trace,
and version provenance match the recovery. SDK quota requires a complete canonical terminal
bundle and QA trace evidence. That bundle may be the verified completion or a restart-sealed
unknown outcome backed by the exact durable dispatch claim. A bare row or manually flipped
terminal status is never enough.

This ledger prevents duplicate application starts only inside that surviving signed-session
scope. It does not prove exactly-once execution at an external provider, and an unknown result
is never replaced automatically. Claims remain until the signed cookie expires, then bounded
startup, periodic, and per-create cleanup removes them. Explicit demo reset refuses without
mutation while a caller-owned claim is reserved, started, or unknown. Once the owner resolves
it to ready, successful reset removes the caller's creation claims and access only; claims and
access belonging to another session, including another session sharing the canonical replay,
remain.

Known admission abandonment deletes an untouched reserved claim and successful reset deletes
ready claims, so both release capacity. An unresolved reset is refused without mutation, and
terminal recovery cleanup alone does not release a still-unexpired claim.

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

If successful demo reset removes this session's access after admission, the next public-ledger
poll ends the stream normally without a synthetic event and releases its lease. Reconnecting
with the detached session receives the ordinary generic 404; access held by another session is
unchanged. This narrow EOF applies only to missing caller access. A retained access row whose
recovery is missing remains an integrity failure rather than being normalized to disconnect.

Every admitted public stream is also bound to the exact UTC expiry verified from its signed
session cookie. The generator checks that deadline before consuming the buffered admission
batch, before and after every later public-ledger read, immediately before each event or
heartbeat emission, and after the disconnect probe. Idle sleeps are capped to the remaining
session lifetime. Once a deadline check reaches expiry, the stream closes without an expiry
control frame; no buffered, polled, or heartbeat data is emitted at or after the deadline, and
its admission lease is released. A native reconnect with the expired cookie receives a fresh
session identity and the same generic `404` boundary for the former recovery; session expiry is
not disclosed separately.

The leased response independently tracks the ASGI response-start handoff against the same UTC
deadline and applies the remaining lifetime to every non-empty body send. Response start
contains no owner evidence, and the stream source is not consumed until it completes. If server
flow control still blocks that handoff at expiry, the admission lease is released without
cancelling the header send; cancellation before Uvicorn records response start could trigger an
unbounded fallback response outside this wrapper. If the handoff later completes, every
non-empty body is suppressed because the session is already expired. A body send that was
already in progress at expiry is cancelled before completion. Control returns to the generator,
which terminates at its next expiry or terminal check and releases the admission lease before
the ordinary empty final body. That final teardown send is attempted for at most 100
milliseconds. Expiry does not become a public error. This application/ASGI boundary can leave
an uncounted response-start task blocked until transport progress or disconnect, and ASGI cannot
recall bytes a transport already accepted before the deadline merely because network delivery
completes later.

This is not a fleet-wide limit. Every worker or container would have independent counters. The
production launcher uses one Uvicorn worker, so the configured defaults cap polling loops only
in that one process; horizontal scaling requires a separate shared-admission design.

The UI stores no cookie, keyed session correlation, serialized run state, approval or decision
payload, or provenance claim in browser storage. Same-tab `sessionStorage` may retain one
syntactically validated hotel recovery UUID as a non-authoritative reload hint and a pending
creation tuple containing the raw UUID `clientRequestId`, exact scenario, and exact execution
mode. The pending tuple authorizes only a retry of that same start and remains until a matching
snapshot is accepted or the user explicitly abandons a conflicting intent. The
server-authorized GET must return a contract-valid hotel snapshot before the recovery UUID can
restore the event stream, consent, or receipt. A generic authorized 404 or a valid correlated
snapshot for another scenario clears that hint; these cover missing, foreign-session,
expired-session, and retention-deleted state without distinguishing them. Network errors,
non-404 responses, malformed JSON, and contract-invalid or miscorrelated success responses
retain the UUID but authorize no snapshot or action. The browser shows fixed redacted copy and
only **Retry saved recovery**; repeated activation coalesces to the same GET. It never surfaces
response text or automatically creates a run, starts a replay, or resumes a decision. A retry
404 clears the hint, while a valid hotel snapshot restores the ordinary lifecycle. On a
keyless runtime after a terminal lookup, the browser offers replay and SDK QA as explicit
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

Approval is claimed in a SQLite `BEGIN IMMEDIATE` transaction. For a missing or incomplete
durable demo-adapter execution, the execution writer acquires another `BEGIN IMMEDIATE`, samples
the authorization time only after acquiring that lock, and then recomputes the consent digest and
rechecks expiry, hard constraints, delegated authority, the stored SDK interruption, the claimed
approve action, and the SDK-approved pending marker before writing. The same transaction rejects
terminal evidence and any different execution for the recovery. The execution binding includes
the recovery, interruption, approved remedy digest, and request fingerprint; the pending action
digest independently binds the complete typed evidence. An exact completed execution key with the
same stored bindings replays before current authorization and expiry checks only when no other
execution key makes the recovery ambiguous. Only the canonical incomplete shape (`pending`, zero
provider execution, and no result) can be completed under current authorization. Mixed states and
multiple keys fail closed without rewriting them. Retrying the same decision ID returns its
stored result; a different loser receives `409 already_decided`. These controls and tests
establish an at-most-one durable dispatch record for this demo adapter, not exactly-once execution
at an external provider. An unrelated session cannot participate in that race: it receives 404
before a decision claim and cannot win, renew a live lease, or trigger provider dispatch.
The browser accepts a decision conflict only when the 409 body has the exact documented shape,
contains an endpoint-specific allowlisted code, and names the recovery being displayed. It
disables the stale action context and requests one authoritative snapshot refresh; it does not
infer the winner, mark the losing action accepted, create a replacement decision ID, retry, or
automatically resume. The exact stale context stays blocked if the inspector switches away and
later revisits the same unchanged evidence. A malformed, unknown, or cross-recovery conflict
remains an ambiguous generic failure under the existing exact-action retry fence.

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
Submit conflicts document `already_decided`, `decision_id_conflict`,
`decision_resume_unavailable`, `decision_unavailable`, and `resume_incompatible`; the shared
continuation can surface the resume-unavailable code if its just-claimed row disappears before a
concurrent completion reload. Resume conflicts document `decision_resume_unavailable`,
`decision_id_conflict`, `decision_unavailable`, and `resume_incompatible`. Both endpoints
document the exact no-fallback `decision_capacity` 429 envelope and the shared bounded-body 413
envelope emitted before route parsing. Resume 422 documentation includes every
consent-validation code the stored claim can reach, in addition to invalid body, expiry, and
malformed-path responses.

This is a signed-session capability, not proof of the original tab: another tab with the same
HttpOnly session and recovery UUID may explicitly continue the already-fixed claim. Concurrent
attempts remain bounded by the durable execution/idempotency ledger to at most one demo-adapter
dispatch, but may duplicate live model work across workers. Capacity returns the existing
`decision_capacity` response and leaves the explicit action available; it never starts replay or
retries automatically. At or after the original consent expiry, the targeted lifecycle sweep
runs before capacity or dispatch. The sole canonical approved provider result that already
committed is reconciled to its completed receipt without another dispatch. Every other exact
unfinished claim seals conservative terminal evidence, after which resume returns stable
`422 remedy_expired`.

If the authoritative UTC expiry is reached with the recovery and envelope still pending and no
decision, execution, receipt, or terminal event exists, a bounded `BEGIN IMMEDIATE` lifecycle
sweep closes the recovery instead. A decision claim that commits first wins and is left for the
normal resume path until its consent deadline; an expiration that commits first makes later
claims unavailable. At that deadline, an unfinished approve seals `outcome_unknown`; an
unfinished decline seals `closed_without_action` only when zero execution is proven and otherwise
seals `outcome_unknown`. The claim and execution rows are preserved. The browser's local timer
only disables both controls and asks for fresh server evidence; it does not infer the outcome.
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
evidence was sealed. A repeated sweep or process restart does not rewrite that evidence. A
claimed-but-incomplete decision uses the distinct `recovery.claim_expired` event. Approve
records one committed approval decision with unknown provider execution; decline records zero
approvals and claims `providerExecution=false` only when its pending state and empty execution
ledger prove no work began. Every ambiguous case remains `outcome_unknown`, while a sole
canonical completed approve execution is finalized instead of expired.

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

The SDK stub commits an exact `pending` execution row in the same transaction as the recovery
and creation event, before its no-approval tool can dispatch. The recovery's internal
`quota_execution_contract=1` marker therefore requires exactly one canonical execution row.
Immediately after the demo adapter returns, the tool boundary changes that row to
`result_recorded` with the exact verified-and-revoked result. The orchestrator accepts only an
SDK result with a concrete empty interruption list and then durably changes the row to
`completed`. The recovery's five intermediate quota events, terminal event, receipt, final
snapshot, and creation claim then commit atomically. A process restart after that validated
`completed` marker reconstructs the unchanged seven-event outcome without calling the adapter.
A restart with only the dispatch claim atomically seals `quota.outcome_unknown`,
`providerExecution=null`, and `redispatched=false`; it does not assert whether execution,
verification, revocation, or an unrecorded human request occurred and never retries the adapter.
Same-owner creation retries return that authoritative terminal resource, while foreign sessions
retain the generic 404.
If the SDK returns a human interruption, a malformed or missing interruption field, or raises
after storing a result, the execution is retained as `sdk_invariant_failed`; no zero-approval
terminal bundle is authoritative, the outer claim becomes unknown, and restart fails closed for
operator review. A restart or concurrent runtime that first sees `result_recorded` also
attempts to quarantine it because SDK completion was not yet durably validated. That quarantine
uses the exact observed state as a compare-and-swap: if the live runner commits `completed`
first, the restart accepts and finalizes that validated marker instead of retracting it. If the
quarantine commits first, the runner cannot validate or publish the row. Only `completed` is
restart-finalizable.
Migration defaults pre-contract rows to marker zero, but accepts a completed row without an
execution only when the first upgrade transaction also recorded its exact recovery fingerprint.
The provenance table, fingerprints, and marker column commit in one savepoint; interruption
rolls all three back for a clean retry. A current marker-one completion cannot be laundered by
deleting its execution and flipping the marker. A canonical sealed legacy completion remains
readable, and its exact same-owner retry promotes a lagging started or unknown outer claim to
ready. Every marker-zero in-progress row and every marker-one row with a missing, ambiguous, or
non-reconcilable execution fails startup without mutation or provider dispatch. The same full
terminal preflight gates `/readyz`. A handled failure before the atomic recovery/claim insert
marks the outer creation ledger unknown; a hard loss remains pending until its stale bound and
then becomes unknown. Neither is represented as a quota resource.
