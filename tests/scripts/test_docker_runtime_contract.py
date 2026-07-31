from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "docker_runtime_contract.py"


@pytest.fixture
def runtime_contract() -> ModuleType:
    if not SCRIPT.is_file():
        pytest.skip("container runtime contract implementation does not exist yet")
    from scripts import docker_runtime_contract

    return docker_runtime_contract


def _valid_payload(
    *,
    volume: str = "runtime-data",
    port: int = 43127,
) -> dict[str, object]:
    return {
        "user": "10001:10001",
        "readOnlyRootfs": True,
        "privileged": False,
        "capAdd": None,
        "capDrop": ["ALL"],
        "securityOpt": [
            "no-new-privileges:true",
            "seccomp=builtin",
        ],
        "pidMode": "",
        "ipcMode": "private",
        "utsMode": "",
        "cgroupnsMode": "private",
        "networkMode": "bridge",
        "usernsMode": "",
        "devices": [],
        "deviceRequests": None,
        "deviceCgroupRules": [],
        "groupAdd": None,
        "pidsLimit": 128,
        "restartPolicy": {
            "Name": "no",
            "MaximumRetryCount": 0,
        },
        "autoRemove": False,
        "publishAllPorts": False,
        "portBindings": {
            "8000/tcp": [
                {
                    "HostIp": "127.0.0.1",
                    "HostPort": str(port),
                }
            ]
        },
        "mounts": [
            {
                "type": "volume",
                "name": volume,
                "destination": "/data",
                "rw": True,
                "driver": "local",
            }
        ],
    }


def test_runtime_contract_script_exists() -> None:
    assert SCRIPT.is_file()


def test_process_probe_matches_the_reviewed_exact_source() -> None:
    from scripts import docker_runtime_contract

    assert hashlib.sha256(
        docker_runtime_contract._PROCESS_PROBE_CODE.encode("utf-8")
    ).hexdigest() == "46c150a19ba71df8bcd73b60b40cb72d2d7170c59813e5846c11b01a778b88ad"


def test_inspect_projection_matches_the_reviewed_exact_source() -> None:
    from scripts import docker_runtime_contract

    assert hashlib.sha256(
        docker_runtime_contract._RUNTIME_INSPECT_FORMAT.encode("utf-8")
    ).hexdigest() == "c6164c1b429bdbbecc6a2f4a36320594de829d228e4999bc2fad62288ec18ca3"


@pytest.mark.parametrize(
    "security_option",
    [
        "no-new-privileges",
        "no-new-privileges:true",
        "no-new-privileges=true",
    ],
)
def test_validator_accepts_exact_daemon_runtime_contract(
    runtime_contract: ModuleType,
    security_option: str,
) -> None:
    payload = _valid_payload()
    payload["securityOpt"] = [
        "seccomp=builtin",
        security_option,
    ]

    runtime_contract._validate_runtime_contract(
        payload,
        expected_volume="runtime-data",
        expected_port=43127,
    )


def test_validator_rejects_runtime_contract_drift(
    runtime_contract: ModuleType,
) -> None:
    mutations: list[tuple[str, object]] = [
        ("user", "0:0"),
        ("readOnlyRootfs", False),
        ("readOnlyRootfs", 1),
        ("privileged", True),
        ("privileged", 0),
        ("capAdd", ["SYS_ADMIN"]),
        ("capDrop", None),
        ("capDrop", []),
        ("capDrop", ["NET_BIND_SERVICE"]),
        ("securityOpt", None),
        ("securityOpt", []),
        ("securityOpt", ["no-new-privileges:true"]),
        ("securityOpt", ["seccomp=builtin"]),
        (
            "securityOpt",
            ["no-new-privileges:false", "seccomp=builtin"],
        ),
        (
            "securityOpt",
            ["no-new-privileges:true", "seccomp=unconfined"],
        ),
        (
            "securityOpt",
            [
                "no-new-privileges:true",
                "seccomp=builtin",
                "systempaths=unconfined",
            ],
        ),
        ("pidMode", "host"),
        ("pidMode", "container:foreign"),
        ("ipcMode", "host"),
        ("ipcMode", "shareable"),
        ("utsMode", "host"),
        ("cgroupnsMode", "host"),
        ("cgroupnsMode", ""),
        ("networkMode", "host"),
        ("networkMode", "container:foreign"),
        ("usernsMode", "host"),
        ("devices", [{"PathOnHost": "/dev/sda"}]),
        ("deviceRequests", [{"Driver": "nvidia"}]),
        ("deviceCgroupRules", ["c 1:3 rwm"]),
        ("groupAdd", ["0"]),
        ("pidsLimit", None),
        ("pidsLimit", -1),
        ("pidsLimit", 127),
        (
            "restartPolicy",
            {"Name": "always", "MaximumRetryCount": 0},
        ),
        ("autoRemove", True),
        ("autoRemove", 0),
        ("publishAllPorts", True),
        (
            "portBindings",
            {
                "8000/tcp": [
                    {
                        "HostIp": "0.0.0.0",
                        "HostPort": "43127",
                    }
                ]
            },
        ),
        (
            "portBindings",
            {
                "8000/tcp": [
                    {
                        "HostIp": "127.0.0.1",
                        "HostPort": "8000",
                    }
                ]
            },
        ),
        ("mounts", []),
        (
            "mounts",
            [
                {
                    "type": "volume",
                    "name": "runtime-data",
                    "destination": "/data",
                    "rw": True,
                    "driver": "local",
                },
                {
                    "type": "tmpfs",
                    "name": "",
                    "destination": "/tmp",
                    "rw": True,
                    "driver": "",
                },
            ],
        ),
    ]
    for field, bad_value in mutations:
        payload = _valid_payload()
        payload[field] = bad_value
        with pytest.raises(runtime_contract.ContainerRuntimeContractFailure):
            runtime_contract._validate_runtime_contract(
                payload,
                expected_volume="runtime-data",
                expected_port=43127,
            )

    for mount_mutation in (
        {"type": "bind"},
        {"name": "foreign-volume"},
        {"destination": "/tmp"},
        {"rw": False},
        {"driver": "foreign"},
        {"unexpected": "metadata"},
    ):
        payload = _valid_payload()
        mounts = copy.deepcopy(payload["mounts"])
        assert isinstance(mounts, list)
        mount = mounts[0]
        assert isinstance(mount, dict)
        mount.update(mount_mutation)
        payload["mounts"] = [mount]
        with pytest.raises(runtime_contract.ContainerRuntimeContractFailure):
            runtime_contract._validate_runtime_contract(
                payload,
                expected_volume="runtime-data",
                expected_port=43127,
            )


