"""Tracing policy for deterministic and OpenAI live lanes."""

from agents import ModelProvider, RunConfig


def configure_sdk_stub_tracing() -> RunConfig:
    """Disable tracing for one explicit keyless SDK run."""

    return RunConfig(
        tracing_disabled=True,
        workflow_name="Backchannel deterministic hotel recovery",
    )


def configure_openai_live_tracing(*, model_provider: ModelProvider) -> RunConfig:
    """Use Agent-owned models and omit all model/tool payloads from spans."""

    return RunConfig(
        model=None,
        model_provider=model_provider,
        tracing_disabled=False,
        trace_include_sensitive_data=False,
        workflow_name="Backchannel live hotel recovery",
    )
