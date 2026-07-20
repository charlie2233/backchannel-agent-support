import asyncio
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from server.config import RuntimeSettings
from server.main import create_app
from server.models import (
    OPENAI_LIVE_BOUNDARY,
    QUOTA_SDK_AUTHORIZATION_SOURCE,
    QUOTA_SDK_PROVIDER_RESULT,
    QUOTA_SDK_STUB_BOUNDARY,
    QUOTA_SDK_VERIFICATION_RESULTS,
    SDK_STUB_BOUNDARY,
    ExecutionMode,
    RecoveryReceipt,
    RecoveryStatus,
    ScenarioId,
)
from server.orchestrator import CompletedSdkRecovery, RecoveryOrchestrator
from server.providers.hotel_simulator import HotelSimulator
from server.providers.quota_simulator import (
    DETERMINISTIC_QUOTA_REQUEST,
    QuotaSimulator,
    QuotaVerificationError,
)
from server.replay.engine import ReplayEngine
from server.replay.loader import ScenarioLoader
from server.store import ReceiptTransitionError, SQLiteStore

EXPECTED_QUOTA_EVENTS = [
    "recovery.created",
    "quota.pressure_detected",
    "quota.ceiling_proven",
    "quota.burst_selected",
    "quota.delegated_authority_confirmed",
    "quota.burst_executed",
    "quota.receipt_sealed",
]


def test_quota_provider_proves_executes_and_revokes_idempotently() -> None:
    provider = QuotaSimulator()

    first = provider.recover(
        DETERMINISTIC_QUOTA_REQUEST,
        idempotency_key="quota-recovery-demo",
    )
    replayed = provider.recover(
        DETERMINISTIC_QUOTA_REQUEST,
        idempotency_key="quota-recovery-demo",
    )
    fresh = QuotaSimulator().recover(
        DETERMINISTIC_QUOTA_REQUEST,
        idempotency_key="quota-recovery-demo",
    )

    assert replayed == first
    assert fresh == first
    assert provider.execution_count == 1
    assert provider.active_permission_ids == ()
    assert first.ceiling_proof.baseline_ceiling_units == 1_000
    assert first.ceiling_proof.required_units == 1_200
    assert first.ceiling_proof.shortfall_units == 200
    assert first.grant.permission_id == "quota-burst-demo-us-east-1"
    assert first.grant.region == "us-east-1"
    assert first.grant.burst_units == 250
    assert first.grant.effective_ceiling_units == 1_250
    assert first.grant.duration_seconds == 900
    assert first.grant.extra_cost_minor == 300
    assert first.authority.maximum_extra_cost_minor == 500
    assert first.hard_constraints_satisfied is True
    assert first.delegated_authority_satisfied is True
    assert first.approval_count == 0
    assert first.execution_verified is True
    assert first.permission_revoked is True
    assert first.restored_ceiling_units == 1_000


def test_quota_provider_revokes_permission_when_verification_fails() -> None:
    provider = QuotaSimulator(fail_verification=True)

    with pytest.raises(QuotaVerificationError):
        provider.recover(
            DETERMINISTIC_QUOTA_REQUEST,
            idempotency_key="quota-verification-failure",
        )

    assert provider.active_permission_ids == ()
    assert provider.execution_count == 1


