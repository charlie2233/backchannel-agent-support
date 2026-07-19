from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from server.models import ApprovalDecisionRequest, ExecutionMode
from server.orchestrator import RecoveryOrchestrator
from server.providers.hotel_simulator import HotelSimulator
from server.store import ApprovalDecisionError, SQLiteStore


def test_approve_and_decline_race_twenty_times_with_one_truthful_winner(
    tmp_path,
) -> None:
    database_path = tmp_path / "approve-decline-races.sqlite3"
    creator_store = SQLiteStore(database_path)
    creator_provider = HotelSimulator(store=creator_store)
    creator = RecoveryOrchestrator(
        store=creator_store,
        hotel_provider=creator_provider,
    )
    first_store = SQLiteStore(database_path)
    second_store = SQLiteStore(database_path)
    first_provider = HotelSimulator(store=first_store)
    second_provider = HotelSimulator(store=second_store)
    first = RecoveryOrchestrator(store=first_store, hotel_provider=first_provider)
    second = RecoveryOrchestrator(store=second_store, hotel_provider=second_provider)

    for iteration in range(20):
        pending = asyncio.run(
            creator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
        )
        recovery_id = pending.recovery.recovery_id
        approval = pending.recovery.pending_approval
        assert approval is not None
        barrier = Barrier(2)

        def submit(orchestrator, decision: str, decision_id: str):
            request = ApprovalDecisionRequest(
                decision=decision,
                clientDecisionId=decision_id,
                remedyId=approval.remedy_id,
                remedyDigest=approval.remedy_digest,
                toolCallId=approval.tool_call_id,
            )
            barrier.wait(timeout=10)
            try:
                response = asyncio.run(orchestrator.decide(recovery_id, request))
            except ApprovalDecisionError as error:
                return error.code, None
            return "won", response

        before_dispatches = first_provider.dispatch_count + second_provider.dispatch_count
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(submit, first, "approve", f"approve-{iteration}"),
                executor.submit(submit, second, "decline", f"decline-{iteration}"),
            ]
            outcomes = [future.result(timeout=20) for future in futures]

        assert sorted(code for code, _response in outcomes) == ["already_decided", "won"]
        winner = next(response for code, response in outcomes if code == "won")
        assert winner is not None
        verifier = SQLiteStore(database_path)
        receipt = verifier.get_receipt(recovery_id)
        terminal = [
            event for event in verifier.list_events(recovery_id) if event.terminal
        ]
        assert verifier.count_decisions(recovery_id) == 1
        assert len(terminal) == 1
        assert verifier.get_permission_scope(recovery_id).status == "revoked"
        dispatch_delta = (
            first_provider.dispatch_count
            + second_provider.dispatch_count
            - before_dispatches
        )
        if winner.decision == "approve":
            assert winner.status == "completed"
            assert receipt.decision == "approved"
            assert verifier.count_executions(recovery_id) == 1
            assert dispatch_delta == 1
        else:
            assert winner.status == "closed_without_action"
            assert receipt.decision == "declined"
            assert verifier.count_executions(recovery_id) == 0
            assert dispatch_delta == 0
        verifier.close()
