# Recovery protocol

Every recovery follows one ordered protocol:

**Detect → Prove → Negotiate → Authorize → Execute → Verify & seal**

`currentStep` is zero-based in the API. The UI renders all six steps and identifies
which transitions are authoritative runtime events versus replay evidence.

## Consent digest and exact decision

The pending approval includes canonical remedy terms and these binding fields:

- `remedyId`: the one pending remedy record;
- `remedyDigest`: `sha256:` plus the canonical digest of scenario, recovery,
  remedy ID, action, replacement, stay, currency, cost, changed fields, provider
  commitments, expiry, constraints, authority result, and interruption identity;
- `toolCallId`: the exact paused `commit_remedy` interruption;
- expiry and `executionStarted=false`.

The browser submits only this exact decision payload:

```json
{
  "decision": "approve | decline",
  "clientDecisionId": "stable retry identifier",
  "remedyId": "pending remedy identifier",
  "remedyDigest": "sha256:<64 lowercase hex>",
  "toolCallId": "paused interruption identifier"
}
```

`clientDecisionId` makes a same-action retry stable. Reusing it with different terms
or the opposite action is rejected. Authorization never comes from a UI-only state.

## Durable claim and resume

The store obtains a durable claim before SDK resume. The claim records the action,
decision ID, remedy/tool identity, version, owner token, lease, and attempt state in
the same SQLite transaction. A valid same-action retry may reclaim an expired lease;
an active owner cannot be replaced. Resume verifies the pinned SDK, protocol, agent
graph, and definition digest before deserializing server-only state.

Approval has three meaningful outcomes:

- `completed`: approval matched, one idempotent demo-provider dispatch completed,
  and the receipt sealed the result;
- `closed_without_action`: decline rejected the exact interruption, provider dispatch
  never began, and the permission scope was revoked;
- `outcome_unknown`: dispatch may have begun but a terminal provider result cannot be
  proved. The system must not relabel this state as a safe decline.

Provider adapters require an idempotency key and persist one result per key. The
receipt distinguishes `providerDispatchStarted`, `providerExecution`, and
`executionCount`; these are not inferred from HTTP success.

## Events and reconnection

Events have a monotonically increasing per-recovery sequence. SSE uses that sequence
as `id`, replays persisted rows after `Last-Event-ID`, then fans out new rows with a
bounded process-local subscriber. A reconnect is session-authorized before any event
is returned. After that ownership check and before HTTP `200`, process-local admission
enforces global, signed-session, and recovery caps; saturation returns the generic
`stream_capacity_reached` public `429` with a one-second retry hint. The defaults are
32 global, 4 per session, and 2 per recovery, configurable through the documented
`BACKCHANNEL_SSE_MAX_*` environment values. Heartbeats do not advance the durable
cursor.

## Public creation admission

`POST /api/recoveries` validates the complete supported scenario/mode request before
charging one durable creation admission. Malformed or extra fields, unsupported
scenario/mode combinations, deployed `sdk_stub`, and keyless `openai_live` requests
do not charge. A supported request is charged once before replay/SDK orchestration
and, for live mode, before the process-local live gate and separate live-only ledger.
The charge is retained if a later capacity, cooldown, upstream, or internal failure
occurs.

The independent session, IP, and global counters use UTC calendar days. Exhaustion
returns HTTP `429`, code `creation_daily_budget_exceeded`, the generic message
`The public demo recovery creation budget is exhausted for today.`, and matching
`Retry-After` / `retryAfterSeconds` seconds until the next UTC midnight. The response
does not reveal which counter decided the rejection, has no replay fallback, and
does not issue a provisional session cookie.

## Endpoint map

The generated, checked contract is [OpenAPI](openapi.json).

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Keyless capability booleans and demo-provider boundary |
| `GET` | `/readyz` | SQLite and static-bundle readiness |
| `GET` | `/api/scenarios` | Public scenario catalog |
| `POST` | `/api/recoveries` | Start an explicit mode/scenario recovery |
| `GET` | `/api/recoveries/{recoveryId}` | Session-owned snapshot |
| `POST` | `/api/recoveries/{recoveryId}/decisions` | Exact approval or decline |
| `GET` | `/api/recoveries/{recoveryId}/events` | Session-owned SSE with `Last-Event-ID` |
| `GET` | `/api/recoveries/{recoveryId}/receipt` | Session-owned terminal receipt |
| `POST` | `/api/demo/reset` | Local-QA session reset when explicitly enabled |

All public failures use a bounded error envelope. Validation errors do not echo
submitted bodies, credentials, serialized state, or internal exceptions.
