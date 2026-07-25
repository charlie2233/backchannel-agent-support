from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, tzinfo
from threading import Barrier
from typing import cast
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from server.agents.factory import HotelAgentContext, build_live_hotel_agents
from server.agents.versioning import (
    LIVE_BROKER_MODEL,
    LIVE_CONSUMER_MODEL,
    live_hotel_definition_digest,
)
from server.config import LIVE_HOTEL_AGENT_GRAPH_VERSION, RuntimeSettings
from server.main import create_app
from server.models import RecoveryStatus
from server.providers.hotel_simulator import HotelSimulator
from server.store import RecoveryNotFoundError, SQLiteStore
from tests.api.test_decisions import (
    claim_without_continuing,
    create_sdk_recovery,
)


@pytest.fixture
def claimed_expiry_client(tmp_path):
    store = SQLiteStore(tmp_path / "claimed-expiry-api.sqlite3")
    provider = HotelSimulator(store=store)
    app = create_app(
        RuntimeSettings(
            live_ready=False,
            terminal_cleanup_interval_seconds=300,
        ),
        store=store,
        hotel_provider=provider,
    )
    with TestClient(app) as client:
        yield client, store, provider


def _advance_cleanup_clock(
    monkeypatch: pytest.MonkeyPatch,
    *,
    expiry: datetime,
) -> None:
    frozen_now = expiry + timedelta(microseconds=1)

    class ExpiredConsentClock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            if tz is not None:
                return frozen_now.astimezone(tz)
            return frozen_now.replace(tzinfo=None)

    monkeypatch.setattr("server.cleanup.datetime", ExpiredConsentClock)


def _claim_approve(
    client: TestClient,
    store: SQLiteStore,
) -> tuple[dict[str, object], dict[str, str], str, datetime]:
    created = create_sdk_recovery(client)
    payload = claim_without_continuing(store, created)
    approval = created["pendingApproval"]
    assert isinstance(approval, dict)
    return (
        created,
        payload,
        str(created["recoveryId"]),
        datetime.fromisoformat(str(approval["expiry"])),
    )


def _event_payloads(response_text: str) -> list[dict[str, object]]:
    return [
        cast(dict[str, object], json.loads(line.removeprefix("data: ")))
        for line in response_text.splitlines()
        if line.startswith("data: ")
    ]


