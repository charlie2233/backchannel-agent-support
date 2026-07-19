import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

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


def test_three_run_smoke_emits_exactly_three_independent_blocked_records() -> None:
    result = _run("smoke_live_three.py")

    assert result.returncode == 2
    assert result.stderr == ""
    records = _parse_records(result.stdout)
    assert len(records) == 3
    for record in records:
        _assert_redacted_blocked_record(record)
