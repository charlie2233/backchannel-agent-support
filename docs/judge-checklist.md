# Three-minute judge checklist

## Links and access status

- **Origin URL:** no public application origin is available. Local final-build capture
  uses an ephemeral `http://127.0.0.1:<port>` owned by the runner.
- **Public-access status:** the source repository was anonymously readable on
  2026-07-20 at [github.com/charlie2233/backchannel-agent-support](https://github.com/charlie2233/backchannel-agent-support).
- **GitHub URL:** <https://github.com/charlie2233/backchannel-agent-support>
- Public demo: **Blocked / not deployed**.

Repository readability is separate from application deployment.

## Timed sequence

| Time | Judge action | Evidence to call out |
| --- | --- | --- |
| **0:00** | State the failure and six-step lifecycle. | One console turns evidence into an exact, bounded remedy. |
| **0:20** | Show the provenance strip and pending hotel recovery. | SDK stub is keyless; execution has not begun; no model IDs. |
| **0:50** | Open **Approve exact remedy** and point to terms, cost, expiry, digest, and tool call. | The decision is bound to the displayed remedy and interruption. |
| **1:30** | Approve once and show **Completed receipt**. | One local demo-provider execution, digest match, permission revoked, scope closed, and “None — no model call.” |
| **2:10** | Reset the session, start a fresh pending recovery, and decline it. | **Closed without action**, “Provider dispatch did not begin.”, and `executionCount = 0`. |
| **2:40** | Show the validation matrix and choose the evidence path below. | Measured proof is separated from simulator facts and blocked external gates. |

## Evidence paths

### Primary: live OpenAI

Run `npm run smoke:live`, then `npm run smoke:live:3`, only with a valid key in the
child environment. Report returned model IDs and the redacted trace evidence produced
by the scripts. This path is currently blocked: see the
[invalid-key blocker](live-validation.md). Do not substitute SDK-stub results for it.

### Local SDK-QA

Run `npm run capture:judge`. This is the primary available visual path: the same
Agents SDK pause/resume contract, exact human decision, local `HotelSimulator`, and
receipt invariants run without OpenAI. It creates desktop/mobile consent, completed,
and declined viewport captures from `web/dist`.

### Replay fallback

Start the built application without a key. The keyless app automatically starts a pending SDK-stub hotel recovery.
`Run replay fixture` is disabled while that recovery is pending.
Approve or decline it and wait for the terminal receipt.
Then choose **Run replay fixture**. State that the trace is recorded evidence only:
replay makes no model call and performs no runtime provider dispatch.

## Measured/tested versus simulator facts

**Measured/tested:** API/session isolation, exact consent digest, durable decision
claim and retry, restart resume, SSE cursors, receipt invariants, and final-build
DOM/API checks and PNG dimensions are test or smoke observations within their recorded
lanes. Hosted container evidence is limited to image build, non-root identity, and two
offline packaged smoke profiles.

**Simulator facts:** hotel replacement and quota changes occur only inside local
deterministic adapters. A completed SDK-stub receipt may truthfully say the local demo
provider executed once; it is not evidence that a hotel, payment processor, or quota
provider changed external state.

## Unverified external gates

- public deployment;
- live three-run proof with a valid OpenAI credential;
- target-host durability/networking;
- abrupt host-loss/backup restoration;
- concurrent multi-container SQLite;
- tag/release and GitHub release artifact;
- real provider execution, booking, payment, or quota mutation.

Do not call the demo released or publicly deployed while any corresponding row above
remains unverified.
