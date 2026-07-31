# Validation

Run commands from the repository root with Node 22, Python 3.12, `npm`, and `uv`.

## Reproducible local gates

```bash
npm ci
uv sync --frozen --all-groups
npm run check
npm run smoke:stub
npm run build
npm run smoke:production
npm run secret:scan
npm run openapi:check
```

`npm run check` covers TypeScript, React/Vitest, the production web build, Ruff, strict mypy,
and pytest. `smoke:stub` proves the keyless deterministic Agents SDK approval path. The local
production smoke starts the same one-process static/API/SSE application used by the image and
checks approval, decline, receipts, replay SSE resume, CSP, readiness, static routing, and a
canary-secret non-disclosure rule. The frontend build deterministically emits the bounded static
manifest; runtime readiness verifies the exact index/asset closure, positive sizes, SHA-256
digests, safe paths, regular-file types, and index asset references. Missing, extra, empty,
modified, linked, malformed, or non-canonical artifacts fail with generic not-ready/404
boundaries. This is partial-build and corruption detection under the root-owned immutable-image
boundary, not signed provenance or proof against coherent replacement of files and manifest. The
smoke also verifies the deployed session cookie remains Secure,
HttpOnly, SameSite=Lax, and bounded by Max-Age. Because the local production lane models TLS
termination over loopback HTTP, only that smoke client manually carries the observed Secure
cookie across the loopback hop; the application never weakens the emitted cookie flags. A pass
also requires bounded SIGTERM handling and ordered, exact-process Uvicorn evidence that the ASGI
lifespan shutdown completed; forced termination is cleanup only and fails the smoke. The
`cleanShutdown=passed` marker remains local-process evidence, not container-orchestrator or
public-deployment proof.

The focused security regression is:

```bash
uv run pytest -q tests/security/test_session_isolation.py
uv run pytest -q tests/domain/test_pending_expiry.py tests/api/test_expiry_lifecycle.py
uv run pytest -q tests/domain/test_decision_resume.py tests/api/test_decision_resume.py
uv run pytest -q tests/domain/test_event_stream_admission.py tests/api/test_event_stream_admission.py
uv run pytest -q tests/api/test_terminal_read_integrity.py
uv run pytest -q tests/api/test_readiness.py tests/domain/test_store.py
uv run pytest -q tests/api/test_creation_idempotency.py
uv run pytest -q tests/integration/test_policy_eligibility.py
uv run pytest -q tests/integration/test_live_contract.py::test_mocked_live_policy_denial_is_generic_and_releases_exact_admission
npm --workspace web exec -- vitest run src/components/EvidenceInspector.test.tsx
npm --workspace web exec -- vitest run src/api/client.test.ts src/App.explicitLive.test.tsx
npm --workspace web exec -- vitest run src/api/events.test.ts src/hooks/useRecovery.test.tsx
npm --workspace web exec -- vitest run src/App.explicitLive.test.tsx src/recoverySession.test.ts
npm --workspace web exec -- vitest run src/App.receiptDeadline.test.tsx
```

