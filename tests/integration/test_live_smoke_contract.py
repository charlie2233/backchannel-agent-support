import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts import smoke_live_three

ROOT = Path(__file__).resolve().parents[2]
RESULT_KEYS = {
    "approvalCount",
    "elapsedMs",
    "errorClass",
    "modelIds",
    "orderedToolNames",
    "status",
    "traceId",
}


def _run(script_name: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("OPENAI_API_KEY", None)
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script_name)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _parse_records(output: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in output.splitlines() if line.strip()]


def _assert_redacted_blocked_record(record: dict[str, Any]) -> None:
    assert set(record) == RESULT_KEYS
    assert record == {
        "approvalCount": 0,
        "elapsedMs": record["elapsedMs"],
        "errorClass": "MissingOpenAIAPIKey",
        "modelIds": [],
        "orderedToolNames": [],
        "status": "blocked",
        "traceId": None,
    }
    assert isinstance(record["elapsedMs"], int)
    assert record["elapsedMs"] >= 0
    serialized = json.dumps(record).lower()
    assert "prompt" not in serialized
    assert "state_json" not in serialized
    assert "traceback" not in serialized


def test_missing_key_live_smoke_emits_only_one_redacted_blocked_record() -> None:
    result = _run("smoke_live.py")

    assert result.returncode == 2
    assert result.stderr == ""
    records = _parse_records(result.stdout)
    assert len(records) == 1
    _assert_redacted_blocked_record(records[0])


def test_missing_key_live_smoke_does_not_import_runtime_stack(tmp_path: Path) -> None:
    import_blocker = tmp_path / "sitecustomize.py"
    import_blocker.write_text(
        """
import builtins

_original_import = builtins.__import__


def _block_runtime_stack(name, *args, **kwargs):
    if (
        name == "openai"
        or name.startswith("openai.")
        or name == "server"
        or name.startswith("server.")
    ):
        raise RuntimeError(f"blocked eager runtime import: {name}")
    return _original_import(name, *args, **kwargs)


builtins.__import__ = _block_runtime_stack
""".lstrip(),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.pop("OPENAI_API_KEY", None)
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(tmp_path), environment.get("PYTHONPATH")))
    )

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "smoke_live.py")],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 2
    assert result.stderr == ""
    records = _parse_records(result.stdout)
    assert len(records) == 1
    _assert_redacted_blocked_record(records[0])


def test_three_run_smoke_emits_exactly_three_independent_blocked_records() -> None:
    result = _run("smoke_live_three.py")

    assert result.returncode == 2
    assert result.stderr == ""
    records = _parse_records(result.stdout)
    assert len(records) == 3
    for record in records:
        _assert_redacted_blocked_record(record)


@pytest.mark.parametrize(
    ("status", "returncode"),
    [("passed", 1), ("failed", 0), ("blocked", 0)],
)
def test_three_run_child_rejects_status_exit_mismatch(
    monkeypatch,
    status: str,
    returncode: int,
) -> None:
    payload = {
        "approvalCount": 1 if status == "passed" else 0,
        "elapsedMs": 1,
        "errorClass": None if status == "passed" else "RuntimeError",
        "modelIds": (["gpt-5.6-luna", "gpt-5.6-terra"] if status == "passed" else []),
        "orderedToolNames": ["commit_remedy"] if status == "passed" else [],
        "status": status,
        "traceId": ("trace_0123456789abcdef0123456789abcdef" if status == "passed" else None),
    }
    monkeypatch.setattr(
        smoke_live_three.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=returncode,
            stdout=json.dumps(payload),
            stderr="",
        ),
    )

    result = smoke_live_three._run_child(ROOT / "scripts" / "smoke_live.py")

    assert result == smoke_live_three._failed_child("LiveSmokeChildProtocolError")


def test_three_run_child_never_forwards_stderr(monkeypatch) -> None:
    payload = smoke_live_three._failed_child("RedactedError")
    monkeypatch.setattr(
        smoke_live_three.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=json.dumps(payload),
            stderr="secret child exception text",
        ),
    )

    result = smoke_live_three._run_child(ROOT / "scripts" / "smoke_live.py")

    assert result == smoke_live_three._failed_child("LiveSmokeChildProtocolError")
    assert "secret" not in json.dumps(result).lower()


def test_three_run_child_rejects_malformed_record(monkeypatch) -> None:
    monkeypatch.setattr(
        smoke_live_three.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"status": "passed", "elapsedMs": "not-an-int"}),
            stderr="",
        ),
    )

    result = smoke_live_three._run_child(ROOT / "scripts" / "smoke_live.py")

    assert result == smoke_live_three._failed_child("LiveSmokeChildProtocolError")


