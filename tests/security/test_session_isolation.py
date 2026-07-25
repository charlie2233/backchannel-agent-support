from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from server.cleanup import cleanup_terminal_recoveries
from server.config import RuntimeSettings
from server.main import create_app
from server.models import ExecutionMode, RecoveryStatus, ScenarioId
from server.providers.hotel_simulator import HotelSimulator
from server.store import SQLiteStore

IDENTITY_SECRET = "session-isolation-test-secret-0123456789abcdef"
COOKIE_NAME = "backchannel_demo_session"


def _settings(*, reset: bool = False) -> RuntimeSettings:
    return RuntimeSettings(
        live_ready=False,
        demo_reset_enabled=reset,
        identity_hash_secret=IDENTITY_SECRET,
    )


def _create_hotel(client: TestClient) -> dict[str, object]:
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


def _decision(
    snapshot: dict[str, object],
    *,
    action: str,
    decision_id: str,
) -> dict[str, str]:
    approval = snapshot["pendingApproval"]
    assert isinstance(approval, dict)
    return {
        "action": action,
        "clientDecisionId": decision_id,
        "remedyId": str(approval["remedyId"]),
        "remedyDigest": str(approval["remedyDigest"]),
        "toolCallId": str(approval["toolCallId"]),
    }


def _stable_not_found_headers(response) -> dict[str, str]:
    return {
        name: response.headers[name]
        for name in (
            "cache-control",
            "content-security-policy",
            "content-type",
            "permissions-policy",
            "referrer-policy",
            "x-content-type-options",
            "x-frame-options",
        )
    }


def _assert_indistinguishable_not_found(foreign, absent) -> None:
    assert foreign.status_code == absent.status_code == 404
    assert foreign.content == absent.content == b'{"detail":"Not found"}'
    assert _stable_not_found_headers(foreign) == _stable_not_found_headers(absent)


def test_foreign_snapshot_sse_and_receipt_match_absent_recovery(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "foreign-reads.sqlite3")
    provider = HotelSimulator(store=store)
    app = create_app(_settings(), store=store, hotel_provider=provider)

    with TestClient(app) as owner, TestClient(app) as foreign:
        foreign.get("/health")
        snapshot = _create_hotel(owner)
        recovery_id = str(snapshot["recoveryId"])
        approved = owner.post(
            f"/api/recoveries/{recovery_id}/decisions",
            json=_decision(snapshot, action="approve", decision_id="owner-read-proof"),
        )
        assert approved.status_code == 200

        absent_id = str(uuid4())
        _assert_indistinguishable_not_found(
            foreign.get(f"/api/recoveries/{recovery_id}"),
            foreign.get(f"/api/recoveries/{absent_id}"),
        )
        _assert_indistinguishable_not_found(
            foreign.get(f"/api/recoveries/{recovery_id}/events"),
            foreign.get(f"/api/recoveries/{absent_id}/events"),
        )
        _assert_indistinguishable_not_found(
            foreign.get(
                f"/api/recoveries/{recovery_id}/events",
                headers={"Last-Event-ID": "not-an-integer"},
            ),
            foreign.get(
                f"/api/recoveries/{absent_id}/events",
                headers={"Last-Event-ID": "not-an-integer"},
            ),
        )
        _assert_indistinguishable_not_found(
            foreign.get(f"/api/recoveries/{recovery_id}/receipt"),
            foreign.get(f"/api/recoveries/{absent_id}/receipt"),
        )

        assert owner.get(f"/api/recoveries/{recovery_id}").status_code == 200
        assert owner.get(f"/api/recoveries/{recovery_id}/events").status_code == 200
        assert owner.get(f"/api/recoveries/{recovery_id}/receipt").status_code == 200


