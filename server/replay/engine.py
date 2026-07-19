"""Persist deterministic replay fixtures without model or provider execution."""

from __future__ import annotations

from uuid import uuid4

from server.models import (
    ExecutionMode,
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
        return self._store.create_replay_recovery(
            recovery_id=recovery_id,
            scenario=scenario,
        )