It proves foreign snapshot/SSE/receipt/approve/decline all match an absent recovery's generic
404, owner approval remains at-most-once under a foreign race, the signed cookie survives a
same-secret restart, tampered cookies fail closed, shared replay access detaches per session,
reset preserves cooldown/budget history, and retention cleanup cascades access rows.
The creation-idempotency suite proves strict bounded request IDs, session-scoped HMAC
storage with no raw-token persistence, one SDK/live owner across sequential, concurrent, and
restart retries, conflict before admission or budget accounting, fixed pending retry metadata,
response-loss replay, pending-before-stale and permanent no-rerun handling for uncertain or stale
starts, safe
abandonment after a known live admission denial, and canonical replay sharing across sessions.
It also proves canonical replay integrity is revalidated on exact retry; bare or tampered
status/receipt/event evidence cannot become ready; authoritative terminal evidence can be
reconciled; claim expiry matches the signed cookie; cleanup is bounded and runs per-create,
at startup, and periodically; and reset removes only the caller session's claims while
preserving another session's shared replay claim.
It also proves bounded integer configuration, independent default and override behavior for
the opaque-IP gate, and the session-not-greater-than-global relationship; atomic per-session,
per-IP, and cross-store global enforcement over every unexpired status; same-key retry after
IP mobility and saturation without rewriting the stored correlation; expired cleanup backlog
exclusion; opaque legacy-row backfill without claim invalidation; release after known
admission abandonment and successful ready reset; reset-refusal slot preservation for
reserved, started, and unknown claims; terminal-cleanup slot preservation; and exact
`429 creation_capacity` denied-start privacy: the denied start itself has zero orchestration,
admission, budget, recovery, or ledger-insert side effects. Ordinary bounded pre-claim
maintenance may still mutate unrelated expired creation rows, pending approvals, or terminal
recoveries. A 30-row expired backlog proves expired rows do not count independently of the
25-row per-request cleanup batch. Fresh signed-cookie sessions from one TestClient address
prove the shared-IP gate while a different address still succeeds; assertions cover only
64-character HMAC correlations in SQLite and no raw address in the database or response.
Trusted-proxy regressions also use raw duplicate `X-Forwarded-For` fields: rotating an
attacker-controlled first field while holding the proxy-appended client field constant produces
one opaque IP correlation and one cooldown domain. Single-field comma chains still stop at the
nearest untrusted hop, while disabled trust and an untrusted direct peer ignore forwarding data.
These are deterministic direct-peer tests, not a unique-user, NAT-fairness, or target-proxy
topology claim.
OpenAPI tests require the exact four-alternative 429 `oneOf`: no-fallback creation capacity,
plus `live_capacity`, `cooldown`, and `daily_budget` with replay fallback; `live_unavailable`
is excluded. Browser tests accept creation capacity only at 429, retain the same-tab intent,
and expose an explicit same-key hotel or quota retry. Quota conflict remains inert until the
user explicitly abandons only the matching tuple and starts a fresh UUID; arbitrary response
text is never surfaced.
The result is same-session request deduplication, not exactly-once external-provider proof.
The readiness suite proves required schema checks plus the trigger-free probe's exact visible and
hidden column/primary-key shape, full singleton row and binary value, one committed generation
toggle per uncached interval, five-second success and failure caching, concurrent request
coalescing, bounded writer-lock contention, query-only refusal, commit-failure rollback and
recovery, schema/row fail-closed behavior, unchanged application-table counts, and independent
`/health` liveness. Its deterministic seams and local SQLite file prove the application
transaction contract, not a total endpoint deadline, POSIX sidecar permissions, ENOSPC, the next
write, backup recovery, NFS, target-host behavior, or multi-container safety.
The policy-eligibility suite proves hard-constraint and delegated-authority denial both stop a
typed SDK-stub attempt before recovery/access/event/remedy/pending/decision/execution/receipt
persistence, with zero demo-provider dispatch. It also proves store refusal occurs before
`BEGIN IMMEDIATE`; every stored consent/evidence/action binding is rechecked; false, integer, and
string policy flags cannot satisfy literal `true`; unknown top-level and nested SDK arguments are
rejected; and tampered pending or claimed rows expose no public decision or resume action. The
mocked-live case proves a generic correlated error, release of the exact admitted lease, retention
of its one budget unit, three expected scripted model calls, and zero fallback, recovery
artifacts, or hotel-adapter dispatch. It is injected local test evidence, not a real OpenAI run.
The expiry suites prove SDK/live zero-dispatch receipts, live-admission release with cooldown and
usage preservation, restart idempotence, decision-versus-expiry writer serialization,
access-check ordering, synchronous snapshot/SSE/receipt/decision truth, bounded UTC maintenance,
stable `remedy_expired` retries, conservative claimed approve/decline terminalization, canonical
completed-execution reconciliation after expiry, and fake-timer controls that refresh once
without posting a stale decision or continuing after unmount.
The terminal-receipt browser suite proves separate finite snapshot and receipt deadlines,
exact abort ownership across timeout, retry, StrictMode, and unmount, snapshot-before-receipt
retry ordering, suppression of late and superseded same-ID results, recovery/mode/status/trace/
model correlation before storage and render, and truthful separation between non-executing quota
replay evidence and the deterministic SDK demo-adapter trace.
The decision-resume suites prove a minimal mutually exclusive claimed view, full stored-request
fingerprint verification, session authorization before JSON or claim inspection, exact empty
requests, stored approve/decline continuation, zero-dispatch declines, completed-response replay,
expiry before live capacity or dispatch, and post-expiry replay of the sole already-committed
provider result without capacity or redispatch. Browser coverage proves StrictMode/reload
performs no resume POST, rapid explicit activation coalesces, response action/digest mismatches
fail closed, and only the server-authored action remains available. Exact same-recovery,
endpoint-specific decision 409s latch the stale controls before one authoritative refresh without
claiming acceptance, selecting a winner, creating a replacement ID, or automatically resuming;
malformed, foreign, unknown, wrong-status, and wrong-endpoint envelopes remain generic. A failed
refresh keeps the stale context disabled, including after an A-to-B-to-A inspector revisit.
OpenAPI tests bind both decision endpoints to the exact
`200/404/409/413/422/429` set, route-specific conflict enums, the complete reachable resume 422
enum, and the shared bounded-body and no-fallback capacity envelopes. The proof is signed-session
scoped and establishes at most one demo-adapter dispatch, not original-tab ownership,
cross-process live-model serialization, deployment, or provider execution outside the demo
adapter.
The execution-authorization suite moves the deterministic clock at writer-lock acquisition and
proves that a missing or incomplete execution rechecks unexpired consent, the raw unfinished
approve claim, its fingerprint and exact bindings, the SDK-approved pending marker, action and
consent evidence, absence of terminal evidence, and absence of another execution in the same
transaction. Two store instances racing the same key produce one durable write and one stored
replay; different keys elect one owner and refuse the loser. The state matrix proves only
canonical pending evidence can advance, mixed states remain unchanged, and multiple completed
keys make replay ambiguous. Trigger-injected insert and update failures roll back without changing
authorization or execution evidence. A sole completed exact-key result still replays after
finalization and consent expiry. This is durable SQLite demo-adapter evidence, not exactly-once
proof for an external provider side effect.
The quota restart-durability suite proves that a provider result is stored as
`result_recorded` inside the SDK tool boundary before terminal trace persistence. An exact empty
SDK interruption list then writes a separate `completed` validation marker; a fresh runtime
reconstructs the exact seven-event completion without redispatch only after that marker exists.
If only the pre-dispatch claim survived, two concurrent fresh store/orchestrator instances
converge on one `outcome_unknown` event and receipt with zero provider calls, no recorded human
approval, and no execution, verification, or revocation assertion. Exact owner POST retry,
snapshot, receipt, and SSE reads return the same recovered resource while a foreign session
receives generic 404. For the no-result restart path, an injected receipt insert failure proves
the `outcome_unknown` execution transition, terminal event, receipt, snapshot, and creation-claim
changes roll back together before a later clean reconciliation. For a validated completed tool
call, the same failure proves that the durable result remains intact while all later
terminal-evidence changes roll back together. Counterexample tests reject changed creation
fingerprints, future creation or execution chronology, a pending execution attached to a
terminal unknown bundle, new marked rows with deleted executions, pre-contract in-progress
rows, and in-progress rows with terminal execution state. A pre-marker schema migration
preserves only a complete legacy terminal completion whose exact recovery fingerprint was
recorded in the same savepoint that adds the marker column, including ready claims with later
timestamps and exact same-owner promotion of lagging started or unknown claims. An injected
fingerprint failure proves table creation and marker addition roll back so the next open can
retry. Downgrading a current marker-one completion after deleting its execution remains invalid.
Deleting a marker-one terminal execution also makes both fresh startup and `/readyz` fail
closed before public reads. Deterministic
barriers prove that reconciliation preflight reads one SQLite snapshot and that an original
runtime accepts a validated result finalized concurrently by a fresh runtime without
duplicating terminal events. SDK invariant mutations cover a truthy human interruption,
missing or false-shaped interruption fields, and an exception after result persistence. Each
quarantines the retained result and changes an exact started outer creation to unknown in the
same transaction, without a zero-approval terminal claim; a trigger-injected creation update
failure rolls both changes back. Restart then fails closed unchanged. A fresh runtime racing
before SDK validation likewise cannot publish
`result_recorded`; it quarantines that evidence for operator review. A second barrier freezes
the restart's stale `result_recorded` snapshot while the original runtime validates and
finalizes. The quarantine compare-and-swap then preserves the canonical completion rather than
retracting it. This is deterministic SQLite and demo-adapter evidence in the
documented single-process runtime, not proof of a real quota provider or distributed lease.
The event-stream admission suites prove atomic global/per-recovery caps under threaded stress,
idempotent cleanup, finite exact capacity framing without a polling loop, privacy and cursor
ordering while saturated, expiry-before-admission, replay preservation, and reacquisition after
normal completion, cancellation, iterator failure, ASGI send failure, and response construction
failure. Signed-session deadline coverage binds the route to the exact verified cookie expiry,
suppresses an already-buffered event when iteration begins expired, suppresses a batch whose
public-ledger read crosses expiry, prevents later events and heartbeats, caps idle sleep to the
remaining lifetime, and proves lease release and reacquisition. Actual leased-response coverage
blocks the response-start handoff across expiry under ASGI 2.3 and 2.4, proves the lease releases
without cancelling that owner-evidence-free send, then suppresses every non-empty body after it
completes. It separately blocks both an event-frame and heartbeat send across expiry, proves
cancellation before the test transport completes either body send, bounds a blocked final empty
body, and reacquires the released slot. The test does not claim that ASGI can recall bytes
already accepted by a real transport or bound a response-start task after its admission lease
has been released. Browser coverage proves exact named-control validation, native retry
remaining open, one-shot preservation of the capacity status at stream EOF, transient error
clearing, and unchanged terminal closure. These tests prove at most the configured polling loops
in this application process. They do not prove a shared limit across workers or containers;
`scripts/start.py` is separately asserted to launch one worker.
Owner-reset coverage removes sole-owner access after admission under ASGI 2.3 and 2.4, proves
frame-free final completion with the lease already released, and reacquires the slot. A shared
recovery case proves reset detaches only that owner and preserves the other session. Terminal
stream then reset stays finite, while retained access pointing to a missing recovery still
propagates an integrity failure and releases the lease.
The terminal-read integrity suite proves public snapshot, receipt, and initial SSE authorization
precedes any targeted expiry mutation inside the same SQLite transaction; a stale outer access
result cannot close another session's recovery. It proves rollback-safe validation before stream
admission, explicit rollback of a newly sealed expiry when validation fails, corruption
detection on a post-admission SSE poll, generic foreign/absent 404 parity, and sanitized owner
failure for missing, duplicate, cross-record, cursor-hidden, or scenario-invalid terminal
evidence. It requires an exact seq-1 creation event plus contiguous, UTC, nondecreasing dynamic
event chronology bounded by the snapshot update. It also binds recovery, receipt, and final-event
provenance plus their UTC seal timestamp, and binds canonical replay creation, every replay
event, and the receipt to one atomic UTC chronology. Raw internal store readers intentionally
remain outside this public integrity fence for reconciliation and migration compatibility.
Active SDK/live hotel coverage deletes the pending envelope or consent record, alters a
digest-bound consent fact, drifts provenance, forges the `approval.requested` event, and injects
impossible execution evidence. Owner snapshot, receipt, and initial SSE reads all fail with the
sanitized integrity response before stream admission, while a foreign session retains
absent-recovery 404 parity. Targeted and background expiry skip corrupt active evidence rather
than laundering it into a terminal bundle, and terminal expiry reads revalidate the exact active
source. Valid unclaimed, approve/decline claimed, conservatively quarantined decline, committed
approve-result, and clock-advanced expiry flows remain readable or seal normally. Same-key
creation retries revalidate both active and expired hotel evidence before returning 201.
Expiry regressions also reject a coherently backdated terminal seal before consent expiry, bind
the pending-envelope update to the exact seal, and prove a corrupt oldest untouched candidate
cannot starve the next valid recovery across one-row keyset pages when the mutation batch is one.
The explicit-live suites prove zero live POSTs on initial mount and authorized restore,
synchronous one-POST activation coalescing under StrictMode, UUID-only storage with exception
safety, valid consent/receipt restoration, terminal invalid-hint clearing, truthful snapshotless
UI, and no stale resume/start continuation after unmount. Retry coverage distinguishes terminal
generic 404 and valid wrong-scenario snapshots from indeterminate network, non-404, malformed, and
miscorrelated responses. It proves that indeterminate state retains only the UUID, exposes fixed
copy and one coalesced same-GET retry, trusts no snapshot, performs no POST or automatic decision
resume, restores a valid claimed snapshot without resuming it, and clears on a later 404. StrictMode
may repeat the idempotent snapshot GET during its development effect probe; the gate is that it
accepts one current result and never turns that probe into recovery creation.
Snapshotless lifecycle coverage also distinguishes stable **Not started** from unresolved
**Awaiting server evidence**, with no current step in either phase, and proves a terminal failed
keyless lookup exposes explicit replay/SDK choices without an automatic replacement POST.

