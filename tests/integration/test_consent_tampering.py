from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from server.config import RuntimeSettings
from server.digest import remedy_consent_digest
from server.main import create_app
from server.models import ApprovalDecisionRequest, ExecutionMode, RecoveryStatus
from server.orchestrator import RecoveryOrchestrator
from server.providers.hotel_simulator import HotelSimulator
from server.store import ApprovalDecisionError, SQLiteStore


def _decision_payload(snapshot: dict[str, object], decision_id: str) -> dict[str, str]:
    approval = snapshot["pendingApproval"]
    assert isinstance(approval, dict)
    return {
        "decision": "approve",
        "clientDecisionId": decision_id,
        "remedyId": str(approval["remedyId"]),
        "remedyDigest": str(approval["remedyDigest"]),
        "toolCallId": str(approval["toolCallId"]),
    }


def _create_api_recovery(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/api/recoveries",
        json={"scenarioId": "hotel", "executionMode": "sdk_stub"},
    )
    assert response.status_code == 201
    return response.json()


def test_pending_snapshot_persists_exact_public_consent_without_sdk_state(
    tmp_path,
) -> None:
    database_path = tmp_path / "consent-snapshot.sqlite3"
    store = SQLiteStore(database_path)
    provider = HotelSimulator(store=store)
    orchestrator = RecoveryOrchestrator(store=store, hotel_provider=provider)

    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    recovery_id = pending.recovery.recovery_id
    snapshot = store.get_recovery(recovery_id)
    approval = snapshot.pending_approval

    assert snapshot.status is RecoveryStatus.PENDING_APPROVAL
    assert approval is not None
    assert approval.remedy_id == "remedy-king-room"
    assert approval.remedy_digest.startswith("sha256:")
    assert len(approval.remedy_digest) == 71
    assert approval.terms.model_dump(mode="json", by_alias=True) == {
        "bookingId": "booking-demo-001",
        "action": "replace_room",
        "replacement": {"fromRoomType": "double", "toRoomType": "king"},
        "stay": {"checkIn": "2026-08-14", "checkOut": "2026-08-16"},
        "currency": "USD",
    }
    assert type(approval.cost_delta_minor) is int
    assert approval.cost_delta_minor == 0
    assert approval.changed_fields == ["room_type"]
    assert approval.provider_commitments == [
        "No additional charge",
        "Preserve booking dates",
    ]
    assert approval.expiry.tzinfo is not None
    assert approval.expiry.utcoffset() == UTC.utcoffset(approval.expiry)
    assert approval.hard_constraint_satisfied is True
    assert approval.delegated_authority_satisfied is True
    assert approval.tool_call_id == pending.sdk_result.interruptions[0].call_id
    assert approval.execution_started is False
    assert provider.dispatch_count == 0
    assert store.count_executions(recovery_id) == 0

    envelope = store.get_pending_approval(recovery_id)
    assert envelope.remedy_id == approval.remedy_id
    assert envelope.consent_digest == approval.remedy_digest
    assert len(envelope.action_digest) == 64
    assert not envelope.action_digest.startswith("sha256:")
    assert envelope.action_digest not in approval.remedy_digest

    public_json = snapshot.model_dump_json(by_alias=True, exclude_none=True)
    public_keys = json.dumps(json.loads(public_json), sort_keys=True).lower()
    assert "pendingapproval" in public_keys
    assert "state_json" not in public_keys
    assert "statejson" not in public_keys
    assert "consumer_proof" not in public_keys
    assert "provider_proof" not in public_keys
    assert '"prompt"' not in public_keys

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM remedies WHERE recovery_id = ?", (recovery_id,)
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM pending_approvals WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM events WHERE recovery_id = ? AND type = ?",
            (recovery_id, "approval.requested"),
        ).fetchone() == (1,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_task4_pending_rows_are_preserved_but_marked_incompatible(tmp_path) -> None:
    database_path = tmp_path / "task4-consent-migration.sqlite3"
    timestamp = "2026-07-18T20:00:00+00:00"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE recoveries (
                id TEXT PRIMARY KEY,
                scenario_id TEXT NOT NULL,
                execution_mode TEXT NOT NULL,
                status TEXT NOT NULL,
                current_step INTEGER NOT NULL,
                current_step_summary TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE remedies (
                id TEXT PRIMARY KEY,
                recovery_id TEXT NOT NULL REFERENCES recoveries(id) ON DELETE CASCADE,
                terms_json TEXT NOT NULL,
                digest TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE pending_approvals (
                tool_call_id TEXT PRIMARY KEY,
                recovery_id TEXT NOT NULL REFERENCES recoveries(id) ON DELETE CASCADE,
                sdk_version TEXT NOT NULL,
                protocol_version TEXT NOT NULL,
                agent_graph_version TEXT NOT NULL,
                definition_digest TEXT NOT NULL,
                root_trace_id TEXT NOT NULL,
                execution_mode TEXT NOT NULL,
                remedy_digest TEXT NOT NULL,
                state_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recovery_id TEXT NOT NULL REFERENCES recoveries(id) ON DELETE CASCADE,
                seq INTEGER NOT NULL,
                type TEXT NOT NULL,
                terminal INTEGER NOT NULL DEFAULT 0,
                data_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (recovery_id, seq)
            );
            """
        )
        connection.execute(
            "INSERT INTO recoveries VALUES "
            "('task4-recovery', 'hotel', 'sdk_stub', 'pending_approval', 3, "
            "'Legacy pending approval.', ?, ?)",
            (timestamp, timestamp),
        )
        connection.execute(
            "INSERT INTO remedies VALUES "
            "('task4-remedy', 'task4-recovery', '{}', NULL, 'pending', ?)",
            (timestamp,),
        )
        connection.execute(
            "INSERT INTO pending_approvals VALUES "
            "('task4-call', 'task4-recovery', '0.18.3', 'v1', 'v1', 'definition', "
            "'qa_trace_1234567890abcdef1234567890abcdef', 'sdk_stub', "
            "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', "
            "'{\"legacy\":true}', 'pending', ?, ?)",
            (timestamp, timestamp),
        )
        connection.execute(
            "INSERT INTO events "
            "(recovery_id, seq, type, terminal, data_json, created_at) VALUES "
            "('task4-recovery', 1, 'approval.requested', 0, '{}', ?)",
            (timestamp,),
        )

    migrated = SQLiteStore(database_path)
    envelope = migrated.get_pending_approval("task4-recovery")

    assert envelope.sdk_version == "legacy-incompatible"
    assert envelope.action_digest == "a" * 64
    assert envelope.remedy_id == "legacy-incompatible"
    assert envelope.consent_digest == "legacy-incompatible"
    assert envelope.state_json == {"legacy": True}
    assert migrated.get_recovery("task4-recovery").pending_approval is None
    assert len(migrated.list_events("task4-recovery")) == 1

    provider = HotelSimulator(store=migrated)
    orchestrator = RecoveryOrchestrator(store=migrated, hotel_provider=provider)
    current = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    approval = current.recovery.pending_approval
    assert approval is not None
    response = asyncio.run(
        orchestrator.approve_decision(
            current.recovery.recovery_id,
            ApprovalDecisionRequest(
                decision="approve",
                clientDecisionId="post-task4-migration",
                remedyId=approval.remedy_id,
                remedyDigest=approval.remedy_digest,
                toolCallId=approval.tool_call_id,
            ),
        )
    )

    assert response.status == "completed"
    assert provider.dispatch_count == 1
    migrated.close()

    reopened = SQLiteStore(database_path)
    assert reopened.get_pending_approval("task4-recovery").state_json == {
        "legacy": True
    }
    assert reopened.get_receipt(current.recovery.recovery_id).provider_execution is True
    with sqlite3.connect(database_path) as connection:
        pending_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(pending_approvals)"
            ).fetchall()
        }
        assert pending_columns == {
            "tool_call_id",
            "recovery_id",
            "sdk_version",
            "protocol_version",
            "agent_graph_version",
            "definition_digest",
            "root_trace_id",
            "execution_mode",
            "action_digest",
            "remedy_id",
            "consent_digest",
            "model_metadata_json",
            "model_metadata_revision",
            "state_json",
            "status",
            "created_at",
            "updated_at",
        }
        assert connection.execute("SELECT COUNT(*) FROM pending_approvals").fetchone() == (
            2,
        )
        assert connection.execute("SELECT COUNT(*) FROM remedies").fetchone() == (2,)
        assert connection.execute(
            "SELECT COUNT(*) FROM events WHERE recovery_id = 'task4-recovery'"
        ).fetchone() == (1,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    reopened.close()


def test_remedy_envelope_and_event_roll_back_as_one_atomic_transition(tmp_path) -> None:
    database_path = tmp_path / "atomic-consent.sqlite3"
    store = SQLiteStore(database_path)
    provider = HotelSimulator(store=store)
    orchestrator = RecoveryOrchestrator(store=store, hotel_provider=provider)
    first = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    first_id = first.recovery.recovery_id
    first_envelope = store.get_pending_approval(first_id)
    first_consent = store.get_remedy_consent(first_id)

    second_id = str(uuid4())
    store.create_recovery(
        recovery_id=second_id,
        scenario_id=first.recovery.scenario_id,
        execution_mode=ExecutionMode.SDK_STUB,
        current_step=0,
        current_step_summary="Second recovery before consent transition.",
    )
    conflicting_envelope = replace(first_envelope, recovery_id=second_id)
    second_consent = replace(first_consent, recovery_id=second_id)

    with pytest.raises(sqlite3.IntegrityError):
        store.record_transition(
            second_id,
            status=RecoveryStatus.PENDING_APPROVAL,
            current_step=3,
            current_step_summary="Must roll back.",
            event_type="approval.requested",
            event_data={"providerExecution": False},
            pending_approval=conflicting_envelope,
            remedy_consent=second_consent,
        )

    second = store.get_recovery(second_id)
    assert second.status is RecoveryStatus.IN_PROGRESS
    assert second.pending_approval is None
    assert [event.type for event in store.list_events(second_id)] == ["recovery.created"]
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM remedies WHERE recovery_id = ?", (second_id,)
        ).fetchone() == (0,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_repeated_recoveries_can_reuse_the_authoritative_remedy_identity(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "repeated-remedy-id.sqlite3")
    provider = HotelSimulator(store=store)
    orchestrator = RecoveryOrchestrator(store=store, hotel_provider=provider)

    first = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    second = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )

    assert first.recovery.recovery_id != second.recovery.recovery_id
    assert first.recovery.pending_approval is not None
    assert second.recovery.pending_approval is not None
    assert first.recovery.pending_approval.remedy_id == "remedy-king-room"
    assert second.recovery.pending_approval.remedy_id == "remedy-king-room"
    assert (
        first.recovery.pending_approval.remedy_digest
        != second.recovery.pending_approval.remedy_digest
    )


def test_terms_mutated_after_display_fail_digest_recheck_with_zero_execution(
    tmp_path,
) -> None:
    database_path = tmp_path / "mutated-terms.sqlite3"
    store = SQLiteStore(database_path)
    provider = HotelSimulator(store=store)
    with TestClient(
        create_app(
            RuntimeSettings(live_ready=False),
            store=store,
            hotel_provider=provider,
        )
    ) as client:
        snapshot = _create_api_recovery(client)
        recovery_id = str(snapshot["recoveryId"])
        payload = _decision_payload(snapshot, "terms-tamper")
        approval = snapshot["pendingApproval"]
        assert isinstance(approval, dict)
        terms = approval["terms"]
        assert isinstance(terms, dict)
        mutated_terms = json.loads(json.dumps(terms))
        mutated_terms["replacement"]["toRoomType"] = "suite"
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                "UPDATE remedies SET terms_json = ? WHERE recovery_id = ?",
                (json.dumps(mutated_terms, sort_keys=True), recovery_id),
            )

        response = client.post(
            f"/api/recoveries/{recovery_id}/decisions",
            json=payload,
        )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "remedy_digest_mismatch"
    assert provider.dispatch_count == 0
    assert store.count_decisions(recovery_id) == 0
    assert store.count_executions(recovery_id) == 0


def test_displayed_commitment_cannot_diverge_from_restored_sdk_arguments(
    tmp_path,
) -> None:
    database_path = tmp_path / "restored-consent-mismatch.sqlite3"
    store = SQLiteStore(database_path)
    provider = HotelSimulator(store=store)
    with TestClient(
        create_app(
            RuntimeSettings(live_ready=False),
            store=store,
            hotel_provider=provider,
        )
    ) as client:
        initial = _create_api_recovery(client)
        recovery_id = str(initial["recoveryId"])
        consent = store.get_remedy_consent(recovery_id)
        displayed_commitments = sorted(
            [*consent.provider_commitments, "Late checkout guaranteed"]
        )
        displayed_evidence = consent.evidence.model_dump(mode="json")
        displayed_evidence["remedy"][
            "provider_commitments"
        ] = displayed_commitments
        displayed_digest = remedy_consent_digest(
            {
                "recoveryId": recovery_id,
                "remedyId": consent.remedy_id,
                "terms": consent.terms.model_dump(mode="json", by_alias=True),
                "costDeltaMinor": consent.cost_delta_minor,
                "changedFields": list(consent.changed_fields),
                "providerCommitments": displayed_commitments,
                "expiry": consent.expiry,
            }
        )
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                """
                UPDATE remedies
                SET provider_commitments_json = ?, evidence_json = ?, digest = ?
                WHERE recovery_id = ?
                """,
                (
                    json.dumps(displayed_commitments),
                    json.dumps(displayed_evidence, sort_keys=True),
                    displayed_digest,
                    recovery_id,
                ),
            )
            connection.execute(
                "UPDATE pending_approvals SET consent_digest = ? WHERE recovery_id = ?",
                (displayed_digest, recovery_id),
            )

        displayed = client.get(f"/api/recoveries/{recovery_id}").json()
        assert displayed["pendingApproval"]["providerCommitments"] == (
            displayed_commitments
        )
        assert displayed["pendingApproval"]["remedyDigest"] == displayed_digest
        response = client.post(
            f"/api/recoveries/{recovery_id}/decisions",
            json=_decision_payload(displayed, "restored-consent-mismatch"),
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "resume_incompatible"
    assert provider.dispatch_count == 0
    assert store.count_executions(recovery_id) == 0


def test_expired_exact_digest_fails_closed(tmp_path) -> None:
    database_path = tmp_path / "expired-consent.sqlite3"
    store = SQLiteStore(database_path)
    provider = HotelSimulator(store=store)
    with TestClient(
        create_app(
            RuntimeSettings(live_ready=False),
            store=store,
            hotel_provider=provider,
        )
    ) as client:
        snapshot = _create_api_recovery(client)
        recovery_id = str(snapshot["recoveryId"])
        consent = store.get_remedy_consent(recovery_id)
        expiry = datetime.now(UTC) - timedelta(seconds=1)
        digest = remedy_consent_digest(
            {
                "recoveryId": recovery_id,
                "remedyId": consent.remedy_id,
                "terms": consent.terms.model_dump(mode="json", by_alias=True),
                "costDeltaMinor": consent.cost_delta_minor,
                "changedFields": list(consent.changed_fields),
                "providerCommitments": list(consent.provider_commitments),
                "expiry": expiry,
            }
        )
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                "UPDATE remedies SET expiry = ?, digest = ? WHERE recovery_id = ?",
                (expiry.isoformat(), digest, recovery_id),
            )
            connection.execute(
                "UPDATE pending_approvals SET consent_digest = ? WHERE recovery_id = ?",
                (digest, recovery_id),
            )
        payload = {
            **_decision_payload(snapshot, "expired-decision"),
            "remedyDigest": digest,
        }

        response = client.post(
            f"/api/recoveries/{recovery_id}/decisions",
            json=payload,
        )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "remedy_expired"
    assert provider.dispatch_count == 0
    assert store.count_decisions(recovery_id) == 0
    assert store.count_executions(recovery_id) == 0


@pytest.mark.parametrize(
    ("regression", "expected_code"),
    [
        ("changed_fields", "constraint_denied"),
        ("authority_cost", "authority_denied"),
    ],
)
def test_recomputed_digest_cannot_bypass_current_policy_or_authority(
    tmp_path,
    regression: str,
    expected_code: str,
) -> None:
    database_path = tmp_path / f"recomputed-{regression}.sqlite3"
    store = SQLiteStore(database_path)
    provider = HotelSimulator(store=store)
    with TestClient(
        create_app(
            RuntimeSettings(live_ready=False),
            store=store,
            hotel_provider=provider,
        )
    ) as client:
        snapshot = _create_api_recovery(client)
        recovery_id = str(snapshot["recoveryId"])
        consent = store.get_remedy_consent(recovery_id)
        evidence = consent.evidence.model_dump(mode="json")
        changed_fields = list(consent.changed_fields)
        cost_delta_minor = consent.cost_delta_minor
        if regression == "changed_fields":
            changed_fields = ["booking_dates", "room_type"]
            evidence["remedy"]["changed_fields"] = changed_fields
        else:
            cost_delta_minor = 1
            evidence["remedy"]["cost_delta_minor"] = cost_delta_minor
        digest = remedy_consent_digest(
            {
                "recoveryId": recovery_id,
                "remedyId": consent.remedy_id,
                "terms": consent.terms.model_dump(mode="json", by_alias=True),
                "costDeltaMinor": cost_delta_minor,
                "changedFields": changed_fields,
                "providerCommitments": list(consent.provider_commitments),
                "expiry": consent.expiry,
            }
        )
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                """
                UPDATE remedies
                SET cost_delta_minor = ?, changed_fields_json = ?,
                    evidence_json = ?, digest = ?
                WHERE recovery_id = ?
                """,
                (
                    cost_delta_minor,
                    json.dumps(changed_fields),
                    json.dumps(evidence, sort_keys=True),
                    digest,
                    recovery_id,
                ),
            )
            connection.execute(
                "UPDATE pending_approvals SET consent_digest = ? WHERE recovery_id = ?",
                (digest, recovery_id),
            )
        payload = {
            **_decision_payload(snapshot, f"recomputed-{regression}"),
            "remedyDigest": digest,
        }

        response = client.post(
            f"/api/recoveries/{recovery_id}/decisions",
            json=payload,
        )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == expected_code
    assert provider.dispatch_count == 0
    assert store.count_decisions(recovery_id) == 0
    assert store.count_executions(recovery_id) == 0


def test_same_claim_resumes_after_process_loss_before_sdk_restore(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "claim-restart.sqlite3"
    store = SQLiteStore(database_path)
    provider = HotelSimulator(store=store)
    orchestrator = RecoveryOrchestrator(store=store, hotel_provider=provider)
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    approval = pending.recovery.pending_approval
    assert approval is not None
    request = ApprovalDecisionRequest(
        decision="approve",
        clientDecisionId="restartable-claim",
        remedyId=approval.remedy_id,
        remedyDigest=approval.remedy_digest,
        toolCallId=approval.tool_call_id,
    )

    class SimulatedProcessLoss(RuntimeError):
        pass

    async def crash_before_restore(*_args, **_kwargs):
        raise SimulatedProcessLoss("process lost after durable claim")

    monkeypatch.setattr(orchestrator, "_resume_claimed_approval", crash_before_restore)
    with pytest.raises(SimulatedProcessLoss, match="durable claim"):
        asyncio.run(
            orchestrator.approve_decision(pending.recovery.recovery_id, request)
        )

    assert store.count_decisions(pending.recovery.recovery_id) == 1
    assert store.count_executions(pending.recovery.recovery_id) == 0
    assert store.get_recovery(pending.recovery.recovery_id).pending_approval is None
    conflicting_retry = request.model_copy(update={"tool_call_id": "mutated-call"})
    with pytest.raises(ApprovalDecisionError) as conflict:
        asyncio.run(
            orchestrator.approve_decision(
                pending.recovery.recovery_id,
                conflicting_retry,
            )
        )
    assert conflict.value.code == "decision_id_conflict"
    store.close()

    fresh_store = SQLiteStore(database_path)
    fresh_provider = HotelSimulator(store=fresh_store)
    fresh_orchestrator = RecoveryOrchestrator(
        store=fresh_store,
        hotel_provider=fresh_provider,
    )
    response = asyncio.run(
        fresh_orchestrator.approve_decision(pending.recovery.recovery_id, request)
    )

    assert response.status == "completed"
    assert fresh_provider.dispatch_count == 1
    assert fresh_store.count_decisions(pending.recovery.recovery_id) == 1
    assert fresh_store.count_executions(pending.recovery.recovery_id) == 1
