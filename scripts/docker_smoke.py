"""Bounded HTTP smoke proof for the packaged one-process application.

The production image defaults to ``BACKCHANNEL_DEPLOYED=true``. That mode uses
Secure cookies and intentionally disables the deterministic SDK stub. Full
approval and rejection checks therefore run only in the explicit
``deterministic-qa`` profile, on loopback or inside an isolated container
network namespace. ``deployed-readonly`` separately checks the hardened public
defaults without pretending that HTTP can exercise a production live workflow.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from email.message import Message
from http.cookiejar import CookieJar
from http.cookies import SimpleCookie
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

SmokeProfile = Literal["deterministic-qa", "deployed-readonly"]
MAX_RESPONSE_BYTES = 2_000_000
REQUEST_TIMEOUT_SECONDS = 10
STARTUP_TIMEOUT_SECONDS = 60
SHUTDOWN_TIMEOUT_SECONDS = 10
FORBIDDEN_PUBLIC_MARKERS = (
    b"openai_api_key",
    b"identity_hmac_secret",
    b"state_json",
    b"consumer_proof",
    b"provider_proof",
    b"authorization: bearer",
)


class SmokeFailure(RuntimeError):
    """A stable smoke error that never includes response or secret contents."""


@dataclass(frozen=True, slots=True)
class SmokeResponse:
    status: int
    headers: dict[str, str]
    body: bytes

    def json_object(self) -> dict[str, Any]:
        try:
            value = json.loads(self.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SmokeFailure("Expected a JSON response object") from error
        if not isinstance(value, dict):
            raise SmokeFailure("Expected a JSON response object")
        return value


@dataclass(frozen=True, slots=True)
class ShutdownResult:
    clean: bool
    evidence: str


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def _validated_base_url(value: str) -> str:
    candidate = value.strip()
    parsed = urlsplit(candidate)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise SmokeFailure("Smoke base URL must be one HTTP(S) origin")
    return candidate.rstrip("/")


def _require_loopback_qa_target(base_url: str) -> None:
    hostname = urlsplit(base_url).hostname
    if hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise SmokeFailure(
            "Deterministic QA may target only loopback inside the host or container"
        )


class SmokeClient:
    def __init__(self, base_url: str, canary: str) -> None:
        self.base_url = _validated_base_url(base_url)
        self._canary = canary.encode("utf-8")
        self._opener = build_opener(HTTPCookieProcessor(CookieJar()))
        self.set_cookie_headers: list[str] = []

    def _scan_public_bytes(self, value: bytes, *, context: str) -> None:
        lowered = value.lower()
        if self._canary and self._canary.lower() in lowered:
            raise SmokeFailure(f"The canary secret appeared in {context}")
        if any(marker in lowered for marker in FORBIDDEN_PUBLIC_MARKERS):
            raise SmokeFailure("An internal secret or serialized state marker was public")

    def _capture_headers(self, headers: Message, *, context: str) -> dict[str, str]:
        pairs: list[tuple[str, str]] = []
        last_values: dict[str, str] = {}
        for name, value in headers.items():
            lowered_name = name.lower()
            pairs.append((name, value))
            last_values[lowered_name] = value
            if lowered_name == "set-cookie":
                self.set_cookie_headers.append(value)
        for raw_name, raw_value in pairs:
            self._scan_public_bytes(raw_name.encode("utf-8"), context=context)
            self._scan_public_bytes(raw_value.encode("utf-8"), context=context)
        return last_values

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        expected_status: int = 200,
    ) -> SmokeResponse:
        if not path.startswith("/") or path.startswith("//"):
            raise SmokeFailure("Smoke request path must be absolute and same-origin")
        request_headers = dict(headers or {})
        body: bytes | None = None
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            headers=request_headers,
            method=method,
        )
        try:
            with self._opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                status = response.status
                response_headers = self._capture_headers(
                    response.headers,
                    context="public response headers",
                )
                response_body = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            status = error.code
            try:
                response_headers = self._capture_headers(
                    error.headers,
                    context="public error response headers",
                )
                response_body = error.read(MAX_RESPONSE_BYTES + 1)
            finally:
                error.close()
        except (TimeoutError, URLError) as error:
            raise SmokeFailure(f"{method} {path} could not reach the server") from error

        _require(
            len(response_body) <= MAX_RESPONSE_BYTES,
            f"{method} {path} exceeded the smoke response limit",
        )
        self._scan_public_bytes(response_body, context="public response body")
        if status != expected_status:
            raise SmokeFailure(
                f"{method} {path} returned HTTP {status}; expected {expected_status}"
            )
        return SmokeResponse(status=status, headers=response_headers, body=response_body)

    def get(
        self,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        expected_status: int = 200,
    ) -> SmokeResponse:
        return self.request(
            "GET",
            path,
            headers=headers,
            expected_status=expected_status,
        )

    def post(self, path: str, payload: dict[str, Any]) -> SmokeResponse:
        return self.request("POST", path, payload=payload, expected_status=201)

    def open_stream(self, path: str) -> Any:
        """Prove one pending SSE event, then leave that exact response open."""

        if not path.startswith("/") or path.startswith("//"):
            raise SmokeFailure("Smoke request path must be absolute and same-origin")
        request = Request(
            f"{self.base_url}{path}",
            headers={"Accept": "text/event-stream"},
            method="GET",
        )
        try:
            response = self._opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS)
        except (HTTPError, TimeoutError, URLError) as error:
            raise SmokeFailure("Could not open the pending SSE shutdown probe") from error
        try:
            response_headers = self._capture_headers(
                response.headers,
                context="SSE response headers",
            )
        except SmokeFailure:
            response.close()
            raise
        if response.status != 200:
            response.close()
            raise SmokeFailure("Pending SSE shutdown probe did not return HTTP 200")
        if not response_headers.get("content-type", "").startswith("text/event-stream"):
            response.close()
            raise SmokeFailure("Pending shutdown probe was not an SSE response")
        saw_pending_event = False
        for _line_number in range(20):
            line = response.readline(65_537)
            if not line or len(line) > 65_536:
                response.close()
                raise SmokeFailure("Pending SSE shutdown probe did not yield bounded evidence")
            try:
                self._scan_public_bytes(line, context="pending SSE evidence")
            except SmokeFailure:
                response.close()
                raise
            if line.startswith(b"data: "):
                try:
                    event = json.loads(line.removeprefix(b"data: "))
                except json.JSONDecodeError as error:
                    response.close()
                    raise SmokeFailure("Pending SSE shutdown evidence was malformed") from error
                if isinstance(event, dict) and event.get("terminal") is False:
                    saw_pending_event = True
            if saw_pending_event and line in {b"\n", b"\r\n"}:
                break
        if not saw_pending_event or response.closed:
            response.close()
            raise SmokeFailure("SSE shutdown probe was not pending and open at signal time")
        return response


def _parse_sse(body: bytes) -> tuple[list[int], list[dict[str, Any]]]:
    try:
        lines = body.decode("utf-8").replace("\r\n", "\n").splitlines()
    except UnicodeDecodeError as error:
        raise SmokeFailure("SSE response was not UTF-8") from error
    identifiers: list[int] = []
    payloads: list[dict[str, Any]] = []
    try:
        for line in lines:
            if line.startswith("id: "):
                identifiers.append(int(line.removeprefix("id: ")))
            elif line.startswith("data: "):
                value = json.loads(line.removeprefix("data: "))
                if not isinstance(value, dict):
                    raise SmokeFailure("SSE data must be a JSON object")
                payloads.append(value)
    except (ValueError, json.JSONDecodeError) as error:
        raise SmokeFailure("SSE fields were malformed") from error
    return identifiers, payloads


def _decision_payload(
    snapshot: dict[str, Any],
    *,
    decision: Literal["approve", "decline"],
) -> dict[str, str]:
    approval = snapshot.get("pendingApproval")
    if not isinstance(approval, dict):
        raise SmokeFailure("Pending recovery omitted exact consent")
    required = ("remedyId", "remedyDigest", "toolCallId")
    if any(not isinstance(approval.get(field), str) for field in required):
        raise SmokeFailure("Pending recovery consent identifiers were malformed")
    return {
        "decision": decision,
        "clientDecisionId": f"production-smoke-{decision}-{secrets.token_hex(8)}",
        "remedyId": approval["remedyId"],
        "remedyDigest": approval["remedyDigest"],
        "toolCallId": approval["toolCallId"],
    }


def _verify_qa_health(health: dict[str, Any]) -> None:
    expected = {
        "backend": "stub",
        "liveReady": False,
        "sdkStubReady": True,
        "providerBoundary": "demo_adapter_only",
    }
    if health != expected:
        raise SmokeFailure(
            "Full workflow smoke requires the explicit keyless deterministic QA profile"
        )


def _verify_deployed_health(health: dict[str, Any]) -> None:
    _require(
        health.get("providerBoundary") == "demo_adapter_only",
        "Deployed provider boundary drifted",
    )
    _require(
        health.get("sdkStubReady") is False,
        "Deployed defaults must disable the deterministic SDK stub",
    )
    live_ready = health.get("liveReady")
    _require(isinstance(live_ready, bool), "Deployed live readiness was malformed")
    expected_backend = "openai" if live_ready else "stub"
    _require(health.get("backend") == expected_backend, "Deployed backend drifted")


def _validate_session_cookie(
    cookie_headers: list[str],
    *,
    secure_required: bool,
) -> None:
    matching: list[Any] = []
    for header in cookie_headers:
        parsed = SimpleCookie()
        parsed.load(header)
        morsel = parsed.get("backchannel_demo_session")
        if morsel is not None:
            matching.append(morsel)
    _require(
        len(matching) == 1,
        "Demo session cookie must be minted exactly once per smoke session",
    )
    morsel = matching[0]
    _require(bool(morsel["httponly"]), "Demo session cookie lost HttpOnly")
    _require(morsel["samesite"].lower() == "lax", "Demo session SameSite drifted")
    _require(morsel["path"] == "/", "Demo session cookie path drifted")
    try:
        max_age = int(morsel["max-age"])
    except ValueError as error:
        raise SmokeFailure("Demo session cookie Max-Age was malformed") from error
    _require(1 <= max_age <= 604_800, "Demo session cookie lifetime drifted")
    if secure_required:
        _require(bool(morsel["secure"]), "Deployed demo session cookie lost Secure")
    else:
        _require(not morsel["secure"], "QA cookie unexpectedly claimed HTTPS transport")


def _verify_frontend_and_health(
    client: SmokeClient,
    *,
    profile: SmokeProfile,
) -> int:
    index = client.get("/", headers={"Accept": "text/html"})
    _require(b'<div id="root"></div>' in index.body, "Frontend index was missing")
    asset_paths = sorted(
        {
            match.decode("utf-8")
            for match in re.findall(rb'(?:src|href)="(/assets/[^\"]+)"', index.body)
        }
    )
    _require(len(asset_paths) >= 2, "Built hashed frontend assets were missing")
    for asset_path in asset_paths:
        asset = client.get(asset_path)
        _require(bool(asset.body), "A built frontend asset was empty")

    fallback = client.get("/judge-demo", headers={"Accept": "text/html"})
    _require(fallback.body == index.body, "SPA fallback did not return the built index")
    missing_api = client.get("/api/production-smoke-missing", expected_status=404)
    _require(
        missing_api.headers.get("content-type", "").startswith("application/json"),
        "Missing API route was swallowed by the SPA fallback",
    )
    _require(b"<html" not in missing_api.body.lower(), "Missing API returned HTML")

    health_response = client.get("/health")
    _require(
        health_response.headers.get("x-content-type-options") == "nosniff",
        "Security response headers were missing",
    )
    health = health_response.json_object()
    if profile == "deterministic-qa":
        _verify_qa_health(health)
    else:
        _verify_deployed_health(health)
        _require(
            health_response.headers.get("strict-transport-security")
            == "max-age=31536000; includeSubDomains",
            "Deployed HSTS header drifted",
        )
    readiness = client.get("/readyz").json_object()
    _require(readiness == {"status": "ready"}, "Readiness contract failed")
    return len(asset_paths)


def _pending_hotel(client: SmokeClient) -> dict[str, Any]:
    snapshot = client.post(
        "/api/recoveries",
        {"scenarioId": "hotel", "executionMode": "sdk_stub"},
    ).json_object()
    _require(snapshot.get("status") == "pending_approval", "Hotel was not pending")
    _require(snapshot.get("executionMode") == "sdk_stub", "SDK provenance drifted")
    _require(snapshot.get("modelIds") == [], "SDK stub claimed returned model IDs")
    approval = snapshot.get("pendingApproval")
    _require(isinstance(approval, dict), "Hotel omitted exact consent")
    if isinstance(approval, dict):
        _require(approval.get("executionStarted") is False, "Execution began before consent")
        _require(
            isinstance(approval.get("remedyDigest"), str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", approval["remedyDigest"])
            is not None,
            "Consent digest was malformed",
        )
    return snapshot


def _verify_approval(client: SmokeClient) -> None:
    snapshot = _pending_hotel(client)
    recovery_id = str(snapshot["recoveryId"])
    payload = _decision_payload(snapshot, decision="approve")
    decision = client.request(
        "POST",
        f"/api/recoveries/{recovery_id}/decisions",
        payload=payload,
    ).json_object()
    _require(decision.get("status") == "completed", "Approval did not complete")
    _require(decision.get("decision") == "approve", "Approval action drifted")
    _require(decision.get("executionStarted") is True, "Approval did not execute")
    _require(
        decision.get("approvedRemedyDigest") == payload["remedyDigest"],
        "Approval response lost the exact digest",
    )
    receipt = client.get(f"/api/recoveries/{recovery_id}/receipt").json_object()
    expected = {
        "executionMode": "sdk_stub",
        "status": "completed",
        "simulated": True,
        "providerExecution": True,
        "modelIds": [],
        "decision": "approved",
        "decisionRemedyDigest": payload["remedyDigest"],
        "executionCount": 1,
        "providerDispatchStarted": True,
        "exactInterruptionRejected": False,
        "permissionRevoked": True,
        "scopeClosed": True,
        "approvedRemedyDigest": payload["remedyDigest"],
    }
    for field, value in expected.items():
        _require(receipt.get(field) == value, f"Approval receipt field {field} drifted")


def _verify_decline(client: SmokeClient) -> None:
    snapshot = _pending_hotel(client)
    recovery_id = str(snapshot["recoveryId"])
    payload = _decision_payload(snapshot, decision="decline")
    decision = client.request(
        "POST",
        f"/api/recoveries/{recovery_id}/decisions",
        payload=payload,
    ).json_object()
    _require(
        decision.get("status") == "closed_without_action",
        "Decline did not close without action",
    )
    _require(decision.get("decision") == "decline", "Decline action drifted")
    _require(decision.get("executionStarted") is False, "Decline executed a remedy")
    _require(
        decision.get("decisionRemedyDigest") == payload["remedyDigest"],
        "Decline response lost the exact digest",
    )
    receipt = client.get(f"/api/recoveries/{recovery_id}/receipt").json_object()
    expected = {
        "executionMode": "sdk_stub",
        "status": "closed_without_action",
        "simulated": True,
        "providerExecution": False,
        "modelIds": [],
        "decision": "declined",
        "decisionRemedyDigest": payload["remedyDigest"],
        "executionCount": 0,
        "providerDispatchStarted": False,
        "exactInterruptionRejected": True,
        "permissionRevoked": True,
        "scopeClosed": True,
        "approvedRemedyDigest": None,
    }
    for field, value in expected.items():
        _require(receipt.get(field) == value, f"Decline receipt field {field} drifted")


def _verify_sse_reconnect(client: SmokeClient, canary: str) -> None:
    replay = client.post(
        "/api/recoveries",
        {"scenarioId": "api-quota", "executionMode": "replay_fixture"},
    ).json_object()
    recovery_id = str(replay["recoveryId"])

    foreign = SmokeClient(client.base_url, canary)
    foreign.post(
        "/api/recoveries",
        {"scenarioId": "api-quota", "executionMode": "replay_fixture"},
    )
    foreign.get(f"/api/recoveries/{recovery_id}", expected_status=404)

    full = client.get(f"/api/recoveries/{recovery_id}/events")
    _require(
        full.headers.get("content-type", "").startswith("text/event-stream"),
        "Recovery events were not SSE",
    )
    identifiers, payloads = _parse_sse(full.body)
    _require(
        len(identifiers) >= 3 and identifiers == sorted(set(identifiers)),
        "SSE identifiers were not unique and ordered",
    )
    _require(
        bool(payloads) and payloads[-1].get("terminal") is True,
        "SSE lacked terminal evidence",
    )
    cursor = identifiers[1]
    resumed = client.get(
        f"/api/recoveries/{recovery_id}/events",
        headers={"Last-Event-ID": str(cursor)},
    )
    resumed_identifiers, _ = _parse_sse(resumed.body)
    _require(
        resumed_identifiers == [identifier for identifier in identifiers if identifier > cursor],
        "SSE reconnect did not resume strictly after Last-Event-ID",
    )


def _mint_deployed_replay_session(client: SmokeClient) -> str:
    """Mint a Secure session using a recording that executes no model/provider action."""

    replay = client.post(
        "/api/recoveries",
        {"scenarioId": "api-quota", "executionMode": "replay_fixture"},
    ).json_object()
    _require(replay.get("status") == "completed", "Deployed replay did not complete")
    _require(
        replay.get("executionMode") == "replay_fixture",
        "Deployed replay provenance drifted",
    )
    _require(replay.get("modelIds") == [], "Deployed replay claimed model output")
    return "recorded_replay_fixture_created"


def _verify_deployed_route_boundaries(client: SmokeClient) -> None:
    reset = client.request(
        "POST",
        "/api/demo/reset",
        payload={},
        expected_status=403,
    ).json_object()
    _require(
        isinstance(reset.get("error"), dict)
        and reset["error"].get("code") == "invalid_request",
        "Deployed reset boundary drifted",
    )
    sdk = client.request(
        "POST",
        "/api/recoveries",
        payload={"scenarioId": "hotel", "executionMode": "sdk_stub"},
        expected_status=422,
    ).json_object()
    _require(
        isinstance(sdk.get("error"), dict)
        and sdk["error"].get("code") == "invalid_request",
        "Deployed SDK-stub boundary drifted",
    )


def run_http_smoke(
    base_url: str,
    canary: str,
    *,
    profile: SmokeProfile,
) -> dict[str, object]:
    if len(canary.encode("utf-8")) < 32:
        raise SmokeFailure("BACKCHANNEL_SMOKE_CANARY must contain at least 32 bytes")
    if profile == "deterministic-qa":
        _require_loopback_qa_target(base_url)
    client = SmokeClient(base_url, canary)
    asset_count = _verify_frontend_and_health(client, profile=profile)
    if profile == "deployed-readonly":
        _verify_deployed_route_boundaries(client)
        replay_evidence = _mint_deployed_replay_session(client)
        _validate_session_cookie(client.set_cookie_headers, secure_required=True)
        return {
            "frontendAssetCount": asset_count,
            "health": "passed",
            "readiness": "passed",
            "profile": profile,
            "replaySession": replay_evidence,
            "reset": "disabled",
            "sdkStub": "disabled",
            "workflowExecution": "not_attempted_over_http",
        }

    _verify_sse_reconnect(client, canary)
    _verify_approval(client)
    _verify_decline(client)
    _validate_session_cookie(client.set_cookie_headers, secure_required=False)
    return {
        "approval": "completed_once",
        "decline": "closed_without_action",
        "frontendAssetCount": asset_count,
        "health": "passed",
        "profile": profile,
        "readiness": "passed",
        "responseCanarySecretAbsent": True,
        "sessionIsolation": "passed",
        "sseReconnect": "passed",
        "transportBoundary": "loopback_or_isolated_container_namespace_only",
    }


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_server(process: subprocess.Popen[bytes], base_url: str) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SmokeFailure("Production process exited before readiness")
        try:
            with urlopen(f"{base_url}/readyz", timeout=1) as response:
                if response.status == 200:
                    return
        except (HTTPError, URLError, TimeoutError):
            time.sleep(0.1)
    raise SmokeFailure("Production process did not become ready within 60 seconds")


def _stop_process(process: subprocess.Popen[bytes]) -> ShutdownResult:
    if process.poll() is not None:
        return ShutdownResult(
            clean=False,
            evidence=f"already_exited_{process.returncode}",
        )
    process.terminate()
    try:
        return_code = process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
        return ShutdownResult(
            clean=return_code == 0,
            evidence=f"sigterm_exit_{return_code}",
        )
    except subprocess.TimeoutExpired:
        process.kill()
        return_code = process.wait(timeout=5)
        return ShutdownResult(
            clean=False,
            evidence=f"forced_kill_exit_{return_code}",
        )


def _validated_shutdown(
    result: ShutdownResult,
    stderr: bytes,
) -> ShutdownResult:
    """Accept Uvicorn's SIGTERM replay only after its fixed completion markers."""

    if result.evidence != f"sigterm_exit_{-signal.SIGTERM}":
        return result
    application_complete = stderr.find(b"Application shutdown complete.")
    server_finished = stderr.find(b"Finished server process")
    if 0 <= application_complete < server_finished:
        return ShutdownResult(
            clean=True,
            evidence="sigterm_reraised_after_server_shutdown",
        )
    return result


