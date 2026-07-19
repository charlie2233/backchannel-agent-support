from __future__ import annotations

import json
import os
from typing import Any, cast

from server.smoke_stub import main


def test_keyless_stub_smoke_proves_approve_and_decline(
    capsys: Any,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "non-secret-smoke-sentinel")

    main()

    output = cast(dict[str, object], json.loads(capsys.readouterr().out))
    assert os.environ["OPENAI_API_KEY"] == "non-secret-smoke-sentinel"
    assert output["smoke"] == "passed"
    assert output["sdkApproveStatus"] == "completed"
    assert output["sdkApproveExecutionCount"] == 1
    assert output["sdkDeclineStatus"] == "closed_without_action"
    assert output["sdkDeclineExecutionCount"] == 0
    assert output["sdkDeclineProviderDispatchCount"] == 0
    assert output["sdkDeclineVerificationResults"] == [
        "Human consent requested.",
        "Remedy declined by operator.",
        "Exact interruption rejected.",
        "No replacement action selected.",
        "Execution count is zero.",
        "Provider dispatch did not begin.",
        "Temporary permission revoked.",
        "Cancellation receipt sealed.",
    ]
    assert output["quotaSdkStatus"] == "completed"
    assert output["quotaSdkApprovalCount"] == 0
    assert output["quotaSdkPermissionRevoked"] is True
    assert output["resetReplayScenarioIds"] == ["hotel", "api-quota"]
