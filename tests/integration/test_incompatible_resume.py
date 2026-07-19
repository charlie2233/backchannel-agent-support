import asyncio
import json
import sqlite3
from unittest.mock import AsyncMock

import pytest
from agents import RunState

from server.models import ExecutionMode
from server.orchestrator import (
    RecoveryOrchestrator,
    ResumeIncompatibleError,
)
from server.providers.hotel_simulator import HotelSimulator
from server.store import RecoveryNotFoundError, SQLiteStore

SENSITIVE_STATE_SENTINEL = "SENSITIVE_STATE_SENTINEL"


@pytest.mark.parametrize(
    "marker",
    [
        "sdk_version",
        "protocol_version",
        "agent_graph_version",
        "definition_digest",
        "root_trace_id",
        "execution_mode",
        "tool_call_id",
        "remedy_digest",
    ],
)
def test_resume_rejects_incompatible_markers_before_sdk_restore_or_dispatch(
    tmp_path,
    monkeypatch,
    marker: str,
) -> None:
    database_path = tmp_path / f"incompatible-{marker}.sqlite3"
    store = SQLiteStore(database_path)
    provider = HotelSimulator(store=store)
    orchestrator = RecoveryOrchestrator(store=store, hotel_provider=provider)
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    recovery_id = pending.recovery.recovery_id
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

    with pytest.raises(ResumeIncompatibleError) as caught:
        asyncio.run(fresh_orchestrator.resume_approved(recovery_id))

    assert caught.value.code == "resume_incompatible"
    assert caught.value.recovery_id == recovery_id
    assert caught.value.public_detail == {
        "code": "resume_incompatible",
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
        asyncio.run(fresh_orchestrator.resume_approved(recovery_id))

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
