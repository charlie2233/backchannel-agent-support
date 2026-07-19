import pytest
from pydantic import ValidationError

from server.models import ExecutionMode, RecoveryReceipt

APPROVED_DIGEST = f"sha256:{'a' * 64}"


def receipt_payload(execution_mode: ExecutionMode) -> dict[str, object]:
    return {
        "recoveryId": "recovery-123",
        "executionMode": execution_mode,
        "status": (
            "simulated_completed"
            if execution_mode is ExecutionMode.REPLAY_FIXTURE
            else "completed"
        ),
        "simulated": True,
        "providerExecution": execution_mode is ExecutionMode.SDK_STUB,
        "modelIds": [],
        "boundary": "Mode-specific test boundary.",
        "providerResult": "Test result.",
        "authorizationSource": "Test authorization.",
        "verificationResults": ["Test verification."],
        **(
            {"approvedRemedyDigest": APPROVED_DIGEST}
            if execution_mode is ExecutionMode.SDK_STUB
            else {}
        ),
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


def test_closed_without_action_requires_zero_provider_execution() -> None:
    payload = receipt_payload(ExecutionMode.SDK_STUB)
    payload.update(
        {
            "status": "closed_without_action",
            "providerExecution": False,
            "approvedRemedyDigest": None,
        }
    )

    receipt = RecoveryReceipt.model_validate(payload)

    assert receipt.provider_execution is False
    with pytest.raises(ValidationError):
        RecoveryReceipt.model_validate({**payload, "providerExecution": None})


@pytest.mark.parametrize("claimed_execution", [False, True])
def test_unknown_outcome_forbids_claiming_provider_execution(
    claimed_execution: bool,
) -> None:
    payload = receipt_payload(ExecutionMode.SDK_STUB)
    payload.update(
        {
            "status": "outcome_unknown",
            "providerExecution": None,
            "approvedRemedyDigest": None,
        }
    )

    receipt = RecoveryReceipt.model_validate(payload)

    assert receipt.provider_execution is None
    with pytest.raises(ValidationError):
        RecoveryReceipt.model_validate(
            {**payload, "providerExecution": claimed_execution}
        )


@pytest.mark.parametrize("status", ["in_progress", "pending_approval", "bogus"])
def test_receipt_rejects_nonterminal_or_unknown_status(status: str) -> None:
    payload = receipt_payload(ExecutionMode.SDK_STUB)

    with pytest.raises(ValidationError):
        RecoveryReceipt.model_validate({**payload, "status": status})


@pytest.mark.parametrize(
    "invalid_update",
    [
        {"providerExecution": False},
        {"providerExecution": None},
        {"approvedRemedyDigest": None},
    ],
)
def test_completed_sdk_receipt_requires_execution_and_approved_digest(
    invalid_update: dict[str, object],
) -> None:
    payload = receipt_payload(ExecutionMode.SDK_STUB)

    with pytest.raises(ValidationError):
        RecoveryReceipt.model_validate({**payload, **invalid_update})


@pytest.mark.parametrize("provider_execution", [False, None])
def test_completed_live_receipt_requires_provider_execution(
    provider_execution: bool | None,
) -> None:
    payload = receipt_payload(ExecutionMode.OPENAI_LIVE)
    payload["providerExecution"] = provider_execution

    with pytest.raises(ValidationError):
        RecoveryReceipt.model_validate(payload)


@pytest.mark.parametrize("status", ["closed_without_action", "outcome_unknown"])
def test_decline_receipt_rejects_approved_digest(status: str) -> None:
    payload = receipt_payload(ExecutionMode.SDK_STUB)
    payload.update(
        {
            "status": status,
            "providerExecution": False if status == "closed_without_action" else None,
        }
    )

    with pytest.raises(ValidationError):
        RecoveryReceipt.model_validate(payload)


def test_replay_receipt_requires_fixture_status_and_no_approved_digest() -> None:
    payload = receipt_payload(ExecutionMode.REPLAY_FIXTURE)

    with pytest.raises(ValidationError):
        RecoveryReceipt.model_validate({**payload, "status": "completed"})
    with pytest.raises(ValidationError):
        RecoveryReceipt.model_validate(
            {**payload, "approvedRemedyDigest": APPROVED_DIGEST}
        )
