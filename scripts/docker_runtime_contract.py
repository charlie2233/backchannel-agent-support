"""Validate the least-privilege runtime state of one packaged container."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from typing import Any

DOCKER_TIMEOUT_SECONDS = 30
MAX_INSPECT_OUTPUT_BYTES = 32_768
_RESOURCE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_NO_NEW_PRIVILEGES_OPTIONS = {
    "no-new-privileges",
    "no-new-privileges:true",
    "no-new-privileges=true",
}
_RUNTIME_INSPECT_FORMAT = (
    '{"user":{{json .Config.User}},'
    '"readOnlyRootfs":{{json .HostConfig.ReadonlyRootfs}},'
    '"privileged":{{json .HostConfig.Privileged}},'
    '"capAdd":{{json .HostConfig.CapAdd}},'
    '"capDrop":{{json .HostConfig.CapDrop}},'
    '"securityOpt":{{json .HostConfig.SecurityOpt}},'
    '"pidMode":{{json .HostConfig.PidMode}},'
    '"ipcMode":{{json .HostConfig.IpcMode}},'
    '"utsMode":{{json .HostConfig.UTSMode}},'
    '"cgroupnsMode":{{json .HostConfig.CgroupnsMode}},'
    '"networkMode":{{json .HostConfig.NetworkMode}},'
    '"usernsMode":{{json .HostConfig.UsernsMode}},'
    '"devices":{{json .HostConfig.Devices}},'
    '"deviceRequests":{{json .HostConfig.DeviceRequests}},'
    '"deviceCgroupRules":{{json .HostConfig.DeviceCgroupRules}},'
    '"groupAdd":{{json .HostConfig.GroupAdd}},'
    '"pidsLimit":{{json .HostConfig.PidsLimit}},'
    '"restartPolicy":{{json .HostConfig.RestartPolicy}},'
    '"autoRemove":{{json .HostConfig.AutoRemove}},'
    '"publishAllPorts":{{json .HostConfig.PublishAllPorts}},'
    '"portBindings":{{json .HostConfig.PortBindings}},'
    '"mounts":['
    "{{range $index, $mount := .Mounts}}"
    "{{if $index}},{{end}}"
    '{"type":{{json $mount.Type}},'
    '"name":{{json $mount.Name}},'
    '"destination":{{json $mount.Destination}},'
    '"rw":{{json $mount.RW}},'
    '"driver":{{json $mount.Driver}}}'
    "{{end}}]}"
)
_PROCESS_PROBE_CODE = """
import errno
import os
import sqlite3
import stat
import tempfile


def require(condition):
    if not condition:
        raise SystemExit(1)


status = {}
with open("/proc/1/status", encoding="utf-8") as handle:
    for line in handle:
        key, separator, value = line.partition(":")
        if separator:
            status[key] = value.split()

require(status.get("Uid") == ["10001", "10001", "10001", "10001"])
require(status.get("Gid") == ["10001", "10001", "10001", "10001"])
require(status.get("NoNewPrivs") == ["1"])
require(status.get("Seccomp") == ["2"])
require(os.getgroups() in ([], [10001]))
require(status.get("Groups", []) == [str(group) for group in os.getgroups()])
for capability in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"):
    require(int(status.get(capability, ["-1"])[0], 16) == 0)

data_status = os.stat("/data")
require(data_status.st_uid == 10001)
require(data_status.st_gid == 10001)
require(stat.S_IMODE(data_status.st_mode) == 0o700)
require(os.environ.get("SQLITE_TMPDIR") == "/data")
with tempfile.NamedTemporaryFile(
    dir="/data",
    prefix=".backchannel-runtime-",
    delete=True,
) as handle:
    handle.write(b"runtime-probe")
    handle.flush()
    os.fsync(handle.fileno())

temporary_database = sqlite3.connect("")
try:
    temporary_database.execute("PRAGMA temp_store=FILE")
    temporary_database.execute("PRAGMA cache_size=-64")
    temporary_database.execute("CREATE TABLE runtime_probe (payload BLOB NOT NULL)")
    temporary_database.execute(
        "INSERT INTO runtime_probe(payload) VALUES (zeroblob(?))",
        (2 * 1024 * 1024,),
    )
    temporary_database.commit()
    row = temporary_database.execute(
        "SELECT length(payload) FROM runtime_probe"
    ).fetchone()
    require(row == (2 * 1024 * 1024,))
finally:
    temporary_database.close()

