"""Bounded, keyless smoke proof for replay and deterministic SDK approval."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from fastapi.testclient import TestClient

from server.config import RuntimeSettings
from server.main import create_app
from server.models import ExecutionMode, RecoveryStatus
from server.orchestrator import RecoveryOrchestrator
from server.providers.hotel_simulator import HotelSimulator
from server.store import SQLiteStore


def main() -> None:
    os.environ.pop("OPENAI_API_KEY", None)
    with TemporaryDirectory(prefix="backchannel-smoke-") as temporary_directory:
        database_path = Path(temporary_directory) / "smoke.sqlite3"
        store = SQLiteStore(database_path)
        with TestClient(
            create_app(
                RuntimeSettings(live_ready=False, demo_reset_enabled=True),
                store=store,
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

        hotel_provider = HotelSimulator(store=store)
        orchestrator = RecoveryOrchestrator(
            store=store,
            hotel_provider=hotel_provider,
        )
        pending = asyncio.run(
            orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
        )
        assert pending.recovery.status is RecoveryStatus.PENDING_APPROVAL
        assert len(pending.sdk_result.interruptions) == 1
        assert hotel_provider.dispatch_count == 0
        sdk_recovery_id = pending.recovery.recovery_id

        store.close()
        del orchestrator, hotel_provider, pending, store

        restarted_store = SQLiteStore(database_path)
        restarted_provider = HotelSimulator(store=restarted_store)
        restarted_orchestrator = RecoveryOrchestrator(
            store=restarted_store,
            hotel_provider=restarted_provider,
        )
        completed_result = asyncio.run(
            restarted_orchestrator.resume_approved(sdk_recovery_id)
        )
        assert completed_result.interruptions == []
        assert restarted_provider.dispatch_count == 1
        sdk_receipt = restarted_store.get_receipt(sdk_recovery_id)
        assert sdk_receipt.execution_mode is ExecutionMode.SDK_STUB
        assert sdk_receipt.provider_execution is True

        print(
            json.dumps(
                {
                    "smoke": "passed",
                    "runtimeMode": "stub_keyless",
                    "replayMode": "replay_fixture",
                    "replayProviderDispatchCount": 0,
                    "sdkProofLane": "internal_orchestrator",
                    "sdkMode": "sdk_stub",
                    "sdkApprovalCount": 1,
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
