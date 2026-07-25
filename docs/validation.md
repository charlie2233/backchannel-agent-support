# Validation matrix

Proof is recorded per commit and per lane. A green source gate is not a browser,
container, live OpenAI, deployment, or release claim.

This file records only `codex/backchannel-v0.3` evidence. It does not import or claim
results from any sibling branch.

## Current capture and evidence activation

- Capture source / frozen runtime:
  `742e3caf2af5a9cce3cd8de242cf113424e8528f`
- Evidence activation:
  `4a317e563c8d45bc45f676e465b01780b2b0be78`
- `npm run capture:judge` reported `29 passed` against the clean capture source
  in Google Chrome 150.0.7871.186 and produced six manifest-bound PNGs at
  1440×1024 and 390×844.
- `uv run pytest -q tests/domain/test_cleanup.py` reported `15 passed` on the
  capture source.
- No Task25 hosted GitHub Actions run or job is recorded or verified in this
  document. The focused local results above are not a full canonical local gate
  and are not hosted proof for the activation.

## Per-SHA proof matrix

| Lane | Exact evidence | Status and boundary |
| --- | --- | --- |
| Local source and tests | Focused cleanup suite: 15 passed | **Verified locally** on capture source `742e3ca…`; not a full canonical gate or external proof |
| Local final-build captures | [`docs/assets/final/manifest.json`](assets/final/manifest.json); six provenance-checked PNGs under [`docs/assets/final`](assets/final) | **Verified locally** on capture source `742e3ca…` in Google Chrome at 1440×1024 and 390×844; not container, deployment, live OpenAI, or release proof |
| Live OpenAI | [`live-validation.md`](live-validation.md) records only a redacted invalid-key result | **Blocked / Unverified**; no valid `OPENAI_API_KEY`, one-run trace, or three consecutive successes |
| Local Docker | No Docker-family runtime is installed on this Mac | **Unverified locally** |
| GitHub CI/container | No Task25 Actions run or job recorded | **Blocked / Unverified** for activation `4a317e5…`; superseded prior evidence below is not inherited |
| Public deployment | No public application origin | **Blocked / Unverified** |
| Tag/release | No release tag or GitHub release | **Blocked / Unverified** |

## Hosted container evidence boundary

No current Task25 hosted container result is recorded. The superseded prior container
job below proved only its prior activation's image build and non-root identity. It
also covered two offline packaged smoke profiles. It did not prove local Docker,
long-lived `/data` persistence, target-host durability/networking, abrupt host-loss
or backup restoration, concurrent multi-container SQLite, or a public browser origin.

## Capture contract

The final-build runner deliberately omits `OPENAI_API_KEY`, generates a never-logged
HMAC secret, starts one loopback process with a fresh SQLite database, and uses
Google Chrome 150.0.7871.186 on the stable `chrome` channel in headless mode. It
validates API and DOM provenance before each viewport screenshot.

The tracked [`docs/assets/final/manifest.json`](assets/final/manifest.json) is schema
v1 and binds the full capture source SHA above, runtime SHA-256 digest
`1bde9788d551e5f76b895b977ad69291c6b1f44d26095b242d531f1bf289418c`
across 83 runtime paths, capture profile `keyless_sdk_stub`, environment
`en-US` / `UTC` / `reduce` / `light`, and all six artifact hashes, states, and
dimensions.

Capture source `742e3ca…`; evidence activation `4a317e5…`. The browser capture was
produced against the clean source SHA, and the activation successor added the captured
files. This document does not claim that CI executed the browser capture or validated
the activation because no current hosted CI run is recorded or verified. The six
images prove only the local keyless SDK-stub final build; they do not prove live
OpenAI, a public deployment, a container, a release, or a real provider mutation.

A later documentation-only successor does not inherit a new browser claim. Keep the
capture lane bound to `742e3ca…` unless `npm run capture:judge` is observed again on a
different clean frozen tree.

## Creation-admission proof boundary

The source contract has a separate durable UTC-day creation ledger with session, IP,
and global limits. Its tests cover atomic race admission, rollback on an injected
second/third write failure, validation-before-charge, live-ledger separation, reset
and restart persistence, bounded retention, and the generic `429`/`Retry-After`
contract. This source/test evidence does not change the final-build capture lane from
`742e3ca…` and is not public deployment, live OpenAI, container-host durability, tag,
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
