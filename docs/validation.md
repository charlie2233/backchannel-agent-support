# Validation matrix

Proof is recorded per commit and per lane. Source-gate evidence is not browser,
container, live OpenAI, deployment, or release evidence.

This file records only `codex/backchannel-v0.3` evidence. It does not import or claim
results from any sibling branch.

## Current Task36 authorization-expiry evidence activation

- Feature source:
  `9239410042de1477c15bdf1a4c72fb1bf00dd831`
- Manifest capture source / corrected owned-build harness:
  `a01f0a361f2c45f762496a0c6af298a4473fde96`
- Capture evidence commit:
  `1030d197ce2801be2abe17bea43522a97736adb8`
- The focused Task36 approval-expiry, restart, migration, and API matrix passed
  `93 passed`. The full Python suite passed `1022 passed`; the web suite passed
  19 files / 382 tests. The full Python result included 3 warnings in 26.36s.
  The production TypeScript/Vite build, Ruff, strict MyPy, OpenAPI verification,
  the history-aware secret scan, and the diff check all passed.
- Three focused public terminal-evidence tamper cases passed `3 passed`: owner
  snapshot, receipt, and SSE each failed closed instead of publishing forged,
  preauthorization, or internally inconsistent terminal evidence.
- The deterministic stub smoke passed. The local one-process production smoke
  passed health, readiness, static serving, API, SSE/reconnect, approval,
  decline, session isolation, and bounded clean SIGTERM.
- Independent source/specification and security reviews passed with no P0-P3
  findings after closing forged execution evidence, cancellation wake,
  exact-at-expiry execution truth, live UI truth, preauthorization chronology,
  public terminal-read fail-open, masked `UserError`, restart-clock, and Task5
  migration gaps.
- The first Task36 capture audit found a P2 provenance gap: a pre-existing
  ignored `web/dist` could have been served while the manifest named a clean
  current source. The corrected harness now builds into a fresh
  capture-owned temporary directory from an allowlisted secret-safe
  environment, rejects Vite environment files and pre-existing, symlinked,
  malformed, excessive, or unexpected build outputs, serves that exact owned
  path, and re-verifies unchanged clean source identity after the build. The
  discarded pre-fix capture is not claimed.
- `npm run test:e2e` then reported `35 passed` against the clean manifest source
  in Google Chrome 150.0.7871.187 and produced six manifest-bound PNGs at
  1440×1024 and 390×844. The run covered 35 capture contracts.
- Five PNGs changed from Task35 only in expected dynamic IDs, digests, and
  expiry values; `mobile-consent.png` was byte-identical. The combined
  before/after visual review and the corrected capture/provenance review both
  passed with no P0-P3 findings.
- The live three-run command remained blocked at its
  `missing_openai_api_key` preflight after `attempted=1` of `target=3`, with
  `approvals=0`, model IDs `[]`, tools `[]`, and `rootTraceId=null`. It did not
  execute three live runs or prove a real provider mutation.
- Feature-source Actions
  [run 30606996966](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30606996966)
  was bounded nonproof: `verify`
  ([job 91081238197](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30606996966/job/91081238197))
  reported `FAILURE` at stale `capture_manifest_runtime`;
  `container-smoke`
  ([job 91081310037](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30606996966/job/91081310037))
  reported `SKIPPED`. Annotation counts were 1 / 0.
- Corrected-harness Actions
  [run 30607860082](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30607860082)
  was also bounded nonproof: `verify`
  ([job 91083947213](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30607860082/job/91083947213))
  reported `FAILURE` at stale `capture_manifest_runtime`;
  `container-smoke`
  ([job 91084012386](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30607860082/job/91084012386))
  reported `SKIPPED`. Annotation counts were 1 / 0.
- Capture-commit Actions
  [run 30608202642](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30608202642)
  remained bounded nonproof. `verify`
  ([job 91084965375](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30608202642/job/91084965375))
  reached `capture_manifest_valid` and 19 web files / 382 tests, then reported
  `FAILURE` with `264 failed, 758 passed, 1 warning` in 58.16s because the
  release-document contract still named Task35 and the removed `pretest:e2e`
  contract. The generic exit-code annotation count was 1. `container-smoke`
  ([job 91085278851](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30608202642/job/91085278851))
  reported `SKIPPED`, with annotations `[]`; stub smoke, OpenAPI, and secret-scan
  workflow steps were not reached after the canonical failure.
- No successful current Task36 hosted `verify`, packaged `container-smoke`, or
  documentation-tip CI result is claimed.

## Current Task36 authorization-expiry proof boundary

A claimed approval retains the immutable authorization expiry from its exact
consent scope. The demo-adapter commit path atomically rechecks the claimed
decision, session owner, generation, policy, scope, expiry, and canonical
provider result before it writes execution evidence.

If authorization is already expired before dispatch, the safe path records
`closed_without_action` + `executionStarted: false` +
`authorization_expired_before_dispatch` with zero provider dispatch and zero
execution. If the system cannot prove whether dispatch occurred, it records
`outcome_unknown` + `executionStarted: true` +
`authorization_expired_with_unresolved_dispatch` and does not infer a
successful terminal provider result.
Cancellation after a committed claim wakes the durable lifecycle instead of
leaving a silent claim.

Execution evidence is accepted only when its exact chronology is
`claimed_at == scope.activated_at <= execution.created_at < expiry`. Public
terminal snapshot, receipt, and SSE readers validate the complete sealed receipt
graph in one transaction and fail closed on tamper or preauthorization evidence.
Task5 migration restores activation from durable `decision.claimed_at`. Typed
SDK-stub and OpenAI-live responses and receipts require the truthful
status/reason pairing. Rolling-receipt normalization only fills an omitted
`terminalReason` as null; it does not invent an expiry reason or provider
execution.

