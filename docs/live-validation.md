# Live OpenAI validation

This document separates local contract proof from externally verified OpenAI calls.
Backchannel's live lane uses OpenAI models to coordinate one hotel-recovery demo, but
the only provider mutation is the local `HotelSimulator`. It never changes a real
hotel booking, charges a payment method, or contacts a hotel system.

## Safe validation contract

- Consumer and provider Agents request `gpt-5.6-luna`; the broker requests
  `gpt-5.6-terra`. These are requested aliases, not claims about the model identifier
  returned by OpenAI.
- `modelIds` contains the ordered, distinct `response.model` values actually returned
  across the initial run and approval resume.
- The three initial Agents run inside one root OpenAI trace. The approval resume may
  create a second trace lifecycle segment after a process restart, but it deliberately
  reuses the same persisted root trace ID and recovery group.
- Model input/output and tool input/output are disabled in SDK tracing and logging.
  Persisted public provenance is limited to returned model IDs, root trace ID, pinned
  SDK/protocol/graph versions, and the prompt/tool schema hash.
- The smoke commands request an SDK trace flush before exit. A successful local flush
  does not by itself prove that a remote trace dashboard accepted or indexed the trace.

The smoke scripts read `OPENAI_API_KEY` only from their child-process environment.
Their output allowlist is pass/fail, elapsed milliseconds, actual returned model IDs,
ordered interruption tool names, approval count, root trace ID, and a sanitized error
class/code. They do not print prompts, tool arguments, model output, response/request
IDs, serialized state, local database paths, or credential values.

## Local contract proof

The pinned-SDK integration tests use a test-only scripted model provider; they are not
evidence of external model access. On 2026-07-19 the following local gates passed:

- `tests/integration/test_live_contract.py`: the consumer, provider, and broker share
  one root trace; the real pinned Responses-model seam and function span are
  payload-free; approval and rejection persist the same trace ID across restart.
- `tests/domain/test_trace_metadata.py`: inherited unsafe logging flags are overridden,
  only explicit Luna/Terra aliases resolve, and `RunConfig` retains no default model
  and disables sensitive trace data.
- `tests/api/test_live_route_contract.py`: public live responses contain complete safe
  provenance and no key, serialized state, proof payload, or prompt payload.

An expected keyless preflight also produced only the redacted code
`LiveSmokeError/missing_openai_api_key`, with no model IDs, tool names, approval, or
trace ID. That preflight proves fail-closed output behavior, not live OpenAI access.

## External live results

Run from a child process containing only the intended `OPENAI_API_KEY` credential:

```bash
npm run smoke:live
npm run smoke:live:3
```

The external validation gate is blocked. A candidate credential was passed only to the
single-smoke child process on 2026-07-19; the command returned the redacted external
auth result `AuthenticationError/invalid_api_key`. Per the validation sequence, the
three-run command was not attempted after the single run failed. This auth failure is
not replaced with stub evidence.

| Run | Result | Elapsed ms | Actual returned model IDs | Tool order | Approvals | Root trace ID | Safe blocker |
| --- | --- | ---: | --- | --- | ---: | --- | --- |
| Single | Blocked | 3204 | none returned | none | 0 | `trace_8989188a27f94d20bfb7367f37149123` | `AuthenticationError/invalid_api_key` |
| Three-run 1 | Not attempted after single-run blocker | - | - | - | - | - | preceding auth blocker |
| Three-run 2 | Not attempted after single-run blocker | - | - | - | - | - | preceding auth blocker |
| Three-run 3 | Not attempted after single-run blocker | - | - | - | - | - | preceding auth blocker |
