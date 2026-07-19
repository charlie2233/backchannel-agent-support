"""Run three fresh, consecutive redacted live recovery smokes."""

from __future__ import annotations

import asyncio
import json
from time import perf_counter

from scripts.smoke_live import _configure_quiet_output, run_live_smoke


async def _run_three() -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for run_number in range(1, 4):
        result = await run_live_smoke(run_number)
        result_json = result.public_json()
        result_json["run"] = run_number
        results.append(result_json)
        if result.smoke == "failed":
            break
    return results


def main() -> int:
    _configure_quiet_output()
    started_at = perf_counter()
    results = asyncio.run(_run_three())
    passed = all(result["smoke"] == "passed" for result in results)
    output = {
        "smoke": "passed" if passed else "failed",
        "targetRunCount": 3,
        "attemptedRunCount": len(results),
        "elapsedMs": max(0, round((perf_counter() - started_at) * 1000)),
        "runs": results,
    }
    print(json.dumps(output, separators=(",", ":"), sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
