from __future__ import annotations

import pytest
from pydantic import ValidationError

from server.agents.schemas import BrokerRemedy, deterministic_hotel_arguments
from server.policy import (
    DETERMINISTIC_HOTEL_AUTHORITY,
    evaluate_hotel_policy,
    exact_hotel_terms,
)


def test_deterministic_hotel_evidence_satisfies_constraints_and_authority() -> None:
    arguments = deterministic_hotel_arguments()

    result = evaluate_hotel_policy(arguments, DETERMINISTIC_HOTEL_AUTHORITY)

    assert result.hard_constraint_satisfied is True
    assert result.delegated_authority_satisfied is True
    assert exact_hotel_terms(arguments).model_dump(mode="json", by_alias=True) == {
        "bookingId": "booking-demo-001",
        "action": "replace_room",
        "replacement": {
            "fromRoomType": "double",
            "toRoomType": "king",
        },
        "stay": {
            "checkIn": "2026-08-14",
            "checkOut": "2026-08-16",
        },
        "currency": "USD",
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "booking_mismatch",
        "replacement_mismatch",
        "replacement_unavailable",
        "dates_not_preserved",
        "changed_fields_regression",
    ],
)
def test_hotel_hard_constraints_fail_closed_on_authoritative_regression(
    mutation: str,
) -> None:
    arguments = deterministic_hotel_arguments()
    if mutation == "booking_mismatch":
        arguments = arguments.model_copy(
            update={
                "provider_proof": arguments.provider_proof.model_copy(
                    update={"booking_id": "booking-other"}
                )
            }
        )
    elif mutation == "replacement_mismatch":
        arguments = arguments.model_copy(
            update={
                "remedy": arguments.remedy.model_copy(update={"room_type": "suite"})
            }
        )
    elif mutation == "replacement_unavailable":
        arguments = arguments.model_copy(
            update={
                "provider_proof": arguments.provider_proof.model_copy(
                    update={"available_room_types": ["double"]}
                )
            }
        )
    elif mutation == "dates_not_preserved":
        arguments = arguments.model_copy(
            update={
                "remedy": arguments.remedy.model_copy(
                    update={"provider_commitments": ["No additional charge"]}
                )
            }
        )
    else:
        arguments = arguments.model_copy(
            update={
                "remedy": arguments.remedy.model_copy(
                    update={"changed_fields": ["room_type", "booking_dates"]}
                )
            }
        )

    result = evaluate_hotel_policy(arguments, DETERMINISTIC_HOTEL_AUTHORITY)

    assert result.hard_constraint_satisfied is False


def test_authority_overflow_fails_even_when_hard_constraints_still_match() -> None:
    arguments = deterministic_hotel_arguments()
    arguments = arguments.model_copy(
        update={
            "remedy": arguments.remedy.model_copy(update={"cost_delta_minor": 1})
        }
    )

    result = evaluate_hotel_policy(arguments, DETERMINISTIC_HOTEL_AUTHORITY)

    assert result.hard_constraint_satisfied is True
    assert result.delegated_authority_satisfied is False


@pytest.mark.parametrize("invalid_cost", [True, False, 0.0, 1.5])
def test_authoritative_remedy_schema_rejects_non_strict_minor_units(
    invalid_cost: object,
) -> None:
    payload = deterministic_hotel_arguments().remedy.model_dump(mode="json")
    payload["cost_delta_minor"] = invalid_cost

    with pytest.raises(ValidationError):
        BrokerRemedy.model_validate(payload)
