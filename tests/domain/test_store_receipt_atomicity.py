import pytest

from server.models import (
    ExecutionMode,
    RecoveryReceipt,
    RecoveryStatus,
    ScenarioId,
)
from server.store import (
    ReceiptTransitionError,
    RecoveryNotFoundError,
    SQLiteStore,
)


def make_receipt(
    *, recovery_id: str, execution_mode: ExecutionMode
) -> RecoveryReceipt:
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
    )


def create_replay_recovery(store: SQLiteStore, recovery_id: str) -> None:
    store.create_recovery(
        recovery_id=recovery_id,
        scenario_id=ScenarioId.HOTEL,
        execution_mode=ExecutionMode.REPLAY_FIXTURE,
        current_step=0,
        current_step_summary="Receipt atomicity test started.",
    )


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
