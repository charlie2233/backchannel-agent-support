from __future__ import annotations

import signal
import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts import docker_smoke

ROOT = Path(__file__).resolve().parents[2]


class FakeLocalProcess:
    def __init__(
        self,
        *,
        graceful_exit_code: int = 0,
        graceful_timeout: bool = False,
        forced_timeout: bool = False,
        already_exited_code: int | None = None,
        terminate_error: bool = False,
        kill_error: bool = False,
        poll_error: bool = False,
        graceful_wait_error: bool = False,
        forced_wait_error: bool = False,
    ) -> None:
        self.pid = 43_190
        self.graceful_exit_code = graceful_exit_code
        self.graceful_timeout = graceful_timeout
        self.forced_timeout = forced_timeout
        self.already_exited_code = already_exited_code
        self.terminate_error = terminate_error
        self.kill_error = kill_error
        self.poll_error = poll_error
        self.graceful_wait_error = graceful_wait_error
        self.forced_wait_error = forced_wait_error
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_timeouts: list[float] = []
        self.stderr_file: Any | None = None
        self.clean_shutdown_log = True

    def poll(self) -> int | None:
        if self.poll_error:
            raise OSError("raw poll detail /private/process")
        return self.already_exited_code

    def terminate(self) -> None:
        self.terminate_calls += 1
        if self.terminate_error:
            raise ProcessLookupError("raw terminate detail")
        if self.clean_shutdown_log:
            assert self.stderr_file is not None
            _write_clean_shutdown_log(self, self.stderr_file)

    def kill(self) -> None:
        self.kill_calls += 1
        if self.kill_error:
            raise ProcessLookupError("raw kill detail")

    def wait(self, *, timeout: float) -> int:
        self.wait_timeouts.append(timeout)
        if self.graceful_wait_error and self.kill_calls == 0:
            raise OSError("raw graceful wait detail")
        if self.graceful_timeout and self.kill_calls == 0:
            raise subprocess.TimeoutExpired("scripts/start.py", timeout)
        if self.forced_wait_error and self.kill_calls > 0:
            raise OSError("raw forced wait detail")
        if self.forced_timeout and self.kill_calls > 0:
            raise subprocess.TimeoutExpired("scripts/start.py", timeout)
        return -9 if self.kill_calls else self.graceful_exit_code


def _install_local_smoke_fakes(
    monkeypatch: pytest.MonkeyPatch,
    process: FakeLocalProcess,
    *,
    clean_shutdown_log: bool = True,
    stale_shutdown_log: bool = False,
    log_canary: bool = False,
    http_failure: BaseException | None = None,
) -> None:
    monkeypatch.setattr(docker_smoke, "_available_port", lambda: 43190)
    monkeypatch.setattr(docker_smoke, "_wait_for_server", lambda *_args: None)
    monkeypatch.setattr(
        docker_smoke.secrets,
        "token_urlsafe",
        lambda _size: "unit-canary",
    )

    def fake_http_smoke(*_args: object) -> dict[str, object]:
        if http_failure is not None:
            raise http_failure
        return {
            "health": "passed",
            "readiness": "passed",
            "responseCanarySecretAbsent": True,
        }

    monkeypatch.setattr(
        docker_smoke,
        "run_http_smoke",
        fake_http_smoke,
    )
    monkeypatch.setattr(
        docker_smoke.subprocess,
        "Popen",
        lambda *_args, **kwargs: _attach_local_process_log(
            process,
            kwargs["stderr"],
            clean_shutdown_log=clean_shutdown_log,
            stale_shutdown_log=stale_shutdown_log,
            log_canary=log_canary,
        ),
    )


def _attach_local_process_log(
    process: FakeLocalProcess,
    stderr_file: Any,
    *,
    clean_shutdown_log: bool,
    stale_shutdown_log: bool,
    log_canary: bool,
) -> FakeLocalProcess:
    process.stderr_file = stderr_file
    process.clean_shutdown_log = clean_shutdown_log
    if stale_shutdown_log:
        _write_clean_shutdown_log(process, stderr_file)
    if log_canary:
        stderr_file.write(b"sk-smoke-unit-canary\n")
        stderr_file.flush()
    return process


