from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

import server.orchestrator as orchestrator_module
from server.agents.schemas import CommitRemedyArguments, deterministic_hotel_arguments
from server.digest import remedy_consent_digest
from server.models import (
    ApprovalDecisionRequest,
    ExecutionMode,
    PendingApprovalView,
    RecoveryStatus,
    ScenarioId,
)
from server.orchestrator import RecoveryOrchestrator
from server.providers.hotel_simulator import HotelSimulator
from server.store import SQLiteStore

PERSISTED_RECOVERY_ARTIFACTS = (
    "recoveries",
    "recovery_access",
    "events",
    "remedies",
    "pending_approvals",
    "approval_decisions",
    "executions",
    "receipts",
)


class SqlTracingStore(SQLiteStore):
    def __init__(self, database_path) -> None:
        self.trace_enabled = False
        self.statements: list[str] = []
        super().__init__(database_path)

    def _connect(self) -> sqlite3.Connection:
        connection = super()._connect()
        if self.trace_enabled:
            connection.set_trace_callback(self.statements.append)
        return connection


def _hard_constraint_failure() -> CommitRemedyArguments:
    arguments = deterministic_hotel_arguments()
    return arguments.model_copy(
        update={
            "provider_proof": arguments.provider_proof.model_copy(
                update={"confirmed_room_type": arguments.consumer_proof.requested_room_type}
            )
        }
    )


def _delegated_authority_failure() -> CommitRemedyArguments:
    arguments = deterministic_hotel_arguments()
    return arguments.model_copy(
        update={
            "remedy": arguments.remedy.model_copy(update={"cost_delta_minor": 1})
        }
    )


def _clone_consent_for_recovery(source, recovery_id: str):
    consent = replace(source, recovery_id=recovery_id)
    digest = remedy_consent_digest(
        {
            "recoveryId": recovery_id,
            "remedyId": consent.remedy_id,
            "terms": consent.terms.model_dump(mode="json", by_alias=True),
            "costDeltaMinor": consent.cost_delta_minor,
            "changedFields": list(consent.changed_fields),
            "providerCommitments": list(consent.provider_commitments),
            "expiry": consent.expiry,
        }
    )
    return replace(consent, consent_digest=digest)


