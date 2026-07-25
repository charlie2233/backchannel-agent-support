"""Packaged replacement-container persistence smoke.

The image must already exist. This proof creates one disposable named volume,
starts two independently named containers in sequence, and carries only the
signed demo-session cookie in the in-memory HTTP client across replacement.
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
from typing import Any, Literal
from urllib.request import urlopen

if __package__:
    from scripts.docker_smoke import (
        SmokeClient,
        SmokeFailure,
        _decision_payload,
        _pending_hotel,
        _sse_ids_and_payloads,
    )
else:
    from docker_smoke import (  # type: ignore[no-redef]
        SmokeClient,
        SmokeFailure,
        _decision_payload,
        _pending_hotel,
        _sse_ids_and_payloads,
    )

DOCKER_TIMEOUT_SECONDS = 60
HTTP_TIMEOUT_SECONDS = 10
READINESS_TIMEOUT_SECONDS = 60
READINESS_POLL_SECONDS = 0.2
CONTAINER_STOP_TIMEOUT_SECONDS = 5
_CONTAINER_PORT = 8000
_DATABASE_PATH = "/data/backchannel.sqlite3"
_GENERIC_NOT_FOUND = b'{"detail":"Not found"}'
_STREAM_CAPACITY_MESSAGE = "Event streaming is temporarily at capacity; retry is automatic."
_READINESS_TIMEOUT_MESSAGES = {
    "first_container": "First container readiness timed out",
    "replacement_container": "Replacement container readiness timed out",
}
_PHASE_FAILURE_MESSAGES = {
    "initialization": "Packaged restart smoke failed during initialization",
    "volume_setup": "Packaged restart smoke failed during volume setup",
    "first_container_start": "Packaged restart smoke failed during first container start",
    "first_container_readiness": (
        "Packaged restart smoke failed during first container readiness"
    ),
    "first_container_api_setup": (
        "Packaged restart smoke failed during first container API setup"
    ),
    "first_container_shutdown": (
        "Packaged restart smoke failed during first container shutdown"
    ),
    "replacement_container_start": (
        "Packaged restart smoke failed during replacement container start"
    ),
    "replacement_container_readiness": (
        "Packaged restart smoke failed during replacement container readiness"
    ),
    "replacement_api_verification": (
        "Packaged restart smoke failed during replacement API verification"
    ),
}
ReadinessPhase = Literal["first_container", "replacement_container"]


class ContainerRestartFailure(RuntimeError):
    """Coarse failure safe to expose in CI output."""


@dataclass(frozen=True, slots=True)
class RestartFixture:
    approval_snapshot: dict[str, Any]
    approval_payload: dict[str, str]
    decline_snapshot: dict[str, Any]
    decline_payload: dict[str, str]
    replay_snapshot: dict[str, Any]
    replay_receipt: dict[str, Any]
    replay_stream: bytes


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContainerRestartFailure(message)


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _docker(
    arguments: list[str],
    *,
    environment: dict[str, str] | None = None,
    allow_failure: bool = False,
) -> str | None:
    try:
        completed = subprocess.run(
            ["docker", *arguments],
            check=False,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=DOCKER_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        if allow_failure:
            return None
        raise ContainerRestartFailure("A bounded Docker operation failed") from None
    if completed.returncode != 0:
        if allow_failure:
            return None
        raise ContainerRestartFailure("A bounded Docker operation failed") from None
    try:
        output = completed.stdout.decode("ascii").strip()
    except UnicodeDecodeError:
        if allow_failure:
            return None
        raise ContainerRestartFailure("Docker returned an invalid opaque identifier") from None
    if len(output) > 128:
        if allow_failure:
            return None
        raise ContainerRestartFailure("Docker returned an invalid opaque identifier")
    return output


def _wait_for_ready(base_url: str, *, phase: ReadinessPhase) -> None:
    deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        request_timeout = min(
            HTTP_TIMEOUT_SECONDS,
            max(0.1, deadline - time.monotonic()),
        )
        try:
            with urlopen(f"{base_url}/readyz", timeout=request_timeout) as response:
                body = response.read(256)
                if response.status == 200 and body == b'{"status":"ready"}':
                    return
        except (OSError, HTTPException):
            pass
        time.sleep(READINESS_POLL_SECONDS)
    raise ContainerRestartFailure(_READINESS_TIMEOUT_MESSAGES[phase])


def _validate_terminal_stream(stream: bytes, *, message: str) -> None:
    try:
        identifiers, payloads = _sse_ids_and_payloads(stream)
    except (SmokeFailure, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise ContainerRestartFailure(message) from None
    _require(bool(identifiers), message)
    _require(identifiers == sorted(set(identifiers)), message)
    _require(bool(payloads) and payloads[-1].get("terminal") is True, message)


def _validate_stream_capacity_response(response: Any, *, recovery_id: str) -> None:
    message = "Active event stream capacity proof drifted"
    content_type = response.headers.get("content-type", "")
    _require(
        content_type.partition(";")[0].strip().lower() == "text/event-stream",
        message,
    )
    try:
        body = response.body.decode("utf-8")
        prefix = "retry: 5000\nevent: stream.capacity\ndata: "
        _require(body.startswith(prefix) and body.endswith("\n\n"), message)
        payload = json.loads(body[len(prefix) : -2])
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ContainerRestartFailure(message) from None
    _require(isinstance(payload, dict), message)
    request_id = payload.get("requestId")
    _require(
        isinstance(request_id, str) and re.fullmatch(r"[0-9a-f]{32}", request_id) is not None,
        message,
    )
    expected_payload = {
        "code": "event_stream_capacity",
        "message": _STREAM_CAPACITY_MESSAGE,
        "requestId": request_id,
    }
    _require(payload == expected_payload, message)
    expected_body = prefix + json.dumps(expected_payload, separators=(",", ":")) + "\n\n"
    _require(body == expected_body, message)
    _require(recovery_id not in body, message)


def _recovery_id(snapshot: dict[str, Any], *, status: str) -> str:
    recovery_id = snapshot.get("recoveryId")
    _require(isinstance(recovery_id, str) and bool(recovery_id), "Recovery identity drifted")
    _require(snapshot.get("status") == status, "Recovery status drifted")
    return recovery_id


def _create_restart_fixture(client: SmokeClient) -> RestartFixture:
    approval_snapshot = _pending_hotel(client)
    decline_snapshot = _pending_hotel(client)
    approval_payload = _decision_payload(approval_snapshot, action="approve")
    decline_payload = _decision_payload(decline_snapshot, action="decline")

    replay_snapshot = client.post(
        "/api/recoveries",
        {
            "scenarioId": "api-quota",
            "executionMode": "replay_fixture",
            "clientRequestId": f"restart-smoke-{secrets.token_hex(16)}",
        },
    ).json()
    replay_id = _recovery_id(replay_snapshot, status="completed")
    _require(
        replay_snapshot.get("executionMode") == "replay_fixture",
        "Replay provenance drifted",
    )
    replay_receipt = client.get(f"/api/recoveries/{replay_id}/receipt").json()
    replay_stream_response = client.get(f"/api/recoveries/{replay_id}/events")
    _require(
        replay_stream_response.headers.get("content-type", "").startswith(
            "text/event-stream"
        ),
        "Replay stream was not SSE",
    )
    replay_stream = replay_stream_response.body
    _validate_terminal_stream(replay_stream, message="Replay terminal stream drifted")
    _require(
        getattr(client, "secure_cookie_validated", False) is True,
        "The owner session cookie was not security-hardened",
    )
    return RestartFixture(
        approval_snapshot=approval_snapshot,
        approval_payload=approval_payload,
        decline_snapshot=decline_snapshot,
        decline_payload=decline_payload,
        replay_snapshot=replay_snapshot,
        replay_receipt=replay_receipt,
        replay_stream=replay_stream,
    )


def _validate_terminal_receipts(
    approval: dict[str, Any],
    decline: dict[str, Any],
    *,
    approval_id: str,
    decline_id: str,
    approved_digest: str,
) -> None:
    _require(approval.get("recoveryId") == approval_id, "Approval receipt identity drifted")
    _require(approval.get("status") == "completed", "Approval receipt status drifted")
    _require(
        type(approval.get("approvalCount")) is int
        and approval.get("approvalCount") == 1,
        "Approval executed more or less than once",
    )
    _require(
        approval.get("providerExecution") is True,
        "Approval provider execution proof drifted",
    )
    _require(approval.get("modelCall") is False, "Approval claimed a model call")
    _require(
        approval.get("approvedRemedyDigest") == approved_digest,
        "Approved remedy digest drifted",
    )

    _require(decline.get("recoveryId") == decline_id, "Decline receipt identity drifted")
    _require(
        decline.get("status") == "closed_without_action",
        "Decline receipt status drifted",
    )
    _require(
        type(decline.get("approvalCount")) is int
        and decline.get("approvalCount") == 0,
        "Decline approval count drifted",
    )
    _require(
        decline.get("providerExecution") is False,
        "Decline claimed provider execution",
    )
    _require(decline.get("modelCall") is False, "Decline claimed a model call")
    _require(
        decline.get("approvedRemedyDigest") is None,
        "Decline claimed an approved remedy",
    )


def _expected_decision(
    *,
    action: str,
    recovery_id: str,
    payload: dict[str, str],
) -> dict[str, Any]:
    approved = action == "approve"
    return {
        "action": action,
        "clientDecisionId": payload["clientDecisionId"],
        "recoveryId": recovery_id,
        "status": "completed" if approved else "closed_without_action",
        "approvedRemedyDigest": payload["remedyDigest"] if approved else None,
        "executionStarted": approved,
    }


def _verify_restart_fixture(
    owner: SmokeClient,
    foreign: SmokeClient,
    fixture: RestartFixture,
) -> None:
    approval_id = _recovery_id(fixture.approval_snapshot, status="pending_approval")
    decline_id = _recovery_id(fixture.decline_snapshot, status="pending_approval")
    replay_id = _recovery_id(fixture.replay_snapshot, status="completed")
    snapshots = (
        (approval_id, fixture.approval_snapshot),
        (decline_id, fixture.decline_snapshot),
        (replay_id, fixture.replay_snapshot),
    )
    for recovery_id, expected_snapshot in snapshots:
        restored = owner.get(f"/api/recoveries/{recovery_id}").json()
        _require(restored == expected_snapshot, "Owner recovery persistence drifted")
        hidden = foreign.get(
            f"/api/recoveries/{recovery_id}",
            expected_status=404,
        )
        _require(hidden.body == _GENERIC_NOT_FOUND, "Foreign recovery response drifted")
    _require(
        getattr(foreign, "secure_cookie_validated", False) is True,
        "The foreign session cookie was not security-hardened",
    )

    approval_path = f"/api/recoveries/{approval_id}/decisions"
    first_approval = owner.request(
        "POST",
        approval_path,
        payload=fixture.approval_payload,
    ).json()
    repeated_approval = owner.request(
        "POST",
        approval_path,
        payload=fixture.approval_payload,
    ).json()
    expected_approval = _expected_decision(
        action="approve",
        recovery_id=approval_id,
        payload=fixture.approval_payload,
    )
    _require(first_approval == expected_approval, "Approval response drifted")
    _require(repeated_approval == expected_approval, "Exact approval retry was not idempotent")

    decline_path = f"/api/recoveries/{decline_id}/decisions"
    decline = owner.request(
        "POST",
        decline_path,
        payload=fixture.decline_payload,
    ).json()
    _require(
        decline
        == _expected_decision(
            action="decline",
            recovery_id=decline_id,
            payload=fixture.decline_payload,
        ),
        "Decline response drifted",
    )

    approval_receipt = owner.get(f"/api/recoveries/{approval_id}/receipt").json()
    decline_receipt = owner.get(f"/api/recoveries/{decline_id}/receipt").json()
    _validate_terminal_receipts(
        approval_receipt,
        decline_receipt,
        approval_id=approval_id,
        decline_id=decline_id,
        approved_digest=fixture.approval_payload["remedyDigest"],
    )
    restored_replay_receipt = owner.get(f"/api/recoveries/{replay_id}/receipt").json()
    _require(
        restored_replay_receipt == fixture.replay_receipt,
        "Replay receipt persistence drifted",
    )

    restored_replay_stream = owner.get(f"/api/recoveries/{replay_id}/events").body
    _require(
        restored_replay_stream == fixture.replay_stream,
        "Replay SSE persistence drifted",
    )
    _validate_terminal_stream(
        restored_replay_stream,
        message="Restored replay terminal stream drifted",
    )
    for recovery_id in (approval_id, decline_id):
        terminal_stream = owner.get(f"/api/recoveries/{recovery_id}/events").body
        _validate_terminal_stream(
            terminal_stream,
            message="Replacement decision terminal stream drifted",
        )


def _container_arguments(
    *,
    image: str,
    name: str,
    volume: str,
    port: int,
) -> list[str]:
    return [
        "run",
        "-d",
        "--name",
        name,
        "-p",
        f"127.0.0.1:{port}:{_CONTAINER_PORT}",
        "--mount",
        f"type=volume,source={volume},target=/data",
        "-e",
        "BACKCHANNEL_CORS_ORIGINS",
        "-e",
        "BACKCHANNEL_DEMO_RESET_ENABLED",
        "-e",
        "BACKCHANNEL_DEPLOYED_MODE",
        "-e",
        "BACKCHANNEL_DB_PATH",
        "-e",
        "BACKCHANNEL_IDENTITY_HASH_SECRET",
        "-e",
        "BACKCHANNEL_MAX_CONCURRENT_EVENT_STREAMS",
        "-e",
        "BACKCHANNEL_MAX_EVENT_STREAMS_PER_RECOVERY",
        "-e",
        "BACKCHANNEL_SMOKE_CANARY",
        "-e",
        "OPENAI_API_KEY",
        image,
    ]


def _start_container(
    *,
    image: str,
    name: str,
    volume: str,
    port: int,
    environment: dict[str, str],
) -> str:
    container_id = _docker(
        _container_arguments(image=image, name=name, volume=volume, port=port),
        environment=environment,
    )
    _require(
        isinstance(container_id, str)
        and re.fullmatch(r"[0-9a-f]{12,64}", container_id) is not None,
        "Docker did not return a valid container identity",
    )
    assert isinstance(container_id, str)
    return container_id


def _stop_and_remove_container(name: str) -> None:
    started_at = time.monotonic()
    _docker(["stop", "--time", str(CONTAINER_STOP_TIMEOUT_SECONDS), name])
    elapsed = time.monotonic() - started_at
    _require(
        elapsed < CONTAINER_STOP_TIMEOUT_SECONDS,
        "First container exceeded its graceful stop budget",
    )
    exit_code = _docker(["inspect", "--format", "{{.State.ExitCode}}", name])
    _require(
        exit_code in {"0", "143"},
        "First container did not exit gracefully",
    )
    _docker(["rm", "-f", name])


def _try_cleanup_docker(arguments: list[str]) -> str | None:
    try:
        return _docker(arguments, allow_failure=True)
    except Exception:
        return None


def _cleanup_named_resource(
    *,
    expected_name: str,
    list_arguments: list[str],
    remove_arguments: list[str],
) -> bool:
    listed = _try_cleanup_docker(list_arguments)
    if listed is None:
        return False
    listed_names = listed.splitlines()
    if not listed_names:
        return True
    if listed_names != [expected_name]:
        return False
    removed = _try_cleanup_docker(remove_arguments)
    return removed == expected_name


def _cleanup_resources(
    *,
    first_name: str | None,
    second_name: str | None,
    volume: str | None,
) -> bool:
    results: list[bool] = []
    for container_name in (first_name, second_name):
        if container_name is None:
            continue
        results.append(
            _cleanup_named_resource(
                expected_name=container_name,
                list_arguments=[
                    "container",
                    "ls",
                    "--all",
                    "--filter",
                    f"name={container_name}",
                    "--format",
                    "{{.Names}}",
                ],
                remove_arguments=["rm", "-f", container_name],
            )
        )
    if volume is not None:
        results.append(
            _cleanup_named_resource(
                expected_name=volume,
                list_arguments=[
                    "volume",
                    "ls",
                    "--filter",
                    f"name={volume}",
                    "--format",
                    "{{.Name}}",
                ],
                remove_arguments=["volume", "rm", "-f", volume],
            )
        )
    return all(results)


def run_container_restart_smoke(*, image: str, canary: str) -> dict[str, object]:
    _require(bool(image.strip()), "A packaged image name is required")
    _require(bool(canary), "A non-empty BACKCHANNEL_SMOKE_CANARY is required")
    phase = "initialization"
    first_name: str | None = None
    second_name: str | None = None
    volume: str | None = None
    primary_failure: ContainerRestartFailure | None = None
    unexpected_failure_message: str | None = None
    try:
        suffix = secrets.token_hex(6)
        first_name = f"backchannel-restart-{suffix}-a"
        second_name = f"backchannel-restart-{suffix}-b"
        volume = f"backchannel-restart-{suffix}-data"
        identity_secret = secrets.token_urlsafe(48)
        port = _available_port()
        base_url = f"http://127.0.0.1:{port}"
        environment = dict(os.environ)
        environment.update(
            {
                "BACKCHANNEL_CORS_ORIGINS": "https://judge.example",
                "BACKCHANNEL_DEMO_RESET_ENABLED": "true",
                "BACKCHANNEL_DEPLOYED_MODE": "true",
                "BACKCHANNEL_DB_PATH": _DATABASE_PATH,
                "BACKCHANNEL_IDENTITY_HASH_SECRET": identity_secret,
                "BACKCHANNEL_MAX_CONCURRENT_EVENT_STREAMS": "1",
                "BACKCHANNEL_MAX_EVENT_STREAMS_PER_RECOVERY": "1",
                "BACKCHANNEL_SMOKE_CANARY": canary,
                "OPENAI_API_KEY": canary,
            }
        )

        phase = "volume_setup"
        _docker(["volume", "create", volume])
        phase = "first_container_start"
        first_id = _start_container(
            image=image,
            name=first_name,
            volume=volume,
            port=port,
            environment=environment,
        )
        phase = "first_container_readiness"
        _wait_for_ready(base_url, phase="first_container")
        phase = "first_container_api_setup"
        owner = SmokeClient(base_url, canary)
        fixture = _create_restart_fixture(owner)

        approval_id = _recovery_id(
            fixture.approval_snapshot,
            status="pending_approval",
        )
        held_stream = owner.open_event_stream(
            f"/api/recoveries/{approval_id}/events",
            expected_recovery_id=approval_id,
        )
        try:
            capacity_response = owner.get(f"/api/recoveries/{approval_id}/events")
            _validate_stream_capacity_response(
                capacity_response,
                recovery_id=approval_id,
            )
            phase = "first_container_shutdown"
            _stop_and_remove_container(first_name)
        finally:
            failure_in_progress = sys.exc_info()[0] is not None
            try:
                held_stream.close()
            except Exception:
                if not failure_in_progress:
                    raise ContainerRestartFailure("Retained event stream cleanup failed") from None
        phase = "replacement_container_start"
        second_id = _start_container(
            image=image,
            name=second_name,
            volume=volume,
            port=port,
            environment=environment,
        )
        _require(first_id != second_id, "Docker reused the first container identity")
        phase = "replacement_container_readiness"
        _wait_for_ready(base_url, phase="replacement_container")
        phase = "replacement_api_verification"
        foreign = SmokeClient(base_url, canary)
        _verify_restart_fixture(owner, foreign, fixture)
    except ContainerRestartFailure as error:
        primary_failure = error
    except Exception:
        unexpected_failure_message = _PHASE_FAILURE_MESSAGES[phase]

    cleanup_succeeded = _cleanup_resources(
        first_name=first_name,
        second_name=second_name,
        volume=volume,
    )
    if primary_failure is not None:
        raise primary_failure from None
    if unexpected_failure_message is not None:
        raise ContainerRestartFailure(unexpected_failure_message) from None
    if not cleanup_succeeded:
        raise ContainerRestartFailure(
            "Packaged restart smoke failed during cleanup"
        ) from None

    return {
        "approval": "completed_once_after_replacement",
        "containerReplacement": "passed",
        "containers": "distinct",
        "decline": "closed_without_action_after_replacement",
        "gracefulActiveSseShutdown": "passed",
        "pendingRecoveryResume": "passed",
        "proofLane": "packaged_replacement_container",
        "receiptPersistence": "passed",
        "sessionIsolation": "passed",
        "smoke": "passed",
        "ssePersistence": "passed",
        "terminalReplayPersistence": "passed",
        "volumePersistence": "passed",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--image",
        default=os.environ.get("BACKCHANNEL_SMOKE_IMAGE", "backchannel:build-week"),
        help="already-built packaged image to replace across one named volume",
    )
    arguments = parser.parse_args()
    try:
        result = run_container_restart_smoke(
            image=arguments.image,
            canary=os.environ.get("BACKCHANNEL_SMOKE_CANARY", ""),
        )
    except ContainerRestartFailure as error:
        print(
            json.dumps({"error": str(error), "smoke": "failed"}, sort_keys=True),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    except Exception:
        print(
            json.dumps(
                {"error": "Unexpected packaged restart failure", "smoke": "failed"},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
