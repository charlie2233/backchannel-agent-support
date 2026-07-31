import asyncio
import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from server.models import (
    ApprovalDecisionRequest,
    ExecutionMode,
    RecoveryReceipt,
    RecoveryStatus,
    ScenarioId,
)
from server.orchestrator import RecoveryOrchestrator
from server.providers.hotel_contract import durable_hotel_dispatch_contract
from server.providers.hotel_simulator import HotelSimulator
from server.providers.quota_simulator import QuotaGrantResult
from server.store import (
    APPROVED_RECEIPT_AUTHORIZATION,
    APPROVED_RECEIPT_VERIFICATIONS,
    DurableExecution,
    ReceiptTransitionError,
    RecoveryNotFoundError,
    SQLiteStore,
    non_replay_receipt_boundary,
)

APPROVED_DIGEST = f"sha256:{'a' * 64}"
OTHER_VALID_DIGEST = f"sha256:{'b' * 64}"
QUOTA_BOUNDARY = (
    "Deterministic Agents SDK quota model and demo quota adapter only; "
    "no OpenAI model call or real provider quota change."
)
QUOTA_PROVIDER_RESULT = (
    "Demo quota adapter verified a temporary US burst grant; "
    "the base quota was unchanged."
)
QUOTA_AUTHORIZATION = (
    "Delegated quota authority covered the temporary grant; "
    "no human approval was requested."
)
QUOTA_VERIFICATIONS = [
    "Provider proof established the 1000 rpm base ceiling.",
    "The 1500 rpm temporary burst covered 1200 rpm demand in US for 900 seconds.",
    "The 250 USD-minor cost stayed within the delegated 500 USD-minor maximum.",
    "The base quota remained unchanged.",
    "Runtime permission was revoked after grant verification.",
]


def quota_sdk_receipt(recovery_id: str) -> RecoveryReceipt:
    return RecoveryReceipt(
        recoveryId=recovery_id,
        executionMode=ExecutionMode.SDK_STUB,
        status="completed",
        simulated=True,
        providerExecution=True,
        modelIds=[],
        rootTraceId=None,
        sdkVersion="0.18.3",
        protocolVersion="backchannel.quota.v1",
        agentGraphVersion="backchannel.quota-agent.v1",
        promptToolSchemaHash="c" * 64,
        boundary=QUOTA_BOUNDARY,
        providerResult=QUOTA_PROVIDER_RESULT,
        authorizationSource=QUOTA_AUTHORIZATION,
        verificationResults=QUOTA_VERIFICATIONS,
        decision=None,
        decisionRemedyDigest=None,
        executionCount=1,
        providerDispatchStarted=True,
        exactInterruptionRejected=False,
        permissionRevoked=True,
        scopeClosed=True,
        approvedRemedyDigest=None,
        quotaEvidence={
            "providerCeilingRpm": 1000,
            "recordedDemandRpm": 1200,
            "temporaryBurstRpm": 1500,
            "region": "US",
            "durationSeconds": 900,
            "extraCostMinor": 250,
            "delegatedAuthorityMaxMinor": 500,
            "currency": "USD",
            "hardConstraints": {
                "regionPreserved": True,
                "burstCoversDemand": True,
                "durationWithinLimit": True,
                "baseQuotaUnchanged": True,
            },
            "humanInterruptions": 0,
            "approvals": 0,
            "providerProofVerified": True,
            "grantVerified": True,
            "source": "sdk_simulator",
            "revocationEvidenceKind": "runtime_permission_revoked",
            "protocolSteps": [
                "Detect",
                "Prove",
                "Negotiate",
                "Authorize",
                "Execute",
                "Verify & seal",
            ],
        },
    )


def quota_provider_result() -> QuotaGrantResult:
    return QuotaGrantResult(
        dispatch_id="demo-quota-dispatch-atomic",
        grant_id="temporary-quota-grant-atomic",
        status="verified",
        simulated=True,
        provider_ceiling_rpm=1000,
        granted_burst_rpm=1500,
        region="US",
        duration_seconds=900,
        extra_cost_minor=250,
        currency="USD",
        provider_result=QUOTA_PROVIDER_RESULT,
    )


