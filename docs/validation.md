# Validation matrix

Proof is recorded per commit and per lane. A green source gate is not a browser,
container, live OpenAI, deployment, or release claim.

This file records only `codex/backchannel-v0.3` evidence. It does not import or claim
results from any sibling branch.

## Last exact published baseline

- Commit: `6cda24b96aae07732e75850be973e4fad8765a77`
- GitHub Actions: [run 29805866404](https://github.com/charlie2233/backchannel-agent-support/actions/runs/29805866404)
- Result: `verify` passed and `container-smoke` passed on the exact commit.
- Local baseline before push: 197 web tests, 454 Python tests, Vite build, Ruff,
  strict MyPy, stub smoke, OpenAPI freshness, history-aware secret scan, and diff
  checks passed.

## Per-SHA proof matrix

| Lane | Exact evidence | Status and boundary |
| --- | --- | --- |
| Local source and tests | Task 12A baseline above | **Verified** on `6cda24b…`; not browser or external proof |
| Local final-build captures | `npm run capture:judge` will generate six provenance-checked PNGs on the Task 12B tree | **Unverified on the published baseline** until the capture is run and the resulting commit SHA is recorded |
| Live OpenAI | [`live-validation.md`](live-validation.md) records one redacted invalid-key result; three-run not attempted | **Blocked / Unverified**; no valid live trace or three consecutive successes |
| Local Docker | No Docker-family runtime is installed on this Mac | **Unverified locally** |
| GitHub CI/container | Run 29805866404, `verify` and packaged `container-smoke` | **Verified** on `6cda24b…`; includes image build, non-root identity, and two offline packaged smoke profiles, not a public deployment |
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
viewport screenshot. Those images prove only the local keyless SDK-stub final build;
they do not prove live OpenAI, a public deployment, a container, a release, or a real
provider mutation.

Update this matrix only after the corresponding same-SHA command or external gate is
actually observed.
