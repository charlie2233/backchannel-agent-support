from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from server.models import ApprovalDecisionRequest, ExecutionMode, RecoveryStatus
from server.orchestrator import RecoveryOrchestrator
from server.providers.hotel_simulator import HotelSimulator
from server.store import ReceiptTransitionError, SQLiteStore


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


def _decline_request(pending, decision_id: str) -> ApprovalDecisionRequest:
    approval = pending.recovery.pending_approval
    assert approval is not None
    return ApprovalDecisionRequest(
        decision="decline",
        clientDecisionId=decision_id,
        remedyId=approval.remedy_id,
        remedyDigest=approval.remedy_digest,
        toolCallId=approval.tool_call_id,
    )


def _insert_decline_dispatch_evidence(
    store: SQLiteStore,
    *,
    recovery_id: str,
    request: ApprovalDecisionRequest,
    evidence_id: str,
) -> None:
    observed_at = store._now().isoformat()
    with sqlite3.connect(store._database_path) as connection:
        connection.execute(
            """
            INSERT INTO executions (
                id, recovery_id, idempotency_key, status, provider_execution,
                request_digest, tool_call_id, remedy_digest, result_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, 'completed', 1, ?, ?, ?, ?, ?, ?)
            """,
            (
                evidence_id,
                recovery_id,
                evidence_id,
                "a" * 64,
                request.tool_call_id,
                request.remedy_digest,
                json.dumps(
                    {
                        "dispatch_id": evidence_id,
                        "status": "confirmed",
                        "simulated": True,
                        "provider_result": "Durable dispatch evidence.",
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                observed_at,
                observed_at,
            ),
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
        connection.execute("DELETE FROM receipt_provenance_migrations")
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
        assert "model_metadata_revision" in pending_columns
        assert connection.execute(
            "SELECT DISTINCT model_metadata_revision FROM pending_approvals"
        ).fetchall() == [(0,)]
        assert connection.execute("SELECT COUNT(*) FROM recoveries").fetchone() == (2,)
        assert connection.execute("SELECT COUNT(*) FROM pending_approvals").fetchone() == (
            2,
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_current_terminal_receipt_bytes_are_stable_across_reopen(tmp_path) -> None:
    database_path = tmp_path / "task7-receipt-stable.sqlite3"
    store = SQLiteStore(database_path)
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(store=store),
    )
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    asyncio.run(
        orchestrator.approve_decision(
            pending.recovery.recovery_id,
            _approval_request(pending),
        )
    )
    store.close()

    with sqlite3.connect(database_path) as connection:
        raw = json.loads(
            connection.execute("SELECT receipt_json FROM receipts").fetchone()[0]
        )
        stable_bytes = json.dumps(raw, indent=2, ensure_ascii=False)
        connection.execute("UPDATE receipts SET receipt_json = ?", (stable_bytes,))

    reopened = SQLiteStore(database_path)
    reopened.close()
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT receipt_json FROM receipts").fetchone()[0] == (
            stable_bytes
        )


@pytest.mark.parametrize("decision", ["approve", "decline"])
def test_modern_terminal_marker_removal_is_rejected_without_repair(
    tmp_path,
    decision: str,
) -> None:
    database_path = tmp_path / f"task7-modern-terminal-marker-{decision}.sqlite3"
    store = SQLiteStore(database_path)
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(store=store),
    )
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    recovery_id = pending.recovery.recovery_id
    if decision == "approve":
        asyncio.run(
            orchestrator.approve_decision(
                recovery_id,
                _approval_request(pending),
            )
        )
    else:
        asyncio.run(
            orchestrator.decline_decision(
                recovery_id,
                _decline_request(pending, "task7-modern-terminal-declined"),
            )
        )
    store.close()

    with sqlite3.connect(database_path) as connection:
        terminal_count = connection.execute(
            """
            SELECT COUNT(*) FROM events
            WHERE recovery_id = ? AND terminal = 1
            """,
            (recovery_id,),
        ).fetchone()
        assert terminal_count == (1,)
        connection.execute(
            """
            UPDATE events SET terminal = 0
            WHERE recovery_id = ? AND terminal = 1
            """,
            (recovery_id,),
        )
        assert connection.execute(
            """
            SELECT COUNT(*) FROM events
            WHERE recovery_id = ? AND terminal = 1
            """,
            (recovery_id,),
        ).fetchone() == (0,)
        tampered_rows = connection.execute(
            """
            SELECT id, recovery_id, seq, type, terminal, data_json, created_at,
                   CAST(type AS BLOB), CAST(data_json AS BLOB),
                   CAST(created_at AS BLOB)
            FROM events
            WHERE recovery_id = ?
            ORDER BY seq
            """,
            (recovery_id,),
        ).fetchall()

    with pytest.raises(ReceiptTransitionError, match="terminal evidence"):
        SQLiteStore(database_path)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            """
            SELECT COUNT(*) FROM events
            WHERE recovery_id = ? AND terminal = 1
            """,
            (recovery_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            """
            SELECT id, recovery_id, seq, type, terminal, data_json, created_at,
                   CAST(type AS BLOB), CAST(data_json AS BLOB),
                   CAST(created_at AS BLOB)
            FROM events
            WHERE recovery_id = ?
            ORDER BY seq
            """,
            (recovery_id,),
        ).fetchall() == tampered_rows


def test_legacy_events_without_terminal_column_are_backfilled_once_without_drift(
    tmp_path,
) -> None:
    database_path = tmp_path / "task7-legacy-events-terminal.sqlite3"
    store = SQLiteStore(database_path)
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(store=store),
    )
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    recovery_id = pending.recovery.recovery_id
    asyncio.run(orchestrator.approve_decision(recovery_id, _approval_request(pending)))
    store.close()

    row_projection = """
        SELECT id, recovery_id, seq, type, data_json, created_at,
               CAST(recovery_id AS BLOB), CAST(type AS BLOB),
               CAST(data_json AS BLOB), CAST(created_at AS BLOB)
        FROM events
        WHERE recovery_id = ?
        ORDER BY seq
    """
    migrated_row_projection = """
        SELECT id, recovery_id, seq, type, terminal, data_json, created_at,
               CAST(recovery_id AS BLOB), CAST(type AS BLOB),
               CAST(data_json AS BLOB), CAST(created_at AS BLOB)
        FROM events
        WHERE recovery_id = ?
        ORDER BY seq
    """
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        expected_rows = connection.execute(
            row_projection,
            (recovery_id,),
        ).fetchall()
        assert connection.execute(
            """
            SELECT COUNT(*) FROM events
            WHERE recovery_id = ? AND terminal = 1
            """,
            (recovery_id,),
        ).fetchone() == (1,)

        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            CREATE TABLE events_task7_legacy (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recovery_id TEXT NOT NULL
                    REFERENCES recoveries(id) ON DELETE CASCADE,
                seq INTEGER NOT NULL CHECK (seq >= 1),
                type TEXT NOT NULL,
                data_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (recovery_id, seq)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO events_task7_legacy (
                id, recovery_id, seq, type, data_json, created_at
            )
            SELECT id, recovery_id, seq, type, data_json, created_at
            FROM events
            ORDER BY id
            """
        )
        connection.execute("DROP TABLE events")
        connection.execute(
            "ALTER TABLE events_task7_legacy RENAME TO events"
        )
        connection.execute(
            """
            CREATE INDEX events_recovery_seq_idx
            ON events(recovery_id, seq)
            """
        )
        connection.commit()
        connection.execute("PRAGMA foreign_keys = ON")

        event_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(events)").fetchall()
        }
        assert "terminal" not in event_columns
        assert connection.execute(row_projection, (recovery_id,)).fetchall() == (
            expected_rows
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)

    migrated = SQLiteStore(database_path)
    migrated.close()

    with sqlite3.connect(database_path) as connection:
        event_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(events)").fetchall()
        }
        assert "terminal" in event_columns
        assert connection.execute(row_projection, (recovery_id,)).fetchall() == (
            expected_rows
        )
        migrated_rows = connection.execute(
            migrated_row_projection,
            (recovery_id,),
        ).fetchall()
        assert [(row[2], row[4]) for row in migrated_rows] == [
            (seq, int(seq == len(expected_rows)))
            for seq in range(1, len(expected_rows) + 1)
        ]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)

    migrated_database_bytes = database_path.read_bytes()
    reopened = SQLiteStore(database_path)
    reopened.close()
    assert database_path.read_bytes() == migrated_database_bytes

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            migrated_row_projection,
            (recovery_id,),
        ).fetchall() == migrated_rows
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


@pytest.mark.parametrize("tamper_mode", ["replace", "remove"])
def test_current_terminal_receipt_provenance_tampering_is_rejected_on_reopen(
    tmp_path,
    tamper_mode: str,
) -> None:
    database_path = tmp_path / "task7-receipt-tamper.sqlite3"
    store = SQLiteStore(database_path)
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(store=store),
    )
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    asyncio.run(
        orchestrator.approve_decision(
            pending.recovery.recovery_id,
            _approval_request(pending),
        )
    )
    store.close()

    with sqlite3.connect(database_path) as connection:
        receipt = json.loads(
            connection.execute("SELECT receipt_json FROM receipts").fetchone()[0]
        )
        if tamper_mode == "replace":
            receipt["rootTraceId"] = f"qa_trace_{'0' * 32}"
        else:
            for key in (
                "rootTraceId",
                "sdkVersion",
                "protocolVersion",
                "agentGraphVersion",
                "promptToolSchemaHash",
            ):
                receipt.pop(key)
        connection.execute(
            "UPDATE receipts SET receipt_json = ?",
            (json.dumps(receipt, separators=(",", ":"), sort_keys=True),),
        )

    with pytest.raises(ReceiptTransitionError, match="provenance"):
        SQLiteStore(database_path)


@pytest.mark.parametrize("tamper_mode", ["replace", "remove"])
def test_current_terminal_receipt_semantics_must_match_durable_completion(
    tmp_path,
    tamper_mode: str,
) -> None:
    database_path = tmp_path / f"task7-receipt-semantic-{tamper_mode}.sqlite3"
    store = SQLiteStore(database_path)
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(store=store),
    )
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    asyncio.run(
        orchestrator.approve_decision(
            pending.recovery.recovery_id,
            _approval_request(pending),
        )
    )
    store.close()

    with sqlite3.connect(database_path) as connection:
        receipt = json.loads(
            connection.execute("SELECT receipt_json FROM receipts").fetchone()[0]
        )
        if tamper_mode == "replace":
            receipt.update(
                {
                    "decision": "declined",
                    "status": RecoveryStatus.OUTCOME_UNKNOWN.value,
                    "exactInterruptionRejected": True,
                }
            )
            receipt.pop("approvedRemedyDigest")
        else:
            receipt.pop("decision")
        connection.execute(
            "UPDATE receipts SET receipt_json = ?",
            (json.dumps(receipt, separators=(",", ":"), sort_keys=True),),
        )

    with pytest.raises(ReceiptTransitionError, match="evidence"):
        SQLiteStore(database_path)


def test_current_non_replay_receipt_requires_its_pending_envelope(tmp_path) -> None:
    database_path = tmp_path / "task7-receipt-missing-envelope.sqlite3"
    store = SQLiteStore(database_path)
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(store=store),
    )
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    asyncio.run(
        orchestrator.approve_decision(
            pending.recovery.recovery_id,
            _approval_request(pending),
        )
    )
    store.close()

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "DELETE FROM pending_approvals WHERE recovery_id = ?",
            (pending.recovery.recovery_id,),
        )

    with pytest.raises(ReceiptTransitionError, match="envelope"):
        SQLiteStore(database_path)


@pytest.mark.parametrize(
    ("field", "tampered_value"),
    [
        ("providerResult", "Tampered approved provider result."),
        ("boundary", "Tampered approved provider boundary."),
        ("authorizationSource", "Tampered approved authorization source."),
        ("verificationResults", ["Tampered approved verification claim."]),
    ],
)
def test_current_approved_receipt_narrative_claims_match_durable_evidence(
    tmp_path,
    field: str,
    tampered_value: str | list[str],
) -> None:
    database_path = tmp_path / f"task7-approved-{field}.sqlite3"
    store = SQLiteStore(database_path)
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(store=store),
    )
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    recovery_id = pending.recovery.recovery_id
    asyncio.run(orchestrator.approve_decision(recovery_id, _approval_request(pending)))
    store.close()

    with sqlite3.connect(database_path) as connection:
        receipt = json.loads(
            connection.execute(
                "SELECT receipt_json FROM receipts WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone()[0]
        )
        receipt[field] = tampered_value
        connection.execute(
            "UPDATE receipts SET receipt_json = ? WHERE recovery_id = ?",
            (
                json.dumps(receipt, separators=(",", ":"), sort_keys=True),
                recovery_id,
            ),
        )

    with pytest.raises(ReceiptTransitionError, match="evidence"):
        SQLiteStore(database_path)


@pytest.mark.parametrize(
    ("terminal_kind", "field", "tampered_value"),
    [
        ("closed", "providerResult", "Tampered claim: provider action completed."),
        ("closed", "boundary", "Tampered claim: real provider boundary."),
        ("closed", "authorizationSource", "Tampered authorization source."),
        ("closed", "verificationResults", ["Tampered verification claim."]),
        ("outcome_unknown", "providerResult", "Tampered claim: no dispatch began."),
        ("outcome_unknown", "verificationResults", ["Tampered cancellation claim."]),
    ],
)
def test_current_decline_receipt_narrative_claims_match_durable_evidence(
    tmp_path,
    terminal_kind: str,
    field: str,
    tampered_value: str | list[str],
) -> None:
    database_path = tmp_path / f"task7-decline-{terminal_kind}-{field}.sqlite3"
    store = SQLiteStore(database_path)
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(store=store),
    )
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    recovery_id = pending.recovery.recovery_id
    request = _decline_request(pending, f"decline-{terminal_kind}-{field}")
    if terminal_kind == "outcome_unknown":
        _insert_decline_dispatch_evidence(
            store,
            recovery_id=recovery_id,
            request=request,
            evidence_id=f"execution-{field}",
        )
    asyncio.run(orchestrator.decline_decision(recovery_id, request))
    store.close()

    with sqlite3.connect(database_path) as connection:
        receipt = json.loads(
            connection.execute(
                "SELECT receipt_json FROM receipts WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone()[0]
        )
        receipt[field] = tampered_value
        connection.execute(
            "UPDATE receipts SET receipt_json = ? WHERE recovery_id = ?",
            (
                json.dumps(receipt, separators=(",", ":"), sort_keys=True),
                recovery_id,
            ),
        )

    with pytest.raises(ReceiptTransitionError, match="evidence"):
        SQLiteStore(database_path)
