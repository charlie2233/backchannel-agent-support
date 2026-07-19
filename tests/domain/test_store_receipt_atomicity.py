import json
import sqlite3

import pytest

from server.models import (
    ExecutionMode,
    RecoveryReceipt,
    RecoveryStatus,
    ScenarioId,
)
from server.store import (
    DurableExecution,
    ReceiptTransitionError,
    RecoveryNotFoundError,
    SQLiteStore,
)

APPROVED_DIGEST = f"sha256:{'a' * 64}"
OTHER_VALID_DIGEST = f"sha256:{'b' * 64}"


def make_receipt(
    *, recovery_id: str, execution_mode: ExecutionMode
) -> RecoveryReceipt:
    return RecoveryReceipt(
        recoveryId=recovery_id,
        executionMode=execution_mode,
        status=(
            "simulated_completed"
            if execution_mode is ExecutionMode.REPLAY_FIXTURE
            else "completed"
        ),
        simulated=True,
        providerExecution=execution_mode is ExecutionMode.SDK_STUB,
        modelIds=[],
        boundary="Atomic receipt test boundary.",
        providerResult="Atomic receipt test result.",
        authorizationSource="Atomic receipt test authorization.",
        verificationResults=["Atomic receipt test verification."],
        approvedRemedyDigest=(
            APPROVED_DIGEST if execution_mode is ExecutionMode.SDK_STUB else None
        ),
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
    recovery_id: str,
) -> tuple[DurableExecution, RecoveryReceipt]:
    store.create_recovery(
        recovery_id=recovery_id,
        scenario_id=ScenarioId.HOTEL,
        execution_mode=ExecutionMode.SDK_STUB,
        current_step=3,
        current_step_summary="Durable provider result awaiting finalization.",
    )
    provider_result = "Bound demo-provider result."
    execution, dispatched = store.record_completed_execution(
        execution_id=f"execution-{recovery_id}",
        recovery_id=recovery_id,
        idempotency_key=f"idempotency-{recovery_id}",
        request_digest="request-digest",
        tool_call_id=f"tool-{recovery_id}",
        remedy_digest=APPROVED_DIGEST,
        result_json={
            "dispatch_id": f"dispatch-{recovery_id}",
            "status": "confirmed",
            "simulated": True,
            "provider_result": provider_result,
        },
    )
    assert dispatched is True
    receipt = RecoveryReceipt(
        recoveryId=recovery_id,
        executionMode=ExecutionMode.SDK_STUB,
        status="completed",
        simulated=True,
        providerExecution=True,
        modelIds=[],
        boundary="Durable evidence test boundary.",
        providerResult=provider_result,
        authorizationSource="Exact approval decision.",
        verificationResults=["Durable provider result verified."],
        approvedRemedyDigest=APPROVED_DIGEST,
    )
    return execution, receipt


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
    recovery_id = "wrong-finalizer-receipt"
    execution, expected_receipt = create_durable_sdk_execution(store, recovery_id)
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
        store.finalize_completed_execution(execution, receipt=expected_receipt)

    assert store.get_recovery(recovery_id).status is RecoveryStatus.IN_PROGRESS
    assert store.get_receipt(recovery_id).approved_remedy_digest == OTHER_VALID_DIGEST
    assert not any(event.terminal for event in store.list_events(recovery_id))


def test_finalizer_rejects_terminal_event_with_wrong_type_and_data(tmp_path) -> None:
    database_path = tmp_path / "wrong-finalizer-terminal.sqlite3"
    store = SQLiteStore(database_path)
    recovery_id = "wrong-finalizer-terminal"
    execution, expected_receipt = create_durable_sdk_execution(store, recovery_id)
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
            ) VALUES (?, 2, 'recovery.cancelled', 1, ?, ?)
            """,
            (
                recovery_id,
                json.dumps(wrong_terminal_data, separators=(",", ":"), sort_keys=True),
                "2026-07-18T20:00:00+00:00",
            ),
        )

    with pytest.raises(ReceiptTransitionError, match="terminal evidence mismatch"):
        store.finalize_completed_execution(execution, receipt=expected_receipt)

    assert store.get_recovery(recovery_id).status is RecoveryStatus.IN_PROGRESS
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
    recovery_id = "matching-finalizer-evidence"
    execution, expected_receipt = create_durable_sdk_execution(store, recovery_id)

    assert store.finalize_completed_execution(execution, receipt=expected_receipt) is True
    assert store.finalize_completed_execution(execution, receipt=expected_receipt) is False

    assert store.get_recovery(recovery_id).status is RecoveryStatus.COMPLETED
    assert store.get_receipt(recovery_id) == expected_receipt
    terminal_events = [event for event in store.list_events(recovery_id) if event.terminal]
    assert len(terminal_events) == 1
    assert terminal_events[0].type == "recovery.completed"
    assert terminal_events[0].data["recoveryId"] == recovery_id
    assert terminal_events[0].data["executionMode"] == "sdk_stub"
    assert terminal_events[0].data["providerExecution"] is True
    assert terminal_events[0].data["approvedRemedyDigest"] == APPROVED_DIGEST
