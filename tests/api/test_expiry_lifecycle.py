from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from server.config import RuntimeSettings
from server.main import create_app
from server.models import RecoveryStatus
from server.providers.hotel_simulator import HotelSimulator
from server.store import RecoveryNotFoundError, SQLiteStore

IDENTITY_SECRET = "expiry-lifecycle-test-secret-0123456789abcdef"


def _settings() -> RuntimeSettings:
    return RuntimeSettings(
        live_ready=False,
        identity_hash_secret=IDENTITY_SECRET,
        terminal_cleanup_interval_seconds=300,
    )


def _create_pending(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/api/recoveries",
        json={
            "scenarioId": "hotel",
            "executionMode": "sdk_stub",
            "clientRequestId": uuid4().hex,
        },
    )
    assert response.status_code == 201
    snapshot = response.json()
    assert snapshot["status"] == "pending_approval"
    return snapshot


def _expire(store: SQLiteStore, database_path, recovery_id: str) -> datetime:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT expiry FROM remedies WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone()
    assert row is not None
    expired_at = datetime.fromisoformat(str(row[0])) + timedelta(seconds=1)
    store._now = lambda: expired_at  # type: ignore[method-assign]
    return expired_at


def _use_expired_cleanup_clock(
    monkeypatch: pytest.MonkeyPatch,
    expired_at: datetime,
) -> None:
    from server.cleanup import expire_pending_approvals

    monkeypatch.setattr(
        "server.main.expire_pending_approvals",
        lambda store, **kwargs: expire_pending_approvals(
            store,
            now=expired_at,
            **kwargs,
        ),
    )


def _promote_pending_fixture_to_live(database_path, recovery_id: str) -> None:
    now = datetime.now(UTC)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            UPDATE recoveries
            SET execution_mode = 'openai_live',
                model_ids_json = ?, root_trace_id = ?, model_call = 1,
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
            SET execution_mode = 'openai_live', model_ids_json = ?,
                root_trace_id = ?, agent_graph_version = ?
            WHERE recovery_id = ?
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
            UPDATE events
            SET data_json = ?
            WHERE recovery_id = ? AND seq = 1
            """,
            (
                json.dumps(
                    {
                        "scenarioId": "hotel",
                        "executionMode": "openai_live",
                        "summary": "Recovery created for the selected execution mode.",
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
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
                now.isoformat(),
                (now + timedelta(minutes=10)).isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO usage_ledger (recovery_id, category, amount, recorded_at)
            VALUES (?, 'live_demo_budget_unit', 1, ?)
            """,
            (recovery_id, now.isoformat()),
        )


def _decision(snapshot: dict[str, object]) -> dict[str, str]:
    approval = snapshot["pendingApproval"]
    assert isinstance(approval, dict)
    return {
        "action": "approve",
        "clientDecisionId": "synchronous-expiry-decision",
        "remedyId": str(approval["remedyId"]),
        "remedyDigest": str(approval["remedyDigest"]),
        "toolCallId": str(approval["toolCallId"]),
    }


@pytest.mark.parametrize("first_endpoint", ["snapshot", "events", "receipt"])
def test_authorized_read_synchronously_expires_target_before_returning(
    tmp_path,
    first_endpoint: str,
) -> None:
    database_path = tmp_path / f"sync-{first_endpoint}.sqlite3"
    store = SQLiteStore(database_path)
    provider = HotelSimulator(store=store)
    with TestClient(create_app(_settings(), store=store, hotel_provider=provider)) as client:
        pending = _create_pending(client)
        recovery_id = str(pending["recoveryId"])
        _expire(store, database_path, recovery_id)

        suffix = {
            "snapshot": "",
            "events": "/events",
            "receipt": "/receipt",
        }[first_endpoint]
        response = client.get(f"/api/recoveries/{recovery_id}{suffix}")

        assert response.status_code == 200
        if first_endpoint == "snapshot":
            assert response.json()["status"] == "closed_without_action"
            assert response.json()["pendingApproval"] is None
        elif first_endpoint == "events":
            assert "recovery.expired" in response.text
            assert '"terminal":true' in response.text
        else:
            assert response.json()["status"] == "closed_without_action"
            assert response.json()["providerExecution"] is False

        assert store.get_recovery(recovery_id).status is RecoveryStatus.CLOSED_WITHOUT_ACTION
        assert store.get_receipt(recovery_id).status == "closed_without_action"
        assert [event.type for event in store.list_events(recovery_id) if event.terminal] == [
            "recovery.expired"
        ]
        assert store.count_decisions(recovery_id) == 0
        assert store.count_executions(recovery_id) == 0
        assert provider.dispatch_count == 0


