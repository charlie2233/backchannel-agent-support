from __future__ import annotations

import binascii
import json
import re
import struct
import subprocess
import zlib
from pathlib import Path

import pytest

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
    payload = path.read_bytes()
    assert payload[:8] == b"\x89PNG\r\n\x1a\n"
    offset = 8
    dimensions: tuple[int, int] | None = None
    chunk_types: list[bytes] = []
    idat_payload = bytearray()
    saw_iend = False

    while offset < len(payload):
        assert len(payload) - offset >= 12
        chunk_length = struct.unpack(">I", payload[offset : offset + 4])[0]
        chunk_type = payload[offset + 4 : offset + 8]
        data_start = offset + 8
        data_end = data_start + chunk_length
        chunk_end = data_end + 4
        assert chunk_end <= len(payload)
        chunk_data = payload[data_start:data_end]
        expected_crc = struct.unpack(">I", payload[data_end:chunk_end])[0]
        actual_crc = binascii.crc32(chunk_type + chunk_data) & 0xFFFFFFFF
        assert actual_crc == expected_crc

        if chunk_type == b"IHDR":
            assert not chunk_types
            assert dimensions is None
            assert chunk_length == 13
            dimensions = struct.unpack(">II", chunk_data[:8])
        elif chunk_type == b"IDAT":
            assert dimensions is not None
            idat_payload.extend(chunk_data)
        elif chunk_type == b"IEND":
            assert chunk_length == 0
            assert not saw_iend
            saw_iend = True

        chunk_types.append(chunk_type)
        offset = chunk_end
        if saw_iend:
            assert offset == len(payload)
            break

    assert saw_iend
    assert chunk_types.count(b"IEND") == 1
    assert chunk_types[-1] == b"IEND"
    assert dimensions is not None
    assert b"IDAT" in chunk_types
    inflater = zlib.decompressobj()
    decompressed = inflater.decompress(bytes(idat_payload)) + inflater.flush()
    assert inflater.eof
    assert inflater.unused_data == b""
    assert inflater.unconsumed_tail == b""
    assert decompressed
    return dimensions


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
    assert scripts["smoke:container:restart"] == (
        "uv run python scripts/docker_restart_smoke.py"
    )

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
    assert "BACKCHANNEL_DEPLOYED_MODE=false" in server_environment
    assert "scripts/start.py" in orchestration

    capture = _read("e2e/capture-release.mjs")
    assert "sdk_stub" in capture
    assert "replay_fixture" not in capture
    assert "@playwright/test" not in capture
    assert 'page.on("console"' in capture
    assert 'page.on("pageerror"' in capture


def test_release_capture_bootstraps_without_starting_the_spa() -> None:
    orchestration = _read("scripts/capture_release.sh")
    assert re.search(
        r'open \\\n\s+"http://127\.0\.0\.1:\$release_port/health" \\\n',
        orchestration,
    )

    capture = _read("e2e/capture-release.mjs")
    reset = capture.split("  const reset = async () => {", 1)[1].split("  };", 1)[0]
    health_navigation = reset.index('page.goto(`${baseUrl}/health`')
    storage_clear = reset.index("window.sessionStorage.clear()")
    storage_assertion = reset.index("window.sessionStorage.length")
    demo_reset = reset.index("/api/demo/reset")
    explicit_demo_choice = reset.index(
        '"backchannel.hotelRecoveryId", "capture-explicit-demo-choice"'
    )
    assert (
        health_navigation
        < storage_clear
        < storage_assertion
        < demo_reset
        < explicit_demo_choice
    )
    assert 'page.goto("about:blank")' not in reset

    expected_post_count = capture.index("let expectedRecoveryPostCount = 0")
    root_navigation = capture.index(
        'await page.goto(baseUrl, { waitUntil: "domcontentloaded" })'
    )
    sdk_button_visible = capture.index(
        'await runButton.waitFor({ state: "visible" })'
    )
    no_automatic_post = capture.index(
        "recoveryPostCount === expectedRecoveryPostCount"
    )
    sdk_button_click = capture.index("await runButton.click()")
    increment_expected_count = capture.index(
        "expectedRecoveryPostCount += 1"
    )
    duplicate_settle = capture.index(
        "await page.waitForTimeout(250)",
        increment_expected_count,
    )
    exactly_one_sdk_post = capture.index(
        "recoveryPostCount === expectedRecoveryPostCount",
        no_automatic_post + 1,
    )
    assert (
        expected_post_count
        < root_navigation
        < sdk_button_visible
        < no_automatic_post
        < sdk_button_click
        < increment_expected_count
        < duplicate_settle
        < exactly_one_sdk_post
    )
    final_stable_count = capture.index("recoveryPostCount === 6")
    final_expected_count = capture.index("expectedRecoveryPostCount === 6")
    completion_return = capture.index('return ["backchannel"')
    assert (
        exactly_one_sdk_post
        < final_stable_count
        < final_expected_count
        < completion_return
    )
    assert "const recoveryPostBaseline" not in capture