def test_foreign_approve_and_decline_cannot_claim_owner_decision(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "foreign-decisions.sqlite3")
    provider = HotelSimulator(store=store)
    app = create_app(_settings(), store=store, hotel_provider=provider)

    with TestClient(app) as owner, TestClient(app) as foreign:
        foreign.get("/health")
        snapshot = _create_hotel(owner)
        recovery_id = str(snapshot["recoveryId"])
        absent_id = str(uuid4())

        for action in ("approve", "decline"):
            payload = _decision(
                snapshot,
                action=action,
                decision_id=f"foreign-{action}",
            )
            _assert_indistinguishable_not_found(
                foreign.post(
                    f"/api/recoveries/{recovery_id}/decisions",
                    json=payload,
                ),
                foreign.post(
                    f"/api/recoveries/{absent_id}/decisions",
                    json=payload,
                ),
            )

        pending = owner.get(f"/api/recoveries/{recovery_id}")
        assert pending.status_code == 200
        assert pending.json()["status"] == "pending_approval"
        assert store.count_decisions(recovery_id) == 0
        assert store.count_executions(recovery_id) == 0
        assert provider.dispatch_count == 0

        payload = _decision(
            snapshot,
            action="approve",
            decision_id="owner-approves-once",
        )
        first = owner.post(
            f"/api/recoveries/{recovery_id}/decisions",
            json=payload,
        )
        duplicate = owner.post(
            f"/api/recoveries/{recovery_id}/decisions",
            json=payload,
        )
        assert first.status_code == duplicate.status_code == 200
        assert first.content == duplicate.content
        assert provider.dispatch_count == 1
        assert store.count_decisions(recovery_id) == 1
        assert store.count_executions(recovery_id) == 1