def _tamper_persisted_consent(
    connection: sqlite3.Connection,
    recovery_id: str,
    regression: str,
) -> None:
    if regression == "stored-hard-false":
        connection.execute(
            "UPDATE remedies SET hard_constraint_satisfied = 0 WHERE recovery_id = ?",
            (recovery_id,),
        )
    elif regression == "stored-authority-false":
        connection.execute(
            "UPDATE remedies SET delegated_authority_satisfied = 0 WHERE recovery_id = ?",
            (recovery_id,),
        )
    elif regression in {
        "stored-hard-two",
        "stored-hard-text-false",
        "stored-authority-two",
        "stored-authority-text-false",
    }:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        field = (
            "hard_constraint_satisfied"
            if regression.startswith("stored-hard-")
            else "delegated_authority_satisfied"
        )
        corrupt_value: object = (
            2 if regression.endswith("-two") else "false"
        )
        connection.execute(
            f"UPDATE remedies SET {field} = ? WHERE recovery_id = ?",
            (corrupt_value, recovery_id),
        )
    elif regression == "current-policy-false":
        evidence = json.loads(
            connection.execute(
                "SELECT evidence_json FROM remedies WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone()[0]
        )
        evidence["remedy"]["cost_delta_minor"] = 1
        connection.execute(
            "UPDATE remedies SET evidence_json = ? WHERE recovery_id = ?",
            (json.dumps(evidence, sort_keys=True), recovery_id),
        )
    elif regression == "remedy-id":
        connection.execute(
            "UPDATE remedies SET id = 'tampered-remedy' WHERE recovery_id = ?",
            (recovery_id,),
        )
        connection.execute(
            "UPDATE pending_approvals SET remedy_id = 'tampered-remedy' WHERE recovery_id = ?",
            (recovery_id,),
        )
    elif regression == "terms":
        terms = json.loads(
            connection.execute(
                "SELECT terms_json FROM remedies WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone()[0]
        )
        terms["replacement"]["toRoomType"] = "suite"
        connection.execute(
            "UPDATE remedies SET terms_json = ? WHERE recovery_id = ?",
            (json.dumps(terms, sort_keys=True), recovery_id),
        )
    elif regression == "cost":
        connection.execute(
            "UPDATE remedies SET cost_delta_minor = 1 WHERE recovery_id = ?",
            (recovery_id,),
        )
    elif regression == "changed-fields":
        connection.execute(
            "UPDATE remedies SET changed_fields_json = ? WHERE recovery_id = ?",
            (json.dumps(["booking_dates", "room_type"]), recovery_id),
        )
    elif regression == "provider-commitments":
        connection.execute(
            "UPDATE remedies SET provider_commitments_json = ? WHERE recovery_id = ?",
            (
                json.dumps(["Preserve booking dates", "No additional charge"]),
                recovery_id,
            ),
        )
    elif regression == "expiry":
        connection.execute(
            "UPDATE remedies SET expiry = ? WHERE recovery_id = ?",
            ("2099-01-01T00:00:00+00:00", recovery_id),
        )
    elif regression == "consent-digest":
        digest = "sha256:" + "b" * 64
        connection.execute(
            "UPDATE remedies SET digest = ? WHERE recovery_id = ?",
            (digest, recovery_id),
        )
        connection.execute(
            "UPDATE pending_approvals SET consent_digest = ? WHERE recovery_id = ?",
            (digest, recovery_id),
        )
    elif regression == "action-digest":
        connection.execute(
            "UPDATE pending_approvals SET action_digest = ? WHERE recovery_id = ?",
            ("c" * 64, recovery_id),
        )
    else:
        raise AssertionError(f"Unknown regression: {regression}")


@pytest.mark.parametrize(
    "arguments_factory",
    [_hard_constraint_failure, _delegated_authority_failure],
    ids=["hard-constraint", "delegated-authority"],
)
def test_ineligible_typed_sdk_attempt_fails_before_any_recovery_artifact(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    arguments_factory,
) -> None:
    database_path = tmp_path / "policy-ineligible-sdk.sqlite3"
    store = SQLiteStore(database_path)
    provider = HotelSimulator(store=store)
    recovery_id = "11111111-2222-4333-8444-555555555555"
    monkeypatch.setattr(
        orchestrator_module,
        "deterministic_hotel_arguments",
        arguments_factory,
    )

    with pytest.raises(RuntimeError, match="^policy_ineligible$"):
        asyncio.run(
            RecoveryOrchestrator(store=store, hotel_provider=provider).start(
                "hotel",
                execution_mode=ExecutionMode.SDK_STUB,
                recovery_id=recovery_id,
                session_key="a" * 64,
            )
        )

    assert provider.dispatch_count == 0
    with sqlite3.connect(database_path) as connection:
        for table in PERSISTED_RECOVERY_ARTIFACTS:
            assert connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone() == (0,)


@pytest.mark.parametrize(
    "policy_field",
    ["hard_constraint_satisfied", "delegated_authority_satisfied"],
)
def test_store_refuses_ineligible_consent_before_begin_immediate(
    tmp_path,
    policy_field: str,
) -> None:
    database_path = tmp_path / f"store-{policy_field}.sqlite3"
    store = SqlTracingStore(database_path)
    eligible = asyncio.run(
        RecoveryOrchestrator(
            store=store,
            hotel_provider=HotelSimulator(store=store),
        ).start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    source_envelope = store.get_pending_approval(eligible.recovery.recovery_id)
    source_consent = store.get_remedy_consent(eligible.recovery.recovery_id)
    recovery_id = str(uuid4())
    store.create_recovery(
        recovery_id=recovery_id,
        scenario_id=ScenarioId.HOTEL,
        execution_mode=ExecutionMode.SDK_STUB,
        current_step=0,
        current_step_summary="Policy eligibility test recovery.",
        root_trace_id=source_envelope.root_trace_id,
        sdk_version=source_envelope.sdk_version,
        protocol_version=source_envelope.protocol_version,
        agent_graph_version=source_envelope.agent_graph_version,
        definition_digest=source_envelope.definition_digest,
    )
    valid_consent = _clone_consent_for_recovery(source_consent, recovery_id)
    envelope = replace(
        source_envelope,
        recovery_id=recovery_id,
        tool_call_id=f"tool-{recovery_id}",
        consent_digest=valid_consent.consent_digest,
    )
    consent = replace(valid_consent, **{policy_field: False})
    store.trace_enabled = True

    with pytest.raises(ValueError, match="policy eligible"):
        store.record_transition(
            recovery_id,
            status=RecoveryStatus.PENDING_APPROVAL,
            current_step=3,
            current_step_summary="Must remain unmodified.",
            event_type="approval.requested",
            event_data={"providerExecution": False},
            pending_approval=envelope,
            remedy_consent=consent,
        )

    assert "BEGIN IMMEDIATE" not in store.statements
    store.trace_enabled = False
    snapshot = store.get_recovery(recovery_id)
    assert snapshot.status is RecoveryStatus.IN_PROGRESS
    assert [event.type for event in store.list_events(recovery_id)] == ["recovery.created"]
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM remedies WHERE recovery_id = ?", (recovery_id,)
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM pending_approvals WHERE recovery_id = ?", (recovery_id,)
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    "regression",
    [
        "remedy-id",
        "terms",
        "cost",
        "changed-fields",
        "provider-commitments",
        "expiry",
        "consent-digest",
        "action-digest",
    ],
)
def test_store_refuses_incompletely_bound_consent_before_begin_immediate(
    tmp_path,
    regression: str,
) -> None:
    database_path = tmp_path / f"store-binding-{regression}.sqlite3"
    store = SqlTracingStore(database_path)
    eligible = asyncio.run(
        RecoveryOrchestrator(
            store=store,
            hotel_provider=HotelSimulator(store=store),
        ).start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    source_envelope = store.get_pending_approval(eligible.recovery.recovery_id)
    source_consent = store.get_remedy_consent(eligible.recovery.recovery_id)
    recovery_id = str(uuid4())
    store.create_recovery(
        recovery_id=recovery_id,
        scenario_id=ScenarioId.HOTEL,
        execution_mode=ExecutionMode.SDK_STUB,
        current_step=0,
        current_step_summary="Consent binding test recovery.",
        root_trace_id=source_envelope.root_trace_id,
        sdk_version=source_envelope.sdk_version,
        protocol_version=source_envelope.protocol_version,
        agent_graph_version=source_envelope.agent_graph_version,
        definition_digest=source_envelope.definition_digest,
    )
    consent = _clone_consent_for_recovery(source_consent, recovery_id)
    envelope = replace(
        source_envelope,
        recovery_id=recovery_id,
        tool_call_id=f"tool-{recovery_id}",
        consent_digest=consent.consent_digest,
    )
    if regression == "remedy-id":
        envelope = replace(envelope, remedy_id="tampered-remedy")
        consent = replace(consent, remedy_id="tampered-remedy")
    elif regression == "terms":
        consent = replace(
            consent,
            terms=consent.terms.model_copy(
                update={
                    "replacement": consent.terms.replacement.model_copy(
                        update={"to_room_type": "suite"}
                    )
                }
            ),
        )
    elif regression == "cost":
        consent = replace(consent, cost_delta_minor=1)
    elif regression == "changed-fields":
        consent = replace(consent, changed_fields=("booking_dates", "room_type"))
    elif regression == "provider-commitments":
        consent = replace(
            consent,
            provider_commitments=("Preserve booking dates", "No additional charge"),
        )
    elif regression == "expiry":
        consent = replace(consent, expiry=consent.expiry.replace(year=2099))
    elif regression == "consent-digest":
        digest = "sha256:" + "b" * 64
        consent = replace(consent, consent_digest=digest)
        envelope = replace(envelope, consent_digest=digest)
    elif regression == "action-digest":
        envelope = replace(envelope, action_digest="c" * 64)
    else:
        raise AssertionError(f"Unknown regression: {regression}")
    store.trace_enabled = True

    with pytest.raises(ValueError, match="(?:policy eligible|binding)"):
        store.record_transition(
            recovery_id,
            status=RecoveryStatus.PENDING_APPROVAL,
            current_step=3,
            current_step_summary="Must remain unmodified.",
            event_type="approval.requested",
            event_data={"providerExecution": False},
            pending_approval=envelope,
            remedy_consent=consent,
        )

    assert "BEGIN IMMEDIATE" not in store.statements
    store.trace_enabled = False
    assert store.get_recovery(recovery_id).status is RecoveryStatus.IN_PROGRESS
    assert [event.type for event in store.list_events(recovery_id)] == ["recovery.created"]
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM pending_approvals WHERE recovery_id = ?", (recovery_id,)
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    "regression",
    [
        "stored-hard-false",
        "stored-authority-false",
        "stored-hard-two",
        "stored-hard-text-false",
        "stored-authority-two",
        "stored-authority-text-false",
        "current-policy-false",
        "remedy-id",
        "terms",
        "cost",
        "changed-fields",
        "provider-commitments",
        "expiry",
        "consent-digest",
        "action-digest",
    ],
)
def test_legacy_ineligible_pending_row_has_no_public_actionable_consent(
    tmp_path,
    regression: str,
) -> None:
    database_path = tmp_path / f"legacy-{regression}.sqlite3"
    store = SQLiteStore(database_path)
    provider = HotelSimulator(store=store)
    pending = asyncio.run(
        RecoveryOrchestrator(
            store=store,
            hotel_provider=provider,
        ).start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    recovery_id = pending.recovery.recovery_id
    with sqlite3.connect(database_path) as connection:
        _tamper_persisted_consent(connection, recovery_id, regression)

    public_snapshot = store.get_recovery(recovery_id)

    assert public_snapshot.status is RecoveryStatus.PENDING_APPROVAL
    assert public_snapshot.pending_approval is None
    assert public_snapshot.claimed_decision is None
    assert provider.dispatch_count == 0
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM approval_decisions").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM executions").fetchone() == (0,)


@pytest.mark.parametrize(
    "regression",
    [
        "cost",
        "stored-hard-false",
        "stored-hard-two",
        "stored-hard-text-false",
        "stored-authority-two",
        "stored-authority-text-false",
        "consent-digest",
        "action-digest",
    ],
)
def test_ineligible_or_unbound_claim_has_no_public_resume_view(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    regression: str,
) -> None:
    database_path = tmp_path / f"claimed-binding-{regression}.sqlite3"
    store = SQLiteStore(database_path)
    provider = HotelSimulator(store=store)
    orchestrator = RecoveryOrchestrator(store=store, hotel_provider=provider)
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    approval = pending.recovery.pending_approval
    assert approval is not None
    request = ApprovalDecisionRequest(
        action="approve",
        clientDecisionId=f"claimed-{regression}",
        remedyId=approval.remedy_id,
        remedyDigest=approval.remedy_digest,
        toolCallId=approval.tool_call_id,
    )

    class SimulatedProcessLoss(RuntimeError):
        pass

    async def stop_after_claim(*_args: Any, **_kwargs: Any) -> None:
        raise SimulatedProcessLoss("stop after durable claim")

    monkeypatch.setattr(orchestrator, "_resume_claimed_approval", stop_after_claim)
    with pytest.raises(SimulatedProcessLoss, match="durable claim"):
        asyncio.run(orchestrator.approve_decision(pending.recovery.recovery_id, request))
    claimed = store.get_recovery(pending.recovery.recovery_id)
    assert claimed.pending_approval is None
    assert claimed.claimed_decision is not None

    with sqlite3.connect(database_path) as connection:
        _tamper_persisted_consent(connection, pending.recovery.recovery_id, regression)

    public_snapshot = store.get_recovery(pending.recovery.recovery_id)

    assert public_snapshot.pending_approval is None
    assert public_snapshot.claimed_decision is None
    assert provider.dispatch_count == 0
    assert store.count_executions(pending.recovery.recovery_id) == 0


@pytest.mark.parametrize(
    "pending_status",
    ["expired", "outcome_unknown", "unknown-corrupt-status"],
)
def test_nonresumable_pending_status_has_no_public_claimed_resume_view(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    pending_status: str,
) -> None:
    database_path = tmp_path / f"claimed-status-{pending_status}.sqlite3"
    store = SQLiteStore(database_path)
    provider = HotelSimulator(store=store)
    orchestrator = RecoveryOrchestrator(store=store, hotel_provider=provider)
    pending = asyncio.run(
        orchestrator.start("hotel", execution_mode=ExecutionMode.SDK_STUB)
    )
    approval = pending.recovery.pending_approval
    assert approval is not None
    request = ApprovalDecisionRequest(
        action="approve",
        clientDecisionId=f"claimed-status-{pending_status}",
        remedyId=approval.remedy_id,
        remedyDigest=approval.remedy_digest,
        toolCallId=approval.tool_call_id,
    )

    class SimulatedProcessLoss(RuntimeError):
        pass

    async def stop_after_claim(*_args: Any, **_kwargs: Any) -> None:
        raise SimulatedProcessLoss("stop after durable claim")

    monkeypatch.setattr(orchestrator, "_resume_claimed_approval", stop_after_claim)
    with pytest.raises(SimulatedProcessLoss, match="durable claim"):
        asyncio.run(orchestrator.approve_decision(pending.recovery.recovery_id, request))
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE pending_approvals SET status = ? WHERE recovery_id = ?",
            (pending_status, pending.recovery.recovery_id),
        )

    public_snapshot = store.get_recovery(pending.recovery.recovery_id)

    assert public_snapshot.pending_approval is None
    assert public_snapshot.claimed_decision is None
    assert provider.dispatch_count == 0
    assert store.count_executions(pending.recovery.recovery_id) == 0


@pytest.mark.parametrize("field", ["hardConstraintSatisfied", "delegatedAuthoritySatisfied"])
@pytest.mark.parametrize("invalid_value", [False, 1, "true"])
def test_pending_approval_public_schema_requires_policy_flags_to_be_true(
    field: str,
    invalid_value: object,
) -> None:
    payload: dict[str, Any] = {
        "remedyId": "remedy-policy-test",
        "remedyDigest": "sha256:" + "a" * 64,
        "terms": {
            "bookingId": "booking-policy-test",
            "action": "replace_room",
            "replacement": {"fromRoomType": "double", "toRoomType": "king"},
            "stay": {"checkIn": "2026-08-14", "checkOut": "2026-08-16"},
            "currency": "USD",
        },
        "costDeltaMinor": 0,
        "changedFields": ["room_type"],
        "providerCommitments": ["Preserve booking dates"],
        "expiry": "2026-08-14T00:00:00Z",
        "hardConstraintSatisfied": True,
        "delegatedAuthoritySatisfied": True,
        "toolCallId": "tool-policy-test",
        "executionStarted": False,
    }
    payload[field] = invalid_value

    with pytest.raises(ValidationError):
        PendingApprovalView.model_validate(payload)


@pytest.mark.parametrize("location", ["top-level", "nested-remedy"])
def test_sdk_interruption_rejects_unknown_fields_before_persistence(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    location: str,
) -> None:
    database_path = tmp_path / f"sdk-extra-{location}.sqlite3"
    store = SQLiteStore(database_path)
    provider = HotelSimulator(store=store)
    original_run = orchestrator_module.Runner.run

    async def run_with_extra(*args: Any, **kwargs: Any):
        result = await original_run(*args, **kwargs)
        interruption = result.interruptions[0]
        payload = json.loads(interruption.arguments or "{}")
        if location == "top-level":
            payload["private_unknown"] = "must-be-rejected"
        else:
            payload["remedy"]["private_unknown"] = "must-be-rejected"
        interruption.raw_item.arguments = json.dumps(payload, sort_keys=True)
        return result

    monkeypatch.setattr(orchestrator_module.Runner, "run", run_with_extra)

    with pytest.raises(RuntimeError, match="invalid remedy arguments"):
        asyncio.run(
            RecoveryOrchestrator(store=store, hotel_provider=provider).start(
                "hotel",
                execution_mode=ExecutionMode.SDK_STUB,
                recovery_id="11111111-2222-4333-8444-555555555555",
                session_key="a" * 64,
            )
        )

    assert provider.dispatch_count == 0
    with sqlite3.connect(database_path) as connection:
        for table in PERSISTED_RECOVERY_ARTIFACTS:
            assert connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone() == (0,)
