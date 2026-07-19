"""Tracing policy for the keyless deterministic SDK lane."""

from agents import RunConfig


def configure_sdk_stub_tracing() -> RunConfig:
    """Disable tracing for one explicit keyless SDK run."""

    return RunConfig(
        tracing_disabled=True,
        workflow_name="Backchannel deterministic hotel recovery",
    )