def test_quota_sdk_stub_completes_without_interruptions_and_seals_receipt(
    tmp_path,
) -> None:
    database_path = tmp_path / "quota-sdk.sqlite3"
    store = SQLiteStore(database_path)
    quota_provider = QuotaSimulator()
    orchestrator = RecoveryOrchestrator(
        store=store,
        hotel_provider=HotelSimulator(),
        quota_provider=quota_provider,
    )

    completed = asyncio.run(
        orchestrator.start(
            ScenarioId.API_QUOTA,
            execution_mode=ExecutionMode.SDK_STUB,
        )
    )

    assert isinstance(completed, CompletedSdkRecovery)
    assert completed.sdk_result.interruptions == []
    snapshot = completed.recovery
    assert snapshot.scenario_id is ScenarioId.API_QUOTA
    assert snapshot.execution_mode is ExecutionMode.SDK_STUB
    assert snapshot.status is RecoveryStatus.COMPLETED
    assert snapshot.current_step == 5
    assert snapshot.pending_approval is None
    assert snapshot.model_ids == []
    assert snapshot.root_trace_id is not None
    assert snapshot.root_trace_id.startswith("qa_trace_")
    assert quota_provider.execution_count == 1
    assert quota_provider.active_permission_ids == ()

    events = store.list_events(snapshot.recovery_id)
    assert [event.type for event in events] == EXPECTED_QUOTA_EVENTS
    assert [event.seq for event in events] == list(range(1, 8))
    assert sum(event.terminal for event in events) == 1
    assert events[-1].data["approvalCount"] == 0
    assert events[-1].data["permissionRevoked"] is True
    assert events[-1].data["executionVerified"] is True

    receipt = store.get_receipt(snapshot.recovery_id)
    assert receipt.execution_mode is ExecutionMode.SDK_STUB
    assert receipt.status == "completed"
    assert receipt.simulated is True
    assert receipt.provider_execution is True
    assert receipt.model_call is False
    assert receipt.model_ids == []
    assert receipt.root_trace_id == snapshot.root_trace_id
    assert receipt.boundary == QUOTA_SDK_STUB_BOUNDARY
    assert receipt.provider_result == QUOTA_SDK_PROVIDER_RESULT
    assert receipt.authorization_source == QUOTA_SDK_AUTHORIZATION_SOURCE
    assert receipt.verification_results == list(QUOTA_SDK_VERIFICATION_RESULTS)
    assert receipt.approval_count == 0
    assert receipt.approved_remedy_digest is None

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM pending_approvals").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM approval_decisions").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM executions").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM receipts").fetchone() == (1,)


def test_quota_replay_records_policy_without_provider_or_model_execution(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "quota-replay.sqlite3")
    snapshot = ReplayEngine(store, ScenarioLoader()).start(
        ScenarioId.API_QUOTA,
        execution_mode=ExecutionMode.REPLAY_FIXTURE,
    )

    assert snapshot.status is RecoveryStatus.COMPLETED
    assert [event.type for event in store.list_events(snapshot.recovery_id)] == (
        EXPECTED_QUOTA_EVENTS
    )
    events = store.list_events(snapshot.recovery_id)
    assert events[1].data == {
        "baselineCeilingUnits": 1000,
        "phase": "Detect",
        "providerExecution": False,
        "region": "us-east-1",
        "requiredUnits": 1200,
        "shortfallUnits": 200,
        "summary": "Recorded quota pressure replayed.",
    }
    assert events[3].data["permissionId"] == "quota-burst-demo-us-east-1"
    assert events[3].data["burstUnits"] == 250
    assert events[3].data["effectiveCeilingUnits"] == 1250
    assert events[3].data["durationSeconds"] == 900
    assert events[3].data["extraCostMinor"] == 300
    assert events[4].data["maximumExtraCostMinor"] == 500
    assert events[-1].data["restoredCeilingUnits"] == 1000
    receipt = store.get_receipt(snapshot.recovery_id)
    assert receipt.status == "simulated_completed"
    assert receipt.approval_count == 0
    assert receipt.provider_execution is False
    assert receipt.model_call is False
    assert receipt.model_ids == []
    assert receipt.root_trace_id is None
    assert "Recorded" in receipt.authorization_source
    assert "no model call or provider execution" in receipt.boundary.lower()
    assert receipt.verification_results == [
        "Recorded provider proof: baseline 1000 units, demand 1200 units, shortfall 200 units.",
        "Recorded temporary burst: 250 units in us-east-1 for 900 seconds.",
        "Recorded effective ceiling: 1250 units.",
        "Recorded cost: 300 USD minor units within the delegated 500-unit limit.",
        "Recorded approval count: zero; no human interruption.",
        (
            "Recorded permission quota-burst-demo-us-east-1 revoked and baseline "
            "restored to 1000 units."
        ),
    ]


