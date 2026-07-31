# Architecture

Backchannel is a React operational console backed by one long-lived FastAPI process and
SQLite. Production serves the Vite build and API from the same origin so browser event
streams, consent decisions, and authoritative receipts share one server boundary.

```text
Browser (React/Vite)
  |-- GET /health, /api/scenarios, snapshots, receipts
  |-- POST /api/recoveries and digest-bound decisions
  `-- long-lived GET /api/recoveries/{id}/events (SSE)
                         |
FastAPI + public-demo controls + recovery orchestrator
  |-- SQLite: creation claims, recoveries, approvals, decisions, executions, events, receipts
  |-- OpenAI Agents SDK: openai_live or deterministic sdk_stub
  `-- simulated hotel/quota demo adapters
```

## Server-authoritative flow

The browser validates the public response shape but never invents provenance or terminal
state. A created recovery returns a typed snapshot. Its `recoveryId` selects one persisted,
ordered event stream; native `EventSource` reconnects with `Last-Event-ID`, and the server
replays later rows before waiting for new commits. Terminal events close the stream, after
which the UI refreshes the authoritative snapshot before accepting its receipt. Both reads have
separate 12-second deadlines, abort ownership, stale-settlement suppression, and an explicit
retry that never posts a decision or starts a replacement recovery. A receipt is rendered only
when its recovery ID, scenario semantics, execution mode, terminal-status mapping, root trace,
and ordered model IDs match the current snapshot; a superseding same-ID snapshot retires the
older receipt request. Quota replay copy states that no adapter ran, while matching SDK evidence
retains its simulated execution claim. The authorized cursor header may occur at most once and
accepts ASCII digits only in the SQLite-safe numeric range
`0..9223372036854775807`; leading zeroes are allowed.

Event delivery uses a lock-protected, fail-fast admission controller. The defaults admit at
most 16 polling loops in this application process and at most four for one recovery. Session
authorization runs first; cursor validation then precedes targeted consent expiry, recovery
existence confirmation, and stream admission. Saturation therefore changes neither the generic
foreign/unknown 404 nor an authorized invalid-cursor 400, and an invalid authorized cursor
cannot mutate lifecycle state. A rejected connection receives one finite `stream.capacity`
control event with a five-second native retry field and then closes; it never enters the polling
loop. The browser validates that exact control envelope, presents its message as transient, and
leaves native `EventSource` reconnection active. A later durable recovery event clears the
message, while a terminal event still closes the source.

An admitted stream retains only the already verified opaque session correlation plus its exact
UTC cookie expiry. The generator rechecks that deadline around public-ledger reads and directly
before event and heartbeat emission, caps idle waits to the remaining lifetime, and closes
silently when a deadline check reaches expiry. Buffered or newly committed owner evidence is
never emitted at or after the deadline, and the lease wrapper releases the process-local slot.
Native reconnect then crosses the ordinary cookie verification boundary; a newly issued session
receives the same generic recovery `404` rather than an expiry-specific disclosure.

The response layer also tracks the ASGI response-start handoff against the remaining session
lifetime. Response start contains no owner evidence and Starlette does not consume the stream
source until that handoff completes. If it remains transport-backpressured at expiry, the
process-local admission lease is released so another polling loop can enter, but the header send
is not cancelled: Uvicorn can otherwise synthesize a fallback response outside the application
deadline. When the header handoff eventually completes, the expired response suppresses every
non-empty body. A body frame that had already begun sending is cancelled at expiry before send
completion; the generator then terminates and releases its lease before the empty closing body.
That final teardown send has a 100-millisecond grace instead of an unbounded wait. This is the
application/ASGI handoff boundary: a response-start task may remain blocked after it stops
counting as an admitted polling loop, and bytes already accepted by the transport before expiry
cannot be recalled if the network delivers them later.

