import asyncio
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from server.config import RuntimeSettings
from server.events import stream_recovery_events
from server.main import create_app
from server.models import ExecutionMode, RecoveryEvent, RecoveryStatus
from server.providers.hotel_simulator import HotelSimulator
from server.replay.engine import ReplayEngine
from server.replay.loader import ScenarioLoader
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
        ({"scenarioId": "api-quota", "executionMode": "sdk_stub"}, 422),
        ({"scenarioId": "hotel", "executionMode": "unknown"}, 422),
    ],
)
def test_create_recovery_validates_scenario_and_execution_mode(
    client: TestClient, payload: dict[str, str], expected_status: int
) -> None:
    assert client.post("/api/recoveries", json=payload).status_code == expected_status


def test_replay_recovery_snapshot_and_receipt_are_durable(client: TestClient) -> None:
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

    assert client.get(f"/api/recoveries/{uuid4()}/receipt").status_code == 404


def test_sdk_stub_hotel_reaches_pending_approval_without_dispatch(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "api-sdk-stub.sqlite3")
    provider = HotelSimulator()
    with TestClient(
        create_app(
            RuntimeSettings(live_ready=False),
            store=store,
            hotel_provider=provider,
        )
    ) as client:
        response = client.post(
            "/api/recoveries",
            json={"scenarioId": "hotel", "executionMode": "sdk_stub"},
        )

    assert response.status_code == 201
    assert response.json()["status"] == "pending_approval"
    assert response.json()["executionMode"] == "sdk_stub"
    assert provider.dispatch_count == 0


def test_demo_reset_is_forbidden_by_default_without_deleting_recovery(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/recoveries",
        json={"scenarioId": "api-quota", "executionMode": "replay_fixture"},
    )
    recovery_id = created.json()["recoveryId"]

    reset = client.post("/api/demo/reset")

    assert reset.status_code == 403
    assert reset.json() == {"detail": "Forbidden"}
    assert client.get(f"/api/recoveries/{recovery_id}").status_code == 200


def test_demo_reset_deletes_recovery_only_when_explicitly_enabled(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "reset-enabled.sqlite3")
    settings = RuntimeSettings(live_ready=False, demo_reset_enabled=True)
    with TestClient(create_app(settings, store=store)) as client:
        created = client.post(
            "/api/recoveries",
            json={"scenarioId": "api-quota", "executionMode": "replay_fixture"},
        )
        recovery_id = created.json()["recoveryId"]

        reset = client.post("/api/demo/reset")

        assert reset.status_code == 200
        assert reset.json() == {"reset": True}
        assert client.get(f"/api/recoveries/{recovery_id}").status_code == 404


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


def test_terminal_transition_after_event_read_is_emitted_before_stream_end(
    tmp_path, monkeypatch
) -> None:
    store = SQLiteStore(tmp_path / "terminal-race.sqlite3")
    recovery = ReplayEngine(store, ScenarioLoader()).start(
        "hotel", execution_mode=ExecutionMode.REPLAY_FIXTURE
    )
    cursor = store.list_events(recovery.recovery_id)[-1].seq
    original_read = store.read_event_batch
    injected = False

    def read_with_terminal_commit(
        recovery_id: str, *, after_seq: int
    ) -> tuple[list[RecoveryEvent], RecoveryStatus]:
        nonlocal injected
        batch = original_read(recovery_id, after_seq=after_seq)
        if not injected:
            injected = True
            store.record_transition(
                recovery_id,
                status=RecoveryStatus.COMPLETED,
                current_step=5,
                current_step_summary="Terminal event committed at the old race boundary.",
                event_type="recovery.test_terminal",
                event_data={"summary": "Final persisted event"},
            )
        return batch

    monkeypatch.setattr(store, "read_event_batch", read_with_terminal_commit)

    async def connected() -> bool:
        return False

    async def collect() -> list[str]:
        return [
            chunk
            async for chunk in stream_recovery_events(
                store,
                recovery.recovery_id,
                after_seq=cursor,
                is_disconnected=connected,
                poll_interval_seconds=0,
            )
        ]

    chunks = asyncio.run(collect())
    assert len(chunks) == 1
    assert f"id: {cursor + 1}" in chunks[0]
    assert '"terminal":true' in chunks[0]