class _LiveHotelOnlyOrchestrator:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store
        self.calls: list[tuple[ScenarioId, ExecutionMode]] = []

    async def start(
        self,
        scenario_id: str | ScenarioId,
        *,
        execution_mode: ExecutionMode,
        recovery_id: str | None = None,
        session_key: str | None = None,
    ) -> SimpleNamespace:
        scenario = ScenarioId(scenario_id)
        self.calls.append((scenario, execution_mode))
        assert scenario is ScenarioId.HOTEL
        assert execution_mode is ExecutionMode.OPENAI_LIVE
        assert recovery_id is not None
        recovery = self.store.create_recovery(
            recovery_id=recovery_id,
            scenario_id=scenario,
            execution_mode=execution_mode,
            current_step=0,
            current_step_summary="Fake live hotel recovery admitted.",
            model_ids=["gpt-5.6-luna", "gpt-5.6-terra"],
            root_trace_id="trace_0123456789abcdef0123456789abcdef",
            model_call=True,
            sdk_version="test-sdk",
            protocol_version="test-protocol",
            agent_graph_version="test-live-hotel",
            definition_digest="a" * 64,
            session_key=session_key,
        )
        return SimpleNamespace(recovery=recovery)


def test_quota_live_is_rejected_before_capacity_cooldown_or_budget_admission(
    tmp_path,
) -> None:
    database_path = tmp_path / "quota-mode-matrix.sqlite3"
    store = SQLiteStore(database_path)
    orchestrator = _LiveHotelOnlyOrchestrator(store)
    settings = RuntimeSettings(
        live_ready=True,
        max_concurrent_live_recoveries=1,
        daily_demo_budget_units=1,
        live_ip_cooldown_seconds=3_600,
        live_session_cooldown_seconds=3_600,
    )
    app = create_app(settings, store=store, orchestrator=orchestrator)

    with TestClient(app) as client:
        rejected = client.post(
            "/api/recoveries",
            json={"scenarioId": "api-quota", "executionMode": "openai_live"},
        )
        assert rejected.status_code == 422
        assert rejected.json()["detail"] == {"code": "unsupported_scenario_mode"}
        with sqlite3.connect(database_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM live_admissions").fetchone() == (0,)
            assert connection.execute("SELECT COUNT(*) FROM usage_ledger").fetchone() == (0,)

        admitted = client.post(
            "/api/recoveries",
            json={"scenarioId": "hotel", "executionMode": "openai_live"},
        )

    assert admitted.status_code == 201
    assert orchestrator.calls == [(ScenarioId.HOTEL, ExecutionMode.OPENAI_LIVE)]
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM live_admissions").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM usage_ledger").fetchone() == (1,)