A live-ready page load is deliberately idle. **Start live recovery** is the only UI action that
creates an `openai_live` hotel run, and an in-flight guard coalesces rapid activation before
React can re-render the disabled control. Same-tab `sessionStorage` may hold the canonical UUID
of the last validated hotel snapshot and, while creation is unresolved, a pending tuple with
the raw UUID `clientRequestId`, exact scenario, and exact execution mode. The tuple is cleared
after a matching snapshot is accepted or the user explicitly abandons the conflicting start.
It stores no cookie, keyed session correlation, serialized run state, approval or decision
payload, or provenance claim; the signed HttpOnly cookie remains the access credential. The
server never persists, logs, or responds with the raw token and stores only its session-scoped
HMAC.

Reload performs the authorized snapshot GET before any fallback or new start. A valid snapshot
restores its SSE, consent, and receipt lifecycle. Malformed local hints, generic 404s, and valid
correlated wrong-scenario lookup results are terminal: they are cleared and leave the UI in a
truthful no-run state. Network failures, non-404 responses, and malformed, contract-invalid, or
miscorrelated success responses are indeterminate instead. They retain exactly the canonical
UUID, trust no snapshot, and expose only **Retry saved recovery**; rapid activation coalesces to
the same authorized GET, with no automatic POST, decision resume, or fallback. If that retry
returns a generic 404 the hint is cleared; a valid hotel snapshot restores normally. If live
mode is unavailable after a terminal lookup, the no-run state offers explicit replay and SDK QA
actions without automatically creating a replacement. While a lookup, retry, or explicit start
is unresolved, the lifecycle remains neutral at **Awaiting server evidence** with no current
step or execution-mode claim. React StrictMode can probe the idempotent GET effect twice in
development, but it does not create a recovery; live creation remains explicitly initiated and
one-POST.

Every creation POST includes a bounded `clientRequestId`. The server derives a
session-scoped HMAC from it and stores only that digest with the opaque session correlation,
signed-cookie expiry, canonical scenario/mode fingerprint, and reserved recovery UUID. A
short SQLite `BEGIN IMMEDIATE` elects one owner; the transaction ends before replay, SDK, or
live work begins. Exact ready retries in the same surviving signed session return the
authoritative stored snapshot without another orchestration, admission, or budget charge.
Changed payloads fail with `idempotency_conflict`, active contenders and hard-loss starts inside
their five-minute stale window receive `creation_pending`, and uncertain handled or stale
post-start outcomes fail closed as `creation_outcome_unknown` without launching a replacement.

Only row-absent claims enter the capacity gate inside that same `BEGIN IMMEDIATE`
transaction. Defaults allow 32 unexpired creation rows for one signed session, 128 for one
opaque HMAC IP correlation, and 2,048 globally;
`BACKCHANNEL_MAX_RECOVERY_CREATIONS_PER_SESSION`,
`BACKCHANNEL_MAX_RECOVERY_CREATIONS_PER_IP`, and
`BACKCHANNEL_MAX_RECOVERY_CREATIONS_GLOBAL` configure bounded integers, with only the session
limit constrained not to exceed the global limit. Reserved, started, ready, and unknown rows
all count while their signed-session expiry is in the future, even if their recovery has
become terminal. Expired rows never count, including while bounded cleanup is still draining
them. An exact existing key is evaluated first and therefore remains retryable at capacity;
IP mobility neither rewrites nor invalidates its first-reservation IP correlation.
The denied start itself returns exact `429 creation_capacity` without an insert, recovery,
orchestration, live-admission, or budget side effect and without `Retry-After`, capacity
counts, limits, fallback metadata, or internal identifiers. Ordinary bounded pre-claim
maintenance runs before capacity evaluation and may mutate unrelated expired creation rows,
pending approvals, or terminal recoveries.

The IP gate persists only the 64-character keyed correlation already derived by the public
controls layer, never a raw peer or forwarded address. It is a defense-in-depth abuse boundary,
not identity or guaranteed per-user fairness: shared NATs can share one gate. The direct peer
is used unless trusted-proxy mode is explicitly enabled with matching proxy CIDRs. Legacy
rows are backfilled from their opaque session correlation so upgrades preserve claim
dispositions without fabricating or exposing historical addresses.

