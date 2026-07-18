from server.agents.schemas import BrokerRemedy
from server.providers.hotel_simulator import HotelDispatchRequest, HotelSimulator


def test_hotel_simulator_returns_stored_result_for_an_idempotency_key() -> None:
    provider = HotelSimulator()
    request = HotelDispatchRequest(
        recovery_id="recovery-123",
        remedy=BrokerRemedy(
            remedy_id="remedy-king-room",
            action="replace_room",
            room_type="king",
            cost_delta_minor=0,
            currency="USD",
            changed_fields=["room_type"],
            provider_commitments=["Preserve booking dates", "No additional charge"],
        ),
    )

    first = provider.dispatch(request, idempotency_key="recovery-123:call-1")
    repeated = provider.dispatch(request, idempotency_key="recovery-123:call-1")

    assert repeated is first
    assert repeated.dispatch_id == first.dispatch_id
    assert provider.dispatch_count == 1
