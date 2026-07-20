"""Bounded, keyless smoke proof for deterministic replay and SDK lanes."""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from fastapi.testclient import TestClient

from server.config import RuntimeSettings
from server.main import create_app
from server.models import ExecutionMode
from server.providers.hotel_simulator import HotelSimulator
from server.providers.quota_simulator import QuotaSimulator
from server.store import SQLiteStore

QUOTA_PROTOCOL_STEPS = [
    "Detect",
    "Prove",
    "Negotiate",
    "Authorize",
    "Execute",
    "Verify & seal",
]
QUOTA_POLICY: dict[str, object] = {
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
    "protocolSteps": QUOTA_PROTOCOL_STEPS,
}


def _expected_quota_evidence(execution_mode: ExecutionMode) -> dict[str, object]:
    return {
        **QUOTA_POLICY,
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


def _assert_quota_receipt(
    receipt: dict[str, object],
    *,
    execution_mode: ExecutionMode,
) -> None:
    runtime = execution_mode is ExecutionMode.SDK_STUB
    assert receipt["executionMode"] == execution_mode.value
    assert receipt["status"] == "completed"
    assert receipt["simulated"] is True
    assert receipt["providerExecution"] is runtime
    assert receipt["modelIds"] == []
    assert receipt["rootTraceId"] is None
    assert receipt["sdkVersion"] == ("0.18.3" if runtime else None)
    assert receipt["protocolVersion"] == (
        "backchannel.quota.v1" if runtime else None
    )
    assert receipt["agentGraphVersion"] == (
        "backchannel.quota-agent.v1" if runtime else None
    )
    definition_digest = receipt["promptToolSchemaHash"]
    if runtime:
        assert isinstance(definition_digest, str)
        assert len(definition_digest) == 64
    else:
        assert definition_digest is None
    assert receipt["decision"] is None
    assert receipt["decisionRemedyDigest"] is None
    assert receipt["executionCount"] == (1 if runtime else 0)
    assert receipt["providerDispatchStarted"] is runtime
    assert receipt["exactInterruptionRejected"] is False
    assert receipt["permissionRevoked"] is runtime
    assert receipt["scopeClosed"] is runtime
    assert receipt["approvedRemedyDigest"] is None
    assert receipt["quotaEvidence"] == _expected_quota_evidence(execution_mode)


def main() -> None:
    os.environ.pop("OPENAI_API_KEY", None)
    with TemporaryDirectory(prefix="backchannel-smoke-") as temporary_directory:
        database_path = Path(temporary_directory) / "smoke.sqlite3"
        store = SQLiteStore(database_path)
        hotel_provider = HotelSimulator(store=store)
        quota_provider = QuotaSimulator()
        with TestClient(
            create_app(
                RuntimeSettings(live_ready=False, demo_reset_enabled=True),
                store=store,
                hotel_provider=hotel_provider,
                quota_provider=quota_provider,
            )
        ) as client:
            health = client.get("/health")
            health.raise_for_status()
            assert health.json()["backend"] == "stub"
            assert health.json()["liveReady"] is False
            assert health.json()["sdkStubReady"] is True

            replay_created = client.post(
                "/api/recoveries",
                json={"scenarioId": "api-quota", "executionMode": "replay_fixture"},
            )
            replay_created.raise_for_status()
            replay_snapshot = cast(dict[str, object], replay_created.json())
            assert replay_snapshot["scenarioId"] == "api-quota"
            assert replay_snapshot["executionMode"] == "replay_fixture"
            assert replay_snapshot["status"] == "completed"
            assert replay_snapshot["currentStep"] == 5
            assert replay_snapshot["pendingApproval"] is None
            replay_recovery_id = cast(str, replay_snapshot["recoveryId"])

            replay_receipt_response = client.get(
                f"/api/recoveries/{replay_recovery_id}/receipt"
            )
            replay_receipt_response.raise_for_status()
            replay_receipt = cast(
                dict[str, object],
                replay_receipt_response.json(),
            )
            _assert_quota_receipt(
                replay_receipt,
                execution_mode=ExecutionMode.REPLAY_FIXTURE,
            )
            replay_events = store.list_events(replay_recovery_id)
            assert [event.data.get("phase") for event in replay_events[1:]] == (
                QUOTA_PROTOCOL_STEPS
            )
            assert quota_provider.dispatch_count == 0
            assert quota_provider.active_permission_count == 0

            quota_sdk_created = client.post(
                "/api/recoveries",
                json={"scenarioId": "api-quota", "executionMode": "sdk_stub"},
            )
            quota_sdk_created.raise_for_status()
            quota_sdk_snapshot = cast(dict[str, object], quota_sdk_created.json())
            assert quota_sdk_snapshot["scenarioId"] == "api-quota"
            assert quota_sdk_snapshot["executionMode"] == "sdk_stub"
            assert quota_sdk_snapshot["status"] == "completed"
            assert quota_sdk_snapshot["currentStep"] == 5
            assert quota_sdk_snapshot["pendingApproval"] is None
            quota_sdk_recovery_id = cast(str, quota_sdk_snapshot["recoveryId"])
            quota_sdk_receipt_response = client.get(
                f"/api/recoveries/{quota_sdk_recovery_id}/receipt"
            )
            quota_sdk_receipt_response.raise_for_status()
            quota_sdk_receipt = cast(
                dict[str, object],
                quota_sdk_receipt_response.json(),
            )
            _assert_quota_receipt(
                quota_sdk_receipt,
                execution_mode=ExecutionMode.SDK_STUB,
            )
            quota_sdk_events = store.list_events(quota_sdk_recovery_id)
            assert [event.data.get("phase") for event in quota_sdk_events[1:]] == (
                QUOTA_PROTOCOL_STEPS
            )
            assert quota_provider.dispatch_count == 1
            assert quota_provider.active_permission_count == 0

            sdk_created = client.post(
                "/api/recoveries",
                json={"scenarioId": "hotel", "executionMode": "sdk_stub"},
            )
            sdk_created.raise_for_status()
            sdk_snapshot = cast(dict[str, object], sdk_created.json())
            assert sdk_snapshot["status"] == "pending_approval"
            approval = cast(dict[str, object], sdk_snapshot["pendingApproval"])
            assert approval["executionStarted"] is False
            assert hotel_provider.dispatch_count == 0
            sdk_recovery_id = cast(str, sdk_snapshot["recoveryId"])
            decision_payload = {
                "decision": "approve",
                "clientDecisionId": "smoke-restart-approval",
                "remedyId": cast(str, approval["remedyId"]),
                "remedyDigest": cast(str, approval["remedyDigest"]),
                "toolCallId": cast(str, approval["toolCallId"]),
            }
            decline_created = client.post(
                "/api/recoveries",
                json={"scenarioId": "hotel", "executionMode": "sdk_stub"},
            )
            decline_created.raise_for_status()
            decline_snapshot = cast(dict[str, object], decline_created.json())
            decline_approval = cast(
                dict[str, object],
                decline_snapshot["pendingApproval"],
            )
            decline_recovery_id = cast(str, decline_snapshot["recoveryId"])
            decline_payload = {
                "decision": "decline",
                "clientDecisionId": "smoke-restart-decline",
                "remedyId": cast(str, decline_approval["remedyId"]),
                "remedyDigest": cast(str, decline_approval["remedyDigest"]),
                "toolCallId": cast(str, decline_approval["toolCallId"]),
            }

        store.close()
        del hotel_provider, quota_provider, store

        restarted_store = SQLiteStore(database_path)
        restarted_provider = HotelSimulator(store=restarted_store)
        with TestClient(
            create_app(
                RuntimeSettings(live_ready=False, demo_reset_enabled=True),
                store=restarted_store,
                hotel_provider=restarted_provider,
            )
        ) as restarted_client:
            completed = restarted_client.post(
                f"/api/recoveries/{sdk_recovery_id}/decisions",
                json=decision_payload,
            )
            completed.raise_for_status()
            assert completed.json()["status"] == "completed"
            declined = restarted_client.post(
                f"/api/recoveries/{decline_recovery_id}/decisions",
                json=decline_payload,
            )
            declined.raise_for_status()
            assert declined.json()["status"] == "closed_without_action"
        assert restarted_provider.dispatch_count == 1
        sdk_receipt = restarted_store.get_receipt(sdk_recovery_id)
        assert sdk_receipt.execution_mode is ExecutionMode.SDK_STUB
        assert sdk_receipt.provider_execution is True
        decline_receipt = restarted_store.get_receipt(decline_recovery_id)
        assert decline_receipt.status == "closed_without_action"
        assert decline_receipt.provider_execution is False
        assert decline_receipt.execution_count == 0
        assert decline_receipt.exact_interruption_rejected is True
        assert decline_receipt.permission_revoked is True

        print(
            json.dumps(
                {
                    "smoke": "passed",
                    "runtimeMode": "stub_keyless",
                    "replayMode": "replay_fixture",
                    "replayProviderDispatchCount": 0,
                    "quotaPolicy": QUOTA_POLICY,
                    "quotaReplayProof": {
                        "executionMode": "replay_fixture",
                        "source": "recorded_fixture",
                        "providerExecution": False,
                        "executionCount": 0,
                        "providerDispatchStarted": False,
                        "permissionRevoked": False,
                        "revocationEvidenceKind": "recorded_revocation_only",
                    },
                    "quotaSdkProof": {
                        "executionMode": "sdk_stub",
                        "source": "sdk_simulator",
                        "providerExecution": True,
                        "executionCount": 1,
                        "providerDispatchStarted": True,
                        "permissionRevoked": True,
                        "revocationEvidenceKind": "runtime_permission_revoked",
                        "simulatorDispatchCount": 1,
                        "activePermissionCount": 0,
                    },
                    "sdkProofLane": "public_typed_decision",
                    "sdkMode": "sdk_stub",
                    "sdkApprovalCount": 1,
                    "sdkDeclineCount": 1,
                    "sdkDeclineProviderDispatchCount": 0,
                    "sdkPreapprovalDispatchCount": 0,
                    "sdkPostapprovalDispatchCount": 1,
                    "sdkRestartResume": "passed",
                    "sdkSerializedStateOutput": "redacted",
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