## Current Task36 pending external gates

- The three exact current Task36 hosted runs above are bounded failures with
  skipped container jobs. A successful activation `verify`, packaged
  `container-smoke`, and documentation-tip CI remain pending.
- There is no current Task36 live OpenAI, public deployment/browser URL, target
  host durability/networking, abrupt-host-loss or backup, concurrent
  multi-container SQLite, real external-provider execution, tag, or release
  proof or claim.

## Per-SHA proof matrix

| Lane | Exact evidence | Status and boundary |
| --- | --- | --- |
| Local source and tests | Feature source `9239410…`: focused 93, public-terminal tamper 3, 19 web files / 382 tests, 1022 Python tests, build, Ruff, strict MyPy, OpenAPI, secret scan, diff, stub smoke, and local production smoke | **Verified locally** in the exact named lanes |
| Local final-build captures | [`docs/assets/final/manifest.json`](assets/final/manifest.json); six provenance-checked PNGs under [`docs/assets/final`](assets/final) | **Local capture evidence only** on clean manifest source `a01f0a3…`, capture commit `1030d19…`, and Google Chrome 150.0.7871.187 at 1440×1024 and 390×844; not CI, container, deployment, live OpenAI, or release proof |
| Live OpenAI | Three-run command stopped at `missing_openai_api_key` after attempt 1 of 3, with zero approvals, model IDs `[]`, tools `[]`, and null root trace | **Blocked / Unverified**; three live runs did not execute |
| Local Docker | No Docker-family runtime is installed on this Mac | **Unverified locally** |
| GitHub CI/container | Runs `30606996966`, `30607860082`, and `30608202642`: all `verify` jobs failed; all `container-smoke` jobs were skipped; latest annotations 1 / 0 | **Failed / Unverified**; bounded nonproof only, with no current Task36 hosted canonical or packaged-container success |
| Public deployment | No public application origin or browser URL | **Blocked / Unverified** |
| Target host/durability | No target-host networking, abrupt-loss, backup, or concurrent multi-container SQLite proof | **Blocked / Unverified** |
| Tag/release | No release tag or GitHub release | **Blocked / Unverified** |

## Hosted container evidence boundary

The three exact current Task36 hosted `verify` jobs failed and their
`container-smoke` jobs were skipped. The latest capture-commit run reached
manifest verification and the web suite before the stale release-document
contract failed; it did not reach the later stub, OpenAPI, or secret-scan
workflow steps. These failures are bounded nonproof and are not converted into
hosted or container success by local gates.

The superseded Task35 hosted result below is historical evidence for its own
exact SHA only and is not inherited by Task36. Local source gates and local
browser captures do not prove packaged Docker, browser capture in CI, public
deployment, live OpenAI, real provider execution, target-host
durability/networking, abrupt host loss or backup, concurrent multi-container
SQLite, documentation-tip CI, tag, or release.

## Capture contract

The final-build runner deliberately omits `OPENAI_API_KEY`, rejects Vite
environment files, accepts only its small secret-safe build/server environment
allowlists, generates a never-logged HMAC secret, starts one loopback process
with a fresh SQLite database, and uses Google Chrome 150.0.7871.187 on the stable
`chrome` channel in headless mode. It validates API and DOM provenance before
each viewport screenshot.

The runner builds the frontend into a newly owned temporary directory, rejects a
pre-existing or symlinked destination and malformed, excessive, or unexpected
artifacts, serves that exact directory, and rechecks that source commit and
runtime input did not change during the build. A stale ignored repository
`web/dist` therefore cannot supply the named capture.

The tracked [`docs/assets/final/manifest.json`](assets/final/manifest.json) is
schema v1 and binds manifest source
`a01f0a361f2c45f762496a0c6af298a4473fde96`, runtime SHA-256 digest
`168e315936fe58a2f2371c0122dbf0c5d5a4f0be93f7f2a1021e4f84dd90a6f4`
across 85 runtime paths, capture profile `keyless_sdk_stub`, environment
`en-US` / `UTC` / `reduce` / `light`, and all six artifact hashes, states, and
dimensions.

Feature source `9239410…`; manifest source and owned-build harness `a01f0a3…`;
capture evidence commit `1030d19…`; runtime digest `168e315…`. The browser
capture remained local; CI did not execute it. The six images prove only the
local keyless SDK-stub final build. They do not prove live OpenAI, a public
deployment, a container, a release, or a real provider mutation.

A later documentation-only successor does not inherit a new browser claim. Keep
the capture lane bound to `a01f0a3…` unless `npm run test:e2e` is observed again
on a different clean frozen tree.

## SQLite file-permission proof boundary

The retained Task32 source creates the SQLite database and its `-journal`, `-wal`, and `-shm`
sidecars as owner-only `0600` regular files. Constructor-time initialization is the
only lane allowed to harden an existing database or sidecar; later connection and
readiness checks are nonmutating and fail closed instead of repairing permission
drift. Every SQLite connection uses a URI with `mode=rw`, so a missing database is
not silently recreated during a runtime check.

Unsupported POSIX capabilities, an unexpected owner, unprotected directory ancestry,
a symlink or other non-regular file, a changed device/inode identity, and insecure
database or sidecar permissions all fail closed. The packaged image creates `/data`
as owner-only `0700`. The 23 focused tests cover these boundaries, constructor-only
hardening, runtime replacement/deletion races, and preservation of parent,
unrelated-file, and symlink-target permissions. This is local source/test evidence;
it is not filesystem encryption, backup-permission, target-host, local-Docker,
container-replacement, abrupt-host-loss, or concurrent multi-container SQLite proof.

