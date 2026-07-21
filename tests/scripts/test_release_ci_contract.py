from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

CHECKOUT_SHA = "d23441a48e516b6c34aea4fa41551a30e30af803"
NODE_SHA = "48b55a011bda9f5d6aeb4c2d9c7362e8dae4041e"
PYTHON_SHA = "a309ff8b426b58ec0e2a45f0f869d46889d02405"
UV_SHA = "08807647e7069bb48b6ef5acd8ec9567f424441b"


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _package_scripts() -> dict[str, str]:
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    return package["scripts"]


def test_workflow_has_exactly_two_bounded_least_privilege_jobs() -> None:
    workflow = _workflow()
    jobs_block = workflow.split("\njobs:\n", maxsplit=1)[1]
    job_names = re.findall(
        r"^  ([a-z][a-z0-9-]*):\n", jobs_block, flags=re.MULTILINE
    )

    assert job_names == ["verify", "container-smoke"]
    assert "permissions:\n  contents: read" in workflow
    assert "concurrency:" in workflow
    assert "cancel-in-progress: true" in workflow
    assert workflow.count("runs-on: ubuntu-24.04") == 2
    assert workflow.count("timeout-minutes:") == 2
    assert "needs: verify" in workflow


def test_verify_uses_immutable_actions_exact_runtimes_and_full_history() -> None:
    workflow = _workflow()

    assert workflow.count(f"actions/checkout@{CHECKOUT_SHA}") == 2
    assert f"actions/setup-node@{NODE_SHA}" in workflow
    assert f"actions/setup-python@{PYTHON_SHA}" in workflow
    assert f"astral-sh/setup-uv@{UV_SHA}" in workflow
    assert "node-version-file: .node-version" in workflow
    assert (ROOT / ".node-version").read_text(encoding="utf-8").strip() == "22.22.0"
    assert "python-version: 3.12.12" in workflow
    assert "version: 0.10.1" in workflow
    assert "fetch-depth: 0" in workflow
    assert workflow.count("persist-credentials: false") == 2

    ordered_commands = (
        "npm ci",
        "uv sync --frozen --all-groups",
        "npm run check",
        "npm run smoke:stub",
        "npm run openapi:check",
        "npm run secret:scan",
    )
    positions = [workflow.index(command) for command in ordered_commands]
    assert positions == sorted(positions)


def test_container_smoke_is_offline_non_root_and_runs_both_packaged_profiles() -> None:
    workflow = _workflow()

    assert "docker build --tag backchannel:ci ." in workflow
    assert "docker image inspect --format '{{.Config.User}}' backchannel:ci" in workflow
    assert '"10001:10001"' in workflow
    base = (
        "docker run --rm --network none --entrypoint python backchannel:ci "
        "scripts/docker_smoke.py --launch"
    )
    assert base in workflow
    assert base + " --profile deployed-readonly" in workflow

    lowered = workflow.lower()
    assert "openai_api_key" not in lowered
    assert "smoke:live" not in lowered
    assert "smoke:production" not in lowered


def test_package_scripts_wire_release_tools_outside_the_canonical_check() -> None:
    scripts = _package_scripts()

    assert scripts["openapi:export"] == "uv run python scripts/export_openapi.py"
    assert scripts["openapi:check"] == "uv run python scripts/export_openapi.py --check"
    assert scripts["secret:scan"] == "uv run python scripts/secret_scan.py"
    assert "scripts/export_openapi.py" in scripts["check"]
    assert "scripts/secret_scan.py" in scripts["check"]
    assert "openapi:" not in scripts["check"]
    assert "secret:scan" not in scripts["check"]


def test_gitignore_covers_generated_playwright_outputs_but_not_release_evidence() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    for expected in ("*.log", "playwright-report/", "test-results/", "blob-report/"):
        assert expected in ignored
    assert "docs/openapi.json" not in ignored
    assert "docs/assets/final/" not in ignored
