"""Bounded, keyless smoke proof for replay and deterministic SDK decisions."""

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
from server.store import SQLiteStore


def _run_smoke() -> None:
    with TemporaryDirectory(prefix="backchannel-smoke-") as temporary_directory:
        database_path = Path(temporary_directory) / "smoke.sqlite3"
        store = SQLiteStore(database_path)
        hotel_provider = HotelSimulator(store=store)
        with TestClient(
            create_app(
                RuntimeSettings(live_ready=False, demo_reset_enabled=True),
                store=store,
                hotel_provider=hotel_provider,
            )
        ) as client:
            health = client.get("/health")
            health.raise_for_status()
            assert health.json()["backend"] == "stub"
            assert health.json()["liveReady"] is False

            created = client.post(
                "/api/recoveries",
                json={"scenarioId": "api-quota", "executionMode": "replay_fixture"},
            )
            created.raise_for_status()
            recovery_id = cast(str, created.json()["recoveryId"])

            receipt_response = client.get(f"/api/recoveries/{recovery_id}/receipt")
            receipt_response.raise_for_status()
            receipt = cast(dict[str, object], receipt_response.json())
            assert receipt["executionMode"] == "replay_fixture"
            assert receipt["simulated"] is True
            assert receipt["providerExecution"] is False
            assert receipt["modelIds"] == []
            boundary = cast(str, receipt["boundary"])
            assert "no model call or provider execution" in boundary.lower()

            approve_created = client.post(
                "/api/recoveries",
                json={"scenarioId": "hotel", "executionMode": "sdk_stub"},
            )
            approve_created.raise_for_status()
            approve_snapshot = cast(dict[str, object], approve_created.json())
            assert approve_snapshot["status"] == "pending_approval"
            approve_record = cast(
                dict[str, object], approve_snapshot["pendingApproval"]
            )
            assert approve_record["executionStarted"] is False

            decline_created = client.post(
                "/api/recoveries",
                json={"scenarioId": "hotel", "executionMode": "sdk_stub"},
            )
            decline_created.raise_for_status()
            decline_snapshot = cast(dict[str, object], decline_created.json())
            assert decline_snapshot["status"] == "pending_approval"
            decline_record = cast(
                dict[str, object], decline_snapshot["pendingApproval"]
            )
            assert decline_record["executionStarted"] is False
            assert hotel_provider.dispatch_count == 0
            approve_recovery_id = cast(str, approve_snapshot["recoveryId"])
            approve_payload = {
                "action": "approve",
                "clientDecisionId": "smoke-restart-approve",
                "remedyId": cast(str, approve_record["remedyId"]),
                "remedyDigest": cast(str, approve_record["remedyDigest"]),
                "toolCallId": cast(str, approve_record["toolCallId"]),
            }
            decline_recovery_id = cast(str, decline_snapshot["recoveryId"])
            decline_payload = {
                "action": "decline",
                "clientDecisionId": "smoke-restart-decline",
                "remedyId": cast(str, decline_record["remedyId"]),
                "remedyDigest": cast(str, decline_record["remedyDigest"]),
                "toolCallId": cast(str, decline_record["toolCallId"]),
            }

        store.close()
        del hotel_provider, store

        restarted_store = SQLiteStore(database_path)
        restarted_provider = HotelSimulator(store=restarted_store)
        with TestClient(
            create_app(
                RuntimeSettings(live_ready=False, demo_reset_enabled=True),
                store=restarted_store,
                hotel_provider=restarted_provider,
            )
        ) as restarted_client:
            approved = restarted_client.post(
                f"/api/recoveries/{approve_recovery_id}/decisions",
                json=approve_payload,
            )
            approved.raise_for_status()
            approved_body = cast(dict[str, object], approved.json())
            assert approved_body["action"] == "approve"
            assert approved_body["status"] == "completed"
            assert approved_body["executionStarted"] is True
            assert restarted_provider.dispatch_count == 1
            assert restarted_store.count_executions(approve_recovery_id) == 1
            approve_receipt = restarted_store.get_receipt(approve_recovery_id)
            assert approve_receipt.execution_mode is ExecutionMode.SDK_STUB
            assert approve_receipt.provider_execution is True

            dispatches_before_decline = restarted_provider.dispatch_count
            declined = restarted_client.post(
                f"/api/recoveries/{decline_recovery_id}/decisions",
                json=decline_payload,
            )
            declined.raise_for_status()
            declined_body = cast(dict[str, object], declined.json())
            assert declined_body["action"] == "decline"
            assert declined_body["status"] == "closed_without_action"
            assert declined_body["approvedRemedyDigest"] is None
            assert declined_body["executionStarted"] is False
            decline_dispatch_count = (
                restarted_provider.dispatch_count - dispatches_before_decline
            )
            assert decline_dispatch_count == 0
            assert restarted_store.count_executions(decline_recovery_id) == 0

            decline_snapshot_response = restarted_client.get(
                f"/api/recoveries/{decline_recovery_id}"
            )
            decline_snapshot_response.raise_for_status()
            terminal_decline = cast(
                dict[str, object], decline_snapshot_response.json()
            )
            assert terminal_decline["status"] == "closed_without_action"
            assert terminal_decline["pendingApproval"] is None

            decline_receipt = restarted_store.get_receipt(decline_recovery_id)
            assert decline_receipt.execution_mode is ExecutionMode.SDK_STUB
            assert decline_receipt.status == "closed_without_action"
            assert decline_receipt.provider_execution is False
            assert decline_receipt.approved_remedy_digest is None
            assert decline_receipt.provider_result == "Provider dispatch did not begin."
            assert decline_receipt.verification_results == [
                "Exact interruption rejected.",
                "No replacement remedy selected.",
                "Execution count is zero.",
                "Provider dispatch did not begin.",
                "Temporary permission revoked.",
                "Cancellation receipt sealed.",
            ]
            assert restarted_provider.dispatch_count == 1

        print(
            json.dumps(
                {
                    "smoke": "passed",
                    "runtimeMode": "stub_keyless",
                    "replayMode": "replay_fixture",
                    "replayProviderDispatchCount": 0,
                    "sdkProofLane": "public_typed_decision",
                    "sdkMode": "sdk_stub",
                    "sdkApprovalCount": 1,
                    "sdkApproveStatus": approved_body["status"],
                    "sdkApproveExecutionCount": restarted_store.count_executions(
                        approve_recovery_id
                    ),
                    "sdkDeclineStatus": declined_body["status"],
                    "sdkDeclineExecutionCount": restarted_store.count_executions(
                        decline_recovery_id
                    ),
                    "sdkDeclineProviderDispatchCount": decline_dispatch_count,
                    "sdkDeclineVerificationResults": decline_receipt.verification_results,
                    "sdkPreapprovalDispatchCount": 0,
                    "sdkPostapprovalDispatchCount": 1,
                    "sdkRestartResume": "passed",
                    "sdkSerializedStateOutput": "redacted",
                },
                sort_keys=True,
            )
        )
        restarted_store.close()


def main() -> None:
    original_api_key = os.environ.pop("OPENAI_API_KEY", None)
    try:
        _run_smoke()
    finally:
        if original_api_key is not None:
            os.environ["OPENAI_API_KEY"] = original_api_key


if __name__ == "__main__":
    main()
