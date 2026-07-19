"""Coordinate deterministic recoveries through the actual Agents SDK runner."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn
from uuid import uuid4

from agents import (
    Agent,
    ModelProvider,
    RunContextWrapper,
    Runner,
    RunResult,
    RunState,
    gen_trace_id,
    trace,
)
from agents.exceptions import UserError

from server.agents.factory import HotelAgentContext, build_hotel_agent
from server.agents.live_factory import (
    LIVE_CONSUMER_PROMPT,
    LIVE_PROVIDER_PROMPT,
    build_live_hotel_agents,
    live_broker_prompt,
)
from server.agents.live_models import ResponseMetadataRecorder
from server.agents.schemas import (
    BrokerOutcome,
    CommitRemedyArguments,
    ConsumerProof,
    ProviderProof,
    deterministic_hotel_arguments,
)
from server.agents.stub_model import CLOSED_WITHOUT_ACTION_MESSAGE, DECLINE_MESSAGE
from server.agents.tracing import (
    configure_openai_live_tracing,
    configure_sdk_stub_tracing,
)
from server.agents.versioning import (
    HOTEL_START_PROMPT,
    ApprovalVersionPolicy,
    hotel_definition_digest,
    is_valid_openai_trace_id,
    is_valid_qa_trace_id,
    live_hotel_definition_digest,
    new_qa_trace_id,
    remedy_action_digest,
)
from server.digest import remedy_consent_digest
from server.models import (
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    DecisionResponse,
    DeclineDecisionResponse,
    ExecutionMode,
    RecoveryReceipt,
    RecoverySnapshot,
    RecoveryStatus,
    ScenarioId,
)
from server.policy import (
    DETERMINISTIC_HOTEL_AUTHORITY,
    evaluate_hotel_policy,
    exact_hotel_terms,
)
from server.providers.hotel_simulator import HotelDispatchResult, HotelSimulator
from server.store import (
    APPROVED_RECEIPT_AUTHORIZATION,
    APPROVED_RECEIPT_VERIFICATIONS,
    ApprovalDecisionClaim,
    ApprovalDecisionError,
    DecisionResumeLease,
    DurableExecution,
    PendingApprovalEnvelope,
    RecoveryNotFoundError,
    RemedyConsentRecord,
    SQLiteStore,
    non_replay_receipt_boundary,
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


class LiveUnavailableError(ValueError):
    """Stable key/capability gate raised before any live model or provider call."""

    code = "live_unavailable"

    def __init__(self) -> None:
        super().__init__(self.code)


LiveModelProviderFactory = Callable[[ResponseMetadataRecorder], ModelProvider]
LiveTraceFactory = Callable[..., AbstractContextManager[Any]]


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
        live_ready: bool = False,
        live_model_provider_factory: LiveModelProviderFactory | None = None,
        live_trace_factory: LiveTraceFactory | None = None,
        decision_lease_duration: timedelta = timedelta(seconds=30),
        decision_wait_interval: float = 0.02,
    ) -> None:
        self._store = store
        self._hotel_provider = hotel_provider
        self._hotel_provider.bind_store(store)
        self._version_policy = version_policy or ApprovalVersionPolicy.current()
        self._live_version_policy = ApprovalVersionPolicy.for_mode(
            ExecutionMode.OPENAI_LIVE
        )
        self._live_ready = live_ready
        self._live_model_provider_factory = live_model_provider_factory
        self._live_trace_factory = live_trace_factory or self._openai_trace
        if decision_lease_duration <= timedelta(0):
            raise ValueError("Decision lease duration must be positive")
        if decision_wait_interval <= 0:
            raise ValueError("Decision wait interval must be positive")
        self._decision_lease_duration = decision_lease_duration
        self._decision_wait_interval = decision_wait_interval
        self._reconcile_completed_executions()
        self._reconcile_claimed_decisions()

    @staticmethod
    def _openai_trace(*, trace_id: str, group_id: str) -> AbstractContextManager[Any]:
        return trace(
            "Backchannel live hotel recovery",
            trace_id=trace_id,
            group_id=group_id,
            metadata={
                "executionMode": ExecutionMode.OPENAI_LIVE.value,
                "providerBoundary": "demo_adapter_only",
            },
        )

    @staticmethod
    def _context_serializer(_context: HotelAgentContext) -> dict[str, Any]:
        """The process-owned context is rebuilt explicitly and never restored from JSON."""

        return {}

    def _receipt_for_execution(self, execution: DurableExecution) -> RecoveryReceipt:
        if execution.result_json is None:
            raise ValueError("Completed durable execution is missing its result")
        dispatch = HotelDispatchResult.model_validate(execution.result_json)
        envelope = self._store.get_pending_approval(execution.recovery_id)
        model_ids = list(
            dict.fromkeys(item.returned_model for item in envelope.model_metadata)
        )
        return RecoveryReceipt(
            recoveryId=execution.recovery_id,
            executionMode=envelope.execution_mode,
            status="completed",
            simulated=True,
            providerExecution=True,
            modelIds=model_ids,
            rootTraceId=envelope.root_trace_id,
            sdkVersion=envelope.sdk_version,
            protocolVersion=envelope.protocol_version,
            agentGraphVersion=envelope.agent_graph_version,
            promptToolSchemaHash=envelope.definition_digest,
            boundary=non_replay_receipt_boundary(envelope.execution_mode),
            providerResult=dispatch.provider_result,
            authorizationSource=APPROVED_RECEIPT_AUTHORIZATION,
            verificationResults=list(APPROVED_RECEIPT_VERIFICATIONS),
            decision="approved",
            decisionRemedyDigest=execution.remedy_digest,
            executionCount=1,
            providerDispatchStarted=True,
            exactInterruptionRejected=False,
            permissionRevoked=True,
            scopeClosed=True,
            approvedRemedyDigest=(
                execution.remedy_digest
                if execution.remedy_digest is not None
                and execution.remedy_digest.startswith("sha256:")
                else None
            ),
        )

    @staticmethod
    def _decision_response(claim: ApprovalDecisionClaim) -> ApprovalDecisionResponse:
        if claim.request.decision != "approve":
            raise TypeError("Approval response requires an approval claim")
        return ApprovalDecisionResponse(
            clientDecisionId=claim.request.client_decision_id,
            recoveryId=claim.recovery_id,
            decision="approve",
            status="completed",
            approvedRemedyDigest=claim.request.remedy_digest,
            executionStarted=True,
        )

    def _reconcile_completed_executions(self) -> None:
        """Finalize committed provider results without calling the provider again."""

        claims = {
            claim.recovery_id: claim for claim in self._store.list_claimed_decisions()
        }
        for execution in self._store.list_executions_needing_finalization():
            claim = claims.get(execution.recovery_id)
            if claim is None or claim.request.decision != "approve":
                continue
            owner_id = f"reconcile-{uuid4()}"
            lease = self._store.acquire_decision_resume(
                claim,
                resume_owner_id=owner_id,
                lease_duration=self._decision_lease_duration,
            )
            if lease.disposition != "owner":
                continue
            try:
                self._store.finalize_completed_execution(
                    execution,
                    receipt=self._receipt_for_execution(execution),
                    resume_owner_id=owner_id,
                    resume_generation=lease.resume_generation,
                )
                self._store.complete_approval_decision(
                    claim,
                    self._decision_response(claim),
                    resume_owner_id=owner_id,
                    resume_generation=lease.resume_generation,
                )
            except Exception:
                logger.exception(
                    "Failed to reconcile committed execution for recovery_id=%s",
                    execution.recovery_id,
                )
                raise

    def _reconcile_claimed_decisions(self) -> None:
        """Complete claimed responses whose durable dispatch already committed."""

        for claim in self._store.list_claimed_decisions():
            if claim.request.decision == "decline":
                try:
                    pending = self._store.get_pending_approval(claim.recovery_id)
                except (RecoveryNotFoundError, ValueError):
                    continue
                if pending.status != "rejected":
                    continue
                execution = None
            elif claim.request.decision == "approve":
                execution = self._store.get_completed_execution(claim.recovery_id)
                if execution is None:
                    continue
            else:
                continue
            owner_id = f"reconcile-{uuid4()}"
            lease = self._store.acquire_decision_resume(
                claim,
                resume_owner_id=owner_id,
                lease_duration=self._decision_lease_duration,
            )
            if lease.disposition != "owner":
                continue
            if claim.request.decision == "decline":
                self._store.finalize_declined_decision(
                    claim,
                    exact_interruption_rejected=True,
                    resume_owner_id=owner_id,
                    resume_generation=lease.resume_generation,
                )
                continue
            assert execution is not None
            self._store.finalize_completed_execution(
                execution,
                receipt=self._receipt_for_execution(execution),
                resume_owner_id=owner_id,
                resume_generation=lease.resume_generation,
            )
            self._store.complete_approval_decision(
                claim,
                self._decision_response(claim),
                resume_owner_id=owner_id,
                resume_generation=lease.resume_generation,
            )

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
        if execution_mode is ExecutionMode.OPENAI_LIVE:
            if approved_scenario is not ScenarioId.HOTEL:
                raise UnsupportedOrchestrationError(
                    "OpenAI live supports the hotel scenario only"
                )
            if not self._live_ready or self._live_model_provider_factory is None:
                raise LiveUnavailableError
            return await self._start_live_hotel()
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
        action_digest = remedy_action_digest(arguments)
        context = HotelAgentContext(
            recovery_id=recovery_id,
            store=self._store,
            hotel_provider=self._hotel_provider,
            execution_mode=ExecutionMode.SDK_STUB,
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
        terms = exact_hotel_terms(arguments)
        expiry = datetime.now(UTC) + timedelta(minutes=30)
        changed_fields = tuple(sorted(arguments.remedy.changed_fields))
        provider_commitments = tuple(sorted(arguments.remedy.provider_commitments))
        consent_digest = remedy_consent_digest(
            {
                "recoveryId": recovery_id,
                "remedyId": arguments.remedy.remedy_id,
                "terms": terms.model_dump(mode="json", by_alias=True),
                "costDeltaMinor": arguments.remedy.cost_delta_minor,
                "changedFields": list(changed_fields),
                "providerCommitments": list(provider_commitments),
                "expiry": expiry,
            }
        )
        policy_result = evaluate_hotel_policy(
            arguments,
            DETERMINISTIC_HOTEL_AUTHORITY,
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
            action_digest=action_digest,
            remedy_id=arguments.remedy.remedy_id,
            consent_digest=consent_digest,
            state_json=state_json,
            model_metadata=(),
        )
        remedy_consent = RemedyConsentRecord(
            remedy_id=arguments.remedy.remedy_id,
            recovery_id=recovery_id,
            terms=terms,
            cost_delta_minor=arguments.remedy.cost_delta_minor,
            changed_fields=changed_fields,
            provider_commitments=provider_commitments,
            expiry=expiry,
            consent_digest=consent_digest,
            hard_constraint_satisfied=policy_result.hard_constraint_satisfied,
            delegated_authority_satisfied=(
                policy_result.delegated_authority_satisfied
            ),
            evidence=arguments,
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
            remedy_consent=remedy_consent,
        )
        return PendingSdkApproval(
            recovery=recovery,
            sdk_result=result,
            original_root_agent=original_root_agent,
        )

    async def _start_live_hotel(self) -> PendingSdkApproval:
        """Run three live Agents under one persisted trace to exact approval."""

        if self._live_model_provider_factory is None:
            raise LiveUnavailableError
        recovery_id = str(uuid4())
        root_trace_id = gen_trace_id()
        recorder = ResponseMetadataRecorder()
        model_provider = self._live_model_provider_factory(recorder)
        run_config = configure_openai_live_tracing(model_provider=model_provider)
        context = HotelAgentContext(
            recovery_id=recovery_id,
            store=self._store,
            hotel_provider=self._hotel_provider,
            execution_mode=ExecutionMode.OPENAI_LIVE,
            root_trace_id=root_trace_id,
            sdk_version=self._live_version_policy.sdk_version,
            protocol_version=self._live_version_policy.protocol_version,
            agent_graph_version=self._live_version_policy.agent_graph_version,
        )
        graph = build_live_hotel_agents()
        definition_digest = live_hotel_definition_digest(
            (graph.consumer, graph.provider, graph.broker)
        )
        authoritative_arguments = deterministic_hotel_arguments()
        try:
            with self._live_trace_factory(
                trace_id=root_trace_id,
                group_id=recovery_id,
            ):
                consumer_result = await Runner.run(
                    graph.consumer,
                    LIVE_CONSUMER_PROMPT,
                    context=context,
                    run_config=run_config,
                )
                if not isinstance(consumer_result.final_output, ConsumerProof):
                    raise RuntimeError("Live consumer Agent returned incompatible output")
                if consumer_result.final_output != authoritative_arguments.consumer_proof:
                    raise RuntimeError(
                        "Live consumer proof changed the fixed source evidence"
                    )
                provider_result = await Runner.run(
                    graph.provider,
                    LIVE_PROVIDER_PROMPT,
                    context=context,
                    run_config=run_config,
                )
                if not isinstance(provider_result.final_output, ProviderProof):
                    raise RuntimeError("Live provider Agent returned incompatible output")
                if provider_result.final_output != authoritative_arguments.provider_proof:
                    raise RuntimeError(
                        "Live provider proof changed the fixed source evidence"
                    )
                proposed = authoritative_arguments.remedy
                proposed_arguments = CommitRemedyArguments(
                    consumer_proof=consumer_result.final_output,
                    provider_proof=provider_result.final_output,
                    remedy=proposed,
                )
                broker_result = await Runner.run(
                    graph.broker,
                    live_broker_prompt(proposed_arguments),
                    context=context,
                    run_config=run_config,
                )
            if len(broker_result.interruptions) != 1:
                raise RuntimeError("Live broker did not produce exactly one interruption")
            interruption = broker_result.interruptions[0]
            if interruption.tool_name != "commit_remedy" or not interruption.call_id:
                raise RuntimeError("Live broker interrupted on an unexpected tool")
            try:
                arguments = CommitRemedyArguments.model_validate_json(
                    interruption.arguments or ""
                )
            except ValueError:
                raise RuntimeError("Live broker produced invalid commit arguments") from None
            if arguments != proposed_arguments:
                raise RuntimeError("Live broker changed the supplied exact remedy evidence")
            metadata = recorder.snapshot()
            if tuple(item.requested_model for item in metadata) != (
                "gpt-5.6-luna",
                "gpt-5.6-luna",
                "gpt-5.6-terra",
            ):
                raise RuntimeError("Live graph model resolution was incomplete")

            state_json = broker_result.to_state().to_json(
                context_serializer=self._context_serializer,
                strict_context=True,
            )
            action_digest = remedy_action_digest(arguments)
            terms = exact_hotel_terms(arguments)
            expiry = datetime.now(UTC) + timedelta(minutes=30)
            changed_fields = tuple(sorted(arguments.remedy.changed_fields))
            provider_commitments = tuple(
                sorted(arguments.remedy.provider_commitments)
            )
            consent_digest = remedy_consent_digest(
                {
                    "recoveryId": recovery_id,
                    "remedyId": arguments.remedy.remedy_id,
                    "terms": terms.model_dump(mode="json", by_alias=True),
                    "costDeltaMinor": arguments.remedy.cost_delta_minor,
                    "changedFields": list(changed_fields),
                    "providerCommitments": list(provider_commitments),
                    "expiry": expiry,
                }
            )
            policy_result = evaluate_hotel_policy(
                arguments,
                DETERMINISTIC_HOTEL_AUTHORITY,
            )
            envelope = PendingApprovalEnvelope(
                tool_call_id=interruption.call_id,
                recovery_id=recovery_id,
                sdk_version=self._live_version_policy.sdk_version,
                protocol_version=self._live_version_policy.protocol_version,
                agent_graph_version=self._live_version_policy.agent_graph_version,
                definition_digest=definition_digest,
                root_trace_id=root_trace_id,
                execution_mode=ExecutionMode.OPENAI_LIVE,
                action_digest=action_digest,
                remedy_id=arguments.remedy.remedy_id,
                consent_digest=consent_digest,
                state_json=state_json,
                model_metadata=metadata,
            )
            remedy_consent = RemedyConsentRecord(
                remedy_id=arguments.remedy.remedy_id,
                recovery_id=recovery_id,
                terms=terms,
                cost_delta_minor=arguments.remedy.cost_delta_minor,
                changed_fields=changed_fields,
                provider_commitments=provider_commitments,
                expiry=expiry,
                consent_digest=consent_digest,
                hard_constraint_satisfied=policy_result.hard_constraint_satisfied,
                delegated_authority_satisfied=(
                    policy_result.delegated_authority_satisfied
                ),
                evidence=arguments,
            )
            recovery = self._store.create_pending_recovery(
                recovery_id=recovery_id,
                scenario_id=ScenarioId.HOTEL,
                execution_mode=ExecutionMode.OPENAI_LIVE,
                current_step_summary=(
                    "Approval required before live demo-provider dispatch."
                ),
                event_data={
                    "phase": "Authorize",
                    "providerExecution": False,
                    "summary": "A live Agents SDK approval interruption is pending.",
                },
                pending_approval=envelope,
                remedy_consent=remedy_consent,
            )
        except BaseException:
            raise
        return PendingSdkApproval(
            recovery=recovery,
            sdk_result=broker_result,
            original_root_agent=graph.broker,
        )

    async def _acquire_resume_lease(
        self,
        claim: ApprovalDecisionClaim,
    ) -> DecisionResumeLease:
        owner_id = f"resume-{uuid4()}"
        while True:
            lease = self._store.acquire_decision_resume(
                claim,
                resume_owner_id=owner_id,
                lease_duration=self._decision_lease_duration,
            )
            if lease.disposition != "wait":
                return lease
            await asyncio.sleep(self._decision_wait_interval)

    async def _heartbeat_resume_lease(
        self,
        claim: ApprovalDecisionClaim,
        *,
        resume_owner_id: str,
        resume_generation: int,
    ) -> None:
        interval = self._decision_lease_duration.total_seconds() / 3
        while True:
            await asyncio.sleep(interval)
            if not self._store.renew_decision_resume(
                claim,
                resume_owner_id=resume_owner_id,
                resume_generation=resume_generation,
                lease_duration=self._decision_lease_duration,
            ):
                return

    @staticmethod
    async def _cancel_heartbeat(task: asyncio.Task[None]) -> None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def _complete_committed_claim(
        self,
        claim: ApprovalDecisionClaim,
        execution: DurableExecution,
        *,
        resume_owner_id: str,
        resume_generation: int,
    ) -> ApprovalDecisionResponse:
        self._store.finalize_completed_execution(
            execution,
            receipt=self._receipt_for_execution(execution),
            resume_owner_id=resume_owner_id,
            resume_generation=resume_generation,
        )
        return self._store.complete_approval_decision(
            claim,
            self._decision_response(claim),
            resume_owner_id=resume_owner_id,
            resume_generation=resume_generation,
        )

    async def approve_decision(
        self,
        recovery_id: str,
        request: ApprovalDecisionRequest,
    ) -> ApprovalDecisionResponse:
        """Claim, resume, and durably replay one exact approval decision."""

        if request.decision != "approve":
            raise ValueError("approve_decision requires decision=approve")
        claim = self._store.claim_decision(recovery_id, request)
        if claim.response is not None:
            if not isinstance(claim.response, ApprovalDecisionResponse):
                raise TypeError("Approval request replayed a decline response")
            return claim.response
        lease = await self._acquire_resume_lease(claim)
        if lease.disposition == "replay":
            if not isinstance(lease.claim.response, ApprovalDecisionResponse):
                raise TypeError("Approval request replayed a decline response")
            return lease.claim.response
        resume_owner_id = lease.resume_owner_id
        if resume_owner_id is None:
            raise RuntimeError("Decision owner lease is missing its owner ID")
        resume_generation = lease.resume_generation
        heartbeat = asyncio.create_task(
            self._heartbeat_resume_lease(
                claim,
                resume_owner_id=resume_owner_id,
                resume_generation=resume_generation,
            )
        )
        try:
            execution = self._store.get_completed_execution(recovery_id)
            if execution is not None:
                return self._complete_committed_claim(
                    claim,
                    execution,
                    resume_owner_id=resume_owner_id,
                    resume_generation=resume_generation,
                )
            try:
                completed = await self._resume_claimed_approval(
                    claim,
                    resume_owner_id=resume_owner_id,
                    resume_generation=resume_generation,
                )
            except ApprovalDecisionError:
                execution = self._store.get_completed_execution(recovery_id)
                if execution is None:
                    raise
                return self._complete_committed_claim(
                    claim,
                    execution,
                    resume_owner_id=resume_owner_id,
                    resume_generation=resume_generation,
                )
            except UserError as sdk_error:
                try:
                    self._store.get_receipt(recovery_id)
                except RecoveryNotFoundError:
                    raise sdk_error
                execution = self._store.get_completed_execution(recovery_id)
                if execution is None:
                    raise
                return self._complete_committed_claim(
                    claim,
                    execution,
                    resume_owner_id=resume_owner_id,
                    resume_generation=resume_generation,
                )
            if completed is None:
                raise ApprovalDecisionError(
                    "resume_owner_lost", recovery_id, status_code=409
                )
            execution = self._store.get_completed_execution(recovery_id)
            if execution is None:
                raise RuntimeError("Approved SDK run completed without a durable execution")
            return self._complete_committed_claim(
                claim,
                execution,
                resume_owner_id=resume_owner_id,
                resume_generation=resume_generation,
            )
        finally:
            await self._cancel_heartbeat(heartbeat)
            self._store.release_decision_resume(
                claim,
                resume_owner_id=resume_owner_id,
                resume_generation=resume_generation,
            )

    async def decline_decision(
        self,
        recovery_id: str,
        request: ApprovalDecisionRequest,
    ) -> DeclineDecisionResponse:
        """Reject the exact SDK interruption, then atomically seal closure evidence."""

        if request.decision != "decline":
            raise ValueError("decline_decision requires decision=decline")
        claim = self._store.claim_decision(recovery_id, request)
        if claim.response is not None:
            if not isinstance(claim.response, DeclineDecisionResponse):
                raise TypeError("Decline request replayed an approval response")
            return claim.response
        lease = await self._acquire_resume_lease(claim)
        if lease.disposition == "replay":
            if not isinstance(lease.claim.response, DeclineDecisionResponse):
                raise TypeError("Decline request replayed an approval response")
            return lease.claim.response
        resume_owner_id = lease.resume_owner_id
        if resume_owner_id is None:
            raise RuntimeError("Decision owner lease is missing its owner ID")
        resume_generation = lease.resume_generation
        heartbeat = asyncio.create_task(
            self._heartbeat_resume_lease(
                claim,
                resume_owner_id=resume_owner_id,
                resume_generation=resume_generation,
            )
        )
        try:
            pending = self._store.get_pending_approval(recovery_id)
            if pending.status != "rejected":
                await self._resume_claimed_decline(
                    claim,
                    resume_owner_id=resume_owner_id,
                    resume_generation=resume_generation,
                )
            return self._store.finalize_declined_decision(
                claim,
                exact_interruption_rejected=True,
                resume_owner_id=resume_owner_id,
                resume_generation=resume_generation,
            )
        finally:
            await self._cancel_heartbeat(heartbeat)
            self._store.release_decision_resume(
                claim,
                resume_owner_id=resume_owner_id,
                resume_generation=resume_generation,
            )

    async def decide(
        self,
        recovery_id: str,
        request: ApprovalDecisionRequest,
    ) -> DecisionResponse:
        """Dispatch one required, durable approve-or-decline decision."""

        if request.decision == "approve":
            return await self.approve_decision(recovery_id, request)
        return await self.decline_decision(recovery_id, request)

    async def _resume_claimed_approval(
        self,
        claim: ApprovalDecisionClaim,
        *,
        resume_owner_id: str,
        resume_generation: int,
    ) -> RunResult | None:
        """Restore and approve only after a durable exact-consent claim."""

        return await self._resume_claimed_decision(
            claim,
            approve=True,
            resume_owner_id=resume_owner_id,
            resume_generation=resume_generation,
        )

    async def _resume_claimed_decline(
        self,
        claim: ApprovalDecisionClaim,
        *,
        resume_owner_id: str,
        resume_generation: int,
    ) -> RunResult | None:
        """Restore and reject only the exact interruption bound to the claim."""

        return await self._resume_claimed_decision(
            claim,
            approve=False,
            resume_owner_id=resume_owner_id,
            resume_generation=resume_generation,
        )

    async def _resume_claimed_decision(
        self,
        claim: ApprovalDecisionClaim,
        *,
        approve: bool,
        resume_owner_id: str,
        resume_generation: int,
    ) -> RunResult | None:
        """Restore and consent-bind one exact SDK approval interruption."""

        recovery_id = claim.recovery_id
        expected_decision = "approve" if approve else "decline"
        if claim.request.decision != expected_decision:
            raise ValueError("Decision claim action does not match resume path")
        self._store.validate_claimed_decision(
            claim,
            resume_owner_id=resume_owner_id,
            resume_generation=resume_generation,
        )
        try:
            recovery = self._store.get_recovery(recovery_id)
            envelope = self._store.get_pending_approval(recovery_id)
            consent = self._store.get_remedy_consent(recovery_id)
        except (RecoveryNotFoundError, ValueError, TypeError):
            self._raise_incompatible(recovery_id, "envelope")

        arguments = consent.evidence
        expected_action_digest = remedy_action_digest(arguments)
        recorder: ResponseMetadataRecorder | None = None
        if envelope.execution_mode is ExecutionMode.OPENAI_LIVE:
            if not self._live_ready or self._live_model_provider_factory is None:
                raise LiveUnavailableError
            recorder = ResponseMetadataRecorder(
                envelope.model_metadata,
                on_record=lambda metadata: self._store.update_pending_model_metadata(
                    recovery_id,
                    metadata,
                    resume_owner_id=resume_owner_id,
                    resume_generation=resume_generation,
                ),
            )
            model_provider = self._live_model_provider_factory(recorder)
            live_graph = build_live_hotel_agents()
            fresh_agent = live_graph.broker
            expected_policy = self._live_version_policy
            expected_definition_digest = live_hotel_definition_digest(
                (live_graph.consumer, live_graph.provider, live_graph.broker)
            )
            expected_tool_call_id = claim.request.tool_call_id
            run_config = configure_openai_live_tracing(
                model_provider=model_provider
            )
        elif envelope.execution_mode is ExecutionMode.SDK_STUB:
            expected_policy = self._version_policy
            fresh_agent = build_hotel_agent(
                context=HotelAgentContext(
                    recovery_id=recovery_id,
                    store=self._store,
                    hotel_provider=self._hotel_provider,
                ),
                arguments=arguments,
            )
            expected_definition_digest = hotel_definition_digest(fresh_agent)
            expected_tool_call_id = f"commit-remedy-{recovery_id}"
            run_config = configure_sdk_stub_tracing()
        else:
            self._raise_incompatible(recovery_id, "execution_mode")
        fresh_context = HotelAgentContext(
            recovery_id=recovery_id,
            store=self._store,
            hotel_provider=self._hotel_provider,
            approved_remedy_digest=claim.request.remedy_digest,
            execution_mode=envelope.execution_mode,
            root_trace_id=envelope.root_trace_id,
            sdk_version=envelope.sdk_version,
            protocol_version=envelope.protocol_version,
            agent_graph_version=envelope.agent_graph_version,
            definition_digest=envelope.definition_digest,
            resume_owner_id=resume_owner_id,
            resume_generation=resume_generation,
        )
        if envelope.execution_mode is ExecutionMode.SDK_STUB:
            fresh_agent = build_hotel_agent(
                context=fresh_context,
                arguments=arguments,
            )

        expected_markers = {
            "sdk_version": expected_policy.sdk_version,
            "protocol_version": expected_policy.protocol_version,
            "agent_graph_version": expected_policy.agent_graph_version,
            "definition_digest": expected_definition_digest,
            "execution_mode": recovery.execution_mode,
            "tool_call_id": expected_tool_call_id,
            "action_digest": expected_action_digest,
            "remedy_id": arguments.remedy.remedy_id,
            "consent_digest": claim.request.remedy_digest,
        }
        actual_markers = {
            "sdk_version": envelope.sdk_version,
            "protocol_version": envelope.protocol_version,
            "agent_graph_version": envelope.agent_graph_version,
            "definition_digest": envelope.definition_digest,
            "execution_mode": envelope.execution_mode,
            "tool_call_id": envelope.tool_call_id,
            "action_digest": envelope.action_digest,
            "remedy_id": envelope.remedy_id,
            "consent_digest": envelope.consent_digest,
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
        allowed_envelope_statuses = (
            {"pending", "approved"} if approve else {"pending", "declined"}
        )
        if envelope.status not in allowed_envelope_statuses:
            self._raise_incompatible(recovery_id, "approval_status")
        trace_id_valid = (
            is_valid_openai_trace_id(envelope.root_trace_id)
            if envelope.execution_mode is ExecutionMode.OPENAI_LIVE
            else is_valid_qa_trace_id(envelope.root_trace_id)
        )
        if not trace_id_valid:
            self._raise_incompatible(recovery_id, "root_trace_id")
        if envelope.execution_mode is ExecutionMode.OPENAI_LIVE:
            requested_models = tuple(
                item.requested_model for item in envelope.model_metadata[:3]
            )
            if requested_models != (
                "gpt-5.6-luna",
                "gpt-5.6-luna",
                "gpt-5.6-terra",
            ):
                self._raise_incompatible(recovery_id, "model_metadata")
        if not isinstance(envelope.state_json, dict) or not envelope.state_json:
            self._raise_incompatible(recovery_id, "state_json")
        recomputed_consent_digest = remedy_consent_digest(
            {
                "recoveryId": consent.recovery_id,
                "remedyId": consent.remedy_id,
                "terms": consent.terms.model_dump(mode="json", by_alias=True),
                "costDeltaMinor": consent.cost_delta_minor,
                "changedFields": list(consent.changed_fields),
                "providerCommitments": list(consent.provider_commitments),
                "expiry": consent.expiry,
            }
        )
        if recomputed_consent_digest != envelope.consent_digest:
            self._raise_incompatible(recovery_id, "consent_digest_recomputed")
        if exact_hotel_terms(consent.evidence) != consent.terms:
            self._raise_incompatible(recovery_id, "consent_terms_evidence")
        if approve:
            policy_result = evaluate_hotel_policy(
                consent.evidence,
                DETERMINISTIC_HOTEL_AUTHORITY,
            )
            if (
                policy_result.hard_constraint_satisfied
                != consent.hard_constraint_satisfied
            ):
                self._raise_incompatible(recovery_id, "hard_constraint_result")
            if not policy_result.hard_constraint_satisfied:
                self._raise_incompatible(recovery_id, "hard_constraint_denied")
            if (
                policy_result.delegated_authority_satisfied
                != consent.delegated_authority_satisfied
            ):
                self._raise_incompatible(recovery_id, "authority_result")
            if not policy_result.delegated_authority_satisfied:
                self._raise_incompatible(recovery_id, "authority_denied")

        try:
            state = await RunState.from_json(
                fresh_agent,
                envelope.state_json,
                context_override=RunContextWrapper(context=fresh_context),
                strict_context=True,
            )
        except Exception:
            logger.error(
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
        if restored_arguments != consent.evidence:
            self._raise_incompatible(recovery_id, "restored_evidence")
        if remedy_action_digest(restored_arguments) != envelope.action_digest:
            self._raise_incompatible(recovery_id, "restored_action_digest")

        restored_consent_digest = remedy_consent_digest(
            {
                "recoveryId": recovery_id,
                "remedyId": restored_arguments.remedy.remedy_id,
                "terms": exact_hotel_terms(restored_arguments).model_dump(
                    mode="json",
                    by_alias=True,
                ),
                "costDeltaMinor": restored_arguments.remedy.cost_delta_minor,
                "changedFields": sorted(restored_arguments.remedy.changed_fields),
                "providerCommitments": sorted(
                    restored_arguments.remedy.provider_commitments
                ),
                "expiry": consent.expiry,
            }
        )
        if restored_consent_digest != envelope.consent_digest:
            self._raise_incompatible(recovery_id, "restored_consent_digest")

        validated_claim = self._store.validate_claimed_decision(
            claim,
            resume_owner_id=resume_owner_id,
            resume_generation=resume_generation,
        )
        if validated_claim.response is not None:
            return None
        execution_count_before = self._store.count_executions(recovery_id)
        if approve:
            state.approve(interruption)
        else:
            state.reject(
                interruption,
                always_reject=False,
                rejection_message=DECLINE_MESSAGE,
            )
        if envelope.execution_mode is ExecutionMode.OPENAI_LIVE:
            with self._live_trace_factory(
                trace_id=envelope.root_trace_id,
                group_id=recovery_id,
            ):
                completed = await Runner.run(
                    fresh_agent,
                    state,
                    run_config=run_config,
                )
        else:
            completed = await Runner.run(
                fresh_agent,
                state,
                run_config=run_config,
            )
        if not approve:
            if self._store.count_executions(recovery_id) != execution_count_before:
                raise RuntimeError("SDK rejection unexpectedly changed execution evidence")
            if envelope.execution_mode is ExecutionMode.OPENAI_LIVE:
                if not isinstance(completed.final_output, BrokerOutcome) or (
                    completed.final_output.status != "closed_without_action"
                ):
                    raise RuntimeError("Live rejection did not close without action")
            elif completed.final_output != CLOSED_WITHOUT_ACTION_MESSAGE:
                raise RuntimeError("Deterministic rejection did not close without action")
            self._store.record_exact_interruption_rejected(
                claim,
                resume_owner_id=resume_owner_id,
                resume_generation=resume_generation,
            )
        elif envelope.execution_mode is ExecutionMode.OPENAI_LIVE:
            if not isinstance(completed.final_output, BrokerOutcome) or (
                completed.final_output.status != "completed"
            ):
                raise RuntimeError("Live approval did not return completed broker output")
        return completed
