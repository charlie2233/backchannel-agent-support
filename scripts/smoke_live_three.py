"""Run the redacted live smoke in exactly three independent child processes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

RESULT_KEYS = {
    "approvalCount",
    "elapsedMs",
    "errorClass",
    "modelIds",
    "orderedToolNames",
    "status",
    "traceId",
}


def _failed_child(error_class: str) -> dict[str, Any]:
    return {
        "status": "failed",
        "elapsedMs": 0,
        "modelIds": [],
        "orderedToolNames": [],
        "approvalCount": 0,
        "traceId": None,
        "errorClass": error_class,
    }


def _run_child(script: Path) -> dict[str, Any]:
    environment = os.environ.copy()
    environment["OPENAI_AGENTS_DONT_LOG_MODEL_DATA"] = "1"
    environment["OPENAI_AGENTS_DONT_LOG_TOOL_DATA"] = "1"
    try:
        completed = subprocess.run(
            [sys.executable, str(script)],
            cwd=script.parent.parent,
            env=environment,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _failed_child("LiveSmokeTimeout")
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        return _failed_child("LiveSmokeChildProtocolError")
    try:
        parsed = json.loads(lines[0])
    except (TypeError, ValueError):
        return _failed_child("LiveSmokeChildProtocolError")
    if not isinstance(parsed, dict) or set(parsed) != RESULT_KEYS:
        return _failed_child("LiveSmokeChildProtocolError")
    return parsed


def main() -> int:
    child_script = Path(__file__).with_name("smoke_live.py").resolve()
    results = [_run_child(child_script) for _ in range(3)]
    for result in results:
        print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    statuses = {result["status"] for result in results}
    if statuses == {"passed"}:
        return 0
    if "failed" in statuses:
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
