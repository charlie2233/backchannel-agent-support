from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from server.config import RuntimeSettings
from server.events import stream_recovery_events
from server.main import create_app
from server.providers.hotel_simulator import HotelSimulator
from server.replay.loader import ScenarioLoader
from server.store import PublicEvidenceIntegrityError, SQLiteStore
from tests.api.test_decisions import create_sdk_recovery, decision_payload
from tests.security.test_session_isolation import _stable_not_found_headers

IDENTITY_SECRET = "terminal-read-integrity-secret-0123456789abcdef"


@pytest.fixture
def terminal_clients(tmp_path: Path):
    database_path = tmp_path / "terminal-read-integrity.sqlite3"
    store = SQLiteStore(database_path)
    provider = HotelSimulator(store=store)
    app = create_app(
        RuntimeSettings(
            live_ready=False,
            identity_hash_secret=IDENTITY_SECRET,
        ),
        store=store,
        hotel_provider=provider,
    )
    with (
        TestClient(app, raise_server_exceptions=False) as owner,
        TestClient(app, raise_server_exceptions=False) as foreign,
    ):
        foreign.get("/health")
        yield owner, foreign, store, database_path, app
    store.close()


def _create_recovery(
    client: TestClient,
    *,
    scenario_id: str,
    execution_mode: str,
) -> dict[str, object]:
    response = client.post(
        "/api/recoveries",
        json={
            "scenarioId": scenario_id,
            "executionMode": execution_mode,
            "clientRequestId": uuid4().hex,
        },
    )
    assert response.status_code == 201
    return response.json()


def _create_replay_quota(client: TestClient) -> dict[str, object]:
    snapshot = _create_recovery(
        client,
        scenario_id="api-quota",
        execution_mode="replay_fixture",
    )
    assert snapshot["status"] == "completed"
    return snapshot


