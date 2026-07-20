from __future__ import annotations

from email.message import Message
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError

import pytest

from scripts.docker_smoke import (
    ShutdownResult,
    SmokeClient,
    SmokeFailure,
    _decision_payload,
    _launch_environment,
    _mint_deployed_replay_session,
    _open_sse_shutdown_evidence,
    _parse_sse,
    _require_loopback_qa_target,
    _stop_process,
    _validated_base_url,
    _validated_shutdown,
    _verify_qa_health,
)


def test_base_url_accepts_only_an_http_origin() -> None:
    assert _validated_base_url("http://127.0.0.1:8000/") == "http://127.0.0.1:8000"
    assert _validated_base_url("https://demo.example") == "https://demo.example"

    for unsafe in (
        "ftp://demo.example",
        "https://user:password@demo.example",
        "https://demo.example/path",
        "https://demo.example?query=1",
        "https://demo.example#fragment",
    ):
        with pytest.raises(SmokeFailure, match="base URL"):
            _validated_base_url(unsafe)


def test_deterministic_qa_target_must_be_loopback() -> None:
    for loopback in (
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "http://[::1]:8000",
    ):
        _require_loopback_qa_target(loopback)

    with pytest.raises(SmokeFailure, match="only loopback"):
        _require_loopback_qa_target("https://public.example")


def test_sse_parser_returns_ordered_identifiers_and_json_payloads() -> None:
    identifiers, payloads = _parse_sse(
        b'id: 1\ndata: {"terminal":false}\n\nid: 2\ndata: {"terminal":true}\n\n'
    )

    assert identifiers == [1, 2]
    assert payloads == [{"terminal": False}, {"terminal": True}]


def test_sse_parser_rejects_non_object_data() -> None:
    with pytest.raises(SmokeFailure, match="JSON object"):
        _parse_sse(b"id: 1\ndata: []\n\n")


def test_decision_payload_binds_all_exact_consent_identifiers() -> None:
    snapshot = {
        "pendingApproval": {
            "remedyId": "remedy-1",
            "remedyDigest": f"sha256:{'a' * 64}",
            "toolCallId": "tool-call-1",
        }
    }

    payload = _decision_payload(snapshot, decision="decline")

    assert payload["decision"] == "decline"
    assert payload["remedyId"] == "remedy-1"
    assert payload["remedyDigest"] == f"sha256:{'a' * 64}"
    assert payload["toolCallId"] == "tool-call-1"
    assert str(payload["clientDecisionId"]).startswith("production-smoke-decline-")


def test_health_requires_explicit_keyless_deterministic_qa_profile() -> None:
    _verify_qa_health(
        {
            "backend": "stub",
            "liveReady": False,
            "sdkStubReady": True,
            "providerBoundary": "demo_adapter_only",
        }
    )

    with pytest.raises(SmokeFailure, match="deterministic QA profile"):
        _verify_qa_health(
            {
                "backend": "stub",
                "liveReady": False,
                "sdkStubReady": False,
                "providerBoundary": "demo_adapter_only",
            }
        )


@pytest.mark.parametrize(
    ("profile", "deployed", "cors"),
    [
        ("deterministic-qa", "false", ""),
        ("deployed-readonly", "true", "https://backchannel-smoke.invalid"),
    ],
)
def test_launch_environment_wires_bundle_and_keeps_profiles_explicit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    profile: str,
    deployed: str,
    cors: str,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-child")
    repository_root = tmp_path / "repository"
    canary = "test-smoke-canary-that-is-at-least-32-bytes"

    environment = _launch_environment(
        repository_root=repository_root,
        temporary_path=tmp_path,
        port=8123,
        canary=canary,
        profile=profile,
    )

    assert "OPENAI_API_KEY" not in environment
    assert environment["BACKCHANNEL_DEPLOYED"] == deployed
    assert environment["BACKCHANNEL_DEMO_RESET_ENABLED"] == "false"
    assert environment["BACKCHANNEL_CORS_ORIGINS"] == cors
    assert environment["BACKCHANNEL_FRONTEND_DIST_PATH"] == str(
        repository_root / "web" / "dist"
    )
    assert environment["BACKCHANNEL_DB_PATH"] == str(
        tmp_path / "production.sqlite3"
    )
    assert environment["BACKCHANNEL_IDENTITY_HMAC_SECRET"] == canary
    assert environment["BACKCHANNEL_SMOKE_CANARY"] == canary
    assert environment["BACKCHANNEL_TRUSTED_PROXY_CIDRS"] == ""
    assert environment["PORT"] == "8123"


def test_shutdown_classification_rejects_ambiguous_preexit_and_nonzero() -> None:
    class FakeProcess:
        def __init__(self, *, poll_result: int | None, wait_result: int) -> None:
            self.returncode = poll_result
            self._poll_result = poll_result
            self._wait_result = wait_result
            self.terminated = False

        def poll(self) -> int | None:
            return self._poll_result

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, *, timeout: int) -> int:
            assert timeout == 10
            self.returncode = self._wait_result
            return self._wait_result

        def kill(self) -> None:
            raise AssertionError("kill must not be used in these classifications")

    preexited = _stop_process(
        cast(Any, FakeProcess(poll_result=0, wait_result=0))
    )
    clean = _stop_process(cast(Any, FakeProcess(poll_result=None, wait_result=0)))
    nonzero = _stop_process(cast(Any, FakeProcess(poll_result=None, wait_result=-15)))

    assert preexited.clean is False
    assert preexited.evidence == "already_exited_0"
    assert clean.clean is True
    assert clean.evidence == "sigterm_exit_0"
    assert nonzero.clean is False
    assert nonzero.evidence == "sigterm_exit_-15"

    missing_marker = _validated_shutdown(nonzero, b"application stopped")
    completed = _validated_shutdown(
        nonzero,
        b"Application shutdown complete.\nFinished server process [123]",
    )

    assert missing_marker.clean is False
    assert completed.clean is True
    assert completed.evidence == "sigterm_reraised_after_server_shutdown"