def test_owner_approval_wins_race_against_foreign_decline(tmp_path) -> None:
    database_path = tmp_path / "foreign-race.sqlite3"
    owner_store = SQLiteStore(database_path)
    foreign_store = SQLiteStore(database_path)
    owner_provider = HotelSimulator(store=owner_store)
    foreign_provider = HotelSimulator(store=foreign_store)
    owner_app = create_app(
        _settings(), store=owner_store, hotel_provider=owner_provider
    )
    foreign_app = create_app(
        _settings(), store=foreign_store, hotel_provider=foreign_provider
    )

    with TestClient(owner_app) as owner, TestClient(foreign_app) as foreign:
        foreign.get("/health")
        snapshot = _create_hotel(owner)
        recovery_id = str(snapshot["recoveryId"])
        barrier = Barrier(2)

        def submit(client: TestClient, action: str):
            barrier.wait(timeout=10)
            return client.post(
                f"/api/recoveries/{recovery_id}/decisions",
                json=_decision(
                    snapshot,
                    action=action,
                    decision_id=f"race-{action}",
                ),
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            owner_future = executor.submit(submit, owner, "approve")
            foreign_future = executor.submit(submit, foreign, "decline")
            owner_response = owner_future.result(timeout=20)
            foreign_response = foreign_future.result(timeout=20)

        assert owner_response.status_code == 200
        assert foreign_response.status_code == 404
        assert foreign_response.content == b'{"detail":"Not found"}'
        assert owner_store.count_decisions(recovery_id) == 1
        assert owner_store.count_executions(recovery_id) == 1
        assert owner_provider.dispatch_count + foreign_provider.dispatch_count <= 1


def test_signed_session_survives_restart_but_other_or_tampered_cookie_cannot_resume(
    tmp_path,
) -> None:
    database_path = tmp_path / "restart-session.sqlite3"
    first_store = SQLiteStore(database_path)
    with TestClient(create_app(_settings(), store=first_store)) as creator:
        snapshot = _create_hotel(creator)
        recovery_id = str(snapshot["recoveryId"])
        owner_cookie = creator.cookies.get(COOKIE_NAME)
    assert owner_cookie is not None
    first_store.close()

    restarted_store = SQLiteStore(database_path)
    restarted_provider = HotelSimulator(store=restarted_store)
    restarted_app = create_app(
        _settings(), store=restarted_store, hotel_provider=restarted_provider
    )
    with (
        TestClient(restarted_app) as owner,
        TestClient(restarted_app) as foreign,
        TestClient(restarted_app) as tampered,
    ):
        owner.cookies.set(COOKIE_NAME, owner_cookie)
        foreign.get("/health")
        replacement = "0" if owner_cookie[-1] != "0" else "1"
        tampered.cookies.set(COOKIE_NAME, owner_cookie[:-1] + replacement)

        assert owner.get(f"/api/recoveries/{recovery_id}").status_code == 200
        assert foreign.get(f"/api/recoveries/{recovery_id}").status_code == 404
        assert tampered.get(f"/api/recoveries/{recovery_id}").status_code == 404

        approved = owner.post(
            f"/api/recoveries/{recovery_id}/decisions",
            json=_decision(snapshot, action="approve", decision_id="restart-owner"),
        )
        assert approved.status_code == 200
        assert restarted_provider.dispatch_count == 1
        assert owner.get(f"/api/recoveries/{recovery_id}/receipt").status_code == 200
        assert foreign.get(f"/api/recoveries/{recovery_id}/receipt").status_code == 404
        assert tampered.get(f"/api/recoveries/{recovery_id}/events").status_code == 404


def test_enabled_reset_removes_only_callers_private_recoveries_and_admissions(
    tmp_path,
) -> None:
    database_path = tmp_path / "session-reset.sqlite3"
    store = SQLiteStore(database_path)
    app = create_app(_settings(reset=True), store=store)

    with TestClient(app) as first, TestClient(app) as second:
        first_snapshot = _create_hotel(first)
        second_snapshot = _create_hotel(second)
        first_id = str(first_snapshot["recoveryId"])
        second_id = str(second_snapshot["recoveryId"])
        now = datetime.now(UTC)

        with sqlite3.connect(database_path) as connection:
            first_session = connection.execute(
                "SELECT session_key FROM recovery_access WHERE recovery_id = ?",
                (first_id,),
            ).fetchone()[0]
            second_session = connection.execute(
                "SELECT session_key FROM recovery_access WHERE recovery_id = ?",
                (second_id,),
            ).fetchone()[0]
            for index, (recovery_id, session_key) in enumerate(
                ((first_id, first_session), (second_id, second_session)), start=1
            ):
                connection.execute(
                    "INSERT INTO live_admissions ("
                    "recovery_id, ip_key, session_key, budget_units, admitted_at, "
                    "expires_at, released_at) VALUES (?, ?, ?, 1, ?, ?, NULL)",
                    (
                        recovery_id,
                        str(index) * 64,
                        session_key,
                        now.isoformat(),
                        (now + timedelta(minutes=5)).isoformat(),
                    ),
                )
                connection.execute(
                    "INSERT INTO usage_ledger (recovery_id, category, amount, recorded_at) "
                    "VALUES (?, 'live_demo_budget_unit', 1, ?)",
                    (recovery_id, now.isoformat()),
                )

        reset = first.post("/api/demo/reset")
        assert reset.status_code == 200
        assert reset.json() == {"reset": True}
        assert first.get(f"/api/recoveries/{first_id}").status_code == 404
        assert second.get(f"/api/recoveries/{second_id}").status_code == 200
        assert first.get(f"/api/recoveries/{second_id}").status_code == 404

        with sqlite3.connect(database_path) as connection:
            assert connection.execute(
                "SELECT id FROM recoveries ORDER BY id"
            ).fetchall() == [(second_id,)]
            assert connection.execute(
                "SELECT recovery_id FROM pending_approvals ORDER BY recovery_id"
            ).fetchall() == [(second_id,)]
            admission_rows = connection.execute(
                "SELECT recovery_id, released_at FROM live_admissions "
                "ORDER BY recovery_id"
            ).fetchall()
            assert [row[0] for row in admission_rows] == sorted([first_id, second_id])
            released_by_recovery = dict(admission_rows)
            assert released_by_recovery[first_id] is not None
            assert released_by_recovery[second_id] is None
            assert connection.execute(
                "SELECT recovery_id FROM usage_ledger ORDER BY recovery_id"
            ).fetchall() == sorted([(first_id,), (second_id,)])


def test_replay_can_be_shared_then_reset_detaches_only_calling_session(tmp_path) -> None:
    database_path = tmp_path / "shared-replay.sqlite3"
    store = SQLiteStore(database_path)
    app = create_app(_settings(reset=True), store=store)
    def replay_payload() -> dict[str, str]:
        return {
            "scenarioId": "api-quota",
            "executionMode": "replay_fixture",
            "clientRequestId": uuid4().hex,
        }

    with TestClient(app) as first, TestClient(app) as second:
        first_created = first.post("/api/recoveries", json=replay_payload())
        second_created = second.post("/api/recoveries", json=replay_payload())
        assert first_created.status_code == second_created.status_code == 201
        recovery_id = str(first_created.json()["recoveryId"])
        assert second_created.json()["recoveryId"] == recovery_id

        with sqlite3.connect(database_path) as connection:
            columns = connection.execute(
                "PRAGMA table_info(recovery_access)"
            ).fetchall()
            assert [(row[1], row[5]) for row in columns] == [
                ("recovery_id", 1),
                ("session_key", 2),
            ]
            access_rows = connection.execute(
                "SELECT recovery_id, session_key FROM recovery_access "
                "WHERE recovery_id = ? ORDER BY session_key",
                (recovery_id,),
            ).fetchall()
            assert len(access_rows) == 2
            assert all(len(session_key) == 64 for _, session_key in access_rows)
            assert len({session_key for _, session_key in access_rows}) == 2
            assert first.cookies.get(COOKIE_NAME) not in {
                session_key for _, session_key in access_rows
            }
            assert second.cookies.get(COOKIE_NAME) not in {
                session_key for _, session_key in access_rows
            }

        assert first.post("/api/demo/reset").status_code == 200
        assert first.get(f"/api/recoveries/{recovery_id}").status_code == 404
        assert second.get(f"/api/recoveries/{recovery_id}").status_code == 200
        assert second.get(f"/api/recoveries/{recovery_id}/receipt").status_code == 200

        with sqlite3.connect(database_path) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM recoveries WHERE id = ?", (recovery_id,)
            ).fetchone() == (1,)
            assert connection.execute(
                "SELECT COUNT(*) FROM recovery_access WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone() == (1,)

        reassociated = first.post("/api/recoveries", json=replay_payload())
        assert reassociated.status_code == 201
        assert reassociated.json()["recoveryId"] == recovery_id
        assert first.get(f"/api/recoveries/{recovery_id}").status_code == 200


def test_session_reset_uses_bounded_sql_and_preserves_shared_recovery(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "bounded-session-reset.sqlite3"
    store = SQLiteStore(database_path)
    owner_session = "1" * 64
    shared_session = "2" * 64
    private_ids = [str(uuid4()) for _ in range(12)]
    shared_id = str(uuid4())

    for recovery_id in [*private_ids, shared_id]:
        store.create_recovery(
            recovery_id=recovery_id,
            scenario_id=ScenarioId.API_QUOTA,
            execution_mode=ExecutionMode.REPLAY_FIXTURE,
            current_step=0,
            current_step_summary="Bounded reset fixture.",
            session_key=owner_session,
        )
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO recovery_access (recovery_id, session_key) VALUES (?, ?)",
            (shared_id, shared_session),
        )

    original_connect = store._connect

    def connect_with_low_variable_limit() -> sqlite3.Connection:
        connection = original_connect()
        connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 4)
        return connection

    monkeypatch.setattr(store, "_connect", connect_with_low_variable_limit)

    store.reset(owner_session)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM recoveries WHERE id IN ("
            + ",".join("?" for _ in private_ids)
            + ")",
            private_ids,
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM recoveries WHERE id = ?",
            (shared_id,),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT session_key FROM recovery_access WHERE recovery_id = ?",
            (shared_id,),
        ).fetchall() == [(shared_session,)]