def _open_sse_shutdown_evidence(
    result: ShutdownResult,
    stderr: bytes,
) -> str:
    """Require Uvicorn's timeout-cancel marker before completed shutdown."""

    timeout_cancel = stderr.find(b"timeout graceful shutdown exceeded")
    application_complete = stderr.find(b"Application shutdown complete.")
    server_finished = stderr.find(b"Finished server process")
    if not (
        result.clean
        and 0 <= timeout_cancel < application_complete < server_finished
    ):
        raise SmokeFailure(
            "Pending SSE timeout cancellation marker was absent or out of order"
        )
    return "server_cancelled_within_grace_timeout"


def _launch_environment(
    *,
    repository_root: Path,
    temporary_path: Path,
    port: int,
    canary: str,
    profile: SmokeProfile,
) -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop("OPENAI_API_KEY", None)
    environment.update(
        {
            "BACKCHANNEL_CORS_ORIGINS": (
                "https://backchannel-smoke.invalid"
                if profile == "deployed-readonly"
                else ""
            ),
            "BACKCHANNEL_DB_PATH": str(temporary_path / "production.sqlite3"),
            "BACKCHANNEL_DEMO_RESET_ENABLED": "false",
            "BACKCHANNEL_DEPLOYED": (
                "true" if profile == "deployed-readonly" else "false"
            ),
            "BACKCHANNEL_FRONTEND_DIST_PATH": str(repository_root / "web" / "dist"),
            "BACKCHANNEL_IDENTITY_HMAC_SECRET": canary,
            "BACKCHANNEL_SMOKE_CANARY": canary,
            "BACKCHANNEL_TRUSTED_PROXY_CIDRS": "",
            "PORT": str(port),
        }
    )
    return environment