def test_validator_rejects_missing_or_extra_projected_fields(
    runtime_contract: ModuleType,
) -> None:
    for payload in (
        {key: value for key, value in _valid_payload().items() if key != "devices"},
        {**_valid_payload(), "Config.Env": ["secret"]},
    ):
        with pytest.raises(runtime_contract.ContainerRuntimeContractFailure):
            runtime_contract._validate_runtime_contract(
                payload,
                expected_volume="runtime-data",
                expected_port=43127,
            )


def test_inspector_requests_only_safe_runtime_fields_with_a_finite_deadline(
    runtime_contract: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((command, kwargs))
        output = kwargs["stdout"]
        assert hasattr(output, "write")
        assert stat.S_ISREG(os.fstat(output.fileno()).st_mode)
        output.write((json.dumps(_valid_payload()) + "\n").encode())
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runtime_contract.subprocess, "run", fake_run)

    runtime_contract.inspect_container_runtime(
        container_name="runtime-a",
        expected_volume="runtime-data",
        expected_port=43127,
    )

    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command == [
        "docker",
        "inspect",
        "--type=container",
        "--format",
        runtime_contract._RUNTIME_INSPECT_FORMAT,
        "runtime-a",
    ]
    serialized_command = json.dumps(command)
    assert "Env" not in serialized_command
    assert "Config.Env" not in serialized_command
    assert "Source" not in serialized_command
    assert kwargs["check"] is False
    assert kwargs["stdout"] is not runtime_contract.subprocess.PIPE
    assert hasattr(kwargs["stdout"], "write")
    assert kwargs["stderr"] is runtime_contract.subprocess.DEVNULL
    assert kwargs["timeout"] == runtime_contract.DOCKER_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    "container_name,volume_name,port",
    [
        ("", "runtime-data", 43127),
        ("../runtime-a", "runtime-data", 43127),
        ("runtime-a", "", 43127),
        ("runtime-a", "../runtime-data", 43127),
        ("runtime-a", "secret value", 43127),
        ("runtime-a", "runtime-data", 0),
        ("runtime-a", "runtime-data", 65_536),
        ("runtime-a", "runtime-data", True),
    ],
)
def test_inspector_rejects_unbounded_inputs_before_docker(
    runtime_contract: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    container_name: str,
    volume_name: str,
    port: int,
) -> None:
    monkeypatch.setattr(
        runtime_contract.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("Docker should not run"),
    )

    with pytest.raises(runtime_contract.ContainerRuntimeContractFailure):
        runtime_contract.inspect_container_runtime(
            container_name=container_name,
            expected_volume=volume_name,
            expected_port=port,
        )


@pytest.mark.parametrize(
    "output",
    [
        b"",
        b"{",
        b'{"Config":{"Env":["private-value-do-not-expose"]}}',
        b"\xff",
        b"x" * 32_769,
    ],
)
def test_inspector_output_failures_are_bounded_and_sanitized(
    runtime_contract: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    output: bytes,
) -> None:
    def fake_run(*_args: object, **kwargs: object) -> SimpleNamespace:
        target = kwargs["stdout"]
        assert hasattr(target, "write")
        target.write(output)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runtime_contract.subprocess, "run", fake_run)

    with pytest.raises(
        runtime_contract.ContainerRuntimeContractFailure,
        match="^Container runtime hardening proof failed$",
    ) as captured:
        runtime_contract.inspect_container_runtime(
            container_name="runtime-a",
            expected_volume="runtime-data",
            expected_port=43127,
        )
    assert "secret" not in str(captured.value).lower()


