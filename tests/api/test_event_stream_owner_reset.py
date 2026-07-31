from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.types import Message, Scope

from server.config import RuntimeSettings
from server.events import lease_event_stream, stream_recovery_events
from server.main import _build_admitted_event_stream_response, create_app
from server.replay.loader import ScenarioLoader
from server.store import PublicEvidenceIntegrityError, SQLiteStore


def _settings() -> RuntimeSettings:
    return RuntimeSettings(
        live_ready=False,
        demo_reset_enabled=True,
        max_concurrent_event_streams=1,
        max_event_streams_per_recovery=1,
    )


def _create_recovery(
    client: TestClient,
    *,
    scenario_id: str,
    execution_mode: str,
) -> str:
    response = client.post(
        "/api/recoveries",
        json={
            "scenarioId": scenario_id,
            "executionMode": execution_mode,
            "clientRequestId": uuid4().hex,
        },
    )
    assert response.status_code == 201
    return str(response.json()["recoveryId"])


def _session_key(database_path, recovery_id: str) -> str:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT session_key FROM recovery_access WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone()
    assert row is not None
    return str(row[0])


async def _connected() -> bool:
    return False


@pytest.mark.parametrize("asgi_spec_version", ["2.3", "2.4"])
def test_owner_reset_completes_admitted_pending_stream_and_releases_lease(
    tmp_path,
    asgi_spec_version: str,
) -> None:
    database_path = tmp_path / f"owner-reset-sse-{asgi_spec_version}.sqlite3"
    store = SQLiteStore(database_path)
    app = create_app(_settings(), store=store)
    with TestClient(app) as client:
        recovery_id = _create_recovery(
            client,
            scenario_id="hotel",
            execution_mode="sdk_stub",
        )
        session_key = _session_key(database_path, recovery_id)
        replay_scenarios = {scenario.id: scenario for scenario in ScenarioLoader().list()}
        initial_batch = store.read_initial_public_event_batch(
            recovery_id,
            session_key=session_key,
            replay_scenarios=replay_scenarios,
        )
        assert initial_batch[0]
        assert initial_batch[1].terminal is False

        controller = app.state.event_stream_admission
        lease = controller.try_acquire(recovery_id)
        assert lease is not None
        expires_at = datetime.now(UTC) + timedelta(minutes=1)
        response = _build_admitted_event_stream_response(
            lease_event_stream(
                stream_recovery_events(
                    store,
                    recovery_id,
                    after_seq=0,
                    is_disconnected=_connected,
                    heartbeat_seconds=60,
                    poll_interval_seconds=0,
                    initial_batch=initial_batch,
                    public_session_key=session_key,
                    public_replay_scenarios=replay_scenarios,
                    public_session_expires_at=expires_at,
                ),
                lease,
            ),
            lease,
            session_expires_at=expires_at,
        )

        never_disconnect = asyncio.Event()
        sent_messages: list[Message] = []
        emitted_frames = 0
        reset_count = 0
        final_body_lease_counts: list[int] = []

        async def receive() -> Message:
            await never_disconnect.wait()
            return {"type": "http.disconnect"}

        async def reset_after_initial_frames(message: Message) -> None:
            nonlocal emitted_frames, reset_count
            sent_messages.append(message)
            if message["type"] != "http.response.body":
                return
            body = message.get("body", b"")
            if body:
                emitted_frames += 1
                if emitted_frames == len(initial_batch[0]):
                    store.reset(session_key)
                    reset_count += 1
            elif message.get("more_body") is False:
                final_body_lease_counts.append(controller.active_count)

        async def invoke() -> None:
            scope: Scope = {
                "type": "http",
                "method": "GET",
                "path": f"/api/recoveries/{recovery_id}/events",
                "headers": [],
                "asgi": {"spec_version": asgi_spec_version},
            }
            await asyncio.wait_for(
                response(scope, receive, reset_after_initial_frames),
                timeout=1,
            )

        asyncio.run(invoke())

        assert reset_count == 1
        assert emitted_frames == len(initial_batch[0])
        assert sent_messages[-1] == {
            "type": "http.response.body",
            "body": b"",
            "more_body": False,
        }
        assert final_body_lease_counts == [0]
        assert controller.active_count == 0
        reacquired = controller.try_acquire(recovery_id)
        assert reacquired is not None
        reacquired.release()


def test_terminal_stream_then_owner_reset_keeps_stream_and_reset_finite(
    tmp_path,
) -> None:
    store = SQLiteStore(tmp_path / "terminal-stream-reset.sqlite3")
    app = create_app(_settings(), store=store)
    with TestClient(app) as client:
        recovery_id = _create_recovery(
            client,
            scenario_id="api-quota",
            execution_mode="replay_fixture",
        )

        streamed = client.get(f"/api/recoveries/{recovery_id}/events")
        reset = client.post("/api/demo/reset")
        reconnect = client.get(f"/api/recoveries/{recovery_id}/events")

        assert streamed.status_code == 200
        assert '"terminal":true' in streamed.text
        assert reset.status_code == 200
        assert reset.json() == {"reset": True}
        assert reconnect.status_code == 404
        assert app.state.event_stream_admission.active_count == 0


