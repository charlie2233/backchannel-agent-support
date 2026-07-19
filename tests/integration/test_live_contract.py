import asyncio

import pytest
from pydantic import ValidationError

from server.models import ExecutionMode, RecoverySnapshot
from server.orchestrator import RecoveryOrchestrator, UnsupportedOrchestrationError
from server.providers.hotel_simulator import HotelSimulator
from server.store import SQLiteStore


def snapshot_payload(*, execution_mode: ExecutionMode) -> dict[str, object]:
    return {
        "recoveryId": "11111111-2222-4333-8444-555555555555",
        "scenarioId": "hotel",
        "executionMode": execution_mode,
        "status": "in_progress",
        "currentStep": 0,
        "currentStepSummary": "Recovery started.",
        "createdAt": "2026-07-19T12:00:00Z",
        "updatedAt": "2026-07-19T12:00:00Z",
        "pendingApproval": None,
    }


def test_every_snapshot_requires_mode_bound_model_and_root_trace_provenance() -> None:
    with pytest.raises(ValidationError):
        RecoverySnapshot.model_validate(snapshot_payload(execution_mode=ExecutionMode.SDK_STUB))

    replay = RecoverySnapshot.model_validate(
        {
            **snapshot_payload(execution_mode=ExecutionMode.REPLAY_FIXTURE),
            "modelIds": [],
            "rootTraceId": None,
        }
    )
    assert replay.model_ids == []
    assert replay.root_trace_id is None

    live = RecoverySnapshot.model_validate(
        {
            **snapshot_payload(execution_mode=ExecutionMode.OPENAI_LIVE),
            "modelIds": ["gpt-5.6-luna", "gpt-5.6-terra"],
            "rootTraceId": "trace_0123456789abcdef0123456789abcdef",
        }
    )
    assert live.model_ids == ["gpt-5.6-luna", "gpt-5.6-terra"]
    assert live.root_trace_id == "trace_0123456789abcdef0123456789abcdef"


def test_false_ready_direct_orchestrator_call_rejects_openai_live(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "false-ready.sqlite3")
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(store=store),
        live_ready=False,
    )

    with pytest.raises(UnsupportedOrchestrationError, match="live-ready"):
        asyncio.run(
            orchestrator.start("hotel", execution_mode=ExecutionMode.OPENAI_LIVE)
        )

    assert store.count_recoveries() == 0
