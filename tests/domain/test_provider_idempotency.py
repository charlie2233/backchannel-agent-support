from concurrent.futures import ThreadPoolExecutor

import pytest

from server.agents.schemas import BrokerRemedy
from server.providers.hotel_simulator import (
    HotelDispatchRequest,
    HotelSimulator,
    IdempotencyConflictError,
)


def make_request(room_type: str = "king") -> HotelDispatchRequest:
    return HotelDispatchRequest(
        recovery_id="recovery-123",
        remedy=BrokerRemedy(
            remedy_id=f"remedy-{room_type}-room",
            action="replace_room",
            room_type=room_type,
            cost_delta_minor=0,
            currency="USD",
            changed_fields=["room_type"],
            provider_commitments=["Preserve booking dates", "No additional charge"],
        ),
    )


def test_hotel_simulator_returns_stored_result_for_an_idempotency_key() -> None:
    provider = HotelSimulator()
    request = make_request()

    first = provider.dispatch(request, idempotency_key="recovery-123:call-1")
    repeated = provider.dispatch(request, idempotency_key="recovery-123:call-1")

    assert repeated is first
    assert repeated.dispatch_id == first.dispatch_id
    assert provider.dispatch_count == 1


def test_hotel_simulator_rejects_conflicting_idempotency_payload() -> None:
    provider = HotelSimulator()
    key = "recovery-123:conflict"
    first = provider.dispatch(make_request("king"), idempotency_key=key)

    with pytest.raises(IdempotencyConflictError):
        provider.dispatch(make_request("suite"), idempotency_key=key)

    assert provider.dispatch_count == 1
    assert provider.dispatch(make_request("king"), idempotency_key=key) is first


def test_concurrent_identical_retries_dispatch_once() -> None:
    provider = HotelSimulator()
    request = make_request()
    key = "recovery-123:concurrent-identical"

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(provider.dispatch, request, idempotency_key=key)
            for _ in range(24)
        ]
    results = [future.result() for future in futures]

    assert provider.dispatch_count == 1
    assert all(result is results[0] for result in results)


def test_concurrent_conflicting_retries_never_redispatch() -> None:
    provider = HotelSimulator()
    key = "recovery-123:concurrent-conflict"
    stored = provider.dispatch(make_request("king"), idempotency_key=key)

    with ThreadPoolExecutor(max_workers=8) as executor:
        conflicting = [
            executor.submit(
                provider.dispatch,
                make_request("suite"),
                idempotency_key=key,
            )
            for _ in range(24)
        ]

    assert provider.dispatch_count == 1
    assert all(
        isinstance(future.exception(), IdempotencyConflictError)
        for future in conflicting
    )
    assert provider.dispatch(make_request("king"), idempotency_key=key) is stored