def _canonical_request_digest(store: SQLiteStore, recovery_id: str) -> str:
    consent = store.get_remedy_consent(recovery_id)
    serialized = json.dumps(
        {
            "recovery_id": recovery_id,
            "remedy": consent.evidence.remedy.model_dump(mode="json"),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _record_committed_execution(
    store: SQLiteStore,
    *,
    recovery_id: str,
    payload: dict[str, str],
):
    store.update_pending_approval_status(
        recovery_id,
        expected_status="pending",
        status="approved",
    )
    return store.record_completed_execution(
        execution_id=f"execution-committed-{recovery_id}",
        recovery_id=recovery_id,
        idempotency_key=(f"{recovery_id}:{payload['toolCallId']}:{payload['remedyDigest']}"),
        request_digest=_canonical_request_digest(store, recovery_id),
        tool_call_id=payload["toolCallId"],
        remedy_digest=payload["remedyDigest"],
        result_json={
            "dispatch_id": f"dispatch-committed-{recovery_id}",
            "status": "confirmed",
            "simulated": True,
            "provider_result": "Bound demo-provider result.",
        },
    )


def _promote_committed_fixture_to_live(
    store: SQLiteStore,
    provider: HotelSimulator,
    *,
    recovery_id: str,
    expiry: datetime,
) -> None:
    context = HotelAgentContext(
        recovery_id=recovery_id,
        store=store,
        hotel_provider=provider,
    )
    live_agents = build_live_hotel_agents(context=context)
    definition_digest = live_hotel_definition_digest(
        consumer_agent=live_agents.consumer,
        provider_agent=live_agents.provider,
        broker_agent=live_agents.broker,
    )
    model_ids_json = json.dumps(
        [LIVE_CONSUMER_MODEL, LIVE_BROKER_MODEL],
        separators=(",", ":"),
    )
    root_trace_id = "trace_0123456789abcdef0123456789abcdef"
    with sqlite3.connect(store._database_path) as connection:
        connection.execute(
            """
            UPDATE recoveries
            SET execution_mode = 'openai_live', model_ids_json = ?,
                root_trace_id = ?, model_call = 1,
                agent_graph_version = ?, definition_digest = ?
            WHERE id = ?
            """,
            (
                model_ids_json,
                root_trace_id,
                LIVE_HOTEL_AGENT_GRAPH_VERSION,
                definition_digest,
                recovery_id,
            ),
        )
        connection.execute(
            """
            UPDATE pending_approvals
            SET execution_mode = 'openai_live', model_ids_json = ?,
                root_trace_id = ?, agent_graph_version = ?,
                definition_digest = ?
            WHERE recovery_id = ?
            """,
            (
                model_ids_json,
                root_trace_id,
                LIVE_HOTEL_AGENT_GRAPH_VERSION,
                definition_digest,
                recovery_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO live_admissions (
                recovery_id, ip_key, session_key, budget_units,
                admitted_at, expires_at, released_at
            ) VALUES (?, ?, ?, 1, ?, ?, NULL)
            """,
            (
                recovery_id,
                "b" * 64,
                "a" * 64,
                (expiry - timedelta(minutes=5)).isoformat(),
                (expiry + timedelta(minutes=10)).isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO usage_ledger (
                recovery_id, category, amount, recorded_at
            ) VALUES (?, 'live_demo_budget_unit', 1, ?)
            """,
            (recovery_id, (expiry - timedelta(minutes=5)).isoformat()),
        )


def test_expired_claim_owner_decision_and_resume_are_stable_without_dispatch(
    claimed_expiry_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store, provider = claimed_expiry_client
    _created, payload, recovery_id, expiry = _claim_approve(client, store)
    _advance_cleanup_clock(monkeypatch, expiry=expiry)
    dispatch = Mock(side_effect=AssertionError("provider dispatch must not run"))
    live_guard = Mock(side_effect=AssertionError("live guard must not run"))
    monkeypatch.setattr(provider, "dispatch", dispatch)
    monkeypatch.setattr(
        client.app.state.public_demo_controls,
        "guard_live_resume",
        live_guard,
    )

    requests = [
        client.post(f"/api/recoveries/{recovery_id}/decisions", json=payload),
        client.post(
            f"/api/recoveries/{recovery_id}/decisions/resume",
            json={},
        ),
        client.post(f"/api/recoveries/{recovery_id}/decisions", json=payload),
        client.post(
            f"/api/recoveries/{recovery_id}/decisions/resume",
            json={},
        ),
    ]

    assert [response.status_code for response in requests] == [422] * 4
    assert [response.json()["detail"] for response in requests] == [
        {"code": "remedy_expired", "recoveryId": recovery_id}
    ] * 4
    dispatch.assert_not_called()
    live_guard.assert_not_called()
    assert provider.dispatch_count == 0
    assert store.count_executions(recovery_id) == 0
    assert store.count_decisions(recovery_id) == 1
    assert store.recovery_has_expiration_evidence(recovery_id) is True

    snapshot_response = client.get(f"/api/recoveries/{recovery_id}")
    receipt_response = client.get(f"/api/recoveries/{recovery_id}/receipt")
    events_response = client.get(f"/api/recoveries/{recovery_id}/events")

    assert snapshot_response.status_code == 200
    assert snapshot_response.json()["status"] == "outcome_unknown"
    assert snapshot_response.json()["pendingApproval"] is None
    assert snapshot_response.json()["claimedDecision"] is None
    assert receipt_response.status_code == 200
    receipt = receipt_response.json()
    assert receipt["status"] == "outcome_unknown"
    assert receipt["providerExecution"] is None
    assert receipt["approvalCount"] == 1
    assert receipt["approvedRemedyDigest"] is None
    assert events_response.status_code == 200
    terminal_events = [
        event for event in _event_payloads(events_response.text) if event["terminal"]
    ]
    assert len(terminal_events) == 1
    assert terminal_events[0]["type"] == "recovery.claim_expired"
    terminal_data = cast(dict[str, object], terminal_events[0]["data"])
    assert terminal_data["decisionAction"] == "approve"
    assert terminal_data["approvalDecisionCount"] == 1
    assert "executionCount" not in terminal_data


def test_post_expiry_resume_reconciles_canonical_committed_execution(
    claimed_expiry_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store, provider = claimed_expiry_client
    _created, payload, recovery_id, expiry = _claim_approve(client, store)
    store.update_pending_approval_status(
        recovery_id,
        expected_status="pending",
        status="approved",
    )
    execution, dispatched = store.record_completed_execution(
        execution_id="execution-completed-before-expiry",
        recovery_id=recovery_id,
        idempotency_key=(f"{recovery_id}:{payload['toolCallId']}:{payload['remedyDigest']}"),
        request_digest=_canonical_request_digest(store, recovery_id),
        tool_call_id=payload["toolCallId"],
        remedy_digest=payload["remedyDigest"],
        result_json={
            "dispatch_id": "dispatch-completed-before-expiry",
            "status": "confirmed",
            "simulated": True,
            "provider_result": "Bound demo-provider result.",
        },
    )
    assert dispatched is True
    frozen_now = expiry + timedelta(microseconds=1)
    _advance_cleanup_clock(monkeypatch, expiry=expiry)
    monkeypatch.setattr(store, "_now", lambda: frozen_now)
    dispatch = Mock(side_effect=AssertionError("provider dispatch must not run"))
    live_guard = Mock(side_effect=AssertionError("live guard must not run"))
    monkeypatch.setattr(provider, "dispatch", dispatch)
    monkeypatch.setattr(
        client.app.state.public_demo_controls,
        "guard_live_resume",
        live_guard,
    )

    responses = [
        client.post(
            f"/api/recoveries/{recovery_id}/decisions/resume",
            json={},
        )
        for _ in range(2)
    ]

    expected = {
        "action": "approve",
        "recoveryId": recovery_id,
        "remedyDigest": payload["remedyDigest"],
        "status": "completed",
        "approvedRemedyDigest": payload["remedyDigest"],
        "executionStarted": True,
    }
    assert [response.status_code for response in responses] == [200, 200]
    assert [response.json() for response in responses] == [expected, expected]
    dispatch.assert_not_called()
    live_guard.assert_not_called()
    assert provider.dispatch_count == 0
    assert store.count_executions(recovery_id) == 1
    assert store.get_completed_execution(recovery_id) == execution
    assert store.get_recovery(recovery_id).status is RecoveryStatus.COMPLETED
    assert store.get_receipt(recovery_id).provider_execution is True
    assert store.recovery_has_expiration_evidence(recovery_id) is False


def test_concurrent_post_expiry_resumes_replay_one_completed_response(
    claimed_expiry_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, store, provider = claimed_expiry_client
    _created, payload, recovery_id, expiry = _claim_approve(owner, store)
    execution, dispatched = _record_committed_execution(
        store,
        recovery_id=recovery_id,
        payload=payload,
    )
    assert dispatched is True
    frozen_now = expiry + timedelta(microseconds=1)
    _advance_cleanup_clock(monkeypatch, expiry=expiry)
    monkeypatch.setattr(store, "_now", lambda: frozen_now)
    dispatch = Mock(side_effect=AssertionError("provider dispatch must not run"))
    monkeypatch.setattr(provider, "dispatch", dispatch)
    cookie_name = "backchannel_demo_session"
    signed_cookie = owner.cookies.get(cookie_name)
    assert signed_cookie is not None
    barrier = Barrier(2)

    def post_resume() -> tuple[int, dict[str, object]]:
        contender = TestClient(owner.app)
        contender.cookies.set(cookie_name, signed_cookie)
        try:
            barrier.wait(timeout=10)
            response = contender.post(
                f"/api/recoveries/{recovery_id}/decisions/resume",
                json={},
            )
            return response.status_code, cast(dict[str, object], response.json())
        finally:
            contender.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: post_resume(), range(2)))

    expected = {
        "action": "approve",
        "recoveryId": recovery_id,
        "remedyDigest": payload["remedyDigest"],
        "status": "completed",
        "approvedRemedyDigest": payload["remedyDigest"],
        "executionStarted": True,
    }
    assert outcomes == [(200, expected), (200, expected)]
    dispatch.assert_not_called()
    assert provider.dispatch_count == 0
    assert store.get_completed_execution(recovery_id) == execution
    assert store.count_executions(recovery_id) == 1
    assert store.count_decisions(recovery_id) == 1
    assert store.get_recovery(recovery_id).status is RecoveryStatus.COMPLETED
    assert store.get_receipt(recovery_id).provider_execution is True
    assert len([event for event in store.list_events(recovery_id) if event.terminal]) == 1
    with sqlite3.connect(store._database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM receipts WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone() == (1,)
        assert connection.execute(
            """
            SELECT status, result_json IS NOT NULL, completed_at IS NOT NULL
            FROM approval_decisions WHERE recovery_id = ?
            """,
            (recovery_id,),
        ).fetchone() == ("completed", 1, 1)


@pytest.mark.parametrize(
    "tamper",
    [
        "status_only",
        "result_only",
        "completed_at_only",
        "forged_completed_tuple",
        "claimed_at_after_expiry",
    ],
)
def test_raw_decision_tuple_tamper_cannot_finalize_committed_result(
    claimed_expiry_client,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    client, store, provider = claimed_expiry_client
    _created, payload, recovery_id, expiry = _claim_approve(client, store)
    _execution, dispatched = _record_committed_execution(
        store,
        recovery_id=recovery_id,
        payload=payload,
    )
    assert dispatched is True
    completed_at = expiry + timedelta(microseconds=1)
    forged_response = json.dumps(
        {
            "action": "approve",
            "clientDecisionId": payload["clientDecisionId"],
            "recoveryId": recovery_id,
            "status": "completed",
            "approvedRemedyDigest": payload["remedyDigest"],
            "executionStarted": True,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    mutations = {
        "status_only": (
            "UPDATE approval_decisions SET status = 'completed' WHERE recovery_id = ?",
            (recovery_id,),
        ),
        "result_only": (
            "UPDATE approval_decisions SET result_json = '{}' WHERE recovery_id = ?",
            (recovery_id,),
        ),
        "completed_at_only": (
            "UPDATE approval_decisions SET completed_at = ? WHERE recovery_id = ?",
            (completed_at.isoformat(), recovery_id),
        ),
        "forged_completed_tuple": (
            """
            UPDATE approval_decisions
            SET status = 'completed', result_json = ?, completed_at = ?
            WHERE recovery_id = ?
            """,
            (forged_response, completed_at.isoformat(), recovery_id),
        ),
        "claimed_at_after_expiry": (
            "UPDATE approval_decisions SET claimed_at = ? WHERE recovery_id = ?",
            (completed_at.isoformat(), recovery_id),
        ),
    }
    statement, parameters = mutations[tamper]
    with sqlite3.connect(store._database_path) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(statement, parameters)
        connection.execute("PRAGMA ignore_check_constraints = OFF")

    frozen_now = expiry + timedelta(seconds=1)
    _advance_cleanup_clock(monkeypatch, expiry=expiry)
    monkeypatch.setattr(store, "_now", lambda: frozen_now)
    dispatch = Mock(side_effect=AssertionError("provider dispatch must not run"))
    live_guard = Mock(side_effect=AssertionError("live guard must not run"))
    monkeypatch.setattr(provider, "dispatch", dispatch)
    monkeypatch.setattr(
        client.app.state.public_demo_controls,
        "guard_live_resume",
        live_guard,
    )

    response = client.post(
        f"/api/recoveries/{recovery_id}/decisions/resume",
        json={},
    )

    assert response.status_code in {409, 422}
    assert response.json()["detail"]["code"] in {
        "resume_incompatible",
        "remedy_expired",
    }
    dispatch.assert_not_called()
    live_guard.assert_not_called()
    assert provider.dispatch_count == 0
    assert store.count_executions(recovery_id) == 1
    assert store.get_recovery(recovery_id).status is not RecoveryStatus.COMPLETED
    assert not any(
        event.type == "recovery.completed"
        for event in store.list_events(recovery_id)
        if event.terminal
    )
    with sqlite3.connect(store._database_path) as connection:
        completed_receipt = connection.execute(
            """
            SELECT 1 FROM receipts
            WHERE recovery_id = ?
              AND json_extract(receipt_json, '$.status') = 'completed'
            """,
            (recovery_id,),
        ).fetchone()
    assert completed_receipt is None


def test_expired_live_committed_crash_finalizes_before_live_capacity_guard(
    claimed_expiry_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store, provider = claimed_expiry_client
    _created, payload, recovery_id, expiry = _claim_approve(client, store)
    execution, dispatched = _record_committed_execution(
        store,
        recovery_id=recovery_id,
        payload=payload,
    )
    assert dispatched is True
    _promote_committed_fixture_to_live(
        store,
        provider,
        recovery_id=recovery_id,
        expiry=expiry,
    )
    frozen_now = expiry + timedelta(seconds=1)
    _advance_cleanup_clock(monkeypatch, expiry=expiry)
    monkeypatch.setattr(store, "_now", lambda: frozen_now)
    dispatch = Mock(side_effect=AssertionError("provider dispatch must not run"))
    live_guard = Mock(side_effect=AssertionError("live guard must not run"))
    monkeypatch.setattr(provider, "dispatch", dispatch)
    monkeypatch.setattr(
        client.app.state.public_demo_controls,
        "guard_live_resume",
        live_guard,
    )

    response = client.post(
        f"/api/recoveries/{recovery_id}/decisions/resume",
        json={},
    )

    assert response.status_code == 200
    assert response.json() == {
        "action": "approve",
        "recoveryId": recovery_id,
        "remedyDigest": payload["remedyDigest"],
        "status": "completed",
        "approvedRemedyDigest": payload["remedyDigest"],
        "executionStarted": True,
    }
    dispatch.assert_not_called()
    live_guard.assert_not_called()
    assert provider.dispatch_count == 0
    assert store.get_completed_execution(recovery_id) == execution
    assert store.count_executions(recovery_id) == 1
    assert store.get_recovery(recovery_id).status is RecoveryStatus.COMPLETED
    receipt = store.get_receipt(recovery_id)
    assert receipt.execution_mode.value == "openai_live"
    assert receipt.model_call is True
    assert receipt.model_ids == [LIVE_CONSUMER_MODEL, LIVE_BROKER_MODEL]
    assert receipt.provider_execution is True
    assert len([event for event in store.list_events(recovery_id) if event.terminal]) == 1
    with sqlite3.connect(store._database_path) as connection:
        assert connection.execute(
            """
            SELECT released_at IS NOT NULL FROM live_admissions
            WHERE recovery_id = ?
            """,
            (recovery_id,),
        ).fetchone() == (1,)
        assert connection.execute(
            """
            SELECT category, amount FROM usage_ledger
            WHERE recovery_id = ?
            """,
            (recovery_id,),
        ).fetchall() == [("live_demo_budget_unit", 1)]


@pytest.mark.parametrize(
    ("endpoint", "expected_code"),
    [
        ("decision", "decision_body_invalid"),
        ("resume", "decision_resume_body_invalid"),
    ],
)
def test_claimed_expiry_runs_only_after_authorized_body_validation(
    claimed_expiry_client,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    expected_code: str,
) -> None:
    client, store, provider = claimed_expiry_client
    _created, payload, recovery_id, expiry = _claim_approve(client, store)
    _advance_cleanup_clock(monkeypatch, expiry=expiry)
    events_before = store.list_events(recovery_id)

    suffix = "/decisions" if endpoint == "decision" else "/decisions/resume"
    malformed = client.post(
        f"/api/recoveries/{recovery_id}{suffix}",
        content="not-json",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )

    assert malformed.status_code == 422
    assert malformed.json()["detail"] == {
        "code": expected_code,
        "recoveryId": recovery_id,
    }
    assert store.get_recovery(recovery_id).status is RecoveryStatus.PENDING_APPROVAL
    assert store.list_events(recovery_id) == events_before
    assert store.recovery_has_expiration_evidence(recovery_id) is False
    with pytest.raises(RecoveryNotFoundError, match="Receipt not found"):
        store.get_receipt(recovery_id)
    assert store.count_decisions(recovery_id) == 1
    assert store.count_executions(recovery_id) == 0
    assert provider.dispatch_count == 0

    valid = (
        client.post(f"/api/recoveries/{recovery_id}/decisions", json=payload)
        if endpoint == "decision"
        else client.post(
            f"/api/recoveries/{recovery_id}/decisions/resume",
            json={},
        )
    )
    assert valid.status_code == 422
    assert valid.json()["detail"] == {
        "code": "remedy_expired",
        "recoveryId": recovery_id,
    }
    assert store.recovery_has_expiration_evidence(recovery_id) is True


def test_foreign_claimed_expiry_requests_are_404_without_owner_mutation(
    claimed_expiry_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, store, provider = claimed_expiry_client
    _created, payload, recovery_id, expiry = _claim_approve(owner, store)
    events_before = store.list_events(recovery_id)

    with TestClient(owner.app) as foreign:
        foreign.get("/health")
        _advance_cleanup_clock(monkeypatch, expiry=expiry)
        decision = foreign.post(
            f"/api/recoveries/{recovery_id}/decisions",
            json=payload,
        )
        resume = foreign.post(
            f"/api/recoveries/{recovery_id}/decisions/resume",
            content="not-json",
            headers={"Content-Type": "application/json"},
        )

    assert decision.status_code == resume.status_code == 404
    assert decision.json() == resume.json() == {"detail": "Not found"}
    assert store.get_recovery(recovery_id).status is RecoveryStatus.PENDING_APPROVAL
    assert store.list_events(recovery_id) == events_before
    assert store.recovery_has_expiration_evidence(recovery_id) is False
    with pytest.raises(RecoveryNotFoundError, match="Receipt not found"):
        store.get_receipt(recovery_id)
    assert store.count_decisions(recovery_id) == 1
    assert store.count_executions(recovery_id) == 0
    assert provider.dispatch_count == 0
