from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import cast
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from server.config import RuntimeSettings
from server.main import create_app
from server.providers.hotel_simulator import HotelSimulator
from server.store import SQLiteStore
from tests.api.test_decisions import (
    claim_without_continuing,
    create_sdk_recovery,
    decision_payload,
)


@pytest.fixture
def resume_client(tmp_path):
    database_path = tmp_path / "decision-resume-api.sqlite3"
    store = SQLiteStore(database_path)
    provider = HotelSimulator(store=store)
    app = create_app(
        RuntimeSettings(live_ready=False),
        store=store,
        hotel_provider=provider,
    )
    with TestClient(app) as client:
        yield client, store, provider, database_path


def test_claimed_snapshot_is_minimal_and_mutually_exclusive(resume_client) -> None:
    client, store, _provider, _database_path = resume_client
    created = create_sdk_recovery(client)
    payload = claim_without_continuing(store, created)

    snapshot = client.get(f"/api/recoveries/{created['recoveryId']}")

    assert snapshot.status_code == 200
    body = snapshot.json()
    assert body["pendingApproval"] is None
    assert body["claimedDecision"] == {
        "action": "approve",
        "remedyDigest": payload["remedyDigest"],
        "expiry": cast(dict[str, object], created["pendingApproval"])["expiry"],
    }
    assert set(body["claimedDecision"]) == {"action", "remedyDigest", "expiry"}
    serialized = json.dumps(body["claimedDecision"])
    for forbidden in (
        "clientDecisionId",
        "remedyId",
        "toolCallId",
        "fingerprint",
        "state_json",
        "requestBody",
    ):
        assert forbidden not in serialized


@pytest.mark.parametrize("action", ["approve", "decline"])
def test_explicit_resume_uses_the_stored_action(resume_client, action: str) -> None:
    client, store, provider, _database_path = resume_client
    created = create_sdk_recovery(client)
    payload = claim_without_continuing(store, created, action=action)
    recovery_id = str(created["recoveryId"])

    response = client.post(
        f"/api/recoveries/{recovery_id}/decisions/resume",
        json={},
    )

    assert response.status_code == 200
    assert response.json() == {
        "action": action,
        "recoveryId": recovery_id,
        "remedyDigest": payload["remedyDigest"],
        "status": "completed" if action == "approve" else "closed_without_action",
        "approvedRemedyDigest": payload["remedyDigest"] if action == "approve" else None,
        "executionStarted": True if action == "approve" else False,
    }
    assert provider.dispatch_count == (1 if action == "approve" else 0)
    assert store.count_executions(recovery_id) == (1 if action == "approve" else 0)


def test_completed_resume_replays_without_reexecution(resume_client) -> None:
    client, store, provider, _database_path = resume_client
    created = create_sdk_recovery(client)
    recovery_id = str(created["recoveryId"])
    payload = decision_payload(created)
    completed = client.post(f"/api/recoveries/{recovery_id}/decisions", json=payload)
    assert completed.status_code == 200
    before = provider.dispatch_count

    replayed = client.post(
        f"/api/recoveries/{recovery_id}/decisions/resume",
        json={},
    )

    assert replayed.status_code == 200
    assert replayed.json()["action"] == "approve"
    assert replayed.json()["remedyDigest"] == payload["remedyDigest"]
    assert provider.dispatch_count == before == 1
    assert store.count_executions(recovery_id) == 1


def test_original_and_resume_race_to_one_demo_dispatch_and_terminal_receipt(
    resume_client,
) -> None:
    owner, store, provider, _database_path = resume_client
    created = create_sdk_recovery(owner)
    recovery_id = str(created["recoveryId"])
    payload = claim_without_continuing(store, created)
    cookie_name = "backchannel_demo_session"
    signed_cookie = owner.cookies.get(cookie_name)
    assert signed_cookie is not None

    def post_original() -> tuple[int, dict[str, object]]:
        client = TestClient(owner.app)
        client.cookies.set(cookie_name, signed_cookie)
        response = client.post(f"/api/recoveries/{recovery_id}/decisions", json=payload)
        return response.status_code, response.json()

    def post_resume() -> tuple[int, dict[str, object]]:
        client = TestClient(owner.app)
        client.cookies.set(cookie_name, signed_cookie)
        response = client.post(
            f"/api/recoveries/{recovery_id}/decisions/resume",
            json={},
        )
        return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        original = pool.submit(post_original)
        resumed = pool.submit(post_resume)
        original_status, original_body = original.result(timeout=20)
        resumed_status, resumed_body = resumed.result(timeout=20)

    assert original_status == resumed_status == 200
    assert original_body["action"] == resumed_body["action"] == "approve"
    assert original_body["approvedRemedyDigest"] == resumed_body["remedyDigest"]
    assert provider.dispatch_count == 1
    assert store.count_executions(recovery_id) == 1
    assert len([event for event in store.list_events(recovery_id) if event.terminal]) == 1


def test_resume_missing_claim_is_owner_only_conflict_without_mutation(resume_client) -> None:
    client, store, provider, _database_path = resume_client
    created = create_sdk_recovery(client)
    recovery_id = str(created["recoveryId"])

    response = client.post(
        f"/api/recoveries/{recovery_id}/decisions/resume",
        json={},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "decision_resume_unavailable",
        "recoveryId": recovery_id,
    }
    assert store.count_decisions(recovery_id) == 0
    assert store.count_executions(recovery_id) == 0
    assert provider.dispatch_count == 0


