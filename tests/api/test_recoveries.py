import asyncio
import json
import sqlite3
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from server.config import RuntimeSettings
from server.events import stream_recovery_events
from server.main import create_app
from server.models import ExecutionMode, RecoveryEvent, RecoveryStatus, ScenarioId
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
        ({"scenarioId": "hotel", "executionMode": "openai_live"}, 503),
        ({"scenarioId": "api-quota", "executionMode": "sdk_stub"}, 201),
        ({"scenarioId": "hotel", "executionMode": "unknown"}, 422),
    ],
)
def test_create_recovery_validates_scenario_and_execution_mode(
    client: TestClient, payload: dict[str, str], expected_status: int
) -> None:
    assert client.post("/api/recoveries", json=payload).status_code == expected_status


@pytest.mark.parametrize("scenario_id", ["hotel", "api-quota"])
def test_replay_recovery_snapshot_and_receipt_are_terminal_and_durable(
    client: TestClient,
    scenario_id: str,
) -> None:
    created = client.post(
        "/api/recoveries",
        json={"scenarioId": scenario_id, "executionMode": "replay_fixture"},
    )

    assert created.status_code == 201
    snapshot = created.json()
    recovery_id = snapshot["recoveryId"]
    assert snapshot["status"] == "completed"
    assert snapshot["currentStep"] == 5
    assert snapshot["executionMode"] == "replay_fixture"
    assert snapshot["pendingApproval"] is None
    assert snapshot["rootTraceId"] is None
    assert snapshot["modelIds"] == []
    assert snapshot["sdkVersion"] is None
    assert snapshot["protocolVersion"] is None
    assert snapshot["agentGraphVersion"] is None
    assert snapshot["promptToolSchemaHash"] is None
    assert client.get(f"/api/recoveries/{recovery_id}").json() == snapshot

    events = client.app.state.recovery_store.list_events(recovery_id)
    assert len(events) == 7
    assert [event.seq for event in events] == [1, 2, 3, 4, 5, 6, 7]
    assert [event.terminal for event in events] == [
        False,
        False,
        False,
        False,
        False,
        False,
        True,
    ]
    assert events[0].type == "recovery.created"
    assert len(events[1:]) == 6
    assert events[-1].type == (
        "recovery.completed"
        if scenario_id == "api-quota"
        else "receipt.simulation_sealed"
    )

    receipt_response = client.get(f"/api/recoveries/{recovery_id}/receipt")
    assert receipt_response.status_code == 200
    receipt = receipt_response.json()
    assert receipt["recoveryId"] == recovery_id
    assert receipt["executionMode"] == "replay_fixture"
    assert receipt["status"] == "completed"
    assert receipt["simulated"] is True
    assert receipt["providerExecution"] is False
    assert receipt["modelIds"] == []
    assert receipt["rootTraceId"] is None
    assert receipt["sdkVersion"] is None
    assert receipt["protocolVersion"] is None
    assert receipt["agentGraphVersion"] is None
    assert receipt["promptToolSchemaHash"] is None
    assert receipt["decision"] is None
    assert receipt["decisionRemedyDigest"] is None
    assert receipt["executionCount"] == 0
    assert receipt["providerDispatchStarted"] is False
    assert receipt["exactInterruptionRejected"] is False
    assert receipt["permissionRevoked"] is False
    assert receipt["scopeClosed"] is False
    assert receipt["approvedRemedyDigest"] is None
    assert "simulated" in json.dumps(receipt).lower()
    expected_boundary = (
        "no model call, runtime provider dispatch"
        if scenario_id == "api-quota"
        else "no model call or provider execution"
    )
    assert expected_boundary in receipt["boundary"].lower()

    assert client.get(f"/api/recoveries/{uuid4()}/receipt").status_code == 404


def test_sdk_stub_hotel_creates_a_pending_recovery_without_execution(tmp_path) -> None:
    database_path = tmp_path / "api-sdk-stub.sqlite3"
    store = SQLiteStore(database_path)
    with TestClient(
        create_app(
            RuntimeSettings(live_ready=False),
            store=store,
        )
    ) as client:
        response = client.post(
            "/api/recoveries",
            json={"scenarioId": "hotel", "executionMode": "sdk_stub"},
        )

    assert response.status_code == 201
    assert response.json()["status"] == "pending_approval"
    assert response.json()["pendingApproval"]["executionStarted"] is False
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM recoveries").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM executions").fetchone() == (0,)


def test_demo_reset_is_forbidden_by_default_without_deleting_recovery(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/recoveries",
        json={"scenarioId": "api-quota", "executionMode": "replay_fixture"},
    )
    recovery_id = created.json()["recoveryId"]

    reset = client.post("/api/demo/reset", json={})

    assert reset.status_code == 403
    assert reset.json()["error"]["code"] == "invalid_request"
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

        reset = client.post("/api/demo/reset", json={})

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
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
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
    recovery = store.create_recovery(
        recovery_id="terminal-race-replay",
        scenario_id=ScenarioId.HOTEL,
        execution_mode=ExecutionMode.REPLAY_FIXTURE,
        current_step=0,
        current_step_summary="Replay race fixture created.",
    )
    recovery = store.record_transition(
        recovery.recovery_id,
        status=RecoveryStatus.IN_PROGRESS,
        current_step=3,
        current_step_summary="Replay race fixture reached its boundary.",
        event_type="authorization.boundary_recorded",
        event_data={"providerExecution": False},
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


@pytest.mark.parametrize(
    "terminal_status",
    [
        RecoveryStatus.COMPLETED,
        RecoveryStatus.CLOSED_WITHOUT_ACTION,
        RecoveryStatus.OUTCOME_UNKNOWN,
    ],
)
def test_event_stream_closes_for_every_terminal_recovery_status(
    tmp_path, monkeypatch, terminal_status: RecoveryStatus
) -> None:
    store = SQLiteStore(tmp_path / f"stream-{terminal_status.value}.sqlite3")

    def read_terminal_batch(
        recovery_id: str, *, after_seq: int
    ) -> tuple[list[RecoveryEvent], RecoveryStatus]:
        del recovery_id, after_seq
        return [], terminal_status

    monkeypatch.setattr(store, "read_event_batch", read_terminal_batch)
    disconnection_checks = 0

    async def connected() -> bool:
        nonlocal disconnection_checks
        disconnection_checks += 1
        return False

    async def collect() -> list[str]:
        return [
            chunk
            async for chunk in stream_recovery_events(
                store,
                "terminal-recovery",
                after_seq=0,
                is_disconnected=connected,
                poll_interval_seconds=0,
            )
        ]

    chunks = asyncio.run(asyncio.wait_for(collect(), timeout=0.1))

    assert chunks == []
    assert disconnection_checks == 0