When a Docker-compatible engine is available, run:

```bash
docker build --pull -t backchannel:v0.3 .
npm run smoke:docker
```

Before the build, `uv run pytest tests/release/test_container_provenance.py -q` verifies that all
four external stages use the reviewed tag-plus-SHA-256 references and that the Python build and
runtime stages cannot drift apart. A post-push hosted Docker build remains the execution gate for
proving that those exact multi-platform index pins still resolve for the runner architecture; the
local contract alone does not make that claim.

The image smoke requires a disposable container, writable `/data`, explicit deployed Host, CORS,
and identity configuration, and no build-time API key. Because the image healthcheck targets
`127.0.0.1`, that address must be included alongside any public DNS name in
`BACKCHANNEL_ALLOWED_HOSTS`. A local production smoke is useful evidence for the packaged
application contract, but it is not container-runtime proof.

The GitHub-hosted `container-smoke` job in
[CI run 30602953030](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30602953030),
job `91069638145`, is verified packaged container evidence for exact source commit
`3c818e049bd8f5ad4c9f57da3e1b83e805d0bade`; both that job and verify job `91069313987`
completed with zero annotations. Verify passed 297 web tests, TypeScript/Vite, Ruff, strict
mypy, 978 Python tests, the deterministic stub and local one-process production smokes,
OpenAPI freshness, and the history secret scan. The container job completed the Docker build,
started the image, and waited for readiness. The packaged app then validated its canonical
bounded SHA-256 frontend manifest and served the built frontend assets, including the HTML title,
CSP headers, and emitted asset files; API calls separately exercised approval, decline,
authoritative receipts, and SSE resume. The smoke also
kept a masked secret canary out of responses and assets and verified the signed session boundary,
secure cookie, and cross-session isolation. This remains an API/static packaged proof in an
ephemeral GitHub Actions runtime, not a browser UI interaction.