def test_foreign_and_absent_resume_are_generic_404_before_malformed_body(
    resume_client,
) -> None:
    owner, _store, _provider, _database_path = resume_client
    created = create_sdk_recovery(owner)
    with TestClient(owner.app) as foreign:
        foreign_response = foreign.post(
            f"/api/recoveries/{created['recoveryId']}/decisions/resume",
            content="not-json",
            headers={"Content-Type": "application/json"},
        )
        absent_response = foreign.post(
            f"/api/recoveries/{uuid4()}/decisions/resume",
            content="not-json",
            headers={"Content-Type": "application/json"},
        )

    assert foreign_response.status_code == absent_response.status_code == 404
    assert foreign_response.json() == absent_response.json() == {"detail": "Not found"}


@pytest.mark.parametrize("body", [{"action": "approve"}, {"extra": None}, []])
def test_resume_rejects_every_nonempty_or_nonobject_body_after_auth(
    resume_client,
    body: object,
) -> None:
    client, store, provider, _database_path = resume_client
    created = create_sdk_recovery(client)
    recovery_id = str(created["recoveryId"])
    claim_without_continuing(store, created)

    response = client.post(
        f"/api/recoveries/{recovery_id}/decisions/resume",
        json=body,
    )

    assert response.status_code == 422
    assert store.count_executions(recovery_id) == 0
    assert provider.dispatch_count == 0


def test_resume_requires_json_content_type_after_auth(resume_client) -> None:
    client, store, provider, _database_path = resume_client
    created = create_sdk_recovery(client)
    recovery_id = str(created["recoveryId"])
    claim_without_continuing(store, created)

    response = client.post(
        f"/api/recoveries/{recovery_id}/decisions/resume",
        content="{}",
        headers={"Content-Type": "text/plain"},
    )

    assert response.status_code == 422
    assert store.count_executions(recovery_id) == 0
    assert provider.dispatch_count == 0


def test_resume_fingerprint_tamper_fails_closed(resume_client) -> None:
    client, store, provider, database_path = resume_client
    created = create_sdk_recovery(client)
    recovery_id = str(created["recoveryId"])
    claim_without_continuing(store, created)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE approval_decisions SET action = 'decline' WHERE recovery_id = ?",
            (recovery_id,),
        )

    response = client.post(
        f"/api/recoveries/{recovery_id}/decisions/resume",
        json={},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "resume_incompatible"
    assert store.count_executions(recovery_id) == 0
    assert provider.dispatch_count == 0


def test_expired_claim_fails_before_live_capacity_or_dispatch(
    resume_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store, provider, database_path = resume_client
    created = create_sdk_recovery(client)
    recovery_id = str(created["recoveryId"])
    claim_without_continuing(store, created)
    approval = cast(dict[str, object], created["pendingApproval"])
    expiry = datetime.fromisoformat(str(approval["expiry"]))
    monkeypatch.setattr(store, "_now", lambda: expiry + timedelta(seconds=1))
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE recoveries SET execution_mode = 'openai_live' WHERE id = ?",
            (recovery_id,),
        )
    controls = client.app.state.public_demo_controls
    guarded = False

    def unexpected_guard(_recovery_id: str) -> None:
        nonlocal guarded
        guarded = True

    monkeypatch.setattr(controls, "guard_live_resume", unexpected_guard)

    response = client.post(
        f"/api/recoveries/{recovery_id}/decisions/resume",
        json={},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "remedy_expired"
    assert guarded is False
    assert provider.dispatch_count == 0
    assert store.count_executions(recovery_id) == 0
    with sqlite3.connect(database_path) as connection:
        persisted_status = connection.execute(
            "SELECT status FROM recoveries WHERE id = ?",
            (recovery_id,),
        ).fetchone()
    assert persisted_status == ("pending_approval",)
    assert all(not event.terminal for event in store.list_events(recovery_id))


def test_incompatible_live_claim_fails_before_capacity_admission(
    resume_client,
) -> None:
    client, store, provider, database_path = resume_client
    created = create_sdk_recovery(client)
    recovery_id = str(created["recoveryId"])
    claim_without_continuing(store, created)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            UPDATE recoveries
            SET execution_mode = 'openai_live',
                model_ids_json = ?,
                root_trace_id = ?,
                model_call = 1,
                agent_graph_version = ?
            WHERE id = ?
            """,
            (
                '["gpt-5.6-luna","gpt-5.6-terra"]',
                "trace_0123456789abcdef0123456789abcdef",
                "backchannel.hotel-agent.live.v1",
                recovery_id,
            ),
        )
        connection.execute(
            """
            UPDATE pending_approvals
            SET sdk_version = 'stale-before-capacity'
            WHERE recovery_id = ?
            """,
            (recovery_id,),
        )
    controls = client.app.state.public_demo_controls
    guarded = False

    def record_guard(_recovery_id: str) -> None:
        nonlocal guarded
        guarded = True

    controls.guard_live_resume = record_guard

    response = client.post(
        f"/api/recoveries/{recovery_id}/decisions/resume",
        json={},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "resume_incompatible"
    assert guarded is False
    assert provider.dispatch_count == 0
    assert store.count_executions(recovery_id) == 0
