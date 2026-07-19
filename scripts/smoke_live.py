"""Redacted real-OpenAI smoke proof for one live hotel recovery."""

# ruff: noqa: I001 -- server must set fail-closed environment before Agents imports.

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Any, Literal

# Import the package before Agents so its fail-closed logging environment is applied.
import server  # noqa: F401
from agents import flush_traces
from openai import AsyncOpenAI

from server.agents.live_models import (
    LIVE_MODEL_ERROR_CODES,
    LiveModelRequestError,
    ResponseMetadataRecorder,
    SafeOpenAIResponsesProvider,
)
from server.config import RuntimeSettings
from server.models import ApprovalDecisionRequest, ExecutionMode, RecoveryStatus
from server.orchestrator import RecoveryOrchestrator
from server.providers.hotel_simulator import HotelSimulator
from server.store import SQLiteStore

_SAFE_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,191}\Z")
_SAFE_SMOKE_ERROR_CODES = frozenset(
    {
        "missing_openai_api_key",
        "unsafe_tool_name",
        "unexpected_approval_count",
        "unexpected_interruption_tool",
        "missing_pending_approval",
        "missing_public_approval",
        "approval_already_started",
        "preconsent_dispatch_detected",
        "approval_not_completed",
        "execution_not_started",
        "receipt_mode_mismatch",
        "demo_boundary_missing",
        "provider_execution_missing",
        "execution_count_mismatch",
        "receipt_decision_mismatch",
        "permission_not_revoked",
        "scope_not_closed",
        "dispatch_count_mismatch",
        "returned_model_ids_missing",
        "returned_model_ids_mismatch",
        "root_trace_id_missing",
        "persisted_root_trace_id_mismatch",
        "root_trace_id_mismatch",
    }
)