def quota_execution(recovery_id: str) -> DurableExecution:
    result = quota_provider_result()
    return DurableExecution(
        execution_id=f"quota-execution-{recovery_id}",
        recovery_id=recovery_id,
        idempotency_key=f"{recovery_id}:grant-quota-{recovery_id}",
        status="completed",
        provider_execution=True,
        request_digest="d" * 64,
        tool_call_id=f"grant-quota-{recovery_id}",
        remedy_digest=None,
        result_json=result.model_dump(mode="json"),
    )


def make_receipt(
    *, recovery_id: str, execution_mode: ExecutionMode
) -> RecoveryReceipt:
    decision_fields = (
        {
            "decision": "approved",
            "decisionRemedyDigest": APPROVED_DIGEST,
            "executionCount": 1,
            "providerDispatchStarted": True,
            "exactInterruptionRejected": False,
            "permissionRevoked": True,
            "scopeClosed": True,
            "approvedRemedyDigest": APPROVED_DIGEST,
        }
        if execution_mode is ExecutionMode.SDK_STUB
        else {}
    )
    return RecoveryReceipt(
        recoveryId=recovery_id,
        executionMode=execution_mode,
        status="completed",
        simulated=True,
        providerExecution=execution_mode is ExecutionMode.SDK_STUB,
        modelIds=[],
        boundary="Atomic receipt test boundary.",
        providerResult="Atomic receipt test result.",
        authorizationSource="Atomic receipt test authorization.",
        verificationResults=["Atomic receipt test verification."],
        **decision_fields,
    )


def create_replay_recovery(store: SQLiteStore, recovery_id: str) -> None:
    store.create_recovery(
        recovery_id=recovery_id,
        scenario_id=ScenarioId.HOTEL,
        execution_mode=ExecutionMode.REPLAY_FIXTURE,
        current_step=0,
        current_step_summary="Receipt atomicity test started.",
    )


def create_durable_sdk_execution(
    store: SQLiteStore,
) -> tuple[str, DurableExecution, RecoveryReceipt, str, int]:
    provider = HotelSimulator(store=store)
    orchestrator = RecoveryOrchestrator(store=store, hotel_provider=provider)
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    recovery_id = pending.recovery.recovery_id
    approval = pending.recovery.pending_approval
    assert approval is not None
    request = ApprovalDecisionRequest(
        decision="approve",
        clientDecisionId=f"decision-{recovery_id}",
        remedyId=approval.remedy_id,
        remedyDigest=approval.remedy_digest,
        toolCallId=approval.tool_call_id,
    )
    claim = store.claim_decision(recovery_id, request)
    owner_id = f"receipt-finalizer-{recovery_id}"
    lease = store.acquire_decision_resume(
        claim,
        resume_owner_id=owner_id,
        lease_duration=timedelta(minutes=1),
    )
    contract = durable_hotel_dispatch_contract(
        recovery_id=recovery_id,
        remedy=store.get_remedy_consent(recovery_id).evidence.remedy,
        tool_call_id=request.tool_call_id,
        remedy_digest=request.remedy_digest,
    )
    provider_result = contract.result_json["provider_result"]
    assert isinstance(provider_result, str)
    execution, dispatched = store.record_completed_execution(
        execution_id=contract.execution_id,
        recovery_id=recovery_id,
        idempotency_key=contract.idempotency_key,
        request_digest=contract.request_digest,
        tool_call_id=approval.tool_call_id,
        remedy_digest=approval.remedy_digest,
        action_digest=store.get_pending_approval(recovery_id).action_digest,
        resume_owner_id=owner_id,
        resume_generation=lease.resume_generation,
        result_json=contract.result_json,
    )
    assert dispatched is True
    envelope = store.get_pending_approval(recovery_id)
    receipt = RecoveryReceipt(
        recoveryId=recovery_id,
        executionMode=ExecutionMode.SDK_STUB,
        status="completed",
        simulated=True,
        providerExecution=True,
        modelIds=[],
        rootTraceId=envelope.root_trace_id,
        sdkVersion=envelope.sdk_version,
        protocolVersion=envelope.protocol_version,
        agentGraphVersion=envelope.agent_graph_version,
        promptToolSchemaHash=envelope.definition_digest,
        boundary=non_replay_receipt_boundary(ExecutionMode.SDK_STUB),
        providerResult=provider_result,
        authorizationSource=APPROVED_RECEIPT_AUTHORIZATION,
        verificationResults=list(APPROVED_RECEIPT_VERIFICATIONS),
        decision="approved",
        decisionRemedyDigest=approval.remedy_digest,
        executionCount=1,
        providerDispatchStarted=True,
        exactInterruptionRejected=False,
        permissionRevoked=True,
        scopeClosed=True,
        approvedRemedyDigest=approval.remedy_digest,
    )
    return recovery_id, execution, receipt, owner_id, lease.resume_generation