def run_launched_smoke(profile: SmokeProfile) -> dict[str, object]:
    repository_root = Path(__file__).resolve().parent.parent
    if not (repository_root / "web" / "dist" / "index.html").is_file():
        raise SmokeFailure("Built frontend is absent; run npm run build first")
    port = _available_port()
    base_url = f"http://127.0.0.1:{port}"
    canary = os.environ.get(
        "BACKCHANNEL_SMOKE_CANARY",
        f"backchannel-smoke-{secrets.token_urlsafe(32)}",
    )
    with TemporaryDirectory(prefix="backchannel-production-smoke-") as temporary:
        temporary_path = Path(temporary)
        environment = _launch_environment(
            repository_root=repository_root,
            temporary_path=temporary_path,
            port=port,
            canary=canary,
            profile=profile,
        )
        stdout_path = temporary_path / "server.stdout.log"
        stderr_path = temporary_path / "server.stderr.log"
        shutdown_result = ShutdownResult(clean=False, evidence="not_started")
        held_sse: Any | None = None
        with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
            process = subprocess.Popen(
                [sys.executable, "scripts/start.py"],
                cwd=repository_root,
                env=environment,
                stdout=stdout_file,
                stderr=stderr_file,
            )
            try:
                _wait_for_server(process, base_url)
                result = run_http_smoke(base_url, canary, profile=profile)
                if profile == "deterministic-qa":
                    shutdown_client = SmokeClient(base_url, canary)
                    pending = _pending_hotel(shutdown_client)
                    held_sse = shutdown_client.open_stream(
                        f"/api/recoveries/{pending['recoveryId']}/events"
                    )
            finally:
                shutdown_result = _stop_process(process)
                if held_sse is not None:
                    held_sse.close()

        encoded_canary = canary.encode("utf-8")
        stdout = stdout_path.read_bytes()
        stderr = stderr_path.read_bytes()
        _require(
            encoded_canary not in stdout and encoded_canary not in stderr,
            "The canary secret appeared in production process logs",
        )
        shutdown_result = _validated_shutdown(shutdown_result, stderr)
        _require(
            shutdown_result.clean,
            "Production process did not exit cleanly after bounded SIGTERM: "
            + shutdown_result.evidence,
        )
        result["canaryCoverage"] = "responses_assets_process_logs"
        result["canarySecretAbsent"] = True
        result["proofLane"] = "local_single_process"
        result["shutdown"] = shutdown_result.evidence
        if profile == "deterministic-qa":
            result["openSseSigterm"] = _open_sse_shutdown_evidence(
                shutdown_result,
                stderr,
            )
        return result


