"""Pure hotel constraint and delegated-authority evaluation."""

from __future__ import annotations

from dataclasses import dataclass

from server.agents.schemas import CommitRemedyArguments
from server.models import (
    HotelRemedyTerms,
    HotelReplacementTerms,
    HotelStayTerms,
)


@dataclass(frozen=True, slots=True)
class HotelDelegatedAuthority:
    booking_id: str
    allowed_changed_fields: frozenset[str]
    minimum_cost_delta_minor: int
    maximum_cost_delta_minor: int


@dataclass(frozen=True, slots=True)
class HotelPolicyResult:
    hard_constraint_satisfied: bool
    delegated_authority_satisfied: bool


DETERMINISTIC_HOTEL_AUTHORITY = HotelDelegatedAuthority(
    booking_id="booking-demo-001",
    allowed_changed_fields=frozenset({"room_type"}),
    minimum_cost_delta_minor=0,
    maximum_cost_delta_minor=0,
)


def exact_hotel_terms(arguments: CommitRemedyArguments) -> HotelRemedyTerms:
    """Derive displayable exact terms only from the authoritative typed evidence."""

    return HotelRemedyTerms(
        bookingId=arguments.consumer_proof.booking_id,
        action=arguments.remedy.action,
        replacement=HotelReplacementTerms(
            fromRoomType=arguments.provider_proof.confirmed_room_type,
            toRoomType=arguments.remedy.room_type,
        ),
        stay=HotelStayTerms(
            checkIn=arguments.consumer_proof.booking_dates[0],
            checkOut=arguments.consumer_proof.booking_dates[1],
        ),
        currency=arguments.remedy.currency,
    )


def evaluate_hotel_policy(
    arguments: CommitRemedyArguments,
    authority: HotelDelegatedAuthority,
) -> HotelPolicyResult:
    """Fail closed against current evidence without I/O or mutable state."""

    consumer = arguments.consumer_proof
    provider = arguments.provider_proof
    remedy = arguments.remedy
    changed_fields = remedy.changed_fields
    hard_constraint_satisfied = all(
        (
            consumer.booking_id == provider.booking_id,
            remedy.action == "replace_room",
            remedy.room_type == consumer.requested_room_type,
            remedy.room_type in provider.available_room_types,
            provider.confirmed_room_type != consumer.requested_room_type,
            "Preserve booking dates" in remedy.provider_commitments,
            changed_fields == ["room_type"],
            type(remedy.cost_delta_minor) is int,
        )
    )
    delegated_authority_satisfied = all(
        (
            consumer.booking_id == authority.booking_id,
            len(changed_fields) == len(set(changed_fields)),
            set(changed_fields) <= authority.allowed_changed_fields,
            type(remedy.cost_delta_minor) is int,
            authority.minimum_cost_delta_minor
            <= remedy.cost_delta_minor
            <= authority.maximum_cost_delta_minor,
        )
    )
    return HotelPolicyResult(
        hard_constraint_satisfied=hard_constraint_satisfied,
        delegated_authority_satisfied=delegated_authority_satisfied,
    )
