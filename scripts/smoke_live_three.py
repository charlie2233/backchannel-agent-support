"""Run the redacted live smoke in exactly three independent child processes."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from server.trace_ids import is_valid_live_trace_id

RESULT_KEYS = {
    "approvalCount",
    "elapsedMs",
    "errorClass",
    "modelIds",
    "orderedToolNames",
    "status",
    "traceId",
}
STATUS_EXIT_CODES = {"passed": 0, "failed": 1, "blocked": 2}
LIVE_MODEL_IDS = ["gpt-5.6-luna", "gpt-5.6-terra"]
LIVE_TOOL_NAMES = ["commit_remedy"]
SAFE_ERROR_CLASS = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,127}\Z")


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


def _is_valid_result(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != RESULT_KEYS:
        return False
    status = value.get("status")
    elapsed_ms = value.get("elapsedMs")
    approval_count = value.get("approvalCount")
    model_ids = value.get("modelIds")
    tool_names = value.get("orderedToolNames")
    trace_id = value.get("traceId")
    error_class = value.get("errorClass")
    common_valid = (
        isinstance(status, str)
        and status in STATUS_EXIT_CODES
        and isinstance(elapsed_ms, int)
        and not isinstance(elapsed_ms, bool)
        and elapsed_ms >= 0
        and isinstance(approval_count, int)
        and not isinstance(approval_count, bool)
        and approval_count >= 0
        and isinstance(model_ids, list)
        and all(isinstance(item, str) for item in model_ids)
        and isinstance(tool_names, list)
        and all(isinstance(item, str) for item in tool_names)
        and (trace_id is None or isinstance(trace_id, str))
        and (error_class is None or isinstance(error_class, str))
    )
    if not common_valid:
        return False
    if status == "passed":
        return (
            approval_count == 1
            and model_ids == LIVE_MODEL_IDS
            and tool_names == LIVE_TOOL_NAMES
            and isinstance(trace_id, str)
            and is_valid_live_trace_id(trace_id)
            and error_class is None
        )
    return (
        approval_count == 0
        and model_ids == []
        and tool_names == []
        and trace_id is None
        and isinstance(error_class, str)
        and SAFE_ERROR_CLASS.fullmatch(error_class) is not None
    )


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
    if not _is_valid_result(parsed):
        return _failed_child("LiveSmokeChildProtocolError")
    status = parsed["status"]
    if completed.stderr or completed.returncode != STATUS_EXIT_CODES[status]:
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
