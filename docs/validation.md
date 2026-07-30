# Validation matrix

Proof is recorded per commit and per lane. A green source gate is not a browser,
container, live OpenAI, deployment, or release claim.

This file records only `codex/backchannel-v0.3` evidence. It does not import or claim
results from any sibling branch.

## Current capture and evidence activation

- Capture source / frozen runtime:
  `c446f05adfb836539ddbaa74a41502034c910092`
- Evidence activation:
  `b4c1bcbd70ff22ce3ac2b8a1de3828b7ce691afe`
- `npm run capture:judge` reported `29 passed` against the clean capture source
  in Google Chrome 150.0.7871.187 and produced six manifest-bound PNGs at
  1440×1024 and 390×844. The run covered 29 capture contracts.
- Five PNGs changed and `mobile-consent.png` remained byte-identical against the
  immediately prior capture set.
- Task31 RED evidence reproduced wrong response media and two duplicate-header
  bypasses before the parser was fixed.
- Pre-capture Task31 gates on clean source
  `c446f05adfb836539ddbaa74a41502034c910092`: the focused client matrix passed
  120 tests; the broader source gate passed 19 web test files / 345 tests,
  TypeScript/Vite, and diff checks.
- After activation `b4c1bcbd70ff22ce3ac2b8a1de3828b7ce691afe`, the clean
  canonical current-tree gate passed 29 capture contracts,
  `capture_manifest_valid`, 19 web test files / 345 tests and the TypeScript/Vite
  build, Ruff, strict MyPy over 35 source files, and 695 Python tests with 3
  warnings in 179.85s. The warnings were one Starlette TestClient deprecation and
  two multiprocessing fork warnings.
- Independent Task31 review: specification and quality reviews passed with no
  P0-P3; 28 adversarial cases with zero mismatches; visual review passed with no
  P0-P3.
- Hosted validation successor:
  `62c37d6261257ea3800335257e43ae45ed839c47`
- GitHub Actions:
  [run 30501109833](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30501109833)
- Result: `verify`
  ([job 90740755548](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30501109833/job/90740755548))
  reported `PASS`; `container-smoke`
  ([job 90741063564](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30501109833/job/90741063564))
  reported `PASS`. Both job annotation APIs returned `[]`.
- Hosted `verify` passed `capture_manifest_valid`, 19 web test files / 345 tests,
  the TypeScript/Vite build, Ruff, strict MyPy over 35 source files, 695 Python
  tests with 1 warning in 43.09s, deterministic stub smoke, OpenAPI verification,
  and the history-aware secret scan.
- Hosted `container-smoke` built the packaged Docker image, confirmed runtime user
  `10001:10001`, and passed the offline network-none `deterministic-qa` and
  `deployed-readonly` profiles.

## Per-SHA proof matrix

| Lane | Exact evidence | Status and boundary |
| --- | --- | --- |
| Local source and tests | Focused/full web gates on source `c446f05…`; post-activation canonical current-tree gate above | **Verified locally** in the exact lanes described above; hosted evidence is separate and neither lane proves browser deployment, live OpenAI, or release |
| Local final-build captures | [`docs/assets/final/manifest.json`](assets/final/manifest.json); six provenance-checked PNGs under [`docs/assets/final`](assets/final) | **Verified locally** on capture source `c446f05…` in Google Chrome at 1440×1024 and 390×844; not container, deployment, live OpenAI, or release proof |
| Live OpenAI | [`live-validation.md`](live-validation.md) records only a redacted invalid-key result | **Blocked / Unverified**; no valid `OPENAI_API_KEY`, one-run trace, or three consecutive successes |
| Local Docker | No Docker-family runtime is installed on this Mac | **Unverified locally** |
| GitHub CI/container | Run 30501109833; `verify` and packaged `container-smoke` | **Verified** on hosted successor `62c37d6…`; both jobs passed with empty annotations, but this is not local Docker, browser capture, public deployment, live OpenAI, or release proof |
| Public deployment | No public application origin | **Blocked / Unverified** |
| Tag/release | No release tag or GitHub release | **Blocked / Unverified** |

## Hosted container evidence boundary

The current hosted container job proves a packaged Docker image build, runtime user
`10001:10001`, and the offline network-none `deterministic-qa` and
`deployed-readonly` profiles. It does not prove local Docker, browser capture in CI,
public deployment or browser URL, live OpenAI, real provider execution, container
replacement or restart, long-lived `/data` volume persistence, target-host
durability/networking, abrupt host-loss or backup, concurrent multi-container SQLite,
tag or release.

## Capture contract

The final-build runner deliberately omits `OPENAI_API_KEY`, generates a never-logged
HMAC secret, starts one loopback process with a fresh SQLite database, and uses
Google Chrome 150.0.7871.187 on the stable `chrome` channel in headless mode. It
validates API and DOM provenance before each viewport screenshot.

The tracked [`docs/assets/final/manifest.json`](assets/final/manifest.json) is schema
v1 and binds the full capture source SHA above, runtime SHA-256 digest
`3bb9737a483fc90e50d5a6158bf0c61c1807e6510d22c9bf6d88e142d21ff5d9`
across 84 runtime paths, capture profile `keyless_sdk_stub`, environment
`en-US` / `UTC` / `reduce` / `light`, and all six artifact hashes, states, and
dimensions.

Capture source `c446f05…`; evidence activation `b4c1bcb…`; runtime digest
`3bb9737…`. The browser capture was
produced against the clean source SHA, and the activation successor added the captured
files. CI did not execute the browser capture. The six images prove only the local keyless
SDK-stub final build; they do not prove live OpenAI, a public deployment, a container,
a release, or a real provider mutation.

A later documentation-only successor does not inherit a new browser claim. Keep the
capture lane bound to `c446f05…` unless `npm run capture:judge` is observed again on a
different clean frozen tree.

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
checkpoint; the current Task31 local gate does not promote it into current hosted or
container proof. It does not prove multi-process or concurrent multi-container SQLite,
target-host scheduling, abrupt host loss, public latency, or live provider behavior.

## Request-body availability proof boundary

The capture source rejects framed `GET` and `HEAD` requests before body reads,
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
`c446f05…` and is not public deployment, live OpenAI, container-host durability, tag,
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
remains truthful historical proof, but is superseded and is not current Task31
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
proof, but is superseded and is not current Task31 source, capture, hosted CI, or
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
is not current Task29 source, capture, hosted CI, or container proof.

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
is not current Task28 source, capture, hosted CI, or container proof.

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
as a superseded prior capture/hosted baseline and is not current Task25 proof.

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