The same job also performed a planned replacement of two distinct single-worker containers. A
and B ran sequentially with the same disposable named volume mounted at `/data` and the same
identity-signing secret. Only the owner's signed cookie was retained in client memory, and
container B accepted it under the same signing secret; a fresh foreign cookie remained isolated
through the generic 404 boundary. Container A created two pending `sdk_stub` recoveries for
approval and decline plus a terminal replay. Container B proved all three records persisted. Two
identical approval HTTP requests were accepted idempotently, while deterministic `sdk_stub` hotel
demo-adapter dispatch occurred once (`approvalCount=1`, `providerExecution=true`, and
`modelCall=false`). The decline request closed without action, and demo-adapter dispatch occurred
zero times (`approvalCount=0` and `providerExecution=false`). This deterministic demo-adapter
execution is not real provider execution. The replay receipt content matched exactly after JSON
decoding; replay SSE bytes matched exactly. Approval and decline then produced authoritative
receipts and terminal SSE in B.

The current replacement runner holds one authorized SSE while confirming the second connection
receives the exact finite capacity frame. `docker stop` must return inside a fixed five-second
budget and inspection must report exact process exit code `0`; signal exit `143` fails before a
replacement starts. Its `cleanExitWithActiveSse` marker therefore means only that PID 1 reached a
clean application exit while the SSE was open. The client closes its retained handle after the
stop and does not claim a server-delivered EOF or that every buffered client byte flushed.