## JSON response media proof boundary

The current client accepts exactly one `Content-Type` field whose media type is the
case-insensitive `application/json` media type, with valid no-comma token or
quoted-string parameters. It rejects missing, wrong, malformed, comma-joined,
duplicate parameters, and split-quote values. When headers are available before an
unlocked body completes, it cancels the unlocked wrong-media response body without
waiting. On that rejection path, typed IDs, fallback, retry metadata, and provenance
remain inactive. SSE behavior is unchanged; ordinary `500` handling and the Task30
exact `503` retry contract remain preserved. Focused client tests and 28 adversarial
cases prove these source-level parser boundaries; the screenshots do not prove
malformed-response handling or public network behavior.

## Terminal evidence retry proof boundary

The current source accepts only the exact owner-correlated store-unavailable tuple on
terminal snapshot and receipt reads: HTTP `503`, code `internal_error`, `recoveryId`
equal to the requested recovery UUID, `retryAfterSeconds: 1`, `fallback: null`, a
matching `Retry-After: 1` header, and `application/json` media. Null, foreign, or
substituted recovery IDs and malformed status, header, body, or media fail closed.
The bounded automatic cycle is one initial authoritative read pair plus exactly three
one-second automatic retries. After exhaustion, one accessible manual retry begins a
fresh equally bounded initial-plus-three cycle. Throughout retry, already verified
snapshot, receipt, event, and cursor state remains visible. No POST, create, decision,
reset, or fallback request is issued by either automatic or manual terminal evidence
retry. Focused tests also cover coalescing, peer abort, timer/controller/generation
fencing, unmount cleanup, and inconsistent pairs. This is local source/test evidence.
The ordinary final-state screenshots do not prove transient store failure, retry
timing, public latency, or hosted behavior.

## Async SQLite proof boundary

The current source retains the bounded-worker SQLite design. The superseded Task29
section below records the exact focused, full, and hosted evidence for that earlier
checkpoint. The current Task36 activation verifies only its exact local source,
capture, and smoke lanes; it does not prove multi-process or concurrent
multi-container SQLite, target-host scheduling, abrupt host loss, public latency,
or live provider behavior.

## Request-body availability proof boundary

The manifest source rejects framed `GET` and `HEAD` requests before body reads,
preserves ordinary bodyless receives for downstream disconnect/SSE handling, and
bounds allowed-body pre-buffering with a configurable cooperative deadline. Those
are source and raw-ASGI contract properties. The six screenshots do not prove
adversarial transport timing, public-host availability, or hard cancellation of a
non-cooperative dependency.

## Creation-admission proof boundary

The source contract has a separate durable UTC-day creation ledger with session, IP,
and global limits. Its tests cover atomic race admission, rollback on an injected
second/third write failure, validation-before-charge, live-ledger separation, reset
and restart persistence, bounded retention, and the generic `429`/`Retry-After`
contract. This source/test evidence does not change the final-build capture lane from
`a01f0a3…` and is not public deployment, live OpenAI, container-host durability, tag,
or release proof.

## Live deadline proof boundary

The source contract bounds the complete live pre-approval graph and each live
decision resume with a configurable 1..300-second cooperative deadline. Focused tests
cover one shared graph budget, external cancellation propagation, no partial recovery,
route-gate release, retained admission charges, exact public `504 live_timeout`
envelopes, retryable approve/decline claims, and no duplicate dispatch when provider
execution committed before the timeout. The OpenAI client and the redacted live-smoke
client use the same configured seconds with transport retries disabled. This remains
source/test evidence only; it is not a real OpenAI trace, hard
process-termination proof, public deployment, or release claim.

## Superseded historical hosted baseline

### Task35 terminal-marker capture and hosted baselines (superseded)

- Capture source / frozen runtime:
  `21d9b0f8dbeb59454e3f3b3d3d9138af28af02ad`
- Capture evidence activation:
  `60fe1243a5eeb760984d78d667f7efdc33630adc`
- Final evidence activation:
  `6533a3f2e212d9a7c4e1af9f1d2dd77f7bd45a18`
- `npm run test:e2e` reported `29 passed` against the clean capture source
  in Google Chrome 150.0.7871.187 and produced six manifest-bound PNGs at
  1440×1024 and 390×844. The run covered 29 capture contracts.
- Five PNGs changed from Task34; `mobile-consent.png` was byte-identical. The
  combined before/after review across all six images passed with no P0-P3
  findings.
- The focused regression was first observed RED with `2 failed, 17 deselected`:
  reopening after clearing the sole terminal marker for approval and decline
  DID NOT RAISE `ReceiptTransitionError`.
- The focused transition-marker gate then passed with `3 passed, 17 deselected`;
  the whole Task7 migration suite passed with `20 passed`.
- The full Python suite passed with `951 passed`, 3 warnings, in 24.57s. Ruff,
  strict MyPy on `server/store.py`, and the diff check passed.
- Independent Task35 specification and quality reviews passed with no P0-P3
  findings after the quality review required and implementation added the
  explicit second-reopen proof.
- The exact local canonical gate on final evidence activation
  `6533a3f2e212d9a7c4e1af9f1d2dd77f7bd45a18` covered 29 capture contracts,
  `capture_manifest_valid`, 19 web test files / 369 tests, the production
  TypeScript/Vite build, Ruff, strict MyPy over 35 source files, and
  `957 passed`, 3 warnings, in 25.36s.
