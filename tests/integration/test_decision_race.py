from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from server.config import RuntimeSettings
from server.main import create_app
from server.providers.hotel_simulator import HotelSimulator
from server.store import SQLiteStore


def _payload(
    snapshot: dict[str, object],
    *,
    action: str,
    decision_id: str,
) -> dict[str, str]:
    approval = cast(dict[str, object], snapshot["pendingApproval"])
    return {
        "action": action,
        "clientDecisionId": decision_id,
        "remedyId": cast(str, approval["remedyId"]),
        "remedyDigest": cast(str, approval["remedyDigest"]),
        "toolCallId": cast(str, approval["toolCallId"]),
    }


@pytest.mark.parametrize("iteration", range(5))
def test_approve_and_decline_race_has_one_durable_terminal_winner(
    tmp_path: Any,
    iteration: int,
) -> None:
    database_path = tmp_path / f"approve-decline-race-{iteration}.sqlite3"
    first_store = SQLiteStore(database_path)
    first_provider = HotelSimulator(store=first_store)
    first_app = create_app(
        RuntimeSettings(live_ready=False),
        store=first_store,
        hotel_provider=first_provider,
    )
    with TestClient(first_app) as creator:
        created = creator.post(
            "/api/recoveries",
            json={"scenarioId": "hotel", "executionMode": "sdk_stub"},
        )
        assert created.status_code == 201
        snapshot = cast(dict[str, object], created.json())
    recovery_id = cast(str, snapshot["recoveryId"])

    second_store = SQLiteStore(database_path)
    second_provider = HotelSimulator(store=second_store)
    second_app = create_app(
        RuntimeSettings(live_ready=False),
        store=second_store,
        hotel_provider=second_provider,
    )
    barrier = Barrier(2)

    def submit(app: Any, action: str) -> tuple[str, int, dict[str, object]]:
        with TestClient(app) as client:
            barrier.wait(timeout=15)
            response = client.post(
                f"/api/recoveries/{recovery_id}/decisions",
                json=_payload(
                    snapshot,
                    action=action,
                    decision_id=f"{action}-race-{iteration}",
                ),
            )
            return action, response.status_code, cast(dict[str, object], response.json())

    executor = ThreadPoolExecutor(max_workers=2)
    futures = []
    try:
        futures = [
            executor.submit(submit, first_app, "approve"),
            executor.submit(submit, second_app, "decline"),
        ]
        outcomes = [future.result(timeout=20) for future in futures]
    finally:
        for future in futures:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)

    assert sorted(status for _action, status, _body in outcomes) == [200, 409]
    winner_action = next(action for action, status, _body in outcomes if status == 200)
    loser = next(body for _action, status, body in outcomes if status == 409)
    assert loser["detail"] == {"code": "already_decided", "recoveryId": recovery_id}

    verifier = SQLiteStore(database_path)
    assert verifier.count_decisions(recovery_id) == 1
    assert len([event for event in verifier.list_events(recovery_id) if event.terminal]) == 1
    assert first_provider.dispatch_count + second_provider.dispatch_count == (
        1 if winner_action == "approve" else 0
    )
    assert verifier.count_executions(recovery_id) == (
        1 if winner_action == "approve" else 0
    )
    receipt = verifier.get_receipt(recovery_id)
    assert receipt.provider_execution is (winner_action == "approve")
    verifier.close()
    first_store.close()
    second_store.close()
