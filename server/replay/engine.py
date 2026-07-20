"""Persist deterministic replay fixtures without model or provider execution."""

from __future__ import annotations

import hashlib
import json
from uuid import UUID, uuid5

from server.models import (
    ExecutionMode,
    RecoverySnapshot,
    ReplayScenarioDefinition,
    ScenarioId,
)
from server.replay.loader import ScenarioLoader
from server.store import SQLiteStore


class UnsupportedExecutionModeError(ValueError):
    """Raised when Task 2 is asked to imply an unsupported execution path."""


REPLAY_RECOVERY_NAMESPACE = UUID("ef0e1c46-d318-54ea-98c5-f372e00359ba")


def replay_definition_digest(scenario: ReplayScenarioDefinition) -> str:
    """Hash the complete typed fixture definition using canonical JSON."""

    canonical = json.dumps(
        scenario.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def replay_recovery_id(scenario: ReplayScenarioDefinition) -> str:
    """Return the stable server-owned identity for one exact fixture definition."""

    digest = replay_definition_digest(scenario)
    return str(uuid5(REPLAY_RECOVERY_NAMESPACE, digest))


class ReplayEngine:
    def __init__(self, store: SQLiteStore, loader: ScenarioLoader) -> None:
        self._store = store
        self._loader = loader

    def start(
        self,
        scenario_id: str | ScenarioId,
        *,
        execution_mode: ExecutionMode,
        session_key: str | None = None,
    ) -> RecoverySnapshot:
        if execution_mode is not ExecutionMode.REPLAY_FIXTURE:
            raise UnsupportedExecutionModeError("Task 2 supports replay_fixture only")

        scenario = self._loader.get(scenario_id)
        return self._store.get_or_create_replay(
            recovery_id=replay_recovery_id(scenario),
            scenario=scenario,
            session_key=session_key,
        )