Ready and reconciled claims do not trust a recovery status alone. Replay retries pass through
the canonical fixture/event/receipt integrity validator. Hotel SDK/live results require either
valid pending or claimed consent evidence, or a terminal mode-matched receipt. SDK quota
creation requires one canonical terminal bundle and QA trace evidence. A normal completion binds
the exact durable quota result to the full seven-event ledger and receipt. A restart after only
the pre-dispatch claim binds an exact `outcome_unknown` receipt and `quota.outcome_unknown`
terminal event instead; that bundle claims neither provider execution nor permission revocation.
For a normal completion, the SDK tool boundary first commits the execution result as
`result_recorded`. After the SDK runner returns, the orchestrator accepts only a concrete empty
interruption list and then durably promotes that row to `completed`. A later `BEGIN IMMEDIATE`
transaction atomically binds the validated result to the recovery, terminal event, receipt, and
matching creation claim. For the no-result restart path, the execution's `outcome_unknown`
transition and that terminal evidence change in the same transaction. A recovery created under
this contract carries an internal marker that requires exactly one canonical execution row.
Migrated marker-free completed quota bundles remain readable with the marker defaulted to
legacy zero only when the same schema-upgrade transaction recorded a fingerprint of the exact
recovery row. Table creation, fingerprint registration, and marker addition share one
savepoint, so an interrupted first open rolls back and retries without losing legacy
provenance. A current marker cannot be downgraded into that set by deleting its execution. An
exact sealed legacy terminal bundle may promote its same-owner started or unknown creation claim
to ready. Legacy in-progress rows and marked rows with missing or non-reconcilable executions
fail startup unchanged and without redispatch. Startup and readiness apply the canonical
terminal-bundle validator to every durable SDK quota completion and unknown outcome. Missing,
cross-bound, partial, or tampered evidence fails closed.

The zero-approval SDK return is also an evidence boundary. Any human interruption, missing or
non-list interruption field, or exception after the demo result was stored leaves only
`result_recorded`; the server retains it under `sdk_invariant_failed` rather than converting it
into a zero-approval terminal bundle. A restart that finds `result_recorded` applies the same
quarantine because the SDK return was never durably validated. The outer creation claim becomes
unknown, public terminal validation rejects the bundle, and subsequent startup refuses the
quarantined row unchanged for operator review. A concurrent runtime cannot publish that
unvalidated result; only a previously validated `completed` marker can be finalized after a
crash.

This is request deduplication inside one signed-session and identity-secret scope, not an
exactly-once guarantee for an external provider outcome. Losing or expiring the cookie, or
rotating the identity secret, creates a new scope that cannot recover the old request token
binding. Claims expire with their signed cookie and are removed in bounded startup, periodic,
and per-create cleanup. Explicit demo reset returns `409 reset_creation_pending` without
mutation while the caller session has a reserved, started, or unknown creation claim; it may be
retried after the owner resolves or the signed session expires. A ready claim is resettable.
Successful reset deletes only the caller session's claims and access; another session's claim
and access to a shared canonical replay remain intact.

Demo reset is a capture-only local capability. Runtime configuration rejects it whenever
deployed mode is enabled, and packaged CI plus replacement-container profiles do not pass the
reset flag. The locked local capture lane runs with deployed mode disabled and opts in explicitly
to create deterministic screenshot states.

Known live-admission rejection deletes an untouched reservation and releases its capacity.
Successful reset releases ready claims, but unresolved reset is refused without mutation and
ordinary terminal-recovery cleanup does not delete a still-unexpired creation claim. Signed
session expiry plus bounded creation-ledger cleanup is the remaining release path.

SQLite persists the pending Agents SDK state envelope, consent evidence, decision claim,
provider execution record, event ledger, receipt, and opaque recovery-access association. A
pending approval carries the Agents
SDK, protocol, agent-graph, and definition versions that must still match on resume. Startup
reconciliation can seal a receipt from an already committed demo-adapter result without
dispatching it again.

The durable demo-adapter execution writer acquires one SQLite writer transaction before sampling
the authorization time. A missing or incomplete execution is admitted only while that same
transaction still sees the fingerprint-verified approve claim, the exact approved interruption
and remedy binding, current consent and policy, the SDK-approved pending marker, no terminal
evidence, and no different execution for the recovery. An exact completed execution is replayed
before those current-authorization checks only when it is the recovery's sole execution. An exact
incomplete row may advance only from `pending`, zero provider execution, and no result; mixed
states and multiple keys fail closed without rewriting evidence. This lets restart recovery
return one unambiguous stored result without redispatch. The fence governs the local durable
demo-adapter ledger; it does not make an external provider side effect exactly once.

