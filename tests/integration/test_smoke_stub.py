import json

from server.smoke_stub import main


def test_keyless_smoke_reports_both_api_quota_proof_lanes(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-used")

    main()

    summary = json.loads(capsys.readouterr().out)
    assert summary["smoke"] == "passed"
    assert summary["runtimeMode"] == "stub_keyless"
    assert summary["quotaPolicy"] == {
        "providerCeilingRpm": 1000,
        "recordedDemandRpm": 1200,
        "temporaryBurstRpm": 1500,
        "region": "US",
        "durationSeconds": 900,
        "extraCostMinor": 250,
        "delegatedAuthorityMaxMinor": 500,
        "currency": "USD",
        "hardConstraints": {
            "regionPreserved": True,
            "burstCoversDemand": True,
            "durationWithinLimit": True,
            "baseQuotaUnchanged": True,
        },
        "humanInterruptions": 0,
        "approvals": 0,
        "providerProofVerified": True,
        "grantVerified": True,
        "protocolSteps": [
            "Detect",
            "Prove",
            "Negotiate",
            "Authorize",
            "Execute",
            "Verify & seal",
        ],
    }
    assert summary["quotaReplayProof"] == {
        "executionMode": "replay_fixture",
        "source": "recorded_fixture",
        "providerExecution": False,
        "executionCount": 0,
        "providerDispatchStarted": False,
        "permissionRevoked": False,
        "revocationEvidenceKind": "recorded_revocation_only",
    }
    assert summary["quotaSdkProof"] == {
        "executionMode": "sdk_stub",
        "source": "sdk_simulator",
        "providerExecution": True,
        "executionCount": 1,
        "providerDispatchStarted": True,
        "permissionRevoked": True,
        "revocationEvidenceKind": "runtime_permission_revoked",
        "simulatorDispatchCount": 1,
        "activePermissionCount": 0,
    }