def test_owner_reset_detaches_shared_recovery_and_finishes_only_its_stream(
    tmp_path,
) -> None:
    database_path = tmp_path / "shared-recovery-owner-reset.sqlite3"
    store = SQLiteStore(database_path)
    app = create_app(_settings(), store=store)
    with TestClient(app) as owner, TestClient(app) as other:
        recovery_id = _create_recovery(
            owner,
            scenario_id="hotel",
            execution_mode="sdk_stub",
        )
        other_recovery_id = _create_recovery(
            other,
            scenario_id="api-quota",
            execution_mode="replay_fixture",
        )
        owner_session_key = _session_key(database_path, recovery_id)
        other_session_key = _session_key(database_path, other_recovery_id)
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                """
                INSERT INTO recovery_access (recovery_id, session_key)
                VALUES (?, ?)
                """,
                (recovery_id, other_session_key),
            )

        replay_scenarios = {scenario.id: scenario for scenario in ScenarioLoader().list()}
        initial_batch = store.read_initial_public_event_batch(
            recovery_id,
            session_key=owner_session_key,
            replay_scenarios=replay_scenarios,
        )
        controller = app.state.event_stream_admission
        lease = controller.try_acquire(recovery_id)
        assert lease is not None
        expires_at = datetime.now(UTC) + timedelta(minutes=1)

        async def exercise() -> None:
            stream = lease_event_stream(
                stream_recovery_events(
                    store,
                    recovery_id,
                    after_seq=0,
                    is_disconnected=_connected,
                    heartbeat_seconds=60,
                    poll_interval_seconds=0,
                    initial_batch=initial_batch,
                    public_session_key=owner_session_key,
                    public_replay_scenarios=replay_scenarios,
                    public_session_expires_at=expires_at,
                ),
                lease,
            )
            for _event in initial_batch[0]:
                await anext(stream)
            store.reset(owner_session_key)
            with pytest.raises(StopAsyncIteration):
                await anext(stream)

        asyncio.run(exercise())

        assert controller.active_count == 0
        assert not store.recovery_is_accessible(recovery_id, owner_session_key)
        assert store.recovery_is_accessible(recovery_id, other_session_key)
        other_snapshot = store.get_public_recovery(
            recovery_id,
            session_key=other_session_key,
            replay_scenarios=replay_scenarios,
        )
        assert other_snapshot.status.value == "pending_approval"


def test_missing_recovery_with_retained_access_remains_an_integrity_failure(
    tmp_path,
) -> None:
    database_path = tmp_path / "missing-recovery-with-access.sqlite3"
    store = SQLiteStore(database_path)
    app = create_app(_settings(), store=store)
    with TestClient(app) as client:
        recovery_id = _create_recovery(
            client,
            scenario_id="hotel",
            execution_mode="sdk_stub",
        )
        session_key = _session_key(database_path, recovery_id)
        replay_scenarios = {scenario.id: scenario for scenario in ScenarioLoader().list()}
        initial_batch = store.read_initial_public_event_batch(
            recovery_id,
            session_key=session_key,
            replay_scenarios=replay_scenarios,
        )
        controller = app.state.event_stream_admission
        lease = controller.try_acquire(recovery_id)
        assert lease is not None
        expires_at = datetime.now(UTC) + timedelta(minutes=1)

        async def exercise() -> None:
            stream = lease_event_stream(
                stream_recovery_events(
                    store,
                    recovery_id,
                    after_seq=0,
                    is_disconnected=_connected,
                    heartbeat_seconds=60,
                    poll_interval_seconds=0,
                    initial_batch=initial_batch,
                    public_session_key=session_key,
                    public_replay_scenarios=replay_scenarios,
                    public_session_expires_at=expires_at,
                ),
                lease,
            )
            for _event in initial_batch[0]:
                await anext(stream)
            with sqlite3.connect(database_path) as connection:
                connection.execute("PRAGMA foreign_keys = OFF")
                assert (
                    connection.execute(
                        "DELETE FROM recoveries WHERE id = ?",
                        (recovery_id,),
                    ).rowcount
                    == 1
                )
                assert connection.execute(
                    """
                        SELECT COUNT(*) FROM recovery_access
                        WHERE recovery_id = ? AND session_key = ?
                        """,
                    (recovery_id, session_key),
                ).fetchone() == (1,)
            with pytest.raises(
                PublicEvidenceIntegrityError,
                match="Public recovery access references a missing recovery",
            ):
                await anext(stream)

        asyncio.run(exercise())

        assert controller.active_count == 0
        reacquired = controller.try_acquire(recovery_id)
        assert reacquired is not None
        reacquired.release()
