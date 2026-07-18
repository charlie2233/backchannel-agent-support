# Backchannel v0.3 design contract

These four generated concepts are the visual target for the operational demo. They are design references, not runtime evidence:

- `desktop-consent.png` — hotel recovery paused at digest-bound human approval.
- `mobile-consent.png` — the same approval state at a 390 px viewport.
- `desktop-completed.png` — a completed hotel recovery with provenance and receipt evidence.
- `desktop-cancelled.png` — a declined remedy closed without provider dispatch.

The final screenshots for submission must be captured from the running application. They must not reuse these concept images.

## Product surface

Backchannel is one operational console, not a marketing page. At 1440 px it uses a 64 px top bar, a 248 px scenario rail, a fluid timeline workspace, and a 400 px evidence inspector. Sections use thin separators and restrained surfaces rather than stacks of decorative cards.

Exactly two scenarios are selectable:

1. **Hotel booking recovery** — live OpenAI or SDK-stub execution with human approval.
2. **API quota recovery** — deterministic stub/replay only, inside delegated authority and without human approval.

The lifecycle is always shown in this order:

1. Detect
2. Prove
3. Negotiate
4. Authorize
5. Execute
6. Verify & seal

## Runtime provenance copy

The interface derives provenance from `/health` and the recovery snapshot or receipt. It never infers a model run merely because an API responded.

| Execution mode | Visible label | Required explanation |
| --- | --- | --- |
| `openai_live` | OpenAI live | Live OpenAI model orchestration against a demo provider adapter. No real hotel booking or payment. |
| `sdk_stub` | SDK stub | Deterministic transport and approval QA. No OpenAI model call. Demo adapter only. |
| `replay_fixture` | Replay fixture | Bundled deterministic protocol trace. No model call or provider execution. |

“GPT-5.6 agents” is allowed only when health reports `backend=openai` and `liveReady=true`, while the active snapshot or receipt records `executionMode=openai_live`. Model identifiers shown in the UI come from the run record; they are never hard-coded as runtime claims.

Unsupported decorative metrics and broad claims are omitted. In particular, do not show “12 providers reachable,” “91% less context,” “18 min human support,” “production ready,” “tamper proof,” “immutable,” “exactly once,” “secure,” or “autonomous” unless a shipped measurement or guarantee supports the exact wording.

## Consent inspector

The pending state uses the title **Approve exact remedy** and displays:

- exact remedy terms and cost delta;
- shortened `sha256:` remedy digest with a copy affordance;
- expiry in UTC;
- hard-constraint result;
- delegated-authority result;
- pending tool-call ID;
- a clear statement that execution has not begun.

Actions are **Decline** and **Approve remedy**. Neither action receives initial keyboard focus. The server is authoritative: the client shows a submitting state and does not optimistically render a terminal result.

## Receipt states

The completed receipt shows status, recovery ID, execution mode, model IDs (or “None — no model call”), root trace ID, authorization source, approved digest, immediate pre-execution digest match, provider result, verification results, permission revocation, protocol/agent/SDK versions, and prompt-tool schema hash.

The declined receipt uses **Closed without action** and proves:

1. Human consent requested.
2. Remedy declined by operator.
3. Exact interruption rejected.
4. No replacement action selected.
5. Temporary permission revoked.
6. Cancellation receipt sealed.

It states `executionCount = 0` and “Provider dispatch did not begin.” If dispatch might have begun, the product must use an `outcome_unknown` state instead of claiming cancellation.

Replay receipts remain visibly simulated and must not imply model calls or provider execution.

## Responsive behavior

At widths below 760 px the permanent rail and inspector become a scenario selector and a full-screen consent/evidence sheet. The order is: app bar, scenario selector, provenance strip, current step (“Step 4 of 6”), timeline, and evidence disclosure. Approval actions stay in a safe-area-aware sticky footer. Technical evidence is collapsed behind a semantic disclosure after the essential terms.

## Visual system

| Token | Value |
| --- | --- |
| App background | `#f7f9fc` |
| Surface | `#ffffff` |
| Primary text | `#101828` |
| Muted text | `#475467` |
| Border | `#d0d5dd` |
| Accent | `#175cd3` |
| Success | `#067647` |
| Warning | `#854a0e` |
| Danger | `#b42318` |

Use Inter when available and the system sans stack otherwise. IDs, digests, and JSON use the system monospace stack. Corners are modest (4–8 px); shadows are limited to modal/sheet separation. Interface icons are small inline SVGs or CSS shapes with visible text labels, never emoji.

## Accessibility contract

- Interactive targets are at least 44 by 44 CSS pixels.
- Keyboard focus is always visible and follows document order.
- The lifecycle is an ordered list; evidence uses definition lists or semantic tables.
- Status changes use a polite live region; failed decisions use an alert.
- Color is never the only status signal.
- Motion honors `prefers-reduced-motion`.
- The consent sheet traps focus while open and restores focus to its trigger when closed.

## Final-build fidelity ledger

Before release, compare the final desktop and mobile captures with these concepts and record:

- what matched (layout, density, provenance strip, lifecycle, evidence hierarchy);
- what intentionally changed to reflect real API data or accessibility constraints;
- what remains different and whether it affects judging or trust.
