"""Thread-safe, idempotent demo hotel provider boundary."""

from __future__ import annotations

import hashlib
import json
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


class IdempotencyConflictError(ValueError):
    """Raised when one idempotency key is reused for a different request."""


class HotelSimulator:
    """Record at most one simulated provider dispatch per idempotency key."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._results: dict[str, tuple[str, HotelDispatchResult]] = {}
        self._dispatch_count = 0

    @staticmethod
    def _request_fingerprint(request: HotelDispatchRequest) -> str:
        canonical_request = json.dumps(
            request.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(canonical_request).hexdigest()

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

        request_fingerprint = self._request_fingerprint(request)
        with self._lock:
            stored = self._results.get(idempotency_key)
            if stored is not None:
                stored_fingerprint, stored_result = stored
                if stored_fingerprint != request_fingerprint:
                    raise IdempotencyConflictError(
                        "Idempotency key was already used for a different request"
                    )
                return stored_result

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
            self._results[idempotency_key] = (request_fingerprint, result)
            return result