Hotel consent has a fail-closed eligibility gate before that persistence boundary. After the
SDK returns one exact typed interruption and, for mocked/live orchestration, the broker arguments
match the validated consumer and provider proofs, both current hard-constraint and delegated-
authority results must be true. Only then may the orchestrator create a recovery and atomically
attach its access row, followed by the pending consent transition. The store independently
recomputes the consent digest and policy and binds the stored remedy identifier, exact terms,
cost, canonically sorted changed fields and commitments, expiry, and action digest to the typed
evidence before `BEGIN IMMEDIATE`. Public snapshots expose literal-true policy fields only;
legacy or tampered pending rows fail closed with `pendingApproval=null`. A tampered unfinished
decision claim also has `claimedDecision=null`, so the browser receives no decision or resume
action. The browser retains an additional no-action rendering guard for malformed input.

Typed SDK evidence forbids unknown fields at every nesting level. Because those JSON Schemas are
part of the canonical agent-definition digest, this tightening intentionally changes that digest:
serialized pending approvals made against the older definition fail resume compatibility rather
than being interpreted under the newer contract.

These unkeyed digests detect accidental or non-coordinated row changes inside the application
trust boundary. They are not authentication against a malicious database writer able to replace
the stored fields, typed evidence, consent digest, and action digest coherently.

For a policy-denied live start, public-demo admission still truthfully records the consumed
budget unit, then releases that exact admission after the orchestrator fails. The response is the
ordinary generic correlated 500 and no replay, recovery artifacts, or hotel-adapter dispatch is
created. The regression uses an injected scripted model provider and does not establish a real
OpenAI call, public deployment, or real booking-provider execution.

The browser never needs a duplicate decision request to continue an unfinished claim. An
authorized snapshot may expose only its server-authored action, remedy digest, and UTC expiry.
The single **Resume exact approval/decline** action posts exact empty JSON; session access and
request-shape checks precede claim inspection, and SQLite supplies the immutable client decision
ID, remedy, digest, tool call, and fingerprint. The orchestrator then uses the same continuation
pipeline and immediate consent/version validation as the original decision. Completed claims
replay without model or provider work. Live claims retain the existing lease and model-capacity
gate, with no automatic retry or replay fallback.

This resume capability follows the signed demo session, not an original browser tab. It cannot
create or change consent, and reload never invokes it. Concurrent attempts retain the durable
demo-adapter idempotency boundary, but they do not prove one live model run across processes.
At the authoritative expiry, a bounded writer transaction classifies an unfinished exact claim:
approve seals `outcome_unknown`; decline seals `closed_without_action` only when zero execution
is proven and otherwise seals `outcome_unknown`. The decision row and execution ledger remain
intact, and the sole canonical completed approve result is instead finalized as `completed`
without capacity acquisition or redispatch. Exact later decision/resume attempts return stable
`remedy_expired` after the terminal evidence is visible.

Untouched SDK/live hotel consent is also durable lifecycle state. A bounded SQLite
`BEGIN IMMEDIATE` sweep runs at startup, on the existing maintenance cadence, and before new
recovery cleanup. Public snapshot and receipt reads use one writer transaction to authorize the
opaque session before sweeping that exact target, then load and validate the resulting snapshot,
receipt, and event ledger before commit. Initial SSE uses the same transaction after the outer
access and cursor checks and acquires its process-local stream lease only after validation.
Subsequent SSE polls use read-only snapshots and repeat access plus evidence validation without
taking a writer lock. A stale outer access result can therefore neither mutate another session's
recovery nor inspect its integrity; validation failure rolls back a just-sealed expiry. Decision
requests retain their exact targeted sweep before claim handling.

Post-admission public polling distinguishes access revocation from corrupt storage. Demo reset
removing the caller's access produces a normal frame-free EOF and releases the process-local
stream lease; a reconnect by that caller reaches the existing generic 404 boundary, while any
other session's access remains valid. If access still exists but its recovery row is missing,
the public evidence fence raises an integrity failure instead of disguising it as disconnect.