- The local deterministic stub smoke reported `PASS`. The local production
  `deterministic-qa` smoke reported `PASS` for health, readiness, session
  isolation, SSE reconnect, approval, decline, and bounded SIGTERM. OpenAPI
  verification and the history-aware secret scan also reported `PASS`.
- The live three-run command remained blocked at its
  `missing_openai_api_key` preflight after `attempted=1` of `target=3`, with
  `approvals=0` and no model IDs, tools, or root trace. It did not execute three
  live runs.
- The earlier capture evidence activation
  `60fe1243a5eeb760984d78d667f7efdc33630adc` and GitHub Actions
  [run 30595724271](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30595724271)
  remain bounded historical nonproof: `verify`
  ([job 91047571247](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30595724271/job/91047571247))
  reported `FAILURE` with `1 failed, 950 passed, 1 warning` in 41.89s and
  exactly one observed annotation; `container-smoke`
  ([job 91047810594](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30595724271/job/91047810594))
  reported `SKIPPED`, with container annotations `[]`.
- Final evidence activation GitHub Actions
  [run 30598406930](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30598406930)
  passed the exact named hosted lanes. `verify`
  ([job 91055708186](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30598406930/job/91055708186))
  reported `PASS` with `957 passed, 1 warning` in 46.45s and covered the
  canonical gate, deterministic stub smoke, OpenAPI verification, and the
  history-aware secret scan. `container-smoke`
  ([job 91055968348](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30598406930/job/91055968348))
  reported `PASS`, built the packaged image, confirmed runtime user
  `10001:10001`, and passed the offline network-none `deterministic-qa` and
  `deployed-readonly` profiles. Both job annotation APIs returned `[]`.
- Final Task35 documentation tip:
  `6cbafce70fa1ea60cf00e315062e4e14a5ffb934`
- Final Task35 documentation-tip GitHub Actions
  [run 30603151491](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30603151491)
  passed its exact named lanes. `verify`
  ([job 91069887646](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30603151491/job/91069887646))
  reported `PASS` with 29 capture contracts, `capture_manifest_valid`, 19 web
  files / 369 tests, the TypeScript/Vite build, Ruff, strict MyPy over 35 source
  files, and `992 passed, 1 warning` in 44.62s. Deterministic stub smoke,
  OpenAPI verification, and the history-aware secret scan passed.
  `container-smoke`
  ([job 91070162086](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30603151491/job/91070162086))
  reported `PASS`, built the packaged image, confirmed runtime user
  `10001:10001`, and passed the offline network-none `deterministic-qa` and
  `deployed-readonly` profiles. Both final-tip job annotation APIs returned
  `[]`.

#### Task35 transition-marker proof boundary

On modern schemas, clearing the sole `terminal = 1` marker for a completed
approval or decline makes reopen fail closed with `ReceiptTransitionError`.
Reopen performs no repair and preserves exact event-row and text-byte stability.

On a genuine legacy events schema without `terminal`, reopen performs a
one-time `terminal` add/backfill, leaves exactly one final marker, and provides exact
unrelated-row preservation and exact database-byte stability across a second reopen;
the foreign-key check was empty and the integrity check returned `ok`.

#### Task35 final-tip result and external gates

- Task35 final documentation-tip CI passed only the exact hosted canonical and
  packaged-container lanes named above. It did not execute the local browser
  capture.
- Task35 retained no live OpenAI, public deployment/browser URL, local-Docker,
  target-host durability/networking, abrupt-host-loss or backup, concurrent
  multi-container SQLite, real external-provider execution, tag, or release
  proof or claim. The blocked missing-key attempt did not execute three live
  runs or prove a real provider mutation.

#### Task35 per-SHA proof matrix

| Lane | Exact evidence | Status and boundary |
| --- | --- | --- |
| Local source and tests | Exact final evidence activation canonical gate on `6533a3f…`: 29 capture contracts, manifest verification, 19 web files / 369 tests, build, Ruff, strict MyPy over 35 files, and 957 Python tests | **Verified locally** in the exact named lanes |
| Local final-build captures | [`docs/assets/final/manifest.json`](assets/final/manifest.json); six provenance-checked PNGs under [`docs/assets/final`](assets/final) | **Local capture evidence only** on source `21d9b0f…` in Google Chrome at 1440×1024 and 390×844; not container, deployment, live OpenAI, or release proof |
| Live OpenAI | Three-run command stopped at `missing_openai_api_key` after attempt 1 of 3, with zero approvals and no model IDs, tools, or root trace | **Blocked / Unverified**; three live runs did not execute |
| Local Docker | No Docker-family runtime is installed on this Mac | **Unverified locally** |
| GitHub CI/container | Final docs tip `6cbafce…`; run `30603151491`; `verify` job `91069887646` = `PASS`; `container-smoke` job `91070162086` = `PASS`; both annotation APIs `[]` | **Verified for the exact Task35 hosted canonical and packaged-container smoke lanes only** |
| Public deployment | No public application origin | **Blocked / Unverified** |
| Tag/release | No release tag or GitHub release | **Blocked / Unverified** |

#### Task35 hosted container evidence boundary

The final activation passed the exact hosted canonical lane and packaged
container smoke lane. The container job built the packaged image, confirmed
runtime user `10001:10001`, and passed only the offline network-none
`deterministic-qa` and `deployed-readonly` profiles. This does not prove local
Docker, browser capture in CI, public deployment or browser URL, live OpenAI,
real provider execution, container replacement or restart durability beyond
those exact profiles, long-lived `/data` volume persistence, target-host
durability/networking, abrupt host loss or backup, concurrent multi-container
SQLite, tag, or release.