def assert_transition_was_atomic(
    store: SQLiteStore,
    recovery_id: str,
    *,
    original_event_count: int,
) -> None:
    snapshot = store.get_recovery(recovery_id)
    assert snapshot.status is RecoveryStatus.IN_PROGRESS
    assert snapshot.current_step == 0
    assert len(store.list_events(recovery_id)) == original_event_count
    with pytest.raises(RecoveryNotFoundError):
        store.get_receipt(recovery_id)


def test_receipt_recovery_id_mismatch_writes_nothing(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "receipt-id-mismatch.sqlite3")
    recovery_id = "recovery-a"
    create_replay_recovery(store, recovery_id)
    event_count = len(store.list_events(recovery_id))

    with pytest.raises(ReceiptTransitionError):
        store.record_transition(
            recovery_id,
            status=RecoveryStatus.COMPLETED,
            current_step=5,
            current_step_summary="Must roll back.",
            event_type="recovery.completed",
            event_data={"summary": "Must roll back."},
            receipt=make_receipt(
                recovery_id="recovery-b",
                execution_mode=ExecutionMode.REPLAY_FIXTURE,
            ),
        )

    assert_transition_was_atomic(store, recovery_id, original_event_count=event_count)


def test_receipt_execution_mode_mismatch_writes_nothing(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "receipt-mode-mismatch.sqlite3")
    recovery_id = "recovery-mode"
    create_replay_recovery(store, recovery_id)
    event_count = len(store.list_events(recovery_id))

    with pytest.raises(ReceiptTransitionError):
        store.record_transition(
            recovery_id,
            status=RecoveryStatus.COMPLETED,
            current_step=5,
            current_step_summary="Must roll back.",
            event_type="recovery.completed",
            event_data={"summary": "Must roll back."},
            receipt=make_receipt(
                recovery_id=recovery_id,
                execution_mode=ExecutionMode.SDK_STUB,
            ),
        )

    assert_transition_was_atomic(store, recovery_id, original_event_count=event_count)


def test_hotel_recovery_rejects_zero_decision_quota_receipt_atomically(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "hotel-quota-receipt.sqlite3")
    recovery_id = "hotel-with-quota-evidence"
    store.create_recovery(
        recovery_id=recovery_id,
        scenario_id=ScenarioId.HOTEL,
        execution_mode=ExecutionMode.SDK_STUB,
        current_step=0,
        current_step_summary="Hotel SDK recovery created.",
    )
    event_count = len(store.list_events(recovery_id))

    with pytest.raises(ReceiptTransitionError, match="scenario"):
        store.record_transition(
            recovery_id,
            status=RecoveryStatus.COMPLETED,
            current_step=5,
            current_step_summary="Must not accept quota evidence.",
            event_type="recovery.completed",
            event_data={"phase": "Verify & seal"},
            receipt=quota_sdk_receipt(recovery_id),
        )

    assert_transition_was_atomic(store, recovery_id, original_event_count=event_count)