Terminal public evidence requires one receipt and exactly one final terminal event, with exact
recovery, mode, status, model/trace/version provenance, scenario semantics, and one shared
timezone-aware UTC seal across the recovery update, receipt, and terminal event. Every dynamic
ledger must start with the exact seq-1 `recovery.created` event at the snapshot creation time;
sequences are contiguous and event timestamps are UTC, nondecreasing, and no later than the
snapshot update. Canonical replay is stricter because it is written atomically: recovery
creation/update, every replay event, and its optional receipt must share that same seal time.
Nonterminal rows may have no terminal evidence. These checks are public-read fences; raw store
readers remain available for internal reconciliation and migration paths. Owner-side
disagreement fails through the generic sanitized 500 boundary, while unauthorized and absent
reads remain the same 404.

An active SDK/live hotel bundle is also exact rather than a status-only snapshot. It requires the
step-three approval summary, the canonical `recovery.created` and `approval.requested` ledger, one
pending approval and one bound remedy/consent record, plus the public view allowed by its durable
state. An untouched run exposes only the pending approval; an ordinary claimed run exposes only
the claimed decision. A conservatively quarantined decline may expose neither while its exact
claim remains durable. Claimed evidence binds the action, digest, pre-expiry UTC timestamp,
claim-specific recovery summary, and the only execution shapes that its action and envelope state
permit. Missing, ambiguous, altered, or impossible active evidence fails before an owner snapshot,
receipt lookup, or SSE admission; foreign access still fails at the indistinguishable 404
boundary. Both targeted and background expiry validate the same active source before sealing it,
and same-key creation retries cannot promote a status-only active or expired hotel row.

Expiry and decision claims serialize as competing writers: an expiration that commits first
prevents a later claim, while a claim that commits first owns continuation only until its consent
deadline or a canonical completed result. Untouched expiry seals one `recovery.expired` event;
claimed expiry seals one `recovery.claim_expired` event with its conservative terminal receipt.
Both mark the pending envelope and remedy terminal and release the matching live admission with
`COALESCE` so cooldown and aggregate usage evidence remain intact. The recovery, receipt, terminal
event, and pending-envelope update share one UTC seal at or after the bound consent expiry.
Background cleanup scans a bounded keyset page before applying its requested mutation limit. Its
SQLite cursor advances past invalid candidates and wraps after the ordered candidate set, so even
a full corrupt page cannot permanently starve later valid expiry evidence across cleanup cycles.

## Runtime provenance and trust boundary

| Mode | Runtime work | Trace evidence | Provider boundary |
| --- | --- | --- | --- |
| `openai_live` | Real OpenAI model calls through the Agents SDK | `trace_...`, recorded model IDs, version and definition digest | Simulated hotel demo adapter only; no real booking or payment |
| `sdk_stub` | Actual Agents SDK runner with deterministic local model responses | `qa_trace_...`, no model IDs, version and definition digest | Simulated hotel or quota demo adapter only |
| `replay_fixture` | Persisted playback of a bundled protocol trace | No root trace, model IDs, SDK version, or definition digest | No model call and no provider execution |

`/health` exposes only `backend`, `liveReady`, and `providerBoundary=demo_adapter_only`;
it never exposes the API key. A live UI label is allowed only when health is live-ready and
the active snapshot or receipt independently records `executionMode=openai_live`.
When no request is pending and no snapshot exists, the inspector, lifecycle, and provenance
strip state that no server run has started. While a request is unresolved, they instead show
neutral awaiting evidence; neither state presents the bundled replay fixture as active evidence.

## Production shape

