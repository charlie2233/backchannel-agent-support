import asyncio
import json
import sqlite3
from unittest.mock import AsyncMock

import pytest
from agents import RunState

from server.models import ApprovalDecisionRequest, ExecutionMode
from server.orchestrator import (
    RecoveryOrchestrator,
    ResumeIncompatibleError,
)
from server.providers.hotel_simulator import HotelSimulator
from server.store import ApprovalDecisionError, RecoveryNotFoundError, SQLiteStore

SENSITIVE_STATE_SENTINEL = "SENSITIVE_STATE_SENTINEL"


def approval_request(pending, decision_id: str) -> ApprovalDecisionRequest:
    approval = pending.recovery.pending_approval
    assert approval is not None
    return ApprovalDecisionRequest(
        decision="approve",
        clientDecisionId=decision_id,
        remedyId=approval.remedy_id,
        remedyDigest=approval.remedy_digest,
        toolCallId=approval.tool_call_id,
    )


@pytest.mark.parametrize(
    ("marker", "expected_code"),
    [
        ("sdk_version", "resume_incompatible"),
        ("protocol_version", "resume_incompatible"),
        ("agent_graph_version", "resume_incompatible"),
        ("definition_digest", "resume_incompatible"),
        ("root_trace_id", "resume_incompatible"),
        ("execution_mode", "resume_incompatible"),
        ("tool_call_id", "tool_call_mismatch"),
        ("action_digest", "resume_incompatible"),
        ("remedy_id", "remedy_mismatch"),
        ("consent_digest", "remedy_digest_mismatch"),
    ],
)
def test_resume_rejects_incompatible_markers_before_sdk_restore_or_dispatch(
    tmp_path,
    monkeypatch,
    marker: str,
    expected_code: str,
) -> None:
    database_path = tmp_path / f"incompatible-{marker}.sqlite3"
    store = SQLiteStore(database_path)
    provider = HotelSimulator(store=store)
    orchestrator = RecoveryOrchestrator(store=store, hotel_provider=provider)
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    recovery_id = pending.recovery.recovery_id
    request = approval_request(pending, f"incompatible-{marker}")
    store.close()

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            f"UPDATE pending_approvals SET {marker} = ? WHERE recovery_id = ?",
            ("incompatible-test-value", recovery_id),
        )

    reopened_store = SQLiteStore(database_path)
    fresh_provider = HotelSimulator(store=reopened_store)
    fresh_orchestrator = RecoveryOrchestrator(
        store=reopened_store,
        hotel_provider=fresh_provider,
    )
    restore_spy = AsyncMock(side_effect=AssertionError("SDK restore must not run"))
    monkeypatch.setattr(RunState, "from_json", restore_spy)

    with pytest.raises((ResumeIncompatibleError, ApprovalDecisionError)) as caught:
        asyncio.run(fresh_orchestrator.approve_decision(recovery_id, request))

    assert caught.value.code == expected_code
    assert caught.value.recovery_id == recovery_id
    assert caught.value.public_detail == {
        "code": expected_code,
        "recoveryId": recovery_id,
    }
    restore_spy.assert_not_awaited()
    assert fresh_provider.dispatch_count == 0
    assert reopened_store.count_executions(recovery_id) == 0
    with pytest.raises(RecoveryNotFoundError):
        reopened_store.get_receipt(recovery_id)


def test_sdk_restore_failure_logs_no_serialized_state_or_exception_message(
    tmp_path,
    caplog,
) -> None:
    database_path = tmp_path / "redacted-sdk-restore.sqlite3"
    store = SQLiteStore(database_path)
    provider = HotelSimulator(store=store)
    orchestrator = RecoveryOrchestrator(store=store, hotel_provider=provider)
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    recovery_id = pending.recovery.recovery_id
    request = approval_request(pending, "corrupt-state")
    store.close()

    with sqlite3.connect(database_path) as connection:
        serialized_state = connection.execute(
            "SELECT state_json FROM pending_approvals WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone()[0]
        corrupted_state = json.loads(serialized_state)
        corrupted_state["$schemaVersion"] = SENSITIVE_STATE_SENTINEL
        corrupted_state_json = json.dumps(corrupted_state, sort_keys=True)
        connection.execute(
            "UPDATE pending_approvals SET state_json = ? WHERE recovery_id = ?",
            (corrupted_state_json, recovery_id),
        )

    reopened_store = SQLiteStore(database_path)
    fresh_provider = HotelSimulator(store=reopened_store)
    fresh_orchestrator = RecoveryOrchestrator(
        store=reopened_store,
        hotel_provider=fresh_provider,
    )
    caplog.set_level("ERROR", logger="server.orchestrator")

    with pytest.raises(ResumeIncompatibleError) as caught:
        asyncio.run(fresh_orchestrator.approve_decision(recovery_id, request))

    assert caught.value.public_detail == {
        "code": "resume_incompatible",
        "recoveryId": recovery_id,
    }
    assert recovery_id in caplog.text
    assert "Agents SDK state restore failed" in caplog.text
    assert SENSITIVE_STATE_SENTINEL not in caplog.text
    assert "Run state schema version" not in caplog.text
    assert corrupted_state_json not in caplog.text
    assert fresh_provider.dispatch_count == 0
    assert reopened_store.count_executions(recovery_id) == 0
    with pytest.raises(RecoveryNotFoundError):
        reopened_store.get_receipt(recovery_id)
