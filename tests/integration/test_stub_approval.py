import asyncio

from agents import RunResult, Runner
from agents.items import ToolApprovalItem

from server.agents.schemas import CommitRemedyArguments
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

    state = pending.sdk_result.to_state()
    state.approve(interruption)
    completed_result = asyncio.run(Runner.run(pending.original_root_agent, state))

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
