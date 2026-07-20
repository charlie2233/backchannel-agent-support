from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from server.models import RecoverySnapshot


def claimed_snapshot_payload() -> dict[str, object]:
    return {
        "recoveryId": "11111111-2222-4333-8444-555555555555",
        "scenarioId": "hotel",
        "executionMode": "sdk_stub",
        "modelIds": [],
        "rootTraceId": "qa_trace_0123456789abcdef0123456789abcdef",
        "status": "pending_approval",
        "currentStep": 3,
        "currentStepSummary": "Exact approval claimed; outcome pending.",
        "createdAt": "2026-07-20T01:00:00Z",
        "updatedAt": "2026-07-20T01:00:01Z",
        "pendingApproval": None,
        "claimedDecision": {
            "action": "approve",
            "remedyDigest": f"sha256:{'a' * 64}",
            "expiry": "2026-07-20T02:00:00Z",
        },
    }


def test_claimed_decision_accepts_only_the_minimal_utc_digest_contract() -> None:
    snapshot = RecoverySnapshot.model_validate(claimed_snapshot_payload())

    assert snapshot.model_dump(mode="json", by_alias=True)["claimedDecision"] == {
        "action": "approve",
        "remedyDigest": f"sha256:{'a' * 64}",
        "expiry": "2026-07-20T02:00:00Z",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("remedyDigest", f"sha256:{'A' * 64}"),
        ("expiry", "2026-07-20T02:00:00-07:00"),
        ("clientDecisionId", "must-never-be-public"),
    ],
)
def test_claimed_decision_rejects_bad_digest_non_utc_and_extra_keys(
    field: str,
    value: str,
) -> None:
    payload = claimed_snapshot_payload()
    claim = dict(payload["claimedDecision"])
    claim[field] = value
    payload["claimedDecision"] = claim

    with pytest.raises(ValidationError):
        RecoverySnapshot.model_validate(payload)


def test_claimed_decision_and_pending_approval_are_mutually_exclusive() -> None:
    payload = claimed_snapshot_payload()
    valid_pending = {
        "remedyId": "remedy-server-742",
        "remedyDigest": f"sha256:{'a' * 64}",
        "terms": {
            "bookingId": "booking-server-742",
            "action": "replace_room",
            "replacement": {
                "fromRoomType": "double",
                "toRoomType": "ocean-view king",
            },
            "stay": {"checkIn": "2026-09-04", "checkOut": "2026-09-07"},
            "currency": "USD",
        },
        "costDeltaMinor": 1250,
        "changedFields": ["guest_note", "room_type"],
        "providerCommitments": ["No additional fees", "Preserve booking dates"],
        "expiry": "2026-09-01T18:45:30Z",
        "hardConstraintSatisfied": True,
        "delegatedAuthoritySatisfied": False,
        "toolCallId": "call-server-742",
        "executionStarted": False,
    }
    pending_only = deepcopy(payload)
    pending_only["claimedDecision"] = None
    pending_only["pendingApproval"] = valid_pending
    RecoverySnapshot.model_validate(pending_only)
    payload["pendingApproval"] = valid_pending

    with pytest.raises(ValidationError):
        RecoverySnapshot.model_validate(payload)


@pytest.mark.parametrize("field", ["status", "scenarioId"])
def test_claimed_decision_requires_pending_hotel_snapshot(field: str) -> None:
    payload = claimed_snapshot_payload()
    payload[field] = "completed" if field == "status" else "api-quota"

    with pytest.raises(ValidationError):
        RecoverySnapshot.model_validate(payload)


def test_claimed_decision_rejects_replay_fixture_provenance() -> None:
    payload = claimed_snapshot_payload()
    payload["executionMode"] = "replay_fixture"
    payload["modelIds"] = []
    payload["rootTraceId"] = None

    with pytest.raises(ValidationError):
        RecoverySnapshot.model_validate(payload)
