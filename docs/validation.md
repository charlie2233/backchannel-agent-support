# Validation matrix

Proof is recorded per commit and per lane. A green source gate is not a browser,
container, live OpenAI, deployment, or release claim.

This file records only `codex/backchannel-v0.3` evidence. It does not import or claim
results from any sibling branch.

## Last exact published evidence baseline

- Capture source / frozen runtime:
  `85e1e8ec9147242adca311c4ba10ea8c1c3008dc`
- Evidence activation and canonical CI:
  `b57868005a3fe0869136f54472ee0098035a9099`
- GitHub Actions: [run 30140554792](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30140554792)
- Result: `verify` ([job 89632837699](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30140554792/job/89632837699))
  and `container-smoke` ([job 89633002245](https://github.com/charlie2233/backchannel-agent-support/actions/runs/30140554792/job/89633002245))
  passed on the exact activation commit.
- Local exact-tree pre-activation gate covered 28 capture contracts, offline
  manifest verification, 224 web tests, 530 Python tests, Vite build, Ruff,
  strict MyPy over 34 source files, stub smoke, local one-process
  production/SSE/SIGTERM smoke, OpenAPI freshness, history-aware secret scan,
  Node syntax, and diff checks.
- Hosted exact-activation `verify` covered the canonical `npm run check`,
  deterministic stub smoke, OpenAPI artifact verification, and the
  history-aware secret scan; `container-smoke` separately covered the
  packaged image build, non-root identity, and two offline packaged smoke
  profiles.
- `npm run capture:judge` passed against the clean capture source in
  Google Chrome 150.0.7871.186 and produced the six manifest-bound PNGs at
  1440×1024 and 390×844. The activation successor added that evidence and made
  offline capture verification canonical in `npm run check`.

## Per-SHA proof matrix

| Lane | Exact evidence | Status and boundary |
| --- | --- | --- |
| Local source and tests | Canonical gate above | **Verified** on activation `b578680…`; not external proof |
| Local final-build captures | [`docs/assets/final/manifest.json`](assets/final/manifest.json); six provenance-checked PNGs under [`docs/assets/final`](assets/final) | **Verified locally** on capture source `85e1e8e…` in Google Chrome at 1440×1024 and 390×844; not container, deployment, live OpenAI, or release proof |
| Live OpenAI | [`live-validation.md`](live-validation.md) records only a redacted invalid-key result | **Blocked / Unverified**; no valid `OPENAI_API_KEY`, one-run trace, or three consecutive successes |
| Local Docker | No Docker-family runtime is installed on this Mac | **Unverified locally** |
| GitHub CI/container | Run 30140554792, `verify` and packaged `container-smoke` | **Verified** on activation `b578680…`; includes image build, non-root identity, and two offline packaged smoke profiles, not browser capture execution or a public deployment |
| Public deployment | No public application origin | **Blocked / Unverified** |
| Tag/release | No release tag or GitHub release | **Blocked / Unverified** |

## Hosted container evidence boundary

The GitHub container job proves image build, non-root identity, and two offline
packaged smoke profiles, then cleans up. It does not prove local Docker, long-lived
`/data` persistence, target-host durability/networking, abrupt host-loss or backup
restoration, concurrent multi-container SQLite, or a public browser origin.

## Capture contract

The final-build runner deliberately omits `OPENAI_API_KEY`, generates a never-logged
HMAC secret, starts one loopback process with a fresh SQLite database, and uses
Google Chrome 150.0.7871.186 on the stable `chrome` channel in headless mode. It
validates API and DOM provenance before each viewport screenshot.

The tracked [`docs/assets/final/manifest.json`](assets/final/manifest.json) is schema
v1 and binds the full capture source SHA above, runtime SHA-256 digest
`eeb6b87ce35ad04cb4c53d048c39fcbc8caf33bb62e142526e0f9aba544afb30`
across 81 runtime paths, capture profile `keyless_sdk_stub`, environment
`en-US` / `UTC` / `reduce` / `light`, and all six artifact hashes, states, and
dimensions.

Capture source `85e1e8e…`; activation and canonical CI `b578680…`. The browser
capture was produced against the clean source SHA; the activation successor added
the captured files and made offline manifest verification part of `npm run check`.
Run 30140554792 validates the activation SHA. CI did not execute the browser capture.
The six images prove only the local keyless SDK-stub final build; they do not prove
live OpenAI, a public deployment, a container, a release, or a real provider
mutation.

A later documentation-only successor does not inherit a new browser claim. Keep the
capture lane bound to `85e1e8e…` unless `npm run capture:judge` is observed again on a
different clean frozen tree.

## Creation-admission proof boundary

The source contract has a separate durable UTC-day creation ledger with session, IP,
and global limits. Its tests cover atomic race admission, rollback on an injected
second/third write failure, validation-before-charge, live-ledger separation, reset
and restart persistence, bounded retention, and the generic `429`/`Retry-After`
contract. This source/test evidence does not change the final-build capture lane from
`85e1e8e…` and is not public deployment, live OpenAI, container-host durability, tag,
or release proof.

## Live deadline proof boundary

The source contract bounds the complete live pre-approval graph and each live
decision resume with a configurable 1..300-second cooperative deadline. Focused tests
cover one shared graph budget, external cancellation propagation, no partial recovery,
route-gate release, retained admission charges, exact public `504 live_timeout`
envelopes, retryable approve/decline claims, and no duplicate dispatch when provider
execution committed before the timeout. The OpenAI client and the redacted live-smoke
client use the same configured seconds with transport retries disabled. This remains
source/test evidence on the activation commit; it is not a real OpenAI trace, hard
process-termination proof, public deployment, or release claim.

Update this matrix only after the corresponding same-SHA command or external gate is
actually observed.
