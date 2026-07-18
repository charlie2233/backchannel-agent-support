# Backchannel v0.3 implementation plan

> **For Codex:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` to execute this plan task by task. For every task, use a fresh implementer, then a fresh specification reviewer, then a fresh code-quality reviewer. Follow test-driven development: add the failing test, observe the expected failure, implement the minimum behavior, rerun the focused test, then run the broader gate before committing.

**Goal:** Deliver a judge-ready Backchannel v0.3 operational demo whose runtime provenance is truthful, consent is bound to exact remedy terms, pending approvals survive restart, rejection executes nothing, and live mode uses one trace while deterministic replay remains available without a key.

**Architecture:** Build a typed React/Vite console and a FastAPI/SQLite server in one repository. The server owns all recovery state, persists an ordered event ledger and serialized Agents SDK run state, exposes snapshots plus SSE, and serves the built web assets in production. A deterministic SDK model exercises the same approval/resume path as live mode. Provider adapters are deliberately simulated and idempotent; receipts state that boundary explicitly.

**Tech stack:** Node 22, npm workspaces, React, TypeScript, Vite, Vitest, Testing Library, Playwright, Python 3.12, uv, FastAPI, Pydantic v2, OpenAI Agents SDK, SQLite, pytest, httpx, Ruff, mypy, Docker.

**Proof lanes:** Local unit/integration/browser proof, OpenAI live smoke proof, container proof, GitHub publication, and public deployment are separate gates. A passing earlier lane must never be reported as proof of a later one.

---

## Task 1: Bootstrap the typed monorepo and truthful replay slice

**Files:**

- Create: `.gitignore`, `.dockerignore`, `.env.example`, `package.json`, `pyproject.toml`
- Create: `web/package.json`, `web/index.html`, `web/tsconfig.json`, `web/vite.config.ts`, `web/src/main.tsx`, `web/src/App.tsx`, `web/src/styles.css`
- Create: `web/src/domain/runtime.ts`, `web/src/domain/recovery.ts`, `web/src/api/client.ts`, `web/src/fixtures/recoveries.ts`
- Create: `web/src/components/ProvenanceStrip.tsx`, `web/src/components/ScenarioRail.tsx`, `web/src/components/Lifecycle.tsx`, `web/src/components/EvidenceInspector.tsx`
- Test: `web/src/domain/runtime.test.ts`, `web/src/App.test.tsx`
- Create: `server/__init__.py`, `server/main.py`, `server/config.py`, `server/models.py`
- Test: `tests/api/test_health.py`

**Step 1: Write failing provenance tests**

Define a table-driven test in `web/src/domain/runtime.test.ts` that calls `deriveRuntimePresentation(health, snapshot)` for all combinations of `openai_live`, `sdk_stub`, and `replay_fixture`. Assert that `showGpt56Agents` is true only for `{backend: "openai", liveReady: true}` plus a snapshot whose `executionMode` is `openai_live`. Assert the exact labels `OpenAI live`, `SDK stub`, and `Replay fixture` and the exact boundary copy in `docs/design/README.md`.

In `tests/api/test_health.py`, request `/health` with no key and assert:

```python
assert body["backend"] == "stub"
assert body["liveReady"] is False
assert body["providerBoundary"] == "demo_adapter_only"
assert "key" not in json.dumps(body).lower()
```

**Step 2: Run tests and verify the expected failures**

Run `npm --workspace web test -- --run src/domain/runtime.test.ts` and `uv run pytest tests/api/test_health.py -q`. The frontend test must fail because the module does not exist; the API test must fail because the app does not exist.

**Step 3: Implement the smallest truthful slice**

Create discriminated TypeScript unions and matching Pydantic enums for execution mode. `deriveRuntimePresentation` must use this explicit condition:

```ts
const showGpt56Agents =
  health.backend === "openai" &&
  health.liveReady === true &&
  snapshot.executionMode === "openai_live";
