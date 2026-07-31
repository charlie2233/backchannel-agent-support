"""Canonical durable evidence contract for the demo hotel adapter."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from pydantic import JsonValue

from server.agents.schemas import BrokerRemedy

DEMO_HOTEL_PROVIDER_RESULT = (
    "Demo hotel adapter confirmed the replacement room; "
    "no real booking or payment was changed."
)


@dataclass(frozen=True, slots=True)
class DurableHotelDispatchContract:
    request_digest: str
    idempotency_key: str
    execution_id: str
    dispatch_id: str
    result_json: dict[str, JsonValue]


def hotel_dispatch_request_digest(
    *,
    recovery_id: str,
    remedy: BrokerRemedy,
) -> str:
    """Hash the exact request object sent to the deterministic adapter."""

    canonical_request = json.dumps(
        {
            "recovery_id": recovery_id,
            "remedy": remedy.model_dump(mode="json"),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical_request).hexdigest()


def durable_hotel_dispatch_contract(
    *,
    recovery_id: str,
    remedy: BrokerRemedy,
    tool_call_id: str,
    remedy_digest: str,
) -> DurableHotelDispatchContract:
    """Derive every durable identity and result field from exact consent."""

    idempotency_key = f"{recovery_id}:{tool_call_id}:{remedy_digest}"
    dispatch_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    dispatch_id = f"demo-hotel-dispatch-{dispatch_hash[:16]}"
    return DurableHotelDispatchContract(
        request_digest=hotel_dispatch_request_digest(
            recovery_id=recovery_id,
            remedy=remedy,
        ),
        idempotency_key=idempotency_key,
        execution_id=f"execution-{dispatch_hash}",
        dispatch_id=dispatch_id,
        result_json={
            "dispatch_id": dispatch_id,
            "status": "confirmed",
            "simulated": True,
            "provider_result": DEMO_HOTEL_PROVIDER_RESULT,
        },
    )
