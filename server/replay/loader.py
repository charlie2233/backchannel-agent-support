"""Strict loader for the two bundled replay scenarios."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from server.models import ReplayScenarioDefinition, ScenarioId

SCENARIO_ORDER = (ScenarioId.HOTEL, ScenarioId.API_QUOTA)


class ScenarioNotFoundError(ValueError):
    """Raised when a request names anything outside the two approved fixtures."""


class ScenarioDefinitionError(RuntimeError):
    """Raised when a bundled scenario no longer matches its typed contract."""


class ScenarioLoader:
    def __init__(self, scenario_directory: Path | None = None) -> None:
        self._scenario_directory = scenario_directory or (
            Path(__file__).resolve().parents[1] / "scenarios"
        )
        self._scenarios = self._load_all()

    def _load_all(self) -> dict[ScenarioId, ReplayScenarioDefinition]:
        scenarios: dict[ScenarioId, ReplayScenarioDefinition] = {}
        for scenario_id in SCENARIO_ORDER:
            path = self._scenario_directory / f"{scenario_id.value}.json"
            try:
                scenario = ReplayScenarioDefinition.model_validate_json(path.read_text())
            except (OSError, ValidationError) as error:
                raise ScenarioDefinitionError(
                    f"Invalid bundled scenario: {scenario_id.value}"
                ) from error
            if scenario.id != scenario_id:
                raise ScenarioDefinitionError(
                    f"Scenario file {path.name} declares {scenario.id.value}"
                )
            scenarios[scenario_id] = scenario
        return scenarios

    def get(self, scenario_id: str | ScenarioId) -> ReplayScenarioDefinition:
        try:
            approved_id = ScenarioId(scenario_id)
        except ValueError as error:
            raise ScenarioNotFoundError("Unknown scenario") from error
        return self._scenarios[approved_id]

    def list(self) -> tuple[ReplayScenarioDefinition, ...]:
        return tuple(self._scenarios[scenario_id] for scenario_id in SCENARIO_ORDER)
