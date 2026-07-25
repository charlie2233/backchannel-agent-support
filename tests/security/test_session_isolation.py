from __future__ import annotations

import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from threading import Barrier
from uuid import uuid4

from fastapi.testclient import TestClient

from server.config import RuntimeSettings
from server.controls import PublicIdentityHasher
from server.main import create_app
from server.store import SQLiteStore

_SECRET = "test-identity-secret-that-is-at-least-32-bytes"
_CREATE = {"scenarioId": "hotel", "executionMode": "replay_fixture"}


def _settings(**overrides: object) -> RuntimeSettings:
    values: dict[str, object] = {
        "live_ready": False,
        "identity_hmac_secret": _SECRET,
    }
    values.update(overrides)
    return RuntimeSettings(**values)  # type: ignore[arg-type]


def _cookie_value(response) -> str:
    parsed = SimpleCookie()
    parsed.load(response.headers["set-cookie"])
    return parsed["backchannel_demo_session"].value


def _decision_payload(
    snapshot: dict[str, object], *, decision: str = "approve"
) -> dict[str, str]:
    pending = snapshot["pendingApproval"]
    assert isinstance(pending, dict)
    return {
        "decision": decision,
        "clientDecisionId": str(uuid4()),
        "remedyId": str(pending["remedyId"]),
        "remedyDigest": str(pending["remedyDigest"]),
        "toolCallId": str(pending["toolCallId"]),
    }


def test_owner_scoped_successes_are_private_no_store_and_preserve_cors_vary(
    tmp_path,
) -> None:
    store = SQLiteStore(tmp_path / "owner-cache-policy.sqlite3")
    origin = "https://demo.example"
    app = create_app(
        _settings(cors_origins=(origin,)),
        store=store,
    )

    def assert_private_no_store(response, expected_status: int) -> None:
        assert response.status_code == expected_status
        assert response.headers["cache-control"] == "private, no-store"
        vary_tokens = {
            token.strip().lower()
            for token in response.headers.get("vary", "").split(",")
            if token.strip()
        }
        assert "origin" in vary_tokens
        assert "cookie" not in vary_tokens

    headers = {"Origin": origin}
    with TestClient(app) as owner:
        created = owner.post(
            "/api/recoveries",
            json={"scenarioId": "hotel", "executionMode": "sdk_stub"},
            headers=headers,
        )
        assert_private_no_store(created, 201)
        assert "set-cookie" in created.headers
        recovery_id = str(created.json()["recoveryId"])

        snapshot = owner.get(f"/api/recoveries/{recovery_id}", headers=headers)
        compact_snapshot = owner.get(
            f"/api/recoveries/{recovery_id.replace('-', '')}",
            headers=headers,
        )
        assert_private_no_store(snapshot, 200)
        assert_private_no_store(compact_snapshot, 200)

        approval = _decision_payload(created.json())
        approved = owner.post(
            f"/api/recoveries/{recovery_id}/decisions",
            json=approval,
            headers=headers,
        )
        replayed = owner.post(
            f"/api/recoveries/{recovery_id}/decisions",
            json=approval,
            headers=headers,
        )
        assert_private_no_store(approved, 200)
        assert_private_no_store(replayed, 200)

        declined_created = owner.post(
            "/api/recoveries",
            json={"scenarioId": "hotel", "executionMode": "sdk_stub"},
            headers=headers,
        )
        assert_private_no_store(declined_created, 201)
        declined = owner.post(
            f"/api/recoveries/{declined_created.json()['recoveryId']}/decisions",
            json=_decision_payload(declined_created.json(), decision="decline"),
            headers=headers,
        )
        assert_private_no_store(declined, 200)

        receipt = owner.get(f"/api/recoveries/{recovery_id}/receipt", headers=headers)
        events = owner.get(f"/api/recoveries/{recovery_id}/events", headers=headers)
        assert_private_no_store(receipt, 200)
        assert_private_no_store(events, 200)


def test_signed_cookie_binds_recovery_and_survives_restart(tmp_path) -> None:
    database_path = tmp_path / "session-restart.sqlite3"
    settings = _settings()
    first_store = SQLiteStore(database_path)

    with TestClient(create_app(settings, store=first_store)) as creator:
        created = creator.post("/api/recoveries", json=_CREATE)
        recovery_id = created.json()["recoveryId"]
        cookie = _cookie_value(created)
        assert created.status_code == 201
        assert re.fullmatch(r"v1\.[0-9]+\.[A-Za-z0-9_-]{43}\.[A-Za-z0-9_-]{43}", cookie)
        assert creator.get(f"/api/recoveries/{recovery_id}").status_code == 200
        reused = creator.post("/api/recoveries", json=_CREATE)
        assert reused.status_code == 201
        assert "set-cookie" not in reused.headers

    reopened = SQLiteStore(database_path)
    with TestClient(create_app(settings, store=reopened)) as restarted:
        restarted.cookies.set("backchannel_demo_session", cookie)
        response = restarted.get(f"/api/recoveries/{recovery_id}")

    assert response.status_code == 200
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT recovery_id, session_hash FROM recovery_access"
        ).fetchone()
    assert row is not None
    assert row[0] == recovery_id
    assert re.fullmatch(r"hmac-sha256:[0-9a-f]{64}", row[1])


