# Backchannel

Backchannel turns a failed agent action into a provable recovery without letting a model silently expand its authority.

**Detect → Prove → Negotiate → Authorize → Execute → Verify & seal**

## Run the demo

Prerequisites: Node 22, Python 3.12, Google Chrome, `npm`, and `uv`. The
final-evidence capture supports Chrome at its standard macOS application path or as
`google-chrome`/`google-chrome-stable` on Linux.

```bash
npm install
uv sync --all-groups
npm run build
npm run start
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). Without an
`OPENAI_API_KEY`, the hotel scenario opens as a replay fixture. Select **Run SDK QA
trace** to exercise the real Agents SDK pause/resume path deterministically, or run:

```bash
npm run smoke:stub
```

The keyless replay contract can also be started directly while the app is running:

```bash
curl -sS http://127.0.0.1:8000/api/recoveries \
  --cookie-jar /tmp/backchannel-demo.cookies \
  --cookie /tmp/backchannel-demo.cookies \
  -H 'Content-Type: application/json' \
  -d '{"scenarioId":"hotel","executionMode":"replay_fixture","clientRequestId":"readme-replay-001"}'
```

Use a fresh `clientRequestId` for each new start intent. Reuse the same value only when
retrying that exact request after a timeout or lost response, with the same cookie jar and
unchanged server identity secret. Remove `/tmp/backchannel-demo.cookies` when finished.
A lost or expired cookie creates a new idempotency scope.

For a live-ready local process, supply `OPENAI_API_KEY` only to the server environment,
restart `npm run start`, and select **Start live recovery** in the hotel scenario. A page load
never starts a billed live run. The redacted command-line gate is:

```bash
npm run smoke:live
```

All three modes stop at the same trust boundary: every provider is a **demo adapter**.
`openai_live` makes real OpenAI model calls, but no mode changes a real hotel booking,
payment, or API quota. `sdk_stub` makes no OpenAI call; `replay_fixture` makes neither a
model call nor a provider dispatch.

## What the console proves

- The server, not the browser, owns recovery state, ordered events, decisions, and receipts.
- Recovery detail is bound to the signed HttpOnly demo session that created or explicitly
  started it. Foreign, expired, missing, and tampered sessions receive the same generic 404.
- Same-tab `sessionStorage` may hold the last validated hotel recovery UUID and, while a
  creation request is unresolved, the pending tuple containing its raw UUID
  `clientRequestId`, scenario, and execution mode. The pending tuple is cleared only after a
  matching server snapshot is accepted or the user explicitly abandons the conflicting start.
  It contains no cookie, keyed session correlation, serialized run state, approval or decision
  payload, or provenance claim. The server never persists, logs, or responds with the raw
  token; it stores only its session-scoped HMAC.
- Reload first asks the session-authorized snapshot endpoint to restore a saved hotel run. A
  malformed local hint, generic authorized 404, or valid correlated wrong-scenario snapshot is
  terminal and clears it. Network, non-404, malformed-response, and
  miscorrelated-response failures retain the UUID and expose only a coalesced **Retry saved
  recovery** GET; they trust no snapshot and never cause a replacement run or automatic
  decision resume.
- When demo reset is enabled, it refuses with `409 reset_creation_pending` and makes no
  mutation while the signed session has a reserved, started, or unknown creation claim. Reset
  can be retried after the owner resolves the claim or the signed session expires.
- New creation intents are atomically bounded to 32 unexpired ledger rows per signed session
  and 2,048 globally by default. Operators can configure those bounded values with
  `BACKCHANNEL_MAX_RECOVERY_CREATIONS_PER_SESSION` and
  `BACKCHANNEL_MAX_RECOVERY_CREATIONS_GLOBAL` (the session limit cannot exceed the global
  limit). Every unexpired reserved, started, ready, or unknown claim counts. At capacity, only
  a new key receives exact `429 creation_capacity`; an existing key still reaches its normal
  retry result, with no extra orchestration, admission, or budget work. The same-tab UI keeps
  that intent for an explicit same-key retry.
- Hotel execution pauses at the Agents SDK `commit_remedy` interruption. Consent displays
  exact terms, a UTC expiry, and a canonical `sha256:` digest.
- Approval rechecks the digest, interruption, expiry, hard constraints, and delegated
  authority immediately before the demo adapter dispatch. Decline closes with zero dispatch.
- If an SDK/live hotel consent window ends before any decision claim, the server atomically
  closes it without action, releases any live lease without erasing cooldown or usage, and
  seals a zero-decision, zero-execution expiration receipt. The browser disables stale controls
  at the displayed deadline and refreshes server evidence instead of inventing the outcome.
- Compatible pending approvals survive a process restart; version or definition drift fails
  closed with `resume_incompatible`.
- An unfinished durable decision claim can be continued before its authoritative expiry without
  storing its request in the browser. The authorized snapshot exposes only the claimed action,
  digest, and expiry; **Resume exact approval/decline** sends an explicit empty request, and the
  server reloads and revalidates its immutable claim. Mount and reload never submit it
  automatically.
- At expiry, an unfinished approve claim seals conservative `outcome_unknown` evidence; a
  zero-work decline can seal `closed_without_action`, while an ambiguous decline also becomes
  `outcome_unknown`. The sole exact provider result already committed before expiry is finalized
  as `completed` without another dispatch. Later owner decision/resume attempts return stable
  `422 remedy_expired` after refreshing that authoritative evidence.
- SSE polling is fail-fast bounded to 16 streams per server process and four per recovery by
  default. At capacity, the server returns a finite `stream.capacity` control event with a
  five-second native retry interval; authorized streams retain durable replay, heartbeat, and
  terminal semantics. These counters are process-local, not a cross-worker or cross-container
  concurrency claim, and the packaged launcher deliberately runs one worker.
- An authorized SSE `Last-Event-ID` may occur at most once and accepts ASCII digits only in the
  SQLite-safe range `0..9223372036854775807` (leading zeroes are allowed). Invalid or duplicate
  cursors return a stable JSON `400` before expiry changes, stream admission, or durable-ledger
  polling; foreign and absent recoveries still receive the same generic `404` before cursor
  parsing.
- Decision resume is signed-session scoped and preserves the demo adapter's at-most-one dispatch
  boundary. It is not an original-tab capability or an at-most-one live-model-run claim.
- API quota recovery is the complementary zero-approval case: deterministic delegated
  authority, simulated execution verification, permission revocation, and a sealed receipt.

## Validation and release evidence

```bash
npm run check
npm run smoke:stub
npm run smoke:production
npm run test:e2e
npm run secret:scan
npm run openapi:check
```

See [architecture](docs/architecture.md), [protocol](docs/protocol.md),
[validation](docs/validation.md), [live validation](docs/live-validation.md), and the
[judge checklist](docs/judge-checklist.md). The public API schema is checked in at
[`docs/openapi.json`](docs/openapi.json).
