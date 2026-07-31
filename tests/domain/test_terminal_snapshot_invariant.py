import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from server.models import (
    ExecutionMode,
    RecoverySnapshot,
    RecoveryStatus,
    ScenarioId,
)
from server.store import SQLiteStore

TERMINAL_STATUSES = (
    RecoveryStatus.COMPLETED,
    RecoveryStatus.CLOSED_WITHOUT_ACTION,
    RecoveryStatus.OUTCOME_UNKNOWN,
)
NONTERMINAL_STATUSES = (
    RecoveryStatus.IN_PROGRESS,
    RecoveryStatus.PENDING_APPROVAL,
)
FIXED_TIME = datetime(2026, 7, 30, 12, tzinfo=UTC)


def _snapshot(*, status: RecoveryStatus, current_step: int) -> RecoverySnapshot:
    return RecoverySnapshot(
        recoveryId="terminal-snapshot-contract",
        scenarioId=ScenarioId.HOTEL,
        executionMode=ExecutionMode.SDK_STUB,
        status=status,
        currentStep=current_step,
        currentStepSummary="Terminal snapshot invariant test.",
        createdAt=FIXED_TIME,
        updatedAt=FIXED_TIME,
    )


@pytest.mark.parametrize("status", TERMINAL_STATUSES)
@pytest.mark.parametrize("current_step", range(5))
def test_terminal_snapshot_rejects_every_nonterminal_step(
    status: RecoveryStatus,
    current_step: int,
) -> None:
    with pytest.raises(
        ValidationError,
        match="Terminal recovery snapshots require currentStep 5",
    ):
        _snapshot(status=status, current_step=current_step)


@pytest.mark.parametrize("status", TERMINAL_STATUSES)
def test_terminal_snapshot_accepts_step_five(status: RecoveryStatus) -> None:
    snapshot = _snapshot(status=status, current_step=5)

    assert snapshot.status is status
    assert snapshot.current_step == 5


@pytest.mark.parametrize("status", NONTERMINAL_STATUSES)
@pytest.mark.parametrize("current_step", range(6))
def test_nonterminal_snapshot_steps_remain_valid(
    status: RecoveryStatus,
    current_step: int,
) -> None:
    snapshot = _snapshot(status=status, current_step=current_step)

    assert snapshot.status is status
    assert snapshot.current_step == current_step


def _persisted_recovery_state(
    database_path: Path,
    recovery_id: str,
) -> tuple[tuple[object, ...], tuple[tuple[object, ...], ...]]:
    with sqlite3.connect(database_path) as connection:
        recovery = connection.execute(
            """
            SELECT status, current_step, current_step_summary, updated_at
            FROM recoveries
            WHERE id = ?
            """,
            (recovery_id,),
        ).fetchone()
        events = connection.execute(
            """
            SELECT seq, type, terminal, data_json, created_at
            FROM events
            WHERE recovery_id = ?
            ORDER BY seq
            """,
            (recovery_id,),
        ).fetchall()
    assert recovery is not None
    return tuple(recovery), tuple(tuple(event) for event in events)


@pytest.mark.parametrize("status", TERMINAL_STATUSES)
@pytest.mark.parametrize("current_step", range(5))
def test_store_rejects_invalid_terminal_transition_before_any_mutation(
    tmp_path: Path,
    status: RecoveryStatus,
    current_step: int,
) -> None:
    database_path = tmp_path / "terminal-transition.sqlite3"
    recovery_id = f"{status.value}-{current_step}"
    store = SQLiteStore(database_path)
    store.create_recovery(
        recovery_id=recovery_id,
        scenario_id=ScenarioId.HOTEL,
        execution_mode=ExecutionMode.SDK_STUB,
        current_step=0,
        current_step_summary="Recovery created.",
    )
    before = _persisted_recovery_state(database_path, recovery_id)

    with pytest.raises(
        ValueError,
        match="Terminal recovery transitions require current_step 5",
    ):
        store.record_transition(
            recovery_id,
            status=status,
            current_step=current_step,
            current_step_summary="Must not persist.",
            event_type="recovery.invalid_terminal",
            event_data={"currentStep": current_step},
        )

    assert _persisted_recovery_state(database_path, recovery_id) == before
    snapshot = store.get_recovery(recovery_id)
    assert snapshot.status is RecoveryStatus.IN_PROGRESS
    assert snapshot.current_step == 0


@pytest.mark.parametrize(
    "status",
    (*TERMINAL_STATUSES, *NONTERMINAL_STATUSES),
)
def test_store_allows_step_five_for_terminal_and_nonterminal_statuses(
    tmp_path: Path,
    status: RecoveryStatus,
) -> None:
    database_path = tmp_path / "step-five-transition.sqlite3"
    recovery_id = status.value
    store = SQLiteStore(database_path)
    store.create_recovery(
        recovery_id=recovery_id,
        scenario_id=ScenarioId.HOTEL,
        execution_mode=ExecutionMode.SDK_STUB,
        current_step=0,
        current_step_summary="Recovery created.",
    )

    snapshot = store.record_transition(
        recovery_id,
        status=status,
        current_step=5,
        current_step_summary="Step five remains valid.",
        event_type="recovery.step_five",
        event_data={"currentStep": 5},
    )

    assert snapshot.status is status
    assert snapshot.current_step == 5
    assert store.list_events(recovery_id)[-1].terminal is (status in TERMINAL_STATUSES)


def test_malformed_legacy_terminal_row_fails_closed_without_rewriting(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "malformed-legacy.sqlite3"
    recovery_id = "malformed-legacy-terminal"
    store = SQLiteStore(database_path)
    store.create_recovery(
        recovery_id=recovery_id,
        scenario_id=ScenarioId.HOTEL,
        execution_mode=ExecutionMode.SDK_STUB,
        current_step=0,
        current_step_summary="Recovery created.",
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            UPDATE recoveries
            SET status = 'completed', current_step = 2,
                current_step_summary = 'Malformed legacy terminal.'
            WHERE id = ?
            """,
            (recovery_id,),
        )
    before = _persisted_recovery_state(database_path, recovery_id)

    with pytest.raises(
        ValidationError,
        match="Terminal recovery snapshots require currentStep 5",
    ):
        store.get_recovery(recovery_id)

    assert _persisted_recovery_state(database_path, recovery_id) == before
