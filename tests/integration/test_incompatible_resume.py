import asyncio
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
