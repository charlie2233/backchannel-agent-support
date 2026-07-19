from __future__ import annotations

import json
import logging
from datetime import timedelta

from fastapi.testclient import TestClient

from server.config import RuntimeSettings
from server.logging import log_public_event, safe_recovery_log_id
from server.main import create_app
from server.models import ExecutionMode, ScenarioId
from server.store import SQLiteStore


def test_logging_correlation_accepts_only_canonical_recovery_uuids(caplog) -> None:
    recovery_id = "11111111-2222-4333-8444-555555555555"
    invalid = "recovery-prompt-canary"
    assert safe_recovery_log_id(recovery_id) == recovery_id
    assert safe_recovery_log_id(invalid) == "invalid"

    with caplog.at_level(logging.INFO, logger="server.public"):
        log_public_event(
            event="request_rejected",
            request_id="req_11111111111111111111111111111111",
            code="not_found",
            status_code=404,
            recovery_id=recovery_id,
        )
        log_public_event(
            event="request_rejected",
            request_id="req_22222222222222222222222222222222",
            code="not_found",
            status_code=404,
            recovery_id=invalid,
        )

    assert recovery_id in caplog.text
    assert invalid not in caplog.text


class _FailingOrchestrator:
    async def startup(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def start(self, scenario_id: object, *, execution_mode: ExecutionMode) -> object:
        del scenario_id, execution_mode
        raise RuntimeError(
            "prompt-canary authorization-canary sdk-state-canary tool-payload-canary"
        )

    async def decide(self, recovery_id: str, payload: object) -> object:
        del recovery_id, payload
        raise RuntimeError("decision-prompt-canary serialized-state-canary")


def _assert_generic_error(response, *, status: int, code: str) -> str:
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {
        "code",
        "message",
        "requestId",
        "recoveryId",
        "retryAfterSeconds",
        "fallback",
    }
    assert body["error"]["code"] == code
    assert body["error"]["message"]
    request_id = body["error"]["requestId"]
    assert request_id.startswith("req_")
    assert response.headers["x-request-id"] == request_id
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    return request_id


def test_malformed_uuid_json_media_and_method_use_stable_generic_errors(tmp_path) -> None:
    settings = RuntimeSettings(
        live_ready=False,
        identity_hmac_secret="test-identity-secret-that-is-at-least-32-bytes",
    )
    store = SQLiteStore(tmp_path / "public-errors.sqlite3")
    with TestClient(create_app(settings, store=store)) as client:
        malformed_uuid = client.get("/api/recoveries/not-a-uuid")
        malformed_json = client.post(
            "/api/recoveries",
            content=b'{"scenarioId":',
            headers={"Content-Type": "application/json"},
        )
        unsupported_media = client.post(
            "/api/recoveries",
            content=b"not-json",
            headers={"Content-Type": "text/plain"},
        )
        wrong_method = client.put("/health")

    _assert_generic_error(malformed_uuid, status=422, code="invalid_request")
    _assert_generic_error(malformed_json, status=422, code="invalid_request")
    _assert_generic_error(
        unsupported_media,
        status=415,
        code="unsupported_media_type",
    )
    _assert_generic_error(wrong_method, status=405, code="method_not_allowed")
    combined = json.dumps(
        [
            malformed_uuid.json(),
            malformed_json.json(),
            unsupported_media.json(),
            wrong_method.json(),
        ]
    ).lower()
    assert "uuid_parsing" not in combined
    assert "json_invalid" not in combined
    assert "input" not in combined


def test_internal_failure_response_and_allowlisted_log_disclose_no_raw_data(
    tmp_path,
    caplog,
) -> None:
    settings = RuntimeSettings(
        live_ready=True,
        identity_hmac_secret="test-identity-secret-that-is-at-least-32-bytes",
        live_cooldown=timedelta(0),
    )
    store = SQLiteStore(tmp_path / "safe-errors.sqlite3")
    app = create_app(
        settings,
        store=store,
        orchestrator=_FailingOrchestrator(),  # type: ignore[arg-type]
    )
    with caplog.at_level(logging.INFO, logger="server.public"):
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post(
                "/api/recoveries?query-canary=do-not-log",
                json={"scenarioId": "hotel", "executionMode": "openai_live"},
                headers={
                    "Authorization": "Bearer authorization-header-canary",
                    "Cookie": "backchannel_demo_session=cookie-canary",
                },
            )

    request_id = _assert_generic_error(
        response,
        status=500,
        code="internal_error",
    )
    public_body = response.text
    public_logs = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name == "server.public"
    )
    assert request_id in public_logs
    assert "internal_error" in public_logs
    for canary in (
        "prompt-canary",
        "authorization-canary",
        "sdk-state-canary",
        "tool-payload-canary",
        "query-canary",
        "authorization-header-canary",
        "cookie-canary",
        "RuntimeError",
    ):
        assert canary not in public_body
        assert canary not in public_logs


