from __future__ import annotations

import json
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from server.config import RuntimeSettings
from server.main import create_app
from server.providers.hotel_simulator import HotelSimulator
from server.store import ApprovalDecisionError, SQLiteStore


@pytest.fixture
def rejection_client(tmp_path: Any):
    store = SQLiteStore(tmp_path / "rejection-api.sqlite3")
    provider = HotelSimulator(store=store)
    with TestClient(
        create_app(
            RuntimeSettings(live_ready=False),
            store=store,
            hotel_provider=provider,
        )
    ) as client:
        yield client, store, provider
    store.close()


def _pending_recovery(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/api/recoveries",
        json={
            "scenarioId": "hotel",
            "executionMode": "sdk_stub",
            "clientRequestId": uuid4().hex,
        },
    )
    assert response.status_code == 201
    return cast(dict[str, object], response.json())


def _decision_payload(
    snapshot: dict[str, object],
    *,
    action: str,
    client_decision_id: str = "decline-api-001",
) -> dict[str, str]:
    approval = cast(dict[str, object], snapshot["pendingApproval"])
    return {
        "action": action,
        "clientDecisionId": client_decision_id,
        "remedyId": cast(str, approval["remedyId"]),
        "remedyDigest": cast(str, approval["remedyDigest"]),
        "toolCallId": cast(str, approval["toolCallId"]),
    }


def test_decision_action_is_required_and_has_no_default(rejection_client: Any) -> None:
    client, store, provider = rejection_client
    snapshot = _pending_recovery(client)
    recovery_id = cast(str, snapshot["recoveryId"])
    payload = _decision_payload(snapshot, action="decline")
    payload.pop("action")

    response = client.post(
        f"/api/recoveries/{recovery_id}/decisions",
        json=payload,
    )

    assert response.status_code == 422
    assert store.count_decisions(recovery_id) == 0
    assert store.count_executions(recovery_id) == 0
    assert provider.dispatch_count == 0


def test_decline_seals_zero_execution_terminal_evidence(rejection_client: Any) -> None:
    client, store, provider = rejection_client
    snapshot = _pending_recovery(client)
    recovery_id = cast(str, snapshot["recoveryId"])

    response = client.post(
        f"/api/recoveries/{recovery_id}/decisions",
        json=_decision_payload(snapshot, action="decline"),
    )

    assert response.status_code == 200
    body = cast(dict[str, object], response.json())
    assert body["action"] == "decline"
    assert body["status"] == "closed_without_action"
    assert body["executionStarted"] is False
    assert provider.dispatch_count == 0
    assert store.count_executions(recovery_id) == 0

    terminal = client.get(f"/api/recoveries/{recovery_id}").json()
    receipt = client.get(f"/api/recoveries/{recovery_id}/receipt").json()
    assert terminal["status"] == "closed_without_action"
    assert terminal["pendingApproval"] is None
    assert receipt["status"] == "closed_without_action"
    assert receipt["providerExecution"] is False
    assert receipt["providerResult"] == "Provider dispatch did not begin."
    assert receipt["authorizationSource"] == (
        "User declined the exact Agents SDK commit_remedy interruption."
    )
    assert receipt["verificationResults"] == [
        "Human consent requested.",
        "Remedy declined by operator.",
        "Exact interruption rejected.",
        "No replacement action selected.",
        "Execution count is zero.",
        "Provider dispatch did not begin.",
        "Temporary permission revoked.",
        "Cancellation receipt sealed.",
    ]
    terminal_events = [event for event in store.list_events(recovery_id) if event.terminal]
    assert len(terminal_events) == 1
    assert terminal_events[0].type == "recovery.closed_without_action"
    assert terminal_events[0].data["executionCount"] == 0
    approval = cast(dict[str, object], snapshot["pendingApproval"])
    with pytest.raises(ApprovalDecisionError) as revoked:
        store.assert_provider_dispatch_authorized(
            recovery_id=recovery_id,
            tool_call_id=cast(str, approval["toolCallId"]),
            remedy_digest=cast(str, approval["remedyDigest"]),
        )
    assert revoked.value.code == "decision_id_conflict"

    public_output = json.dumps(
        {"decision": body, "snapshot": terminal, "receipt": receipt}
    ).lower()
    assert "state_json" not in public_output
    assert "statejson" not in public_output
    assert "serialized_state" not in public_output


def test_exact_decline_replays_but_changed_action_conflicts(rejection_client: Any) -> None:
    client, store, provider = rejection_client
    snapshot = _pending_recovery(client)
    recovery_id = cast(str, snapshot["recoveryId"])
    payload = _decision_payload(snapshot, action="decline")

    first = client.post(f"/api/recoveries/{recovery_id}/decisions", json=payload)
    duplicate = client.post(f"/api/recoveries/{recovery_id}/decisions", json=payload)
    changed_action = client.post(
        f"/api/recoveries/{recovery_id}/decisions",
        json={**payload, "action": "approve"},
    )

    assert first.status_code == duplicate.status_code == 200
    assert first.content == duplicate.content
    assert changed_action.status_code == 409
    assert changed_action.json()["detail"] == {
        "code": "decision_id_conflict",
        "recoveryId": recovery_id,
    }
    assert store.count_decisions(recovery_id) == 1
    assert store.count_executions(recovery_id) == 0
    assert provider.dispatch_count == 0
    assert len([event for event in store.list_events(recovery_id) if event.terminal]) == 1


def test_decline_after_approval_may_have_begun_marks_outcome_unknown(
    rejection_client: Any,
) -> None:
    client, store, provider = rejection_client
    snapshot = _pending_recovery(client)
    recovery_id = cast(str, snapshot["recoveryId"])
    store.update_pending_approval_status(recovery_id, status="approved")

    response = client.post(
        f"/api/recoveries/{recovery_id}/decisions",
        json=_decision_payload(snapshot, action="decline", client_decision_id="late-decline"),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "outcome_unknown"
    assert response.json()["executionStarted"] is None
    receipt = client.get(f"/api/recoveries/{recovery_id}/receipt").json()
    assert receipt["status"] == "outcome_unknown"
    assert receipt["providerExecution"] is None
    assert "may have begun" in receipt["providerResult"]
    assert "cancellation was not claimed" in receipt["authorizationSource"]
    assert "Execution count is zero." not in receipt["verificationResults"]
    assert "Provider dispatch did not begin." not in receipt["verificationResults"]
    assert "Cancellation receipt sealed." not in receipt["verificationResults"]
    assert store.count_executions(recovery_id) == 0
    assert provider.dispatch_count == 0
    terminal_events = [event for event in store.list_events(recovery_id) if event.terminal]
    assert len(terminal_events) == 1
    assert terminal_events[0].type == "recovery.outcome_unknown"
    assert "executionCount" not in terminal_events[0].data