class LiveSmokeError(RuntimeError):
    """A stable smoke invariant code whose message contains no external payload."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class LiveSmokeResult:
    smoke: Literal["passed", "failed"]
    elapsed_ms: int
    model_ids: tuple[str, ...]
    ordered_tool_names: tuple[str, ...]
    approval_count: int
    root_trace_id: str | None
    error_class: str | None
    error_code: str | None

    def public_json(self) -> dict[str, object]:
        """Return the complete output allowlist for this smoke run."""

        return {
            "smoke": self.smoke,
            "elapsedMs": self.elapsed_ms,
            "modelIds": list(self.model_ids),
            "orderedToolNames": list(self.ordered_tool_names),
            "approvalCount": self.approval_count,
            "rootTraceId": self.root_trace_id,
            "errorClass": self.error_class,
            "errorCode": self.error_code,
        }


@dataclass(slots=True)
class _Observation:
    recorder: ResponseMetadataRecorder | None = None
    trace_ids: tuple[str, ...] = ()

    def record_trace(self, trace_id: str) -> None:
        self.trace_ids = (*self.trace_ids, trace_id)

    def model_ids(self) -> tuple[str, ...]:
        if self.recorder is None:
            return ()
        return self.recorder.public_model_ids()


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise LiveSmokeError(code)


def _safe_error(error: Exception) -> tuple[str, str]:
    if isinstance(error, LiveSmokeError):
        code = error.code if error.code in _SAFE_SMOKE_ERROR_CODES else "external_error"
        return "LiveSmokeError", code
    if isinstance(error, LiveModelRequestError):
        code = error.code if error.code in LIVE_MODEL_ERROR_CODES else "external_error"
        return "LiveModelRequestError", code
    if isinstance(error, AssertionError):
        return "AssertionError", "smoke_invariant_failed"
    return "ExternalError", "external_error"


async def run_live_smoke(run_number: int = 1) -> LiveSmokeResult:
    """Run one approved live workflow with a fresh database and redacted evidence."""

    started_at = perf_counter()
    observation = _Observation()
    ordered_tool_names: tuple[str, ...] = ()
    approval_count = 0
    root_trace_id: str | None = None
    error: Exception | None = None
    temporary_directory: TemporaryDirectory[str] | None = None
    store: SQLiteStore | None = None
    client: AsyncOpenAI | None = None

    try:
        if not RuntimeSettings.from_environment().live_ready:
            raise LiveSmokeError("missing_openai_api_key")

        temporary_directory = TemporaryDirectory(prefix="backchannel-live-smoke-")
        store = SQLiteStore(Path(temporary_directory.name) / "live.sqlite3")
        hotel_provider = HotelSimulator(store=store)
        client = AsyncOpenAI()

        def provider_factory(
            recorder: ResponseMetadataRecorder,
        ) -> SafeOpenAIResponsesProvider:
            observation.recorder = recorder
            return SafeOpenAIResponsesProvider(client=client, recorder=recorder)

        def trace_factory(
            *, trace_id: str, group_id: str
        ) -> AbstractContextManager[Any]:
            observation.record_trace(trace_id)
            return RecoveryOrchestrator._openai_trace(
                trace_id=trace_id,
                group_id=group_id,
            )

        orchestrator = RecoveryOrchestrator(
            store=store,
            hotel_provider=hotel_provider,
            live_ready=True,
            live_model_provider_factory=provider_factory,
            live_trace_factory=trace_factory,
        )
        pending = await orchestrator.start(
            "hotel",
            execution_mode=ExecutionMode.OPENAI_LIVE,
        )
        root_trace_id = pending.recovery.root_trace_id
        pending_root_trace_id = root_trace_id
        interruptions = pending.sdk_result.interruptions
        names = tuple(interruption.tool_name for interruption in interruptions)
        _require(
            all(_SAFE_LABEL.fullmatch(name) is not None for name in names),
            "unsafe_tool_name",
        )
        ordered_tool_names = names
        approval_count = len(interruptions)
        _require(approval_count == 1, "unexpected_approval_count")
        _require(
            ordered_tool_names == ("commit_remedy",),
            "unexpected_interruption_tool",
        )
        _require(
            pending.recovery.status is RecoveryStatus.PENDING_APPROVAL,
            "missing_pending_approval",
        )
        approval = pending.recovery.pending_approval
        _require(approval is not None, "missing_public_approval")
        if approval is None:
            raise LiveSmokeError("missing_public_approval")
        _require(approval.execution_started is False, "approval_already_started")
        _require(hotel_provider.dispatch_count == 0, "preconsent_dispatch_detected")

        decision = ApprovalDecisionRequest(
            decision="approve",
            clientDecisionId=f"live-smoke-approval-{run_number}",
            remedyId=approval.remedy_id,
            remedyDigest=approval.remedy_digest,
            toolCallId=approval.tool_call_id,
        )
        decision_result = await orchestrator.decide(
            pending.recovery.recovery_id,
            decision,
        )
        _require(decision_result.status == "completed", "approval_not_completed")
        _require(decision_result.execution_started is True, "execution_not_started")
        receipt = store.get_receipt(pending.recovery.recovery_id)
        root_trace_id = receipt.root_trace_id
        _require(
            receipt.execution_mode is ExecutionMode.OPENAI_LIVE,
            "receipt_mode_mismatch",
        )
        _require(receipt.simulated, "demo_boundary_missing")
        _require(receipt.provider_execution, "provider_execution_missing")
        _require(receipt.execution_count == 1, "execution_count_mismatch")
        _require(receipt.decision == "approved", "receipt_decision_mismatch")
        _require(receipt.permission_revoked, "permission_not_revoked")
        _require(receipt.scope_closed, "scope_not_closed")
        _require(hotel_provider.dispatch_count == 1, "dispatch_count_mismatch")
        _require(bool(receipt.model_ids), "returned_model_ids_missing")
        _require(
            receipt.model_ids == list(observation.model_ids()),
            "returned_model_ids_mismatch",
        )
        _require(root_trace_id is not None, "root_trace_id_missing")
        _require(
            root_trace_id == pending_root_trace_id,
            "persisted_root_trace_id_mismatch",
        )
        _require(
            bool(observation.trace_ids)
            and set(observation.trace_ids) == {root_trace_id},
            "root_trace_id_mismatch",
        )
    except Exception as caught:
        error = caught
    finally:
        try:
            flush_traces()
        except Exception as caught:
            if error is None:
                error = caught
        if client is not None:
            try:
                await client.close()
            except Exception as caught:
                if error is None:
                    error = caught
        if store is not None:
            try:
                store.close()
            except Exception as caught:
                if error is None:
                    error = caught
        if temporary_directory is not None:
            try:
                temporary_directory.cleanup()
            except Exception as caught:
                if error is None:
                    error = caught

    elapsed_ms = max(0, round((perf_counter() - started_at) * 1000))
    model_ids = observation.model_ids()
    if root_trace_id is None and observation.trace_ids:
        root_trace_id = observation.trace_ids[0]
    if error is not None:
        error_class, error_code = _safe_error(error)
        return LiveSmokeResult(
            smoke="failed",
            elapsed_ms=elapsed_ms,
            model_ids=model_ids,
            ordered_tool_names=ordered_tool_names,
            approval_count=approval_count,
            root_trace_id=root_trace_id,
            error_class=error_class,
            error_code=error_code,
        )
    return LiveSmokeResult(
        smoke="passed",
        elapsed_ms=elapsed_ms,
        model_ids=model_ids,
        ordered_tool_names=ordered_tool_names,
        approval_count=approval_count,
        root_trace_id=root_trace_id,
        error_class=None,
        error_code=None,
    )


def _configure_quiet_output() -> None:
    # The sole process output is the allowlisted result below. Payload logging is also
    # disabled unconditionally by server.__init__ before Agents imports.
    logging.disable(logging.CRITICAL)


def main() -> int:
    _configure_quiet_output()
    result = asyncio.run(run_live_smoke())
    print(json.dumps(result.public_json(), separators=(",", ":"), sort_keys=True))
    return 0 if result.smoke == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
