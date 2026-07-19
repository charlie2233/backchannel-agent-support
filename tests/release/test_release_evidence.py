from __future__ import annotations

import json
import re
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FINAL_ASSETS = ROOT / "docs" / "assets" / "final"
CAPTURES = {
    "pending-desktop.png": (1440, 1024),
    "completed-desktop.png": (1440, 1024),
    "declined-desktop.png": (1440, 1024),
    "pending-mobile.png": (390, 844),
    "completed-mobile.png": (390, 844),
    "declined-mobile.png": (390, 844),
}


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _png_size(path: Path) -> tuple[int, int]:
    payload = path.read_bytes()[:24]
    assert payload[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", payload[16:24])


def test_release_documentation_covers_the_judge_contract() -> None:
    required = [
        "README.md",
        "docs/architecture.md",
        "docs/protocol.md",
        "docs/validation.md",
        "docs/judge-checklist.md",
    ]
    for relative_path in required:
        assert (ROOT / relative_path).is_file(), relative_path

    readme = _read("README.md")
    for phrase in (
        "Detect",
        "Prove",
        "Negotiate",
        "Authorize",
        "Execute",
        "Verify & seal",
        "npm run smoke:live",
        "npm run start",
        "demo adapter",
        "Google Chrome",
    ):
        assert phrase in readme

    checklist = _read("docs/judge-checklist.md")
    assert "https://github.com/charlie2233/backchannel-agent-support" in checklist
    assert "Public demo URL" in checklist
    assert "OPENAI_API_KEY" in checklist
    assert "Docker" in checklist
    assert "three-minute" in checklist.lower()
    assert "Measured" in checklist
    assert "Simulated" in checklist
    assert "Fidelity ledger" in checklist


def test_release_commands_use_cli_first_browser_capture() -> None:
    package = json.loads(_read("package.json"))
    scripts = package["scripts"]
    assert package["devDependencies"]["@playwright/cli"] == "0.1.17"
    lockfile = json.loads(_read("package-lock.json"))
    assert lockfile["packages"][""]["devDependencies"]["@playwright/cli"] == "0.1.17"
    assert scripts["test:e2e"] == "bash scripts/capture_release.sh"
    assert scripts["secret:scan"] == "uv run python scripts/secret_scan.py"
    assert scripts["openapi:export"] == "uv run python scripts/export_openapi.py"
    assert scripts["openapi:check"] == "uv run python scripts/export_openapi.py --check"

    orchestration = _read("scripts/capture_release.sh")
    assert "npm run build" in orchestration
    assert "run-code --filename" in orchestration
    assert "npx --no-install" in orchestration
    assert "node_modules/.bin/playwright-cli" in orchestration
    assert "--browser chrome" in orchestration
    assert '--config "$release_config"' in orchestration
    assert "PWTEST_CLI_GLOBAL_CONFIG" in orchestration
    assert "${TMPDIR:-/tmp}" in orchestration
    assert "mktemp -d" in orchestration
    assert "/Users/" not in orchestration
    assert "/private/tmp" not in orchestration
    config = json.loads(_read("e2e/playwright-cli.config.json"))
    assert config["browser"]["browserName"] == "chromium"
    assert config["browser"]["isolated"] is True
    assert config["browser"]["launchOptions"] == {
        "channel": "chrome",
        "headless": True,
    }
    assert config["browser"]["contextOptions"]["locale"] == "en-US"
    assert config["browser"]["contextOptions"]["serviceWorkers"] == "block"

    server_environment = orchestration.split("# sanitized-server-env:start", 1)[1].split(
        "# sanitized-server-env:end", 1
    )[0]
    assert "env -i" in server_environment
    for explicit_name in (
        "PATH",
        "HOME",
        "TMPDIR",
        "UV_CACHE_DIR",
        "BACKCHANNEL_DB_PATH",
        "BACKCHANNEL_DEMO_RESET_ENABLED",
        "BACKCHANNEL_DEPLOYED_MODE",
        "PORT",
    ):
        assert f"{explicit_name}=" in server_environment
    assert "OPENAI_API_KEY" not in server_environment
    assert set(re.findall(r"\b(BACKCHANNEL_[A-Z_]+)=", server_environment)) == {
        "BACKCHANNEL_DB_PATH",
        "BACKCHANNEL_DEMO_RESET_ENABLED",
        "BACKCHANNEL_DEPLOYED_MODE",
    }
    assert "OPENAI_API_KEY" not in orchestration
    assert "BACKCHANNEL_DEMO_RESET_ENABLED=true" in orchestration
    assert "scripts/start.py" in orchestration

    capture = _read("e2e/capture-release.mjs")
    assert "sdk_stub" in capture
    assert "replay_fixture" not in capture
    assert "@playwright/test" not in capture
    assert "page.on(\"console\"" in capture
    assert "page.on(\"pageerror\"" in capture


def test_generated_openapi_is_current_and_public() -> None:
    schema = json.loads(_read("docs/openapi.json"))
    assert schema["info"]["title"] == "Backchannel API"
    assert schema["info"]["version"] == "0.3.0"
    assert "/health" in schema["paths"]
    assert "/readyz" in schema["paths"]
    assert "/api/recoveries" in schema["paths"]
    serialized = json.dumps(schema).lower()
    assert "openai_api_key" not in serialized
    assert "state_json" not in serialized
    assert "securityschemes" not in serialized
    assert not any(
        parameter.get("name", "").lower() == "authorization"
        for path in schema["paths"].values()
        for operation in path.values()
        for parameter in operation.get("parameters", [])
    )


def test_final_build_captures_exist_at_exact_viewports() -> None:
    assert sorted(path.name for path in FINAL_ASSETS.glob("*.png")) == sorted(CAPTURES)
    for filename, expected_size in CAPTURES.items():
        assert _png_size(FINAL_ASSETS / filename) == expected_size


def test_ci_is_keyless_lockfile_based_and_declares_external_gates() -> None:
    workflow = _read(".github/workflows/ci.yml")
    for phrase in (
        "npm ci",
        "uv sync --frozen --all-groups",
        "npm run check",
        "npm run smoke:stub",
        "npm run smoke:production",
        "npm run secret:scan",
        "npm run openapi:check",
        "docker build",
        "npm run smoke:docker",
    ):
        assert phrase in workflow
    assert "OPENAI_API_KEY: \"\"" in workflow
    assert "openssl rand -hex" in workflow
    assert "::add-mask::$release_canary" in workflow
    assert "BACKCHANNEL_SMOKE_CANARY=%s" in workflow
    assert '-e OPENAI_API_KEY="$BACKCHANNEL_SMOKE_CANARY"' in workflow
    assert re.search(r"sk-[A-Za-z0-9_-]{20,}", workflow) is None
