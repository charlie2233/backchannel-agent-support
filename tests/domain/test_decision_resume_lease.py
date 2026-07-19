from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from threading import Event, Thread

import pytest

from server.models import ApprovalDecisionRequest, ExecutionMode
from server.orchestrator import RecoveryOrchestrator
from server.providers.hotel_simulator import HotelSimulator
from server.store import ApprovalDecisionError, SQLiteStore


def _pending_claim(store: SQLiteStore):
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(store=store),
    )
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    approval = pending.recovery.pending_approval
    assert approval is not None
    request = ApprovalDecisionRequest(
        decision="approve",
        clientDecisionId="lease-takeover-decision",
        remedyId=approval.remedy_id,
        remedyDigest=approval.remedy_digest,
        toolCallId=approval.tool_call_id,
    )
    return store.claim_decision(pending.recovery.recovery_id, request)


def test_expired_owner_takeover_fences_stale_provider_guard_and_heartbeat(
    tmp_path,
) -> None:
    database_path = tmp_path / "decision-resume-lease.sqlite3"
    first_store = SQLiteStore(database_path)
    claim = _pending_claim(first_store)
    second_store = SQLiteStore(database_path)
    started_at = datetime(2026, 7, 19, 8, 0, tzinfo=UTC)
    lease_duration = timedelta(seconds=30)

    first = first_store.acquire_decision_resume(
        claim,
        resume_owner_id="owner-a",
        lease_duration=lease_duration,
        now=started_at,
    )
    assert first.disposition == "owner"
    assert first.resume_generation == 1
    assert second_store.acquire_decision_resume(
        claim,
        resume_owner_id="owner-b",
        lease_duration=lease_duration,
        now=started_at + timedelta(seconds=20),
    ).disposition == "wait"

    assert first_store.renew_decision_resume(
        claim,
        resume_owner_id="owner-a",
        resume_generation=first.resume_generation,
        lease_duration=lease_duration,
        now=started_at + timedelta(seconds=20),
    )
    assert second_store.acquire_decision_resume(
        claim,
        resume_owner_id="owner-b",
        lease_duration=lease_duration,
        now=started_at + timedelta(seconds=35),
    ).disposition == "wait"

    takeover = second_store.acquire_decision_resume(
        claim,
        resume_owner_id="owner-b",
        lease_duration=lease_duration,
        now=started_at + timedelta(seconds=51),
    )
    assert takeover.disposition == "owner"
    assert takeover.resume_generation == 2
    envelope = second_store.get_pending_approval(claim.recovery_id)

    with pytest.raises(ApprovalDecisionError, match="resume_owner_lost"):
        first_store.assert_provider_dispatch_authorized(
            recovery_id=claim.recovery_id,
            tool_call_id=claim.request.tool_call_id,
            remedy_digest=claim.request.remedy_digest,
            action_digest=envelope.action_digest,
            resume_owner_id="owner-a",
            resume_generation=first.resume_generation,
            now=started_at + timedelta(seconds=52),
        )
    second_store.assert_provider_dispatch_authorized(
        recovery_id=claim.recovery_id,
        tool_call_id=claim.request.tool_call_id,
        remedy_digest=claim.request.remedy_digest,
        action_digest=envelope.action_digest,
        resume_owner_id="owner-b",
        resume_generation=takeover.resume_generation,
        now=started_at + timedelta(seconds=52),
    )


def test_provider_guard_samples_clock_after_write_transaction_begins(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "decision-provider-guard-clock.sqlite3"
    store = SQLiteStore(database_path)
    claim = _pending_claim(store)
    started_at = datetime(2026, 7, 19, 8, 0, tzinfo=UTC)
    lease = store.acquire_decision_resume(
        claim,
        resume_owner_id="clock-owner",
        lease_duration=timedelta(seconds=30),
        now=started_at,
    )
    envelope = store.get_pending_approval(claim.recovery_id)
    begin_attempted = Event()
    allow_begin = Event()
    clock_sampled = Event()
    original_connect = store._connect

    class BlockingConnection:
        def __init__(self, connection) -> None:
            self._connection = connection

        def __enter__(self):
            self._connection.__enter__()
            return self

        def __exit__(self, *args):
            return self._connection.__exit__(*args)

        def execute(self, statement, parameters=()):
            if statement == "BEGIN IMMEDIATE":
                begin_attempted.set()
                assert allow_begin.wait(timeout=2)
            return self._connection.execute(statement, parameters)

    monkeypatch.setattr(
        store,
        "_connect",
        lambda: BlockingConnection(original_connect()),
    )

    def expired_now() -> datetime:
        clock_sampled.set()
        return started_at + timedelta(seconds=31)

    monkeypatch.setattr(store, "_now", expired_now)
    errors: list[BaseException] = []

    def run_guard() -> None:
        try:
            store.assert_provider_dispatch_authorized(
                recovery_id=claim.recovery_id,
                tool_call_id=claim.request.tool_call_id,
                remedy_digest=claim.request.remedy_digest,
                action_digest=envelope.action_digest,
                resume_owner_id="clock-owner",
                resume_generation=lease.resume_generation,
            )
        except BaseException as error:
            errors.append(error)

    worker = Thread(target=run_guard)
    worker.start()
    assert begin_attempted.wait(timeout=2)
    assert not clock_sampled.is_set()
    allow_begin.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert clock_sampled.is_set()
    assert len(errors) == 1
    assert isinstance(errors[0], ApprovalDecisionError)
    assert errors[0].code == "resume_owner_lost"