`npm run build` produces `web/dist` and a deterministic, canonical
`.backchannel-static-manifest.json`. The manifest covers `index.html` and the exact emitted asset
tree with positive byte sizes and SHA-256 digests; the runtime verifies it but never creates or
repairs it. `npm run start` launches one Uvicorn worker through
`scripts/start.py`; local mode binds to `127.0.0.1`, while explicit deployed mode binds to
`0.0.0.0`. FastAPI mounts hashed `/assets` and uses a restricted route-like SPA fallback;
API, documentation, missing asset, and suspicious paths remain non-HTML 404s. A configured static
root does not serve the SPA shell or assets while its manifest, exact file closure, type, path,
size, digest, or index-to-asset references fail verification. Symbolic links and special files
are rejected.
The launcher configures Uvicorn's process-local admission threshold at 64 tracked
connections/tasks, a 128-connection listen backlog, and a five-second keep-alive timeout. Once
the threshold is reached, newly parsed requests receive HTTP 503. These controls do not bound
pre-request sockets, replace edge connection/header timeouts, or prove fleet-wide and
multi-container saturation behavior.

The multi-stage Dockerfile builds Node assets separately, installs the frozen Python runtime,
runs as UID/GID 10001, stores SQLite under writable `/data`, and checks `/readyz`. Every external
base keeps a readable version tag plus a reviewed immutable OCI index digest. A release contract
binds the frontend, package installer, Python build, and runtime stages to those exact references
and requires both Python stages to share one base. Updating a base therefore requires a deliberate
tag-and-digest change followed by the packaged build/smoke gate.
The root build context recursively excludes local `uv` caches, browser/runtime capture output,
common private-key container formats, and npm/Python/netrc credential files before transfer to
the builder. The release contract binds the complete root ignore-rule set, rejects ignore-file
negations, malformed raw line boundaries, byte-order marks, NUL bytes, and alternate root
Dockerfile-specific ignore files. Exact raw SHA-256 digests bind both `.dockerignore` and the
Dockerfile before the readable semantic checks run. The Dockerfile contract rejects byte-order
marks, all line-continuation syntax, external frontend selectors, unexpected pre-`FROM`
instructions, and compound `ONBUILD` instructions. It also binds every `COPY` and `RUN`
instruction to the reviewed lists and rejects every direct `ADD` source. This minimizes
builder-visible local data. It does not prove a malicious builder or external cache is
trustworthy, erase content retained by an earlier build, or identify secrets stored under an
unknown future filename.

The checked-in packaged launch profiles make the runtime boundary explicit. Both the primary
container smoke and the planned replacement smoke request UID/GID `10001:10001`, a read-only root
filesystem, all Linux capabilities dropped, no-new-privileges, Docker's built-in seccomp profile,
private IPC and cgroup namespaces, bridge networking, a 128-process ceiling, no restart policy,
and a loopback-only published port. Exactly one writable local named volume is mounted at `/data`;
the image creates that directory as owner-only `0700` and routes SQLite temporary files there with
`SQLITE_TMPDIR=/data`.

Before readiness, a secret-safe helper inspects the returned container ID rather than its mutable
name. Its projection excludes environment values and host mount sources while binding the
daemon-recorded user, privilege, capability, namespace, device, process, restart, port, and mount
configuration. Inspect output is captured outside the Python heap and accepted only below 32 KiB.
A separate suppressed-output `docker exec` probe uses the pinned base interpreter in isolated,
no-site, no-bytecode mode. It observes PID 1 as UID/GID 10001 with no unexpected supplementary
groups, zero capability sets, `NoNewPrivs: 1`, and seccomp filter mode; verifies `/data` ownership
and writeability; exercises a file-backed SQLite temporary database; and confirms an owner-writable
image directory is read-only at runtime. Cleanup attempts the owned containers before their
volume, checks both are absent, and fails closed without broad prune operations or masked errors.
This proves the requested daemon configuration and observed process state only on the exact
runtime that executes the helper. It does not identify the byte-for-byte built-in seccomp profile,
trust the daemon, kernel, pinned base interpreter, or dynamic loader, prove
user-namespace/rootless isolation, constrain outbound network access, encrypt the volume, survive
abrupt runner loss, or establish target-host parity.

