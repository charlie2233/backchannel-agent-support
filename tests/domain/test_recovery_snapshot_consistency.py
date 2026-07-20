from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Event

from server.models import (
    ApprovalDecisionRequest,
    DecisionAction,
    ExecutionMode,
    PendingApprovalView,
)
from server.orchestrator import RecoveryOrchestrator
from server.providers.hotel_simulator import HotelSimulator
from server.store import SQLiteStore


class PausingSnapshotStore(SQLiteStore):
    """Pause one recovery read after its pre-claim pending projection."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        pending_projected: Event,
        claim_committed: Event,
    ) -> None:
        self._pending_projected = pending_projected
        self._claim_committed = claim_committed
        super().__init__(database_path)

    def _public_pending_view(
        self,
        connection: sqlite3.Connection,
        recovery_id: str,
    ) -> PendingApprovalView | None:
        pending_view = super()._public_pending_view(connection, recovery_id)
        if pending_view is not None:
            self._pending_projected.set()
            if not self._claim_committed.wait(timeout=10):
                raise TimeoutError("Decision claim did not commit while snapshot read paused")
        return pending_view


def test_recovery_read_uses_one_snapshot_across_pending_and_claimed_views(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "recovery-snapshot.sqlite3"
    setup_store = SQLiteStore(database_path)
    provider = HotelSimulator(store=setup_store)
    pending = asyncio.run(
        RecoveryOrchestrator(store=setup_store, hotel_provider=provider).start(
            "hotel",
            execution_mode=ExecutionMode.SDK_STUB,
        )
    )
    recovery_id = pending.recovery.recovery_id
    approval = pending.recovery.pending_approval
    assert approval is not None
    setup_store.close()

    with closing(sqlite3.connect(database_path)) as connection:
        assert connection.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)

    pending_projected = Event()
    claim_committed = Event()
    reader = PausingSnapshotStore(
        database_path,
        pending_projected=pending_projected,
        claim_committed=claim_committed,
    )
    writer = SQLiteStore(database_path)
    request = ApprovalDecisionRequest(
        action=DecisionAction.APPROVE,
        clientDecisionId="snapshot-race-decision",
        remedyId=approval.remedy_id,
        remedyDigest=approval.remedy_digest,
        toolCallId=approval.tool_call_id,
    )

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            paused_read = executor.submit(reader.get_recovery, recovery_id)
            assert pending_projected.wait(timeout=10)
            try:
                writer.claim_approval_decision(recovery_id, request)
            finally:
                claim_committed.set()
            before_claim = paused_read.result(timeout=10)

        assert before_claim.pending_approval is not None
        assert before_claim.claimed_decision is None

        after_claim = writer.get_recovery(recovery_id)
        assert after_claim.pending_approval is None
        assert after_claim.claimed_decision is not None
        assert after_claim.claimed_decision.action is request.action
        assert after_claim.claimed_decision.remedy_digest == request.remedy_digest
        assert after_claim.claimed_decision.expiry == approval.expiry
        assert writer.count_decisions(recovery_id) == 1
        assert writer.count_executions(recovery_id) == 0
        assert provider.dispatch_count == 0
    finally:
        claim_committed.set()
        reader.close()
        writer.close()
