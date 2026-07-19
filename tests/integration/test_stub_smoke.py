from __future__ import annotations

import json
from typing import Any, cast

from server.smoke_stub import main


def test_keyless_stub_smoke_proves_approve_and_decline(capsys: Any) -> None:
    main()

    output = cast(dict[str, object], json.loads(capsys.readouterr().out))
    assert output["smoke"] == "passed"
    assert output["sdkApproveStatus"] == "completed"
    assert output["sdkApproveExecutionCount"] == 1
    assert output["sdkDeclineStatus"] == "closed_without_action"
    assert output["sdkDeclineExecutionCount"] == 0
    assert output["sdkDeclineProviderDispatchCount"] == 0
    assert output["sdkDeclineVerificationResults"] == [
        "Exact interruption rejected.",
        "No replacement remedy selected.",
        "Execution count is zero.",
        "Provider dispatch did not begin.",
        "Temporary permission revoked.",
        "Cancellation receipt sealed.",
    ]

