"""Coordinate deterministic recoveries through the actual Agents SDK runner."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn
from uuid import uuid4

from agents import (
    Agent,
    RunContextWrapper,
    Runner,
    RunResult,
    RunState,
    gen_trace_id,
    trace,
)
from agents.models.interface import ModelProvider

from server.agents.factory import (
    HotelAgentContext,
    build_hotel_agent,
    build_live_hotel_agents,
)
from server.agents.schemas import (
    CommitRemedyArguments,
    ConsumerProof,
    ProviderProof,
    deterministic_hotel_arguments,
)
from server.agents.stub_model import (
    EXACT_REMEDY_REJECTION_MESSAGE,
)
from server.agents.tracing import (
    LIVE_WORKFLOW_NAME,
    configure_live_tracing,
    configure_sdk_stub_tracing,
    live_trace_metadata,
)
from server.agents.versioning import (
    HOTEL_START_PROMPT,
    LIVE_BROKER_MODEL,
    LIVE_CONSUMER_MODEL,
    LIVE_CONSUMER_START_PROMPT,
    LIVE_PROVIDER_START_PROMPT,
    ApprovalVersionPolicy,
    hotel_definition_digest,
    live_broker_start_prompt,
    live_hotel_definition_digest,
    new_qa_trace_id,
    remedy_action_digest,
)
from server.digest import remedy_consent_digest
from server.logging import get_safe_logger
from server.models import (
    QUOTA_SDK_INITIAL_SUMMARY,
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    DecisionAction,
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
from server.providers.hotel_simulator import HotelSimulator
from server.providers.quota_simulator import (
    QUOTA_AGENT_GRAPH_VERSION,
    QUOTA_PROTOCOL_VERSION,
    QUOTA_START_PROMPT,
    QuotaAgentContext,
    QuotaRecoveryResult,
    QuotaSimulator,
    build_quota_agent,
    quota_definition_digest,
)
from server.store import (
    QUOTA_EXECUTION_RESULT_RECORDED,
    ApprovalDecisionClaim,
    ApprovalDecisionError,
    DurableExecution,
    ExecutionConflictError,
    PendingApprovalEnvelope,
    ReceiptTransitionError,
    RecoveryNotFoundError,
    RemedyConsentRecord,
    SQLiteStore,
)
from server.trace_ids import is_valid_live_trace_id, is_valid_qa_trace_id

logger = get_safe_logger(__name__)


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


@dataclass(frozen=True, slots=True)
class ValidatedDecisionResume:
    recovery: RecoverySnapshot
    envelope: PendingApprovalEnvelope
    consent: RemedyConsentRecord
    context: HotelAgentContext
    agent: Agent[HotelAgentContext]


@dataclass(frozen=True, slots=True)
class CompletedSdkRecovery:
    recovery: RecoverySnapshot
    sdk_result: RunResult


class RecoveryOrchestrator:
    def __init__(
        self,
        *,
        store: SQLiteStore,
        hotel_provider: HotelSimulator,
        quota_provider: QuotaSimulator | None = None,
        version_policy: ApprovalVersionPolicy | None = None,
        live_ready: bool = False,
        model_provider: ModelProvider | None = None,
    ) -> None:
        self._store = store
        self._hotel_provider = hotel_provider
        self._hotel_provider.bind_store(store)
        self._quota_provider = quota_provider or QuotaSimulator()
        self._version_policy = version_policy or ApprovalVersionPolicy.current()
        self._live_ready = live_ready
        self._model_provider = model_provider
        self._reconcile_claimed_decisions()
        self._reconcile_quota_executions()
        self._reconcile_completed_executions()

    @staticmethod
    def _context_serializer(_context: HotelAgentContext) -> dict[str, Any]:
        """The process-owned context is rebuilt explicitly and never restored from JSON."""

        return {}

    def _receipt_for_execution(self, execution: DurableExecution) -> RecoveryReceipt:
        return self._store.completed_receipt_for_execution(execution)

    @staticmethod
    def _decision_response(claim: ApprovalDecisionClaim) -> ApprovalDecisionResponse:
        if claim.request.action is not DecisionAction.APPROVE:
            raise ValueError("Execution responses require an approve decision")
        return ApprovalDecisionResponse(
            action=DecisionAction.APPROVE,
            clientDecisionId=claim.request.client_decision_id,
            recoveryId=claim.recovery_id,
            status="completed",
            approvedRemedyDigest=claim.request.remedy_digest,
            executionStarted=True,
        )

    def _reconcile_completed_executions(self) -> None:
        """Finalize committed provider results without calling the provider again."""

        for execution in self._store.list_executions_needing_finalization():
            try:
                self._store.finalize_completed_execution(
                    execution,
                    receipt=self._receipt_for_execution(execution),
                )
            except Exception as error:
                logger.error(
                    "execution_reconcile_failed recovery_id=%s error_type=%s",
                    execution.recovery_id,
                    type(error).__name__,
                )
                raise

    def _reconcile_quota_executions(self) -> None:
        """Finalize durable quota results or conservatively seal stale claims."""

        for execution in self._store.list_quota_executions_needing_reconciliation():
            try:
                if execution.status == "completed":
                    self._store.finalize_completed_quota_execution(execution)
                elif execution.status == QUOTA_EXECUTION_RESULT_RECORDED:
                    try:
                        self._store.quarantine_quota_execution_invariant_failure(execution)
                    except ExecutionConflictError:
                        current = self._store.get_quota_execution(execution.recovery_id)
                        if current is None or current.status != "completed":
                            raise
                        self._store.finalize_completed_quota_execution(current)
                    else:
                        raise ExecutionConflictError(
                            "Quota SDK completion was not durably validated"
                        )
                elif execution.status == "pending":
                    self._store.finalize_pending_quota_execution_unknown(execution)
                else:
                    raise ValueError("Quota execution has no reconcilable durable state")
            except Exception as error:
                logger.error(
                    "quota_execution_reconcile_failed recovery_id=%s error_type=%s",
                    execution.recovery_id,
                    type(error).__name__,
                )
                raise

    def _reconcile_claimed_decisions(self) -> None:
        """Complete claimed responses whose durable dispatch already committed."""

        for claim in self._store.list_claimed_decisions():
            if claim.request.action is not DecisionAction.APPROVE:
                continue
            try:
                execution = self._store.get_completed_execution(claim.recovery_id)
                if execution is None:
                    continue
                self._complete_committed_claim(claim, execution)
            except (
                ApprovalDecisionError,
                ReceiptTransitionError,
                TypeError,
                ValueError,
            ) as error:
                try:
                    pending_recovery = self._store.get_recovery(claim.recovery_id)
                except (RecoveryNotFoundError, TypeError, ValueError):
                    raise
                if pending_recovery.status is not RecoveryStatus.PENDING_APPROVAL:
                    raise
                logger.warning(
                    "claimed_execution_reconcile_deferred recovery_id=%s error_type=%s",
                    claim.recovery_id,
                    type(error).__name__,
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
        recovery_id: str | None = None,
        session_key: str | None = None,
    ) -> PendingSdkApproval | CompletedSdkRecovery:
        try:
            approved_scenario = ScenarioId(scenario_id)
        except ValueError as error:
            raise UnsupportedOrchestrationError("Unknown scenario") from error
        if approved_scenario is ScenarioId.API_QUOTA:
            if execution_mode is not ExecutionMode.SDK_STUB:
                raise UnsupportedOrchestrationError(
                    "API quota recovery supports sdk_stub only in orchestration"
                )
            return await self._start_quota_sdk(
                recovery_id=recovery_id,
                session_key=session_key,
            )
        if execution_mode is ExecutionMode.OPENAI_LIVE and not self._live_ready:
            raise UnsupportedOrchestrationError(
                "openai_live requires a server-side live-ready runtime"
            )
        if approved_scenario is not ScenarioId.HOTEL or execution_mode not in {
            ExecutionMode.SDK_STUB,
            ExecutionMode.OPENAI_LIVE,
        }:
            raise UnsupportedOrchestrationError(
                "Agents SDK orchestration supports hotel recovery only"
            )

        recovery_id = recovery_id or str(uuid4())
        context = HotelAgentContext(
            recovery_id=recovery_id,
            store=self._store,
            hotel_provider=self._hotel_provider,
        )
        if execution_mode is ExecutionMode.SDK_STUB:
            root_trace_id = new_qa_trace_id()
            model_ids: list[str] = []
            arguments = deterministic_hotel_arguments()
            original_root_agent = build_hotel_agent(
                context=context,
                arguments=arguments,
            )
            agent_graph_version = self._version_policy.agent_graph_version
            definition_digest = hotel_definition_digest(original_root_agent)
            recovery_summary = "Deterministic Agents SDK recovery started."
            result = await Runner.run(
                original_root_agent,
                HOTEL_START_PROMPT,
                context=context,
                run_config=configure_sdk_stub_tracing(),
            )
        else:
            root_trace_id = gen_trace_id()
            model_ids = [LIVE_CONSUMER_MODEL, LIVE_BROKER_MODEL]
            live_agents = build_live_hotel_agents(context=context)
            original_root_agent = live_agents.broker
            agent_graph_version = self._version_policy.live_agent_graph_version
            definition_digest = live_hotel_definition_digest(
                consumer_agent=live_agents.consumer,
                provider_agent=live_agents.provider,
                broker_agent=live_agents.broker,
            )
            live_run_config = configure_live_tracing(
                recovery_id=recovery_id,
                root_trace_id=root_trace_id,
                model_provider=self._model_provider,
            )
            with trace(
                LIVE_WORKFLOW_NAME,
                trace_id=root_trace_id,
                group_id=recovery_id,
                metadata=live_trace_metadata(),
            ):
                consumer_result = await Runner.run(
                    live_agents.consumer,
                    LIVE_CONSUMER_START_PROMPT,
                    context=context,
                    run_config=live_run_config,
                )
                consumer_proof = consumer_result.final_output_as(
                    ConsumerProof,
                    raise_if_incorrect_type=True,
                )
                provider_result = await Runner.run(
                    live_agents.provider,
                    LIVE_PROVIDER_START_PROMPT,
                    context=context,
                    run_config=live_run_config,
                )
                provider_proof = provider_result.final_output_as(
                    ProviderProof,
                    raise_if_incorrect_type=True,
                )
                result = await Runner.run(
                    live_agents.broker,
                    live_broker_start_prompt(consumer_proof, provider_proof),
                    context=context,
                    run_config=live_run_config,
                )
            if len(result.interruptions) != 1:
                raise RuntimeError("Live SDK run did not produce exactly one interruption")
            live_interruption = result.interruptions[0]
            try:
                arguments = CommitRemedyArguments.model_validate_json(
                    live_interruption.arguments or ""
                )
            except ValueError as error:
                raise RuntimeError("Live broker returned invalid remedy arguments") from error
            if (
                arguments.consumer_proof != consumer_proof
                or arguments.provider_proof != provider_proof
            ):
                raise RuntimeError("Live broker changed the validated proof outputs")
            recovery_summary = "OpenAI Agents SDK recovery started."

        if len(result.interruptions) != 1:
            raise RuntimeError("SDK run did not produce exactly one interruption")
        interruption = result.interruptions[0]
        if interruption.tool_name != "commit_remedy":
            raise RuntimeError("SDK run interrupted on an unexpected tool")
        if not interruption.call_id:
            raise RuntimeError("SDK interruption is missing its call ID")
        try:
            interruption_arguments = CommitRemedyArguments.model_validate_json(
                interruption.arguments or ""
            )
        except ValueError as error:
            raise RuntimeError("SDK interruption returned invalid remedy arguments") from error
        if interruption_arguments != arguments:
            raise RuntimeError("SDK interruption changed validated remedy arguments")
        arguments = interruption_arguments

        policy_result = evaluate_hotel_policy(
            arguments,
            DETERMINISTIC_HOTEL_AUTHORITY,
        )
        if (
            not policy_result.hard_constraint_satisfied
            or not policy_result.delegated_authority_satisfied
        ):
            raise RuntimeError("policy_ineligible")

        state_json = result.to_state().to_json(
            context_serializer=self._context_serializer,
            strict_context=True,
        )
        action_digest = remedy_action_digest(arguments)
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
        self._store.create_recovery(
            recovery_id=recovery_id,
            scenario_id=approved_scenario,
            execution_mode=execution_mode,
            current_step=0,
            current_step_summary=recovery_summary,
            model_ids=model_ids,
            root_trace_id=root_trace_id,
            model_call=execution_mode is ExecutionMode.OPENAI_LIVE,
            sdk_version=self._version_policy.sdk_version,
            protocol_version=self._version_policy.protocol_version,
            agent_graph_version=agent_graph_version,
            definition_digest=definition_digest,
            session_key=session_key,
        )
        envelope = PendingApprovalEnvelope(
            tool_call_id=interruption.call_id,
            recovery_id=recovery_id,
            sdk_version=self._version_policy.sdk_version,
            protocol_version=self._version_policy.protocol_version,
            agent_graph_version=agent_graph_version,
            definition_digest=definition_digest,
            root_trace_id=root_trace_id,
            model_ids=tuple(model_ids),
            execution_mode=execution_mode,
            action_digest=action_digest,
            remedy_id=arguments.remedy.remedy_id,
            consent_digest=consent_digest,
            state_json=state_json,
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
            delegated_authority_satisfied=(policy_result.delegated_authority_satisfied),
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

    async def _start_quota_sdk(
        self,
        *,
        recovery_id: str | None,
        session_key: str | None,
    ) -> CompletedSdkRecovery:
        """Complete the zero-interruption quota protocol through one keyless SDK run."""

        quota_recovery_id = recovery_id or str(uuid4())
        root_trace_id = new_qa_trace_id()
        execution_claim: DurableExecution | None = None

        def persist_result(result: QuotaRecoveryResult) -> None:
            if execution_claim is None:
                raise RuntimeError("Quota result cannot persist before its durable dispatch claim")
            self._store.record_completed_quota_execution(
                execution_claim,
                result=result,
            )

        context = QuotaAgentContext(
            recovery_id=quota_recovery_id,
            provider=self._quota_provider,
            persist_result=persist_result,
        )
        agent = build_quota_agent(context=context)
        definition_digest = quota_definition_digest(agent)
        self._store.create_recovery(
            recovery_id=quota_recovery_id,
            scenario_id=ScenarioId.API_QUOTA,
            execution_mode=ExecutionMode.SDK_STUB,
            current_step=0,
            current_step_summary=QUOTA_SDK_INITIAL_SUMMARY,
            model_ids=[],
            root_trace_id=root_trace_id,
            model_call=False,
            sdk_version=self._version_policy.sdk_version,
            protocol_version=QUOTA_PROTOCOL_VERSION,
            agent_graph_version=QUOTA_AGENT_GRAPH_VERSION,
            definition_digest=definition_digest,
            session_key=session_key,
            quota_execution_claim=True,
        )
        execution_claim = self._store.get_quota_execution(quota_recovery_id)
        if execution_claim is None or execution_claim.status != "pending":
            raise RuntimeError("Quota recovery did not persist its dispatch claim")
        try:
            sdk_result = await Runner.run(
                agent,
                QUOTA_START_PROMPT,
                context=context,
                run_config=configure_sdk_stub_tracing(
                    "Backchannel deterministic API quota recovery"
                ),
            )
            interruptions = sdk_result.interruptions
        except BaseException:
            durable_execution = self._store.get_quota_execution(quota_recovery_id)
            if (
                durable_execution is not None
                and durable_execution.status == QUOTA_EXECUTION_RESULT_RECORDED
            ):
                self._store.quarantine_quota_execution_invariant_failure(durable_execution)
            elif durable_execution is not None and durable_execution.status == "completed":
                self._store.finalize_completed_quota_execution(durable_execution)
            raise
        durable_execution = self._store.get_quota_execution(quota_recovery_id)
        if type(interruptions) is not list or interruptions:
            if durable_execution is not None:
                self._store.quarantine_quota_execution_invariant_failure(durable_execution)
            raise RuntimeError("Quota SDK run must return an exact empty interruption list")
        if durable_execution is None:
            raise RuntimeError("Quota SDK result lost its durable execution")
        if durable_execution.status == QUOTA_EXECUTION_RESULT_RECORDED:
            durable_execution = self._store.validate_quota_sdk_completion(durable_execution)
        elif durable_execution.status != "completed":
            self._store.quarantine_quota_execution_invariant_failure(durable_execution)
            raise RuntimeError("Quota SDK result did not persist a validated completion")
        self._store.finalize_completed_quota_execution(durable_execution)
        return CompletedSdkRecovery(
            recovery=self._store.get_recovery(quota_recovery_id),
            sdk_result=sdk_result,
        )

    def _complete_committed_claim(
        self,
        claim: ApprovalDecisionClaim,
        execution: DurableExecution,
    ) -> ApprovalDecisionResponse:
        response = self._decision_response(claim)
        return self._store.finalize_completed_execution_claim(
            execution,
            receipt=self._receipt_for_execution(execution),
            claim=claim,
            response=response,
        )

    async def approve_decision(
        self,
        recovery_id: str,
        request: ApprovalDecisionRequest,
    ) -> ApprovalDecisionResponse:
        """Claim, resume, and durably replay one exact approve or decline decision."""

        claim = self._store.claim_approval_decision(recovery_id, request)
        return await self._continue_decision_claim(claim)

    def preflight_decision_resume(self, recovery_id: str) -> ApprovalDecisionClaim:
        """Load and fully validate the stored claim before any live capacity wait."""

        claim = self._store.load_decision_claim_for_resume(recovery_id)
        if claim.response is None and claim.request.action is DecisionAction.APPROVE:
            execution = self._store.get_completed_execution(recovery_id)
            if execution is not None:
                self._complete_committed_claim(claim, execution)
                return self._store.load_decision_claim_for_resume(recovery_id)
        if claim.response is None:
            self._validate_decision_resume_compatibility(claim)
        return claim

    async def resume_decision(
        self,
        claim: ApprovalDecisionClaim,
    ) -> ApprovalDecisionResponse:
        """Continue only the already-durable claim supplied by server preflight."""

        durable_claim = self._store.load_decision_claim_for_resume(claim.recovery_id)
        if (
            durable_claim.request_fingerprint != claim.request_fingerprint
            or durable_claim.request != claim.request
        ):
            raise ApprovalDecisionError(
                "resume_incompatible",
                claim.recovery_id,
                status_code=409,
            )
        return await self._continue_decision_claim(durable_claim)

    async def _continue_decision_claim(
        self,
        claim: ApprovalDecisionClaim,
    ) -> ApprovalDecisionResponse:
        """Shared continuation pipeline for an original or resumed exact claim."""

        recovery_id = claim.recovery_id
        if claim.response is not None:
            return claim.response
        if claim.request.action is DecisionAction.DECLINE:
            try:
                await self._resume_claimed_approval(claim)
                return self._store.complete_decline_decision(claim)
            except (
                ApprovalDecisionError,
                ReceiptTransitionError,
                ResumeIncompatibleError,
            ):
                self._raise_if_claim_expired(recovery_id)
                raise
        execution = self._store.get_completed_execution(recovery_id)
        if execution is not None:
            return self._complete_committed_claim(claim, execution)
        try:
            completed = await self._resume_claimed_approval(claim)
        except (ApprovalDecisionError, ResumeIncompatibleError):
            execution = self._store.get_completed_execution(recovery_id)
            if execution is None:
                self._raise_if_claim_expired(recovery_id)
                raise
            return self._complete_committed_claim(claim, execution)
        if completed is None:
            replayed = self._store.load_decision_claim_for_resume(recovery_id)
            if replayed.response is not None:
                return replayed.response
        execution = self._store.get_completed_execution(recovery_id)
        if execution is None:
            self._raise_if_claim_expired(recovery_id)
            raise RuntimeError("Approved SDK run completed without a durable execution")
        return self._complete_committed_claim(claim, execution)

    def _raise_if_claim_expired(self, recovery_id: str) -> None:
        """Map only the exact atomic claim-expiry seal to its stable owner error."""

        if self._store.recovery_has_expiration_evidence(recovery_id):
            raise ApprovalDecisionError(
                "remedy_expired",
                recovery_id,
                status_code=422,
            )

    def _validate_decision_resume_compatibility(
        self,
        claim: ApprovalDecisionClaim,
    ) -> ValidatedDecisionResume:
        """Recompute every static resume marker without running a model or provider."""

        recovery_id = claim.recovery_id
        try:
            recovery = self._store.get_recovery(recovery_id)
            envelope = self._store.get_pending_approval(recovery_id)
            consent = self._store.get_remedy_consent(recovery_id)
        except (RecoveryNotFoundError, ValueError, TypeError):
            self._raise_incompatible(recovery_id, "envelope")
        fresh_context = HotelAgentContext(
            recovery_id=recovery_id,
            store=self._store,
            hotel_provider=self._hotel_provider,
            approved_remedy_digest=(
                claim.request.remedy_digest
                if claim.request.action is DecisionAction.APPROVE
                else None
            ),
        )
        if recovery.execution_mode is ExecutionMode.SDK_STUB:
            arguments = deterministic_hotel_arguments()
            fresh_agent = build_hotel_agent(
                context=fresh_context,
                arguments=arguments,
            )
            expected_agent_graph_version = self._version_policy.agent_graph_version
            expected_definition_digest = hotel_definition_digest(fresh_agent)
            expected_model_ids: tuple[str, ...] = ()
            expected_tool_call_id = f"commit-remedy-{recovery_id}"
            valid_root_trace = is_valid_qa_trace_id(envelope.root_trace_id)
        elif recovery.execution_mode is ExecutionMode.OPENAI_LIVE:
            arguments = consent.evidence
            live_agents = build_live_hotel_agents(context=fresh_context)
            fresh_agent = live_agents.broker
            expected_agent_graph_version = self._version_policy.live_agent_graph_version
            expected_definition_digest = live_hotel_definition_digest(
                consumer_agent=live_agents.consumer,
                provider_agent=live_agents.provider,
                broker_agent=live_agents.broker,
            )
            expected_model_ids = (LIVE_CONSUMER_MODEL, LIVE_BROKER_MODEL)
            expected_tool_call_id = envelope.tool_call_id
            valid_root_trace = is_valid_live_trace_id(envelope.root_trace_id)
        else:
            self._raise_incompatible(recovery_id, "execution_mode")
        expected_action_digest = remedy_action_digest(arguments)

        expected_markers = {
            "sdk_version": self._version_policy.sdk_version,
            "protocol_version": self._version_policy.protocol_version,
            "agent_graph_version": expected_agent_graph_version,
            "definition_digest": expected_definition_digest,
            "execution_mode": recovery.execution_mode,
            "model_ids": expected_model_ids,
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
            "model_ids": envelope.model_ids,
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
        if envelope.status not in {"pending", "approved"}:
            self._raise_incompatible(recovery_id, "approval_status")
        if recovery.model_ids != list(envelope.model_ids):
            self._raise_incompatible(recovery_id, "recovery_model_ids")
        if recovery.root_trace_id != envelope.root_trace_id:
            self._raise_incompatible(recovery_id, "recovery_root_trace_id")
        if not valid_root_trace:
            self._raise_incompatible(recovery_id, "root_trace_id")
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
        policy_result = evaluate_hotel_policy(
            consent.evidence,
            DETERMINISTIC_HOTEL_AUTHORITY,
        )
        if policy_result.hard_constraint_satisfied != consent.hard_constraint_satisfied:
            self._raise_incompatible(recovery_id, "hard_constraint_result")
        if not policy_result.hard_constraint_satisfied:
            self._raise_incompatible(recovery_id, "hard_constraint_denied")
        if policy_result.delegated_authority_satisfied != consent.delegated_authority_satisfied:
            self._raise_incompatible(recovery_id, "authority_result")
        if not policy_result.delegated_authority_satisfied:
            self._raise_incompatible(recovery_id, "authority_denied")

        return ValidatedDecisionResume(
            recovery=recovery,
            envelope=envelope,
            consent=consent,
            context=fresh_context,
            agent=fresh_agent,
        )

    async def _resume_claimed_approval(
        self,
        claim: ApprovalDecisionClaim,
    ) -> RunResult | None:
        """Restore the exact interruption only after a durable decision claim."""

        validated = self._validate_decision_resume_compatibility(claim)
        recovery_id = claim.recovery_id
        recovery = validated.recovery
        envelope = validated.envelope
        consent = validated.consent
        fresh_context = validated.context
        fresh_agent = validated.agent
        if recovery.execution_mode is ExecutionMode.SDK_STUB:
            run_config = configure_sdk_stub_tracing()
        elif recovery.execution_mode is ExecutionMode.OPENAI_LIVE:
            run_config = configure_live_tracing(
                recovery_id=recovery_id,
                root_trace_id=envelope.root_trace_id,
                model_provider=self._model_provider,
            )
        else:
            self._raise_incompatible(recovery_id, "execution_mode")

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
                "providerCommitments": sorted(restored_arguments.remedy.provider_commitments),
                "expiry": consent.expiry,
            }
        )
        if restored_consent_digest != envelope.consent_digest:
            self._raise_incompatible(recovery_id, "restored_consent_digest")

        validated_claim = self._store.validate_claimed_decision(claim)
        if validated_claim.response is not None:
            return None
        if claim.request.action is DecisionAction.APPROVE:
            state.approve(interruption)
            self._store.update_pending_approval_status(
                recovery_id,
                expected_status=envelope.status,
                status="approved",
            )
        else:
            state.reject(
                interruption,
                rejection_message=EXACT_REMEDY_REJECTION_MESSAGE,
            )
        completed = await Runner.run(
            fresh_agent,
            state,
            run_config=run_config,
        )
        if claim.request.action is DecisionAction.DECLINE:
            if (
                completed.interruptions
                or self._store.get_completed_execution(recovery_id) is not None
            ):
                # Never claim cancellation when the resumed run produced another
                # authorization boundary or durable provider evidence.
                self._store.update_pending_approval_status(
                    recovery_id,
                    expected_status=envelope.status,
                    status="outcome_unknown",
                )
        return completed
