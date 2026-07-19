import pytest
from pydantic import ValidationError

from server.models import ExecutionMode, RecoveryReceipt

DECISION_DIGEST = f"sha256:{'a' * 64}"
OTHER_DIGEST = f"sha256:{'b' * 64}"


def receipt_payload(execution_mode: ExecutionMode) -> dict[str, object]:
    payload: dict[str, object] = {
        "recoveryId": "recovery-123",
        "executionMode": execution_mode,
        "status": "completed",
        "simulated": True,
        "providerExecution": execution_mode is ExecutionMode.SDK_STUB,
        "modelIds": [],
        "boundary": "Mode-specific test boundary.",
        "providerResult": "Test result.",
        "authorizationSource": "Test authorization.",
        "verificationResults": ["Test verification."],
    }
    if execution_mode is ExecutionMode.SDK_STUB:
        payload.update(
            {
                "decision": "approved",
                "decisionRemedyDigest": DECISION_DIGEST,
                "executionCount": 1,
                "providerDispatchStarted": True,
                "exactInterruptionRejected": False,
                "permissionRevoked": True,
                "scopeClosed": True,
                "approvedRemedyDigest": DECISION_DIGEST,
            }
        )
    return payload


@pytest.mark.parametrize(
    "invalid_update",
    [
        {"status": "closed_without_action"},
        {"simulated": False},
        {"providerExecution": True},
        {"modelIds": ["impossible-replay-model"]},
        {"rootTraceId": "trace_11111111111111111111111111111111"},
        {"sdkVersion": "0.18.3"},
        {"protocolVersion": "backchannel.approval.v1"},
        {"agentGraphVersion": "backchannel.hotel-agent.v1"},
        {"promptToolSchemaHash": "b" * 64},
        {"decision": "approved"},
        {"decisionRemedyDigest": DECISION_DIGEST},
        {"executionCount": 1},
        {"providerDispatchStarted": True},
        {"exactInterruptionRejected": True},
        {"permissionRevoked": True},
        {"scopeClosed": True},
        {"approvedRemedyDigest": DECISION_DIGEST},
    ],
)
def test_replay_receipt_rejects_impossible_provenance(
    invalid_update: dict[str, object],
) -> None:
    payload = receipt_payload(ExecutionMode.REPLAY_FIXTURE)
    payload.update(invalid_update)

    with pytest.raises(ValidationError):
        RecoveryReceipt.model_validate(payload)


@pytest.mark.parametrize(
    "invalid_update",
    [
        {"simulated": False},
        {"modelIds": ["impossible-stub-model"]},
        {"providerExecution": False},
        {"executionCount": 0},
        {"providerDispatchStarted": False},
        {"exactInterruptionRejected": True},
        {"permissionRevoked": False},
        {"scopeClosed": False},
        {"approvedRemedyDigest": OTHER_DIGEST},
    ],
)
def test_sdk_stub_receipt_rejects_impossible_provenance(
    invalid_update: dict[str, object],
) -> None:
    payload = receipt_payload(ExecutionMode.SDK_STUB)
    payload.update(invalid_update)

    with pytest.raises(ValidationError):
        RecoveryReceipt.model_validate(payload)


def cancellation_payload() -> dict[str, object]:
    payload = receipt_payload(ExecutionMode.SDK_STUB)
    payload.update(
        {
            "status": "closed_without_action",
            "providerExecution": False,
            "providerResult": "Provider dispatch did not begin.",
            "decision": "declined",
            "executionCount": 0,
            "providerDispatchStarted": False,
            "exactInterruptionRejected": True,
        }
    )
    payload.pop("approvedRemedyDigest")
    return payload


@pytest.mark.parametrize(
    "invalid_update",
    [
        {"providerExecution": True},
        {"executionCount": 1},
        {"providerDispatchStarted": True},
        {"exactInterruptionRejected": False},
        {"permissionRevoked": False},
        {"scopeClosed": False},
        {"approvedRemedyDigest": DECISION_DIGEST},
    ],
)
def test_cancellation_receipt_rejects_impossible_execution_or_scope_evidence(
    invalid_update: dict[str, object],
) -> None:
    payload = cancellation_payload()
    payload.update(invalid_update)

    with pytest.raises(ValidationError):
        RecoveryReceipt.model_validate(payload)


@pytest.mark.parametrize(
    "invalid_update",
    [
        {"executionCount": 0},
        {"providerDispatchStarted": False},
        {"exactInterruptionRejected": False},
    ],
)
def test_outcome_unknown_requires_concrete_dispatch_and_rejection_evidence(
    invalid_update: dict[str, object],
) -> None:
    payload = cancellation_payload()
    payload.update(
        {
            "status": "outcome_unknown",
            "providerExecution": True,
            "executionCount": 1,
            "providerDispatchStarted": True,
        }
    )
    payload.update(invalid_update)

    with pytest.raises(ValidationError):
        RecoveryReceipt.model_validate(payload)
