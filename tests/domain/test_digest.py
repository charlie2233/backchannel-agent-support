from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from server.digest import canonical_remedy_json, remedy_consent_digest


def consent_payload() -> dict[str, Any]:
    return {
        "recoveryId": "recovery-é-東京",
        "remedyId": "remedy-king-room",
        "terms": "Replace double with king — no extra charge 🏨",
        "costDeltaMinor": 0,
        "changedFields": ["room_type", "guest_note"],
        "providerCommitments": [
            "Preserve booking dates",
            "No additional charge",
        ],
        "expiry": datetime(2026, 8, 14, 19, 30, 45, 123456, tzinfo=UTC),
    }


def test_canonical_json_is_exact_compact_sorted_utf8_and_digest_is_prefixed() -> None:
    payload = consent_payload()

    canonical = canonical_remedy_json(payload)
    parsed = json.loads(canonical)

    assert canonical == (
        '{"changedFields":["guest_note","room_type"],"costDeltaMinor":0,'
        '"expiry":"2026-08-14T19:30:45.123456Z",'
        '"providerCommitments":["No additional charge","Preserve booking dates"],'
        '"recoveryId":"recovery-é-東京","remedyId":"remedy-king-room",'
        '"terms":"Replace double with king — no extra charge 🏨"}'
    ).encode()
    assert parsed["recoveryId"] == "recovery-é-東京"
    expected = f"sha256:{hashlib.sha256(canonical).hexdigest()}"
    assert remedy_consent_digest(payload) == expected
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", expected)


def test_reordered_object_keys_and_set_like_arrays_have_the_same_digest() -> None:
    original = consent_payload()
    reordered = dict(reversed(list(original.items())))
    reordered["changedFields"] = list(reversed(original["changedFields"]))
    reordered["providerCommitments"] = list(
        reversed(original["providerCommitments"])
    )

    assert remedy_consent_digest(reordered) == remedy_consent_digest(original)
    assert original["changedFields"] == ["room_type", "guest_note"]
    assert original["providerCommitments"] == [
        "Preserve booking dates",
        "No additional charge",
    ]


def test_structured_terms_use_canonical_nested_object_key_order() -> None:
    original = {
        **consent_payload(),
        "terms": {
            "stay": {"checkOut": "2026-08-16", "checkIn": "2026-08-14"},
            "replacement": {"to": "king", "from": "double"},
        },
    }
    reordered = {
        **original,
        "terms": {
            "replacement": {"from": "double", "to": "king"},
            "stay": {"checkIn": "2026-08-14", "checkOut": "2026-08-16"},
        },
    }

    assert remedy_consent_digest(reordered) == remedy_consent_digest(original)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("recoveryId", "another-recovery"),
        ("remedyId", "another-remedy"),
        ("terms", "Mutated terms"),
        ("costDeltaMinor", 1),
        ("changedFields", ["room_type"]),
        ("providerCommitments", ["Preserve booking dates"]),
        ("expiry", datetime(2026, 8, 14, 19, 30, 46, 123456, tzinfo=UTC)),
    ],
)
def test_mutating_any_bound_field_changes_the_digest(
    field: str, replacement: object
) -> None:
    original = consent_payload()
    mutated = {**original, field: replacement}

    assert remedy_consent_digest(mutated) != remedy_consent_digest(original)


@pytest.mark.parametrize("invalid_cost", [True, False, 0.0, 1.25])
def test_cost_delta_minor_rejects_bool_and_float(invalid_cost: object) -> None:
    payload = {**consent_payload(), "costDeltaMinor": invalid_cost}

    with pytest.raises(ValueError, match="costDeltaMinor"):
        remedy_consent_digest(payload)


@pytest.mark.parametrize(
    "invalid_expiry",
    [
        datetime(2026, 8, 14, 19, 30, 45),
        datetime(
            2026,
            8,
            14,
            20,
            30,
            45,
            tzinfo=timezone(timedelta(hours=1)),
        ),
    ],
)
def test_expiry_rejects_naive_and_non_utc_datetimes(invalid_expiry: datetime) -> None:
    payload = {**consent_payload(), "expiry": invalid_expiry}

    with pytest.raises(ValueError, match="expiry"):
        remedy_consent_digest(payload)


@pytest.mark.parametrize(
    "invalid_payload",
    [
        {key: value for key, value in consent_payload().items() if key != "terms"},
        {**consent_payload(), "unexpected": "field"},
    ],
)
def test_exact_consent_object_rejects_missing_or_extra_fields(
    invalid_payload: dict[str, Any],
) -> None:
    with pytest.raises(ValueError, match="exactly"):
        remedy_consent_digest(invalid_payload)
