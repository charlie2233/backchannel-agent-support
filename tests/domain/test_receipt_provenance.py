import pytest
from pydantic import ValidationError

from server.models import ExecutionMode, RecoveryReceipt


def receipt_payload(execution_mode: ExecutionMode) -> dict[str, object]:
    return {
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


@pytest.mark.parametrize(
    "invalid_update",
    [
        {"simulated": False},
        {"providerExecution": True},
        {"modelIds": ["impossible-replay-model"]},
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
    ],
)
def test_sdk_stub_receipt_rejects_impossible_provenance(
    invalid_update: dict[str, object],
) -> None:
    payload = receipt_payload(ExecutionMode.SDK_STUB)
    payload.update(invalid_update)

    with pytest.raises(ValidationError):
        RecoveryReceipt.model_validate(payload)