def _decline_hotel(client: TestClient) -> dict[str, object]:
    pending = create_sdk_recovery(client)
    recovery_id = str(pending["recoveryId"])
    response = client.post(
        f"/api/recoveries/{recovery_id}/decisions",
        json={
            **decision_payload(
                pending,
                client_decision_id=f"terminal-integrity-{uuid4().hex}",
            ),
            "action": "decline",
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "closed_without_action"
    return client.get(f"/api/recoveries/{recovery_id}").json()


def _approve_hotel(client: TestClient) -> dict[str, object]:
    pending = create_sdk_recovery(client)
    recovery_id = str(pending["recoveryId"])
    response = client.post(
        f"/api/recoveries/{recovery_id}/decisions",
        json=decision_payload(
            pending,
            client_decision_id=f"terminal-integrity-{uuid4().hex}",
        ),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    return client.get(f"/api/recoveries/{recovery_id}").json()


def _terminal_read_responses(
    client: TestClient,
    recovery_id: str,
) -> tuple[Any, Any, Any]:
    return (
        client.get(f"/api/recoveries/{recovery_id}"),
        client.get(f"/api/recoveries/{recovery_id}/receipt"),
        client.get(f"/api/recoveries/{recovery_id}/events"),
    )


def _assert_integrity_error(response: Any) -> None:
    assert response.status_code == 500
    assert response.json() == {
        "code": "internal_error",
        "message": "The request could not be completed.",
        "requestId": response.headers["x-request-id"],
    }
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"].startswith("application/json")


def _delete_receipt(connection: sqlite3.Connection, recovery_id: str) -> None:
    connection.execute(
        "DELETE FROM receipts WHERE recovery_id = ?",
        (recovery_id,),
    )


def _delete_terminal_event(connection: sqlite3.Connection, recovery_id: str) -> None:
    connection.execute(
        "DELETE FROM events WHERE recovery_id = ? AND terminal = 1",
        (recovery_id,),
    )


def _duplicate_terminal_event(connection: sqlite3.Connection, recovery_id: str) -> None:
    connection.execute(
        """
        INSERT INTO events (
            recovery_id, seq, type, terminal, data_json, created_at
        )
        SELECT
            recovery_id,
            (SELECT MAX(seq) + 1 FROM events WHERE recovery_id = ?),
            type,
            terminal,
            data_json,
            created_at
        FROM events
        WHERE recovery_id = ? AND terminal = 1
        """,
        (recovery_id, recovery_id),
    )


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(_delete_receipt, id="missing-receipt"),
        pytest.param(_delete_terminal_event, id="missing-terminal-event"),
        pytest.param(_duplicate_terminal_event, id="duplicate-terminal-event"),
    ],
)
def test_public_terminal_bundle_rejects_missing_or_duplicate_receipt_event(
    terminal_clients: tuple[TestClient, TestClient, SQLiteStore, Path, Any],
    mutate: Callable[[sqlite3.Connection, str], None],
) -> None:
    owner, _foreign, _store, database_path, _app = terminal_clients
    recovery_id = str(_create_replay_quota(owner)["recoveryId"])
    with sqlite3.connect(database_path) as connection:
        mutate(connection, recovery_id)

    for response in _terminal_read_responses(owner, recovery_id):
        _assert_integrity_error(response)


def test_public_receipt_rejects_schema_valid_cross_record_drift(
    terminal_clients: tuple[TestClient, TestClient, SQLiteStore, Path, Any],
) -> None:
    owner, _foreign, _store, database_path, _app = terminal_clients
    recovery_id = str(_create_replay_quota(owner)["recoveryId"])
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT receipt_json FROM receipts WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone()
        assert row is not None
        receipt = json.loads(str(row[0]))
        receipt["recoveryId"] = str(uuid4())
        connection.execute(
            "UPDATE receipts SET receipt_json = ? WHERE recovery_id = ?",
            (json.dumps(receipt, separators=(",", ":"), sort_keys=True), recovery_id),
        )

    for response in _terminal_read_responses(owner, recovery_id):
        _assert_integrity_error(response)


@pytest.mark.parametrize(
    "delete_sql",
    [
        pytest.param(
            "DELETE FROM approval_decisions WHERE recovery_id = ?",
            id="missing-decision",
        ),
        pytest.param(
            "DELETE FROM pending_approvals WHERE recovery_id = ?",
            id="missing-pending-approval",
        ),
        pytest.param(
            "DELETE FROM remedies WHERE recovery_id = ?",
            id="missing-remedy",
        ),
    ],
)
def test_public_approved_hotel_requires_complete_decision_authority(
    terminal_clients: tuple[TestClient, TestClient, SQLiteStore, Path, Any],
    delete_sql: str,
) -> None:
    owner, _foreign, _store, database_path, _app = terminal_clients
    recovery_id = str(_approve_hotel(owner)["recoveryId"])
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(delete_sql, (recovery_id,)).rowcount == 1

    responses = _terminal_read_responses(owner, recovery_id)
    for response in responses:
        _assert_integrity_error(response)


def test_public_sse_rejects_terminal_type_or_payload_drift_before_admission(
    terminal_clients: tuple[TestClient, TestClient, SQLiteStore, Path, Any],
) -> None:
    owner, _foreign, _store, database_path, app = terminal_clients
    recovery_id = str(_create_replay_quota(owner)["recoveryId"])
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            UPDATE events
            SET type = 'recovery.outcome_unknown',
                data_json = ?
            WHERE recovery_id = ? AND terminal = 1
            """,
            (
                json.dumps(
                    {
                        "providerExecution": True,
                        "summary": "Forged terminal evidence.",
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                recovery_id,
            ),
        )

    for response in _terminal_read_responses(owner, recovery_id):
        _assert_integrity_error(response)
    assert app.state.event_stream_admission.active_count == 0


def test_public_nonterminal_reads_reject_injected_terminal_evidence(
    terminal_clients: tuple[TestClient, TestClient, SQLiteStore, Path, Any],
) -> None:
    owner, _foreign, _store, database_path, app = terminal_clients
    pending = create_sdk_recovery(owner)
    recovery_id = str(pending["recoveryId"])
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO events (
                recovery_id, seq, type, terminal, data_json, created_at
            )
            SELECT
                ?,
                COALESCE(MAX(seq), 0) + 1,
                'recovery.completed',
                1,
                ?,
                MAX(created_at)
            FROM events
            WHERE recovery_id = ?
            """,
            (
                recovery_id,
                json.dumps(
                    {
                        "providerExecution": True,
                        "summary": "Injected terminal evidence.",
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                recovery_id,
            ),
        )

    snapshot_response = owner.get(f"/api/recoveries/{recovery_id}")
    receipt_response = owner.get(f"/api/recoveries/{recovery_id}/receipt")
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            owner.get,
            f"/api/recoveries/{recovery_id}/events",
        )
        deadline = time.monotonic() + 5
        while (
            not future.done()
            and app.state.event_stream_admission.active_count == 0
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        if not future.done():
            assert app.state.event_stream_admission.active_count == 1
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    """
                    UPDATE recoveries
                    SET status = 'completed', current_step = 5
                    WHERE id = ?
                    """,
                    (recovery_id,),
                )
        stream_response = future.result(timeout=5)

    for response in (snapshot_response, receipt_response, stream_response):
        _assert_integrity_error(response)
    assert app.state.event_stream_admission.active_count == 0


def test_public_sse_validates_terminal_bundle_even_when_cursor_skips_terminal_event(
    terminal_clients: tuple[TestClient, TestClient, SQLiteStore, Path, Any],
) -> None:
    owner, _foreign, _store, database_path, _app = terminal_clients
    recovery_id = str(_create_replay_quota(owner)["recoveryId"])
    with sqlite3.connect(database_path) as connection:
        terminal_row = connection.execute(
            """
            SELECT seq FROM events
            WHERE recovery_id = ? AND terminal = 1
            """,
            (recovery_id,),
        ).fetchone()
        assert terminal_row is not None
        terminal_seq = int(terminal_row[0])
        _delete_receipt(connection, recovery_id)

    response = owner.get(
        f"/api/recoveries/{recovery_id}/events",
        headers={"Last-Event-ID": str(terminal_seq)},
    )

    _assert_integrity_error(response)


def test_foreign_corrupt_terminal_bundle_remains_indistinguishable_from_absent(
    terminal_clients: tuple[TestClient, TestClient, SQLiteStore, Path, Any],
) -> None:
    owner, foreign, _store, database_path, _app = terminal_clients
    recovery_id = str(_create_replay_quota(owner)["recoveryId"])
    absent_id = str(uuid4())
    with sqlite3.connect(database_path) as connection:
        _delete_receipt(connection, recovery_id)

    for suffix in ("", "/receipt", "/events"):
        corrupt = foreign.get(f"/api/recoveries/{recovery_id}{suffix}")
        absent = foreign.get(f"/api/recoveries/{absent_id}{suffix}")
        assert corrupt.status_code == absent.status_code == 404
        assert corrupt.content == absent.content == b'{"detail":"Not found"}'
        assert _stable_not_found_headers(corrupt) == _stable_not_found_headers(absent)


def test_public_bundle_rechecks_session_access_inside_its_read_snapshot(
    terminal_clients: tuple[TestClient, TestClient, SQLiteStore, Path, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, foreign, store, _database_path, _app = terminal_clients
    recovery_id = str(_create_replay_quota(owner)["recoveryId"])
    monkeypatch.setattr(
        store,
        "recovery_is_accessible",
        lambda _recovery_id, _session_key: True,
    )

    for suffix in ("", "/receipt", "/events"):
        response = foreign.get(f"/api/recoveries/{recovery_id}{suffix}")
        assert response.status_code == 404
        assert response.content == b'{"detail":"Not found"}'


@pytest.mark.parametrize("suffix", ["", "/receipt", "/events"])
def test_stale_outer_access_cannot_expire_foreign_pending_recovery(
    terminal_clients: tuple[TestClient, TestClient, SQLiteStore, Path, Any],
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
) -> None:
    owner, foreign, store, database_path, _app = terminal_clients
    pending = create_sdk_recovery(owner)
    recovery_id = str(pending["recoveryId"])
    expired_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE remedies SET expiry = ? WHERE recovery_id = ?",
            (expired_at, recovery_id),
        )
    monkeypatch.setattr(
        store,
        "recovery_is_accessible",
        lambda _recovery_id, _session_key: True,
    )

    response = foreign.get(f"/api/recoveries/{recovery_id}{suffix}")

    assert response.status_code == 404
    assert response.content == b'{"detail":"Not found"}'
    with sqlite3.connect(database_path) as connection:
        recovery_row = connection.execute(
            "SELECT status, current_step FROM recoveries WHERE id = ?",
            (recovery_id,),
        ).fetchone()
        assert recovery_row == ("pending_approval", 3)
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM receipts WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                """
                SELECT COUNT(*) FROM events
                WHERE recovery_id = ? AND terminal = 1
                """,
                (recovery_id,),
            ).fetchone()[0]
            == 0
        )


