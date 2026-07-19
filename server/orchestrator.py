"""Coordinate deterministic recoveries through the actual Agents SDK runner."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn
from uuid import uuid4

from agents import Agent, RunContextWrapper, Runner, RunResult, RunState
from agents.exceptions import UserError

from server.agents.factory import HotelAgentContext, build_hotel_agent
from server.agents.schemas import CommitRemedyArguments, deterministic_hotel_arguments
from server.agents.stub_model import CLOSED_WITHOUT_ACTION_MESSAGE, DECLINE_MESSAGE
from server.agents.tracing import configure_sdk_stub_tracing
from server.agents.versioning import (
    HOTEL_START_PROMPT,
    ApprovalVersionPolicy,
    hotel_definition_digest,
    is_valid_qa_trace_id,
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
    ApprovalDecisionClaim,
    ApprovalDecisionError,
    DurableExecution,
    PendingApprovalEnvelope,
    RecoveryNotFoundError,
    RemedyConsentRecord,
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
        self._reconcile_claimed_decisions()

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
                "Temporary permission revoked after terminal completion.",
            ],
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

    def _reconcile_claimed_decisions(self) -> None:
        """Complete claimed responses whose durable dispatch already committed."""

        for claim in self._store.list_claimed_decisions():
            if claim.request.decision == "decline":
                try:
                    pending = self._store.get_pending_approval(claim.recovery_id)
                except (RecoveryNotFoundError, ValueError):
                    continue
                if pending.status == "rejected":
                    self._store.finalize_declined_decision(
                        claim,
                        exact_interruption_rejected=True,
                    )
                continue
            if claim.request.decision != "approve":
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
        action_digest = remedy_action_digest(arguments)
        context = HotelAgentContext(
            recovery_id=recovery_id,
            store=self._store,
            hotel_provider=self._hotel_provider,
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
        """Claim, resume, and durably replay one exact approval decision."""

        if request.decision != "approve":
            raise ValueError("approve_decision requires decision=approve")
        claim = self._store.claim_decision(recovery_id, request)
        if claim.response is not None:
            if not isinstance(claim.response, ApprovalDecisionResponse):
                raise TypeError("Approval request replayed a decline response")
            return claim.response
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
        except UserError as sdk_error:
            replayed = self._store.claim_decision(recovery_id, request)
            if replayed.response is not None:
                if not isinstance(replayed.response, ApprovalDecisionResponse):
                    raise TypeError("Approval request replayed a decline response")
                return replayed.response
            try:
                self._store.get_receipt(recovery_id)
            except RecoveryNotFoundError:
                raise sdk_error
            execution = self._store.get_completed_execution(recovery_id)
            if execution is None:
                raise
            return self._complete_committed_claim(claim, execution)
        if completed is None:
            replayed = self._store.claim_decision(recovery_id, request)
            if replayed.response is not None:
                if not isinstance(replayed.response, ApprovalDecisionResponse):
                    raise TypeError("Approval request replayed a decline response")
                return replayed.response
        execution = self._store.get_completed_execution(recovery_id)
        if execution is None:
            raise RuntimeError("Approved SDK run completed without a durable execution")
        return self._complete_committed_claim(claim, execution)

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
        pending = self._store.get_pending_approval(recovery_id)
        if pending.status == "rejected":
            return self._store.finalize_declined_decision(
                claim,
                exact_interruption_rejected=True,
            )
        await self._resume_claimed_decline(claim)
        return self._store.finalize_declined_decision(
            claim,
            exact_interruption_rejected=True,
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
    ) -> RunResult | None:
        """Restore and approve only after a durable exact-consent claim."""

        return await self._resume_claimed_decision(claim, approve=True)

    async def _resume_claimed_decline(
        self,
        claim: ApprovalDecisionClaim,
    ) -> RunResult | None:
        """Restore and reject only the exact interruption bound to the claim."""

        return await self._resume_claimed_decision(claim, approve=False)

    async def _resume_claimed_decision(
        self,
        claim: ApprovalDecisionClaim,
        *,
        approve: bool,
    ) -> RunResult | None:
        """Restore and consent-bind one exact SDK approval interruption."""

        recovery_id = claim.recovery_id
        expected_decision = "approve" if approve else "decline"
        if claim.request.decision != expected_decision:
            raise ValueError("Decision claim action does not match resume path")
        arguments = deterministic_hotel_arguments()
        expected_action_digest = remedy_action_digest(arguments)
        fresh_context = HotelAgentContext(
            recovery_id=recovery_id,
            store=self._store,
            hotel_provider=self._hotel_provider,
            approved_remedy_digest=claim.request.remedy_digest,
        )
        fresh_agent = build_hotel_agent(context=fresh_context, arguments=arguments)
        try:
            recovery = self._store.get_recovery(recovery_id)
            envelope = self._store.get_pending_approval(recovery_id)
            consent = self._store.get_remedy_consent(recovery_id)
        except (RecoveryNotFoundError, ValueError, TypeError):
            self._raise_incompatible(recovery_id, "envelope")

        expected_markers = {
            "sdk_version": self._version_policy.sdk_version,
            "protocol_version": self._version_policy.protocol_version,
            "agent_graph_version": self._version_policy.agent_graph_version,
            "definition_digest": hotel_definition_digest(fresh_agent),
            "execution_mode": ExecutionMode.SDK_STUB,
            "tool_call_id": f"commit-remedy-{recovery_id}",
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
        if not is_valid_qa_trace_id(envelope.root_trace_id):
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

        validated_claim = self._store.validate_claimed_decision(claim)
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
        completed = await Runner.run(
            fresh_agent,
            state,
            run_config=configure_sdk_stub_tracing(),
        )
        if not approve:
            if self._store.count_executions(recovery_id) != execution_count_before:
                raise RuntimeError("SDK rejection unexpectedly changed execution evidence")
            if completed.final_output != CLOSED_WITHOUT_ACTION_MESSAGE:
                raise RuntimeError("Deterministic rejection did not close without action")
            self._store.record_exact_interruption_rejected(claim)
        return completed
