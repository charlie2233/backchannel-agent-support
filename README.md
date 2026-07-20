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
restart `npm run start`, and use the hotel scenario. The redacted command-line gate is:

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