def test_decision_request_expires_before_claim_and_returns_stable_public_code(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "sync-decision.sqlite3"
    store = SQLiteStore(database_path)
    provider = HotelSimulator(store=store)
    with TestClient(create_app(_settings(), store=store, hotel_provider=provider)) as client:
        pending = _create_pending(client)
        recovery_id = str(pending["recoveryId"])
        expired_at = _expire(store, database_path, recovery_id)
        _use_expired_cleanup_clock(monkeypatch, expired_at)

        response = client.post(
            f"/api/recoveries/{recovery_id}/decisions",
            json=_decision(pending),
        )

        assert response.status_code == 422
        assert response.json()["detail"] == {
            "code": "remedy_expired",
            "recoveryId": recovery_id,
        }
        durable_receipt = store.get_receipt(recovery_id)
        durable_events = store.list_events(recovery_id)
        retry_payloads = [
            _decision(pending),
            {
                **_decision(pending),
                "action": "decline",
                "clientDecisionId": "different-after-expiry",
            },
        ]
        for retry_payload in retry_payloads:
            retry = client.post(
                f"/api/recoveries/{recovery_id}/decisions",
                json=retry_payload,
            )
            assert retry.status_code == 422
            assert retry.json()["detail"] == {
                "code": "remedy_expired",
                "recoveryId": recovery_id,
            }
        assert store.get_recovery(recovery_id).status is RecoveryStatus.CLOSED_WITHOUT_ACTION
        assert store.get_receipt(recovery_id) == durable_receipt
        assert durable_receipt.provider_execution is False
        assert store.list_events(recovery_id) == durable_events
        assert store.count_decisions(recovery_id) == 0
        assert store.count_executions(recovery_id) == 0
        assert provider.dispatch_count == 0


def test_expired_live_decision_releases_without_lease_reacquire_or_rebilling(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "sync-live-decision.sqlite3"
    store = SQLiteStore(database_path)
    app = create_app(_settings(), store=store)
    guard_live_resume = Mock(side_effect=AssertionError("lease guard must not run"))
    app.state.public_demo_controls.guard_live_resume = guard_live_resume
    with TestClient(app) as client:
        pending = _create_pending(client)
        recovery_id = str(pending["recoveryId"])
        _promote_pending_fixture_to_live(database_path, recovery_id)
        expired_at = _expire(store, database_path, recovery_id)
        _use_expired_cleanup_clock(monkeypatch, expired_at)

        response = client.post(
            f"/api/recoveries/{recovery_id}/decisions",
            json=_decision(pending),
        )

        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "remedy_expired"
        guard_live_resume.assert_not_called()
        assert store.count_decisions(recovery_id) == 0
        assert store.count_executions(recovery_id) == 0
        with sqlite3.connect(database_path) as connection:
            assert (
                connection.execute(
                    "SELECT released_at FROM live_admissions WHERE recovery_id = ?",
                    (recovery_id,),
                ).fetchone()[0]
                is not None
            )
            assert connection.execute(
                "SELECT category, amount FROM usage_ledger WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchall() == [("live_demo_budget_unit", 1)]


def test_foreign_access_check_does_not_expire_an_owner_recovery(tmp_path) -> None:
    database_path = tmp_path / "expiry-access-first.sqlite3"
    store = SQLiteStore(database_path)
    app = create_app(_settings(), store=store)
    with TestClient(app) as owner, TestClient(app) as foreign:
        foreign.get("/health")
        pending = _create_pending(owner)
        recovery_id = str(pending["recoveryId"])
        _expire(store, database_path, recovery_id)

        denied = foreign.get(f"/api/recoveries/{recovery_id}/receipt")

        assert denied.status_code == 404
        assert denied.content == b'{"detail":"Not found"}'
        assert store.get_recovery(recovery_id).status is RecoveryStatus.PENDING_APPROVAL
        with pytest.raises(RecoveryNotFoundError, match="Receipt not found"):
            store.get_receipt(recovery_id)

        authorized = owner.get(f"/api/recoveries/{recovery_id}/receipt")
        assert authorized.status_code == 200
        assert authorized.json()["status"] == "closed_without_action"