def _write_clean_shutdown_log(
    process: FakeLocalProcess,
    stderr_file: Any,
) -> None:
    stderr_file.write(_clean_shutdown_log(process.pid))
    stderr_file.flush()


def _clean_shutdown_log(process_id: int) -> bytes:
    return (
        "INFO:     Shutting down\n"
        "INFO:     Waiting for application shutdown.\n"
        "INFO:     Application shutdown complete.\n"
        f"INFO:     Finished server process [{process_id}]\n"
    ).encode("ascii")


@pytest.mark.parametrize("exit_code", [0, -signal.SIGTERM])
def test_local_production_smoke_requires_clean_graceful_exit(
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int,
) -> None:
    process = FakeLocalProcess(graceful_exit_code=exit_code)
    _install_local_smoke_fakes(monkeypatch, process)

    result = docker_smoke.run_local_single_process_smoke()

    assert result["proofLane"] == "local_single_process_production"
    assert result["cleanShutdown"] == "passed"
    assert process.terminate_calls == 1
    assert process.kill_calls == 0
    assert process.wait_timeouts == [5]


def test_local_production_smoke_rejects_forced_process_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeLocalProcess(graceful_timeout=True)
    _install_local_smoke_fakes(monkeypatch, process)

    with pytest.raises(
        docker_smoke.SmokeFailure,
        match="did not exit cleanly after SIGTERM",
    ):
        docker_smoke.run_local_single_process_smoke()

    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.wait_timeouts == [5, 5]


def test_local_production_smoke_rejects_nonzero_graceful_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeLocalProcess(graceful_exit_code=3)
    _install_local_smoke_fakes(monkeypatch, process)

    with pytest.raises(
        docker_smoke.SmokeFailure,
        match="did not exit cleanly after SIGTERM",
    ):
        docker_smoke.run_local_single_process_smoke()

    assert process.terminate_calls == 1
    assert process.kill_calls == 0


def test_local_production_smoke_rejects_missing_shutdown_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeLocalProcess()
    _install_local_smoke_fakes(
        monkeypatch,
        process,
        clean_shutdown_log=False,
    )

    with pytest.raises(
        docker_smoke.SmokeFailure,
        match="did not emit complete graceful shutdown evidence",
    ):
        docker_smoke.run_local_single_process_smoke()

    assert process.terminate_calls == 1
    assert process.kill_calls == 0


@pytest.mark.parametrize(
    "stderr_log",
    (
        (
            b"INFO:     Not Shutting down yet\n"
            b"INFO:     Waiting for application shutdown.\n"
            b"INFO:     Application shutdown complete.\n"
            b"INFO:     Finished server process [43190]\n"
        ),
        (
            b"INFO:     Shutting down\n"
            b"INFO:     Waiting for application shutdown.\n"
            b"INFO:     Application shutdown complete.\n"
            b"INFO:     Finished server process [43190] trailing-garbage\n"
        ),
        _clean_shutdown_log(43_191),
        (
            b"INFO:     Waiting for application shutdown.\n"
            b"INFO:     Shutting down\n"
            b"INFO:     Application shutdown complete.\n"
            b"INFO:     Finished server process [43190]\n"
        ),
    ),
    ids=("prefix", "suffix", "wrong-pid", "wrong-order"),
)
def test_local_shutdown_evidence_rejects_noncanonical_lines(
    stderr_log: bytes,
) -> None:
    with pytest.raises(
        docker_smoke.SmokeFailure,
        match="did not emit complete graceful shutdown evidence",
    ):
        docker_smoke._require_clean_local_shutdown_evidence(
            process_id=43_190,
            exit_code=-signal.SIGTERM,
            stderr_log=stderr_log,
        )


