"""Tracing policy for the keyless deterministic SDK lane."""

from agents import RunConfig, set_trace_provider
from agents.tracing.provider import DefaultTraceProvider

_STUB_TRACE_PROVIDER = DefaultTraceProvider()


def configure_sdk_stub_tracing() -> RunConfig:
    """Prevent a keyless stub run or its direct SDK resume from exporting traces."""

    # The resumed call is intentionally the public `Runner.run(agent, state)` shape, so it
    # cannot depend on callers remembering an extra RunConfig. A provider with no processors
    # keeps both halves inside the SDK while guaranteeing that this lane exports nothing.
    set_trace_provider(_STUB_TRACE_PROVIDER)
    return RunConfig(
        tracing_disabled=True,
        workflow_name="Backchannel deterministic hotel recovery",
    )
