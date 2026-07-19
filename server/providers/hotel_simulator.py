"""Thread-safe, idempotent demo hotel provider boundary."""

from __future__ import annotations

import hashlib
import json
from threading import RLock
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from server.agents.schemas import BrokerRemedy
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
        tool_call_id: str | None = None,
        remedy_digest: str | None = None,
    ) -> HotelDispatchResult:
        """Return the stored result when the same dispatch is retried."""

        if not idempotency_key.strip():
            raise ValueError("idempotency_key must not be empty")

        request_fingerprint = self._request_fingerprint(request)
        with self._lock:
            if self._store is not None:
                if not tool_call_id or not remedy_digest:
                    raise ValueError(
                        "Durable dispatch requires exact tool call and remedy digests"
                    )
                dispatch_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
                candidate = HotelDispatchResult(
                    dispatch_id=f"demo-hotel-dispatch-{dispatch_hash[:16]}",
                    status="confirmed",
                    simulated=True,
                    provider_result=(
                        "Demo hotel adapter confirmed the replacement room; "
                        "no real booking or payment was changed."
                    ),
                )
                try:
                    execution, dispatched = self._store.record_completed_execution(
                        execution_id=f"execution-{dispatch_hash}",
                        recovery_id=request.recovery_id,
                        idempotency_key=idempotency_key,
                        request_digest=request_fingerprint,
                        tool_call_id=tool_call_id,
                        remedy_digest=remedy_digest,
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
