from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import ANY

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "docker_restart_smoke.py"


class FakeResponse:
    def __init__(
        self,
        payload: dict[str, Any] | None = None,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._payload = payload
        self.body = body if body is not None else json.dumps(payload or {}).encode("utf-8")
        self.headers = headers or {}

    def json(self) -> dict[str, Any]:
        assert self._payload is not None
        return self._payload


@pytest.fixture
def restart_smoke() -> ModuleType:
    if not SCRIPT.is_file():
        pytest.skip("restart smoke implementation does not exist yet")
    from scripts import docker_restart_smoke

    return docker_restart_smoke


def test_restart_smoke_script_exists() -> None:
    assert SCRIPT.is_file()


def test_replacement_uses_distinct_containers_one_volume_and_memory_only_cookie(
    restart_smoke: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, str] | None, bool]] = []
    container_ids = iter(("a" * 64, "b" * 64))

    def fake_docker(
        arguments: list[str],
        *,
        environment: dict[str, str] | None = None,
        allow_failure: bool = False,
    ) -> str:
        calls.append((tuple(arguments), environment, allow_failure))
        if arguments[0] == "run":
            return next(container_ids)
        return ""

    readiness: list[str] = []
    clients = [
        type("Client", (), {"secure_cookie_validated": True})(),
        type("Client", (), {"secure_cookie_validated": True})(),
    ]
    fixture = object()
    verify_calls: list[tuple[object, object, object]] = []
    monkeypatch.setattr(restart_smoke, "_docker", fake_docker)
    monkeypatch.setattr(restart_smoke, "_available_port", lambda: 43127)
    monkeypatch.setattr(restart_smoke, "_wait_for_ready", readiness.append)
    monkeypatch.setattr(restart_smoke.secrets, "token_hex", lambda _size: "unit123")
    monkeypatch.setattr(
        restart_smoke.secrets,
        "token_urlsafe",
        lambda _size: "identity-secret-kept-out-of-argv",
    )
    monkeypatch.setattr(
        restart_smoke,
        "SmokeClient",
        lambda _url, _canary: clients.pop(0),
    )
    monkeypatch.setattr(restart_smoke, "_create_restart_fixture", lambda owner: fixture)
    monkeypatch.setattr(
        restart_smoke,
        "_verify_restart_fixture",
        lambda owner, foreign, evidence: verify_calls.append((owner, foreign, evidence)),
    )

    result = restart_smoke.run_container_restart_smoke(
        image="backchannel:test",
        canary="canary-kept-out-of-argv",
    )

    run_calls = [call for call in calls if call[0][0] == "run"]
    assert len(run_calls) == 2
    first_arguments, first_environment, _ = run_calls[0]
    second_arguments, second_environment, _ = run_calls[1]
    first_name = first_arguments[first_arguments.index("--name") + 1]
    second_name = second_arguments[second_arguments.index("--name") + 1]
    assert first_name != second_name
    assert first_name.endswith("-a")
    assert second_name.endswith("-b")
    first_mount = first_arguments[first_arguments.index("--mount") + 1]
    second_mount = second_arguments[second_arguments.index("--mount") + 1]
    assert first_mount == second_mount
    assert "type=volume" in first_mount
    assert "target=/data" in first_mount
    assert readiness == ["http://127.0.0.1:43127", "http://127.0.0.1:43127"]
    assert verify_calls == [(ANY, ANY, fixture)]
    owner, foreign, _ = verify_calls[0]
    assert owner is not foreign

    assert first_environment is not None
    assert second_environment is not None
    for key in (
        "BACKCHANNEL_DB_PATH",
        "BACKCHANNEL_IDENTITY_HASH_SECRET",
        "BACKCHANNEL_SMOKE_CANARY",
        "OPENAI_API_KEY",
    ):
        assert first_environment[key] == second_environment[key]
    assert first_environment["BACKCHANNEL_DB_PATH"] == "/data/backchannel.sqlite3"
    serialized_arguments = json.dumps([arguments for arguments, _, _ in calls])
    for secret_value in (
        "identity-secret-kept-out-of-argv",
        "canary-kept-out-of-argv",
        "/data/backchannel.sqlite3",
    ):
        assert secret_value not in serialized_arguments
    assert "Cookie" not in serialized_arguments
    stop_index = next(
        index for index, (arguments, _, _) in enumerate(calls) if arguments[:1] == ("stop",)
    )
    second_run_index = next(
        index
        for index, (arguments, _, _) in enumerate(calls)
        if arguments == second_arguments
    )
    assert stop_index < second_run_index
    cleanup = [arguments for arguments, _, allow_failure in calls if allow_failure]
    assert ("rm", "-f", first_name) in cleanup
    assert ("rm", "-f", second_name) in cleanup
    assert any(arguments[:3] == ("volume", "rm", "-f") for arguments in cleanup)
    assert result == {
        "approval": "completed_once_after_replacement",
        "containerReplacement": "passed",
        "containers": "distinct",
        "decline": "closed_without_action_after_replacement",
        "pendingRecoveryResume": "passed",
        "proofLane": "packaged_replacement_container",
        "receiptPersistence": "passed",
        "sessionIsolation": "passed",
        "smoke": "passed",
        "ssePersistence": "passed",
        "terminalReplayPersistence": "passed",
        "volumePersistence": "passed",
    }