def test_public_terminal_bundle_rejects_snapshot_seal_timestamp_drift(
    terminal_clients: tuple[TestClient, TestClient, SQLiteStore, Path, Any],
) -> None:
    owner, _foreign, _store, database_path, _app = terminal_clients
    recovery_id = str(_create_replay_quota(owner)["recoveryId"])
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE recoveries SET updated_at = ? WHERE id = ?",
            ("2099-01-01T00:00:00+00:00", recovery_id),
        )

    for response in _terminal_read_responses(owner, recovery_id):
        _assert_integrity_error(response)


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(
            "UPDATE recoveries SET created_at = '2000-01-01T00:00:00+00:00' "
            "WHERE id = ?",
            id="recovery-created-at",
        ),
        pytest.param(
            "UPDATE events SET created_at = '2099-01-01T00:00:00+00:00' "
            "WHERE recovery_id = ? AND seq = 1",
            id="nonterminal-event-created-at",
        ),
    ],
)
def test_public_replay_bundle_rejects_forged_atomic_chronology(
    terminal_clients: tuple[TestClient, TestClient, SQLiteStore, Path, Any],
    mutation: str,
) -> None:
    owner, _foreign, _store, database_path, _app = terminal_clients
    recovery_id = str(_create_replay_quota(owner)["recoveryId"])
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(mutation, (recovery_id,)).rowcount == 1

    for response in _terminal_read_responses(owner, recovery_id):
        _assert_integrity_error(response)


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(
            "UPDATE recoveries SET created_at = '2000-01-01T00:00:00+00:00' "
            "WHERE id = ?",
            id="snapshot-creation-drift",
        ),
        pytest.param(
            "UPDATE events SET created_at = '2099-01-01T00:00:00+00:00' "
            "WHERE recovery_id = ? AND seq = 1",
            id="creation-event-after-seal",
        ),
        pytest.param(
            "UPDATE events SET created_at = '2000-01-01T00:00:00+00:00' "
            "WHERE recovery_id = ? AND seq = 2",
            id="event-before-creation",
        ),
    ],
)
def test_public_dynamic_bundle_rejects_forged_event_chronology(
    terminal_clients: tuple[TestClient, TestClient, SQLiteStore, Path, Any],
    mutation: str,
) -> None:
    owner, _foreign, _store, database_path, _app = terminal_clients
    recovery_id = str(
        _create_recovery(
            owner,
            scenario_id="api-quota",
            execution_mode="sdk_stub",
        )["recoveryId"]
    )
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(mutation, (recovery_id,)).rowcount == 1

    for response in _terminal_read_responses(owner, recovery_id):
        _assert_integrity_error(response)


