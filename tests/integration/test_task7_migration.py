from __future__ import annotations

import asyncio
import json
import sqlite3

from server.models import ApprovalDecisionRequest, ExecutionMode, RecoveryStatus
from server.orchestrator import RecoveryOrchestrator
from server.providers.hotel_simulator import HotelSimulator
from server.store import SQLiteStore


def _approval_request(pending) -> ApprovalDecisionRequest:
    approval = pending.recovery.pending_approval
    assert approval is not None
    return ApprovalDecisionRequest(
        decision="approve",
        clientDecisionId="task7-migration-completed",
        remedyId=approval.remedy_id,
        remedyDigest=approval.remedy_digest,
        toolCallId=approval.tool_call_id,
    )


def _downgrade_pending_envelopes_to_task6(database_path) -> None:
    with sqlite3.connect(database_path) as connection:
        receipt_rows = connection.execute(
            "SELECT recovery_id, receipt_json FROM receipts"
        ).fetchall()
        for recovery_id, receipt_json in receipt_rows:
            receipt = json.loads(receipt_json)
            for key in (
                "rootTraceId",
                "sdkVersion",
                "protocolVersion",
                "agentGraphVersion",
                "promptToolSchemaHash",
            ):
                receipt.pop(key, None)
            connection.execute(
                "UPDATE receipts SET receipt_json = ? WHERE recovery_id = ?",
                (
                    json.dumps(receipt, separators=(",", ":"), sort_keys=True),
                    recovery_id,
                ),
            )
        connection.commit()
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            CREATE TABLE pending_approvals_task6 (
                tool_call_id TEXT PRIMARY KEY,
                recovery_id TEXT NOT NULL REFERENCES recoveries(id) ON DELETE CASCADE,
                sdk_version TEXT NOT NULL,
                protocol_version TEXT NOT NULL,
                agent_graph_version TEXT NOT NULL,
                definition_digest TEXT NOT NULL,
                root_trace_id TEXT NOT NULL,
                execution_mode TEXT NOT NULL,
                action_digest TEXT NOT NULL,
                remedy_id TEXT NOT NULL,
                consent_digest TEXT NOT NULL,
                state_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO pending_approvals_task6 (
                tool_call_id, recovery_id, sdk_version, protocol_version,
                agent_graph_version, definition_digest, root_trace_id,
                execution_mode, action_digest, remedy_id, consent_digest,
                state_json, status, created_at, updated_at
            )
            SELECT
                tool_call_id, recovery_id, sdk_version, protocol_version,
                agent_graph_version, definition_digest, root_trace_id,
                execution_mode, action_digest, remedy_id, consent_digest,
                state_json, status, created_at, updated_at
            FROM pending_approvals
            """
        )
        connection.execute("DROP TABLE pending_approvals")
        connection.execute(
            "ALTER TABLE pending_approvals_task6 RENAME TO pending_approvals"
        )
        connection.commit()
        connection.execute("PRAGMA foreign_keys = ON")


def test_task6_envelopes_rows_receipts_and_foreign_keys_migrate_without_loss(
    tmp_path,
) -> None:
    database_path = tmp_path / "task6-to-task7.sqlite3"
    store = SQLiteStore(database_path)
    provider = HotelSimulator(store=store)
    orchestrator = RecoveryOrchestrator(store=store, hotel_provider=provider)
    completed = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    asyncio.run(
        orchestrator.approve_decision(
            completed.recovery.recovery_id,
            _approval_request(completed),
        )
    )
    completed_events = len(store.list_events(completed.recovery.recovery_id))
    pending_events = len(store.list_events(pending.recovery.recovery_id))
    store.close()

    _downgrade_pending_envelopes_to_task6(database_path)
    migrated = SQLiteStore(database_path)

    for recovery_id in (
        completed.recovery.recovery_id,
        pending.recovery.recovery_id,
    ):
        envelope = migrated.get_pending_approval(recovery_id)
        assert envelope.sdk_version == "0.18.3"
        assert envelope.protocol_version == "backchannel.approval.v1"
        assert envelope.agent_graph_version == "backchannel.hotel-agent.v1"
        assert len(envelope.definition_digest) == 64
        assert envelope.model_metadata == ()
    assert migrated.get_recovery(completed.recovery.recovery_id).status is (
        RecoveryStatus.COMPLETED
    )
    receipt = migrated.get_receipt(completed.recovery.recovery_id)
    assert receipt.root_trace_id is not None
    assert receipt.sdk_version == "0.18.3"
    assert receipt.model_ids == []
    assert len(migrated.list_events(completed.recovery.recovery_id)) == completed_events
    assert len(migrated.list_events(pending.recovery.recovery_id)) == pending_events
    with sqlite3.connect(database_path) as connection:
        pending_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(pending_approvals)"
            ).fetchall()
        }
        assert "model_metadata_json" in pending_columns
        assert connection.execute("SELECT COUNT(*) FROM recoveries").fetchone() == (2,)
        assert connection.execute("SELECT COUNT(*) FROM pending_approvals").fetchone() == (
            2,
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
