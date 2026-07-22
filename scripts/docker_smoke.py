"""HTTP smoke proof for a packaged Backchannel process.

Run without arguments against an already-running container, or pass ``--launch``
to build-equivalent smoke the local one-process production entry point.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from http.client import HTTPException
from http.cookiejar import CookieJar
from http.cookies import SimpleCookie
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import (
    HTTPCookieProcessor,
    Request,
    build_opener,
    urlopen,
)

HTTP_TIMEOUT_SECONDS = 10
INITIAL_SSE_FRAME_MAX_BYTES = 65_536

HTML_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'none'"
)
API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
SDK_VERSION = "0.18.3"
SDK_PROTOCOL_VERSION = "backchannel.approval.v1"
SDK_AGENT_GRAPH_VERSION = "backchannel.hotel-agent.v1"
SDK_DEFINITION_DIGEST = (
    "6dfe3cc5b949d1ec809f17b5a22e2bc6e463d4dfe1098fcee660fcb39d6ff2dd"
)
SDK_BOUNDARY = (
    "Deterministic Agents SDK model and demo hotel adapter only; "
    "no OpenAI model call, real booking, or payment change."
)
APPROVAL_PROVIDER_RESULT = (
    "Demo hotel adapter confirmed the replacement room; "
    "no real booking or payment was changed."
)
APPROVAL_AUTHORIZATION_SOURCE = "Approved Agents SDK commit_remedy interruption."
DECLINE_AUTHORIZATION_SOURCE = (
    "User declined the exact Agents SDK commit_remedy interruption."
)
APPROVAL_PROOFS = [
    "Immediate pre-execution remedy digest matched the approved digest.",
    "Demo provider dispatch returned confirmed.",
    "Provider result stored under one idempotency key.",
    "Temporary provider-dispatch permission revoked after the approved execution.",
]
DECLINE_PROOFS = [
    "Human consent requested.",
    "Remedy declined by operator.",
    "Exact interruption rejected.",
    "No replacement action selected.",
    "Execution count is zero.",
    "Provider dispatch did not begin.",
    "Temporary permission revoked.",
    "Cancellation receipt sealed.",
]


class SmokeFailure(RuntimeError):
    """Safe diagnostic that never contains a response body or secret value."""


@dataclass(frozen=True, slots=True)
class SmokeResponse:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> dict[str, Any]:
        parsed = json.loads(self.body)
        if not isinstance(parsed, dict):
            raise SmokeFailure("Expected a JSON object response")
        return parsed


class HeldEventStream:
    """Own one dedicated streaming response and close it at most once."""

    def __init__(self, response: Any, opener: Any) -> None:
        self._response = response
        self._opener = opener
        self._closed = False

    @property
    def is_open(self) -> bool:
        return not self._closed

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._response.close()


class SmokeClient:
    def __init__(self, base_url: str, canary: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._canary = canary.encode("utf-8")
        self._opener = build_opener(HTTPCookieProcessor(CookieJar()))
        parsed_base_url = urlparse(self._base_url)
        self._loopback_http = (
            parsed_base_url.scheme == "http"
            and parsed_base_url.hostname in {"127.0.0.1", "localhost", "::1"}
        )
        self._loopback_session_cookie: str | None = None
        self.secure_cookie_validated = False

    def _capture_session_cookie(self, header: str) -> None:
        parsed = SimpleCookie()
        try:
            parsed.load(header)
        except Exception as error:
            raise SmokeFailure("The demo session cookie header was malformed") from error
        morsel = parsed.get("backchannel_demo_session")
        if morsel is None:
            return
        try:
            max_age = int(morsel["max-age"])
        except ValueError as error:
            raise SmokeFailure("The demo session cookie Max-Age was invalid") from error
        if (
            not morsel["secure"]
            or not morsel["httponly"]
            or morsel["samesite"].lower() != "lax"
            or morsel["path"] != "/"
            or not 1 <= max_age <= 604_800
        ):
            raise SmokeFailure("The demo session cookie security attributes drifted")
        self.secure_cookie_validated = True
        if self._loopback_http:
            # This smoke-only bridge models TLS termination for the loopback HTTP
            # hop. The application still emits and requires a Secure cookie.
            self._loopback_session_cookie = morsel.value

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        expected_status: int = 200,
    ) -> SmokeResponse:
        request_headers = dict(headers or {})
        if self._loopback_http and self._loopback_session_cookie is not None:
            request_headers["Cookie"] = (
                "backchannel_demo_session=" + self._loopback_session_cookie
            )
        body = None
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = Request(
            f"{self._base_url}{path}",
            data=body,
            headers=request_headers,
            method=method,
        )
        try:
            with self._opener.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                status_code = response.status
                response_headers = {
                    name.lower(): value for name, value in response.headers.items()
                }
                response_body = response.read()
        except HTTPError as error:
            status_code = error.code
            response_headers = {
                name.lower(): value for name, value in error.headers.items()
            }
            response_body = error.read()
        except URLError as error:
            raise SmokeFailure(f"{method} {path} could not reach the server") from error

        serialized_headers = json.dumps(response_headers, sort_keys=True).encode("utf-8")
        set_cookie = response_headers.get("set-cookie")
        if set_cookie is not None:
            self._capture_session_cookie(set_cookie)
        if self._canary and (
            self._canary in response_body or self._canary in serialized_headers
        ):
            raise SmokeFailure("A canary secret appeared in a public response")
        if status_code != expected_status:
            raise SmokeFailure(
                f"{method} {path} returned HTTP {status_code}; expected {expected_status}"
            )
        return SmokeResponse(status_code, response_headers, response_body)

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

    def open_event_stream(
        self,
        path: str,
        *,
        expected_recovery_id: str,
    ) -> HeldEventStream:
        """Open and validate one bounded frame while retaining the live response."""

        if not self._loopback_http or self._loopback_session_cookie is None:
            raise SmokeFailure("The owner session cookie was unavailable for streaming")
        opener = build_opener(HTTPCookieProcessor(CookieJar()))
        request = Request(
            f"{self._base_url}{path}",
            headers={
                "Accept": "text/event-stream",
                "Cookie": ("backchannel_demo_session=" + self._loopback_session_cookie),
            },
            method="GET",
        )
        try:
            response = opener.open(request, timeout=HTTP_TIMEOUT_SECONDS)
        except HTTPError as error:
            error.close()
            raise SmokeFailure("The retained event stream returned an error") from None
        except (HTTPException, OSError, URLError):
            raise SmokeFailure("The retained event stream could not be opened") from None

        try:
            status_code = response.status
            response_headers = {name.lower(): value for name, value in response.headers.items()}
            if status_code != 200:
                raise SmokeFailure("The retained event stream returned an invalid status")
            content_type = response_headers.get("content-type", "")
            if content_type.partition(";")[0].strip().lower() != "text/event-stream":
                raise SmokeFailure("The retained event stream was not SSE")

            frame_parts: list[bytes] = []
            frame_size = 0
            while True:
                remaining = INITIAL_SSE_FRAME_MAX_BYTES - frame_size
                if remaining <= 0:
                    raise SmokeFailure("The retained event stream frame was too large")
                line = response.readline(remaining + 1)
                if not line:
                    raise SmokeFailure("The retained event stream ended before one frame")
                if len(line) > remaining:
                    raise SmokeFailure("The retained event stream frame was too large")
                frame_parts.append(line)
                frame_size += len(line)
                if line in {b"\n", b"\r\n"}:
                    break

            frame = b"".join(frame_parts)
            serialized_headers = json.dumps(
                response_headers,
                sort_keys=True,
            ).encode("utf-8")
            if self._canary and (self._canary in frame or self._canary in serialized_headers):
                raise SmokeFailure("A canary secret appeared in a public response")
            identifiers, payloads = _sse_ids_and_payloads(frame)
            if len(identifiers) != 1 or len(payloads) != 1:
                raise SmokeFailure("The retained event stream initial frame drifted")
            payload = payloads[0]
            if (
                payload.get("recoveryId") != expected_recovery_id
                or payload.get("terminal") is not False
            ):
                raise SmokeFailure("The retained event stream initial frame drifted")
        except (
            HTTPException,
            OSError,
            UnicodeDecodeError,
            ValueError,
            json.JSONDecodeError,
        ):
            response.close()
            raise SmokeFailure("The retained event stream initial frame drifted") from None
        except Exception:
            response.close()
            raise

        return HeldEventStream(response, opener)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def _sse_ids_and_payloads(body: bytes) -> tuple[list[int], list[dict[str, Any]]]:
    identifiers: list[int] = []
    payloads: list[dict[str, Any]] = []
    for line in body.decode("utf-8").splitlines():
        if line.startswith("id: "):
            identifiers.append(int(line.removeprefix("id: ")))
        elif line.startswith("data: "):
            payload = json.loads(line.removeprefix("data: "))
            if not isinstance(payload, dict):
                raise SmokeFailure("SSE data was not a JSON object")
            payloads.append(payload)
    return identifiers, payloads


def _pending_hotel(client: SmokeClient) -> dict[str, Any]:
    created = client.post(
        "/api/recoveries",
        {"scenarioId": "hotel", "executionMode": "sdk_stub"},
    ).json()
    approval = created.get("pendingApproval")
    _require(created.get("status") == "pending_approval", "SDK hotel was not pending")
    _require(created.get("executionMode") == "sdk_stub", "SDK provenance was incorrect")
    _require(created.get("modelIds") == [], "SDK stub unexpectedly claimed a model")
    if not isinstance(approval, dict):
        raise SmokeFailure("SDK hotel omitted exact consent")
    expected_terms = {
        "bookingId": "booking-demo-001",
        "action": "replace_room",
        "replacement": {"fromRoomType": "double", "toRoomType": "king"},
        "stay": {"checkIn": "2026-08-14", "checkOut": "2026-08-16"},
        "currency": "USD",
    }
    _require(approval.get("terms") == expected_terms, "Consent terms were not exact")
    _require(approval.get("costDeltaMinor") == 0, "Consent cost delta was not exact")
    _require(approval.get("changedFields") == ["room_type"], "Changed fields drifted")
    _require(
        approval.get("providerCommitments")
        == ["No additional charge", "Preserve booking dates"],
        "Provider commitments drifted",
    )
    _require(approval.get("hardConstraintSatisfied") is True, "Hard constraint failed")
    _require(
        approval.get("delegatedAuthoritySatisfied") is True,
        "Delegated authority failed",
    )
    _require(approval.get("executionStarted") is False, "Execution began before consent")
    _require(
        isinstance(approval.get("remedyDigest"), str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", approval["remedyDigest"]) is not None,
        "Consent digest was invalid",
    )
    return created


def _decision_payload(
    snapshot: dict[str, Any],
    *,
    action: str,
) -> dict[str, str]:
    approval = snapshot["pendingApproval"]
    if not isinstance(approval, dict):
        raise SmokeFailure("Pending approval disappeared before decision")
    return {
        "action": action,
        "clientDecisionId": f"production-smoke-{action}-{secrets.token_hex(8)}",
        "remedyId": str(approval["remedyId"]),
        "remedyDigest": str(approval["remedyDigest"]),
        "toolCallId": str(approval["toolCallId"]),
    }


def _verify_approval(client: SmokeClient) -> None:
    snapshot = _pending_hotel(client)
    recovery_id = str(snapshot["recoveryId"])
    payload = _decision_payload(snapshot, action="approve")
    decision = client.request(
        "POST",
        f"/api/recoveries/{recovery_id}/decisions",
        payload=payload,
    ).json()
    _require(
        decision
        == {
            "action": "approve",
            "clientDecisionId": payload["clientDecisionId"],
            "recoveryId": recovery_id,
            "status": "completed",
            "approvedRemedyDigest": payload["remedyDigest"],
            "executionStarted": True,
        },
        "Approval decision response drifted",
    )
    receipt = client.get(f"/api/recoveries/{recovery_id}/receipt").json()
    _require(
        receipt
        == {
            "recoveryId": recovery_id,
            "executionMode": "sdk_stub",
            "status": "completed",
            "simulated": True,
            "providerExecution": True,
            "modelCall": False,
            "modelIds": [],
            "rootTraceId": snapshot["rootTraceId"],
            "sdkVersion": SDK_VERSION,
            "protocolVersion": SDK_PROTOCOL_VERSION,
            "agentGraphVersion": SDK_AGENT_GRAPH_VERSION,
            "definitionDigest": SDK_DEFINITION_DIGEST,
            "boundary": SDK_BOUNDARY,
            "providerResult": APPROVAL_PROVIDER_RESULT,
            "authorizationSource": APPROVAL_AUTHORIZATION_SOURCE,
            "verificationResults": APPROVAL_PROOFS,
            "approvalCount": 1,
            "approvedRemedyDigest": payload["remedyDigest"],
        },
        "Approval receipt drifted from the exact production contract",
    )


def _verify_decline(client: SmokeClient) -> None:
    snapshot = _pending_hotel(client)
    recovery_id = str(snapshot["recoveryId"])
    payload = _decision_payload(snapshot, action="decline")
    decision = client.request(
        "POST",
        f"/api/recoveries/{recovery_id}/decisions",
        payload=payload,
    ).json()
    _require(
        decision
        == {
            "action": "decline",
            "clientDecisionId": payload["clientDecisionId"],
            "recoveryId": recovery_id,
            "status": "closed_without_action",
            "approvedRemedyDigest": None,
            "executionStarted": False,
        },
        "Decline decision response drifted",
    )
    receipt = client.get(f"/api/recoveries/{recovery_id}/receipt").json()
    _require(
        receipt
        == {
            "recoveryId": recovery_id,
            "executionMode": "sdk_stub",
            "status": "closed_without_action",
            "simulated": True,
            "providerExecution": False,
            "modelCall": False,
            "modelIds": [],
            "rootTraceId": snapshot["rootTraceId"],
            "sdkVersion": SDK_VERSION,
            "protocolVersion": SDK_PROTOCOL_VERSION,
            "agentGraphVersion": SDK_AGENT_GRAPH_VERSION,
            "definitionDigest": SDK_DEFINITION_DIGEST,
            "boundary": SDK_BOUNDARY,
            "providerResult": "Provider dispatch did not begin.",
            "authorizationSource": DECLINE_AUTHORIZATION_SOURCE,
            "verificationResults": DECLINE_PROOFS,
            "approvalCount": 0,
            "approvedRemedyDigest": None,
        },
        "Decline receipt drifted from the exact production contract",
    )


def run_http_smoke(base_url: str, canary: str) -> dict[str, object]:
    if not canary:
        raise SmokeFailure("A non-empty BACKCHANNEL_SMOKE_CANARY is required")
    client = SmokeClient(base_url, canary)

    index = client.get("/")
    _require(index.headers.get("content-security-policy") == HTML_CSP, "HTML CSP drifted")
    _require(b"<title>Backchannel</title>" in index.body, "Frontend index was missing")
    asset_paths = sorted(
        {
            match.decode("utf-8")
            for match in re.findall(rb'(?:src|href)="(/assets/[^\"]+)"', index.body)
        }
    )
    _require(len(asset_paths) >= 2, "Built frontend assets were missing")
    for asset_path in asset_paths:
        asset = client.get(asset_path)
        _require(asset.body != b"", "A built frontend asset was empty")
        _require(
            asset.headers.get("content-security-policy") == API_CSP,
            "Non-HTML asset CSP drifted",
        )

    health = client.get("/health")
    _require(health.headers.get("content-security-policy") == API_CSP, "API CSP drifted")
    health_body = health.json()
    _require(health_body.get("backend") in {"openai", "stub"}, "Health backend drifted")
    _require(
        health_body.get("providerBoundary") == "demo_adapter_only",
        "Health provider boundary drifted",
    )
    _require("key" not in json.dumps(health_body).lower(), "Health exposed key metadata")
    readiness = client.get("/readyz").json()
    _require(readiness == {"status": "ready"}, "Production readiness failed")

    replay = client.post(
        "/api/recoveries",
        {"scenarioId": "api-quota", "executionMode": "replay_fixture"},
    ).json()
    replay_id = str(replay["recoveryId"])
    foreign_client = SmokeClient(base_url, canary)
    foreign_client.get("/health")
    foreign_snapshot = foreign_client.get(
        f"/api/recoveries/{replay_id}",
        expected_status=404,
    )
    _require(
        foreign_snapshot.body == b'{"detail":"Not found"}',
        "A foreign smoke session could inspect a recovery",
    )
    full_stream = client.get(f"/api/recoveries/{replay_id}/events")
    _require(
        full_stream.headers.get("content-type", "").startswith("text/event-stream"),
        "Recovery stream was not SSE",
    )
    full_ids, full_payloads = _sse_ids_and_payloads(full_stream.body)
    _require(len(full_ids) >= 3 and full_ids == sorted(full_ids), "SSE order was invalid")
    _require(
        bool(full_payloads) and full_payloads[-1].get("terminal") is True,
        "SSE terminal evidence was missing",
    )
    resume_cursor = full_ids[1]
    resumed = client.get(
        f"/api/recoveries/{replay_id}/events",
        headers={"Last-Event-ID": str(resume_cursor)},
    )
    resumed_ids, _ = _sse_ids_and_payloads(resumed.body)
    _require(
        resumed_ids == [identifier for identifier in full_ids if identifier > resume_cursor],
        "SSE resume did not return exactly the newer events",
    )

    _verify_approval(client)
    _verify_decline(client)
    _require(client.secure_cookie_validated, "Secure demo session cookie was not observed")
    _require(
        foreign_client.secure_cookie_validated,
        "Foreign smoke session cookie was not security-hardened",
    )

    return {
        "approval": "completed_once",
        "decline": "closed_without_action",
        "frontendAssetCount": len(asset_paths),
        "health": "passed",
        "readiness": "passed",
        "responseCanarySecretAbsent": True,
        "sessionIsolation": "passed",
        "secureCookie": "validated",
        "sseResume": "passed",
    }


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_server(process: subprocess.Popen[bytes], base_url: str) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SmokeFailure("Production server exited before becoming healthy")
        try:
            with urlopen(f"{base_url}/health", timeout=1) as response:
                if response.status == 200:
                    return
        except (HTTPError, URLError, TimeoutError):
            pass
        time.sleep(0.1)
    raise SmokeFailure("Production server did not become healthy within 60 seconds")


def run_local_single_process_smoke() -> dict[str, object]:
    repository_root = Path(__file__).resolve().parent.parent
    port = _available_port()
    base_url = f"http://127.0.0.1:{port}"
    canary = f"sk-smoke-{secrets.token_urlsafe(24)}"
    with TemporaryDirectory(prefix="backchannel-production-smoke-") as temporary:
        temporary_path = Path(temporary)
        environment = dict(os.environ)
        environment.update(
            {
                "BACKCHANNEL_CORS_ORIGINS": "https://backchannel.example",
                "BACKCHANNEL_DB_PATH": str(temporary_path / "production.sqlite3"),
                "BACKCHANNEL_DEPLOYED_MODE": "true",
                "BACKCHANNEL_IDENTITY_HASH_SECRET": (
                    "production-smoke-identity-secret-0123456789"
                ),
                "BACKCHANNEL_SMOKE_CANARY": canary,
                "OPENAI_API_KEY": canary,
                "PORT": str(port),
            }
        )
        stdout_path = temporary_path / "server.stdout.log"
        stderr_path = temporary_path / "server.stderr.log"
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
                result = run_http_smoke(base_url, canary)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)

        encoded_canary = canary.encode("utf-8")
        if any(
            encoded_canary in log_path.read_bytes()
            for log_path in (stdout_path, stderr_path)
        ):
            raise SmokeFailure("A canary secret appeared in production server logs")
        result["canaryCoverage"] = "responses_assets_server_logs"
        result["canarySecretAbsent"] = True
        result["proofLane"] = "local_single_process_production"
        return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--launch",
        action="store_true",
        help="launch the local one-process production entry point before smoking it",
    )
    arguments = parser.parse_args()
    if arguments.launch:
        result = run_local_single_process_smoke()
    else:
        base_url = os.environ.get(
            "BACKCHANNEL_SMOKE_BASE_URL",
            "http://127.0.0.1:8000",
        )
        canary = os.environ.get("BACKCHANNEL_SMOKE_CANARY", "")
        result = run_http_smoke(base_url, canary)
        result["canaryCoverage"] = "responses_assets"
        result["canarySecretAbsent"] = result["responseCanarySecretAbsent"]
        result["proofLane"] = "configured_http_target"
    result["smoke"] = "passed"
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except SmokeFailure as error:
        print(
            json.dumps(
                {"smoke": "failed", "error": str(error)},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