The earlier capture activation run `30595724271` remains bounded historical
nonproof: `verify` failed and `container-smoke` was skipped. The successful
final activation and final documentation-tip run do not erase that result.
Live OpenAI, public deployment, local Docker, target-host durability, tag, and
release remained separate blocked or unverified lanes. The browser capture
remained local; CI did not execute the browser capture.

#### Task35 capture contract

The final-build runner deliberately omits `OPENAI_API_KEY`, generates a never-logged
HMAC secret, starts one loopback process with a fresh SQLite database, and uses
Google Chrome 150.0.7871.187 on the stable `chrome` channel in headless mode. It
validates API and DOM provenance before each viewport screenshot.

The tracked [`docs/assets/final/manifest.json`](assets/final/manifest.json) is schema
v1 and binds the full capture source SHA above, runtime SHA-256 digest
`eabc2743f9475e384fe7a49344f42a9635d2f57277f788708d23425edd845385`
across 84 runtime paths, capture profile `keyless_sdk_stub`, environment
`en-US` / `UTC` / `reduce` / `light`, and all six artifact hashes, states, and
dimensions.

Capture source `21d9b0f…`; capture evidence activation `60fe124…`; final
evidence activation `6533a3f…`; final documentation tip `6cbafce…`; runtime
digest `eabc274…`. The browser capture was produced against the clean source
SHA, and the capture activation successor added the captured files. The final
documentation tip adds no new browser claim. CI did not execute the browser
capture. The six images prove only the local keyless SDK-stub final build; they
do not prove live OpenAI, a public deployment, a container, a release, or a real
provider mutation.

A later documentation-only successor does not inherit a new browser claim. Keep the
capture lane bound to `21d9b0f…` unless `npm run test:e2e` is observed again on a
different clean frozen tree.

### Task34 terminal-snapshot capture and hosted baselines (superseded)

- Prior capture source:
  `8942517f43045a124e11a8e79296f6e4de875936`
- Prior evidence activation:
  `4ca13252e05357ff98023f9290375cd1718bcce6`
- Prior manifest runtime digest:
  `e208c767646af920d90aa9396f441fb328c999c3235aff646ac20a5611543f27`
  across 84 runtime paths, capture profile `keyless_sdk_stub`, Google Chrome
  150.0.7871.187 stable `chrome` in headless mode, environment `en-US` / `UTC` /
  `reduce` / `light`, and six PNGs at 1440×1024 and 390×844.
- Prior pre-CI/current activation:
  `5364dd4f9f825f5d68c85ea005ceb277ab943e4c`
- Prior final documentation tip:
  `9d7e850cbf6761524e9a51359801944cdf7fabf4`
- The superseded local source gate covered the model/store matrix with 51 / 51
  tests, the combined focused model/store/OpenAPI gate with 63 / 63, and
  935 / 935 tests with 3 warnings in 24.53s. Ruff, strict MyPy over 31 source
  files, OpenAPI verification, and the diff check passed.
- The Task34 server model accepted `completed`, `closed_without_action`, and
  `outcome_unknown` only at terminal `currentStep: 5`; invalid transitions
  mutated neither the recovery row nor event ledger. Hydrating a malformed
  legacy terminal row failed closed without rewriting it, and the OpenAPI
  `RecoverySnapshot` schema published the terminal-status conditional.
- The superseded local exact activation gate covered 29 capture contracts,
  `capture_manifest_valid`, 19 web test files / 369 tests and TypeScript/Vite,
  Ruff, strict MyPy over 35 source files, 944 / 944 Python tests with 3 warnings
  in 25.73s, deterministic stub and local
  production/static/API/SSE/reconnect/shutdown smokes, OpenAPI verification, the
  history-aware secret scan, and the diff check.
- The superseded combined before/after visual review covered all six Chrome
  images and passed with no P0-P3 findings.
- The superseded live three-run command stopped at its missing-`OPENAI_API_KEY`
  preflight after attempt 1 of 3, with zero approvals and no model IDs, tools,
  or root trace; it did not execute three live runs.
- Prior activation GitHub Actions:
  [run 30593858167](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30593858167),
  including `verify`
  ([job 91041814602](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30593858167/job/91041814602))
  and `container-smoke`
  ([job 91042106744](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30593858167/job/91042106744)).
- Both activation job annotation APIs returned `[]`.
- Activation hosted `verify` covered `capture_manifest_valid`, 19 web test files /
  369 tests, Ruff, strict MyPy over 35 source files, 944 Python tests with
  1 warning in 43.54s, deterministic stub smoke, OpenAPI verification, and the
  history-aware secret scan.
- Prior final-tip GitHub Actions:
  [run 30594431983](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30594431983),
  including `verify`
  ([job 91043601366](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30594431983/job/91043601366))
  and `container-smoke`
  ([job 91043886907](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30594431983/job/91043886907)).
- Both final-tip job annotation APIs returned `[]`.
- Final-tip hosted `verify` covered `capture_manifest_valid`, 19 web test files /
  369 tests, Ruff, strict MyPy over 35 source files, 948 Python tests with
  1 warning in 42.45s, deterministic stub smoke, OpenAPI verification, and the
  history-aware secret scan.
- Both prior `container-smoke` jobs built the packaged image, confirmed runtime
  user `10001:10001`, and passed the offline network-none `deterministic-qa` and
  `deployed-readonly` profiles, including readiness, session isolation, SSE,
  reconnect, and bounded shutdown checks.

