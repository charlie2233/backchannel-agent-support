import sqlite3

from agents import Runner
from agents.tool import FunctionTool
from fastapi.testclient import TestClient

from server.config import RuntimeSettings
from server.main import create_app
from server.models import RecoveryReceipt
from server.store import ReceiptTransitionError, SQLiteStore

EXPECTED_STEPS = [
    "Detect",
    "Prove",
    "Negotiate",
    "Authorize",
    "Execute",
    "Verify & seal",
]

EXPECTED_QUOTA_POLICY = {
    "providerCeilingRpm": 1000,
    "recordedDemandRpm": 1200,
    "temporaryBurstRpm": 1500,
    "region": "US",
    "durationSeconds": 900,
    "extraCostMinor": 250,
    "delegatedAuthorityMaxMinor": 500,
    "currency": "USD",
    "hardConstraints": {
        "regionPreserved": True,
        "burstCoversDemand": True,
        "durationWithinLimit": True,
        "baseQuotaUnchanged": True,
    },
    "humanInterruptions": 0,
    "approvals": 0,
    "providerProofVerified": True,
    "grantVerified": True,
    "protocolSteps": EXPECTED_STEPS,
}


def expected_quota_evidence(execution_mode: str) -> dict[str, object]:
    return {
        **EXPECTED_QUOTA_POLICY,
        "source": (
            "sdk_simulator" if execution_mode == "sdk_stub" else "recorded_fixture"
        ),
        "revocationEvidenceKind": (
            "runtime_permission_revoked"
            if execution_mode == "sdk_stub"
            else "recorded_revocation_only"
        ),
    }


def test_api_quota_sdk_stub_runs_one_complete_delegated_trace_atomically(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "quota-sdk.sqlite3"
    store = SQLiteStore(database_path)
    observed_sdk_runs: list[tuple[bool, int, bool, int]] = []
    original_run = Runner.run

    async def observe_quota_sdk_run(agent, *args, **kwargs):
        run_config = kwargs["run_config"]
        assert run_config.tracing_disabled is True
        assert len(agent.tools) == 1
        tool = agent.tools[0]
        assert isinstance(tool, FunctionTool)
        assert tool.needs_approval is False
        result = await original_run(agent, *args, **kwargs)
        observed_sdk_runs.append(
            (
                run_config.tracing_disabled,
                len(agent.tools),
                tool.needs_approval,
                len(result.interruptions),
            )
        )
        return result

    monkeypatch.setattr("server.orchestrator.Runner.run", observe_quota_sdk_run)

    with TestClient(
        create_app(RuntimeSettings(live_ready=False), store=store)
    ) as client:
        created = client.post(
            "/api/recoveries",
            json={"scenarioId": "api-quota", "executionMode": "sdk_stub"},
        )

        assert created.status_code == 201
        snapshot = created.json()
        assert snapshot["scenarioId"] == "api-quota"
        assert snapshot["executionMode"] == "sdk_stub"
        assert snapshot["status"] == "completed"
        assert snapshot["currentStep"] == 5
        assert snapshot["pendingApproval"] is None
        assert snapshot["modelIds"] == []
        recovery_id = snapshot["recoveryId"]

        receipt_response = client.get(f"/api/recoveries/{recovery_id}/receipt")
        assert receipt_response.status_code == 200
        receipt = receipt_response.json()
        assert receipt["executionMode"] == "sdk_stub"
        assert receipt["status"] == "completed"
        assert receipt["simulated"] is True
        assert receipt["providerExecution"] is True
        assert receipt["modelIds"] == []
        assert receipt["rootTraceId"] is None
        assert receipt["sdkVersion"] == "0.18.3"
        assert receipt["protocolVersion"] == "backchannel.quota.v1"
        assert receipt["agentGraphVersion"] == "backchannel.quota-agent.v1"
        assert len(receipt["promptToolSchemaHash"]) == 64
        assert receipt["decision"] is None
        assert receipt["decisionRemedyDigest"] is None
        assert receipt["executionCount"] == 1
        assert receipt["providerDispatchStarted"] is True
        assert receipt["exactInterruptionRejected"] is False
        assert receipt["permissionRevoked"] is True
        assert receipt["scopeClosed"] is True
        assert receipt["approvedRemedyDigest"] is None
        assert receipt["quotaEvidence"] == expected_quota_evidence("sdk_stub")

        events = client.app.state.recovery_store.list_events(recovery_id)
        assert len(events) == 7
        assert [event.data.get("phase") for event in events[1:]] == EXPECTED_STEPS
        assert [event.terminal for event in events] == [
            False,
            False,
            False,
            False,
            False,
            False,
            True,
        ]

    assert observed_sdk_runs == [(True, 1, False, 0)]

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM recoveries").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone() == (7,)
        assert connection.execute("SELECT COUNT(*) FROM executions").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM receipts").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM approval_decisions").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM pending_approvals").fetchone() == (0,)
        assert connection.execute(
            "SELECT status FROM permission_scopes"
        ).fetchone() == ("revoked",)


