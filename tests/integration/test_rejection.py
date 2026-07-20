from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from agents import RunState
from fastapi.testclient import TestClient

from server.config import RuntimeSettings
from server.main import create_app
from server.models import ApprovalDecisionRequest, ExecutionMode
from server.orchestrator import RecoveryOrchestrator
from server.providers.hotel_simulator import HotelSimulator
from server.store import SQLiteStore


def _create_pending(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/api/recoveries",
        json={"scenarioId": "hotel", "executionMode": "sdk_stub"},
    )
    assert response.status_code == 201
    return cast(dict[str, object], response.json())


def _decline_payload(snapshot: dict[str, object]) -> dict[str, str]:
    approval = cast(dict[str, object], snapshot["pendingApproval"])
    return {
        "action": "decline",
        "clientDecisionId": "restart-decline-001",
        "remedyId": cast(str, approval["remedyId"]),
        "remedyDigest": cast(str, approval["remedyDigest"]),
        "toolCallId": cast(str, approval["toolCallId"]),
    }


def test_restart_decline_rejects_exact_interruption_and_replays(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    database_path = tmp_path / "restart-decline.sqlite3"
    creating_store = SQLiteStore(database_path)
    creating_provider = HotelSimulator(store=creating_store)
    with TestClient(
        create_app(
            RuntimeSettings(live_ready=False),
            store=creating_store,
            hotel_provider=creating_provider,
        )
    ) as client:
        snapshot = _create_pending(client)
        session_cookie = client.cookies.get("backchannel_demo_session")
        assert session_cookie is not None
    recovery_id = cast(str, snapshot["recoveryId"])
    approval = cast(dict[str, object], snapshot["pendingApproval"])
    payload = _decline_payload(snapshot)
    creating_store.close()

    observed_rejections: list[dict[str, object]] = []
    original_reject = RunState.reject

    def recording_reject(
        self: RunState[Any],
        approval_item: Any,
        always_reject: bool = False,
        *,
        rejection_message: str | None = None,
    ) -> None:
        observed_rejections.append(
            {
                "callId": approval_item.call_id,
                "alwaysReject": always_reject,
                "message": rejection_message,
            }
        )
        original_reject(
            self,
            approval_item,
            always_reject=always_reject,
            rejection_message=rejection_message,
        )

    monkeypatch.setattr(RunState, "reject", recording_reject)
    restarted_store = SQLiteStore(database_path)
    restarted_provider = HotelSimulator(store=restarted_store)
    with TestClient(
        create_app(
            RuntimeSettings(live_ready=False),
            store=restarted_store,
            hotel_provider=restarted_provider,
        )
    ) as restarted_client:
        restarted_client.cookies.set("backchannel_demo_session", session_cookie)
        first = restarted_client.post(
            f"/api/recoveries/{recovery_id}/decisions",
            json=payload,
        )
    restarted_store.close()

    replay_store = SQLiteStore(database_path)
    replay_provider = HotelSimulator(store=replay_store)
    with TestClient(
        create_app(
            RuntimeSettings(live_ready=False),
            store=replay_store,
            hotel_provider=replay_provider,
        )
    ) as replay_client:
        replay_client.cookies.set("backchannel_demo_session", session_cookie)
        duplicate = replay_client.post(
            f"/api/recoveries/{recovery_id}/decisions",
            json=payload,
        )

    assert first.status_code == duplicate.status_code == 200
    assert first.content == duplicate.content
    assert observed_rejections == [
        {
            "callId": approval["toolCallId"],
            "alwaysReject": False,
            "message": (
                "The user declined this exact remedy. Do not execute it or select "
                "an alternative."
            ),
        }
    ]
    assert restarted_provider.dispatch_count == replay_provider.dispatch_count == 0
    assert replay_store.count_executions(recovery_id) == 0
    assert len(
        [event for event in replay_store.list_events(recovery_id) if event.terminal]
    ) == 1
    replay_store.close()


def test_claimed_decline_resumes_after_process_loss_before_sdk_restore(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    database_path = tmp_path / "claimed-decline-restart.sqlite3"
    store = SQLiteStore(database_path)
    provider = HotelSimulator(store=store)
    orchestrator = RecoveryOrchestrator(store=store, hotel_provider=provider)
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    approval = pending.recovery.pending_approval
    assert approval is not None
    request = ApprovalDecisionRequest(
        action="decline",
        clientDecisionId="claimed-decline-restart",
        remedyId=approval.remedy_id,
        remedyDigest=approval.remedy_digest,
        toolCallId=approval.tool_call_id,
    )

    class SimulatedProcessLoss(RuntimeError):
        pass

    async def lose_process_before_restore(_claim: Any) -> None:
        raise SimulatedProcessLoss("process lost after decline claim")

    monkeypatch.setattr(
        orchestrator,
        "_resume_claimed_approval",
        lose_process_before_restore,
    )
    with pytest.raises(SimulatedProcessLoss, match="after decline claim"):
        asyncio.run(orchestrator.approve_decision(pending.recovery.recovery_id, request))
    assert store.count_decisions(pending.recovery.recovery_id) == 1
    assert store.count_executions(pending.recovery.recovery_id) == 0
    store.close()

    restarted_store = SQLiteStore(database_path)
    restarted_provider = HotelSimulator(store=restarted_store)
    restarted = RecoveryOrchestrator(
        store=restarted_store,
        hotel_provider=restarted_provider,
    )

    completed = asyncio.run(
        restarted.approve_decision(pending.recovery.recovery_id, request)
    )
    duplicate = asyncio.run(
        restarted.approve_decision(pending.recovery.recovery_id, request)
    )

    assert completed == duplicate
    assert completed.status == "closed_without_action"
    assert restarted_provider.dispatch_count == 0
    assert restarted_store.count_executions(pending.recovery.recovery_id) == 0
    assert len(
        [
            event
            for event in restarted_store.list_events(pending.recovery.recovery_id)
            if event.terminal
        ]
    ) == 1
    restarted_store.close()