def test_inspector_rejects_nonzero_docker_without_exposing_output(
    runtime_contract: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*_args: object, **kwargs: object) -> SimpleNamespace:
        target = kwargs["stdout"]
        assert hasattr(target, "write")
        target.write(b"private-value-do-not-expose")
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(runtime_contract.subprocess, "run", fake_run)

    with pytest.raises(
        runtime_contract.ContainerRuntimeContractFailure,
        match="^Container runtime hardening proof failed$",
    ):
        runtime_contract.inspect_container_runtime(
            container_name="runtime-a",
            expected_volume="runtime-data",
            expected_port=43127,
        )


@pytest.mark.parametrize("failure", [OSError("unavailable"), "timeout"])
def test_inspector_maps_execution_failures_to_one_safe_error(
    runtime_contract: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    failure: object,
) -> None:
    def fail_run(*_args: object, **_kwargs: object) -> object:
        if failure == "timeout":
            raise runtime_contract.subprocess.TimeoutExpired(
                cmd=["docker"],
                timeout=1,
            )
        assert isinstance(failure, OSError)
        raise failure

    monkeypatch.setattr(runtime_contract.subprocess, "run", fail_run)

    with pytest.raises(
        runtime_contract.ContainerRuntimeContractFailure,
        match="^Container runtime hardening proof failed$",
    ):
        runtime_contract.inspect_container_runtime(
            container_name="runtime-a",
            expected_volume="runtime-data",
            expected_port=43127,
        )


def test_process_probe_is_fixed_secret_safe_and_deadline_bounded(
    runtime_contract: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runtime_contract.subprocess, "run", fake_run)

    runtime_contract.probe_container_process(container_name="runtime-a")

    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command == [
        "docker",
        "exec",
        "runtime-a",
        "/usr/local/bin/python",
        "-I",
        "-S",
        "-B",
        "-c",
        runtime_contract._PROCESS_PROBE_CODE,
    ]
    serialized_command = json.dumps(command)
    assert "OPENAI_API_KEY" not in serialized_command
    assert "Config.Env" not in serialized_command
    for required_probe_contract in (
        'status.get("Uid") == ["10001", "10001", "10001", "10001"]',
        'status.get("Gid") == ["10001", "10001", "10001", "10001"]',
        'status.get("NoNewPrivs") == ["1"]',
        'status.get("Seccomp") == ["2"]',
        "os.getgroups() in ([], [10001])",
        'status.get("Groups", []) == [str(group) for group in os.getgroups()]',
        '("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")',
        'os.environ.get("SQLITE_TMPDIR") == "/data"',
        "stat.S_IMODE(data_status.st_mode) == 0o700",
        'tempfile.NamedTemporaryFile(\n    dir="/data"',
        'sqlite3.connect("")',
        '"PRAGMA temp_store=FILE"',
        "2 * 1024 * 1024",
        'root_probe = "/home/backchannel/.backchannel-runtime-write-probe"',
        "error.errno == errno.EROFS",
    ):
        assert required_probe_contract in runtime_contract._PROCESS_PROBE_CODE
    assert kwargs["check"] is False
    assert kwargs["stdout"] is runtime_contract.subprocess.DEVNULL
    assert kwargs["stderr"] is runtime_contract.subprocess.DEVNULL
    assert kwargs["timeout"] == runtime_contract.DOCKER_TIMEOUT_SECONDS


def test_process_probe_rejects_nonzero_or_timeout_with_one_safe_error(
    runtime_contract: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failures = (
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1),
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            runtime_contract.subprocess.TimeoutExpired(
                cmd=["docker"],
                timeout=1,
            )
        ),
    )
    for failure in failures:
        monkeypatch.setattr(runtime_contract.subprocess, "run", failure)
        with pytest.raises(
            runtime_contract.ContainerRuntimeContractFailure,
            match="^Container runtime hardening proof failed$",
        ):
            runtime_contract.probe_container_process(container_name="runtime-a")


def test_verifier_inspects_before_running_the_kernel_process_probe(
    runtime_contract: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        runtime_contract,
        "inspect_container_runtime",
        lambda **kwargs: lifecycle.append(("inspect", kwargs)),
    )
    monkeypatch.setattr(
        runtime_contract,
        "probe_container_process",
        lambda **kwargs: lifecycle.append(("probe", kwargs)),
    )

    runtime_contract.verify_container_runtime(
        container_name="runtime-a",
        expected_volume="runtime-data",
        expected_port=43127,
    )

    assert lifecycle == [
        (
            "inspect",
            {
                "container_name": "runtime-a",
                "expected_volume": "runtime-data",
                "expected_port": 43127,
            },
        ),
        (
            "probe",
            {
                "container_name": "runtime-a",
            },
        ),
    ]
