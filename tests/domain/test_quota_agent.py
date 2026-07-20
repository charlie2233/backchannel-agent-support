import asyncio

from agents import Runner
from agents.tool import FunctionTool

from server.agents.quota_stub import (
    QUOTA_START_PROMPT,
    QuotaAgentContext,
    build_quota_agent,
    quota_definition_digest,
)
from server.agents.tracing import configure_quota_sdk_stub_tracing
from server.providers.quota_simulator import QuotaSimulator


def test_quota_agent_runs_one_nonapproval_tool_and_revokes_its_scope() -> None:
    provider = QuotaSimulator()
    context = QuotaAgentContext(
        recovery_id="quota-agent-recovery",
        quota_provider=provider,
        permission_scope_id="quota-agent-scope",
    )
    agent = build_quota_agent(context=context)

    assert len(agent.tools) == 1
    tool = agent.tools[0]
    assert isinstance(tool, FunctionTool)
    assert tool.needs_approval is False

    result = asyncio.run(
        Runner.run(
            agent,
            input=QUOTA_START_PROMPT,
            context=context,
            run_config=configure_quota_sdk_stub_tracing(),
        )
    )

    assert result.interruptions == []
    assert context.dispatch_result is not None
    assert context.dispatch_result.status == "verified"
    assert context.tool_call_id == "grant-quota-quota-agent-recovery"
    assert provider.dispatch_count == 1
    assert provider.active_permission_count == 0
    assert provider.was_permission_revoked("quota-agent-scope") is True


def test_quota_agent_definition_digest_is_stable_and_complete() -> None:
    context = QuotaAgentContext(
        recovery_id="quota-agent-digest",
        quota_provider=QuotaSimulator(),
        permission_scope_id="quota-agent-digest-scope",
    )
    agent = build_quota_agent(context=context)

    first = quota_definition_digest(agent)
    second = quota_definition_digest(build_quota_agent(context=context))

    assert first == second
    assert len(first) == 64
    assert set(first) <= set("0123456789abcdef")
