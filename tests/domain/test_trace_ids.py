from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from server.models import (
    OPENAI_LIVE_BOUNDARY,
    SDK_STUB_BOUNDARY,
    ExecutionMode,
    RecoveryReceipt,
    RecoverySnapshot,
    ScenarioId,
)
from server.store import SQLiteStore

INVALID_QA_ROOTS = [
    "qa_trace_spoofed",
    "qa_trace_0123456789ABCDEF0123456789ABCDEF",
    "qa_trace_0123456789abcdef",
]
INVALID_LIVE_ROOTS = [
    "trace_spoofed",
    "trace_0123456789ABCDEF0123456789ABCDEF",
    "trace_0123456789abcdef",
]


def _snapshot(mode: ExecutionMode, root_trace_id: str) -> dict[str, object]:
    return {
        "recoveryId": "trace-validation-recovery",
        "scenarioId": "hotel",
        "executionMode": mode,
        "modelIds": (
            ["gpt-5.6-luna", "gpt-5.6-terra"] if mode is ExecutionMode.OPENAI_LIVE else []
        ),
        "rootTraceId": root_trace_id,
        "status": "in_progress",
        "currentStep": 0,
        "currentStepSummary": "Trace validation.",
        "createdAt": datetime.now(UTC),
        "updatedAt": datetime.now(UTC),
    }


@pytest.mark.parametrize(
    ("mode", "invalid_root"),
    [
        *[(ExecutionMode.SDK_STUB, value) for value in INVALID_QA_ROOTS],
        *[(ExecutionMode.OPENAI_LIVE, value) for value in INVALID_LIVE_ROOTS],
    ],
)
def test_snapshot_rejects_spoofed_or_malformed_trace_ids(
    mode: ExecutionMode,
    invalid_root: str,
) -> None:
    with pytest.raises(ValidationError):
        RecoverySnapshot.model_validate(_snapshot(mode, invalid_root))


@pytest.mark.parametrize(
    ("mode", "invalid_root"),
    [
        (ExecutionMode.SDK_STUB, "qa_trace_spoofed"),
        (ExecutionMode.OPENAI_LIVE, "trace_spoofed"),
    ],
)
def test_receipt_rejects_spoofed_trace_ids(
    mode: ExecutionMode,
    invalid_root: str,
) -> None:
    is_live = mode is ExecutionMode.OPENAI_LIVE
    with pytest.raises(ValidationError):
        RecoveryReceipt(
            recoveryId="trace-validation-recovery",
            executionMode=mode,
            status="completed",
            simulated=True,
            providerExecution=True,
            modelCall=is_live,
            modelIds=(["gpt-5.6-luna", "gpt-5.6-terra"] if is_live else []),
            rootTraceId=invalid_root,
            sdkVersion="0.18.3",
            protocolVersion="backchannel.approval.v1",
            agentGraphVersion="graph-v1",
            definitionDigest="a" * 64,
            boundary=OPENAI_LIVE_BOUNDARY if is_live else SDK_STUB_BOUNDARY,
            providerResult="Demo provider result.",
            authorizationSource="Exact approval.",
            verificationResults=["Verified."],
            approvedRemedyDigest=f"sha256:{'b' * 64}",
        )


@pytest.mark.parametrize(
    ("mode", "invalid_root"),
    [
        (ExecutionMode.SDK_STUB, "qa_trace_spoofed"),
        (ExecutionMode.OPENAI_LIVE, "trace_spoofed"),
    ],
)
def test_store_rejects_spoofed_trace_ids(
    tmp_path,
    mode: ExecutionMode,
    invalid_root: str,
) -> None:
    store = SQLiteStore(tmp_path / f"{mode.value}.sqlite3")
    live = mode is ExecutionMode.OPENAI_LIVE
    with pytest.raises(ValueError, match="trace provenance"):
        store.create_recovery(
            recovery_id=f"recovery-{mode.value}",
            scenario_id=ScenarioId.HOTEL,
            execution_mode=mode,
            current_step=0,
            current_step_summary="Trace validation.",
            model_ids=(["gpt-5.6-luna", "gpt-5.6-terra"] if live else []),
            root_trace_id=invalid_root,
            model_call=live,
            sdk_version="0.18.3",
            protocol_version="backchannel.approval.v1",
            agent_graph_version="graph-v1",
            definition_digest="a" * 64,
        )
