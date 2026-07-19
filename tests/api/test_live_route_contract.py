from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient

from server.config import RuntimeSettings
from server.main import create_app
from server.models import ExecutionMode, RecoverySnapshot, ScenarioId
from server.orchestrator import UnsupportedOrchestrationError
from server.providers.hotel_simulator import HotelSimulator
from server.store import SQLiteStore


class _RouteContractOrchestrator:
    """Route-only seam; live Runner/provider behavior is covered by integration tests."""

    def __init__(self) -> None:
        self.model_calls = 0
        self.provider_calls = 0

    async def start(
        self,
        scenario_id: str | ScenarioId,
        *,
        execution_mode: ExecutionMode,
    ) -> SimpleNamespace:
        if ScenarioId(scenario_id) is not ScenarioId.HOTEL:
            raise UnsupportedOrchestrationError(
                "OpenAI live supports the hotel scenario only"
            )
        assert execution_mode is ExecutionMode.OPENAI_LIVE
        self.model_calls += 3
        now = datetime.now(UTC)
        return SimpleNamespace(
            recovery=RecoverySnapshot(
                recoveryId="11111111-2222-4333-8444-555555555555",
                scenarioId=ScenarioId.HOTEL,
                executionMode=ExecutionMode.OPENAI_LIVE,
                status="pending_approval",
                currentStep=3,
                currentStepSummary="Exact live approval pending.",
                createdAt=now,
                updatedAt=now,
                pendingApproval=None,
                rootTraceId="trace_11111111111111111111111111111111",
                modelIds=[
                    "gpt-5.6-luna-2026-07-15-returned",
                    "gpt-5.6-terra-2026-07-15-returned",
                ],
                sdkVersion="0.18.3",
                protocolVersion="backchannel.approval.v1",
                agentGraphVersion="backchannel.hotel-live-agent.v1",
                promptToolSchemaHash="a" * 64,
            )
        )

    async def decide(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("Decision route is outside this focused contract")


def test_live_ready_hotel_route_returns_only_safe_complete_provenance(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "live-route.sqlite3")
    provider = HotelSimulator(store=store)
    orchestrator = _RouteContractOrchestrator()
    client = TestClient(
        create_app(
            RuntimeSettings(
                live_ready=True,
                identity_hmac_secret=(
                    "test-identity-secret-that-is-at-least-32-bytes"
                ),
            ),
            store=store,
            hotel_provider=provider,
            orchestrator=orchestrator,
        )
    )

    response = client.post(
        "/api/recoveries",
        json={"scenarioId": "hotel", "executionMode": "openai_live"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["executionMode"] == "openai_live"
    assert body["rootTraceId"].startswith("trace_")
    assert body["modelIds"] == [
        "gpt-5.6-luna-2026-07-15-returned",
        "gpt-5.6-terra-2026-07-15-returned",
    ]
    assert body["sdkVersion"] == "0.18.3"
    assert body["protocolVersion"]
    assert body["agentGraphVersion"]
    assert len(body["promptToolSchemaHash"]) == 64
    public_json = json.dumps(body).lower()
    for forbidden in (
        "api_key",
        "authorization",
        "state_json",
        "consumer_proof",
        "provider_proof",
    ):
        assert forbidden not in public_json
    assert orchestrator.model_calls == 3
    assert orchestrator.provider_calls == provider.dispatch_count == 0

    model_calls_before = orchestrator.model_calls
    rejected = client.post(
        "/api/recoveries",
        json={"scenarioId": "api-quota", "executionMode": "openai_live"},
    )
    assert rejected.status_code == 422
    assert orchestrator.model_calls == model_calls_before
    assert orchestrator.provider_calls == provider.dispatch_count == 0