This evidence does not prove local Docker; abrupt host loss or backup recovery; target-host
durability or target-host networking; concurrent multi-container SQLite; public deployment or
public reachability; or live OpenAI or real provider execution. Those remain independent gates.

## Browser and release artifacts

```bash
npm run test:e2e
npm run openapi:export
npm run openapi:check
npm run secret:scan
```

The capture command requires Google Chrome at its standard macOS application path or as
`google-chrome`/`google-chrome-stable` on Linux. It uses the exact repository-locked Playwright
CLI and repository config, disables user-global CLI configuration, builds the final frontend,
and starts `scripts/start.py` with a sanitized environment, temporary database, and demo reset
enabled. It drives `sdk_stub` rather than replay provenance. It records pending, completed, and
declined states at 1440×1024 and 390×844 while failing on browser console or page errors. The six
PNGs under `docs/assets/final` are runtime captures; files under `docs/design` are concepts and
never count as runtime evidence.

The locked Playwright CLI can report a `run-code` error while exiting zero. The capture therefore
starts on the same-origin `/health` route, clears and verifies empty session storage before each
demo reset, then uses a consumed invalid recovery hint to enter the explicit demo chooser without
an automatic replay. It freshly captures the CLI output and accepts only its exact result marker.
The browser script returns that marker only after writing all six PNGs and asserting that no
browser error was observed; the shell checks it before validating image dimensions, so an echoed
source fragment or missing result fails closed.

