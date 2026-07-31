from __future__ import annotations

import dataclasses
import json
from http.client import BadStatusLine, HTTPException, IncompleteRead, RemoteDisconnected
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


def _active_sse_fixture() -> SimpleNamespace:
    return SimpleNamespace(
        approval_snapshot={"recoveryId": "approval-id", "status": "pending_approval"}
    )


class FakeActiveSseOwner:
    secure_cookie_validated = True

    def __init__(self) -> None:
        self.close_calls = 0

    def open_event_stream(self, *_args: object, **_kwargs: object) -> object:
        owner = self

        class Held:
            def close(self) -> None:
                owner.close_calls += 1

        return Held()

    def get(self, _path: str) -> FakeResponse:
        request_id = "f" * 32
        return FakeResponse(
            body=(
                "retry: 5000\n"
                "event: stream.capacity\n"
                'data: {"code":"event_stream_capacity","message":'
                '"Event streaming is temporarily at capacity; retry is automatic.",'
                f'"requestId":"{request_id}"}}\n\n'
            ).encode(),
            headers={"content-type": "text/event-stream"},
        )


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
    lifecycle: list[str] = []
    container_ids = iter(("a" * 64, "b" * 64))
    containers: set[str] = set()
    volumes: set[str] = set()

    def fake_docker(
        arguments: list[str],
        *,
        environment: dict[str, str] | None = None,
        allow_failure: bool = False,
    ) -> str | None:
        calls.append((tuple(arguments), environment, allow_failure))
        if arguments[0] == "run":
            lifecycle.append(
                "run_a" if arguments[arguments.index("--name") + 1].endswith("-a") else "run_b"
            )
            containers.add(arguments[arguments.index("--name") + 1])
            return next(container_ids)
        if arguments[:2] == ["volume", "create"]:
            volumes.add(arguments[-1])
            return arguments[-1]
        if arguments[0] == "stop":
            assert held_stream.is_open
            lifecycle.append("stop_a")
            return arguments[-1]
        if arguments[0] == "inspect":
            assert held_stream.is_open
            lifecycle.append("inspect_a")
            return "0"
        if arguments[:2] == ["container", "ls"]:
            name = arguments[arguments.index("--filter") + 1].removeprefix("name=")
            return name if name in containers else ""
        if arguments[:2] == ["volume", "ls"]:
            name = arguments[arguments.index("--filter") + 1].removeprefix("name=")
            return name if name in volumes else ""
        if arguments[0] == "rm":
            name = arguments[-1]
            if name not in containers:
                return None
            containers.remove(name)
            return name
        if arguments[:3] == ["volume", "rm", "-f"]:
            name = arguments[-1]
            if name not in volumes:
                return None
            volumes.remove(name)
            return name
        return ""

    readiness: list[tuple[str, str]] = []

    def fake_readiness(base_url: str, *, phase: str) -> None:
        readiness.append((base_url, phase))

    class FakeHeldStream:
        def __init__(self) -> None:
            self.is_open = True
            self.close_calls = 0

        def close(self) -> None:
            assert self.is_open
            self.is_open = False
            self.close_calls += 1
            lifecycle.append("close_a_stream")

    held_stream = FakeHeldStream()
    request_id = "f" * 32
    capacity_body = (
        "retry: 5000\n"
        "event: stream.capacity\n"
        'data: {"code":"event_stream_capacity","message":'
        '"Event streaming is temporarily at capacity; retry is automatic.",'
        f'"requestId":"{request_id}"}}\n\n'
    ).encode()

    class OwnerClient:
        secure_cookie_validated = True

        def open_event_stream(
            self,
            path: str,
            *,
            expected_recovery_id: str,
        ) -> FakeHeldStream:
            assert path == "/api/recoveries/approval-id/events"
            assert expected_recovery_id == "approval-id"
            lifecycle.append("open_a_stream")
            return held_stream

        def get(self, path: str) -> FakeResponse:
            assert path == "/api/recoveries/approval-id/events"
            assert held_stream.is_open
            lifecycle.append("capacity_probe")
            return FakeResponse(
                body=capacity_body,
                headers={"content-type": "text/event-stream"},
            )

    owner_client = OwnerClient()
    foreign_client = type("Client", (), {"secure_cookie_validated": True})()
    clients = [owner_client, foreign_client]
    fixture = SimpleNamespace(
        approval_snapshot={"recoveryId": "approval-id", "status": "pending_approval"}
    )
    verify_calls: list[tuple[object, object, object]] = []
    monkeypatch.setattr(restart_smoke, "_docker", fake_docker)
    monkeypatch.setattr(restart_smoke, "_available_port", lambda: 43127)
    monkeypatch.setattr(restart_smoke, "_wait_for_ready", fake_readiness)
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
    assert readiness == [
        ("http://127.0.0.1:43127", "first_container"),
        ("http://127.0.0.1:43127", "replacement_container"),
    ]
    assert verify_calls == [(ANY, ANY, fixture)]
    owner, foreign, _ = verify_calls[0]
    assert owner is not foreign

    assert first_environment is not None
    assert second_environment is not None
    for key in (
        "BACKCHANNEL_ALLOWED_HOSTS",
        "BACKCHANNEL_DB_PATH",
        "BACKCHANNEL_IDENTITY_HASH_SECRET",
        "BACKCHANNEL_SMOKE_CANARY",
        "OPENAI_API_KEY",
    ):
        assert first_environment[key] == second_environment[key]
    assert first_environment["BACKCHANNEL_DB_PATH"] == "/data/backchannel.sqlite3"
    assert first_environment["BACKCHANNEL_ALLOWED_HOSTS"] == "127.0.0.1"
    assert first_environment["BACKCHANNEL_MAX_CONCURRENT_EVENT_STREAMS"] == "1"
    assert first_environment["BACKCHANNEL_MAX_EVENT_STREAMS_PER_RECOVERY"] == "1"
    assert "BACKCHANNEL_DEMO_RESET_ENABLED" not in first_arguments
    assert "BACKCHANNEL_DEMO_RESET_ENABLED" not in second_arguments
    assert "BACKCHANNEL_ALLOWED_HOSTS" in first_arguments
    assert "BACKCHANNEL_ALLOWED_HOSTS" in second_arguments
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
    assert lifecycle == [
        "run_a",
        "open_a_stream",
        "capacity_probe",
        "stop_a",
        "inspect_a",
        "close_a_stream",
        "run_b",
    ]
    assert held_stream.close_calls == 1
    cleanup = [arguments for arguments, _, allow_failure in calls if allow_failure]
    assert any(
        arguments[:2] == ("container", "ls")
        and any(first_name in argument for argument in arguments)
        for arguments in cleanup
    )
    assert any(
        arguments[:2] == ("container", "ls")
        and any(second_name in argument for argument in arguments)
        for arguments in cleanup
    )
    assert ("rm", "-f", second_name) in cleanup
    assert any(arguments[:3] == ("volume", "rm", "-f") for arguments in cleanup)
    assert not containers
    assert not volumes
    assert result == {
        "approval": "completed_once_after_replacement",
        "containerReplacement": "passed",
        "containers": "distinct",
        "decline": "closed_without_action_after_replacement",
        "cleanExitWithActiveSse": "passed",
        "pendingRecoveryResume": "passed",
        "proofLane": "packaged_replacement_container",
        "receiptPersistence": "passed",
        "sessionIsolation": "passed",
        "smoke": "passed",
        "ssePersistence": "passed",
        "terminalReplayPersistence": "passed",
        "volumePersistence": "passed",
    }