def test_api_quota_replay_has_identical_recorded_policy_values_without_runtime_claims(
    tmp_path,
) -> None:
    store = SQLiteStore(tmp_path / "quota-replay.sqlite3")
    with TestClient(
        create_app(RuntimeSettings(live_ready=False), store=store)
    ) as client:
        created = client.post(
            "/api/recoveries",
            json={"scenarioId": "api-quota", "executionMode": "replay_fixture"},
        )
        assert created.status_code == 201
        recovery_id = created.json()["recoveryId"]
        receipt = client.get(f"/api/recoveries/{recovery_id}/receipt").json()

    assert receipt["quotaEvidence"] == expected_quota_evidence("replay_fixture")
    assert receipt["providerExecution"] is False
    assert receipt["executionCount"] == 0
    assert receipt["providerDispatchStarted"] is False
    assert receipt["permissionRevoked"] is False
    assert receipt["scopeClosed"] is False
    assert receipt["modelIds"] == []
    assert receipt["rootTraceId"] is None
    assert receipt["sdkVersion"] is None
    assert receipt["protocolVersion"] is None
    assert receipt["agentGraphVersion"] is None
    assert receipt["promptToolSchemaHash"] is None
    assert receipt["decision"] is None


def test_api_quota_openai_live_is_rejected_before_public_live_admission(tmp_path) -> None:
    database_path = tmp_path / "quota-live-rejected.sqlite3"
    store = SQLiteStore(database_path)
    settings = RuntimeSettings(live_ready=False)

    with TestClient(create_app(settings, store=store)) as client:
        response = client.post(
            "/api/recoveries",
            json={"scenarioId": "api-quota", "executionMode": "openai_live"},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM recoveries").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM usage_ledger").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM executions").fetchone() == (0,)


def test_api_quota_terminal_graph_rolls_back_as_one_transaction(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "quota-rollback.sqlite3"
    store = SQLiteStore(database_path)
    original_validate = store._validate_durable_receipt_evidence

    def fail_after_quota_graph_writes(
        connection,
        receipt: RecoveryReceipt,
        *,
        phase,
    ) -> None:
        if receipt.quota_evidence is not None:
            raise ReceiptTransitionError("Injected quota finalization failure")
        original_validate(connection, receipt, phase=phase)

    monkeypatch.setattr(
        store,
        "_validate_durable_receipt_evidence",
        fail_after_quota_graph_writes,
    )

    with TestClient(
        create_app(RuntimeSettings(live_ready=False), store=store),
        raise_server_exceptions=False,
    ) as client:
        response = client.post(
            "/api/recoveries",
            json={"scenarioId": "api-quota", "executionMode": "sdk_stub"},
        )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    with sqlite3.connect(database_path) as connection:
        for table in (
            "recoveries",
            "events",
            "executions",
            "receipts",
            "permission_scopes",
            "receipt_provenance_migrations",
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