def test_legacy_private_recovery_is_fail_closed_and_cannot_be_first_touch_claimed(
    tmp_path,
) -> None:
    database_path = tmp_path / "legacy-unowned.sqlite3"
    store = SQLiteStore(database_path)
    recovery_id = "11111111-2222-4333-8444-555555555555"
    store.create_recovery(
        recovery_id=recovery_id,
        scenario_id=ScenarioId.HOTEL,
        execution_mode=ExecutionMode.SDK_STUB,
        current_step=0,
        current_step_summary="Legacy recovery without a trustworthy session owner.",
        sdk_version="0.18.3",
        protocol_version="backchannel.approval.v1",
        agent_graph_version="backchannel.hotel-agent.v1",
        definition_digest="a" * 64,
    )
    app = create_app(_settings(), store=store)

    with TestClient(app) as client:
        assert client.get(f"/api/recoveries/{recovery_id}").status_code == 404
        assert client.get(f"/api/recoveries/{recovery_id}/events").status_code == 404
        client.post(
            "/api/recoveries",
            json={
                "scenarioId": "api-quota",
                "executionMode": "replay_fixture",
                "clientRequestId": uuid4().hex,
            },
        )
        assert client.get(f"/api/recoveries/{recovery_id}").status_code == 404

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM recovery_access WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone() == (0,)