One capture invocation holds an atomic per-worktree lock before clearing shared audit logs or
building, keeps it through marker and image validation on successful runs, and releases it during
exit cleanup on success or failure; a concurrent or stale lock blocks without touching the current
run. Image validation reads every PNG chunk, verifies complete
boundaries and CRCs, requires IHDR and IDAT data plus one terminal IEND with no trailing bytes,
successfully decompresses the full IDAT stream, and then enforces the exact viewport dimensions.

`openapi:check` fails if `docs/openapi.json` differs from the current FastAPI schema.
`secret:scan` applies its configured rules to current release inputs and HEAD-ancestry file blobs,
including deleted historical paths. Shallow or otherwise incomplete Git history makes the
scan fail closed, as does exceeding its finite path or byte ceilings. This complements, but
cannot replace, runtime secret management.

## Live OpenAI gate

Run the external proof only with a key supplied to the command process:

```bash
npm run smoke:live
npm run smoke:live:3
```

A passing record is limited to status, elapsed milliseconds, model IDs, ordered tool names,
approval count, and root trace ID. Model and tool payload tracing is disabled. Exit code 0 means
verified live completion, 1 means a local contract failure, and 2 means an external blocker such
as a missing key, authentication, access, quota, or connectivity.

Current status on 2026-07-19: **BLOCKED**. `OPENAI_API_KEY` was absent, so no real OpenAI
request was attempted and the required three consecutive live completions remain unverified.
See `docs/live-validation.md` for the redacted records. SDK-stub and replay evidence must not be
substituted for this gate.

## External proof boundary

The local container runtime remains unavailable: Docker, Podman, Colima, and OrbStack are absent
from this workstation, so reproducing the image build and smoke locally is unverified. The
GitHub-hosted packaged container result above does not erase that local boundary. There is also
no deployed public demo URL, so deployment and public reachability remain unverified. Repository,
local-process, and GitHub Actions results do not prove live OpenAI access or a target-host release.