def test_followup_public_event_read_stays_read_only_until_initial_admission(
    terminal_clients: tuple[TestClient, TestClient, SQLiteStore, Path, Any],
) -> None:
    owner, _foreign, store, database_path, _app = terminal_clients
    pending = create_sdk_recovery(owner)
    recovery_id = str(pending["recoveryId"])
    expired_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE remedies SET expiry = ? WHERE recovery_id = ?",
            (expired_at, recovery_id),
        )
        access_row = connection.execute(
            "SELECT session_key FROM recovery_access WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone()
        assert access_row is not None
        session_key = str(access_row[0])
    replay_scenarios = {
        scenario.id: scenario for scenario in ScenarioLoader().list()
    }

    _events, status = store.read_public_event_batch(
        recovery_id,
        session_key=session_key,
        replay_scenarios=replay_scenarios,
    )

    assert status.value == "pending_approval"
    assert store.get_recovery(recovery_id).status.value == "pending_approval"

    admitted_events, admitted_status = store.read_initial_public_event_batch(
        recovery_id,
        session_key=session_key,
        replay_scenarios=replay_scenarios,
    )

    assert admitted_status.value == "closed_without_action"
    assert admitted_events[-1].type == "recovery.expired"
    assert admitted_events[-1].terminal is True


def test_public_integrity_failure_rolls_back_targeted_expiry_seal(
    terminal_clients: tuple[TestClient, TestClient, SQLiteStore, Path, Any],
) -> None:
    owner, _foreign, _store, database_path, _app = terminal_clients
    pending = create_sdk_recovery(owner)
    recovery_id = str(pending["recoveryId"])
    expired_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE remedies SET expiry = ? WHERE recovery_id = ?",
            (expired_at, recovery_id),
        )
        connection.execute(
            """
            UPDATE events
            SET data_json = '{"summary":"corrupt"}'
            WHERE recovery_id = ? AND seq = 1
            """,
            (recovery_id,),
        )

    response = owner.get(f"/api/recoveries/{recovery_id}")

    _assert_integrity_error(response)
    with sqlite3.connect(database_path) as connection:
        recovery_row = connection.execute(
            "SELECT status, current_step FROM recoveries WHERE id = ?",
            (recovery_id,),
        ).fetchone()
        assert recovery_row == ("pending_approval", 3)
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM receipts WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                """
                SELECT COUNT(*) FROM events
                WHERE recovery_id = ? AND terminal = 1
                """,
                (recovery_id,),
            ).fetchone()[0]
            == 0
        )