Readiness coalesces separate database and static-artifact probes behind process-local locks. Both
success and failure are cached for five seconds, so a newly damaged or repaired static tree can
remain stale for less than one cache interval. A database refresh uses a
dedicated SQLite connection configured with a 350 ms writer-lock timeout, starts
`BEGIN IMMEDIATE`, retains the quick integrity, foreign-key, and required-schema checks, requires
the probe to be a real trigger-free table with exact columns and primary-key positions, validates
the full singleton row and binary value, toggles its generation exactly once, and explicitly
commits. The endpoint never creates or reseeds that row; store initialization and migration own
it. This proves one committed local SQLite write at the most recent uncached probe. The
five-second result can be stale, and the writer timeout does not bound every integrity query. It
does not prove the next write, available disk space, backup recovery, target-host durability or
networking, NFS behavior, or concurrent multi-container safety. HTML receives a self-only
script/style/image/connect CSP; API and non-HTML responses receive `default-src 'none'`.

The static manifest detects partial builds, corruption, and build skew inside the intended
root-owned immutable container image. It is not a signature or external provenance statement.
A principal that can coherently replace both files and manifest, or mutate the tree during a
request, remains outside this local integrity claim; image signing and target-host controls are
separate release gates.

The pinned base digests make the checked-in Docker inputs reproducible and fail closed if a
referenced manifest disappears. They do not sign the resulting image, attest the builder, scan
base contents, or prove that a registry publisher is trustworthy; signing, SBOM/vulnerability
policy, and target-host admission remain separate release gates.

This deployment requires a host that preserves one long-lived HTTP connection for SSE and a
persistent `/data` volume. It is intentionally not converted to short-lived serverless
functions. One worker avoids presenting the in-process demo adapters as a distributed
production provider system.

SSE admission is deliberately process-local. Its lock makes global and per-recovery acquisition
atomic only inside this application process; separate Uvicorn workers or separate containers
would each own independent counters and could exceed the configured aggregate. The checked-in
launcher therefore remains `workers=1`. A future horizontally scaled deployment would need an
explicit shared admission design before claiming a fleet-wide limit.

## Public-demo controls

The server applies request-size limits, exact Host and CORS configuration, generic public errors,
redacted server logging, per-IP and per-session live cooldowns, a live concurrency cap, a
configurable daily admission budget, terminal-record TTL cleanup, and an HttpOnly demo-session
cookie. Long-lived event streams add configurable process and per-recovery caps plus a bounded
retry interval and are bound to the verified signed-session expiry. Admission leases are
released idempotently after session expiry, terminal completion, client disconnect,
cancellation, iterator/store/encoding failure, blocked-send expiry, ASGI send failure, or
response construction failure; zero-count recovery entries are removed. Every HTTP request must
carry exactly one syntactically valid Host whose normalized bare DNS name or IP literal is
allowlisted; rejection happens before body reads, session issuance, routing, or durable mutation.
Development permits only loopback names/addresses and the test harness. Deployed mode additionally
requires an explicit bare-host allowlist, an exact HTTPS origin allowlist, and an explicit
32-byte-or-longer identity-hash secret. Proxy headers are ignored unless the direct peer is in an
explicit trusted CIDR allowlist.

The cookie contains a random nonce, expiry, and HMAC signature. SQLite never stores that raw
cookie, nonce, client address, API key, or authorization value. Instead, creation atomically
associates the recovery with a keyed 64-character session correlation value. Snapshot, SSE,
receipt, and decision routes check that association before any state read, lease renewal,
decision claim, or provider dispatch. Unknown, missing, expired, tampered, and unrelated
sessions all receive the same generic 404. Canonical replay rows may be associated with more
than one session only after each session explicitly starts that replay scenario.

The reset endpoint is disabled by default and is intended only for disposable capture runs.
When enabled, the reset transaction first refuses without mutation if the caller owns a
reserved, started, or unknown creation claim; this prevents reset from deleting the
deduplication claim beneath an owner that can still finish. After that owner resolves the claim
to ready, reset detaches only the caller's associations, deletes recovery detail only when no
other session retains access, and marks the caller's live admissions released. An unresolved
unknown claim remains fenced until owner reconciliation or signed-session expiry. Cooldown
history and the global usage ledger remain intact, so reset cannot restore live budget or
bypass an IP or session cooldown. Retention cleanup cascades access rows with expired recovery
detail while preserving aggregate usage. Secrets remain runtime inputs: they are not copied
into the frontend build or container image.