def test_cleanup_is_attempted_when_first_container_api_proof_fails(
    restart_smoke: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[str, ...], bool]] = []

    def fake_docker(
        arguments: list[str],
        *,
        environment: dict[str, str] | None = None,
        allow_failure: bool = False,
    ) -> str:
        del environment
        calls.append((tuple(arguments), allow_failure))
        return "c" * 64 if arguments[0] == "run" else ""

    monkeypatch.setattr(restart_smoke, "_docker", fake_docker)
    monkeypatch.setattr(restart_smoke, "_available_port", lambda: 43128)
    monkeypatch.setattr(restart_smoke, "_wait_for_ready", lambda _url: None)
    monkeypatch.setattr(restart_smoke, "SmokeClient", lambda _url, _canary: object())
    monkeypatch.setattr(
        restart_smoke,
        "_create_restart_fixture",
        lambda _owner: (_ for _ in ()).throw(restart_smoke.ContainerRestartFailure("safe")),
    )

    with pytest.raises(restart_smoke.ContainerRestartFailure, match="safe"):
        restart_smoke.run_container_restart_smoke(
            image="backchannel:test",
            canary="test-canary",
        )

    cleanup = [arguments for arguments, allow_failure in calls if allow_failure]
    assert len([arguments for arguments in cleanup if arguments[:2] == ("rm", "-f")]) == 2
    assert len(
        [arguments for arguments in cleanup if arguments[:3] == ("volume", "rm", "-f")]
    ) == 1