def test_public_sse_revalidates_integrity_after_admission(
    terminal_clients: tuple[TestClient, TestClient, SQLiteStore, Path, Any],
) -> None:
    owner, _foreign, store, database_path, _app = terminal_clients
    pending = create_sdk_recovery(owner)
    recovery_id = str(pending["recoveryId"])
    with sqlite3.connect(database_path) as connection:
        access_row = connection.execute(
            "SELECT session_key FROM recovery_access WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone()
        assert access_row is not None
        session_key = str(access_row[0])
    replay_scenarios = {
        scenario.id: scenario for scenario in ScenarioLoader().list()
    }
    initial_batch = store.read_initial_public_event_batch(
        recovery_id,
        session_key=session_key,
        replay_scenarios=replay_scenarios,
    )

    async def verify_post_admission_poll() -> None:
        stream = stream_recovery_events(
            store,
            recovery_id,
            after_seq=0,
            is_disconnected=lambda: asyncio.sleep(0, result=False),
            heartbeat_seconds=60,
            poll_interval_seconds=0,
            initial_batch=initial_batch,
            public_session_key=session_key,
            public_replay_scenarios=replay_scenarios,
        )
        for _event in initial_batch[0]:
            await anext(stream)
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                """
                UPDATE events
                SET created_at = '2099-01-01T00:00:00+00:00'
                WHERE recovery_id = ? AND seq = 1
                """,
                (recovery_id,),
            )
        with pytest.raises(PublicEvidenceIntegrityError):
            await anext(stream)
        await stream.aclose()

    asyncio.run(verify_post_admission_poll())


def test_representative_valid_terminal_bundles_remain_readable(
    terminal_clients: tuple[TestClient, TestClient, SQLiteStore, Path, Any],
) -> None:
    owner, _foreign, _store, _database_path, _app = terminal_clients
    replay_quota = _create_replay_quota(owner)
    sdk_quota = _create_recovery(
        owner,
        scenario_id="api-quota",
        execution_mode="sdk_stub",
    )
    declined_hotel = _decline_hotel(owner)

    for snapshot in (replay_quota, sdk_quota, declined_hotel):
        recovery_id = str(snapshot["recoveryId"])
        snapshot_response, receipt_response, stream_response = (
            _terminal_read_responses(owner, recovery_id)
        )
        assert snapshot_response.status_code == 200
        assert snapshot_response.json()["status"] in {
            "completed",
            "closed_without_action",
        }
        assert receipt_response.status_code == 200
        assert stream_response.status_code == 200
        terminal_events = [
            json.loads(line.removeprefix("data: "))
            for line in stream_response.text.splitlines()
            if line.startswith("data: ")
            and json.loads(line.removeprefix("data: "))["terminal"]
        ]
        assert len(terminal_events) == 1