@pytest.mark.parametrize(
    "payload",
    [
        {
            "approvalCount": 0,
            "elapsedMs": 1,
            "errorClass": None,
            "modelIds": [],
            "orderedToolNames": [],
            "status": "passed",
            "traceId": None,
        },
        {
            "approvalCount": 0,
            "elapsedMs": 1,
            "errorClass": "PromptSecretAbc123",
            "modelIds": [],
            "orderedToolNames": [],
            "status": "blocked",
            "traceId": None,
        },
    ],
)
def test_three_run_child_rejects_impossible_or_unsafe_status_evidence(
    monkeypatch,
    payload: dict[str, Any],
) -> None:
    returncode = smoke_live_three.STATUS_EXIT_CODES[payload["status"]]
    monkeypatch.setattr(
        smoke_live_three.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=returncode,
            stdout=json.dumps(payload),
            stderr="",
        ),
    )

    result = smoke_live_three._run_child(ROOT / "scripts" / "smoke_live.py")

    assert result == smoke_live_three._failed_child("LiveSmokeChildProtocolError")


def test_three_run_child_accepts_only_complete_live_pass_evidence(monkeypatch) -> None:
    payload = {
        "approvalCount": 1,
        "elapsedMs": 1,
        "errorClass": None,
        "modelIds": ["gpt-5.6-luna", "gpt-5.6-terra"],
        "orderedToolNames": ["commit_remedy"],
        "status": "passed",
        "traceId": "trace_0123456789abcdef0123456789abcdef",
    }
    monkeypatch.setattr(
        smoke_live_three.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        ),
    )

    assert smoke_live_three._run_child(ROOT / "scripts" / "smoke_live.py") == payload


def test_three_run_main_rejects_duplicate_pass_trace_ids(monkeypatch, capsys) -> None:
    passed = {
        "approvalCount": 1,
        "elapsedMs": 1,
        "errorClass": None,
        "modelIds": ["gpt-5.6-luna", "gpt-5.6-terra"],
        "orderedToolNames": ["commit_remedy"],
        "status": "passed",
        "traceId": "trace_0123456789abcdef0123456789abcdef",
    }
    monkeypatch.setattr(smoke_live_three, "_run_child", lambda _script: passed.copy())

    exit_code = smoke_live_three.main()

    assert exit_code == 1
    records = _parse_records(capsys.readouterr().out)
    assert len(records) == 3
    assert all(record["status"] == "failed" for record in records)
    assert all(record["errorClass"] == "DuplicateLiveTraceId" for record in records)
    assert all(record["traceId"] is None for record in records)


def test_three_run_main_accepts_three_distinct_complete_live_proofs(
    monkeypatch,
    capsys,
) -> None:
    results = [
        {
            "approvalCount": 1,
            "elapsedMs": 1,
            "errorClass": None,
            "modelIds": ["gpt-5.6-luna", "gpt-5.6-terra"],
            "orderedToolNames": ["commit_remedy"],
            "status": "passed",
            "traceId": f"trace_{index:032x}",
        }
        for index in range(1, 4)
    ]
    calls = 0

    def fake_child(_script: Path) -> dict[str, Any]:
        nonlocal calls
        result = results[calls]
        calls += 1
        return result

    monkeypatch.setattr(smoke_live_three, "_run_child", fake_child)

    exit_code = smoke_live_three.main()

    assert exit_code == 0
    assert calls == 3
    assert _parse_records(capsys.readouterr().out) == results


def test_three_run_failed_child_normalizes_unknown_error_class() -> None:
    result = smoke_live_three._failed_child("PromptSecretAbc123")

    assert result["errorClass"] == "LiveSmokeChildProtocolError"
    assert "PromptSecretAbc123" not in json.dumps(result)


def test_three_run_main_preserves_three_redacted_records_and_failed_exit(
    monkeypatch,
    capsys,
) -> None:
    results = [
        smoke_live_three._failed_child("FirstRedactedError"),
        smoke_live_three._failed_child("SecondRedactedError"),
        smoke_live_three._failed_child("ThirdRedactedError"),
    ]
    calls = 0

    def fake_child(_script: Path) -> dict[str, Any]:
        nonlocal calls
        result = results[calls]
        calls += 1
        return result

    monkeypatch.setattr(smoke_live_three, "_run_child", fake_child)

    exit_code = smoke_live_three.main()

    assert exit_code == 1
    assert calls == 3
    assert _parse_records(capsys.readouterr().out) == results