This Task34 source, capture, activation, hosted, packaged-container, visual, and
missing-key evidence is truthful superseded history only. It is not current
Task35 source, capture, hosted CI, container, live OpenAI, deployment, tag, or
release proof.

### Client-terminal capture and hosted baselines (superseded)

- Prior capture source:
  `84af413c3a7833daa635e6a0b84e8f331cc1f7f2`
- Prior evidence activation:
  `7a8092f9ac0be03274358bb9814db3d8e6a34507`
- Prior manifest digest:
  `00d4636da32836689202464fc55deb5f4b2a8f5e6ffd02831d27d22a9146bc90`
  across 84 runtime paths.
- The prior Task33 source gate covered the 144 / 144 focused client matrix,
  369 / 369 web tests, and the TypeScript/Vite build. Independent
  specification, quality, and combined before/after visual reviews passed with
  no P0-P3 findings.
- The prior local exact gate on the pre-CI documentation successor
  `0dee3af822edc164b06be6cff61441023ae703dc` covered 29 capture
  contracts, `capture_manifest_valid`, 19 web test files / 369 tests and
  TypeScript/Vite, Ruff, strict MyPy over 35 source files, 866 / 866 Python
  tests with 3 warnings in 24.98s, deterministic stub and local
  production/SSE/reconnect/shutdown smokes, OpenAPI verification, and the
  history-aware secret scan.
- Prior pre-CI GitHub Actions:
  [run 30591690973](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30591690973),
  including `verify`
  ([job 91035189804](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30591690973/job/91035189804))
  and `container-smoke`
  ([job 91035490238](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30591690973/job/91035490238)).
- Both pre-CI jobs passed and both pre-CI job annotation APIs returned `[]`.
- Pre-CI hosted `verify` covered `capture_manifest_valid`, 19 web test files /
  369 tests, Ruff, strict MyPy over 35 source files, 866 Python tests with 1
  warning in 42.00s, deterministic stub smoke, OpenAPI verification, and the
  history-aware secret scan.
- Prior final documentation tip:
  `d02ed41e25d1ffa8a43ee8d74583a1008a4d8026`
- Prior final-tip GitHub Actions:
  [run 30592336018](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30592336018),
  including `verify`
  ([job 91037154486](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30592336018/job/91037154486))
  and `container-smoke`
  ([job 91037472406](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30592336018/job/91037472406)).
- Both final-tip jobs passed and both final-tip job annotation APIs returned
  `[]`.
- Final-tip hosted `verify` covered `capture_manifest_valid`, 19 web test files
  / 369 tests, Ruff, strict MyPy over 35 source files, 884 Python tests with 1
  warning in 43.91s, deterministic stub smoke, OpenAPI verification, and the
  history-aware secret scan.
- Both prior `container-smoke` jobs built the packaged image with owner-only
  `/data` mode `0700`, confirmed runtime user `10001:10001`, and passed the
  offline network-none `deterministic-qa` and `deployed-readonly` profiles.

Prior capture source `84af413…`; activation `7a8092f…`; manifest digest
`00d4636…`; pre-CI successor `0dee3af…`; final docs tip `d02ed41…`. This
evidence remains truthful historical proof, but is superseded and is not current
Task35 source, capture, hosted CI, or container proof.

### SQLite-permission capture and hosted baselines (superseded)

- Prior capture source:
  `a8057a8e0c29bdcc95e35949819c64005e5ee064`
- Prior evidence activation:
  `9de054e5131ad3f610902d0a7bd4bd97c5968c7a`
- Prior manifest digest:
  `a5fdc8adc9788f181ace5f4de9cce7974344af02312cef2caa9e123193744503`
  across 84 runtime paths.
- The prior local exact gate on the pre-CI documentation successor
  `c6c60d4354eba7348aef3245d19e787661544fa7` covered 29 capture contracts,
  `capture_manifest_valid`, 19 web test files / 345 tests and TypeScript/Vite,
  Ruff, strict MyPy over 35 source files, 778 / 778 Python tests with 3 warnings
  in 28.51s, deterministic stub and local single-process production smokes,
  OpenAPI verification, and the history-aware secret scan.
- Prior pre-CI GitHub Actions:
  [run 30588357735](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30588357735),
  including `verify`
  ([job 91024909140](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30588357735/job/91024909140))
  and `container-smoke`
  ([job 91025281220](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30588357735/job/91025281220)).
- Both pre-CI jobs passed and both pre-CI job annotation APIs returned `[]`.
- Prior final documentation tip:
  `abdf644b4c35ecfaee2d22917c4c621f9d123a5b`
- Prior final-tip GitHub Actions:
  [run 30589435325](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30589435325),
  including `verify`
  ([job 91028250060](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30589435325/job/91028250060))
  and `container-smoke`
  ([job 91028596793](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30589435325/job/91028596793)).
- Both final-tip jobs passed and both final-tip job annotation APIs returned `[]`.
- Final-tip hosted `verify` covered `capture_manifest_valid`, 19 web test files /
  345 tests, Ruff, strict MyPy over 35 source files, 796 Python tests,
  deterministic stub smoke, OpenAPI verification, and the history-aware secret
  scan.
- Both prior `container-smoke` jobs built the packaged image with owner-only
  `/data` mode `0700`, confirmed runtime user `10001:10001`, and passed the
  offline network-none `deterministic-qa` and `deployed-readonly` profiles.

Prior capture source `a8057a8…`; activation `9de054e…`; manifest digest
`a5fdc8a…`; pre-CI successor `c6c60d4…`; final docs tip `abdf644…`. This evidence
remains truthful historical proof, but is superseded and is not current Task36
source, capture, hosted CI, or container proof.