def test_terminal_cleanup_cascades_access_without_erasing_usage(tmp_path) -> None:
    database_path = tmp_path / "access-cleanup.sqlite3"
    store = SQLiteStore(database_path)
    recovery_id = "22222222-3333-4444-8555-666666666666"
    session_key = "a" * 64
    store.create_recovery(
        recovery_id=recovery_id,
        scenario_id=ScenarioId.HOTEL,
        execution_mode=ExecutionMode.REPLAY_FIXTURE,
        current_step=0,
        current_step_summary="Cleanup ownership fixture.",
        session_key=session_key,
    )
    store.record_transition(
        recovery_id,
        status=RecoveryStatus.COMPLETED,
        current_step=5,
        current_step_summary="Cleanup ownership fixture completed.",
        event_type="recovery.completed",
        event_data={"summary": "Cleanup ownership fixture completed."},
    )
    now = datetime.now(UTC)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE recoveries SET updated_at = ? WHERE id = ?",
            ((now - timedelta(days=2)).isoformat(), recovery_id),
        )
        connection.execute(
            "INSERT INTO usage_ledger (recovery_id, category, amount, recorded_at) "
            "VALUES (?, 'replay_fixture_start', 1, ?)",
            (recovery_id, now.isoformat()),
        )

    assert cleanup_terminal_recoveries(
        store,
        terminal_ttl=timedelta(days=1),
        now=now,
    ) == 1
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM recovery_access WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT category, amount FROM usage_ledger WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchall() == [("replay_fixture_start", 1)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_session_reset_cannot_erase_ip_or_session_cooldown_history(tmp_path) -> None:
    database_path = tmp_path / "reset-cooldown.sqlite3"
    store = SQLiteStore(database_path)
    recovery_id = "33333333-4444-4555-8666-777777777777"
    owner_session = "b" * 64
    now = datetime.now(UTC)
    store.create_recovery(
        recovery_id=recovery_id,
        scenario_id=ScenarioId.HOTEL,
        execution_mode=ExecutionMode.OPENAI_LIVE,
        current_step=0,
        current_step_summary="Live cooldown reset fixture.",
        model_ids=["gpt-5.6-luna", "gpt-5.6-terra"],
        root_trace_id="trace_0123456789abcdef0123456789abcdef",
        model_call=True,
        sdk_version="0.18.3",
        protocol_version="backchannel.approval.v1",
        agent_graph_version="backchannel.hotel-agent.live.v1",
        definition_digest="a" * 64,
        session_key=owner_session,
    )
    assert store.try_admit_live_recovery(
        recovery_id=recovery_id,
        ip_key="c" * 64,
        session_key=owner_session,
        max_active=10,
        ip_cooldown=timedelta(hours=1),
        session_cooldown=timedelta(hours=1),
        daily_budget_units=10,
        lease_ttl=timedelta(minutes=5),
        now=now,
    ) is None

    store.reset(owner_session)

    common = {
        "max_active": 10,
        "ip_cooldown": timedelta(hours=1),
        "session_cooldown": timedelta(hours=1),
        "daily_budget_units": 10,
        "lease_ttl": timedelta(minutes=5),
        "now": now + timedelta(seconds=1),
    }
    assert store.try_admit_live_recovery(
        recovery_id="44444444-5555-4666-8777-888888888888",
        ip_key="c" * 64,
        session_key="d" * 64,
        **common,
    ) == "cooldown"
    assert store.try_admit_live_recovery(
        recovery_id="55555555-6666-4777-8888-999999999999",
        ip_key="e" * 64,
        session_key=owner_session,
        **common,
    ) == "cooldown"
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT amount FROM usage_ledger WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchall() == [(1,)]
        admission = connection.execute(
            "SELECT released_at FROM live_admissions WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone()
        assert admission is not None
        assert admission[0] is not None


def test_store_rejects_malformed_access_migration_instead_of_serving_it(
    tmp_path,
) -> None:
    database_path = tmp_path / "malformed-access.sqlite3"
    SQLiteStore(database_path).close()
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE recovery_access")
        connection.execute(
            """
            CREATE TABLE recovery_access (
                recovery_id TEXT NOT NULL,
                session_key TEXT NOT NULL,
                PRIMARY KEY (recovery_id, session_key)
            )
            """
        )

    with pytest.raises(RuntimeError, match="recovery access foreign key"):
        SQLiteStore(database_path)
