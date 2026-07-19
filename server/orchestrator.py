"""Coordinate deterministic recoveries through the actual Agents SDK runner."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, NoReturn
from uuid import uuid4

from agents import Agent, RunContextWrapper, Runner, RunResult, RunState

from server.agents.factory import HotelAgentContext, build_hotel_agent
from server.agents.schemas import CommitRemedyArguments, deterministic_hotel_arguments
from server.agents.tracing import configure_sdk_stub_tracing
from server.agents.versioning import (
    HOTEL_START_PROMPT,
    ApprovalVersionPolicy,
    hotel_definition_digest,
    is_valid_qa_trace_id,
    new_qa_trace_id,
    remedy_action_digest,
)
from server.models import (
    ExecutionMode,
    RecoveryReceipt,
    RecoverySnapshot,
    RecoveryStatus,
    ScenarioId,
)
from server.providers.hotel_simulator import HotelDispatchResult, HotelSimulator
from server.store import (
    DurableExecution,
    PendingApprovalEnvelope,
    RecoveryNotFoundError,
    SQLiteStore,
)

logger = logging.getLogger(__name__)


class UnsupportedOrchestrationError(ValueError):
    """Raised when Task 3 is asked to run outside the SDK-stub hotel rail."""


class ResumeIncompatibleError(ValueError):
    """Stable public failure for an unsafe or version-incompatible SDK resume."""

    code = "resume_incompatible"

    def __init__(self, recovery_id: str) -> None:
        self.recovery_id = recovery_id
        super().__init__(self.code)

    @property
    def public_detail(self) -> dict[str, str]:
        return {"code": self.code, "recoveryId": self.recovery_id}


@dataclass(frozen=True, slots=True)
class PendingSdkApproval:
    recovery: RecoverySnapshot
    sdk_result: RunResult
    original_root_agent: Agent[HotelAgentContext]


class RecoveryOrchestrator:
    def __init__(
        self,
        *,
        store: SQLiteStore,
        hotel_provider: HotelSimulator,
        version_policy: ApprovalVersionPolicy | None = None,
    ) -> None:
        self._store = store
        self._hotel_provider = hotel_provider
        self._hotel_provider.bind_store(store)
        self._version_policy = version_policy or ApprovalVersionPolicy.current()
        self._reconcile_completed_executions()

    @staticmethod
    def _context_serializer(_context: HotelAgentContext) -> dict[str, Any]:
        """The process-owned context is rebuilt explicitly and never restored from JSON."""

        return {}

    @staticmethod
    def _receipt_for_execution(execution: DurableExecution) -> RecoveryReceipt:
        if execution.result_json is None:
            raise ValueError("Completed durable execution is missing its result")
        dispatch = HotelDispatchResult.model_validate(execution.result_json)
        return RecoveryReceipt(
            recoveryId=execution.recovery_id,
            executionMode=ExecutionMode.SDK_STUB,
            status="completed",
            simulated=True,
            providerExecution=True,
            modelIds=[],
            boundary=(
                "Deterministic Agents SDK model and demo hotel adapter only; "
                "no OpenAI model call, real booking, or payment change."
            ),
            providerResult=dispatch.provider_result,
            authorizationSource="Approved Agents SDK commit_remedy interruption.",
            verificationResults=[
                "Demo provider dispatch returned confirmed.",
                "Provider result stored under one idempotency key.",
            ],
        )

    def _reconcile_completed_executions(self) -> None:
        """Finalize committed provider results without calling the provider again."""

        for execution in self._store.list_executions_needing_finalization():
            try:
                self._store.finalize_completed_execution(
                    execution,
                    receipt=self._receipt_for_execution(execution),
                )
            except Exception:
                logger.exception(
                    "Failed to reconcile committed execution for recovery_id=%s",
                    execution.recovery_id,
                )
                raise

    @staticmethod
    def _raise_incompatible(recovery_id: str, marker: str) -> NoReturn:
        logger.error(
            "Serialized approval resume incompatible recovery_id=%s marker=%s",
            recovery_id,
            marker,
        )
        raise ResumeIncompatibleError(recovery_id)

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
        arguments = deterministic_hotel_arguments()
        remedy_digest = remedy_action_digest(arguments)
        context = HotelAgentContext(
            recovery_id=recovery_id,
            store=self._store,
            hotel_provider=self._hotel_provider,
            remedy_digest=remedy_digest,
        )
        original_root_agent = build_hotel_agent(
            context=context,
            arguments=arguments,
        )
        result = await Runner.run(
            original_root_agent,
            HOTEL_START_PROMPT,
            context=context,
            run_config=configure_sdk_stub_tracing(),
        )
        if len(result.interruptions) != 1:
            raise RuntimeError("Deterministic SDK run did not produce exactly one interruption")
        interruption = result.interruptions[0]
        if interruption.tool_name != "commit_remedy":
            raise RuntimeError("Deterministic SDK run interrupted on an unexpected tool")
        if not interruption.call_id:
            raise RuntimeError("Deterministic SDK interruption is missing its call ID")

        state_json = result.to_state().to_json(
            context_serializer=self._context_serializer,
            strict_context=True,
        )
        envelope = PendingApprovalEnvelope(
            tool_call_id=interruption.call_id,
            recovery_id=recovery_id,
            sdk_version=self._version_policy.sdk_version,
            protocol_version=self._version_policy.protocol_version,
            agent_graph_version=self._version_policy.agent_graph_version,
            definition_digest=hotel_definition_digest(original_root_agent),
            root_trace_id=new_qa_trace_id(),
            execution_mode=execution_mode,
            remedy_digest=remedy_digest,
            state_json=state_json,
        )

        recovery = self._store.record_transition(
            recovery_id,
            status=RecoveryStatus.PENDING_APPROVAL,
            current_step=3,
            current_step_summary="Approval required before demo-provider dispatch.",
            event_type="approval.requested",
            event_data={
                "phase": "Authorize",
                "providerExecution": False,
                "summary": "An internal Agents SDK approval interruption is pending.",
            },
            pending_approval=envelope,
        )
        return PendingSdkApproval(
            recovery=recovery,
            sdk_result=result,
            original_root_agent=original_root_agent,
        )

    async def resume_approved(self, pending: PendingSdkApproval | str) -> RunResult:
        """Validate, restore, and approve the exact durable SDK interruption once."""

        recovery_id = (
            pending.recovery.recovery_id
            if isinstance(pending, PendingSdkApproval)
            else pending
        )
        arguments = deterministic_hotel_arguments()
        expected_remedy_digest = remedy_action_digest(arguments)
        fresh_context = HotelAgentContext(
            recovery_id=recovery_id,
            store=self._store,
            hotel_provider=self._hotel_provider,
            remedy_digest=expected_remedy_digest,
        )
        fresh_agent = build_hotel_agent(context=fresh_context, arguments=arguments)
        try:
            recovery = self._store.get_recovery(recovery_id)
            envelope = self._store.get_pending_approval(recovery_id)
        except (RecoveryNotFoundError, ValueError, TypeError):
            self._raise_incompatible(recovery_id, "envelope")

        expected_markers = {
            "sdk_version": self._version_policy.sdk_version,
            "protocol_version": self._version_policy.protocol_version,
            "agent_graph_version": self._version_policy.agent_graph_version,
            "definition_digest": hotel_definition_digest(fresh_agent),
            "execution_mode": ExecutionMode.SDK_STUB,
            "tool_call_id": f"commit-remedy-{recovery_id}",
            "remedy_digest": expected_remedy_digest,
        }
        actual_markers = {
            "sdk_version": envelope.sdk_version,
            "protocol_version": envelope.protocol_version,
            "agent_graph_version": envelope.agent_graph_version,
            "definition_digest": envelope.definition_digest,
            "execution_mode": envelope.execution_mode,
            "tool_call_id": envelope.tool_call_id,
            "remedy_digest": envelope.remedy_digest,
        }
        for marker, expected in expected_markers.items():
            if actual_markers[marker] != expected:
                self._raise_incompatible(recovery_id, marker)
        if recovery.execution_mode is not envelope.execution_mode:
            self._raise_incompatible(recovery_id, "recovery_execution_mode")
        if recovery.status is not RecoveryStatus.PENDING_APPROVAL:
            self._raise_incompatible(recovery_id, "recovery_status")
        if recovery.scenario_id is not ScenarioId.HOTEL:
            self._raise_incompatible(recovery_id, "scenario_id")
        if envelope.status not in {"pending", "approved"}:
            self._raise_incompatible(recovery_id, "approval_status")
        if not is_valid_qa_trace_id(envelope.root_trace_id):
            self._raise_incompatible(recovery_id, "root_trace_id")
        if not isinstance(envelope.state_json, dict) or not envelope.state_json:
            self._raise_incompatible(recovery_id, "state_json")

        try:
            state = await RunState.from_json(
                fresh_agent,
                envelope.state_json,
                context_override=RunContextWrapper(context=fresh_context),
                strict_context=True,
            )
        except Exception:
            logger.exception(
                "Agents SDK state restore failed recovery_id=%s",
                recovery_id,
            )
            raise ResumeIncompatibleError(recovery_id) from None
        interruptions = state.get_interruptions()
        if len(interruptions) != 1:
            self._raise_incompatible(recovery_id, "restored_interruption_count")
        interruption = interruptions[0]
        if (
            interruption.tool_name != "commit_remedy"
            or interruption.call_id != envelope.tool_call_id
        ):
            self._raise_incompatible(recovery_id, "restored_interruption")
        try:
            restored_arguments = CommitRemedyArguments.model_validate_json(
                interruption.arguments or ""
            )
        except ValueError:
            self._raise_incompatible(recovery_id, "restored_arguments")
        if remedy_action_digest(restored_arguments) != envelope.remedy_digest:
            self._raise_incompatible(recovery_id, "restored_remedy_digest")

        state.approve(interruption)
        self._store.update_pending_approval_status(recovery_id, status="approved")
        completed = await Runner.run(
            fresh_agent,
            state,
            run_config=configure_sdk_stub_tracing(),
        )
        self._store.update_pending_approval_status(recovery_id, status="completed")
        return completed
