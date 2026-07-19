"""Mode-specific tracing policy for deterministic and live SDK runs."""

from agents import RunConfig
from agents.models.interface import ModelProvider

from server.config import APPROVAL_PROTOCOL_VERSION, LIVE_HOTEL_AGENT_GRAPH_VERSION

LIVE_WORKFLOW_NAME = "Backchannel live hotel recovery"
LIVE_TRACE_METADATA_KEYS = frozenset(
    {
        "agentGraphVersion",
        "executionMode",
        "protocolVersion",
        "providerBoundary",
        "scenarioId",
    }
)


def configure_sdk_stub_tracing() -> RunConfig:
    """Disable tracing for one explicit keyless SDK run."""

    return RunConfig(
        tracing_disabled=True,
        workflow_name="Backchannel deterministic hotel recovery",
    )


def live_trace_metadata() -> dict[str, str]:
    """Return the fixed non-sensitive metadata allowlist for the live workflow."""

    metadata = {
        "agentGraphVersion": LIVE_HOTEL_AGENT_GRAPH_VERSION,
        "executionMode": "openai_live",
        "protocolVersion": APPROVAL_PROTOCOL_VERSION,
        "providerBoundary": "demo_adapter_only",
        "scenarioId": "hotel",
    }
    if frozenset(metadata) != LIVE_TRACE_METADATA_KEYS:
        raise RuntimeError("Live trace metadata escaped its allowlist")
    return metadata


def configure_live_tracing(
    *,
    recovery_id: str,
    root_trace_id: str,
    model_provider: ModelProvider | None = None,
) -> RunConfig:
    """Build one shared redacted RunConfig for all live agent calls."""

    if model_provider is None:
        return RunConfig(
            trace_include_sensitive_data=False,
            workflow_name=LIVE_WORKFLOW_NAME,
            trace_id=root_trace_id,
            group_id=recovery_id,
            trace_metadata=live_trace_metadata(),
        )
    return RunConfig(
        model_provider=model_provider,
        trace_include_sensitive_data=False,
        workflow_name=LIVE_WORKFLOW_NAME,
        trace_id=root_trace_id,
        group_id=recovery_id,
        trace_metadata=live_trace_metadata(),
    )
