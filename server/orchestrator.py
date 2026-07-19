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
    QUOTA_SDK_AUTHORIZATION_SOURCE,
    QUOTA_SDK_PROVIDER_RESULT,
    QUOTA_SDK_STUB_BOUNDARY,
    QUOTA_SDK_VERIFICATION_RESULTS,
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
    ApprovalDecisionClaim,
    ApprovalDecisionError,
    DurableExecution,
    PendingApprovalEnvelope,
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
        self._reconcile_completed_executions()
        self._reconcile_claimed_decisions()

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

    def _reconcile_claimed_decisions(self) -> None:
        """Complete claimed responses whose durable dispatch already committed."""

        for claim in self._store.list_claimed_decisions():
            if claim.request.action is not DecisionAction.APPROVE:
                continue
            execution = self._store.get_completed_execution(claim.recovery_id)
            if execution is None:
                continue
            self._store.finalize_completed_execution(
                execution,
                receipt=self._receipt_for_execution(execution),
            )
            self._store.complete_approval_decision(
                claim,
                self._decision_response(claim),
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
            return await self._start_quota_sdk(recovery_id=recovery_id)
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
            self._store.create_recovery(
                recovery_id=recovery_id,
                scenario_id=approved_scenario,
                execution_mode=execution_mode,
                current_step=0,
                current_step_summary="Deterministic Agents SDK recovery started.",
                model_ids=model_ids,
                root_trace_id=root_trace_id,
                model_call=False,
                sdk_version=self._version_policy.sdk_version,
                protocol_version=self._version_policy.protocol_version,
                agent_graph_version=agent_graph_version,
                definition_digest=definition_digest,
            )
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
            self._store.create_recovery(
                recovery_id=recovery_id,
                scenario_id=approved_scenario,
                execution_mode=execution_mode,
                current_step=0,
                current_step_summary="OpenAI Agents SDK recovery started.",
                model_ids=model_ids,
                root_trace_id=root_trace_id,
                model_call=True,
                sdk_version=self._version_policy.sdk_version,
                protocol_version=self._version_policy.protocol_version,
                agent_graph_version=agent_graph_version,
                definition_digest=definition_digest,
            )

        action_digest = remedy_action_digest(arguments)
        if len(result.interruptions) != 1:
            raise RuntimeError("SDK run did not produce exactly one interruption")
        interruption = result.interruptions[0]
        if interruption.tool_name != "commit_remedy":
            raise RuntimeError("SDK run interrupted on an unexpected tool")
        if not interruption.call_id:
            raise RuntimeError("SDK interruption is missing its call ID")

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
    ) -> CompletedSdkRecovery:
        """Complete the zero-interruption quota protocol through one keyless SDK run."""

        quota_recovery_id = recovery_id or str(uuid4())
        root_trace_id = new_qa_trace_id()
        context = QuotaAgentContext(
            recovery_id=quota_recovery_id,
            provider=self._quota_provider,
        )
        agent = build_quota_agent(context=context)
        definition_digest = quota_definition_digest(agent)
        self._store.create_recovery(
            recovery_id=quota_recovery_id,
            scenario_id=ScenarioId.API_QUOTA,
            execution_mode=ExecutionMode.SDK_STUB,
            current_step=0,
            current_step_summary="Deterministic API quota SDK recovery started.",
            model_ids=[],
            root_trace_id=root_trace_id,
            model_call=False,
            sdk_version=self._version_policy.sdk_version,
            protocol_version=QUOTA_PROTOCOL_VERSION,
            agent_graph_version=QUOTA_AGENT_GRAPH_VERSION,
            definition_digest=definition_digest,
        )
        sdk_result = await Runner.run(
            agent,
            QUOTA_START_PROMPT,
            context=context,
            run_config=configure_sdk_stub_tracing(
                "Backchannel deterministic API quota recovery"
            ),
        )
        if sdk_result.interruptions:
            raise RuntimeError("Quota SDK run must not create a human interruption")
        result = self._quota_provider.result_for(context.idempotency_key)
        self._record_quota_trace(
            recovery_id=quota_recovery_id,
            root_trace_id=root_trace_id,
            definition_digest=definition_digest,
            result=result,
        )
        return CompletedSdkRecovery(
            recovery=self._store.get_recovery(quota_recovery_id),
            sdk_result=sdk_result,
        )

    def _record_quota_trace(
        self,
        *,
        recovery_id: str,
        root_trace_id: str,
        definition_digest: str,
        result: QuotaRecoveryResult,
    ) -> None:
        """Persist the completed, already-revoked quota history and terminal receipt."""

        proof = result.ceiling_proof
        grant = result.grant
        authority = result.authority
        transitions: list[tuple[str, int, str, dict[str, Any]]] = [
            (
                "quota.pressure_detected",
                0,
                "Quota demand exceeds the provider-proven baseline ceiling.",
                {
                    "phase": "Detect",
                    "region": grant.region,
                    "baselineCeilingUnits": proof.baseline_ceiling_units,
                    "requiredUnits": proof.required_units,
                    "shortfallUnits": proof.shortfall_units,
                },
            ),
            (
                "quota.ceiling_proven",
                1,
                "Provider evidence proves the exact quota ceiling and shortfall.",
                {
                    "phase": "Prove",
                    "providerEvidenceId": proof.provider_evidence_id,
                    "baselineCeilingUnits": proof.baseline_ceiling_units,
                    "requiredUnits": proof.required_units,
                    "shortfallUnits": proof.shortfall_units,
                },
            ),
            (
                "quota.burst_selected",
                2,
                "A temporary US-region burst covers the proven shortfall.",
                {
                    "phase": "Negotiate",
                    "permissionId": grant.permission_id,
                    "region": grant.region,
                    "burstUnits": grant.burst_units,
                    "effectiveCeilingUnits": grant.effective_ceiling_units,
                    "durationSeconds": grant.duration_seconds,
                    "extraCostMinor": grant.extra_cost_minor,
                    "currency": grant.currency,
                },
            ),
            (
                "quota.delegated_authority_confirmed",
                3,
                "Delegated policy authorizes the exact burst with zero human approvals.",
                {
                    "phase": "Authorize",
                    "approvalCount": result.approval_count,
                    "hardConstraintsSatisfied": result.hard_constraints_satisfied,
                    "delegatedAuthoritySatisfied": result.delegated_authority_satisfied,
                    "maximumExtraCostMinor": authority.maximum_extra_cost_minor,
                    "maximumDurationSeconds": authority.maximum_duration_seconds,
                    "allowedRegions": list(authority.allowed_regions),
                },
            ),
            (
                "quota.burst_executed",
                4,
                "The demo adapter executed and verified the temporary burst.",
                {
                    "phase": "Execute",
                    "providerExecution": True,
                    "executionVerified": result.execution_verified,
                    "effectiveCeilingUnits": grant.effective_ceiling_units,
                },
            ),
        ]
        for event_type, step, summary, data in transitions:
            self._store.record_transition(
                recovery_id,
                status=RecoveryStatus.IN_PROGRESS,
                current_step=step,
                current_step_summary=summary,
                event_type=event_type,
                event_data=data,
            )

        receipt = RecoveryReceipt(
            recoveryId=recovery_id,
            executionMode=ExecutionMode.SDK_STUB,
            status="completed",
            simulated=True,
            providerExecution=True,
            modelCall=False,
            modelIds=[],
            rootTraceId=root_trace_id,
            sdkVersion=self._version_policy.sdk_version,
            protocolVersion=QUOTA_PROTOCOL_VERSION,
            agentGraphVersion=QUOTA_AGENT_GRAPH_VERSION,
            definitionDigest=definition_digest,
            boundary=QUOTA_SDK_STUB_BOUNDARY,
            providerResult=QUOTA_SDK_PROVIDER_RESULT,
            authorizationSource=QUOTA_SDK_AUTHORIZATION_SOURCE,
            verificationResults=list(QUOTA_SDK_VERIFICATION_RESULTS),
            approvalCount=0,
        )
        self._store.record_transition(
            recovery_id,
            status=RecoveryStatus.COMPLETED,
            current_step=5,
            current_step_summary=(
                "Execution verified, temporary permission revoked, and receipt sealed."
            ),
            event_type="quota.receipt_sealed",
            event_data={
                "phase": "Verify & seal",
                "approvalCount": result.approval_count,
                "providerExecution": True,
                "executionVerified": result.execution_verified,
                "permissionRevoked": result.permission_revoked,
                "restoredCeilingUnits": result.restored_ceiling_units,
                "summary": "Verified quota recovery evidence sealed.",
            },
            receipt=receipt,
        )

    def _complete_committed_claim(
        self,
        claim: ApprovalDecisionClaim,
        execution: DurableExecution,
    ) -> ApprovalDecisionResponse:
        self._store.finalize_completed_execution(
            execution,
            receipt=self._receipt_for_execution(execution),
        )
        return self._store.complete_approval_decision(
            claim,
            self._decision_response(claim),
        )

    async def approve_decision(
        self,
        recovery_id: str,
        request: ApprovalDecisionRequest,
    ) -> ApprovalDecisionResponse:
        """Claim, resume, and durably replay one exact approve or decline decision."""

        claim = self._store.claim_approval_decision(recovery_id, request)
        if claim.response is not None:
            return claim.response
        if claim.request.action is DecisionAction.DECLINE:
            await self._resume_claimed_approval(claim)
            return self._store.complete_decline_decision(claim)
        execution = self._store.get_completed_execution(recovery_id)
        if execution is not None:
            return self._complete_committed_claim(claim, execution)
        try:
            completed = await self._resume_claimed_approval(claim)
        except ApprovalDecisionError:
            execution = self._store.get_completed_execution(recovery_id)
            if execution is None:
                raise
            return self._complete_committed_claim(claim, execution)
        if completed is None:
            replayed = self._store.claim_approval_decision(recovery_id, request)
            if replayed.response is not None:
                return replayed.response
        execution = self._store.get_completed_execution(recovery_id)
        if execution is None:
            raise RuntimeError("Approved SDK run completed without a durable execution")
        return self._complete_committed_claim(claim, execution)

    async def _resume_claimed_approval(
        self,
        claim: ApprovalDecisionClaim,
    ) -> RunResult | None:
        """Restore the exact interruption only after a durable decision claim."""

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
            run_config = configure_sdk_stub_tracing()
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
            run_config = configure_live_tracing(
                recovery_id=recovery_id,
                root_trace_id=envelope.root_trace_id,
                model_provider=self._model_provider,
            )
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
            self._store.update_pending_approval_status(recovery_id, status="approved")
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
                    status="outcome_unknown",
                )
        return completed
