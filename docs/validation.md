# Validation matrix

Proof is recorded per commit and per lane. A green source gate is not a browser,
container, live OpenAI, deployment, or release claim.

This file records only `codex/backchannel-v0.3` evidence. It does not import or claim
results from any sibling branch.

## Last exact published evidence baseline

- Commit: `9bda860a45efaf7ecc061810cbbc54562cc9c338`
- GitHub Actions: [run 29868045880](https://github.com/charlie2233/backchannel-agent-support/actions/runs/29868045880)
- Result: `verify` ([job 88761073597](https://github.com/charlie2233/backchannel-agent-support/actions/runs/29868045880/job/88761073597))
  and `container-smoke` ([job 88761454403](https://github.com/charlie2233/backchannel-agent-support/actions/runs/29868045880/job/88761454403))
  passed on the exact commit.
- Local frozen-tree gate before commit: 17 capture contracts, 198 web tests,
  465 Python tests, Vite build, Ruff, strict MyPy, stub smoke, local single-process
  production/SSE/SIGTERM smoke, OpenAPI freshness, history-aware secret scan,
  Node syntax, and diff checks passed.
- `npm run capture:judge` passed on that frozen tree in Google Chrome and produced
  the six tracked PNGs at 1440×1024 and 390×844. No tracked file changed between
  the successful capture and commit `9bda860…`.

## Per-SHA proof matrix

| Lane | Exact evidence | Status and boundary |
| --- | --- | --- |
| Local source and tests | Frozen-tree gate above | **Verified** on `9bda860…`; not external proof |
| Local final-build captures | `npm run capture:judge`; six provenance-checked PNGs tracked under [`docs/assets/final`](assets/final) | **Verified locally** on `9bda860…` in Google Chrome at 1440×1024 and 390×844; not container, deployment, live OpenAI, or release proof |
| Live OpenAI | [`live-validation.md`](live-validation.md) records one redacted invalid-key result; three-run not attempted | **Blocked / Unverified**; no valid live trace or three consecutive successes |
| Local Docker | No Docker-family runtime is installed on this Mac | **Unverified locally** |
| GitHub CI/container | Run 29868045880, `verify` and packaged `container-smoke` | **Verified** on `9bda860…`; includes image build, non-root identity, and two offline packaged smoke profiles, not a public deployment |
| Public deployment | No public application origin | **Blocked / Unverified** |
| Tag/release | No release tag or GitHub release | **Blocked / Unverified** |

## Hosted container evidence boundary

The GitHub container job proves image build, non-root identity, and two offline
packaged smoke profiles, then cleans up. It does not prove local Docker, long-lived
`/data` persistence, target-host durability/networking, abrupt host-loss or backup
restoration, concurrent multi-container SQLite, or a public browser origin.

## Capture contract

The final-build runner deliberately omits `OPENAI_API_KEY`, generates a never-logged
HMAC secret, starts one loopback process with a fresh SQLite database, and uses Google
Chrome at 1440×1024 and 390×844. It validates API and DOM provenance before each
viewport screenshot. The six images tracked by `9bda860…` prove only the local
keyless SDK-stub final build; they do not prove live OpenAI, a public deployment, a
container, a release, or a real provider mutation.

A later documentation-only successor does not inherit a new browser claim. Keep the
capture lane bound to `9bda860…` unless `npm run capture:judge` is observed again on a
different frozen tree.

## Creation-admission proof boundary

The source contract has a separate durable UTC-day creation ledger with session, IP,
and global limits. Its tests cover atomic race admission, rollback on an injected
second/third write failure, validation-before-charge, live-ledger separation, reset
and restart persistence, bounded retention, and the generic `429`/`Retry-After`
contract. This source/test evidence does not change the final-build capture lane from
`9bda860…` and is not public deployment, live OpenAI, container-host durability, tag,
or release proof.

Update this matrix only after the corresponding same-SHA command or external gate is
actually observed.
