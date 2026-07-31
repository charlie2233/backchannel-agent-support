"""Thread-safe, idempotent demo hotel provider boundary."""

from __future__ import annotations

from threading import RLock
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from server.agents.schemas import BrokerRemedy
from server.providers.hotel_contract import (
    DEMO_HOTEL_PROVIDER_RESULT,
    durable_hotel_dispatch_contract,
    hotel_dispatch_request_digest,
)
from server.store import ExecutionConflictError

if TYPE_CHECKING:
    from server.store import SQLiteStore


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

    def __init__(self, *, store: SQLiteStore | None = None) -> None:
        self._lock = RLock()
        self._results: dict[str, tuple[str, HotelDispatchResult]] = {}
        self._dispatch_count = 0
        self._store = store

    def bind_store(self, store: SQLiteStore) -> None:
        """Attach the process-local adapter to the durable execution ledger."""

        with self._lock:
            if self._store is not None and self._store is not store:
                raise ValueError("Hotel simulator is already bound to another store")
            self._store = store

    @staticmethod
    def _request_fingerprint(request: HotelDispatchRequest) -> str:
        return hotel_dispatch_request_digest(
            recovery_id=request.recovery_id,
            remedy=request.remedy,
        )

    @property
    def dispatch_count(self) -> int:
        with self._lock:
            return self._dispatch_count

    def dispatch(
        self,
        request: HotelDispatchRequest,
        *,
        idempotency_key: str,
        tool_call_id: str | None = None,
        remedy_digest: str | None = None,
        action_digest: str | None = None,
        resume_owner_id: str | None = None,
        resume_generation: int | None = None,
    ) -> HotelDispatchResult:
        """Return the stored result when the same dispatch is retried."""

        if not idempotency_key.strip():
            raise ValueError("idempotency_key must not be empty")

        request_fingerprint = self._request_fingerprint(request)
        with self._lock:
            if self._store is not None:
                if (
                    not tool_call_id
                    or not remedy_digest
                    or not action_digest
                    or resume_owner_id is None
                    or resume_generation is None
                ):
                    raise ValueError(
                        "Durable dispatch requires exact authorization identity"
                    )
                contract = durable_hotel_dispatch_contract(
                    recovery_id=request.recovery_id,
                    remedy=request.remedy,
                    tool_call_id=tool_call_id,
                    remedy_digest=remedy_digest,
                )
                if idempotency_key != contract.idempotency_key:
                    raise ValueError(
                        "Durable dispatch idempotency key is not canonical"
                    )
                candidate = HotelDispatchResult(
                    dispatch_id=contract.dispatch_id,
                    status="confirmed",
                    simulated=True,
                    provider_result=DEMO_HOTEL_PROVIDER_RESULT,
                )
                try:
                    execution, dispatched = self._store.record_completed_execution(
                        execution_id=contract.execution_id,
                        recovery_id=request.recovery_id,
                        idempotency_key=idempotency_key,
                        request_digest=contract.request_digest,
                        tool_call_id=tool_call_id,
                        remedy_digest=remedy_digest,
                        action_digest=action_digest,
                        resume_owner_id=resume_owner_id,
                        resume_generation=resume_generation,
                        result_json=candidate.model_dump(mode="json"),
                    )
                except ExecutionConflictError as error:
                    raise IdempotencyConflictError(
                        "Idempotency key was already used for a different request"
                    ) from error
                if execution.result_json is None:
                    raise RuntimeError("Completed execution is missing its provider result")
                result = HotelDispatchResult.model_validate(execution.result_json)
                if dispatched:
                    self._dispatch_count += 1
                return result

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