def test_legacy_receipts_infer_approval_count_without_changing_provenance() -> None:
    replay = RecoveryReceipt(
        recoveryId="legacy-replay",
        executionMode="replay_fixture",
        status="simulated_completed",
        simulated=True,
        providerExecution=False,
        modelIds=[],
        boundary="Bundled replay only.",
        providerResult="Recorded fixture.",
        authorizationSource="Recorded fixture.",
        verificationResults=["Recorded fixture verified."],
    )
    assert replay.approval_count == 0

    sdk_completed = RecoveryReceipt(
        recoveryId="legacy-sdk",
        executionMode="sdk_stub",
        status="completed",
        simulated=True,
        providerExecution=True,
        modelCall=False,
        modelIds=[],
        rootTraceId="qa_trace_0123456789abcdef0123456789abcdef",
        sdkVersion="legacy-sdk",
        protocolVersion="legacy-protocol",
        agentGraphVersion="legacy-hotel",
        definitionDigest="a" * 64,
        boundary=SDK_STUB_BOUNDARY,
        providerResult="Legacy hotel result.",
        authorizationSource="Legacy approval.",
        verificationResults=["Legacy hotel result verified."],
        approvedRemedyDigest=f"sha256:{'b' * 64}",
    )
    assert sdk_completed.approval_count == 1

    live_completed = RecoveryReceipt(
        recoveryId="legacy-live",
        executionMode="openai_live",
        status="completed",
        simulated=True,
        providerExecution=True,
        modelCall=True,
        modelIds=["gpt-5.6-luna", "gpt-5.6-terra"],
        rootTraceId="trace_0123456789abcdef0123456789abcdef",
        sdkVersion="legacy-sdk",
        protocolVersion="legacy-protocol",
        agentGraphVersion="legacy-live-hotel",
        definitionDigest="c" * 64,
        boundary=OPENAI_LIVE_BOUNDARY,
        providerResult="Legacy live demo result.",
        authorizationSource="Legacy approval.",
        verificationResults=["Legacy live result verified."],
        approvedRemedyDigest=f"sha256:{'d' * 64}",
    )
    assert live_completed.approval_count == 1

    declined = RecoveryReceipt(
        recoveryId="legacy-declined",
        executionMode="sdk_stub",
        status="closed_without_action",
        simulated=True,
        providerExecution=False,
        modelCall=False,
        modelIds=[],
        rootTraceId="qa_trace_fedcba9876543210fedcba9876543210",
        sdkVersion="legacy-sdk",
        protocolVersion="legacy-protocol",
        agentGraphVersion="legacy-hotel",
        definitionDigest="e" * 64,
        boundary=SDK_STUB_BOUNDARY,
        providerResult="Provider dispatch did not begin.",
        authorizationSource="Legacy decline.",
        verificationResults=["Legacy cancellation sealed."],
    )
    assert declined.approval_count == 0

    unknown = RecoveryReceipt(
        recoveryId="legacy-unknown",
        executionMode="sdk_stub",
        status="outcome_unknown",
        simulated=True,
        providerExecution=None,
        modelCall=False,
        modelIds=[],
        rootTraceId="qa_trace_fedcba9876543210fedcba9876543210",
        sdkVersion="legacy-sdk",
        protocolVersion="legacy-protocol",
        agentGraphVersion="legacy-hotel",
        definitionDigest="f" * 64,
        boundary=SDK_STUB_BOUNDARY,
        providerResult="Provider outcome unknown.",
        authorizationSource="Legacy uncertain decline.",
        verificationResults=["Legacy uncertain receipt sealed."],
    )
    assert unknown.approval_count == 0


def test_store_rejects_delegated_quota_receipt_for_hotel_recovery(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "quota-receipt-cross-check.sqlite3")
    recovery = store.create_recovery(
        recovery_id="hotel-with-quota-receipt",
        scenario_id=ScenarioId.HOTEL,
        execution_mode=ExecutionMode.SDK_STUB,
        current_step=0,
        current_step_summary="Hotel SDK recovery.",
        root_trace_id="qa_trace_0123456789abcdef0123456789abcdef",
        sdk_version="test-sdk",
        protocol_version="test-protocol",
        agent_graph_version="test-graph",
        definition_digest="a" * 64,
    )
    quota_receipt = RecoveryReceipt(
        recoveryId=recovery.recovery_id,
        executionMode="sdk_stub",
        status="completed",
        simulated=True,
        providerExecution=True,
        modelCall=False,
        modelIds=[],
        rootTraceId=recovery.root_trace_id,
        sdkVersion="test-sdk",
        protocolVersion="test-protocol",
        agentGraphVersion="test-graph",
        definitionDigest="a" * 64,
        boundary=QUOTA_SDK_STUB_BOUNDARY,
        providerResult=QUOTA_SDK_PROVIDER_RESULT,
        authorizationSource=QUOTA_SDK_AUTHORIZATION_SOURCE,
        verificationResults=list(QUOTA_SDK_VERIFICATION_RESULTS),
        approvalCount=0,
    )

    with pytest.raises(ReceiptTransitionError, match="scenario"):
        store.record_transition(
            recovery.recovery_id,
            status=RecoveryStatus.COMPLETED,
            current_step=5,
            current_step_summary="Invalid cross-domain terminal state.",
            event_type="quota.receipt_sealed",
            event_data={"approvalCount": 0},
            receipt=quota_receipt,
        )

    assert store.get_recovery(recovery.recovery_id).status is RecoveryStatus.IN_PROGRESS
    assert store.list_events(recovery.recovery_id)[-1].type == "recovery.created"


