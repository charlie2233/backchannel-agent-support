"""Run one bounded real-OpenAI recovery and emit only redacted proof fields."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, TypedDict

# These must be set before importing the Agents SDK through the server modules.
os.environ["OPENAI_AGENTS_DONT_LOG_MODEL_DATA"] = "1"
os.environ["OPENAI_AGENTS_DONT_LOG_TOOL_DATA"] = "1"

from openai import OpenAIError  # noqa: E402

from server.models import ApprovalDecisionRequest, ExecutionMode  # noqa: E402
from server.orchestrator import RecoveryOrchestrator  # noqa: E402
from server.providers.hotel_simulator import HotelSimulator  # noqa: E402
from server.store import SQLiteStore  # noqa: E402

logging.disable(logging.CRITICAL)


class SmokeResult(TypedDict):
    status: Literal["passed", "failed", "blocked"]
    elapsedMs: int
    modelIds: list[str]
    orderedToolNames: list[str]
    approvalCount: int
    traceId: str | None
    errorClass: str | None


BLOCKED_EXCEPTIONS = (OpenAIError,)


def _result(
    *,
    status: Literal["passed", "failed", "blocked"],
    started: float,
    model_ids: list[str] | None = None,
    ordered_tool_names: list[str] | None = None,
    approval_count: int = 0,
    trace_id: str | None = None,
    error_class: str | None = None,
) -> SmokeResult:
    return {
        "status": status,
        "elapsedMs": max(0, round((time.monotonic() - started) * 1000)),
        "modelIds": model_ids or [],
        "orderedToolNames": ordered_tool_names or [],
        "approvalCount": approval_count,
        "traceId": trace_id,
        "errorClass": error_class,
    }


async def _run_live(database_path: Path, started: float) -> SmokeResult:
    store = SQLiteStore(database_path)
    provider = HotelSimulator(store=store)
    try:
        orchestrator = RecoveryOrchestrator(
            store=store,
            hotel_provider=provider,
            live_ready=True,
        )
        pending = await orchestrator.start(
            "hotel",
            execution_mode=ExecutionMode.OPENAI_LIVE,
        )
        approval = pending.recovery.pending_approval
        if approval is None:
            raise RuntimeError("Live recovery did not reach its approval boundary")
        tool_names = [item.tool_name for item in pending.sdk_result.interruptions]
        decision = await orchestrator.approve_decision(
            pending.recovery.recovery_id,
            ApprovalDecisionRequest(
                action="approve",
                clientDecisionId="live-smoke-approval",
                remedyId=approval.remedy_id,
                remedyDigest=approval.remedy_digest,
                toolCallId=approval.tool_call_id,
            ),
        )
        receipt = store.get_receipt(pending.recovery.recovery_id)
        if (
            decision.status != "completed"
            or receipt.execution_mode is not ExecutionMode.OPENAI_LIVE
            or not receipt.model_call
            or receipt.provider_execution is not True
            or provider.dispatch_count != 1
        ):
            raise RuntimeError("Live recovery proof did not satisfy its terminal contract")
        return _result(
            status="passed",
            started=started,
            model_ids=receipt.model_ids,
            ordered_tool_names=tool_names,
            approval_count=1,
            trace_id=receipt.root_trace_id,
        )
    finally:
        store.close()


def main() -> int:
    started = time.monotonic()
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        output = _result(
            status="blocked",
            started=started,
            error_class="MissingOpenAIAPIKey",
        )
        print(json.dumps(output, separators=(",", ":"), sort_keys=True))
        return 2

    try:
        with TemporaryDirectory(prefix="backchannel-live-smoke-") as directory:
            database_path = Path(directory) / "live-smoke.sqlite3"
            output = asyncio.run(_run_live(database_path, started))
    except BLOCKED_EXCEPTIONS as error:
        output = _result(
            status="blocked",
            started=started,
            error_class=type(error).__name__,
        )
    except Exception as error:
        output = _result(
            status="failed",
            started=started,
            error_class=type(error).__name__,
        )
    print(json.dumps(output, separators=(",", ":"), sort_keys=True))
    return {"passed": 0, "failed": 1, "blocked": 2}[output["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
