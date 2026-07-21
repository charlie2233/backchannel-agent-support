# Backchannel v0.3

Backchannel turns cross-party operational failures into evidence-bound remedies that cannot execute without the exact authority they require.

**Six-step lifecycle:** Detect → Prove → Negotiate → Authorize → Execute → Verify & seal

**Keyless final-build proof**

Prerequisite: install Google Chrome Stable on the capture host. From a fresh clone,
install both locked dependency sets before running the browser gate:

```bash
npm ci
uv sync --frozen --all-groups
npm run capture:judge
```

The capture command builds `web/dist`, launches one loopback-only SDK-stub process,
and writes six provenance-checked viewport captures under `docs/assets/final/`. For
the separate recorded replay fallback, start the final build without a key:

```bash
npm run build
env -u OPENAI_API_KEY BACKCHANNEL_FRONTEND_DIST_PATH=web/dist npm run start
```

The keyless app automatically starts a pending SDK-stub hotel recovery.
`Run replay fixture` is disabled while that recovery is pending.
Approve or decline it and wait for the terminal receipt.
Then choose **Run replay fixture**.

**Live proof — SDK QA first.** Run the keyless SDK-QA path above before supplying a credential to either live smoke. The live lane remains blocked until one smoke and all three repetitions return valid external evidence.

```bash
env OPENAI_API_KEY='<credential-from-secret-manager>' npm run smoke:live
env OPENAI_API_KEY='<credential-from-secret-manager>' npm run smoke:live:3
```

**Exact trust boundary:** Live mode may call OpenAI, but provider mutation remains inside the local `HotelSimulator`. SDK stub makes no OpenAI call. Replay uses no model and no runtime provider. Nothing here books a real hotel, charges a payment method, or changes real quota.

<!-- JUDGE FIRST SCREEN END -->

## Judge and operator map

- [Three-minute judge checklist](docs/judge-checklist.md)
- [Architecture and deployment boundary](docs/architecture.md)
- [Protocol, consent, durability, and SSE](docs/protocol.md)
- [Per-SHA validation matrix](docs/validation.md)
- [Live OpenAI validation](docs/live-validation.md)
- [Generated design concepts and final-build fidelity ledger](docs/design/README.md)
- [Published OpenAPI contract](docs/openapi.json)

## Development

The repository pins Node in `.node-version`, Python/uv in `pyproject.toml` and
`uv.lock`, and browser automation to `playwright-core@1.61.1`. Install exactly
the locked dependencies, then run the canonical gate:

```bash
npm ci
uv sync --frozen --all-groups
npm run check
npm run smoke:stub
npm run openapi:check
npm run secret:scan
```

`npm run check` validates the capture helper contract but does not launch Chrome.
`npm run capture:judge` is the explicit local final-build browser gate. GitHub CI
keeps browser capture out of its two bounded jobs.

## Runtime modes

| Mode | What runs | What it proves |
| --- | --- | --- |
| `openai_live` | OpenAI orchestration plus the local hotel simulator | External model evidence only after the live smoke succeeds |
| `sdk_stub` | Deterministic Agents SDK approval/resume plus local simulators | Transport, consent, durable resume, and provider-adapter behavior without OpenAI |
| `replay_fixture` | Recorded protocol events only | UI and protocol fallback; no runtime model/provider execution |

The web application is served by the same FastAPI process in final-build and
container profiles. Development CORS is limited to the listed local Vite origins;
deployed mode requires explicit HTTPS origins and an explicit signing secret.
