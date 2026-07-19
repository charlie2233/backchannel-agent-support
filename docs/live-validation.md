# Live OpenAI validation

Status as of 2026-07-19: **BLOCKED — `OPENAI_API_KEY` was not present in the command
process.** No real OpenAI request was attempted, and SDK-stub evidence was not substituted for
the live gate.

## Commands and observed results

`npm run smoke:live` exited 2 with one redacted record:

```json
{"approvalCount":0,"elapsedMs":0,"errorClass":"MissingOpenAIAPIKey","modelIds":[],"orderedToolNames":[],"status":"blocked","traceId":null}
```

`npm run smoke:live:3` launched exactly three independent child processes. Each child exited
before orchestration or temporary-database creation because the key was absent; when live-ready,
each child creates its own temporary SQLite database:

```json
{"approvalCount":0,"elapsedMs":0,"errorClass":"MissingOpenAIAPIKey","modelIds":[],"orderedToolNames":[],"status":"blocked","traceId":null}
{"approvalCount":0,"elapsedMs":0,"errorClass":"MissingOpenAIAPIKey","modelIds":[],"orderedToolNames":[],"status":"blocked","traceId":null}
{"approvalCount":0,"elapsedMs":0,"errorClass":"MissingOpenAIAPIKey","modelIds":[],"orderedToolNames":[],"status":"blocked","traceId":null}
```

The single-run and three-run commands intentionally return exit code 2 for a blocked external
gate, 1 for a local contract failure, and 0 only for verified live success.

## What is locally verified without a key

- A live-ready API request runs two narrow `gpt-5.6-luna` structured-output agents followed by
  one `gpt-5.6-terra` broker.
- The broker is configured to call `commit_remedy` with parallel tool calls disabled. Provider
  dispatch remains at zero until the exact SDK interruption is approved.
- The three initial model calls share one explicit Agents SDK root trace. Trace model/tool input
  and output data is disabled, and trace metadata is restricted to a fixed non-sensitive
  allowlist.
- The selected model names are persisted only after the corresponding model calls succeed. In
  Agents SDK 0.18.3, `ModelResponse` does not expose the provider response's raw `model` field, so
  the receipt records the successfully invoked agent model selections rather than claiming a raw
  response echo.
- Serialized approval state, model IDs, root trace ID, SDK/protocol/graph versions, and the
  prompt-tool-schema digest survive a fresh store and orchestrator. Crash reconciliation reads
  this durable provenance and does not relabel a live execution as SDK stub.
- All provider work remains inside the simulated hotel adapter. A passing live smoke would prove
  OpenAI model calls plus the approval flow, not a real booking or payment change.

## Trace continuity boundary

The consumer, provider, and preapproval broker calls are wrapped in one explicit trace scope and
are locally proven to share one root. On approval resume, the durable root ID is supplied again in
the public `RunConfig` contract. Agents SDK 0.18.3 does not provide a documented public guarantee
that a serialized run resumed in a different operating-system process will append to the same
already-exported backend trace object. Therefore the current process-restart test proves durable
correlation and receipt provenance, but it is not claimed as external proof of one continuous
backend trace across an OS restart.

## Re-running the external gate

Provide `OPENAI_API_KEY` only in the command process, then run:

```bash
npm run smoke:live
npm run smoke:live:3
```

The scripts set `OPENAI_AGENTS_DONT_LOG_MODEL_DATA=1` and
`OPENAI_AGENTS_DONT_LOG_TOOL_DATA=1` before importing the SDK. Output is limited to status,
elapsed milliseconds, model IDs, ordered tool names, approval count, trace ID, and a redacted
error class. It never prints the key, prompts, tool arguments, serialized run state, or exception
text. Auth, quota, connectivity, or model-access errors remain **BLOCKED** under their exception
class when it is in the parent's fixed allowlist; unknown child error classes are normalized to
`LiveSmokeChildProtocolError` and are never replaced with stub results. The three-run command
returns success only when all three complete proof records carry distinct valid live trace IDs.
