from __future__ import annotations

import json

from fastapi.testclient import TestClient

from server.config import RuntimeSettings
from server.main import create_app
from server.providers.hotel_simulator import HotelSimulator
from server.store import SQLiteStore


def _create(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/api/recoveries",
        json={"scenarioId": "hotel", "executionMode": "sdk_stub"},
    )
    assert response.status_code == 201
    return response.json()


def _payload(snapshot: dict[str, object], *, decision_id: str = "api-decline"):
    approval = snapshot["pendingApproval"]
    assert isinstance(approval, dict)
    return {
        "decision": "decline",
        "clientDecisionId": decision_id,
        "remedyId": approval["remedyId"],
        "remedyDigest": approval["remedyDigest"],
        "toolCallId": approval["toolCallId"],
    }


def test_public_decline_closes_without_action_and_leaks_no_internal_state(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "api-decline.sqlite3")
    provider = HotelSimulator(store=store)
    with TestClient(
        create_app(
            RuntimeSettings(live_ready=False),
            store=store,
            hotel_provider=provider,
        )
    ) as client:
        snapshot = _create(client)
        recovery_id = str(snapshot["recoveryId"])
        payload = _payload(snapshot)

        response = client.post(
            f"/api/recoveries/{recovery_id}/decisions",
            json=payload,
        )

        assert response.status_code == 200
        assert response.json() == {
            "clientDecisionId": "api-decline",
            "recoveryId": recovery_id,
            "decision": "decline",
            "status": "closed_without_action",
            "decisionRemedyDigest": payload["remedyDigest"],
            "executionStarted": False,
        }
        receipt_response = client.get(f"/api/recoveries/{recovery_id}/receipt")
        assert receipt_response.status_code == 200
        receipt = receipt_response.json()
        assert receipt["decision"] == "declined"
        assert receipt["providerExecution"] is False
        assert receipt["executionCount"] == 0
        assert receipt["providerDispatchStarted"] is False
        assert receipt["exactInterruptionRejected"] is True
        assert receipt["permissionRevoked"] is True
        assert receipt["scopeClosed"] is True
        public_json = json.dumps(
            {"response": response.json(), "receipt": receipt},
            sort_keys=True,
        ).lower()
        for forbidden in (
            "state_json",
            "statejson",
            "consumer_proof",
            "provider_proof",
            "rejection_message",
            '"prompt"',
            "evidence_json",
        ):
            assert forbidden not in public_json
        assert provider.dispatch_count == 0
        assert store.count_executions(recovery_id) == 0


def test_decision_discriminator_is_required_and_identity_tampering_writes_nothing(
    tmp_path,
) -> None:
    store = SQLiteStore(tmp_path / "api-decline-tamper.sqlite3")
    provider = HotelSimulator(store=store)
    with TestClient(
        create_app(
            RuntimeSettings(live_ready=False),
            store=store,
            hotel_provider=provider,
        )
    ) as client:
        snapshot = _create(client)
        foreign = _create(client)
        recovery_id = str(snapshot["recoveryId"])
        payload = _payload(snapshot)
        foreign_approval = foreign["pendingApproval"]
        assert isinstance(foreign_approval, dict)
        missing = client.post(
            f"/api/recoveries/{recovery_id}/decisions",
            json={key: value for key, value in payload.items() if key != "decision"},
        )
        assert missing.status_code == 422
        cases = [
            ("remedyId", "foreign-remedy", "remedy_mismatch"),
            ("remedyDigest", f"sha256:{'0' * 64}", "remedy_digest_mismatch"),
            ("toolCallId", "foreign-tool-call", "tool_call_mismatch"),
            (
                "remedyDigest",
                foreign_approval["remedyDigest"],
                "remedy_digest_mismatch",
            ),
        ]
        for index, (field, value, expected_code) in enumerate(cases):
            tampered = client.post(
                f"/api/recoveries/{recovery_id}/decisions",
                json={
                    **payload,
                    "clientDecisionId": f"tampered-decline-{index}",
                    field: value,
                },
            )
            assert tampered.status_code == 422
            assert tampered.json()["error"]["code"] == expected_code
        assert store.count_decisions(recovery_id) == 0
        assert store.count_executions(recovery_id) == 0
        assert provider.dispatch_count == 0


def test_same_client_id_opposite_action_is_a_conflict(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "api-opposite-action.sqlite3")
    provider = HotelSimulator(store=store)
    with TestClient(
        create_app(
            RuntimeSettings(live_ready=False),
            store=store,
            hotel_provider=provider,
        )
    ) as client:
        snapshot = _create(client)
        recovery_id = str(snapshot["recoveryId"])
        decline = _payload(snapshot, decision_id="same-action-id")
        first = client.post(
            f"/api/recoveries/{recovery_id}/decisions",
            json=decline,
        )
        opposite = client.post(
            f"/api/recoveries/{recovery_id}/decisions",
            json={**decline, "decision": "approve"},
        )

        assert first.status_code == 200
        assert opposite.status_code == 409
        assert opposite.json()["error"]["code"] == "decision_id_conflict"
        assert store.count_decisions(recovery_id) == 1
        assert store.count_executions(recovery_id) == 0