def test_api_quota_recovery_rejects_terminal_receipt_without_quota_evidence(
    tmp_path,
) -> None:
    store = SQLiteStore(tmp_path / "quota-missing-evidence.sqlite3")
    recovery_id = "quota-missing-evidence"
    store.create_recovery(
        recovery_id=recovery_id,
        scenario_id=ScenarioId.API_QUOTA,
        execution_mode=ExecutionMode.SDK_STUB,
        current_step=0,
        current_step_summary="Quota trace started.",
    )
    event_count = len(store.list_events(recovery_id))
    missing_evidence = quota_sdk_receipt(recovery_id).model_copy(
        update={"quota_evidence": None}
    )

    with pytest.raises(ReceiptTransitionError, match="quota evidence"):
        store.record_transition(
            recovery_id,
            status=RecoveryStatus.COMPLETED,
            current_step=5,
            current_step_summary="Must not seal missing evidence.",
            event_type="recovery.completed",
            event_data={"summary": "Must not seal missing evidence."},
            receipt=missing_evidence,
        )

    assert_transition_was_atomic(store, recovery_id, original_event_count=event_count)


def test_completed_quota_graph_is_persisted_as_one_terminal_transaction(
    tmp_path,
) -> None:
    database_path = tmp_path / "quota-terminal-atomic.sqlite3"
    store = SQLiteStore(database_path)
    recovery_id = "quota-terminal-atomic"
    receipt = quota_sdk_receipt(recovery_id)

    snapshot = store.create_completed_quota_recovery(
        recovery_id=recovery_id,
        execution=quota_execution(recovery_id),
        receipt=receipt,
    )

    assert snapshot.status is RecoveryStatus.COMPLETED
    assert snapshot.current_step == 5
    assert store.get_receipt(recovery_id) == receipt
    events = store.list_events(recovery_id)
    assert len(events) == 7
    assert [event.data.get("phase") for event in events[1:]] == [
        "Detect",
        "Prove",
        "Negotiate",
        "Authorize",
        "Execute",
        "Verify & seal",
    ]
    assert [event.terminal for event in events] == [
        False,
        False,
        False,
        False,
        False,
        False,
        True,
    ]
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM recoveries").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone() == (7,)
        assert connection.execute("SELECT COUNT(*) FROM executions").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM receipts").fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM approval_decisions"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM pending_approvals"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT status FROM permission_scopes"
        ).fetchone() == ("revoked",)
        assert connection.execute(
            "SELECT COUNT(*) FROM receipt_provenance_migrations"
        ).fetchone() == (1,)


