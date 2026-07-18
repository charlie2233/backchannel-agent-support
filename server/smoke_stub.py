"""Bounded, keyless smoke proof for the deterministic replay path."""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from fastapi.testclient import TestClient

from server.config import RuntimeSettings
from server.main import create_app
from server.store import SQLiteStore


def main() -> None:
    os.environ.pop("OPENAI_API_KEY", None)
    with TemporaryDirectory(prefix="backchannel-smoke-") as temporary_directory:
        store = SQLiteStore(Path(temporary_directory) / "smoke.sqlite3")
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

            print(
                json.dumps(
                    {
                        "smoke": "passed",
                        "runtime": "Stub backend (keyless)",
                        "executionMode": "replay_fixture",
                        "receipt": "Simulated replay receipt",
                        "providerExecution": False,
                        "boundary": boundary,
                    },
                    sort_keys=True,
                )
            )


if __name__ == "__main__":
    main()
