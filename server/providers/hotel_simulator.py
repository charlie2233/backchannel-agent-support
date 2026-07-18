"""Thread-safe, idempotent demo hotel provider boundary."""

from __future__ import annotations

from threading import RLock
from typing import Literal

from pydantic import BaseModel, ConfigDict

from server.agents.schemas import BrokerRemedy


class HotelDispatchRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    recovery_id: str
    remedy: BrokerRemedy


class HotelDispatchResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    dispatch_id: str
    status: Literal["confirmed"]
    simulated: Literal[True]
    provider_result: str


class HotelSimulator:
    """Record at most one simulated provider dispatch per idempotency key."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._results: dict[str, HotelDispatchResult] = {}
        self._dispatch_count = 0

    @property
    def dispatch_count(self) -> int:
        with self._lock:
            return self._dispatch_count

    def dispatch(
        self,
        request: HotelDispatchRequest,
        *,
        idempotency_key: str,
    ) -> HotelDispatchResult:
        """Return the stored result when the same dispatch is retried."""

        if not idempotency_key.strip():
            raise ValueError("idempotency_key must not be empty")

        with self._lock:
            stored = self._results.get(idempotency_key)
            if stored is not None:
                return stored

            self._dispatch_count += 1
            result = HotelDispatchResult(
                dispatch_id=f"demo-hotel-dispatch-{self._dispatch_count}",
                status="confirmed",
                simulated=True,
                provider_result=(
                    "Demo hotel adapter confirmed the replacement room; "
                    "no real booking or payment was changed."
                ),
            )
            self._results[idempotency_key] = result
            return result