def test_completed_quota_graph_rolls_back_when_final_validation_fails(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "quota-terminal-rollback.sqlite3"
    store = SQLiteStore(database_path)
    recovery_id = "quota-terminal-rollback"
    original_validate = store._validate_durable_receipt_evidence

    def reject_after_writes(connection, receipt, *, phase) -> None:
        if receipt.quota_evidence is not None:
            raise ReceiptTransitionError("Injected quota final validation failure")
        original_validate(connection, receipt, phase=phase)

    monkeypatch.setattr(store, "_validate_durable_receipt_evidence", reject_after_writes)

    with pytest.raises(ReceiptTransitionError, match="Injected quota"):
        store.create_completed_quota_recovery(
            recovery_id=recovery_id,
            execution=quota_execution(recovery_id),
            receipt=quota_sdk_receipt(recovery_id),
        )

    with sqlite3.connect(database_path) as connection:
        for table in (
            "recoveries",
            "events",
            "executions",
            "receipts",
            "permission_scopes",
            "receipt_provenance_migrations",
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (
                0,
            )


def test_nonterminal_transition_with_receipt_writes_nothing(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "receipt-nonterminal.sqlite3")
    recovery_id = "recovery-nonterminal"
    create_replay_recovery(store, recovery_id)
    event_count = len(store.list_events(recovery_id))

    with pytest.raises(ReceiptTransitionError):
        store.record_transition(
            recovery_id,
            status=RecoveryStatus.PENDING_APPROVAL,
            current_step=3,
            current_step_summary="Must roll back.",
            event_type="approval.requested",
            event_data={"summary": "Must roll back."},
            receipt=make_receipt(
                recovery_id=recovery_id,
                execution_mode=ExecutionMode.REPLAY_FIXTURE,
            ),
        )

    assert_transition_was_atomic(store, recovery_id, original_event_count=event_count)


def test_finalizer_rejects_existing_receipt_with_another_approved_digest(
    tmp_path,
) -> None:
    database_path = tmp_path / "wrong-finalizer-receipt.sqlite3"
    store = SQLiteStore(database_path)
    recovery_id, execution, expected_receipt, owner_id, generation = (
        create_durable_sdk_execution(store)
    )
    wrong_receipt = expected_receipt.model_copy(
        update={"approved_remedy_digest": OTHER_VALID_DIGEST}
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO receipts (recovery_id, receipt_json, created_at) VALUES (?, ?, ?)",
            (
                recovery_id,
                wrong_receipt.model_dump_json(by_alias=True, exclude_none=True),
                "2026-07-18T20:00:00+00:00",
            ),
        )

    with pytest.raises(ReceiptTransitionError, match="receipt evidence mismatch"):
        store.finalize_completed_execution(
            execution,
            receipt=expected_receipt,
            resume_owner_id=owner_id,
            resume_generation=generation,
        )

    assert store.get_recovery(recovery_id).status is RecoveryStatus.PENDING_APPROVAL
    with sqlite3.connect(database_path) as connection:
        stored = json.loads(
            connection.execute(
                "SELECT receipt_json FROM receipts WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone()[0]
        )
    assert stored["approvedRemedyDigest"] == OTHER_VALID_DIGEST
    assert not any(event.terminal for event in store.list_events(recovery_id))


def test_finalizer_rejects_terminal_event_with_wrong_type_and_data(tmp_path) -> None:
    database_path = tmp_path / "wrong-finalizer-terminal.sqlite3"
    store = SQLiteStore(database_path)
    recovery_id, execution, expected_receipt, owner_id, generation = (
        create_durable_sdk_execution(store)
    )
    wrong_terminal_data = {
        "phase": "Cancelled",
        "providerExecution": False,
        "summary": "Wrong terminal evidence.",
    }
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO events (
                recovery_id, seq, type, terminal, data_json, created_at
            ) VALUES (?, 3, 'recovery.cancelled', 1, ?, ?)
            """,
            (
                recovery_id,
                json.dumps(wrong_terminal_data, separators=(",", ":"), sort_keys=True),
                "2026-07-18T20:00:00+00:00",
            ),
        )

    with pytest.raises(ReceiptTransitionError, match="terminal evidence mismatch"):
        store.finalize_completed_execution(
            execution,
            receipt=expected_receipt,
            resume_owner_id=owner_id,
            resume_generation=generation,
        )

    assert store.get_recovery(recovery_id).status is RecoveryStatus.PENDING_APPROVAL
    with pytest.raises(RecoveryNotFoundError, match="Receipt not found"):
        store.get_receipt(recovery_id)
    terminal_events = [event for event in store.list_events(recovery_id) if event.terminal]
    assert len(terminal_events) == 1
    assert terminal_events[0].type == "recovery.cancelled"
    assert terminal_events[0].data == wrong_terminal_data


def test_finalizer_replays_matching_receipt_and_terminal_evidence_idempotently(
    tmp_path,
) -> None:
    store = SQLiteStore(tmp_path / "matching-finalizer-evidence.sqlite3")
    recovery_id, execution, expected_receipt, owner_id, generation = (
        create_durable_sdk_execution(store)
    )

    assert store.finalize_completed_execution(
        execution,
        receipt=expected_receipt,
        resume_owner_id=owner_id,
        resume_generation=generation,
    ) is True
    assert store.finalize_completed_execution(
        execution,
        receipt=expected_receipt,
        resume_owner_id=owner_id,
        resume_generation=generation,
    ) is False

    assert store.get_recovery(recovery_id).status is RecoveryStatus.COMPLETED
    assert store.get_receipt(recovery_id) == expected_receipt
    terminal_events = [event for event in store.list_events(recovery_id) if event.terminal]
    assert len(terminal_events) == 1
    assert terminal_events[0].type == "recovery.completed"
    assert terminal_events[0].data["recoveryId"] == recovery_id
    assert terminal_events[0].data["executionMode"] == "sdk_stub"
    assert terminal_events[0].data["providerExecution"] is True
    assert terminal_events[0].data["approvedRemedyDigest"] == (
        expected_receipt.approved_remedy_digest
    )


@pytest.mark.parametrize(
    "tamper",
    ["request_digest", "pre_authorization_timestamp"],
)
def test_owner_terminal_readers_fail_closed_on_tampered_execution_evidence(
    tmp_path,
    tamper: str,
) -> None:
    database_path = tmp_path / f"public-terminal-{tamper}.sqlite3"
    store = SQLiteStore(database_path)
    recovery_id, execution, receipt, owner_id, generation = (
        create_durable_sdk_execution(store)
    )
    assert store.finalize_completed_execution(
        execution,
        receipt=receipt,
        resume_owner_id=owner_id,
        resume_generation=generation,
    )
    session_hash = "hmac-sha256:" + "f" * 64
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO recovery_access (recovery_id, session_hash)
            VALUES (?, ?)
            """,
            (recovery_id, session_hash),
        )

    assert store.get_recovery_for_session(recovery_id, session_hash).status is (
        RecoveryStatus.COMPLETED
    )
    assert store.get_receipt_for_session(recovery_id, session_hash) == receipt
    assert store.read_event_batch_for_session(
        recovery_id,
        session_hash=session_hash,
    )[1] is RecoveryStatus.COMPLETED

    with sqlite3.connect(database_path) as connection:
        if tamper == "request_digest":
            connection.execute(
                "UPDATE executions SET request_digest = ? WHERE recovery_id = ?",
                ("0" * 64, recovery_id),
            )
        else:
            claimed_at_row = connection.execute(
                """
                SELECT claimed_at FROM approval_decisions
                WHERE recovery_id = ?
                """,
                (recovery_id,),
            ).fetchone()
            assert claimed_at_row is not None
            before_authorization = (
                datetime.fromisoformat(str(claimed_at_row[0]))
                - timedelta(seconds=1)
            ).isoformat()
            connection.execute(
                """
                UPDATE executions
                SET created_at = ?, updated_at = ?
                WHERE recovery_id = ?
                """,
                (before_authorization, before_authorization, recovery_id),
            )

    with pytest.raises(ReceiptTransitionError):
        store.get_recovery_for_session(recovery_id, session_hash)
    with pytest.raises(ReceiptTransitionError):
        store.get_receipt_for_session(recovery_id, session_hash)
    with pytest.raises(ReceiptTransitionError):
        store.read_event_batch_for_session(
            recovery_id,
            session_hash=session_hash,
        )


def test_finalizer_rejects_incomplete_task7_provenance_without_sealing(
    tmp_path,
) -> None:
    database_path = tmp_path / "incomplete-finalizer-provenance.sqlite3"
    store = SQLiteStore(database_path)
    recovery_id, execution, receipt, owner_id, generation = (
        create_durable_sdk_execution(store)
    )
    incomplete_receipt = receipt.model_copy(
        update={
            "root_trace_id": None,
            "sdk_version": None,
            "protocol_version": None,
            "agent_graph_version": None,
            "prompt_tool_schema_hash": None,
        }
    )

    with pytest.raises(ReceiptTransitionError, match="provenance"):
        store.finalize_completed_execution(
            execution,
            receipt=incomplete_receipt,
            resume_owner_id=owner_id,
            resume_generation=generation,
        )

    assert store.get_recovery(recovery_id).status is RecoveryStatus.PENDING_APPROVAL
    with pytest.raises(RecoveryNotFoundError, match="Receipt not found"):
        store.get_receipt(recovery_id)
    assert not any(event.terminal for event in store.list_events(recovery_id))
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM receipt_provenance_migrations WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone() == (0,)
