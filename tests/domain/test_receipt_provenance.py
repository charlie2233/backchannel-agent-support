import pytest
from pydantic import ValidationError

from server.models import ExecutionMode, RecoveryReceipt

DECISION_DIGEST = f"sha256:{'a' * 64}"
OTHER_DIGEST = f"sha256:{'b' * 64}"

QUOTA_POLICY_EVIDENCE = {
    "providerCeilingRpm": 1000,
    "recordedDemandRpm": 1200,
    "temporaryBurstRpm": 1500,
    "region": "US",
    "durationSeconds": 900,
    "extraCostMinor": 250,
    "delegatedAuthorityMaxMinor": 500,
    "currency": "USD",
    "hardConstraints": {
        "regionPreserved": True,
        "burstCoversDemand": True,
        "durationWithinLimit": True,
        "baseQuotaUnchanged": True,
    },
    "humanInterruptions": 0,
    "approvals": 0,
    "providerProofVerified": True,
    "grantVerified": True,
    "protocolSteps": [
        "Detect",
        "Prove",
        "Negotiate",
        "Authorize",
        "Execute",
        "Verify & seal",
    ],
}


def quota_evidence(execution_mode: ExecutionMode) -> dict[str, object]:
    return {
        **QUOTA_POLICY_EVIDENCE,
        "source": (
            "sdk_simulator"
            if execution_mode is ExecutionMode.SDK_STUB
            else "recorded_fixture"
        ),
        "revocationEvidenceKind": (
            "runtime_permission_revoked"
            if execution_mode is ExecutionMode.SDK_STUB
            else "recorded_revocation_only"
        ),
    }


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


def quota_receipt_payload(execution_mode: ExecutionMode) -> dict[str, object]:
    payload = receipt_payload(execution_mode)
    payload.update(
        {
            "providerExecution": execution_mode is ExecutionMode.SDK_STUB,
            "decision": None,
            "decisionRemedyDigest": None,
            "executionCount": (
                1 if execution_mode is ExecutionMode.SDK_STUB else 0
            ),
            "providerDispatchStarted": execution_mode is ExecutionMode.SDK_STUB,
            "exactInterruptionRejected": False,
            "permissionRevoked": execution_mode is ExecutionMode.SDK_STUB,
            "scopeClosed": execution_mode is ExecutionMode.SDK_STUB,
            "approvedRemedyDigest": None,
            "quotaEvidence": quota_evidence(execution_mode),
        }
    )
    if execution_mode is ExecutionMode.SDK_STUB:
        payload.update(
            {
                "rootTraceId": None,
                "sdkVersion": "0.18.3",
                "protocolVersion": "backchannel.quota.v1",
                "agentGraphVersion": "backchannel.quota-agent.v1",
                "promptToolSchemaHash": "c" * 64,
            }
        )
    return payload


@pytest.mark.parametrize(
    "execution_mode",
    [ExecutionMode.SDK_STUB, ExecutionMode.REPLAY_FIXTURE],
)
def test_quota_receipt_accepts_only_the_delegated_or_recorded_branch(
    execution_mode: ExecutionMode,
) -> None:
    receipt = RecoveryReceipt.model_validate(quota_receipt_payload(execution_mode))

    assert receipt.quota_evidence is not None
    assert receipt.quota_evidence.temporary_burst_rpm == 1500
    assert receipt.quota_evidence.human_interruptions == 0
    assert receipt.quota_evidence.approvals == 0


@pytest.mark.parametrize(
    ("path", "invalid_value"),
    [
        ("providerCeilingRpm", 999),
        ("recordedDemandRpm", 1199),
        ("temporaryBurstRpm", 1199),
        ("region", "EU"),
        ("durationSeconds", 901),
        ("extraCostMinor", 501),
        ("humanInterruptions", 1),
        ("approvals", 1),
        ("grantVerified", False),
        ("source", "recorded_fixture"),
        ("revocationEvidenceKind", "recorded_revocation_only"),
        ("protocolSteps", ["Detect", "Verify & seal"]),
    ],
)
def test_quota_receipt_rejects_noncanonical_policy_evidence(
    path: str,
    invalid_value: object,
) -> None:
    payload = quota_receipt_payload(ExecutionMode.SDK_STUB)
    invalid_evidence = quota_evidence(ExecutionMode.SDK_STUB)
    invalid_evidence[path] = invalid_value
    payload["quotaEvidence"] = invalid_evidence

    with pytest.raises(ValidationError):
        RecoveryReceipt.model_validate(payload)


@pytest.mark.parametrize(
    "invalid_update",
    [
        {"source": "sdk_simulator"},
        {"revocationEvidenceKind": "runtime_permission_revoked"},
    ],
)
def test_quota_replay_rejects_runtime_source_or_revocation_claims(
    invalid_update: dict[str, object],
) -> None:
    payload = quota_receipt_payload(ExecutionMode.REPLAY_FIXTURE)
    evidence = quota_evidence(ExecutionMode.REPLAY_FIXTURE)
    evidence.update(invalid_update)
    payload["quotaEvidence"] = evidence

    with pytest.raises(ValidationError):
        RecoveryReceipt.model_validate(payload)


@pytest.mark.parametrize(
    "invalid_update",
    [
        {"providerExecution": False},
        {"executionCount": 0},
        {"providerDispatchStarted": False},
        {"permissionRevoked": False},
        {"scopeClosed": False},
        {"decision": "approved"},
        {"decisionRemedyDigest": DECISION_DIGEST},
        {"approvedRemedyDigest": DECISION_DIGEST},
        {"rootTraceId": "qa_trace_11111111111111111111111111111111"},
        {"sdkVersion": None},
        {"protocolVersion": None},
        {"agentGraphVersion": None},
        {"promptToolSchemaHash": None},
    ],
)
def test_quota_sdk_receipt_rejects_impossible_runtime_evidence(
    invalid_update: dict[str, object],
) -> None:
    payload = quota_receipt_payload(ExecutionMode.SDK_STUB)
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
