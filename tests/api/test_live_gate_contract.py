from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from server.config import RuntimeSettings
from server.controls import PublicIdentityHasher
from server.main import create_app
from server.models import (
    ApprovalDecisionRequest,
    DeclineDecisionResponse,
    ExecutionMode,
    ScenarioId,
)
from server.orchestrator import RecoveryOrchestrator
from server.providers.hotel_simulator import HotelSimulator
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
        session_hash: str | None = None,
    ) -> Any:
        assert session_hash is not None
        assert execution_mode is ExecutionMode.OPENAI_LIVE
        self.start_calls += 1
        self.start_entered.set()
        await asyncio.Event().wait()
        raise AssertionError("blocked live start should be cancelled")

    async def decide(
        self,
        recovery_id: str,
        payload: ApprovalDecisionRequest,
        **_kwargs: object,
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
        secret = "test-identity-secret-that-is-at-least-32-bytes"
        hasher = PublicIdentityHasher(secret)
        signed_cookie, credential = hasher.demo_session_cookie_codec(
            lifetime_seconds=24 * 60 * 60
        ).mint(now=datetime.now(UTC))
        session_hash = hasher.session(credential.nonce)
        setup = RecoveryOrchestrator(
            store=store,
            hotel_provider=HotelSimulator(store=store),
        )
        pending = await setup.start(
            ScenarioId.HOTEL,
            execution_mode=ExecutionMode.SDK_STUB,
            session_hash=session_hash,
        )
        recovery_id = pending.recovery.recovery_id
        approval = pending.recovery.pending_approval
        assert approval is not None
        live_gate_snapshot = pending.recovery.model_copy(
            update={"execution_mode": ExecutionMode.OPENAI_LIVE}
        )

        def get_live_gate_snapshot(_recovery_id: str):
            return live_gate_snapshot

        store.get_recovery = get_live_gate_snapshot  # type: ignore[method-assign]
        orchestrator = _ControllableLiveOrchestrator()
        settings = RuntimeSettings(
            live_ready=True,
            identity_hmac_secret=secret,
            live_max_concurrent=1,
            live_cooldown=timedelta(0),
            live_daily_budget=1,
            cleanup_interval=timedelta(hours=1),
        )
        app = create_app(
            settings,
            store=store,
            orchestrator=orchestrator,  # type: ignore[arg-type]
        )
        transport = httpx.ASGITransport(app=app)
        decision = {
            "decision": "decline",
            "clientDecisionId": "shared-gate-decision",
            "remedyId": approval.remedy_id,
            "remedyDigest": approval.remedy_digest,
            "toolCallId": approval.tool_call_id,
        }

        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            cookies={"backchannel_demo_session": signed_cookie},
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


def test_completed_live_decision_replay_bypasses_saturated_gate(
    tmp_path,
    monkeypatch,
) -> None:
    async def exercise() -> None:
        database_path = tmp_path / "completed-live-replay.sqlite3"
        store = SQLiteStore(database_path)
        secret = "test-identity-secret-that-is-at-least-32-bytes"
        hasher = PublicIdentityHasher(secret)
        signed_cookie, credential = hasher.demo_session_cookie_codec(
            lifetime_seconds=24 * 60 * 60
        ).mint(now=datetime.now(UTC))
        session_hash = hasher.session(credential.nonce)
        hotel = HotelSimulator(store=store)
        orchestrator = RecoveryOrchestrator(store=store, hotel_provider=hotel)
        pending = await orchestrator.start(
            ScenarioId.HOTEL,
            execution_mode=ExecutionMode.SDK_STUB,
            session_hash=session_hash,
        )
        recovery_id = pending.recovery.recovery_id
        approval = pending.recovery.pending_approval
        assert approval is not None
        decision = ApprovalDecisionRequest(
            decision="decline",
            clientDecisionId="completed-live-replay",
            remedyId=approval.remedy_id,
            remedyDigest=approval.remedy_digest,
            toolCallId=approval.tool_call_id,
        )
        completed = await orchestrator.decide(
            recovery_id,
            decision,
            session_hash=session_hash,
        )
        assert completed.status == "closed_without_action"
        live_snapshot = store.get_recovery(recovery_id).model_copy(
            update={"execution_mode": ExecutionMode.OPENAI_LIVE}
        )
        monkeypatch.setattr(store, "get_recovery", lambda _recovery_id: live_snapshot)

        async def reject_resume_work(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("completed replay must not resume provider work")

        monkeypatch.setattr(orchestrator, "_acquire_resume_lease", reject_resume_work)
        app = create_app(
            RuntimeSettings(
                live_ready=True,
                identity_hmac_secret=secret,
                live_max_concurrent=1,
                live_cooldown=timedelta(0),
                live_daily_budget=1,
                cleanup_interval=timedelta(hours=1),
            ),
            store=store,
            orchestrator=orchestrator,
        )
        transport = httpx.ASGITransport(app=app)
        before_dispatches = hotel.dispatch_count

        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            cookies={"backchannel_demo_session": signed_cookie},
        ) as client:
            async with app.state.live_gate.slot():
                assert app.state.live_gate.active == 1
                response = await client.post(
                    f"/api/recoveries/{recovery_id}/decisions",
                    json=decision.model_dump(by_alias=True, mode="json"),
                )

        assert response.status_code == 200
        assert response.json() == completed.model_dump(by_alias=True, mode="json")
        assert hotel.dispatch_count == before_dispatches == 0

    asyncio.run(exercise())
