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
  |-- SQLite: recoveries, approvals, decisions, executions, events, receipts
  |-- OpenAI Agents SDK: openai_live or deterministic sdk_stub
  `-- simulated hotel/quota demo adapters
```

## Server-authoritative flow

The browser validates the public response shape but never invents provenance or terminal
state. A created recovery returns a typed snapshot. Its `recoveryId` selects one persisted,
ordered event stream; native `EventSource` reconnects with `Last-Event-ID`, and the server
replays later rows before waiting for new commits. Terminal events close the stream, after
which the UI fetches the authoritative snapshot and receipt.

Event delivery uses a lock-protected, fail-fast admission controller. The defaults admit at
most 16 polling loops in this application process and at most four for one recovery. Session
authorization, targeted consent expiry, `Last-Event-ID` validation, and recovery existence are
checked before admission, so saturation changes neither the generic foreign/unknown 404 nor an
authorized invalid-cursor 400. A rejected connection receives one finite `stream.capacity`
control event with a five-second native retry field and then closes; it never enters the polling
loop. The browser validates that exact control envelope, presents its message as transient, and
leaves native `EventSource` reconnection active. A later durable recovery event clears the
message, while a terminal event still closes the source.

A live-ready page load is deliberately idle. **Start live recovery** is the only UI action that
creates an `openai_live` hotel run, and an in-flight guard coalesces rapid activation before
React can re-render the disabled control. After any validated hotel snapshot, the browser may
store only its canonical UUID in same-tab `sessionStorage`; the signed HttpOnly cookie remains
the access credential. Reload performs the authorized snapshot GET before any fallback or new
start. A valid snapshot restores its SSE, consent, and receipt lifecycle. Malformed, stale,
foreign-session, expired-session, and retention-deleted hints are cleared and leave the UI in a
truthful no-run state. If live mode is unavailable, that state offers explicit replay and SDK QA
actions without automatically creating a replacement. While a resume or explicit start is
unresolved, the lifecycle remains neutral at **Awaiting server evidence** with no current step or
execution-mode claim. React StrictMode can probe the idempotent GET effect twice in development,
but it does not create a recovery; live creation remains explicitly initiated and one-POST.

SQLite persists the pending Agents SDK state envelope, consent evidence, decision claim,
provider execution record, event ledger, receipt, and opaque recovery-access association. A
pending approval carries the Agents
SDK, protocol, agent-graph, and definition versions that must still match on resume. Startup
reconciliation can seal a receipt from an already committed demo-adapter result without
dispatching it again.

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
demo-adapter idempotency boundary, but they do not prove one live model run across processes. A
claim that remains unfinished at expiry fails closed; the server does not synthesize cancellation
while another request or worker might still be resolving it.

Untouched SDK/live hotel consent is also durable lifecycle state. A bounded SQLite
`BEGIN IMMEDIATE` sweep runs at startup, on the existing maintenance cadence, and before new
recovery cleanup. Authorized snapshot, initial SSE, receipt, and decision requests first sweep
their exact target after the session-access check. Expiry and decision claims therefore
serialize as competing writers: a committed claim is never expired, while an expiration that
commits first prevents a later claim. Expiration seals one `recovery.expired` event and receipt,
marks the pending envelope and remedy expired, and releases the matching live admission with
`COALESCE` so cooldown and aggregate usage evidence remain intact.

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

`npm run build` produces `web/dist`. `npm run start` launches one Uvicorn worker through
`scripts/start.py`; local mode binds to `127.0.0.1`, while explicit deployed mode binds to
`0.0.0.0`. FastAPI mounts hashed `/assets` and uses a restricted route-like SPA fallback;
API, documentation, missing asset, and suspicious paths remain non-HTML 404s.

The multi-stage Dockerfile builds Node assets separately, installs the frozen Python runtime,
runs as UID/GID 10001, stores SQLite under writable `/data`, and checks `/readyz`.
Readiness probes both the required SQLite schema and the built index. HTML receives a
self-only script/style/image/connect CSP; API and non-HTML responses receive
`default-src 'none'`.

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

The server applies request-size limits, exact CORS configuration, generic public errors,
redacted server logging, per-IP and per-session live cooldowns, a live concurrency cap, a
configurable daily admission budget, terminal-record TTL cleanup, and an HttpOnly demo-session
cookie. Long-lived event streams add configurable process and per-recovery caps plus a bounded
retry interval. Admission leases are released idempotently after terminal completion, client
disconnect, cancellation, iterator/store/encoding failure, ASGI send failure, or response
construction failure; zero-count recovery entries are removed. Deployed mode additionally
requires an exact HTTPS origin allowlist and an explicit
32-byte-or-longer identity-hash secret. Proxy headers are ignored unless the direct peer is in
an explicit trusted CIDR allowlist.

The cookie contains a random nonce, expiry, and HMAC signature. SQLite never stores that raw
cookie, nonce, client address, API key, or authorization value. Instead, creation atomically
associates the recovery with a keyed 64-character session correlation value. Snapshot, SSE,
receipt, and decision routes check that association before any state read, lease renewal,
decision claim, or provider dispatch. Unknown, missing, expired, tampered, and unrelated
sessions all receive the same generic 404. Canonical replay rows may be associated with more
than one session only after each session explicitly starts that replay scenario.

The reset endpoint is disabled by default and is intended only for disposable capture runs.
When enabled, it detaches only the caller's associations, deletes recovery detail only when no
other session retains access, and marks the caller's live admissions released. Cooldown history
and the global usage ledger remain intact, so reset cannot restore live budget or bypass an IP
or session cooldown. Retention cleanup cascades access rows with expired recovery detail while
preserving aggregate usage. Secrets remain runtime inputs: they are not copied into the
frontend build or container image.