### JSON-media capture and hosted baselines (superseded)

- Prior capture source:
  `c446f05adfb836539ddbaa74a41502034c910092`
- Prior evidence activation:
  `b4c1bcbd70ff22ce3ac2b8a1de3828b7ce691afe`
- Prior manifest digest:
  `3bb9737a483fc90e50d5a6158bf0c61c1807e6510d22c9bf6d88e142d21ff5d9`
  across 84 runtime paths.
- The prior local gate covered the 120-test client matrix, 19 web test files /
  345 tests and TypeScript/Vite, Ruff, strict MyPy over 35 source files, and 695
  Python tests with 3 warnings in 179.85s. Independent specification, quality,
  and visual reviews passed with no P0-P3 findings; 28 adversarial cases had zero
  mismatches.
- Prior pre-CI documentation successor:
  `62c37d6261257ea3800335257e43ae45ed839c47`
- Prior pre-CI GitHub Actions:
  [run 30501109833](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30501109833),
  including `verify`
  ([job 90740755548](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30501109833/job/90740755548))
  and `container-smoke`
  ([job 90741063564](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30501109833/job/90741063564)).
- Both pre-CI jobs passed and both pre-CI job annotation APIs returned `[]`.
- Prior final documentation tip:
  `3abd059087e216fb4fed613b45927f27a3af627f`
- Prior final-tip GitHub Actions:
  [run 30501426777](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30501426777),
  including `verify`
  ([job 90741727313](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30501426777/job/90741727313))
  and `container-smoke`
  ([job 90742037347](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30501426777/job/90742037347)).
- Both final-tip jobs passed and both final-tip job annotation APIs returned `[]`.
- Both prior hosted `verify` jobs covered the canonical checks, deterministic stub
  smoke, OpenAPI verification, and the history-aware secret scan. Both prior
  `container-smoke` jobs built the packaged image, confirmed runtime user
  `10001:10001`, and passed the offline network-none `deterministic-qa` and
  `deployed-readonly` profiles.

Prior capture source `c446f05…`; activation `b4c1bcb…`; manifest digest
`3bb9737…`; pre-CI successor `62c37d6…`; final docs tip `3abd059…`. This evidence
remains truthful historical proof, but is superseded and is not current Task36
source, capture, hosted CI, or container proof.

### Terminal-retry capture and hosted baselines (superseded)

- Prior capture source:
  `96543fff62bb5d0a3c0f8a0464e9a07bf9c54568`
- Prior evidence activation:
  `98dae414b3cdd36ee25d0dad3fe78257f3f4c135`
- Prior manifest digest:
  `e22c42085703fcdfaaa3f994334cc418a4e81e858187757edb5a33b8e2221e13`
  across 84 runtime paths.
- Prior pre-CI documentation successor:
  `a3e3179bafb8050598cc5512d56e0c7661318c64`
- Prior pre-CI GitHub Actions:
  [run 30499178838](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30499178838),
  including `verify`
  ([job 90734790003](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30499178838/job/90734790003))
  and `container-smoke`
  ([job 90735121608](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30499178838/job/90735121608)).
- Both pre-CI jobs passed and both pre-CI job annotation APIs returned `[]`.
- Prior final documentation tip:
  `9e19b12de072e55932d1b32e24987576284ac4f0`
- Prior final-tip GitHub Actions:
  [run 30499500934](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30499500934),
  including `verify`
  ([job 90735804461](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30499500934/job/90735804461))
  and `container-smoke`
  ([job 90736149754](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30499500934/job/90736149754)).
- Both final-tip jobs passed and both final-tip job annotation APIs returned `[]`.
- Both prior hosted `verify` jobs covered the canonical checks, deterministic stub
  smoke, OpenAPI verification, and the history-aware secret scan. Both prior
  `container-smoke` jobs built the packaged image, confirmed runtime user
  `10001:10001`, and passed the offline network-none `deterministic-qa` and
  `deployed-readonly` profiles.

Prior capture source `96543ff…`; activation `98dae41…`; manifest digest
`e22c420…`; pre-CI successor `a3e3179…`; final docs tip `9e19b12…`. This evidence
remains truthful historical proof, but is superseded and is not current Task36
source, capture, hosted CI, or container proof.

### Async SQLite capture and hosted baseline (superseded)

- Prior capture source:
  `8d0a896c753c4c60894301a20a3866bbbfa1e76f`
- Prior evidence activation:
  `cca97a8e75d52a26889d3bbb66740756041d9caf`
- Prior hosted validation successor:
  `e188202a4b0612a66503fda2b6ee1a3c89ec7b65`
- Prior GitHub Actions:
  [run 30208188300](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30208188300),
  including `verify`
  ([job 89809811619](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30208188300/job/89809811619))
  and `container-smoke`
  ([job 89809998273](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30208188300/job/89809998273)).
- Both prior jobs passed and both prior job annotation APIs returned `[]`.
- The prior local source gate included two consecutive 133-test Task29 matrices,
  19 web test files / 292 tests, Ruff, strict MyPy over 35 source files, 622
  Python tests with 1 warning, stub and local single-process production smokes,
  OpenAPI freshness, the history-aware secret scan, and diff checks.
- Prior hosted `verify` ran 29 capture contracts, `capture_manifest_valid`, 19
  web test files / 292 tests, Ruff, strict MyPy over 35 source files, 638 Python
  tests with 1 warning, deterministic stub smoke, OpenAPI verification, and the
  history-aware secret scan.
