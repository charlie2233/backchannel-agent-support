"""Coordinate deterministic recoveries through the actual Agents SDK runner."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from agents import Agent, Runner, RunResult

from server.agents.factory import HotelAgentContext, build_hotel_agent
from server.agents.schemas import deterministic_hotel_arguments
from server.agents.tracing import configure_sdk_stub_tracing
from server.models import ExecutionMode, RecoverySnapshot, RecoveryStatus, ScenarioId
from server.providers.hotel_simulator import HotelSimulator
from server.store import SQLiteStore


class UnsupportedOrchestrationError(ValueError):
    """Raised when Task 3 is asked to run outside the SDK-stub hotel rail."""


@dataclass(frozen=True, slots=True)
class PendingSdkApproval:
    recovery: RecoverySnapshot
    sdk_result: RunResult
    original_root_agent: Agent[HotelAgentContext]


class RecoveryOrchestrator:
    def __init__(self, *, store: SQLiteStore, hotel_provider: HotelSimulator) -> None:
        self._store = store
        self._hotel_provider = hotel_provider

    async def start(
        self,
        scenario_id: str | ScenarioId,
        *,
        execution_mode: ExecutionMode,
    ) -> PendingSdkApproval:
        try:
            approved_scenario = ScenarioId(scenario_id)
        except ValueError as error:
            raise UnsupportedOrchestrationError("Unknown scenario") from error
        if (
            approved_scenario is not ScenarioId.HOTEL
            or execution_mode is not ExecutionMode.SDK_STUB
        ):
            raise UnsupportedOrchestrationError(
                "Task 3 supports sdk_stub hotel recovery only"
            )

        recovery_id = str(uuid4())
        self._store.create_recovery(
            recovery_id=recovery_id,
            scenario_id=approved_scenario,
            execution_mode=execution_mode,
            current_step=0,
            current_step_summary="Deterministic Agents SDK recovery started.",
        )
        context = HotelAgentContext(
            recovery_id=recovery_id,
            store=self._store,
            hotel_provider=self._hotel_provider,
        )
        original_root_agent = build_hotel_agent(
            context=context,
            arguments=deterministic_hotel_arguments(),
        )
        result = await Runner.run(
            original_root_agent,
            "Run the deterministic hotel recovery to its authorization boundary.",
            context=context,
            run_config=configure_sdk_stub_tracing(),
        )
        if len(result.interruptions) != 1:
            raise RuntimeError("Deterministic SDK run did not produce exactly one interruption")
        interruption = result.interruptions[0]
        if interruption.tool_name != "commit_remedy":
            raise RuntimeError("Deterministic SDK run interrupted on an unexpected tool")

        recovery = self._store.record_transition(
            recovery_id,
            status=RecoveryStatus.PENDING_APPROVAL,
            current_step=3,
            current_step_summary="Approval required before demo-provider dispatch.",
            event_type="approval.requested",
            event_data={
                "phase": "Authorize",
                "providerExecution": False,
                "summary": "Agents SDK commit_remedy interruption is pending.",
                "toolCallId": interruption.call_id or "unavailable",
                "toolName": interruption.tool_name,
            },
        )
        return PendingSdkApproval(
            recovery=recovery,
            sdk_result=result,
            original_root_agent=original_root_agent,
        )