def test_release_capture_lock_precedes_and_protects_shared_evidence(
    tmp_path: Path,
) -> None:
    orchestration = _read("scripts/capture_release.sh")
    lock_helpers = orchestration.split("# capture-lock:start", 1)[1].split(
        "# capture-lock:end", 1
    )[0]
    lock_path = tmp_path / "capture.lock"
    lock_path.mkdir()
    audit_log = tmp_path / "run-code.stdout.log"
    audit_log.write_text("first capture still owns this log\n", encoding="utf-8")
    command = (
        "set -euo pipefail\n"
        f"{lock_helpers}\n"
        'acquire_release_capture_lock "$1"\n'
        ': > "$2"'
    )
    contender = subprocess.run(
        ["bash", "-c", command, "lock-test", str(lock_path), str(audit_log)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert contender.returncode != 0
    assert "already running" in contender.stderr.lower()
    assert audit_log.read_text(encoding="utf-8") == (
        "first capture still owns this log\n"
    )

    acquire = orchestration.index(
        'acquire_release_capture_lock "$release_lock"'
    )
    output_setup = orchestration.index(
        'mkdir -p "$release_output" "$release_captures"',
        acquire,
    )
    run_log_truncate = orchestration.index(': > "$release_run_log"', output_setup)
    server_stdout_truncate = orchestration.index(
        ': > "$release_server_stdout_log"',
        output_setup,
    )
    server_stderr_truncate = orchestration.index(
        ': > "$release_server_stderr_log"',
        output_setup,
    )
    prerequisite_check = orchestration.index(
        "if ! command -v npx",
        output_setup,
    )
    build = orchestration.index("npm run build", prerequisite_check)
    browser_open = orchestration.index(
        'run_release_cli --session "$release_session" open',
        build,
    )
    assert (
        acquire
        < output_setup
        < run_log_truncate
        < server_stdout_truncate
        < server_stderr_truncate
        < prerequisite_check
        < build
        < browser_open
    )
    assert 'release_release_capture_lock "$release_lock"' in orchestration
    assert "trap cleanup_release_capture EXIT" in orchestration
    assert "trap 'exit 130' INT" in orchestration
    assert "trap 'exit 143' TERM" in orchestration


def test_release_capture_requires_the_exact_success_result_marker(
    tmp_path: Path,
) -> None:
    orchestration = _read("scripts/capture_release.sh")
    validator = orchestration.split("# marker-validation:start", 1)[1].split(
        "# marker-validation:end", 1
    )[0]
    command = (
        "set -euo pipefail\n"
        f"{validator}\n"
        'require_release_completion_marker "$1" "$2"'
    )
    marker = "backchannel:release:capture:complete"

    successful_output = tmp_path / "successful.log"
    successful_output.write_text(
        f"### Result\n{json.dumps(marker)}\n### Ran Playwright code\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["bash", "-c", command, "marker-test", str(successful_output), marker],
        check=True,
    )

    echoed_source_only = tmp_path / "echoed-source-only.log"
    echoed_source_only.write_text(
        f"### Ran Playwright code\n{json.dumps(marker)}\n",
        encoding="utf-8",
    )
    failed = subprocess.run(
        ["bash", "-c", command, "marker-test", str(echoed_source_only), marker],
        check=False,
        capture_output=True,
        text=True,
    )
    assert failed.returncode != 0
    assert "completion marker" in failed.stderr.lower()

    truncate = orchestration.index(': > "$release_run_log"')
    run_code = orchestration.index("run-code --filename")
    marker_check = orchestration.index(
        'require_release_completion_marker "$release_run_log"'
    )
    dimension_check = orchestration.index(
        "tests/release/test_release_evidence.py::"
        "test_final_build_captures_exist_at_exact_viewports"
    )
    assert truncate < run_code < marker_check < dimension_check

    capture = _read("e2e/capture-release.mjs")
    browser_assertion = capture.index("browserErrors.length === 0")
    completion_return = capture.index('return ["backchannel"')
    assert browser_assertion < completion_return
    assert marker not in capture


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


def test_final_build_capture_validation_rejects_truncation_and_corruption(
    tmp_path: Path,
) -> None:
    source = FINAL_ASSETS / "pending-mobile.png"
    payload = source.read_bytes()

    truncated = tmp_path / "truncated.png"
    truncated.write_bytes(payload[:-8])
    with pytest.raises(AssertionError):
        _png_size(truncated)

    corrupted_payload = bytearray(payload)
    offset = 8
    while offset < len(corrupted_payload):
        chunk_length = struct.unpack(">I", corrupted_payload[offset : offset + 4])[0]
        chunk_type = bytes(corrupted_payload[offset + 4 : offset + 8])
        if chunk_type == b"IDAT" and chunk_length > 0:
            corrupted_payload[offset + 8] ^= 0x01
            break
        offset += 12 + chunk_length
    else:
        raise AssertionError("Fixture PNG contained no IDAT data to corrupt.")
    corrupted = tmp_path / "corrupted.png"
    corrupted.write_bytes(corrupted_payload)
    with pytest.raises(AssertionError):
        _png_size(corrupted)


def test_ci_is_keyless_lockfile_based_and_declares_external_gates() -> None:
    workflow = _read(".github/workflows/ci.yml")
    verify_job, container_job = workflow.split("  container-smoke:", 1)
    packaged_start = container_job.split("- name: Start packaged app", 1)[1].split(
        "- run: npm run smoke:docker",
        1,
    )[0]
    approved_action_pins = (
        "actions/checkout@93cb6efe18208431cddfb8368fd83d5badbf9bfd # v5.0.1",
        "actions/setup-node@a0853c24544627f65ddf259abe73b1d18a591444 # v5.0.0",
        "actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405 # v6.2.0",
        "astral-sh/setup-uv@37802adc94f370d6bfd71619e3f0bf239e1f3b78 # v7.6.0",
    )
    for pin in approved_action_pins:
        assert verify_job.count(pin) == 1, pin
        assert container_job.count(pin) == 1, pin
    for deprecated_ref in (
        "actions/checkout@v4",
        "actions/setup-node@v4",
        "actions/setup-python@v5",
        "astral-sh/setup-uv@v6",
    ):
        assert deprecated_ref not in workflow

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
        "npm run smoke:container:restart",
    ):
        assert phrase in workflow
    assert 'OPENAI_API_KEY: ""' in workflow
    assert "openssl rand -hex" in workflow
    assert "::add-mask::$release_canary" in workflow
    assert "BACKCHANNEL_SMOKE_CANARY=%s" in workflow
    assert "-e BACKCHANNEL_ALLOWED_HOSTS=127.0.0.1" in packaged_start
    assert '-e OPENAI_API_KEY="$BACKCHANNEL_SMOKE_CANARY"' in workflow
    assert "docker build --pull -t backchannel:build-week ." in container_job
    assert "BACKCHANNEL_DEMO_RESET_ENABLED" not in packaged_start
    assert re.search(r"sk-[A-Za-z0-9_-]{20,}", workflow) is None

    assert re.search(
        r"uses: actions/checkout@93cb6efe18208431cddfb8368fd83d5badbf9bfd"
        r" # v5\.0\.1\n\s+with:\n\s+fetch-depth: 0",
        verify_job,
    )
    assert "fetch-depth: 0" not in container_job
    assert "timeout-minutes: 10" in verify_job
    assert "timeout-minutes: 15" in container_job
    for job in (verify_job, container_job):
        assert job.count("node-version-file: .node-version") == 1
        assert job.count("cache: npm") == 1
        assert job.count('python-version: "3.12"') == 1

    validation = _read("docs/validation.md").lower()
    assert "head-ancestry file blobs" in validation
    assert "current release inputs" in validation
    assert "shallow" in validation and "fail closed" in validation
    assert "path or byte ceilings" in validation


def test_release_docs_separate_hosted_container_proof_from_external_gates() -> None:
    source_sha = "250a70a08c95346bac409c98c198499d7395276b"
    run_id = "30605239475"
    verify_job_id = "91076029786"
    container_job_id = "91076408372"
    run_url = "https://github.com/charlie2233/backchannel-agent-support/actions/runs/30605239475"
    documents = {
        "validation": _read("docs/validation.md"),
        "judge checklist": _read("docs/judge-checklist.md"),
    }

    for name, document in documents.items():
        normalized = " ".join(document.lower().split())
        assert run_url in document, name
        assert set(
            re.findall(
                r"https://github\.com/charlie2233/backchannel-agent-support/actions/runs/\d+",
                document,
            )
        ) == {run_url}, name
        assert set(re.findall(r"(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])", normalized)) == {
            source_sha
        }, name
        assert set(re.findall(r"(?<!\d)\d{11}(?!\d)", document)) == {
            run_id,
            verify_job_id,
            container_job_id,
        }, name
        assert source_sha in document, name
        assert verify_job_id in document, name
        assert container_job_id in document, name
        assert "github-hosted" in normalized, name
        assert "packaged container" in normalized, name
        if name == "validation":
            evidence_scopes = [
                " ".join(paragraph.lower().split())
                for paragraph in re.split(r"\n\s*\n", document)
                if run_url in paragraph
            ]
        else:
            evidence_scopes = [
                " ".join(line.lower().split())
                for line in document.splitlines()
                if run_url in line
            ]
        assert evidence_scopes, name
        for evidence_scope in evidence_scopes:
            assert source_sha in evidence_scope, name
            assert container_job_id in evidence_scope, name
            assert re.search(
                r"(?:^|[.!?]\s)the container(?: job)? used "
                r"`docker build --pull`(?: and|,) resolved the reviewed "
                r"node, python, and `uv` tag-plus-oci-index-digest references",
                evidence_scope,
            ), name
        assert "built frontend assets" in normalized, name
        assert "title" in normalized and "csp" in normalized, name
        assert "api calls" in normalized, name
        assert "sse resume" in normalized, name
        assert "not a browser ui interaction" in normalized, name
        assert "local container runtime" in normalized, name
        assert "public" in normalized and "unverified" in normalized, name
        for restart_claim in (
            "planned replacement of two distinct single-worker containers",
            "same disposable named volume",
            "`/data`",
            "same identity-signing secret",
            "only the owner's signed cookie was retained in client memory",
            "container b accepted it under the same signing secret",
            "fresh foreign cookie remained isolated",
            "two pending `sdk_stub` recoveries",
            "approval and decline",
            "terminal replay",
            "two identical approval http requests were accepted idempotently",
            "deterministic `sdk_stub` hotel demo-adapter dispatch occurred once",
            "decline request closed without action",
            "demo-adapter dispatch occurred zero times",
            "this deterministic demo-adapter execution is not real provider execution",
            "replay receipt content matched exactly after json decoding",
            "replay sse bytes matched exactly",
            "exact process exit code `0`",
            "signal exit `143` fails before a replacement starts",
            "`cleanexitwithactivesse`",
            (
                "does not claim a server-delivered eof or that every buffered "
                "client byte flushed"
            ),
        ):
            assert restart_claim in normalized, (name, restart_claim)
        for ambiguous_claim in (
            "owner in-memory signed session continuity",
            "exact approval retry executed once",
            "decline executed zero times",
            "receipt and sse persisted byte-for-byte",
        ):
            assert ambiguous_claim not in normalized, (name, ambiguous_claim)
        assert "does not prove local docker" in normalized, name
        for excluded_gate in (
            "abrupt host loss",
            "backup",
            "target-host durability",
            "target-host networking",
            "concurrent multi-container sqlite",
            "public deployment",
            "public reachability",
            "live openai",
            "provider execution",
            "image signing",
            "builder attestation",
            "sbom",
            "vulnerability scanning",
            "publisher trust",
            "target-host admission",
            "cross-platform bit-identical images",
        ):
            assert excluded_gate in normalized, (name, excluded_gate)

    combined = "\n".join(documents.values()).lower()
    for verified_step in (
        "docker build",
        "readiness",
        "approval",
        "decline",
        "receipt",
        "secret canary",
        "session boundary",
    ):
        assert verified_step in combined

    for stale_denial in (
        "neither a docker build nor a running-container smoke has been verified",
        "no image build or container smoke is claimed",
        "container, deployment, and public reachability | **unverified external gates**",
        "frontend/api/sse approval",
        "exercised the built frontend",
        "covered frontend/api/sse approval",
    ):
        assert stale_denial not in combined