def test_local_production_smoke_ignores_stale_pre_sigterm_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeLocalProcess()
    _install_local_smoke_fakes(
        monkeypatch,
        process,
        clean_shutdown_log=False,
        stale_shutdown_log=True,
    )

    with pytest.raises(
        docker_smoke.SmokeFailure,
        match="did not emit complete graceful shutdown evidence",
    ):
        docker_smoke.run_local_single_process_smoke()

    assert process.terminate_calls == 1
    assert process.kill_calls == 0


def test_local_production_smoke_rejects_process_that_exited_before_teardown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeLocalProcess(already_exited_code=0)
    _install_local_smoke_fakes(monkeypatch, process)

    with pytest.raises(
        docker_smoke.SmokeFailure,
        match="exited before the smoke requested graceful shutdown",
    ):
        docker_smoke.run_local_single_process_smoke()

    assert process.terminate_calls == 0
    assert process.kill_calls == 0


def test_local_production_smoke_bounds_forced_termination_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeLocalProcess(
        graceful_timeout=True,
        forced_timeout=True,
    )
    _install_local_smoke_fakes(monkeypatch, process)

    with pytest.raises(
        docker_smoke.SmokeFailure,
        match="remained alive after bounded forced termination",
    ):
        docker_smoke.run_local_single_process_smoke()

    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.wait_timeouts == [5, 5]


def test_local_production_smoke_preserves_primary_failure_and_scans_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeLocalProcess(graceful_timeout=True)
    primary = docker_smoke.SmokeFailure("Primary HTTP proof failed.")
    _install_local_smoke_fakes(
        monkeypatch,
        process,
        log_canary=True,
        http_failure=primary,
    )

    with pytest.raises(docker_smoke.SmokeFailure) as captured:
        docker_smoke.run_local_single_process_smoke()

    message = str(captured.value)
    assert message.startswith("Primary HTTP proof failed.")
    assert "A canary secret appeared in production server logs" in message
    assert "did not exit cleanly after SIGTERM" in message
    assert "unit-canary" not in message
    assert process.terminate_calls == 1
    assert process.kill_calls == 1


def test_local_production_smoke_sanitizes_unexpected_primary_and_keeps_secondary_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeLocalProcess(graceful_timeout=True)
    _install_local_smoke_fakes(
        monkeypatch,
        process,
        log_canary=True,
        http_failure=RuntimeError(
            "raw unexpected unit-canary /private/database.sqlite3"
        ),
    )

    with pytest.raises(docker_smoke.SmokeFailure) as captured:
        docker_smoke.run_local_single_process_smoke()

    message = str(captured.value)
    assert message.startswith("Production smoke encountered an unexpected local error")
    assert "A canary secret appeared in production server logs" in message
    assert "did not exit cleanly after SIGTERM" in message
    assert "raw unexpected" not in message
    assert "unit-canary" not in message
    assert "/private/" not in message
    assert process.terminate_calls == 1
    assert process.kill_calls == 1


def test_local_production_smoke_converts_successful_system_exit_to_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeLocalProcess(graceful_timeout=True)
    _install_local_smoke_fakes(
        monkeypatch,
        process,
        log_canary=True,
        http_failure=SystemExit(0),
    )

    with pytest.raises(docker_smoke.SmokeFailure) as captured:
        docker_smoke.run_local_single_process_smoke()

    message = str(captured.value)
    assert message.startswith("Production smoke encountered an unexpected local error")
    assert "A canary secret appeared in production server logs" in message
    assert "did not exit cleanly after SIGTERM" in message
    assert "unit-canary" not in message
    assert process.terminate_calls == 1
    assert process.kill_calls == 1


def test_local_production_smoke_sanitizes_poll_failure_and_still_terminates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeLocalProcess(poll_error=True)
    _install_local_smoke_fakes(monkeypatch, process)

    with pytest.raises(
        docker_smoke.SmokeFailure,
        match="^Production server pre-shutdown status could not be confirmed$",
    ) as captured:
        docker_smoke.run_local_single_process_smoke()

    assert "raw poll" not in str(captured.value)
    assert process.terminate_calls == 1
    assert process.kill_calls == 0
    assert process.wait_timeouts == [5]