@pytest.mark.parametrize(
    ("model_field", "api_field", "tampered_value"),
    [
        ("provider_result", "providerResult", "Tampered provider result."),
        (
            "authorization_source",
            "authorizationSource",
            "Tampered authorization source.",
        ),
        (
            "verification_results",
            "verificationResults",
            [*QUOTA_SDK_VERIFICATION_RESULTS[:-1], "Tampered revocation proof."],
        ),
    ],
)
def test_store_rejects_same_scenario_quota_receipt_evidence_tampering(
    tmp_path,
    model_field: str,
    api_field: str,
    tampered_value: object,
) -> None:
    database_path = tmp_path / "quota-receipt-tampering.sqlite3"
    store = SQLiteStore(database_path)
    recovery = store.create_recovery(
        recovery_id="quota-with-tampered-receipt",
        scenario_id=ScenarioId.API_QUOTA,
        execution_mode=ExecutionMode.SDK_STUB,
        current_step=0,
        current_step_summary="Quota SDK recovery.",
        root_trace_id="qa_trace_0123456789abcdef0123456789abcdef",
        sdk_version="test-sdk",
        protocol_version="test-protocol",
        agent_graph_version="test-graph",
        definition_digest="a" * 64,
    )
    canonical_receipt = RecoveryReceipt(
        recoveryId=recovery.recovery_id,
        executionMode="sdk_stub",
        status="completed",
        simulated=True,
        providerExecution=True,
        modelCall=False,
        modelIds=[],
        rootTraceId=recovery.root_trace_id,
        sdkVersion="test-sdk",
        protocolVersion="test-protocol",
        agentGraphVersion="test-graph",
        definitionDigest="a" * 64,
        boundary=QUOTA_SDK_STUB_BOUNDARY,
        providerResult=QUOTA_SDK_PROVIDER_RESULT,
        authorizationSource=QUOTA_SDK_AUTHORIZATION_SOURCE,
        verificationResults=list(QUOTA_SDK_VERIFICATION_RESULTS),
        approvalCount=0,
    )
    tampered_payload = canonical_receipt.model_dump(by_alias=True)
    tampered_payload[api_field] = tampered_value

    with pytest.raises(ValidationError, match="canonical provider"):
        RecoveryReceipt.model_validate(tampered_payload)

    # Pydantic explicitly does not revalidate model_copy updates. The store must
    # still protect durable evidence if an internal caller passes such an object.
    tampered_receipt = canonical_receipt.model_copy(
        update={model_field: tampered_value},
    )
    with pytest.raises(ReceiptTransitionError, match="canonical facts"):
        store.record_transition(
            recovery.recovery_id,
            status=RecoveryStatus.COMPLETED,
            current_step=5,
            current_step_summary="Tampered quota terminal state.",
            event_type="quota.receipt_sealed",
            event_data={"approvalCount": 0},
            receipt=tampered_receipt,
        )

    assert store.get_recovery(recovery.recovery_id).status is RecoveryStatus.IN_PROGRESS
    assert store.list_events(recovery.recovery_id)[-1].type == "recovery.created"
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM receipts").fetchone() == (0,)


def test_api_routes_quota_sdk_stub_to_completed_receipt(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "quota-api-sdk.sqlite3")
    with TestClient(create_app(RuntimeSettings(live_ready=False), store=store)) as client:
        created = client.post(
            "/api/recoveries",
            json={"scenarioId": "api-quota", "executionMode": "sdk_stub"},
        )
        assert created.status_code == 201
        snapshot = created.json()
        receipt_response = client.get(f"/api/recoveries/{snapshot['recoveryId']}/receipt")

    assert snapshot["scenarioId"] == "api-quota"
    assert snapshot["executionMode"] == "sdk_stub"
    assert snapshot["status"] == "completed"
    assert snapshot["pendingApproval"] is None
    assert receipt_response.status_code == 200
    receipt = receipt_response.json()
    assert receipt["approvalCount"] == 0
    assert receipt["providerExecution"] is True
    assert receipt["modelCall"] is False
    assert receipt["modelIds"] == []
    assert receipt["boundary"] == QUOTA_SDK_STUB_BOUNDARY
