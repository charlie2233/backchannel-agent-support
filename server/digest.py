"""Canonical, exact consent binding for one proposed remedy."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

_CONSENT_FIELDS = frozenset(
    {
        "recoveryId",
        "remedyId",
        "terms",
        "costDeltaMinor",
        "changedFields",
        "providerCommitments",
        "expiry",
    }
)


def _require_string(payload: Mapping[str, object], field: str) -> str:
    value = payload[field]
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    return value


def _require_string_array(payload: Mapping[str, object], field: str) -> list[str]:
    value = payload[field]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be an array of strings")
    return sorted(value)


def _canonical_expiry(payload: Mapping[str, object]) -> str:
    value = payload["expiry"]
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("expiry must be a timezone-aware UTC datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError("expiry must use UTC")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def canonical_remedy_json(payload: Mapping[str, object]) -> bytes:
    """Return the exact consent object as compact, sorted, UTF-8 JSON.

    ``changedFields`` and ``providerCommitments`` are the only set-like fields;
    their input ordering is intentionally normalized without mutating the caller.
    """

    actual_fields = frozenset(payload)
    if actual_fields != _CONSENT_FIELDS:
        raise ValueError("Consent payload must contain exactly the canonical fields")

    cost_delta_minor = payload["costDeltaMinor"]
    if type(cost_delta_minor) is not int:
        raise ValueError("costDeltaMinor must be an integer minor-unit value")

    canonical: dict[str, object] = {
        "recoveryId": _require_string(payload, "recoveryId"),
        "remedyId": _require_string(payload, "remedyId"),
        "terms": payload["terms"],
        "costDeltaMinor": cost_delta_minor,
        "changedFields": _require_string_array(payload, "changedFields"),
        "providerCommitments": _require_string_array(
            payload, "providerCommitments"
        ),
        "expiry": _canonical_expiry(payload),
    }
    try:
        serialized = json.dumps(
            canonical,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("terms must contain only finite JSON values") from error
    return serialized.encode("utf-8")


def remedy_consent_digest(payload: Mapping[str, object]) -> str:
    """Return the lowercase SHA-256 binding for an exact canonical remedy."""

    digest = hashlib.sha256(canonical_remedy_json(payload)).hexdigest()
    return f"sha256:{digest}"
