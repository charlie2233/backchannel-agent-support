"""Typed evidence and remedy inputs emitted by the deterministic SDK model."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StubSchema(BaseModel):
    """Immutable base for deterministic model and provider contracts."""

    model_config = ConfigDict(frozen=True)


class ConsumerProof(StubSchema):
    booking_id: str
    requested_room_type: str
    booking_dates: list[str] = Field(min_length=2, max_length=2)
    paid_total_minor: int = Field(ge=0)
    currency: Literal["USD"]


class ProviderProof(StubSchema):
    booking_id: str
    confirmed_room_type: str
    available_room_types: list[str] = Field(min_length=1)
    conflict_code: Literal["room_assignment_mismatch"]


class BrokerRemedy(StubSchema):
    remedy_id: str
    action: Literal["replace_room"]
    room_type: str
    cost_delta_minor: int
    currency: Literal["USD"]
    changed_fields: list[str] = Field(min_length=1)
    provider_commitments: list[str] = Field(min_length=1)


class CommitRemedyArguments(StubSchema):
    consumer_proof: ConsumerProof
    provider_proof: ProviderProof
    remedy: BrokerRemedy


def deterministic_hotel_arguments() -> CommitRemedyArguments:
    """Return the fixed, typed hotel evidence used by the keyless SDK model."""

    return CommitRemedyArguments(
        consumer_proof=ConsumerProof(
            booking_id="booking-demo-001",
            requested_room_type="king",
            booking_dates=["2026-08-14", "2026-08-16"],
            paid_total_minor=42000,
            currency="USD",
        ),
        provider_proof=ProviderProof(
            booking_id="booking-demo-001",
            confirmed_room_type="double",
            available_room_types=["king"],
            conflict_code="room_assignment_mismatch",
        ),
        remedy=BrokerRemedy(
            remedy_id="remedy-king-room",
            action="replace_room",
            room_type="king",
            cost_delta_minor=0,
            currency="USD",
            changed_fields=["room_type"],
            provider_commitments=[
                "Preserve booking dates",
                "No additional charge",
            ],
        ),
    )
