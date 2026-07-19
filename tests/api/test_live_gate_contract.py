from __future__ import annotations

import asyncio
import sqlite3
from datetime import timedelta
from typing import Any

import httpx
import pytest

from server.config import RuntimeSettings
from server.main import create_app
from server.models import (
    ApprovalDecisionRequest,
    DeclineDecisionResponse,
    ExecutionMode,
    ScenarioId,
)
from server.store import SQLiteStore


class _ControllableLiveOrchestrator:
    def __init__(self) -> None:
        self.start_entered = asyncio.Event()
        self.start_calls = 0
        self.decide_calls = 0

    async def startup(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def start(
        self,
        _scenario_id: object,
        *,
        execution_mode: ExecutionMode,
    ) -> Any:
        assert execution_mode is ExecutionMode.OPENAI_LIVE
        self.start_calls += 1
        self.start_entered.set()
        await asyncio.Event().wait()
        raise AssertionError("blocked live start should be cancelled")

    async def decide(
        self,
        recovery_id: str,
        payload: ApprovalDecisionRequest,
    ) -> DeclineDecisionResponse:
        self.decide_calls += 1
        return DeclineDecisionResponse(
            clientDecisionId=payload.client_decision_id,
            recoveryId=recovery_id,
            decision="decline",
            status="closed_without_action",
            decisionRemedyDigest=payload.remedy_digest,
            executionStarted=False,
        )


def _usage_rows(database_path) -> list[tuple[object, ...]]:
    with sqlite3.connect(database_path) as connection:
        return connection.execute(
            """
            SELECT identity_kind, identity_hash, usage_day, amount,
                   last_admitted_at, updated_at
            FROM usage_ledger
            ORDER BY identity_kind, identity_hash, usage_day
            """
        ).fetchall()


def test_start_and_decision_share_gate_cancellation_releases_and_decision_does_not_charge(
    tmp_path,
) -> None:
    async def exercise() -> None:
        database_path = tmp_path / "shared-live-gate.sqlite3"
        store = SQLiteStore(database_path)
        recovery_id = "99999999-2222-4333-8444-555555555555"
        store.create_recovery(
            recovery_id=recovery_id,
            scenario_id=ScenarioId.HOTEL,
            execution_mode=ExecutionMode.OPENAI_LIVE,
            current_step=0,
            current_step_summary="Seeded live recovery for gate contract.",
        )
        orchestrator = _ControllableLiveOrchestrator()
        app = create_app(
            RuntimeSettings(
                live_ready=True,
                identity_hmac_secret=(
                    "test-identity-secret-that-is-at-least-32-bytes"
                ),
                live_max_concurrent=1,
                live_cooldown=timedelta(0),
                live_daily_budget=1,
                cleanup_interval=timedelta(hours=1),
            ),
            store=store,
            orchestrator=orchestrator,  # type: ignore[arg-type]
        )
        transport = httpx.ASGITransport(app=app)
        decision = {
            "decision": "decline",
            "clientDecisionId": "shared-gate-decision",
            "remedyId": "shared-gate-remedy",
            "remedyDigest": "sha256:" + "a" * 64,
            "toolCallId": "shared-gate-call",
        }

        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            live_start = asyncio.create_task(
                client.post(
                    "/api/recoveries",
                    json={"scenarioId": "hotel", "executionMode": "openai_live"},
                )
            )
            await asyncio.wait_for(orchestrator.start_entered.wait(), timeout=2)
            assert app.state.live_gate.active == 1

            capacity = await client.post(
                f"/api/recoveries/{recovery_id}/decisions",
                json=decision,
            )
            assert capacity.status_code == 429
            assert capacity.json()["error"]["code"] == "live_capacity_reached"
            assert capacity.json()["error"]["fallback"] is None
            assert orchestrator.decide_calls == 0

            live_start.cancel()
            with pytest.raises(asyncio.CancelledError):
                await live_start
            assert app.state.live_gate.active == 0

            exhausted = await client.post(
                "/api/recoveries",
                json={"scenarioId": "hotel", "executionMode": "openai_live"},
            )
            assert exhausted.status_code == 429
            assert exhausted.json()["error"]["code"] == (
                "live_daily_budget_exceeded"
            )
            assert orchestrator.start_calls == 1

            usage_before_decision = _usage_rows(database_path)
            accepted = await client.post(
                f"/api/recoveries/{recovery_id}/decisions",
                json=decision,
            )
            usage_after_decision = _usage_rows(database_path)

        assert accepted.status_code == 200
        assert accepted.json()["clientDecisionId"] == "shared-gate-decision"
        assert orchestrator.decide_calls == 1
        assert usage_after_decision == usage_before_decision
        assert len(usage_after_decision) == 3

    asyncio.run(exercise())