def test_open_sse_timeout_label_requires_ordered_uvicorn_log_evidence() -> None:
    shutdown = ShutdownResult(
        clean=True,
        evidence="sigterm_reraised_after_server_shutdown",
    )

    with pytest.raises(SmokeFailure, match="timeout cancellation marker"):
        _open_sse_shutdown_evidence(
            shutdown,
            b"Application shutdown complete.\nFinished server process [123]",
        )

    assert (
        _open_sse_shutdown_evidence(
            shutdown,
            b"timeout graceful shutdown exceeded\n"
            b"Application shutdown complete.\n"
            b"Finished server process [123]",
        )
        == "server_cancelled_within_grace_timeout"
    )


def test_deployed_replay_label_claims_only_observed_creation() -> None:
    class Response:
        @staticmethod
        def json_object() -> dict[str, object]:
            return {
                "status": "completed",
                "executionMode": "replay_fixture",
                "modelIds": [],
            }

    class Client:
        @staticmethod
        def post(path: str, payload: dict[str, object]) -> Response:
            assert path == "/api/recoveries"
            assert payload == {
                "scenarioId": "api-quota",
                "executionMode": "replay_fixture",
            }
            return Response()

    assert (
        _mint_deployed_replay_session(cast(Any, Client()))
        == "recorded_replay_fixture_created"
    )


class _DuplicateHeaderResponse:
    def __init__(self, headers: Message, *, body: bytes = b"{}") -> None:
        self.status = 200
        self.headers = headers
        self.closed = False
        self._body = body
        self._lines = iter(
            [b'data: {"terminal":false}\n', b"\n"]
        )

    def __enter__(self) -> _DuplicateHeaderResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def read(self, _limit: int) -> bytes:
        return self._body

    def readline(self, _limit: int) -> bytes:
        return next(self._lines, b"")

    def close(self) -> None:
        self.closed = True


class _FakeOpener:
    def __init__(self, response: _DuplicateHeaderResponse) -> None:
        self._response = response

    def open(self, _request: object, *, timeout: int) -> _DuplicateHeaderResponse:
        assert timeout == 10
        return self._response


@pytest.mark.parametrize(
    "canary",
    [
        "duplicate-header-canary-that-is-at-least-32-bytes",
        "秘密のカナリア" * 8,
        'quoted"header-canary-' * 4,
        "backslash\\header-canary-" * 4,
    ],
)
def test_request_scans_earlier_duplicate_header_for_canary(canary: str) -> None:
    headers = Message()
    headers.add_header("X-Duplicate-Proof", canary)
    headers.add_header("X-Duplicate-Proof", "later-safe-value")
    headers.add_header("Content-Type", "application/json")
    response = _DuplicateHeaderResponse(headers)
    client = SmokeClient("http://127.0.0.1:8000", canary)
    client._opener = cast(Any, _FakeOpener(response))

    with pytest.raises(SmokeFailure, match="canary secret"):
        client.get("/health")


def test_http_error_closes_when_duplicate_header_scan_rejects_response() -> None:
    canary = "error-header-canary-that-is-at-least-32-bytes"
    headers = Message()
    headers.add_header("X-Duplicate-Proof", canary)
    headers.add_header("X-Duplicate-Proof", "later-safe-value")
    headers.add_header("Content-Type", "application/json")

    class ErrorBody:
        def __init__(self) -> None:
            self.closed = False

        @staticmethod
        def read(_limit: int = -1) -> bytes:
            return b"{}"

        def close(self) -> None:
            self.closed = True

    error_body = ErrorBody()
    error = HTTPError(
        "http://127.0.0.1:8000/failure",
        500,
        "failure",
        headers,
        cast(Any, error_body),
    )

    class ErrorOpener:
        @staticmethod
        def open(_request: object, *, timeout: int) -> Any:
            assert timeout == 10
            raise error

    client = SmokeClient("http://127.0.0.1:8000", canary)
    client._opener = cast(Any, ErrorOpener())

    with pytest.raises(SmokeFailure, match="canary secret"):
        client.get("/failure", expected_status=500)
    assert error_body.closed is True


def test_pending_sse_scans_earlier_duplicate_header_for_forbidden_marker() -> None:
    headers = Message()
    headers.add_header("X-Duplicate-Proof", "identity_hmac_secret=leaked")
    headers.add_header("X-Duplicate-Proof", "later-safe-value")
    headers.add_header("Content-Type", "text/event-stream")
    response = _DuplicateHeaderResponse(headers)
    client = SmokeClient(
        "http://127.0.0.1:8000",
        "unrelated-canary-that-is-at-least-32-bytes",
    )
    client._opener = cast(Any, _FakeOpener(response))

    with pytest.raises(SmokeFailure, match="serialized state marker"):
        client.open_stream("/api/recoveries/example/events")
    assert response.closed is True