def test_local_production_smoke_still_terminates_when_log_boundary_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeLocalProcess()
    _install_local_smoke_fakes(monkeypatch, process)
    original_stat = Path.stat
    injected = False

    def fail_first_stderr_stat(path: Path, *args: Any, **kwargs: Any) -> Any:
        nonlocal injected
        if path.name == "server.stderr.log" and not injected:
            injected = True
            raise OSError("raw stat detail /private/log")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fail_first_stderr_stat)

    with pytest.raises(
        docker_smoke.SmokeFailure,
        match="^Production server shutdown log boundary could not be recorded$",
    ) as captured:
        docker_smoke.run_local_single_process_smoke()

    assert "raw stat" not in str(captured.value)
    assert process.terminate_calls == 1
    assert process.kill_calls == 0


@pytest.mark.parametrize(
    ("log_name", "expected_message"),
    (
        ("server.stdout.log", "Production server stdout log could not be read"),
        ("server.stderr.log", "Production server stderr log could not be read"),
    ),
    ids=("stdout", "stderr"),
)
def test_local_production_smoke_sanitizes_log_read_failures_after_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    log_name: str,
    expected_message: str,
) -> None:
    process = FakeLocalProcess()
    _install_local_smoke_fakes(monkeypatch, process)
    original_read_bytes = Path.read_bytes

    def fail_selected_log_read(path: Path) -> bytes:
        if path.name == log_name:
            raise OSError("raw log read detail /private/log")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fail_selected_log_read)

    with pytest.raises(
        docker_smoke.SmokeFailure,
        match=f"^{expected_message}$",
    ) as captured:
        docker_smoke.run_local_single_process_smoke()

    assert "raw log" not in str(captured.value)
    assert process.terminate_calls == 1
    assert process.kill_calls == 0


@pytest.mark.parametrize(
    ("process_kwargs", "expected_message"),
    (
        (
            {"terminate_error": True},
            "Production server could not receive bounded SIGTERM",
        ),
        (
            {"graceful_wait_error": True},
            "Production server graceful shutdown status could not be confirmed",
        ),
        (
            {"graceful_timeout": True, "kill_error": True},
            "Production server forced cleanup could not be started",
        ),
        (
            {"graceful_timeout": True, "forced_wait_error": True},
            "Production server forced cleanup could not be confirmed",
        ),
    ),
    ids=("terminate", "wait", "kill", "kill-wait"),
)
def test_local_production_smoke_sanitizes_process_control_errors(
    monkeypatch: pytest.MonkeyPatch,
    process_kwargs: dict[str, bool],
    expected_message: str,
) -> None:
    process = FakeLocalProcess(**process_kwargs)
    _install_local_smoke_fakes(monkeypatch, process)

    with pytest.raises(docker_smoke.SmokeFailure) as captured:
        docker_smoke.run_local_single_process_smoke()

    message = str(captured.value)
    assert expected_message in message
    if process_kwargs.get("graceful_timeout"):
        assert message.startswith(
            "Production server did not exit cleanly after SIGTERM"
        )
    else:
        assert message == expected_message
    assert "raw " not in message
    if process_kwargs.get("terminate_error") or process_kwargs.get(
        "graceful_wait_error"
    ):
        assert process.kill_calls == 1


def test_local_production_smoke_sanitizes_process_start_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(docker_smoke, "_available_port", lambda: 43190)
    monkeypatch.setattr(
        docker_smoke.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("raw start failure detail")
        ),
    )

    with pytest.raises(
        docker_smoke.SmokeFailure,
        match="^Production server process could not be started$",
    ) as captured:
        docker_smoke.run_local_single_process_smoke()

    assert "raw " not in str(captured.value)


def test_validation_docs_keep_local_shutdown_proof_bounded() -> None:
    validation = (ROOT / "docs" / "validation.md").read_text(encoding="utf-8")

    assert "`cleanShutdown=passed`" in validation
    assert "forced termination is cleanup only and fails the smoke" in validation
    assert "not container-orchestrator or" in validation
    assert "public-deployment proof" in validation
