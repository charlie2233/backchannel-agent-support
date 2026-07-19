# Judge checklist

## Submission status

| Item | Status | Evidence boundary |
| --- | --- | --- |
| GitHub URL | **Verified** | <https://github.com/charlie2233/backchannel-agent-support> |
| Public demo URL | **UNVERIFIED / BLOCKED** | No deployment or public URL is available in this environment. |
| Live OpenAI run | **UNVERIFIED / BLOCKED** | `OPENAI_API_KEY` was absent; `npm run smoke:live` made no real OpenAI request. |
| Three-run live evidence | **UNVERIFIED / BLOCKED** | All three independent children stopped at `MissingOpenAIAPIKey`; no live trace IDs were produced. |
| Container proof | **UNVERIFIED / BLOCKED** | Docker, Podman, Colima, and OrbStack were absent; no image build or container smoke is claimed. |
| Release tag | **Not created** | `v0.3.0-build-week` must wait for applicable checks, external proof, and release authorization. |

The app remains fully judgeable through the deterministic SDK QA path and replay fallback, but
neither is presented as live-provider, container, deployment, or public-reachability evidence.

## Exact three-minute demo sequence

**0:00–0:25 — Establish the boundary.** Open the console and read the provenance strip. Point
out `providerBoundary=demo_adapter_only`: even `openai_live` never changes a real booking,
payment, or quota.

**0:25–0:55 — Show the protocol.** Select **Hotel booking recovery** and name the six fixed
steps: Detect, Prove, Negotiate, Authorize, Execute, Verify & seal. Select **Run SDK QA trace**
so the demo uses the real Agents SDK interruption/resume path without claiming a model call.

**0:55–1:35 — Inspect exact consent.** At **Approve exact remedy**, show the booking and stay,
room replacement, zero cost delta, UTC expiry, hard-constraint and delegated-authority results,
pending tool-call ID, and the shortened digest. Copy the full digest. Emphasize **Execution has
not begun**.

**1:35–2:05 — Approve and verify.** Select **Approve remedy**. Wait for the terminal SSE event
and authoritative receipt. Show `approvalCount=1`, the approved digest, immediate digest match,
one simulated provider result, verification results, revoked permission, QA root, SDK/protocol/
graph versions, and prompt-tool definition digest.

**2:05–2:35 — Prove rejection.** Start another SDK QA trace and select **Decline**. Wait for
**Closed without action**; show `executionCount = 0`, `providerExecution=false`, no approved
digest, no replacement action, revoked permission, and the sealed cancellation receipt.

**2:35–3:00 — Prove the complementary policy.** Select **API quota recovery**. Show the
deterministic zero-approval path (`approvalCount=0`), delegated limits, simulated verification,
permission revocation, and restored baseline. Close by switching back to the replay fallback and
showing that it explicitly says no model call or provider execution.

## Replay fallback sequence

1. Start `npm run start` without `OPENAI_API_KEY`.
2. The hotel rail loads `replay_fixture` and explains that live mode is unavailable.
3. Follow the persisted lifecycle and event ledger to the simulated replay receipt.
4. Confirm model IDs and root trace are absent and `providerExecution=false`.
5. Use **Run SDK QA trace** when an interactive consent/approve/decline demonstration is needed;
   keep its `sdk_stub` provenance visible.

## Claims: Measured vs. Simulated

| Claim | Classification | What the evidence supports |
| --- | --- | --- |
| Typed API, digest, restart, race, rejection, quota, SSE, accessibility, and UI contracts | **Measured locally** | Automated tests exercise these source contracts in a disposable local environment. |
| SDK QA hotel pause, approval, decline, and receipt | **Measured locally** | Actual Agents SDK runner with deterministic local model responses; no OpenAI model call. |
| Local production static/API/SSE process | **Measured locally** | `npm run smoke:production`; this is not a Docker or public-deployment result. |
| Final desktop/mobile screenshots | **Measured locally** | Generated from the final build by `npm run test:e2e`; not design concepts and not live proof. |
| Hotel booking/payment change | **Simulated** | The hotel demo adapter records a demo result only. |
| API quota grant, verification, and revocation | **Simulated** | SDK stub may execute the quota demo adapter; replay only replays recorded evidence. |
| Live OpenAI success | **Unverified external gate** | Missing `OPENAI_API_KEY`; deterministic evidence is not substituted. |
| Container, deployment, and public reachability | **Unverified external gates** | No compatible container runtime and no public demo URL were available. |

## Fidelity ledger

The checked-in files under `docs/design` are design concepts, not runtime evidence. The six
capture names below are the release contract: `npm run test:e2e` will generate them from the
final build at the exact viewport shown. Each row is a concrete comparison point.

| Concept and comparison point | Final-build capture | Viewport | Fidelity contract |
| --- | --- | --- | --- |
| `docs/design/desktop-consent.png` — 64 px app bar, 248 px rail, fluid lifecycle, 400 px consent inspector | `docs/assets/final/pending-desktop.png` | 1440×1024 | Pending SDK QA provenance, exact consent, and execution-not-started boundary stay above the fold. |
| `docs/design/desktop-completed.png` — operational shell and evidence hierarchy | `docs/assets/final/completed-desktop.png` | 1440×1024 | Authoritative completed receipt shows digest match, provider result, verification, and technical provenance. |
| `docs/design/desktop-cancelled.png` — closed-without-action hierarchy | `docs/assets/final/declined-desktop.png` | 1440×1024 | Decline proof shows zero execution, no dispatch, permission revocation, and sealed cancellation. |
| `docs/design/mobile-consent.png` — compact header, selector, provenance, and horizontal six-step lifecycle | `docs/assets/final/pending-mobile.png` | 390×844 | Native full-screen consent dialog keeps essential terms ahead of collapsed technical detail and uses a safe sticky action footer. |
| `docs/design/mobile-consent.png` — responsive evidence density | `docs/assets/final/completed-mobile.png` | 390×844 | Completed evidence remains readable without desktop rail/inspector chrome or horizontal page overflow. |
| `docs/design/mobile-consent.png` plus desktop cancellation hierarchy | `docs/assets/final/declined-mobile.png` | 390×844 | Mobile decline retains the same server-authored proof and explicit closed-without-action wording. |
| All concepts — restrained operational visual system | All six captures | Exact viewports above | Final UI keeps navy header, thin separators, semantic status text, visible focus, 44 px targets, and no unsupported decorative metrics. |

Intentional differences are limited to real server-generated IDs, UTC timestamps, digests,
event content, and accessibility behavior such as the native mobile dialog and semantic
disclosures. Those differences strengthen provenance and do not change the judging sequence.
Any capture with replay provenance in the SDK QA approval/decline set, browser console errors,
wrong dimensions, clipped controls, or unsupported claims fails this ledger.

## Before any external release

- Run the final locked local gates in `docs/validation.md` and preserve their exact output.
- Re-run `npm run smoke:live:3` only with authorized model access; require three distinct valid
  live root trace IDs before changing the live status above.
- Build and smoke the image on an available Docker-compatible runtime.
- Deploy to a long-lived SSE-capable host with persistent `/data`, then verify the public URL.
- Re-run secret and OpenAPI checks on the release tree. Do not create the tag until these gates
  are satisfied; no tag is claimed now.
