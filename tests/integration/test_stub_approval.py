import asyncio

from agents import RunContextWrapper, Runner, RunResult, RunState
from agents.items import ToolApprovalItem

from server.agents.factory import HotelAgentContext, build_hotel_agent
from server.agents.schemas import CommitRemedyArguments, deterministic_hotel_arguments
from server.agents.tracing import configure_sdk_stub_tracing
from server.models import ExecutionMode, RecoveryStatus
from server.orchestrator import RecoveryOrchestrator
from server.providers.hotel_simulator import HotelSimulator
from server.store import SQLiteStore


def test_sdk_stub_pauses_and_resumes_through_the_agents_sdk(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "stub-approval.sqlite3")
    provider = HotelSimulator()
    orchestrator = RecoveryOrchestrator(store=store, hotel_provider=provider)

    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )

    assert isinstance(pending.sdk_result, RunResult)
    assert pending.recovery.status is RecoveryStatus.PENDING_APPROVAL
    assert len(pending.sdk_result.interruptions) == 1
    interruption = pending.sdk_result.interruptions[0]
    assert isinstance(interruption, ToolApprovalItem)
    assert interruption.tool_name == "commit_remedy"
    assert interruption.call_id is not None
    assert interruption.arguments is not None
    arguments = CommitRemedyArguments.model_validate_json(interruption.arguments)
    assert arguments.consumer_proof.booking_id
    assert arguments.provider_proof.conflict_code == "room_assignment_mismatch"
    assert arguments.remedy.cost_delta_minor == 0
    assert provider.dispatch_count == 0

    completed_result = asyncio.run(orchestrator.resume_approved(pending))

    assert completed_result.interruptions == []
    assert provider.dispatch_count == 1
    completed = store.get_recovery(pending.recovery.recovery_id)
    assert completed.status is RecoveryStatus.COMPLETED
    receipt = store.get_receipt(pending.recovery.recovery_id)
    assert receipt.execution_mode is ExecutionMode.SDK_STUB
    assert receipt.status == "completed"
    assert receipt.simulated is True
    assert receipt.provider_execution is True
    assert receipt.model_ids == []


def test_sdk_state_resumes_with_an_equivalent_fresh_agent(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fresh-agent-resume.sqlite3")
    provider = HotelSimulator()
    orchestrator = RecoveryOrchestrator(store=store, hotel_provider=provider)
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    pending_state = pending.sdk_result.to_state()
    serialized_state = pending_state.to_json(context_serializer=lambda _context: {})
    fresh_context = HotelAgentContext(
        recovery_id=pending.recovery.recovery_id,
        store=store,
        hotel_provider=provider,
    )
    fresh_agent = build_hotel_agent(
        context=fresh_context,
        arguments=deterministic_hotel_arguments(),
    )
    restored_state = asyncio.run(
        RunState.from_json(
            fresh_agent,
            serialized_state,
            context_override=RunContextWrapper(context=fresh_context),
        )
    )
    interruptions = restored_state.get_interruptions()
    assert len(interruptions) == 1
    restored_state.approve(interruptions[0])

    completed = asyncio.run(
        Runner.run(
            fresh_agent,
            restored_state,
            run_config=configure_sdk_stub_tracing(),
        )
    )

    assert completed.last_agent is fresh_agent
    assert completed.interruptions == []
    assert len(completed.raw_responses) == 2
    assert provider.dispatch_count == 1
