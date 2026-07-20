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
canary-secret non-disclosure rule. It also verifies the deployed session cookie remains Secure,
HttpOnly, SameSite=Lax, and bounded by Max-Age. Because the local production lane models TLS
termination over loopback HTTP, only that smoke client manually carries the observed Secure
cookie across the loopback hop; the application never weakens the emitted cookie flags.

The focused security regression is:

```bash
uv run pytest -q tests/security/test_session_isolation.py
uv run pytest -q tests/domain/test_pending_expiry.py tests/api/test_expiry_lifecycle.py
npm --workspace web exec -- vitest run src/components/EvidenceInspector.test.tsx
npm --workspace web exec -- vitest run src/App.explicitLive.test.tsx src/recoverySession.test.ts
```

It proves foreign snapshot/SSE/receipt/approve/decline all match an absent recovery's generic
404, owner approval remains at-most-once under a foreign race, the signed cookie survives a
same-secret restart, tampered cookies fail closed, shared replay access detaches per session,
reset preserves cooldown/budget history, and retention cleanup cascades access rows.
The expiry suites prove SDK/live zero-dispatch receipts, live-admission release with cooldown and
usage preservation, restart idempotence, decision-versus-expiry writer serialization,
access-check ordering, synchronous snapshot/SSE/receipt/decision truth, bounded UTC maintenance,
stable `remedy_expired` retries, and fake-timer controls that refresh once without posting a
stale decision or continuing after unmount.
The explicit-live suites prove zero live POSTs on initial mount and authorized restore,
synchronous one-POST activation coalescing under StrictMode, UUID-only storage with exception
safety, valid consent/receipt restoration, invalid-hint clearing, truthful snapshotless UI, and
no stale resume/start continuation after unmount. StrictMode may repeat the idempotent snapshot
GET during its development effect probe; the gate is that it accepts one current result and
never turns that probe into recovery creation.
Snapshotless lifecycle coverage also distinguishes stable **Not started** from unresolved
**Awaiting server evidence**, with no current step in either phase, and proves a failed keyless
resume exposes explicit replay/SDK choices without an automatic replacement POST.

When a Docker-compatible engine is available, run:

```bash
docker build -t backchannel:v0.3 .
npm run smoke:docker
```

The image smoke requires a disposable container, writable `/data`, explicit deployed CORS and
identity configuration, and no build-time API key. A local production smoke is useful evidence
for the packaged application contract, but it is not container-runtime proof.

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

`openapi:check` fails if `docs/openapi.json` differs from the current FastAPI schema.
`secret:scan` checks tracked source, built assets, capture artifacts, and release inputs for
secret-shaped content; it complements, but cannot replace, proper runtime secret management.

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

In the current environment Docker, Podman, Colima, and OrbStack are absent, so neither a Docker
build nor a running-container smoke has been verified. There is also no deployed public demo URL.
The repository and deterministic local tests can be verified independently, but those results do
not prove live OpenAI access, a container runtime, a deployment, or public reachability.
