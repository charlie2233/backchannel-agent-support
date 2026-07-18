"""Persist deterministic replay fixtures without model or provider execution."""

from __future__ import annotations

from uuid import uuid4

from server.models import (
    ExecutionMode,
    RecoveryReceipt,
    RecoverySnapshot,
    ScenarioId,
)
from server.replay.loader import ScenarioLoader
from server.store import SQLiteStore


class UnsupportedExecutionModeError(ValueError):
    """Raised when Task 2 is asked to imply an unsupported execution path."""


class ReplayEngine:
    def __init__(self, store: SQLiteStore, loader: ScenarioLoader) -> None:
        self._store = store
        self._loader = loader

    def start(
        self,
        scenario_id: str | ScenarioId,
        *,
        execution_mode: ExecutionMode,
    ) -> RecoverySnapshot:
        if execution_mode is not ExecutionMode.REPLAY_FIXTURE:
            raise UnsupportedExecutionModeError("Task 2 supports replay_fixture only")

        scenario = self._loader.get(scenario_id)
        recovery_id = str(uuid4())
        snapshot = self._store.create_recovery(
            recovery_id=recovery_id,
            scenario_id=scenario.id,
            execution_mode=execution_mode,
            current_step=scenario.initial_step,
            current_step_summary=scenario.initial_summary,
        )
        final_event_index = len(scenario.events) - 1
        for index, event in enumerate(scenario.events):
            receipt = None
            if index == final_event_index and scenario.receipt is not None:
                receipt = RecoveryReceipt(
                    recoveryId=recovery_id,
                    executionMode=ExecutionMode.REPLAY_FIXTURE,
                    **scenario.receipt.model_dump(),
                )
            snapshot = self._store.record_transition(
                recovery_id,
                status=event.status,
                current_step=event.current_step,
                current_step_summary=event.summary,
                event_type=event.type,
                event_data=event.data,
                receipt=receipt,
            )
        return snapshot
