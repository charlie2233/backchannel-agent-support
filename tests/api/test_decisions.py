from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from server.config import RuntimeSettings
from server.main import create_app
from server.providers.hotel_simulator import HotelSimulator
from server.store import SQLiteStore


def decision_payload(
    snapshot: dict[str, object],
    *,
    client_decision_id: str = "decision-001",
) -> dict[str, str]:
    approval = snapshot["pendingApproval"]
    assert isinstance(approval, dict)
    return {
        "clientDecisionId": client_decision_id,
        "remedyId": str(approval["remedyId"]),
        "remedyDigest": str(approval["remedyDigest"]),
        "toolCallId": str(approval["toolCallId"]),
    }


@pytest.fixture
def sdk_client(tmp_path):
    store = SQLiteStore(tmp_path / "decision-api.sqlite3")
    provider = HotelSimulator(store=store)
    with TestClient(
        create_app(
            RuntimeSettings(live_ready=False),
            store=store,
            hotel_provider=provider,
        )
    ) as client:
        yield client, store, provider


def create_sdk_recovery(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/api/recoveries",
        json={"scenarioId": "hotel", "executionMode": "sdk_stub"},
    )
    assert response.status_code == 201
    snapshot = response.json()
    assert snapshot["status"] == "pending_approval"
    assert snapshot["pendingApproval"]["executionStarted"] is False
    return snapshot


def test_public_sdk_stub_creation_and_typed_approval_complete_once(
    sdk_client,
) -> None:
    client, store, provider = sdk_client
    snapshot = create_sdk_recovery(client)
    recovery_id = str(snapshot["recoveryId"])
    payload = decision_payload(snapshot)

    response = client.post(f"/api/recoveries/{recovery_id}/decisions", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "clientDecisionId": "decision-001",
        "recoveryId": recovery_id,
        "status": "completed",
        "approvedRemedyDigest": payload["remedyDigest"],
        "executionStarted": True,
    }
    assert provider.dispatch_count == 1
    assert store.count_executions(recovery_id) == 1
    completed = client.get(f"/api/recoveries/{recovery_id}").json()
    assert completed["status"] == "completed"
    assert completed["pendingApproval"] is None
    receipt = client.get(f"/api/recoveries/{recovery_id}/receipt").json()
    assert receipt["approvedRemedyDigest"] == payload["remedyDigest"]
    assert receipt["providerExecution"] is True
    public_output = json.dumps({"snapshot": completed, "receipt": receipt}).lower()
    assert "state_json" not in public_output
    assert "statejson" not in public_output
    assert "consumer_proof" not in public_output
    assert "provider_proof" not in public_output


def test_openai_live_creation_remains_rejected(sdk_client) -> None:
    client, store, _provider = sdk_client

    response = client.post(
        "/api/recoveries",
        json={"scenarioId": "hotel", "executionMode": "openai_live"},
    )

    assert response.status_code == 422
    assert store.count_recoveries() == 0


def test_exact_duplicate_replays_stored_response_without_redispatch(sdk_client) -> None:
    client, store, provider = sdk_client
    snapshot = create_sdk_recovery(client)
    recovery_id = str(snapshot["recoveryId"])
    payload = decision_payload(snapshot)

    first = client.post(f"/api/recoveries/{recovery_id}/decisions", json=payload)
    second = client.post(f"/api/recoveries/{recovery_id}/decisions", json=payload)

    assert first.status_code == second.status_code == 200
    assert first.content == second.content
    assert provider.dispatch_count == 1
    assert store.count_executions(recovery_id) == 1
    assert len([event for event in store.list_events(recovery_id) if event.terminal]) == 1


def test_same_decision_id_with_changed_tuple_is_conflict(sdk_client) -> None:
    client, store, provider = sdk_client
    snapshot = create_sdk_recovery(client)
    recovery_id = str(snapshot["recoveryId"])
    payload = decision_payload(snapshot)
    first = client.post(f"/api/recoveries/{recovery_id}/decisions", json=payload)
    assert first.status_code == 200

    conflict = client.post(
        f"/api/recoveries/{recovery_id}/decisions",
        json={**payload, "toolCallId": "different-call"},
    )

    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "decision_id_conflict"
    assert provider.dispatch_count == 1
    assert store.count_executions(recovery_id) == 1