def test_missing_foreign_and_tampered_sessions_fail_closed_without_rotation(
    tmp_path,
) -> None:
    store = SQLiteStore(tmp_path / "foreign.sqlite3")
    settings = _settings()
    with TestClient(create_app(settings, store=store)) as owner:
        created = owner.post("/api/recoveries", json=_CREATE)
        recovery_id = created.json()["recoveryId"]
        owner_cookie = _cookie_value(created)

        with TestClient(create_app(settings, store=store)) as foreign:
            foreign_created = foreign.post("/api/recoveries", json=_CREATE)
            assert foreign_created.status_code == 201
            foreign_response = foreign.get(f"/api/recoveries/{recovery_id}")

        with TestClient(create_app(settings, store=store)) as missing:
            missing_response = missing.get(f"/api/recoveries/{recovery_id}")

        with TestClient(create_app(settings, store=store)) as tampered:
            tampered.cookies.set(
                "backchannel_demo_session",
                owner_cookie[:-1] + ("A" if owner_cookie[-1] != "A" else "B"),
            )
            tampered_response = tampered.get(f"/api/recoveries/{recovery_id}")

    for response in (foreign_response, missing_response, tampered_response):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
        assert "set-cookie" not in response.headers


def test_invalid_or_tampered_create_never_sets_cookie_or_mutates_recoveries(
    tmp_path,
) -> None:
    store = SQLiteStore(tmp_path / "denied-create.sqlite3")
    settings = _settings()
    with TestClient(create_app(settings, store=store)) as client:
        invalid = client.post(
            "/api/recoveries",
            json={"scenarioId": "unknown", "executionMode": "replay_fixture"},
        )
        client.cookies.clear()
        client.cookies.set("backchannel_demo_session", "v1.9999999999." + "A" * 43 + "." + "B" * 43)
        tampered = client.post("/api/recoveries", json=_CREATE)

    assert invalid.status_code == 422
    assert tampered.status_code == 400
    assert "set-cookie" not in invalid.headers
    assert "set-cookie" not in tampered.headers
    assert store.count_recoveries() == 0


def test_expired_and_oversized_named_cookies_are_rejected_without_rotation(
    tmp_path,
) -> None:
    settings = _settings()
    store = SQLiteStore(tmp_path / "expired-cookie.sqlite3")
    codec = PublicIdentityHasher(_SECRET).demo_session_cookie_codec(
        lifetime_seconds=int(settings.demo_session_ttl.total_seconds())
    )
    expired, _credential = codec.mint(
        now=datetime.now(UTC) - settings.demo_session_ttl - timedelta(seconds=1)
    )
    with TestClient(create_app(settings, store=store)) as client:
        client.cookies.set("backchannel_demo_session", expired)
        expired_response = client.post("/api/recoveries", json=_CREATE)
        client.cookies.clear()
        client.cookies.set("backchannel_demo_session", "x" * 10_000)
        oversized_response = client.post("/api/recoveries", json=_CREATE)

    for response in (expired_response, oversized_response):
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"
        assert "set-cookie" not in response.headers
    assert store.count_recoveries() == 0


def test_malformed_cookie_header_fails_create_closed_but_valid_unrelated_cookie_does_not(
    tmp_path,
) -> None:
    store = SQLiteStore(tmp_path / "malformed-cookie.sqlite3")
    with TestClient(create_app(_settings(), store=store)) as client:
        malformed = client.post(
            "/api/recoveries",
            json=_CREATE,
            headers={"Cookie": "unrelated=valid; broken-cookie-segment"},
        )
        unrelated = client.post(
            "/api/recoveries",
            json=_CREATE,
            headers={"Cookie": "first=1; second=2"},
        )

    assert malformed.status_code == 400
    assert malformed.json()["error"]["code"] == "invalid_request"
    assert "set-cookie" not in malformed.headers
    assert unrelated.status_code == 201
    assert "set-cookie" in unrelated.headers
    assert store.count_recoveries() == 1


