from agents.tracing import setup as tracing_setup

from server.agents.tracing import configure_sdk_stub_tracing


def test_stub_run_config_does_not_replace_the_global_trace_provider(monkeypatch) -> None:
    sentinel_provider = object()
    monkeypatch.setattr(tracing_setup, "GLOBAL_TRACE_PROVIDER", sentinel_provider)

    run_config = configure_sdk_stub_tracing()

    assert run_config.tracing_disabled is True
    assert tracing_setup.GLOBAL_TRACE_PROVIDER is sentinel_provider
