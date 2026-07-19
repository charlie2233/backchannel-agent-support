from agents.models.interface import Model, ModelProvider

from server.agents.tracing import (
    LIVE_TRACE_METADATA_KEYS,
    LIVE_WORKFLOW_NAME,
    configure_live_tracing,
)
from server.config import APPROVAL_PROTOCOL_VERSION, LIVE_HOTEL_AGENT_GRAPH_VERSION


class UnusedProvider(ModelProvider):
    def get_model(self, model_name: str | None) -> Model:
        del model_name
        raise AssertionError("Trace metadata tests do not resolve a model")


def test_live_run_config_has_one_allowlisted_redacted_trace_contract() -> None:
    provider = UnusedProvider()
    recovery_id = "11111111-2222-4333-8444-555555555555"
    root_trace_id = "trace_0123456789abcdef0123456789abcdef"

    run_config = configure_live_tracing(
        recovery_id=recovery_id,
        root_trace_id=root_trace_id,
        model_provider=provider,
    )

    assert run_config.tracing_disabled is False
    assert run_config.trace_include_sensitive_data is False
    assert run_config.workflow_name == LIVE_WORKFLOW_NAME
    assert run_config.trace_id == root_trace_id
    assert run_config.group_id == recovery_id
    assert run_config.model_provider is provider
    assert run_config.trace_metadata == {
        "agentGraphVersion": LIVE_HOTEL_AGENT_GRAPH_VERSION,
        "executionMode": "openai_live",
        "protocolVersion": APPROVAL_PROTOCOL_VERSION,
        "providerBoundary": "demo_adapter_only",
        "scenarioId": "hotel",
    }
    assert frozenset(run_config.trace_metadata) == LIVE_TRACE_METADATA_KEYS
    serialized = repr(run_config.trace_metadata).lower()
    assert "booking" not in serialized
    assert "prompt" not in serialized
    assert "key" not in serialized
