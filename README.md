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
  -H 'Content-Type: application/json' \
  -d '{"scenarioId":"hotel","executionMode":"replay_fixture"}'
```

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
- The browser keeps only the last validated hotel recovery UUID in `sessionStorage`. Reload
  first asks the session-authorized snapshot endpoint to restore that run. A malformed local
  hint, generic authorized 404, or valid correlated wrong-scenario snapshot is terminal and
  clears it. Network, non-404, malformed-response, and miscorrelated-response failures retain
  the UUID and expose only a coalesced **Retry saved recovery** GET; they trust no snapshot and
  never cause a replacement run or automatic decision resume.
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
- An unfinished durable decision claim can be continued after reload without storing its request
  in the browser. The authorized snapshot exposes only the claimed action, digest, and expiry;
  **Resume exact approval/decline** sends an explicit empty request, and the server reloads and
  revalidates its immutable claim. Mount and reload never submit it automatically.
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
  boundary. It is not an original-tab capability or an at-most-one live-model-run claim, and an
  expired unfinished claim still fails closed without inventing a terminal outcome.
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