root_probe = "/home/backchannel/.backchannel-runtime-write-probe"
try:
    descriptor = os.open(
        root_probe,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
except OSError as error:
    require(error.errno == errno.EROFS)
else:
    os.close(descriptor)
    os.unlink(root_probe)
    require(False)
"""


class ContainerRuntimeContractFailure(RuntimeError):
    """Coarse runtime-hardening failure safe to expose in CI."""


def _require(condition: bool) -> None:
    if not condition:
        raise ContainerRuntimeContractFailure(
            "Container runtime hardening proof failed"
        )


def _validate_resource_name(value: str) -> None:
    _require(_RESOURCE_NAME.fullmatch(value) is not None)


def _validate_expected_port(value: int) -> None:
    _require(type(value) is int and 1 <= value <= 65_535)


def _validate_runtime_contract(
    payload: object,
    *,
    expected_volume: str,
    expected_port: int,
) -> None:
    _require(isinstance(payload, dict))
    assert isinstance(payload, dict)
    _require(
        set(payload)
        == {
            "user",
            "readOnlyRootfs",
            "privileged",
            "capAdd",
            "capDrop",
            "securityOpt",
            "pidMode",
            "ipcMode",
            "utsMode",
            "cgroupnsMode",
            "networkMode",
            "usernsMode",
            "devices",
            "deviceRequests",
            "deviceCgroupRules",
            "groupAdd",
            "pidsLimit",
            "restartPolicy",
            "autoRemove",
            "publishAllPorts",
            "portBindings",
            "mounts",
        }
    )
    _require(payload["user"] == "10001:10001")
    _require(payload["readOnlyRootfs"] is True)
    _require(payload["privileged"] is False)
    _require(payload["capAdd"] in (None, []))

    cap_drop = payload["capDrop"]
    _require(
        isinstance(cap_drop, list)
        and len(cap_drop) == 1
        and isinstance(cap_drop[0], str)
        and cap_drop[0].casefold() == "all"
    )
    security_options = payload["securityOpt"]
    _require(
        isinstance(security_options, list)
        and len(security_options) == 2
        and all(isinstance(option, str) for option in security_options)
        and sum(
            option in _NO_NEW_PRIVILEGES_OPTIONS
            for option in security_options
        )
        == 1
        and security_options.count("seccomp=builtin") == 1
    )

    _require(payload["pidMode"] == "")
    _require(payload["ipcMode"] == "private")
    _require(payload["utsMode"] == "")
    _require(payload["cgroupnsMode"] == "private")
    _require(payload["networkMode"] == "bridge")
    _require(payload["usernsMode"] == "")
    _require(payload["devices"] in (None, []))
    _require(payload["deviceRequests"] in (None, []))
    _require(payload["deviceCgroupRules"] in (None, []))
    _require(payload["groupAdd"] in (None, []))
    _require(payload["pidsLimit"] == 128)
    _require(
        payload["restartPolicy"] == {"Name": "no", "MaximumRetryCount": 0}
    )
    _require(payload["autoRemove"] is False)
    _require(payload["publishAllPorts"] is False)
    _require(
        payload["portBindings"]
        == {
            "8000/tcp": [
                {
                    "HostIp": "127.0.0.1",
                    "HostPort": str(expected_port),
                }
            ]
        }
    )

    mounts = payload["mounts"]
    _require(isinstance(mounts, list) and len(mounts) == 1)
    mount = mounts[0]
    _require(
        mount
        == {
            "type": "volume",
            "name": expected_volume,
            "destination": "/data",
            "rw": True,
            "driver": "local",
        }
    )


def inspect_container_runtime(
    *,
    container_name: str,
    expected_volume: str,
    expected_port: int,
) -> None:
    _validate_resource_name(container_name)
    _validate_resource_name(expected_volume)
    _validate_expected_port(expected_port)
    try:
        with tempfile.TemporaryFile() as output:
            completed = subprocess.run(
                [
                    "docker",
                    "inspect",
                    "--type=container",
                    "--format",
                    _RUNTIME_INSPECT_FORMAT,
                    container_name,
                ],
                check=False,
                stdout=output,
                stderr=subprocess.DEVNULL,
                timeout=DOCKER_TIMEOUT_SECONDS,
            )
            _require(completed.returncode == 0)
            output.seek(0, 2)
            _require(output.tell() <= MAX_INSPECT_OUTPUT_BYTES)
            output.seek(0)
            encoded_payload = output.read(MAX_INSPECT_OUTPUT_BYTES + 1)
    except (OSError, subprocess.TimeoutExpired):
        raise ContainerRuntimeContractFailure(
            "Container runtime hardening proof failed"
        ) from None
    _require(isinstance(encoded_payload, bytes))
    _require(len(encoded_payload) <= MAX_INSPECT_OUTPUT_BYTES)
    try:
        payload: Any = json.loads(encoded_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ContainerRuntimeContractFailure(
            "Container runtime hardening proof failed"
        ) from None
    _validate_runtime_contract(
        payload,
        expected_volume=expected_volume,
        expected_port=expected_port,
    )


def probe_container_process(*, container_name: str) -> None:
    _validate_resource_name(container_name)
    try:
        completed = subprocess.run(
            [
                "docker",
                "exec",
                container_name,
                "/usr/local/bin/python",
                "-I",
                "-S",
                "-B",
                "-c",
                _PROCESS_PROBE_CODE,
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=DOCKER_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ContainerRuntimeContractFailure(
            "Container runtime hardening proof failed"
        ) from None
    _require(completed.returncode == 0)


def verify_container_runtime(
    *,
    container_name: str,
    expected_volume: str,
    expected_port: int,
) -> None:
    inspect_container_runtime(
        container_name=container_name,
        expected_volume=expected_volume,
        expected_port=expected_port,
    )
    probe_container_process(container_name=container_name)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--container", required=True)
    parser.add_argument("--volume", required=True)
    parser.add_argument("--port", required=True, type=int)
    arguments = parser.parse_args()
    try:
        verify_container_runtime(
            container_name=arguments.container,
            expected_volume=arguments.volume,
            expected_port=arguments.port,
        )
    except ContainerRuntimeContractFailure as error:
        print(
            json.dumps(
                {
                    "error": str(error),
                    "runtimeContract": "failed",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    print(
        json.dumps(
            {
                "daemonRuntimeConfig": "passed",
                "kernelProcessProbe": "passed",
                "runtimeContract": "passed",
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