def test_different_decision_after_winner_is_already_decided(sdk_client) -> None:
    client, store, provider = sdk_client
    snapshot = create_sdk_recovery(client)
    recovery_id = str(snapshot["recoveryId"])
    payload = decision_payload(snapshot)
    assert (
        client.post(f"/api/recoveries/{recovery_id}/decisions", json=payload).status_code
        == 200
    )

    loser = client.post(
        f"/api/recoveries/{recovery_id}/decisions",
        json={**payload, "clientDecisionId": "decision-loser"},
    )

    assert loser.status_code == 409
    assert loser.json()["detail"]["code"] == "already_decided"
    assert provider.dispatch_count == 1
    assert store.count_executions(recovery_id) == 1


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("remedyId", "wrong-remedy", "remedy_mismatch"),
        ("remedyDigest", "sha256:" + "0" * 64, "remedy_digest_mismatch"),
        ("toolCallId", "wrong-call", "tool_call_mismatch"),
    ],
)
def test_exact_identity_mismatches_fail_closed(
    sdk_client,
    field: str,
    value: str,
    code: str,
) -> None:
    client, store, provider = sdk_client
    snapshot = create_sdk_recovery(client)
    recovery_id = str(snapshot["recoveryId"])
    payload = {**decision_payload(snapshot), field: value}

    response = client.post(f"/api/recoveries/{recovery_id}/decisions", json=payload)

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == code
    assert provider.dispatch_count == 0
    assert store.count_executions(recovery_id) == 0
    assert store.count_decisions(recovery_id) == 0


def test_digest_from_another_recovery_fails_closed(sdk_client) -> None:
    client, store, provider = sdk_client
    first = create_sdk_recovery(client)
    second = create_sdk_recovery(client)
    first_id = str(first["recoveryId"])
    second_approval = second["pendingApproval"]
    assert isinstance(second_approval, dict)
    payload = {
        **decision_payload(first),
        "remedyDigest": str(second_approval["remedyDigest"]),
    }

    response = client.post(f"/api/recoveries/{first_id}/decisions", json=payload)

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "remedy_digest_mismatch"
    assert provider.dispatch_count == 0
    assert store.count_executions(first_id) == 0


def test_two_apps_race_twenty_times_with_one_durable_winner(tmp_path) -> None:
    for iteration in range(20):
        database_path = tmp_path / f"decision-race-{iteration}.sqlite3"
        first_store = SQLiteStore(database_path)
        first_provider = HotelSimulator(store=first_store)
        first_app = create_app(
            RuntimeSettings(live_ready=False),
            store=first_store,
            hotel_provider=first_provider,
        )
        with TestClient(first_app) as creator:
            snapshot = create_sdk_recovery(creator)
        recovery_id = str(snapshot["recoveryId"])

        second_store = SQLiteStore(database_path)
        second_provider = HotelSimulator(store=second_store)
        second_app = create_app(
            RuntimeSettings(live_ready=False),
            store=second_store,
            hotel_provider=second_provider,
        )
        barrier = Barrier(2)

        def submit(app, decision_id: str):
            payload = decision_payload(snapshot, client_decision_id=decision_id)
            with TestClient(app) as client:
                barrier.wait()
                response = client.post(
                    f"/api/recoveries/{recovery_id}/decisions",
                    json=payload,
                )
                return response.status_code, response.json()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(submit, first_app, f"winner-a-{iteration}"),
                executor.submit(submit, second_app, f"winner-b-{iteration}"),
            ]
            outcomes = [future.result(timeout=15) for future in futures]

        assert sorted(status for status, _body in outcomes) == [200, 409]
        loser = next(body for status, body in outcomes if status == 409)
        assert loser["detail"]["code"] == "already_decided"
        assert first_provider.dispatch_count + second_provider.dispatch_count == 1
        verifier = SQLiteStore(database_path)
        assert verifier.count_decisions(recovery_id) == 1
        assert verifier.count_executions(recovery_id) == 1
        assert verifier.get_receipt(recovery_id).provider_execution is True
        assert len([event for event in verifier.list_events(recovery_id) if event.terminal]) == 1


def test_same_decision_id_can_race_and_replay_one_result(tmp_path) -> None:
    database_path = tmp_path / "same-decision-race.sqlite3"
    first_store = SQLiteStore(database_path)
    first_provider = HotelSimulator(store=first_store)
    first_app = create_app(
        RuntimeSettings(live_ready=False),
        store=first_store,
        hotel_provider=first_provider,
    )
    with TestClient(first_app) as creator:
        snapshot = create_sdk_recovery(creator)
    recovery_id = str(snapshot["recoveryId"])
    second_store = SQLiteStore(database_path)
    second_provider = HotelSimulator(store=second_store)
    second_app = create_app(
        RuntimeSettings(live_ready=False),
        store=second_store,
        hotel_provider=second_provider,
    )
    barrier = Barrier(2)

    def submit(app):
        with TestClient(app) as client:
            barrier.wait()
            response = client.post(
                f"/api/recoveries/{recovery_id}/decisions",
                json=decision_payload(snapshot, client_decision_id="same-winner"),
            )
            return response.status_code, response.content

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [
            future.result(timeout=15)
            for future in [executor.submit(submit, first_app), executor.submit(submit, second_app)]
        ]

    assert [status for status, _content in outcomes] == [200, 200]
    assert outcomes[0][1] == outcomes[1][1]
    assert first_provider.dispatch_count + second_provider.dispatch_count == 1
    verifier = SQLiteStore(database_path)
    assert verifier.count_decisions(recovery_id) == 1
    assert verifier.count_executions(recovery_id) == 1
    assert len([event for event in verifier.list_events(recovery_id) if event.terminal]) == 1