def test_first_container_creates_two_pending_sdk_recoveries_and_terminal_replay(
    restart_smoke: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approval = {
        "recoveryId": "approval-id",
        "status": "pending_approval",
        "pendingApproval": {
            "remedyId": "approval-remedy",
            "remedyDigest": "sha256:" + "1" * 64,
            "toolCallId": "approval-tool",
        },
    }
    decline = {
        "recoveryId": "decline-id",
        "status": "pending_approval",
        "pendingApproval": {
            "remedyId": "decline-remedy",
            "remedyDigest": "sha256:" + "2" * 64,
            "toolCallId": "decline-tool",
        },
    }
    pending = iter((approval, decline))
    monkeypatch.setattr(restart_smoke, "_pending_hotel", lambda _client: next(pending))

    replay = {
        "recoveryId": "replay-id",
        "status": "simulated_completed",
        "executionMode": "replay_fixture",
    }
    replay_receipt = {"recoveryId": "replay-id", "status": "simulated_completed"}
    replay_stream = b'id: 1\ndata: {"terminal":true}\n\n'

    class FirstClient:
        secure_cookie_validated = True

        def __init__(self) -> None:
            self.posts: list[tuple[str, dict[str, str]]] = []

        def post(self, path: str, payload: dict[str, str]) -> FakeResponse:
            self.posts.append((path, payload))
            return FakeResponse(replay)

        def get(self, path: str, **_kwargs: object) -> FakeResponse:
            if path.endswith("/receipt"):
                return FakeResponse(replay_receipt)
            if path.endswith("/events"):
                return FakeResponse(
                    body=replay_stream,
                    headers={"content-type": "text/event-stream"},
                )
            raise AssertionError(path)

    client = FirstClient()
    fixture = restart_smoke._create_restart_fixture(client)

    assert client.posts == [
        (
            "/api/recoveries",
            {"scenarioId": "api-quota", "executionMode": "replay_fixture"},
        )
    ]
    assert fixture.approval_snapshot == approval
    assert fixture.decline_snapshot == decline
    assert fixture.replay_snapshot == replay
    assert fixture.replay_receipt == replay_receipt
    assert fixture.replay_stream == replay_stream
    assert fixture.approval_payload["action"] == "approve"
    assert fixture.decline_payload["action"] == "decline"
    assert "cookie" not in {field.name.lower() for field in dataclasses.fields(fixture)}


def test_replacement_reads_all_isolates_foreign_and_replays_exact_approval(
    restart_smoke: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approval_digest = "sha256:" + "1" * 64
    approval_snapshot = {"recoveryId": "approval-id", "status": "pending_approval"}
    decline_snapshot = {"recoveryId": "decline-id", "status": "pending_approval"}
    replay_snapshot = {"recoveryId": "replay-id", "status": "simulated_completed"}
    replay_receipt = {"recoveryId": "replay-id", "status": "simulated_completed"}
    replay_stream = b'id: 1\ndata: {"terminal":true}\n\n'
    approval_payload = {
        "action": "approve",
        "clientDecisionId": "stable-approval-attempt",
        "remedyId": "approval-remedy",
        "remedyDigest": approval_digest,
        "toolCallId": "approval-tool",
    }
    decline_payload = {
        "action": "decline",
        "clientDecisionId": "stable-decline-attempt",
        "remedyId": "decline-remedy",
        "remedyDigest": "sha256:" + "2" * 64,
        "toolCallId": "decline-tool",
    }
    fixture = restart_smoke.RestartFixture(
        approval_snapshot=approval_snapshot,
        approval_payload=approval_payload,
        decline_snapshot=decline_snapshot,
        decline_payload=decline_payload,
        replay_snapshot=replay_snapshot,
        replay_receipt=replay_receipt,
        replay_stream=replay_stream,
    )
    approval_decision = {
        "action": "approve",
        "clientDecisionId": "stable-approval-attempt",
        "recoveryId": "approval-id",
        "status": "completed",
        "approvedRemedyDigest": approval_digest,
        "executionStarted": True,
    }
    decline_decision = {
        "action": "decline",
        "clientDecisionId": "stable-decline-attempt",
        "recoveryId": "decline-id",
        "status": "closed_without_action",
        "approvedRemedyDigest": None,
        "executionStarted": False,
    }
    approval_receipt = {
        "recoveryId": "approval-id",
        "status": "completed",
        "approvalCount": 1,
        "providerExecution": True,
        "modelCall": False,
        "approvedRemedyDigest": approval_digest,
    }
    decline_receipt = {
        "recoveryId": "decline-id",
        "status": "closed_without_action",
        "approvalCount": 0,
        "providerExecution": False,
        "modelCall": False,
        "approvedRemedyDigest": None,
    }
    terminal_stream = b'id: 2\ndata: {"terminal":true}\n\n'

    class OwnerClient:
        def __init__(self) -> None:
            self.requests: list[tuple[str, str, dict[str, str]]] = []

        def get(self, path: str, **_kwargs: object) -> FakeResponse:
            responses = {
                "/api/recoveries/approval-id": FakeResponse(approval_snapshot),
                "/api/recoveries/decline-id": FakeResponse(decline_snapshot),
                "/api/recoveries/replay-id": FakeResponse(replay_snapshot),
                "/api/recoveries/approval-id/receipt": FakeResponse(approval_receipt),
                "/api/recoveries/decline-id/receipt": FakeResponse(decline_receipt),
                "/api/recoveries/replay-id/receipt": FakeResponse(replay_receipt),
                "/api/recoveries/approval-id/events": FakeResponse(body=terminal_stream),
                "/api/recoveries/decline-id/events": FakeResponse(body=terminal_stream),
                "/api/recoveries/replay-id/events": FakeResponse(body=replay_stream),
            }
            return responses[path]

        def request(
            self,
            method: str,
            path: str,
            *,
            payload: dict[str, str],
        ) -> FakeResponse:
            self.requests.append((method, path, payload))
            if path.endswith("approval-id/decisions"):
                return FakeResponse(approval_decision)
            return FakeResponse(decline_decision)

    class ForeignClient:
        secure_cookie_validated = True

        def __init__(self) -> None:
            self.paths: list[str] = []

        def get(self, path: str, *, expected_status: int) -> FakeResponse:
            assert expected_status == 404
            self.paths.append(path)
            return FakeResponse(body=b'{"detail":"Not found"}')

    owner = OwnerClient()
    foreign = ForeignClient()
    restart_smoke._verify_restart_fixture(owner, foreign, fixture)

    assert owner.requests == [
        ("POST", "/api/recoveries/approval-id/decisions", approval_payload),
        ("POST", "/api/recoveries/approval-id/decisions", approval_payload),
        ("POST", "/api/recoveries/decline-id/decisions", decline_payload),
    ]
    assert foreign.paths == [
        "/api/recoveries/approval-id",
        "/api/recoveries/decline-id",
        "/api/recoveries/replay-id",
    ]


@pytest.mark.parametrize(
    ("receipt_name", "field", "bad_value"),
    [
        ("approval", "approvalCount", 2),
        ("approval", "providerExecution", False),
        ("approval", "modelCall", True),
        ("decline", "approvalCount", 1),
        ("decline", "providerExecution", True),
        ("decline", "modelCall", True),
    ],
)
def test_receipt_contract_rejects_execution_or_provenance_drift(
    restart_smoke: ModuleType,
    receipt_name: str,
    field: str,
    bad_value: object,
) -> None:
    approval = {
        "recoveryId": "approval-id",
        "status": "completed",
        "approvalCount": 1,
        "providerExecution": True,
        "modelCall": False,
        "approvedRemedyDigest": "sha256:" + "1" * 64,
    }
    decline = {
        "recoveryId": "decline-id",
        "status": "closed_without_action",
        "approvalCount": 0,
        "providerExecution": False,
        "modelCall": False,
        "approvedRemedyDigest": None,
    }
    target = approval if receipt_name == "approval" else decline
    target[field] = bad_value

    with pytest.raises(restart_smoke.ContainerRestartFailure):
        restart_smoke._validate_terminal_receipts(
            approval,
            decline,
            approval_id="approval-id",
            decline_id="decline-id",
            approved_digest="sha256:" + "1" * 64,
        )


def test_all_external_operations_have_finite_deadlines(
    restart_smoke: ModuleType,
) -> None:
    assert 0 < restart_smoke.DOCKER_TIMEOUT_SECONDS <= 60
    assert 0 < restart_smoke.HTTP_TIMEOUT_SECONDS <= 15
    assert 0 < restart_smoke.READINESS_TIMEOUT_SECONDS <= 60
    assert restart_smoke.READINESS_POLL_SECONDS > 0


def test_docker_inherits_secret_values_outside_argv_and_bounds_cleanup(
    restart_smoke: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append({"command": command, **kwargs})
        return SimpleNamespace(returncode=0, stdout=b"d" * 64 + b"\n")

    monkeypatch.setattr(restart_smoke.subprocess, "run", fake_run)
    environment = {
        "PATH": "/usr/bin",
        "BACKCHANNEL_IDENTITY_HASH_SECRET": "identity-value",
        "BACKCHANNEL_SMOKE_CANARY": "canary-value",
        "BACKCHANNEL_DB_PATH": "/data/backchannel.sqlite3",
    }

    assert (
        restart_smoke._docker(
            [
                "run",
                "-e",
                "BACKCHANNEL_IDENTITY_HASH_SECRET",
                "-e",
                "BACKCHANNEL_SMOKE_CANARY",
                "-e",
                "BACKCHANNEL_DB_PATH",
                "image:test",
            ],
            environment=environment,
        )
        == "d" * 64
    )
    restart_smoke._docker(["rm", "-f", "container-a"], allow_failure=True)

    assert calls[0]["env"] is environment
    serialized_command = json.dumps(calls[0]["command"])
    for value in (
        "identity-value",
        "canary-value",
        "/data/backchannel.sqlite3",
    ):
        assert value not in serialized_command
    assert all(
        call["timeout"] == restart_smoke.DOCKER_TIMEOUT_SECONDS for call in calls
    )
    assert all(call["stderr"] is restart_smoke.subprocess.DEVNULL for call in calls)