def test_stop_requires_exact_clean_exit_before_removal_with_strict_budget(
    restart_smoke: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    clock = iter((100.0, 104.999))

    def fake_docker(arguments: list[str], **_kwargs: object) -> str:
        calls.append(tuple(arguments))
        return "0" if arguments[0] == "inspect" else "restart-a"

    monkeypatch.setattr(restart_smoke, "_docker", fake_docker)
    monkeypatch.setattr(restart_smoke.time, "monotonic", lambda: next(clock))

    restart_smoke._stop_and_remove_container("restart-a")

    assert restart_smoke.CONTAINER_STOP_TIMEOUT_SECONDS == 5
    assert calls == [
        ("stop", "--time", "5", "restart-a"),
        ("inspect", "--format", "{{.State.ExitCode}}", "restart-a"),
        ("rm", "-f", "restart-a"),
    ]


@pytest.mark.parametrize(
    "exit_code",
    ["143", "137", "1", "-1", "+0", "0 extra", "", None],
)
def test_stop_rejects_forced_malformed_missing_or_other_exit_before_removal(
    restart_smoke: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    exit_code: str | None,
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_docker(arguments: list[str], **_kwargs: object) -> str | None:
        calls.append(tuple(arguments))
        return exit_code if arguments[0] == "inspect" else "restart-a"

    monkeypatch.setattr(restart_smoke, "_docker", fake_docker)
    monkeypatch.setattr(restart_smoke.time, "monotonic", iter((10.0, 11.0)).__next__)

    with pytest.raises(
        restart_smoke.ContainerRestartFailure,
        match="^First container did not exit cleanly$",
    ):
        restart_smoke._stop_and_remove_container("restart-a")

    assert calls == [
        ("stop", "--time", "5", "restart-a"),
        ("inspect", "--format", "{{.State.ExitCode}}", "restart-a"),
    ]


def test_stop_rejects_elapsed_time_at_fixed_boundary_before_inspect_or_remove(
    restart_smoke: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_docker(arguments: list[str], **_kwargs: object) -> str:
        calls.append(tuple(arguments))
        return "restart-a"

    monkeypatch.setattr(restart_smoke, "_docker", fake_docker)
    monkeypatch.setattr(restart_smoke.time, "monotonic", iter((10.0, 15.0)).__next__)

    with pytest.raises(
        restart_smoke.ContainerRestartFailure,
        match="^First container exceeded its clean stop budget$",
    ):
        restart_smoke._stop_and_remove_container("restart-a")

    assert calls == [("stop", "--time", "5", "restart-a")]


def test_sigterm_exit_cannot_start_replacement_or_return_success(
    restart_smoke: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[str, ...], bool]] = []
    owner = FakeActiveSseOwner()

    def fake_docker(
        arguments: list[str],
        *,
        environment: dict[str, str] | None = None,
        allow_failure: bool = False,
    ) -> str:
        del environment
        calls.append((tuple(arguments), allow_failure))
        if arguments[:2] == ["volume", "create"]:
            return arguments[-1]
        if arguments[0] == "run":
            return "a" * 64
        if arguments[0] == "stop":
            return arguments[-1]
        if arguments[0] == "inspect":
            return "143"
        if arguments[:2] in (["container", "ls"], ["volume", "ls"]):
            return ""
        return ""

    monkeypatch.setattr(restart_smoke, "_docker", fake_docker)
    monkeypatch.setattr(restart_smoke, "_available_port", lambda: 43128)
    monkeypatch.setattr(
        restart_smoke,
        "_wait_for_ready",
        lambda _url, *, phase: None,
    )
    monkeypatch.setattr(
        restart_smoke,
        "SmokeClient",
        lambda _url, _canary: owner,
    )
    monkeypatch.setattr(
        restart_smoke,
        "_create_restart_fixture",
        lambda _owner: _active_sse_fixture(),
    )
    monkeypatch.setattr(
        restart_smoke,
        "_verify_restart_fixture",
        lambda *_args: pytest.fail("replacement verification must not begin"),
    )

    with pytest.raises(
        restart_smoke.ContainerRestartFailure,
        match="^First container did not exit cleanly$",
    ):
        restart_smoke.run_container_restart_smoke(
            image="backchannel:test",
            canary="canary-kept-out-of-output",
        )

    run_calls = [arguments for arguments, _ in calls if arguments[0] == "run"]
    assert len(run_calls) == 1
    assert owner.close_calls == 1
    assert not any(
        arguments[:2] == ("rm", "-f") and not allow_failure
        for arguments, allow_failure in calls
    )


@pytest.mark.parametrize(
    "drift",
    [
        "wrong_code",
        "malformed_request_id",
        "extra_frame",
        "recovery_leak",
        "wrong_content_type",
        "prefix_confusable_content_type",
    ],
)
def test_capacity_probe_rejects_any_nonexact_finite_control_frame(
    restart_smoke: ModuleType,
    drift: str,
) -> None:
    recovery_id = "11111111-2222-4333-8444-555555555555"
    request_id = "f" * 32
    payload = {
        "code": "event_stream_capacity",
        "message": ("Event streaming is temporarily at capacity; retry is automatic."),
        "requestId": request_id,
    }
    if drift == "wrong_code":
        payload["code"] = "capacity"
    if drift == "malformed_request_id":
        payload["requestId"] = "not-a-request-id"
    if drift == "recovery_leak":
        payload["recoveryId"] = recovery_id
    body = (
        "retry: 5000\n"
        "event: stream.capacity\n"
        f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
    )
    if drift == "extra_frame":
        body += "event: extra\ndata: {}\n\n"
    if drift == "wrong_content_type":
        content_type = "application/json"
    elif drift == "prefix_confusable_content_type":
        content_type = "text/event-stream-evil; charset=utf-8"
    else:
        content_type = "text/event-stream"

    with pytest.raises(
        restart_smoke.ContainerRestartFailure,
        match="^Active event stream capacity proof drifted$",
    ):
        restart_smoke._validate_stream_capacity_response(
            FakeResponse(
                body=body.encode(),
                headers={"content-type": content_type},
            ),
            recovery_id=recovery_id,
        )


def test_capacity_probe_accepts_case_insensitive_parameterized_sse_media_type(
    restart_smoke: ModuleType,
) -> None:
    request_id = "f" * 32
    response = FakeResponse(
        body=(
            "retry: 5000\n"
            "event: stream.capacity\n"
            'data: {"code":"event_stream_capacity","message":'
            '"Event streaming is temporarily at capacity; retry is automatic.",'
            f'"requestId":"{request_id}"}}\n\n'
        ).encode(),
        headers={"content-type": " Text/Event-Stream ; charset=utf-8"},
    )

    restart_smoke._validate_stream_capacity_response(
        response,
        recovery_id="11111111-2222-4333-8444-555555555555",
    )


def test_held_stream_close_error_does_not_mask_primary_stop_failure(
    restart_smoke: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CloseFailingHeldStream:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            raise RuntimeError("close-canary-private-detail")

    held = CloseFailingHeldStream()

    class OwnerClient:
        def open_event_stream(self, *_args: object, **_kwargs: object) -> object:
            return held

        def get(self, _path: str) -> FakeResponse:
            return FakeResponse(body=b"capacity", headers={})

    starts: list[str] = []

    def fake_start_container(**kwargs: object) -> str:
        name = str(kwargs["name"])
        starts.append(name)
        return "a" * 64

    monkeypatch.setattr(restart_smoke, "_docker", lambda *_args, **_kwargs: "volume")
    monkeypatch.setattr(restart_smoke, "_available_port", lambda: 43140)
    monkeypatch.setattr(restart_smoke, "_start_container", fake_start_container)
    monkeypatch.setattr(restart_smoke, "_wait_for_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(restart_smoke, "SmokeClient", lambda *_args, **_kwargs: OwnerClient())
    monkeypatch.setattr(
        restart_smoke,
        "_create_restart_fixture",
        lambda _owner: SimpleNamespace(
            approval_snapshot={
                "recoveryId": "approval-id",
                "status": "pending_approval",
            }
        ),
    )
    monkeypatch.setattr(
        restart_smoke,
        "_validate_stream_capacity_response",
        lambda *_args, **_kwargs: None,
        raising=False,
    )
    monkeypatch.setattr(
        restart_smoke,
        "_stop_and_remove_container",
        lambda _name: (_ for _ in ()).throw(
            restart_smoke.ContainerRestartFailure("primary stop proof failed")
        ),
    )
    monkeypatch.setattr(restart_smoke, "_cleanup_resources", lambda **_kwargs: True)

    with pytest.raises(
        restart_smoke.ContainerRestartFailure,
        match="^primary stop proof failed$",
    ) as captured:
        restart_smoke.run_container_restart_smoke(
            image="backchannel:test",
            canary="canary-kept-out-of-output",
        )

    assert starts and len(starts) == 1
    assert held.close_calls == 1
    assert "close-canary" not in str(captured.value)


def test_smoke_client_opens_private_bounded_nonterminal_stream_on_dedicated_opener(
    restart_smoke: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import docker_smoke

    recovery_id = "11111111-2222-4333-8444-555555555555"
    frame = (
        "id: 1\n"
        "data: "
        + json.dumps(
            {
                "recoveryId": recovery_id,
                "seq": 1,
                "type": "recovery.started",
                "terminal": False,
                "data": {"summary": "started"},
                "createdAt": "2026-07-20T00:00:00Z",
            },
            separators=(",", ":"),
        )
        + "\n\n"
    ).encode("utf-8")

    class RawStream:
        status = 200
        headers = {"content-type": " Text/Event-Stream ; charset=utf-8"}

        def __init__(self) -> None:
            self._lines = iter(frame.splitlines(keepends=True))
            self.readline_limits: list[int] = []
            self.close_calls = 0

        def readline(self, limit: int) -> bytes:
            self.readline_limits.append(limit)
            return next(self._lines, b"")

        def close(self) -> None:
            self.close_calls += 1

    raw_stream = RawStream()
    opened_requests: list[tuple[object, float]] = []

    class DedicatedOpener:
        def open(self, request: object, *, timeout: float) -> RawStream:
            opened_requests.append((request, timeout))
            return raw_stream

    dedicated_opener = DedicatedOpener()
    monkeypatch.setattr(
        docker_smoke,
        "build_opener",
        lambda *_handlers: dedicated_opener,
    )
    client = restart_smoke.SmokeClient(
        "http://127.0.0.1:43141",
        "canary-not-in-frame",
    )
    client._loopback_session_cookie = "private-loopback-cookie"  # type: ignore[attr-defined]

    held = client.open_event_stream(
        f"/api/recoveries/{recovery_id}/events",
        expected_recovery_id=recovery_id,
    )

    assert len(opened_requests) == 1
    request, timeout = opened_requests[0]
    assert timeout == docker_smoke.HTTP_TIMEOUT_SECONDS
    assert request.get_header("Cookie") == (  # type: ignore[union-attr]
        "backchannel_demo_session=private-loopback-cookie"
    )
    assert raw_stream.readline_limits
    assert all(
        0 < limit <= docker_smoke.INITIAL_SSE_FRAME_MAX_BYTES + 1
        for limit in raw_stream.readline_limits
    )
    assert held.is_open
    assert raw_stream.close_calls == 0

    held.close()
    held.close()

    assert not held.is_open
    assert raw_stream.close_calls == 1


def test_smoke_client_rejects_prefix_confusable_sse_media_type_and_closes_response(
    restart_smoke: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import docker_smoke

    recovery_id = "11111111-2222-4333-8444-555555555555"
    frame = (
        f'id: 1\ndata: {{"recoveryId":"{recovery_id}","terminal":false}}\n\n'
    ).encode()

    class PrefixConfusableStream:
        status = 200
        headers = {"content-type": "text/event-stream-evil; charset=utf-8"}

        def __init__(self) -> None:
            self._lines = iter(frame.splitlines(keepends=True))
            self.close_calls = 0

        def readline(self, _limit: int) -> bytes:
            return next(self._lines, b"")

        def close(self) -> None:
            self.close_calls += 1

    raw_stream = PrefixConfusableStream()

    class DedicatedOpener:
        def open(self, _request: object, *, timeout: float) -> PrefixConfusableStream:
            assert timeout == docker_smoke.HTTP_TIMEOUT_SECONDS
            return raw_stream

    monkeypatch.setattr(
        docker_smoke,
        "build_opener",
        lambda *_handlers: DedicatedOpener(),
    )
    client = restart_smoke.SmokeClient(
        "http://127.0.0.1:43142",
        "canary-not-in-frame",
    )
    client._loopback_session_cookie = "private-loopback-cookie"  # type: ignore[attr-defined]

    with pytest.raises(
        restart_smoke.SmokeFailure,
        match="retained event stream was not SSE",
    ):
        client.open_event_stream(
            f"/api/recoveries/{recovery_id}/events",
            expected_recovery_id=recovery_id,
        )

    assert raw_stream.close_calls == 1


def test_cleanup_is_attempted_when_first_container_api_proof_fails(
    restart_smoke: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[str, ...], bool]] = []
    containers: set[str] = set()
    volumes: set[str] = set()

    def fake_docker(
        arguments: list[str],
        *,
        environment: dict[str, str] | None = None,
        allow_failure: bool = False,
    ) -> str | None:
        del environment
        calls.append((tuple(arguments), allow_failure))
        if arguments[0] == "run":
            containers.add(arguments[arguments.index("--name") + 1])
            return "c" * 64
        if arguments[:2] == ["volume", "create"]:
            volumes.add(arguments[-1])
            return arguments[-1]
        if arguments[:2] == ["container", "ls"]:
            name = arguments[arguments.index("--filter") + 1].removeprefix("name=")
            return name if name in containers else ""
        if arguments[:2] == ["volume", "ls"]:
            name = arguments[arguments.index("--filter") + 1].removeprefix("name=")
            return name if name in volumes else ""
        if arguments[0] == "rm":
            name = arguments[-1]
            containers.remove(name)
            return name
        if arguments[:3] == ["volume", "rm", "-f"]:
            name = arguments[-1]
            volumes.remove(name)
            return name
        return ""

    monkeypatch.setattr(restart_smoke, "_docker", fake_docker)
    monkeypatch.setattr(restart_smoke, "_available_port", lambda: 43128)
    monkeypatch.setattr(
        restart_smoke,
        "_wait_for_ready",
        lambda _url, *, phase: None,
    )
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
    assert len(
        [arguments for arguments in cleanup if arguments[:2] == ("container", "ls")]
    ) == 2
    assert len([arguments for arguments in cleanup if arguments[:2] == ("rm", "-f")]) == 1
    assert len([arguments for arguments in cleanup if arguments[:2] == ("volume", "ls")]) == 1
    assert len([arguments for arguments in cleanup if arguments[:3] == ("volume", "rm", "-f")]) == 1
    assert not containers
    assert not volumes


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
        "status": "completed",
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
            {
                "scenarioId": "api-quota",
                "executionMode": "replay_fixture",
                "clientRequestId": ANY,
            },
        )
    ]
    assert client.posts[0][1]["clientRequestId"].startswith("restart-smoke-")
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
    replay_snapshot = {"recoveryId": "replay-id", "status": "completed"}
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


def test_readiness_retries_transient_resets_before_success(
    restart_smoke: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReadyResponse:
        status = 200

        def __enter__(self) -> ReadyResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return b'{"status":"ready"}'

    attempts: list[float] = []
    outcomes: list[BaseException | ReadyResponse] = [
        ConnectionResetError("canary-reset-detail"),
        RemoteDisconnected("canary-disconnect-detail"),
        ReadyResponse(),
    ]

    def fake_urlopen(_url: str, *, timeout: float) -> ReadyResponse:
        attempts.append(timeout)
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(restart_smoke, "urlopen", fake_urlopen)
    monkeypatch.setattr(restart_smoke.time, "sleep", lambda _seconds: None)

    restart_smoke._wait_for_ready(
        "http://127.0.0.1:43129",
        phase="first_container",
    )

    assert len(attempts) == 3
    assert all(0 < timeout <= restart_smoke.HTTP_TIMEOUT_SECONDS for timeout in attempts)


def test_readiness_oserror_retries_stop_at_finite_deadline(
    restart_smoke: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": 0.0}
    attempts: list[float] = []

    def fake_urlopen(_url: str, *, timeout: float) -> None:
        attempts.append(timeout)
        raise OSError("canary-reset-detail /private/path")

    def fake_sleep(seconds: float) -> None:
        clock["now"] += seconds

    monkeypatch.setattr(restart_smoke, "READINESS_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(restart_smoke, "READINESS_POLL_SECONDS", 0.1)
    monkeypatch.setattr(restart_smoke, "urlopen", fake_urlopen)
    monkeypatch.setattr(restart_smoke.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(restart_smoke.time, "sleep", fake_sleep)

    with pytest.raises(
        restart_smoke.ContainerRestartFailure,
        match="^Replacement container readiness timed out$",
    ) as captured:
        restart_smoke._wait_for_ready(
            "http://127.0.0.1:43130",
            phase="replacement_container",
        )

    assert 1 <= len(attempts) <= 4
    assert clock["now"] <= 0.4
    assert all(0 < timeout <= restart_smoke.HTTP_TIMEOUT_SECONDS for timeout in attempts)
    assert "canary" not in str(captured.value)
    assert "path" not in str(captured.value)


@pytest.mark.parametrize(
    ("failure_phase", "expected_message"),
    [
        (
            "first_readiness",
            "Packaged restart smoke failed during first container readiness",
        ),
        (
            "replacement_verification",
            "Packaged restart smoke failed during replacement API verification",
        ),
    ],
)
def test_unexpected_orchestration_failures_map_to_fixed_secret_safe_phase(
    restart_smoke: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    failure_phase: str,
    expected_message: str,
) -> None:
    container_ids = iter(("a" * 64, "b" * 64))

    def fake_docker(
        arguments: list[str],
        *,
        environment: dict[str, str] | None = None,
        allow_failure: bool = False,
    ) -> str:
        del environment, allow_failure
        if arguments[0] == "run":
            return next(container_ids)
        if arguments[0] == "inspect":
            return "0"
        return ""

    readiness_calls = 0

    def fake_readiness(_base_url: str, *, phase: str) -> None:
        nonlocal readiness_calls
        readiness_calls += 1
        assert phase in {"first_container", "replacement_container"}
        if failure_phase == "first_readiness" and readiness_calls == 1:
            raise RuntimeError("canary-secret /private/database.sqlite3")

    clients = [FakeActiveSseOwner(), object()]
    monkeypatch.setattr(restart_smoke, "_docker", fake_docker)
    monkeypatch.setattr(restart_smoke, "_available_port", lambda: 43131)
    monkeypatch.setattr(restart_smoke, "_wait_for_ready", fake_readiness)
    monkeypatch.setattr(restart_smoke, "SmokeClient", lambda _url, _canary: clients.pop(0))
    monkeypatch.setattr(
        restart_smoke,
        "_create_restart_fixture",
        lambda _owner: _active_sse_fixture(),
    )

    def fake_verify(_owner: object, _foreign: object, _fixture: object) -> None:
        if failure_phase == "replacement_verification":
            raise RuntimeError("canary-secret /private/database.sqlite3")

    monkeypatch.setattr(restart_smoke, "_verify_restart_fixture", fake_verify)

    with pytest.raises(
        restart_smoke.ContainerRestartFailure,
        match=f"^{expected_message}$",
    ) as captured:
        restart_smoke.run_container_restart_smoke(
            image="backchannel:test",
            canary="canary-kept-out-of-output",
        )

    rendered = str(captured.value)
    assert rendered == expected_message
    assert "canary" not in rendered
    assert "private" not in rendered
    assert "sqlite" not in rendered


@pytest.mark.parametrize(
    ("phase", "expected_message"),
    [
        ("first_container", "First container readiness timed out"),
        ("replacement_container", "Replacement container readiness timed out"),
    ],
)
def test_readiness_timeout_is_fixed_and_phase_accurate(
    restart_smoke: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    expected_message: str,
) -> None:
    clock = {"now": 0.0}

    def fail_network(_url: str, *, timeout: float) -> None:
        assert 0 < timeout <= restart_smoke.HTTP_TIMEOUT_SECONDS
        raise ConnectionResetError("canary-timeout-detail /private/path")

    def advance(seconds: float) -> None:
        clock["now"] += seconds

    monkeypatch.setattr(restart_smoke, "READINESS_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(restart_smoke, "READINESS_POLL_SECONDS", 0.1)
    monkeypatch.setattr(restart_smoke, "urlopen", fail_network)
    monkeypatch.setattr(restart_smoke.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(restart_smoke.time, "sleep", advance)

    with pytest.raises(
        restart_smoke.ContainerRestartFailure,
        match=f"^{expected_message}$",
    ) as captured:
        restart_smoke._wait_for_ready(
            "http://127.0.0.1:43132",
            phase=phase,
        )

    assert str(captured.value) == expected_message
    assert "canary" not in str(captured.value)
    assert "private" not in str(captured.value)


def test_readiness_retries_transient_http_protocol_failures(
    restart_smoke: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReadyResponse:
        status = 200

        def __enter__(self) -> ReadyResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return b'{"status":"ready"}'

    outcomes: list[HTTPException | ReadyResponse] = [
        BadStatusLine("canary-bad-status"),
        IncompleteRead(b"canary-partial", 64),
        HTTPException("canary-http-detail"),
        ReadyResponse(),
    ]
    attempts: list[float] = []

    def fake_urlopen(_url: str, *, timeout: float) -> ReadyResponse:
        attempts.append(timeout)
        outcome = outcomes.pop(0)
        if isinstance(outcome, HTTPException):
            raise outcome
        return outcome

    monkeypatch.setattr(restart_smoke, "urlopen", fake_urlopen)
    monkeypatch.setattr(restart_smoke.time, "sleep", lambda _seconds: None)

    restart_smoke._wait_for_ready(
        "http://127.0.0.1:43133",
        phase="first_container",
    )

    assert len(attempts) == 4
    assert all(0 < timeout <= restart_smoke.HTTP_TIMEOUT_SECONDS for timeout in attempts)


def test_readiness_does_not_swallow_non_network_programming_errors(
    restart_smoke: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_programming(_url: str, *, timeout: float) -> None:
        del timeout
        raise RuntimeError("programming failure")

    monkeypatch.setattr(restart_smoke, "urlopen", fail_programming)

    with pytest.raises(RuntimeError, match="^programming failure$"):
        restart_smoke._wait_for_ready(
            "http://127.0.0.1:43134",
            phase="first_container",
        )


@pytest.mark.parametrize("failure_kind", ["known", "unexpected"])
def test_cleanup_failure_attempts_every_resource_and_fails_closed(
    restart_smoke: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    calls: list[tuple[tuple[str, ...], int]] = []
    container_ids = iter(("a" * 64, "b" * 64))
    run_count = 0
    cleanup_failure_injected = False
    containers: set[str] = set()
    volumes: set[str] = set()

    def fake_docker(
        arguments: list[str],
        *,
        environment: dict[str, str] | None = None,
        allow_failure: bool = False,
    ) -> str | None:
        nonlocal cleanup_failure_injected, run_count
        del environment
        if arguments[0] == "run":
            run_count += 1
            calls.append((tuple(arguments), run_count))
            containers.add(arguments[arguments.index("--name") + 1])
            return next(container_ids)
        calls.append((tuple(arguments), run_count))
        if arguments[:2] == ["volume", "create"]:
            volumes.add(arguments[-1])
            return arguments[-1]
        if arguments[0] == "stop":
            return arguments[-1]
        if arguments[0] == "inspect":
            return "0"
        if arguments[:2] == ["container", "ls"]:
            name = arguments[arguments.index("--filter") + 1].removeprefix("name=")
            return name if name in containers else ""
        if arguments[:2] == ["volume", "ls"]:
            name = arguments[arguments.index("--filter") + 1].removeprefix("name=")
            return name if name in volumes else ""
        if arguments[0] == "rm":
            name = arguments[-1]
            if run_count == 2 and name.endswith("-b") and not cleanup_failure_injected:
                assert allow_failure is True
                cleanup_failure_injected = True
                if failure_kind == "known":
                    return None
                raise RuntimeError("canary cleanup /private/path")
            containers.remove(name)
            return name
        if arguments[:3] == ["volume", "rm", "-f"]:
            name = arguments[-1]
            volumes.remove(name)
            return name
        return ""

    monkeypatch.setattr(restart_smoke, "_docker", fake_docker)
    monkeypatch.setattr(restart_smoke, "_available_port", lambda: 43135)
    monkeypatch.setattr(
        restart_smoke,
        "_wait_for_ready",
        lambda _url, *, phase: None,
    )
    clients = [FakeActiveSseOwner(), object()]
    monkeypatch.setattr(
        restart_smoke,
        "SmokeClient",
        lambda _url, _canary: clients.pop(0),
    )
    monkeypatch.setattr(
        restart_smoke,
        "_create_restart_fixture",
        lambda _owner: _active_sse_fixture(),
    )
    monkeypatch.setattr(
        restart_smoke,
        "_verify_restart_fixture",
        lambda _owner, _foreign, _fixture: None,
    )

    with pytest.raises(
        restart_smoke.ContainerRestartFailure,
        match="^Packaged restart smoke failed during cleanup$",
    ) as captured:
        restart_smoke.run_container_restart_smoke(
            image="backchannel:test",
            canary="canary-kept-out-of-output",
        )

    cleanup_calls = [
        arguments
        for arguments, observed_runs in calls
        if observed_runs == 2 and arguments[0] != "run"
    ]
    assert len(cleanup_calls) == 5
    assert cleanup_calls[0][:2] == ("container", "ls")
    assert cleanup_calls[1][:2] == ("container", "ls")
    assert cleanup_calls[2][:2] == ("rm", "-f")
    assert cleanup_calls[3][:2] == ("volume", "ls")
    assert cleanup_calls[4][:3] == ("volume", "rm", "-f")
    assert "secret" not in str(captured.value)
    assert "canary" not in str(captured.value)
    assert "private" not in str(captured.value)


def test_cleanup_failure_preserves_primary_failure_and_attempts_every_resource(
    restart_smoke: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[str, ...], int]] = []
    container_ids = iter(("a" * 64, "b" * 64))
    run_count = 0
    cleanup_failure_injected = False
    containers: set[str] = set()
    volumes: set[str] = set()

    def fake_docker(
        arguments: list[str],
        *,
        environment: dict[str, str] | None = None,
        allow_failure: bool = False,
    ) -> str | None:
        nonlocal cleanup_failure_injected, run_count
        del environment
        if arguments[0] == "run":
            run_count += 1
            calls.append((tuple(arguments), run_count))
            containers.add(arguments[arguments.index("--name") + 1])
            return next(container_ids)
        calls.append((tuple(arguments), run_count))
        if arguments[:2] == ["volume", "create"]:
            volumes.add(arguments[-1])
            return arguments[-1]
        if arguments[0] == "stop":
            return arguments[-1]
        if arguments[0] == "inspect":
            return "0"
        if arguments[:2] == ["container", "ls"]:
            name = arguments[arguments.index("--filter") + 1].removeprefix("name=")
            return name if name in containers else ""
        if arguments[:2] == ["volume", "ls"]:
            name = arguments[arguments.index("--filter") + 1].removeprefix("name=")
            return name if name in volumes else ""
        if arguments[0] == "rm":
            name = arguments[-1]
            if run_count == 2 and name.endswith("-b") and not cleanup_failure_injected:
                assert allow_failure is True
                cleanup_failure_injected = True
                raise RuntimeError("cleanup canary /private/path")
            containers.remove(name)
            return name
        if arguments[:3] == ["volume", "rm", "-f"]:
            name = arguments[-1]
            volumes.remove(name)
            return name
        return ""

    monkeypatch.setattr(restart_smoke, "_docker", fake_docker)
    monkeypatch.setattr(restart_smoke, "_available_port", lambda: 43136)
    monkeypatch.setattr(
        restart_smoke,
        "_wait_for_ready",
        lambda _url, *, phase: None,
    )
    clients = [FakeActiveSseOwner(), object()]
    monkeypatch.setattr(
        restart_smoke,
        "SmokeClient",
        lambda _url, _canary: clients.pop(0),
    )
    monkeypatch.setattr(
        restart_smoke,
        "_create_restart_fixture",
        lambda _owner: _active_sse_fixture(),
    )
    monkeypatch.setattr(
        restart_smoke,
        "_verify_restart_fixture",
        lambda _owner, _foreign, _fixture: (_ for _ in ()).throw(
            RuntimeError("primary canary /private/database.sqlite3")
        ),
    )

    with pytest.raises(
        restart_smoke.ContainerRestartFailure,
        match="^Packaged restart smoke failed during replacement API verification$",
    ) as captured:
        restart_smoke.run_container_restart_smoke(
            image="backchannel:test",
            canary="canary-kept-out-of-output",
        )

    cleanup_calls = [
        arguments
        for arguments, observed_runs in calls
        if observed_runs == 2 and arguments[0] != "run"
    ]
    assert len(cleanup_calls) == 5
    assert "cleanup" not in str(captured.value).lower()
    assert "canary" not in str(captured.value)
    assert "private" not in str(captured.value)
