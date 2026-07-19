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

SQLite persists the pending Agents SDK state envelope, consent evidence, decision claim,
provider execution record, event ledger, and receipt. A pending approval carries the Agents
SDK, protocol, agent-graph, and definition versions that must still match on resume. Startup
reconciliation can seal a receipt from an already committed demo-adapter result without
dispatching it again.

## Runtime provenance and trust boundary

| Mode | Runtime work | Trace evidence | Provider boundary |
| --- | --- | --- | --- |
| `openai_live` | Real OpenAI model calls through the Agents SDK | `trace_...`, recorded model IDs, version and definition digest | Simulated hotel demo adapter only; no real booking or payment |
| `sdk_stub` | Actual Agents SDK runner with deterministic local model responses | `qa_trace_...`, no model IDs, version and definition digest | Simulated hotel or quota demo adapter only |
| `replay_fixture` | Persisted playback of a bundled protocol trace | No root trace, model IDs, SDK version, or definition digest | No model call and no provider execution |

`/health` exposes only `backend`, `liveReady`, and `providerBoundary=demo_adapter_only`;
it never exposes the API key. A live UI label is allowed only when health is live-ready and
the active snapshot or receipt independently records `executionMode=openai_live`.

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

## Public-demo controls

The server applies request-size limits, exact CORS configuration, generic public errors,
redacted server logging, per-IP and per-session live cooldowns, a live concurrency cap, a
configurable daily admission budget, terminal-record TTL cleanup, and an HttpOnly demo-session
cookie. Deployed mode additionally requires an exact HTTPS origin allowlist and an explicit
32-byte-or-longer identity-hash secret. Proxy headers are ignored unless the direct peer is in
an explicit trusted CIDR allowlist.

The reset endpoint is disabled by default and is intended only for disposable capture runs.
Secrets remain runtime inputs: they are not copied into the frontend build or container image.