- Prior hosted `container-smoke` built the packaged Docker image, confirmed
  runtime user `10001:10001`, and passed the offline network-none
  `deterministic-qa` and `deployed-readonly` profiles.
- The prior manifest digest was
  `a1e0ecaf69926044419e29c7359102188c80550834ddb9351021aae411705054`
  across 84 runtime paths.

Prior capture source `8d0a896…`; activation `cca97a8…`; hosted successor
`e188202…`; manifest digest `a1e0eca…`. This evidence remains truthful historical
proof, but is superseded and is not current Task36 source, capture, hosted CI, or
container proof.

### Request-boundary capture and hosted baseline (superseded)

- Prior capture source:
  `8823d29d7de93d44f4843a2fa4db1adec4e452bd`
- Prior evidence activation:
  `55af1e6d68f11542b1c5cc5e3465b87dc158ec08`
- Prior hosted validation successor:
  `117e4ebe40efea36f89bbb737143de9c918f938f`
- Prior GitHub Actions:
  [run 30182741263](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30182741263),
  including `verify`
  ([job 89742014836](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30182741263/job/89742014836))
  and `container-smoke`
  ([job 89742159778](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30182741263/job/89742159778)).
- Both prior jobs passed and both job annotation APIs returned `[]`.
- Prior hosted `verify` ran 29 capture contracts, `capture_manifest_valid`, 19 web
  test files / 292 tests, Ruff, strict MyPy over 34 source files, 568 Python tests
  with 1 warning, stub smoke, OpenAPI verification, and the history-aware secret
  scan.
- Prior hosted `container-smoke` built the packaged Docker image, confirmed image
  user `10001:10001`, and passed the offline network-none `deterministic-qa` and
  `deployed-readonly` profiles.
- The prior manifest digest was
  `4ad336eaf2d304906e939fc0eb433c6633519e73d9855207ae72c4daea42eed2`
  across 83 runtime paths.

Prior capture source `8823d29…`; activation `55af1e6…`; hosted successor
`117e4eb…`. This evidence remains truthful historical proof, but is superseded and
is not current Task36 source, capture, hosted CI, or container proof.

### Immediate prior capture and hosted baseline (superseded)

- Prior capture source:
  `742e3caf2af5a9cce3cd8de242cf113424e8528f`
- Prior evidence activation:
  `4a317e563c8d45bc45f676e465b01780b2b0be78`
- Prior hosted validation successor:
  `1289b773abe92bb2f5842f77e3c7f50c432352f4`
- Prior GitHub Actions:
  [run 30174822102](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30174822102),
  including `verify`
  ([job 89721793096](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30174822102/job/89721793096))
  and `container-smoke`
  ([job 89721989116](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30174822102/job/89721989116)).
- Both prior jobs passed and both job annotation APIs returned `[]`.
- The prior local exact-parent gate on `1289b77…` covered 29 capture contracts,
  offline manifest verification, 292 web tests, 550 Python tests, TypeScript/Vite,
  Ruff, strict MyPy over 34 source files, stub smoke, local
  deterministic-production and deployed-readonly smokes, OpenAPI freshness,
  history-aware secret scan, Node syntax, and diff checks.
- Prior hosted `verify` ran 29 capture contracts, `capture_manifest_valid`, 19 web
  test files / 292 tests, Ruff, strict MyPy over 34 source files, 550 Python tests,
  stub smoke, OpenAPI verification, and the history-aware secret scan.
- Prior hosted `container-smoke` built the packaged Docker image, confirmed image
  user `10001:10001`, and passed two offline packaged smoke profiles:
  `deterministic-qa` and `deployed-readonly`.
- The prior manifest digest was
  `1bde9788d551e5f76b895b977ad69291c6b1f44d26095b242d531f1bf289418c`
  across 83 runtime paths.

Prior capture source `742e3ca…`; activation `4a317e5…`; hosted successor
`1289b77…`. This evidence remains truthful historical proof, but is superseded and
is not current Task36 source, capture, hosted CI, or container proof.

### Prior capture and hosted baseline (superseded)

- Prior capture source:
  `7d9128a8171ddb4978d8f7b0debb9effce997e27`
- Prior evidence activation:
  `576ba5e3dfd3d13b9f797c1c516c2352c4e40688`
- Prior GitHub Actions:
  [run 30162644778](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30162644778),
  including `verify`
  ([job 89690352979](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30162644778/job/89690352979))
  and `container-smoke`
  ([job 89690513333](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30162644778/job/89690513333)).
- The prior local exact-tree gate covered 29 capture contracts, offline manifest
  verification, 292 web tests, 534 Python tests, Vite build, Ruff, strict MyPy over
  34 source files, stub smoke, local one-process production/SSE/SIGTERM smoke,
  OpenAPI freshness, history-aware secret scan, Node syntax, and diff checks.

Prior capture source `7d9128a…`; activation `576ba5e…`. This evidence is retained only
as a superseded prior capture/hosted baseline and is not current Task36 proof.

### Older hosted baseline (superseded)

- Older activation:
  `b57868005a3fe0869136f54472ee0098035a9099`
- Older GitHub Actions:
  [run 30140554792](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30140554792),
  including `verify`
  ([job 89632837699](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30140554792/job/89632837699))
  and `container-smoke`
  ([job 89633002245](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30140554792/job/89633002245)).

This older run is retained only as explicitly superseded historical hosted evidence.
It is not the current manifest, capture source, activation, or hosted-CI truth.

Update this matrix only after the corresponding same-SHA command or external gate is
actually observed.
