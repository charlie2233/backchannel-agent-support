import pytest

from server.providers.quota_simulator import (
    QuotaGrantRequest,
    QuotaSimulator,
    QuotaVerificationError,
)


def canonical_request() -> QuotaGrantRequest:
    return QuotaGrantRequest(
        recovery_id="recovery-quota-1",
        recorded_demand_rpm=1200,
        temporary_burst_rpm=1500,
        region="US",
        duration_seconds=900,
        extra_cost_minor=250,
        delegated_authority_max_minor=500,
        currency="USD",
    )


def test_quota_simulator_is_idempotent_for_the_exact_request() -> None:
    simulator = QuotaSimulator()

    first = simulator.dispatch(
        canonical_request(),
        idempotency_key="quota-idempotency-1",
        permission_scope_id="quota-scope-1",
    )
    second = simulator.dispatch(
        canonical_request(),
        idempotency_key="quota-idempotency-1",
        permission_scope_id="quota-scope-2",
    )

    assert second == first
    assert first.status == "verified"
    assert first.provider_ceiling_rpm == 1000
    assert first.granted_burst_rpm == 1500
    assert simulator.dispatch_count == 1
    assert simulator.active_permission_count == 0
    assert simulator.was_permission_revoked("quota-scope-1") is True
    assert simulator.was_permission_revoked("quota-scope-2") is True


def test_quota_simulator_fails_closed_when_grant_cannot_be_verified() -> None:
    simulator = QuotaSimulator(grant_verifier=lambda _result: False)

    with pytest.raises(QuotaVerificationError, match="verification"):
        simulator.dispatch(
            canonical_request(),
            idempotency_key="quota-idempotency-failed",
            permission_scope_id="quota-scope-failed",
        )

    assert simulator.dispatch_count == 1
    assert simulator.active_permission_count == 0
    assert simulator.was_permission_revoked("quota-scope-failed") is True


def test_quota_simulator_revokes_permission_when_dispatch_raises() -> None:
    def fail_provider_operation(_request: QuotaGrantRequest) -> None:
        raise RuntimeError("provider dispatch failed")

    simulator = QuotaSimulator(provider_operation=fail_provider_operation)

    with pytest.raises(RuntimeError, match="provider dispatch"):
        simulator.dispatch(
            canonical_request(),
            idempotency_key="quota-idempotency-error",
            permission_scope_id="quota-scope-error",
        )

    assert simulator.dispatch_count == 0
    assert simulator.active_permission_count == 0
    assert simulator.was_permission_revoked("quota-scope-error") is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("region", "EU"),
        ("temporary_burst_rpm", 1199),
        ("duration_seconds", 901),
        ("extra_cost_minor", 501),
    ],
)
def test_quota_simulator_rejects_policy_violations_before_dispatch(
    field: str,
    value: object,
) -> None:
    simulator = QuotaSimulator()
    request = canonical_request().model_copy(update={field: value})

    with pytest.raises(QuotaVerificationError, match="policy"):
        simulator.dispatch(
            request,
            idempotency_key=f"quota-invalid-{field}",
            permission_scope_id=f"quota-invalid-scope-{field}",
        )

    assert simulator.dispatch_count == 0
    assert simulator.active_permission_count == 0
    assert simulator.was_permission_revoked(f"quota-invalid-scope-{field}") is True