def test_legacy_unowned_recovery_is_not_backfilled_or_publicly_readable(tmp_path) -> None:
    database_path = tmp_path / "legacy.sqlite3"
    SQLiteStore(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO recoveries (
                id, scenario_id, execution_mode, status, current_step,
                current_step_summary, created_at, updated_at
            ) VALUES (
                '00000000-0000-4000-8000-000000000001', 'hotel',
                'replay_fixture', 'in_progress', 0, 'legacy',
                '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
            )
            """
        )

    reopened = SQLiteStore(database_path)
    settings = _settings()
    with TestClient(create_app(settings, store=reopened)) as client:
        owned = client.post("/api/recoveries", json=_CREATE)
        assert owned.status_code == 201
        response = client.get(
            "/api/recoveries/00000000-0000-4000-8000-000000000001"
        )

    assert response.status_code == 404
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM recovery_access "
            "WHERE recovery_id = '00000000-0000-4000-8000-000000000001'"
        ).fetchone() == (0,)


def test_public_endpoint_matrix_is_owner_scoped_and_cursor_has_no_oracle(
    tmp_path,
) -> None:
    store = SQLiteStore(tmp_path / "endpoint-matrix.sqlite3")
    settings = _settings()
    app = create_app(settings, store=store)
    with TestClient(app) as owner, TestClient(app) as foreign:
        terminal = owner.post("/api/recoveries", json=_CREATE)
        terminal_id = terminal.json()["recoveryId"]
        pending = owner.post(
            "/api/recoveries",
            json={"scenarioId": "hotel", "executionMode": "sdk_stub"},
        )
        pending_snapshot = pending.json()
        pending_id = pending_snapshot["recoveryId"]
        foreign.post("/api/recoveries", json=_CREATE)

        owner_responses = (
            owner.get(f"/api/recoveries/{terminal_id}"),
            owner.get(f"/api/recoveries/{terminal_id}/receipt"),
            owner.get(f"/api/recoveries/{terminal_id}/events"),
        )
        foreign_responses = (
            foreign.get(f"/api/recoveries/{terminal_id}"),
            foreign.get(f"/api/recoveries/{terminal_id}/receipt"),
            foreign.get(f"/api/recoveries/{terminal_id}/events"),
            foreign.get(
                f"/api/recoveries/{terminal_id}/events",
                headers={"Last-Event-ID": "not-an-integer"},
            ),
            foreign.post(
                f"/api/recoveries/{pending_id}/decisions",
                json=_decision_payload(pending_snapshot),
            ),
        )

        owner_bad_cursor = owner.get(
            f"/api/recoveries/{terminal_id}/events",
            headers={"Last-Event-ID": "not-an-integer"},
        )
        missing_id = uuid4()
        unknown = owner.get(f"/api/recoveries/{missing_id}")

    assert all(response.status_code == 200 for response in owner_responses)
    assert all(response.status_code == 404 for response in foreign_responses)
    assert all(response.json()["error"]["code"] == "not_found" for response in foreign_responses)
    assert all("set-cookie" not in response.headers for response in foreign_responses)
    assert owner_bad_cursor.status_code == 400
    assert unknown.status_code == 404
    assert store.count_decisions(pending_id) == 0


def test_foreign_and_nonexistent_recoveries_have_equivalent_not_found_bodies(
    tmp_path,
) -> None:
    store = SQLiteStore(tmp_path / "no-oracle-body.sqlite3")
    app = create_app(_settings(), store=store)
    with TestClient(app) as owner, TestClient(app) as foreign:
        recovery_id = owner.post("/api/recoveries", json=_CREATE).json()["recoveryId"]
        foreign.post("/api/recoveries", json=_CREATE)
        foreign_known = foreign.get(f"/api/recoveries/{recovery_id}")
        nonexistent = foreign.get(f"/api/recoveries/{uuid4()}")

    def normalized(response) -> dict[str, object]:
        body = response.json()
        body["error"]["requestId"] = "normalized"
        body["error"]["recoveryId"] = "normalized"
        return body

    assert foreign_known.status_code == nonexistent.status_code == 404
    assert normalized(foreign_known) == normalized(nonexistent)


def test_detach_before_atomic_claim_never_enters_orchestrator(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "decision-detach.sqlite3"
    store = SQLiteStore(database_path)
    app = create_app(_settings(), store=store)
    with TestClient(app) as owner:
        pending = owner.post(
            "/api/recoveries",
            json={"scenarioId": "hotel", "executionMode": "sdk_stub"},
        ).json()
        recovery_id = pending["recoveryId"]
        payload = _decision_payload(pending)
        barrier = Barrier(2)
        original_claim = store.claim_decision_for_session

        def claim_after_detach(*args, **kwargs):
            barrier.wait(timeout=5)
            barrier.wait(timeout=5)
            return original_claim(*args, **kwargs)

        monkeypatch.setattr(store, "claim_decision_for_session", claim_after_detach)
        orchestrator = app.state.recovery_orchestrator
        original_decide = orchestrator.decide
        entered_orchestrator = False

        async def tracked_decide(*args, **kwargs):
            nonlocal entered_orchestrator
            entered_orchestrator = True
            return await original_decide(*args, **kwargs)

        monkeypatch.setattr(orchestrator, "decide", tracked_decide)

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                owner.post,
                f"/api/recoveries/{recovery_id}/decisions",
                json=payload,
            )
            barrier.wait(timeout=5)
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    "DELETE FROM recovery_access WHERE recovery_id = ?",
                    (recovery_id,),
                )
            barrier.wait(timeout=5)
            response = future.result(timeout=10)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert entered_orchestrator is False
    assert app.state.live_gate.active == 0
    assert store.count_decisions(recovery_id) == 0


def test_scoped_reset_is_constant_bind_and_preserves_aggregate_history(tmp_path) -> None:
    database_path = tmp_path / "scoped-reset.sqlite3"
    settings = _settings(demo_reset_enabled=True)
    store = SQLiteStore(database_path)
    app = create_app(settings, store=store)
    with TestClient(app) as owner, TestClient(app) as foreign:
        owner_created = owner.post("/api/recoveries", json=_CREATE).json()
        foreign_created = foreign.post("/api/recoveries", json=_CREATE).json()
        with sqlite3.connect(database_path) as connection:
            owner_hash = connection.execute(
                "SELECT session_hash FROM recovery_access WHERE recovery_id = ?",
                (owner_created["recoveryId"],),
            ).fetchone()[0]
            foreign_hash = connection.execute(
                "SELECT session_hash FROM recovery_access WHERE recovery_id = ?",
                (foreign_created["recoveryId"],),
            ).fetchone()[0]
            now = datetime.now(UTC).isoformat()
            recoveries = [
                (
                    f"bulk-reset-{index}",
                    "hotel",
                    "replay_fixture",
                    "in_progress",
                    0,
                    "bulk",
                    now,
                    now,
                )
                for index in range(1_105)
            ]
            connection.executemany(
                """
                INSERT INTO recoveries (
                    id, scenario_id, execution_mode, status, current_step,
                    current_step_summary, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                recoveries,
            )
            connection.executemany(
                "INSERT INTO recovery_access (recovery_id, session_hash) VALUES (?, ?)",
                [(row[0], owner_hash) for row in recoveries],
            )
            connection.execute(
                "INSERT INTO recovery_access (recovery_id, session_hash) VALUES (?, ?)",
                (recoveries[-1][0], foreign_hash),
            )

        hasher = PublicIdentityHasher(_SECRET)
        store.claim_public_live_admission(
            session_hash=owner_hash,
            ip_hash=hasher.ip("127.0.0.1"),
            cooldown=timedelta(0),
            daily_budget=10,
            session_expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        before_usage = store.count_public_live_usage_rows()
        reset = owner.post("/api/demo/reset", json={})

    assert reset.status_code == 200
    assert reset.json() == {"reset": True}
    assert store.count_public_live_usage_rows() == before_usage
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM recovery_access WHERE session_hash = ?",
            (owner_hash,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM recoveries WHERE id = ?",
            (recoveries[-1][0],),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM recoveries WHERE id = ?",
            (foreign_created["recoveryId"],),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM public_live_cooldowns"
        ).fetchone()[0] > 0


def test_reset_without_valid_cookie_is_successful_noop_and_never_mints(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "reset-noop.sqlite3")
    settings = _settings(demo_reset_enabled=True)
    with TestClient(create_app(settings, store=store)) as owner:
        created = owner.post("/api/recoveries", json=_CREATE)
        recovery_id = created.json()["recoveryId"]
        with TestClient(create_app(settings, store=store)) as missing:
            missing_reset = missing.post("/api/demo/reset", json={})
        with TestClient(create_app(settings, store=store)) as invalid:
            invalid.cookies.set("backchannel_demo_session", "tampered")
            invalid_reset = invalid.post("/api/demo/reset", json={})

        assert owner.get(f"/api/recoveries/{recovery_id}").status_code == 200

    for response in (missing_reset, invalid_reset):
        assert response.status_code == 200
        assert response.json() == {"reset": True}
        assert "set-cookie" not in response.headers


def test_readiness_fails_generically_on_recovery_access_index_drift(tmp_path) -> None:
    database_path = tmp_path / "readiness-drift.sqlite3"
    store = SQLiteStore(database_path)
    with TestClient(create_app(_settings(), store=store)) as client:
        assert client.get("/readyz").status_code == 200
        with sqlite3.connect(database_path) as connection:
            connection.execute("DROP INDEX recovery_access_session_idx")
        drifted = client.get("/readyz")

    assert drifted.status_code == 503
    assert drifted.json()["error"]["code"] == "internal_error"
    assert "set-cookie" not in drifted.headers
