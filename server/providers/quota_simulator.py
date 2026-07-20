"""Deterministic, idempotent API-quota provider boundary."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from threading import RLock
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field


class QuotaGrantRequest(BaseModel):
    """Caller-owned delegated grant request; provider proof is not caller supplied."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    recovery_id: str = Field(min_length=1)
    recorded_demand_rpm: int = Field(gt=0)
    temporary_burst_rpm: int = Field(gt=0)
    region: str = Field(min_length=1)
    duration_seconds: int = Field(gt=0)
    extra_cost_minor: int = Field(ge=0)
    delegated_authority_max_minor: int = Field(ge=0)
    currency: str = Field(min_length=1)


class QuotaGrantResult(BaseModel):
    """Verified provider proof returned by the deterministic boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    dispatch_id: str
    grant_id: str
    status: Literal["verified"]
    simulated: Literal[True]
    provider_ceiling_rpm: Literal[1000]
    granted_burst_rpm: Literal[1500]
    region: Literal["US"]
    duration_seconds: Literal[900]
    extra_cost_minor: Literal[250]
    currency: Literal["USD"]
    provider_result: str


class QuotaVerificationError(ValueError):
    """Raised when policy or returned provider proof cannot be verified."""


class QuotaIdempotencyConflictError(ValueError):
    """Raised when an idempotency key is reused for a different request."""


ProviderOperation = Callable[[QuotaGrantRequest], None]
GrantVerifier = Callable[[QuotaGrantResult], bool]


class QuotaSimulator:
    """Issue one verified temporary grant and always revoke runtime permission."""

    PROVIDER_CEILING_RPM: ClassVar[Literal[1000]] = 1000

    def __init__(
        self,
        *,
        provider_operation: ProviderOperation | None = None,
        grant_verifier: GrantVerifier | None = None,
    ) -> None:
        self._lock = RLock()
        self._provider_operation = provider_operation or (lambda _request: None)
        self._grant_verifier = grant_verifier or self._verify_grant
        self._results: dict[str, tuple[str, QuotaGrantResult]] = {}
        self._active_permissions: set[str] = set()
        self._revoked_permissions: set[str] = set()
        self._dispatch_count = 0

    @staticmethod
    def _fingerprint(request: QuotaGrantRequest) -> str:
        encoded = json.dumps(
            request.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _verify_grant(result: QuotaGrantResult) -> bool:
        return (
            result.provider_ceiling_rpm == 1000
            and result.granted_burst_rpm == 1500
            and result.region == "US"
            and result.duration_seconds <= 900
            and result.extra_cost_minor == 250
            and result.currency == "USD"
        )

    @staticmethod
    def _assert_policy(request: QuotaGrantRequest) -> None:
        canonical = (
            request.recorded_demand_rpm == 1200
            and request.temporary_burst_rpm == 1500
            and request.region == "US"
            and request.duration_seconds == 900
            and request.extra_cost_minor == 250
            and request.delegated_authority_max_minor == 500
            and request.currency == "USD"
        )
        constraints = (
            request.temporary_burst_rpm >= request.recorded_demand_rpm
            and request.duration_seconds <= 900
            and request.extra_cost_minor <= request.delegated_authority_max_minor
        )
        if not canonical or not constraints:
            raise QuotaVerificationError("Quota policy verification failed")

    @property
    def dispatch_count(self) -> int:
        with self._lock:
            return self._dispatch_count

    @property
    def active_permission_count(self) -> int:
        with self._lock:
            return len(self._active_permissions)

    def was_permission_revoked(self, permission_scope_id: str) -> bool:
        with self._lock:
            return permission_scope_id in self._revoked_permissions

    def dispatch(
        self,
        request: QuotaGrantRequest,
        *,
        idempotency_key: str,
        permission_scope_id: str,
    ) -> QuotaGrantResult:
        if not idempotency_key.strip():
            raise ValueError("idempotency_key must not be empty")
        if not permission_scope_id.strip():
            raise ValueError("permission_scope_id must not be empty")

        validated_request = QuotaGrantRequest.model_validate(
            request.model_dump(mode="python")
        )
        fingerprint = self._fingerprint(validated_request)
        with self._lock:
            self._active_permissions.add(permission_scope_id)
            try:
                self._assert_policy(validated_request)
                stored = self._results.get(idempotency_key)
                if stored is not None:
                    stored_fingerprint, stored_result = stored
                    if stored_fingerprint != fingerprint:
                        raise QuotaIdempotencyConflictError(
                            "Idempotency key was already used for a different request"
                        )
                    return stored_result

                self._provider_operation(validated_request)
                self._dispatch_count += 1
                dispatch_hash = hashlib.sha256(
                    idempotency_key.encode("utf-8")
                ).hexdigest()
                result = QuotaGrantResult(
                    dispatch_id=f"demo-quota-dispatch-{dispatch_hash[:16]}",
                    grant_id=f"temporary-quota-grant-{dispatch_hash[:16]}",
                    status="verified",
                    simulated=True,
                    provider_ceiling_rpm=self.PROVIDER_CEILING_RPM,
                    granted_burst_rpm=1500,
                    region="US",
                    duration_seconds=900,
                    extra_cost_minor=250,
                    currency="USD",
                    provider_result=(
                        "Demo quota adapter verified a temporary US burst grant; "
                        "the base quota was unchanged."
                    ),
                )
                if not self._grant_verifier(result):
                    raise QuotaVerificationError("Quota grant verification failed")
                self._results[idempotency_key] = (fingerprint, result)
                return result
            finally:
                self._active_permissions.discard(permission_scope_id)
                self._revoked_permissions.add(permission_scope_id)
