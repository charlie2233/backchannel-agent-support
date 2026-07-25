# Architecture and deployment boundary

Backchannel is a typed React/Vite console served by a FastAPI application. FastAPI
persists recovery state, consent claims, ordered events, receipts, and usage controls
in SQLite. The browser receives snapshots over JSON and live updates over
server-sent events (SSE); SQLite remains authoritative when a stream reconnects.

## Ownership and trust

A signed demo-session cookie establishes signed session ownership. The server stores
only a hash of that identity with each recovery and returns a foreign recovery as
`404`, including decision, receipt, and SSE routes. The browser never receives the
signing secret or serialized SDK resume state. That server-only serialized SDK state
contains the paused Agents SDK state plus protocol, graph, SDK, and definition-version
markers; all markers are checked before resume.

Consent is one-use and remedy-bound. The server compares the exact remedy, canonical
digest, interruption tool call, session, expiry, and durable decision claim before a
provider dispatch. A terminal receipt closes and revokes that permission scope.

## Three execution modes

| Mode | Orchestration | Provider boundary |
| --- | --- | --- |
| `openai_live` | OpenAI Agents SDK models, when a server-side key is configured | Local `HotelSimulator`; no real provider mutation |
| `sdk_stub` | Deterministic Agents SDK model and the same pause/resume path | Local hotel/quota simulators |
| `replay_fixture` | Persisted recorded events, no model runtime | No runtime provider dispatch |

The API and UI derive provenance from the explicit mode and server health. Stub and
replay states cannot claim returned model identifiers.

Live model work has two aligned timeout layers. `AsyncOpenAI` uses the configured
`BACKCHANNEL_LIVE_OPERATION_TIMEOUT_SECONDS` value as its per-attempt transport
timeout with SDK retries disabled. Separately, the orchestrator applies one shared
application wall-clock deadline to the full three-Agent pre-approval graph and one
deadline to each live approval/decline resume. The setting defaults to 60 seconds and
is restricted to 1..300 seconds. This deadline is cooperative cancellation inside the
Python process, not hard process termination; a cancellation-resistant dependency
would still require process supervision to enforce a hard stop.

## Process and container topology

Production-compatible startup uses **one process** and one worker. Process-local live
admission and bounded SSE fanout therefore remain authoritative for that topology. The
packaged image runs as non-root UID/GID `10001:10001`, serves the built React bundle,
and stores its SQLite database below the mounted `/data` volume.

The hosted CI container job proves image build, non-root identity, and two offline
packaged smoke profiles. It does not prove a long-lived volume, abrupt host-loss,
backup restore, target-host networking, or concurrent multi-container SQLite writes.
SQLite is deliberately a single-writer/single-process deployment choice here;
concurrent multi-container SQLite operation is unsupported and unverified. A scaled
deployment would require a shared transactional database plus distributed admission
and event fanout.

Every supported public `POST /api/recoveries` is admitted through a separate durable
SQLite creation ledger before replay or SDK orchestration and before the live-only
gate and ledger. Strict body/schema, scenario/mode support, deployed SDK availability,
and keyless live availability are checked first and do not consume creation budget.
An admitted attempt remains charged if a later capacity, cooldown, upstream, or
timeout, or internal step fails; there is intentionally no refund state machine.
Session, IP, and global UTC-day counters are incremented atomically, contain only HMAC
identities plus one fixed global key, and are never mixed with the live-only usage
ledger.

The creation defaults are 12 per signed session, 60 per IP, and 120 globally per UTC
day. Any value may be zero as an operator kill switch. Rows are retained for eight
days by the bounded cleanup service; demo reset, recovery deletion, and process
restart do not erase the current counters.

## Long-lived single-host container recipe

This is an operator recipe, not deployment evidence. Build the exact source tree,
create a persistent `/data` volume, prepare a root-readable environment file, and run
the packaged non-root image:

```bash
sudo docker build --tag backchannel:v0.3 .
sudo docker volume create backchannel-data
sudo install -d -m 0700 /etc/backchannel
sudoedit /etc/backchannel/runtime.env
sudo docker run --detach --name backchannel --restart unless-stopped \
  --publish 127.0.0.1:8000:8000 \
  --mount type=volume,src=backchannel-data,dst=/data \
  --env-file /etc/backchannel/runtime.env \
  backchannel:v0.3
```

Generate the signing value with a cryptographically secure secret manager; do not
store it in Git. The environment file must contain an explicit signing secret and one
exact HTTPS browser origin:

```dotenv
BACKCHANNEL_IDENTITY_HMAC_SECRET=<random-value-from-secret-manager>
BACKCHANNEL_CORS_ORIGINS=https://demo.example.com
BACKCHANNEL_DEPLOYED=true
```

The image command starts `scripts/start.py`, which enforces one process and one worker.
Do not add a second replica while SQLite and process-local admission/SSE fanout remain
the deployed architecture.

Terminate HTTPS at an SSE-capable reverse proxy. Preserve streaming, disable response
buffering/cache, and keep the read timeout longer than the 15-second heartbeat:

```nginx
server {
    listen 443 ssl;
    server_name demo.example.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 75s;
        proxy_send_timeout 75s;
    }
}
```

The host still needs managed TLS certificates, firewall policy, health monitoring,
volume backup/restore, log retention, and a verified public smoke. Public deployment remains unverified.

## Component flow

1. React loads `/health`, creates or restores a session-owned recovery, and subscribes
   to its SSE ledger.
2. FastAPI validates public input, atomically admits the supported creation, and then
   delegates transitions to the orchestrator and SQLite store.
3. The orchestrator pauses the SDK run at `commit_remedy`; the store records the
   exact interruption and digest.
4. The decision route durably claims one exact action before resuming the SDK state.
5. The idempotent simulator records at most one result for its idempotency key.
6. Snapshot, ordered events, and the terminal receipt are committed and rendered.

See [protocol.md](protocol.md) for endpoint semantics and
[validation.md](validation.md) for proof-lane status.