```

Render the two approved scenarios and the six-step lifecycle using the replay fixtures. Implement `/health`, `/readyz`, and static development CORS configuration. Never read or return an API-key value; `liveReady` is only the boolean result of non-empty server-side configuration.

**Step 4: Pass focused and baseline gates**

Run:

```bash
npm install
uv sync --all-groups
npm --workspace web test -- --run src/domain/runtime.test.ts src/App.test.tsx
uv run pytest tests/api/test_health.py -q
npm run check
```

Expected: all pass; the application renders exactly two scenarios and no replay/stub state contains “GPT-5.6 agents.”

**Step 5: Commit**

Commit as `feat: bootstrap truthful Backchannel console`.

## Task 2: Persist recoveries, events, and receipts in SQLite

**Files:**

- Create: `server/store.py`, `server/events.py`, `server/replay/engine.py`, `server/replay/loader.py`
- Create: `server/scenarios/hotel.json`, `server/scenarios/api-quota.json`
- Modify: `server/main.py`, `server/models.py`
- Create: `web/src/api/events.ts`, `web/src/hooks/useRecovery.ts`
- Test: `tests/domain/test_store.py`, `tests/api/test_recoveries.py`, `web/src/hooks/useRecovery.test.tsx`

**Step 1: Write failing persistence and SSE tests**

Test that a recovery created from `hotel` has a UUID, monotonically increasing integer event sequence, and survives closing and reopening the store. Test that `Last-Event-ID: 2` only streams persisted events with `seq > 2`. Test that the frontend reducer ignores duplicate sequence IDs and preserves server order.

**Step 2: Observe failures**

Run `uv run pytest tests/domain/test_store.py tests/api/test_recoveries.py -q` and `npm --workspace web test -- --run src/hooks/useRecovery.test.tsx`. Expect import/route failures.

**Step 3: Implement durable event sourcing**

Create SQLite tables `recoveries`, `remedies`, `pending_approvals`, `executions`, `events`, `receipts`, `usage_ledger`, and `demo_sessions`. Give `events` a unique `(recovery_id, seq)` constraint. All state transitions and their event append occur in one transaction. Expose:

- `GET /api/scenarios`
- `POST /api/recoveries`
- `GET /api/recoveries/{recovery_id}`
- `GET /api/recoveries/{recovery_id}/events`
- `GET /api/recoveries/{recovery_id}/receipt`
- `POST /api/demo/reset`

SSE uses `id: <seq>`, a 15-second comment heartbeat, `Cache-Control: no-cache`, and `X-Accel-Buffering: no`. Replay persisted events before subscribing for new events.

**Step 4: Pass focused tests and full check**

Run the focused tests, then `npm run check` and `npm run smoke:stub`. Assert a newly created replay recovery ends with a visibly simulated receipt and has no provider-execution claim.

**Step 5: Commit**

Commit as `feat: persist replay recoveries and event streams`.

## Task 3: Exercise Agents SDK approval with a deterministic model

**Files:**

- Create: `server/agents/factory.py`, `server/agents/schemas.py`, `server/agents/stub_model.py`, `server/agents/tracing.py`
- Create: `server/orchestrator.py`, `server/providers/hotel_simulator.py`
- Modify: `server/models.py`, `server/main.py`, `server/store.py`
- Test: `tests/integration/test_stub_approval.py`, `tests/domain/test_provider_idempotency.py`

**Step 1: Write a failing pause/resume test**

Start an `sdk_stub` hotel recovery and assert it reaches `pending_approval` with one Agents SDK interruption whose tool name is `commit_remedy`. Assert the simulated provider has zero dispatches. Approve the interruption through the SDK state and assert exactly one idempotent dispatch and a completed receipt.

**Step 2: Observe the failure**

Run `uv run pytest tests/integration/test_stub_approval.py tests/domain/test_provider_idempotency.py -q`. Expect missing orchestrator/model failures.

**Step 3: Implement the shared SDK path**

Implement the deterministic test model against the Agents SDK `Model` protocol. It must produce typed consumer proof, provider proof, broker remedy, then a `commit_remedy` tool call declared with `needs_approval=True`. Use the actual SDK interruption, `RunResult.to_state()`, `state.approve(...)`, and `Runner.run(original_agent, state)`; do not invent a parallel approval mechanism. The hotel adapter accepts an idempotency key and stores one result per key.

**Step 4: Verify**

Run the focused tests, `npm run smoke:stub`, and `npm run check`. Expected: one pause, no preapproval dispatch, one postapproval dispatch.

**Step 5: Commit**

Commit as `feat: add deterministic Agents SDK approval flow`.

## Task 4: Make serialized approval restart-safe and version-bound

**Files:**

- Create: `server/agents/versioning.py`
- Modify: `server/orchestrator.py`, `server/store.py`, `server/models.py`, `server/config.py`
- Test: `tests/integration/test_restart_resume.py`, `tests/integration/test_incompatible_resume.py`

**Step 1: Write failing restart tests**

Pause a recovery, destroy the orchestrator and database connection, create fresh instances against the same database, load the serialized SDK state, approve, and assert one execution. Create variants with a changed protocol version, agent graph version, SDK version, and definition digest; each must fail before provider dispatch with a public `resume_incompatible` code.

**Step 2: Observe failures**

Run `uv run pytest tests/integration/test_restart_resume.py tests/integration/test_incompatible_resume.py -q` and confirm the missing envelope behavior.

**Step 3: Implement the state envelope**

Persist `sdk_version`, `protocol_version`, `agent_graph_version`, `definition_digest`, `root_trace_id`, `execution_mode`, and `state_json`. Compute `definition_digest` from canonical prompt text, structured-output schemas, and tool schemas. Validate every marker before calling `RunState.from_json`; log the internal mismatch, but return only the stable public code and recovery ID.

Add a startup reconciler that resumes a transaction already committed in `executions` but not yet reflected in a terminal receipt. The provider remains idempotent under `recoveryId:interruptionId:remedyDigest`.

**Step 4: Verify**

Run both focused suites five times, then `npm run check` and `npm run smoke:stub`.

**Step 5: Commit**

Commit as `feat: resume versioned approvals after restart`.

## Task 5: Bind consent to a canonical remedy digest

**Files:**

- Create: `server/digest.py`, `server/policy.py`
- Modify: `server/models.py`, `server/orchestrator.py`, `server/store.py`, `server/main.py`
- Modify: `web/src/domain/recovery.ts`, `web/src/components/EvidenceInspector.tsx`, `web/src/api/client.ts`
- Test: `tests/domain/test_digest.py`, `tests/integration/test_consent_tampering.py`, `tests/api/test_decisions.py`, `web/src/components/EvidenceInspector.test.tsx`

**Step 1: Write failing canonicalization and attack tests**

Use fixtures with reordered object keys and set-like arrays to prove stable digests. Use integer minor currency units and UTC expiry; reject floats and non-UTC timestamps. Test terms mutated after display, expiry, another recovery’s digest, another tool-call ID, constraint regression, authority overflow, duplicate approval, and two concurrent approvals. Assert every failed case leaves execution count at zero except the one legitimate winner in the concurrency test.

**Step 2: Observe failures**

Run `uv run pytest tests/domain/test_digest.py tests/integration/test_consent_tampering.py tests/api/test_decisions.py -q` and the inspector component test.

**Step 3: Implement exact consent binding**

Canonicalize this exact object:

```python
{
    "recoveryId": recovery_id,
    "remedyId": remedy_id,
    "terms": exact_terms,
    "costDeltaMinor": cost_delta_minor,
    "changedFields": sorted(changed_fields),
    "providerCommitments": sorted(provider_commitments),
    "expiry": expiry_utc,
}
```

Serialize UTF-8 JSON with sorted keys and no insignificant whitespace; return `sha256:<64 lowercase hex>`. Store digest and expiry on the pending approval. Require `clientDecisionId`, `remedyId`, `remedyDigest`, and `toolCallId` in `POST /api/recoveries/{id}/decisions`.

Use SQLite `BEGIN IMMEDIATE` to claim a pending decision. Recompute the digest and recheck expiry, hard constraints, delegated authority, and the exact interruption immediately before creating an execution. A repeated `clientDecisionId` returns its stored result; a different loser returns `409 already_decided` without dispatch.

Render all consent fields from the server snapshot. Shorten only the visible digest; the copy action copies the full value.

**Step 4: Verify**

Run focused suites, a 20-iteration concurrency test, `npm run check`, and `npm run smoke:stub`.

**Step 5: Commit**

Commit as `feat: bind approval to exact remedy digest`.

## Task 6: Complete the rejection lifecycle without execution

**Files:**

- Modify: `server/orchestrator.py`, `server/store.py`, `server/main.py`, `server/models.py`
- Modify: `web/src/components/EvidenceInspector.tsx`, `web/src/hooks/useRecovery.ts`, `web/src/App.tsx`
- Test: `tests/integration/test_rejection.py`, `tests/integration/test_decision_race.py`, `tests/api/test_rejection.py`, `web/src/components/EvidenceInspector.test.tsx`

**Step 1: Write failing rejection tests**

Decline the exact interruption and assert the SDK state receives a model-visible rejection message. The stub broker must close without action; assert zero provider dispatches, permission revocation, `closed_without_action`, and a sealed cancellation receipt. Repeat across process restart. Race approve and decline; exactly one terminal decision may win and execution count is either one for approved or zero for declined.

**Step 2: Observe failures**

Run all four focused suites and confirm rejection currently lacks a safe terminal path.

**Step 3: Implement rejection**

Call the Agents SDK rejection API on the exact stored interruption and resume the original root agent. The deterministic broker chooses no alternative and explains closure. Allow a future alternative only if it independently passes current hard constraints and delegated authority. Revoke scoped temporary permissions in every terminal branch.

Render `Decline` beside `Approve remedy`, require an explicit submitting state, and wait for the server event before changing the receipt. Use the cancellation content in `docs/design/README.md`; never show `cancelled` when dispatch may have begun—use `outcome_unknown` instead.

**Step 4: Verify**

Run the focused suites repeatedly, `npm run check`, and stub smoke paths for approve and decline.

**Step 5: Commit**

Commit as `feat: close rejected remedies without action`.

## Task 7: Add one root OpenAI trace and the live model path

**Files:**

- Modify: `server/agents/factory.py`, `server/agents/tracing.py`, `server/orchestrator.py`, `server/config.py`, `server/models.py`
- Create: `scripts/smoke_live.py`, `scripts/smoke_live_three.py`
- Create: `docs/live-validation.md`
- Test: `tests/integration/test_live_contract.py`, `tests/domain/test_trace_metadata.py`

**Step 1: Write failing trace contract tests**

Mock the SDK transport and assert consumer, provider, and broker runs share one root trace ID. Assert sensitive trace input/output is disabled, the receipt contains model IDs from the actual run, and `openai_live` cannot be selected when health is not live-ready.

**Step 2: Observe failures**

Run `uv run pytest tests/integration/test_live_contract.py tests/domain/test_trace_metadata.py -q`.

**Step 3: Implement live orchestration**

Wrap the three runs in one SDK `trace()` scope. Configure `RunConfig(trace_include_sensitive_data=False)` and disable model/tool payload logging. Use `gpt-5.6-luna` for narrow consumer/provider structured outputs and `gpt-5.6-terra` for the broker. Preserve returned model IDs and the single root trace ID in state and receipts. Keep all provider work in the demo adapter.

The smoke script reads `OPENAI_API_KEY` from the process environment only. It prints no key, prompt payload, or sensitive trace content. Its redacted result includes pass/fail, elapsed milliseconds, model IDs, ordered tool names, approval count, and trace ID.

**Step 4: Validate the real path**

Validate candidate local keys without displaying them, export only the working key to the command process, then run:

```bash
npm run smoke:live
npm run smoke:live:3
```

Record all three results in `docs/live-validation.md`. If external quota, auth, or model access blocks a run, preserve the exact redacted error class and mark the live gate blocked; do not substitute stub evidence.

**Step 5: Commit**

Commit as `feat: trace live recovery under one root`.

## Task 8: Add public-demo controls, cleanup, and safe errors

**Files:**

- Create: `server/controls.py`, `server/logging.py`, `server/cleanup.py`
- Modify: `server/main.py`, `server/orchestrator.py`, `server/store.py`, `server/config.py`
- Modify: `web/src/App.tsx`, `web/src/api/client.ts`
- Test: `tests/security/test_limits.py`, `tests/security/test_public_errors.py`, `tests/domain/test_cleanup.py`, `web/src/App.test.tsx`

**Step 1: Write failing abuse and disclosure tests**

Test max concurrent live recoveries, per-session/IP cooldown, configurable daily budget, record TTL cleanup, and automatic replay fallback. Send malformed IDs, oversized payloads, and internal exceptions; public responses must use generic codes while captured server logs retain request/recovery correlation IDs but contain no API key, authorization header, prompt, serialized run state, or tool payload.

**Step 2: Observe failures**

Run the focused security/domain/frontend suites.

**Step 3: Implement controls**

Use bounded Pydantic request models, trusted proxy configuration that defaults off, server-issued demo-session cookies, SQLite-backed quota counters, and an async semaphore for live concurrency. Apply security headers, strict allowed methods/origins in deployed mode, request size limits, and generic exception mapping. Cleanup only terminal recoveries older than the configured TTL; retain aggregate usage without sensitive payloads.

When live is unavailable or a public limit is reached, return a stable explanation and offer `replay_fixture`; never silently relabel replay as live.

**Step 4: Verify**

Run the focused suites, `npm run check`, and `npm run smoke:stub`.

**Step 5: Commit**

Commit as `feat: harden the public demo boundary`.

## Task 9: Complete the API quota recovery trace

**Files:**

- Create: `server/providers/quota_simulator.py`
- Modify: `server/scenarios/api-quota.json`, `server/replay/engine.py`, `server/orchestrator.py`
- Modify: `web/src/fixtures/recoveries.ts`, `web/src/App.tsx`
- Test: `tests/integration/test_quota_recovery.py`, `web/src/App.test.tsx`

**Step 1: Write the failing complementary-policy test**

Assert the provider proves the quota ceiling, grants a temporary US-region burst, preserves every hard constraint, stays inside delegated cost authority, creates zero human interruptions, verifies execution, revokes permission, and seals a receipt. Assert the scenario rejects `openai_live` and supports only `sdk_stub` or `replay_fixture`.

**Step 2: Observe failures**

Run the focused server and frontend tests.

**Step 3: Implement exactly one second complete trace**

Add a deterministic provider and replay event sequence for the quota case. Surface delegated authority as the authorization source and an approval count of zero. Keep the rail to exactly the two named scenarios and remove all placeholder navigation.

**Step 4: Verify**

Run focused tests, `npm run check`, and replay both scenarios from a reset database.

**Step 5: Commit**

Commit as `feat: add deterministic quota recovery proof`.

## Task 10: Match the approved responsive operational UI

**Files:**

- Create: `web/src/components/AppHeader.tsx`, `web/src/components/EventLedger.tsx`, `web/src/components/ConsentSheet.tsx`, `web/src/components/Receipt.tsx`, `web/src/components/TechnicalEvidence.tsx`
- Modify: `web/src/App.tsx`, `web/src/styles.css`, all existing components
- Test: `web/src/components/ConsentSheet.test.tsx`, `web/src/components/Receipt.test.tsx`, `web/src/App.a11y.test.tsx`

**Step 1: Write failing semantic and truth-copy tests**

Assert the six steps are an ordered list, all buttons are named, the mobile consent surface is a dialog with focus management, live changes use `aria-live`, errors use `role=alert`, and terminal receipts contain the required provenance/evidence fields. Assert the declined receipt says execution count zero and provider dispatch did not begin. Run an automated accessibility scan for critical violations.

**Step 2: Observe failures**

Run the focused Vitest suites at desktop and mobile matchMedia settings.

**Step 3: Implement the design contract**

Follow `docs/design/README.md` and the four concept images. Use CSS Grid for the desktop shell and one-column flow below 760 px. Use inline SVG/CSS icons with text labels, 44 px targets, visible focus, safe-area insets, reduced motion, and semantic evidence. Do not add decorative cards, gradients, marketing sections, unsupported metrics, or a third scenario.

**Step 4: Verify**

Run all frontend tests, `npm run check`, and compare browser renders at 1440×1024 and 390×844 to the concepts. Record material intentional differences in the final fidelity ledger.

**Step 5: Commit**

Commit as `feat: finish responsive recovery console`.

## Task 11: Package one production process and verify SSE in a container

**Files:**

- Create: `Dockerfile`, `scripts/docker_smoke.py`, `scripts/start.py`
- Modify: `.dockerignore`, `server/main.py`, `server/config.py`, `package.json`
- Test: `tests/api/test_static_fallback.py`, `tests/api/test_readiness.py`

**Step 1: Write failing production-serving tests**

Build the frontend to `web/dist`, mount it in the FastAPI app, and test `/`, hashed assets, SPA fallback, `/health`, `/readyz`, and `/api/*` precedence. Test local bind defaults to `127.0.0.1`, while deployed mode binds `0.0.0.0`.

**Step 2: Observe failures**

Run the focused tests and confirm static production serving is absent.

**Step 3: Implement the multi-stage image**

Use a Node build stage and a Python 3.12 slim runtime. Install locked Python dependencies without development groups, copy `web/dist`, create a non-root user, use a writable data directory, add a health check, and start one FastAPI process suitable for long-lived SSE. Do not embed `.env*`, Git data, tests, docs concepts, screenshots, keys, or caches.

**Step 4: Verify locally**

Run:

```bash
docker build -t backchannel:build-week .
npm run smoke:docker
```

The Docker smoke must verify frontend, health, SSE reconnect, approval, rejection, both receipt types, and absence of secrets in responses. If the host lacks Docker, record that exact environment blocker and run the equivalent single-process production smoke outside a container; do not call it container proof.

**Step 5: Commit**

Commit as `build: package the SSE demo for deployment`.

## Task 12: Submission evidence, browser capture, and release gate

**Files:**

- Create: `README.md`, `docs/architecture.md`, `docs/protocol.md`, `docs/validation.md`, `docs/judge-checklist.md`
- Create: `e2e/judge-flow.spec.ts`, `playwright.config.ts`, `scripts/secret_scan.py`, `scripts/export_openapi.py`
- Create generated final captures under: `docs/assets/final/`
- Create: `.github/workflows/ci.yml`
- Modify: `package.json`, `.gitignore`

**Step 1: Write failing release checks**

Add Playwright tests that reset the demo and capture pending consent, completed approval, and declined receipt at 1440×1024 and 390×844. The tests inspect provenance text against API state before each capture. Add a secret scan that fails on tracked `.env.local`, key-like strings, authorization headers, serialized SDK state, secrets in `web/dist`, captures, logs, or a `git archive`.

**Step 2: Observe failures**

Run `npm run test:e2e` and `npm run secret:scan`; expect missing configuration/scripts.

**Step 3: Complete docs and automation**

The README first screen contains the one-sentence problem, the six steps, live and replay commands, and the exact demo-provider trust boundary. The architecture/protocol/validation docs match implemented behavior. The judge checklist separates public URL, GitHub URL, three-minute demo, live trace evidence, replay fallback, measured claims, simulated claims, and unverified external gates.

CI installs from lockfiles, runs lint/type/test/build/stub smoke/secret scan, and never requires a live key. Export OpenAPI and capture final screenshots only from the final build. Compare them with `docs/design/*.png` and complete the fidelity ledger.

**Step 4: Run every applicable gate**

Run:

```bash
npm ci
uv sync --frozen --all-groups
npm run check
npm run smoke:stub
npm run smoke:live
npm run smoke:live:3
npm run test:e2e
npm run secret:scan
docker build -t backchannel:build-week .
npm run smoke:docker
git status --short
```

Record exact pass/fail/blocker results. Submit smoke-test failures to the user’s supplied Formspree endpoint only with redacted diagnostics and no local paths, credentials, payloads, or trace-sensitive content.

**Step 5: Commit, publish, deploy, and tag only through passed gates**

Commit as `docs: prepare the Build Week release evidence`.

If `gh auth status` succeeds and no target repository exists, create public `charlie2233/backchannel-agent-support`, add it as `origin`, and push the branch. Never overwrite an existing repository. Deploy the long-lived container to a provider that supports SSE, then update `docs/judge-checklist.md` with the verified URL and commit/push that update.

Only after local, live, container, GitHub, and deployment checks pass, create and push annotated tag `v0.3.0-build-week`. If any external gate remains blocked, do not tag; report the exact gate and retain the working branch.

## Required final evidence

The handoff report must enumerate commits, changed-file groups, local gates, live evidence for three consecutive runs, container proof or exact missing-Docker blocker, public deployment URL or blocker, GitHub URL or auth/repository blocker, and remaining judge-impacting risks. It must explicitly distinguish generated design concepts from final-build screenshots and must not claim that the demo adapter booked a real hotel, charged a payment method, or changed a real quota.
