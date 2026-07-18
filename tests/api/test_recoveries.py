import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from server.config import RuntimeSettings
from server.main import create_app
from server.store import SQLiteStore


@pytest.fixture
def client(tmp_path) -> TestClient:
    store = SQLiteStore(tmp_path / "api.sqlite3")
    with TestClient(
        create_app(RuntimeSettings(live_ready=False), store=store)
    ) as test_client:
        yield test_client


def test_scenarios_are_exactly_the_two_replay_definitions(client: TestClient) -> None:
    response = client.get("/api/scenarios")

    assert response.status_code == 200
    scenarios = response.json()
    assert [scenario["id"] for scenario in scenarios] == ["hotel", "api-quota"]
    assert all(scenario["executionMode"] == "replay_fixture" for scenario in scenarios)


@pytest.mark.parametrize(
    ("payload", "expected_status"),
    [
        ({"scenarioId": "unknown", "executionMode": "replay_fixture"}, 422),
        ({"scenarioId": "hotel", "executionMode": "openai_live"}, 422),
        ({"scenarioId": "hotel", "executionMode": "sdk_stub"}, 422),
        ({"scenarioId": "hotel", "executionMode": "unknown"}, 422),
    ],
)
def test_create_recovery_validates_scenario_and_execution_mode(
    client: TestClient, payload: dict[str, str], expected_status: int
) -> None:
    assert client.post("/api/recoveries", json=payload).status_code == expected_status


def test_replay_recovery_snapshot_receipt_and_reset_are_durable(client: TestClient) -> None:
    created = client.post(
        "/api/recoveries",
        json={"scenarioId": "api-quota", "executionMode": "replay_fixture"},
    )

    assert created.status_code == 201
    snapshot = created.json()
    recovery_id = snapshot["recoveryId"]
    assert snapshot["status"] == "completed"
    assert snapshot["executionMode"] == "replay_fixture"
    assert client.get(f"/api/recoveries/{recovery_id}").json() == snapshot

    receipt_response = client.get(f"/api/recoveries/{recovery_id}/receipt")
    assert receipt_response.status_code == 200
    receipt = receipt_response.json()
    assert receipt["recoveryId"] == recovery_id
    assert receipt["executionMode"] == "replay_fixture"
    assert receipt["simulated"] is True
    assert receipt["providerExecution"] is False
    assert receipt["modelIds"] == []
    assert "simulated" in json.dumps(receipt).lower()
    assert "no model call or provider execution" in receipt["boundary"].lower()

    reset = client.post("/api/demo/reset")
    assert reset.status_code == 200
    assert reset.json() == {"reset": True}
    assert client.get(f"/api/recoveries/{recovery_id}").status_code == 404
    assert client.get(f"/api/recoveries/{uuid4()}/receipt").status_code == 404


def test_last_event_id_replays_only_newer_persisted_events(client: TestClient) -> None:
    created = client.post(
        "/api/recoveries",
        json={"scenarioId": "api-quota", "executionMode": "replay_fixture"},
    )
    recovery_id = created.json()["recoveryId"]

    response = client.get(
        f"/api/recoveries/{recovery_id}/events",
        headers={"Last-Event-ID": "2"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    streamed_ids = [
        int(line.removeprefix("id: "))
        for line in response.text.splitlines()
        if line.startswith("id: ")
    ]
    assert streamed_ids
    assert streamed_ids == sorted(streamed_ids)
    assert all(sequence > 2 for sequence in streamed_ids)


def test_last_event_id_must_be_a_non_negative_integer(client: TestClient) -> None:
    created = client.post(
        "/api/recoveries",
        json={"scenarioId": "api-quota", "executionMode": "replay_fixture"},
    )
    recovery_id = created.json()["recoveryId"]

    assert (
        client.get(
            f"/api/recoveries/{recovery_id}/events",
            headers={"Last-Event-ID": "not-an-integer"},
        ).status_code
        == 400
    )