def test_recovery_scoped_validation_and_internal_errors_keep_safe_correlation(
    tmp_path,
    caplog,
) -> None:
    recovery_id = "11111111-2222-4333-8444-555555555555"
    settings = RuntimeSettings(
        live_ready=False,
        identity_hmac_secret="test-identity-secret-that-is-at-least-32-bytes",
    )
    store = SQLiteStore(tmp_path / "scoped-errors.sqlite3")
    store.create_recovery(
        recovery_id=recovery_id,
        scenario_id=ScenarioId.HOTEL,
        execution_mode=ExecutionMode.SDK_STUB,
        current_step=3,
        current_step_summary="Pending scoped error probe.",
    )
    app = create_app(
        settings,
        store=store,
        orchestrator=_FailingOrchestrator(),  # type: ignore[arg-type]
    )
    valid_decision = {
        "decision": "decline",
        "clientDecisionId": "scoped-error-decision",
        "remedyId": "scoped-error-remedy",
        "remedyDigest": f"sha256:{'a' * 64}",
        "toolCallId": "scoped-error-call",
    }

    with caplog.at_level(logging.INFO, logger="server.public"):
        with TestClient(app, raise_server_exceptions=False) as client:
            invalid_body = client.post(
                f"/api/recoveries/{recovery_id}/decisions",
                json={"decision": "decline"},
            )
            media_error = client.post(
                f"/api/recoveries/{recovery_id}/decisions",
                content=b"decision=decline",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            decision_failure = client.post(
                f"/api/recoveries/{recovery_id}/decisions",
                json=valid_decision,
            )

            original_get_recovery = store.get_recovery

            def failing_get_recovery(_recovery_id: str):
                raise RuntimeError("get-recovery-state-canary")

            store.get_recovery = failing_get_recovery  # type: ignore[method-assign]
            get_failure = client.get(f"/api/recoveries/{recovery_id}")
            store.get_recovery = original_get_recovery  # type: ignore[method-assign]

            original_get_receipt = store.get_receipt

            def failing_get_receipt(_recovery_id: str):
                raise RuntimeError("get-receipt-state-canary")

            store.get_receipt = failing_get_receipt  # type: ignore[method-assign]
            receipt_failure = client.get(f"/api/recoveries/{recovery_id}/receipt")
            store.get_receipt = original_get_receipt  # type: ignore[method-assign]

    for response, status, code in (
        (invalid_body, 422, "invalid_request"),
        (media_error, 415, "unsupported_media_type"),
        (decision_failure, 500, "internal_error"),
        (get_failure, 500, "internal_error"),
        (receipt_failure, 500, "internal_error"),
    ):
        assert response.status_code == status
        assert response.json()["error"]["code"] == code
        assert response.json()["error"]["recoveryId"] == recovery_id

    public_logs = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name == "server.public"
    )
    assert recovery_id in public_logs
    for canary in (
        "decision-prompt-canary",
        "serialized-state-canary",
        "get-recovery-state-canary",
        "get-receipt-state-canary",
        "RuntimeError",
    ):
        assert canary not in public_logs


def test_live_unavailable_and_limits_include_strict_explicit_replay_offer(tmp_path) -> None:
    settings = RuntimeSettings(
        live_ready=False,
        identity_hmac_secret="test-identity-secret-that-is-at-least-32-bytes",
    )
    store = SQLiteStore(tmp_path / "live-offer.sqlite3")
    with TestClient(create_app(settings, store=store)) as client:
        response = client.post(
            "/api/recoveries",
            json={"scenarioId": "hotel", "executionMode": "openai_live"},
        )

    assert response.status_code == 503
    detail = response.json()["error"]
    assert detail == {
        "code": "live_unavailable",
        "message": "Live mode is unavailable on this server.",
        "requestId": response.headers["x-request-id"],
        "recoveryId": None,
        "retryAfterSeconds": None,
        "fallback": {
            "kind": "show_replay_fixture",
            "scenarioId": "hotel",
            "executionMode": "replay_fixture",
        },
    }
    assert store.count_recoveries() == 0