def _parse_arguments(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base-url",
        default=os.environ.get("BACKCHANNEL_SMOKE_BASE_URL", "http://127.0.0.1:8000"),
        help="origin of an already-running packaged process",
    )
    parser.add_argument(
        "--launch",
        action="store_true",
        help="launch the one-process entry point locally before running the smoke",
    )
    parser.add_argument(
        "--profile",
        choices=("deterministic-qa", "deployed-readonly"),
        default="deterministic-qa",
        help="full isolated QA flow or hardened deployed-default read-only checks",
    )
    return parser.parse_args(arguments)


def main(arguments: list[str] | None = None) -> None:
    parsed = _parse_arguments(arguments)
    profile: SmokeProfile = parsed.profile
    if parsed.launch:
        result = run_launched_smoke(profile)
    else:
        canary = os.environ.get("BACKCHANNEL_SMOKE_CANARY", "")
        result = run_http_smoke(parsed.base_url, canary, profile=profile)
        result["canaryCoverage"] = "responses_and_assets"
        result["canarySecretAbsent"] = result.get("responseCanarySecretAbsent", True)
        result["proofLane"] = "configured_http_target"
    result["smoke"] = "passed"
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except SmokeFailure as error:
        print(
            json.dumps({"error": str(error), "smoke": "failed"}, sort_keys=True),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
