from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3

from server.models import ApprovalDecisionRequest, ExecutionMode, RecoveryStatus
from server.orchestrator import RecoveryOrchestrator
from server.providers.hotel_simulator import HotelSimulator
from server.store import SQLiteStore


def _approval_request(pending, decision_id: str) -> ApprovalDecisionRequest:
    approval = pending.recovery.pending_approval
    assert approval is not None
    return ApprovalDecisionRequest(
        decision="approve",
        clientDecisionId=decision_id,
        remedyId=approval.remedy_id,
        remedyDigest=approval.remedy_digest,
        toolCallId=approval.tool_call_id,
    )


def _downgrade_to_task5_decisions_and_receipt(database_path) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT * FROM approval_decisions").fetchall()
        task5_rows: list[tuple[object, ...]] = []
        for row in rows:
            result_json = row["result_json"]
            if result_json is not None:
                result = json.loads(result_json)
                result.pop("decision", None)
                result_json = json.dumps(result, separators=(",", ":"), sort_keys=True)
            old_fingerprint = hashlib.sha256(
                f"task5:{row['recovery_id']}:{row['client_decision_id']}".encode()
            ).hexdigest()
            task5_rows.append(
                (
                    row["recovery_id"],
                    row["client_decision_id"],
                    row["remedy_id"],
                    row["remedy_digest"],
                    row["tool_call_id"],
                    old_fingerprint,
                    row["status"],
                    result_json,
                    row["claimed_at"],
                    row["completed_at"],
                )
            )

        receipt_row = connection.execute(
            "SELECT recovery_id, receipt_json FROM receipts"
        ).fetchone()
        assert receipt_row is not None
        legacy_receipt = json.loads(receipt_row["receipt_json"])
        for key in (
            "decision",
            "decisionRemedyDigest",
            "executionCount",
            "providerDispatchStarted",
            "exactInterruptionRejected",
            "permissionRevoked",
            "scopeClosed",
        ):
            legacy_receipt.pop(key, None)
        legacy_receipt["verificationResults"] = [
            statement
            for statement in legacy_receipt["verificationResults"]
            if "permission revoked" not in statement.lower()
        ]
        terminal_row = connection.execute(
            "SELECT id, data_json FROM events WHERE terminal = 1"
        ).fetchone()
        assert terminal_row is not None
        legacy_terminal = json.loads(terminal_row["data_json"])
        for key in (
            "decision",
            "decisionRemedyDigest",
            "executionCount",
            "providerDispatchStarted",
            "exactInterruptionRejected",
            "permissionRevoked",
            "scopeClosed",
        ):
            legacy_terminal.pop(key, None)

        connection.commit()
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DROP TABLE permission_scopes")
        connection.execute(
            """
            CREATE TABLE approval_decisions_task5 (
                recovery_id TEXT PRIMARY KEY REFERENCES recoveries(id) ON DELETE CASCADE,
                client_decision_id TEXT NOT NULL UNIQUE,
                remedy_id TEXT NOT NULL,
                remedy_digest TEXT NOT NULL,
                tool_call_id TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('claimed', 'completed')),
                result_json TEXT,
                claimed_at TEXT NOT NULL,
                completed_at TEXT,
                CHECK (
                    (status = 'claimed' AND result_json IS NULL
                        AND completed_at IS NULL)
                    OR
                    (status = 'completed' AND result_json IS NOT NULL
                        AND completed_at IS NOT NULL)
                )
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO approval_decisions_task5 (
                recovery_id, client_decision_id, remedy_id, remedy_digest,
                tool_call_id, request_fingerprint, status, result_json,
                claimed_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            task5_rows,
        )
        connection.execute("DROP TABLE approval_decisions")
        connection.execute(
            "ALTER TABLE approval_decisions_task5 RENAME TO approval_decisions"
        )
        connection.execute(
            "UPDATE receipts SET receipt_json = ? WHERE recovery_id = ?",
            (
                json.dumps(legacy_receipt, separators=(",", ":"), sort_keys=True),
                receipt_row["recovery_id"],
            ),
        )
        connection.execute(
            "UPDATE events SET data_json = ? WHERE id = ?",
            (
                json.dumps(legacy_terminal, separators=(",", ":"), sort_keys=True),
                terminal_row["id"],
            ),
        )
        connection.commit()
        connection.execute("PRAGMA foreign_keys = ON")


def test_task5_decisions_receipt_events_and_scopes_migrate_without_loss(
    tmp_path,
) -> None:
    database_path = tmp_path / "task5-to-task6.sqlite3"
    store = SQLiteStore(database_path)
    provider = HotelSimulator(store=store)
    orchestrator = RecoveryOrchestrator(store=store, hotel_provider=provider)
    completed_pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    completed_id = completed_pending.recovery.recovery_id
    completed_request = _approval_request(completed_pending, "legacy-completed")
    completed_response = asyncio.run(
        orchestrator.approve_decision(completed_id, completed_request)
    )
    claimed_pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    claimed_id = claimed_pending.recovery.recovery_id
    claimed_request = _approval_request(claimed_pending, "legacy-claimed")
    store.claim_decision(claimed_id, claimed_request)
    completed_event_count = len(store.list_events(completed_id))
    claimed_event_count = len(store.list_events(claimed_id))
    store.close()

    _downgrade_to_task5_decisions_and_receipt(database_path)
    migrated = SQLiteStore(database_path)

    completed_claim = migrated.claim_decision(completed_id, completed_request)
    assert completed_claim.request.decision == "approve"
    assert completed_claim.response == completed_response
    assert migrated.get_recovery(completed_id).status is RecoveryStatus.COMPLETED
    completed_receipt = migrated.get_receipt(completed_id)
    assert completed_receipt.decision == "approved"
    assert completed_receipt.permission_revoked is True
    assert completed_receipt.scope_closed is True
    assert migrated.get_permission_scope(completed_id).status == "revoked"
    assert len(migrated.list_events(completed_id)) == completed_event_count
    assert len(
        [event for event in migrated.list_events(completed_id) if event.terminal]
    ) == 1

    claimed_claim = migrated.claim_decision(claimed_id, claimed_request)
    assert claimed_claim.request.decision == "approve"
    assert claimed_claim.response is None
    claimed_scope = migrated.get_permission_scope(claimed_id)
    assert claimed_scope.status == "active"
    assert claimed_scope.activated_at is not None
    assert claimed_scope.revoked_at is None
    assert migrated.count_executions(claimed_id) == 0
    assert len(migrated.list_events(claimed_id)) == claimed_event_count
    with sqlite3.connect(database_path) as connection:
        decision_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(approval_decisions)"
            ).fetchall()
        }
        assert "decision" in decision_columns
        assert connection.execute("SELECT COUNT(*) FROM approval_decisions").fetchone() == (
            2,
        )
        assert connection.execute("SELECT COUNT(*) FROM permission_scopes").fetchone() == (
            2,
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
