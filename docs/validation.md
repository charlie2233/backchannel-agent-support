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
```

It proves foreign snapshot/SSE/receipt/approve/decline all match an absent recovery's generic
404, owner approval remains at-most-once under a foreign race, the signed cookie survives a
same-secret restart, tampered cookies fail closed, shared replay access detaches per session,
reset preserves cooldown/budget history, and retention cleanup cascades access rows.

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
