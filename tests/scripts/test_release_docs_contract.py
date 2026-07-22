from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

FINAL_CAPTURE_NAMES = (
    "desktop-consent.png",
    "desktop-completed.png",
    "desktop-declined.png",
    "mobile-consent.png",
    "mobile-completed.png",
    "mobile-declined.png",
)


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_package_wires_a_pinned_standalone_capture_contract() -> None:
    package = json.loads(_read("package.json"))
    scripts = package["scripts"]

    assert package["devDependencies"]["playwright-core"] == "1.61.1"
    assert scripts["test:capture-contract"] == "node --test e2e/judge-flow.test.mjs"
    assert scripts["pretest:e2e"] == "npm run build"
    assert scripts["test:e2e"] == (
        "npm run test:capture-contract && node e2e/judge-flow.mjs"
    )
    assert scripts["capture:judge"] == "npm run test:e2e"
    assert "npm run test:capture-contract" in scripts["check"]

    ignored = _read(".gitignore").splitlines()
    assert "output/playwright/" in ignored
    assert "docs/assets/final/" not in ignored


def test_capture_runner_is_final_build_chrome_only_and_never_added_to_ci() -> None:
    runner = _read("e2e/judge-flow.mjs")
    workflow = _read(".github/workflows/ci.yml")

    assert "from \"playwright-core\"" in runner
    assert "channel: \"chrome\"" in runner
    assert "headless: true" in runner
    assert "new URL(\"http://127.0.0.1" in runner
    assert '[".venv/bin/python", "scripts/start.py"]' in runner
    assert "BACKCHANNEL_FRONTEND_DIST_PATH" in runner
    assert "BACKCHANNEL_DEMO_RESET_ENABLED" in runner
    assert "BACKCHANNEL_DEPLOYED" in runner
    assert "delete" not in runner or "delete environment.OPENAI_API_KEY" in runner
    assert "networkidle" not in runner
    assert "domcontentloaded" in runner
    assert "document.fonts.ready" in runner
    assert "requestAnimationFrame" in runner
    assert "deviceScaleFactor: 1" in runner
    assert "locale: \"en-US\"" in runner
    assert "timezoneId: \"UTC\"" in runner
    assert "colorScheme: \"light\"" in runner
    assert "reducedMotion: \"reduce\"" in runner
    assert "page.route(\"**/*\"" in runner
    assert "capture_external_origin" in runner
    assert "fullPage: false" in runner

    jobs_block = workflow.split("\njobs:\n", maxsplit=1)[1]
    jobs = re.findall(r"^  ([a-z][a-z0-9-]*):\n", jobs_block, flags=re.MULTILINE)
    assert jobs == ["verify", "container-smoke"]
    assert "test:e2e" not in workflow
    assert "capture:judge" not in workflow
    assert "playwright install" not in workflow


def test_capture_runner_uses_session_owned_api_truth_and_exact_six_outputs() -> None:
    runner = _read("e2e/judge-flow.mjs")

    for capture_name in FINAL_CAPTURE_NAMES:
        assert capture_name in runner
        assert f"docs/assets/final/{capture_name}" in _read("docs/design/README.md")

    assert "POST /api/recoveries" not in runner
    assert "page.waitForResponse" in runner
    assert "/api/recoveries/" in runner
    assert "/api/demo/reset" in runner
    assert "credentials: \"same-origin\"" in runner
    assert "assertResetResponse" in runner
    assert "sessionStorage.clear()" in runner
    assert "getByRole(\"link\", { name: \"Review exact remedy\" })" in runner
    assert "getByRole(\"button\", { name: \"Approve remedy\" })" in runner
    assert "getByRole(\"button\", { name: \"Decline\" })" in runner
    assert 'getByRole("complementary", {' in runner
    assert 'name: "Completed receipt"' in runner
    assert 'name: "Closed without action"' in runner
    assert "parsePngDimensions" in runner
    assert "assertCaptureSafeDom" in runner


def test_readme_first_screen_is_judge_runnable_and_truthful() -> None:
    readme = _read("README.md")
    first_screen, remainder = readme.split("<!-- JUDGE FIRST SCREEN END -->", maxsplit=1)

    problem = (
        "Backchannel turns cross-party operational failures into evidence-bound "
        "remedies that cannot execute without the exact authority they require."
    )
    lifecycle = "Detect → Prove → Negotiate → Authorize → Execute → Verify & seal"
    ordered = (
        problem,
        lifecycle,
        "npm run capture:judge",
        "Run replay fixture",
        "npm run smoke:live",
        "npm run smoke:live:3",
        "Live mode may call OpenAI",
    )
    positions = [first_screen.index(fragment) for fragment in ordered]
    assert positions == sorted(positions)
    assert "uv sync --frozen --all-groups" in first_screen
    assert "Google Chrome Stable" in first_screen
    assert first_screen.index("uv sync --frozen --all-groups") < first_screen.index(
        "npm run capture:judge"
    )
    assert first_screen.index("Google Chrome Stable") < first_screen.index(
        "npm run capture:judge"
    )
    assert "SDK QA first" in first_screen
    assert "provider mutation remains inside the local `HotelSimulator`" in first_screen
    assert "SDK stub makes no OpenAI call" in first_screen
    assert "Replay uses no model and no runtime provider" in first_screen
    assert (
        "Nothing here books a real hotel, charges a payment method, or changes real quota."
        in first_screen
    )
    assert "docs/judge-checklist.md" in remainder


def test_replay_runbooks_finish_the_initial_sdk_recovery_before_switching() -> None:
    sequence = (
        "The keyless app automatically starts a pending SDK-stub hotel recovery.",
        "`Run replay fixture` is disabled while that recovery is pending.",
        "Approve or decline it and wait for the terminal receipt.",
        "Then choose **Run replay fixture**.",
    )
    for path in ("README.md", "docs/judge-checklist.md"):
        runbook = _read(path)
        positions = [runbook.index(step) for step in sequence]
        assert positions == sorted(positions), path


def test_release_docs_cover_architecture_protocol_and_evidence_boundaries() -> None:
    required = (
        "README.md",
        "docs/architecture.md",
        "docs/protocol.md",
        "docs/validation.md",
        "docs/judge-checklist.md",
    )
    for path in required:
        assert (ROOT / path).is_file(), path

    architecture = _read("docs/architecture.md")
    for phrase in (
        "React",
        "FastAPI",
        "SQLite",
        "server-sent events",
        "signed session ownership",
        "server-only serialized SDK state",
        "`openai_live`",
        "`sdk_stub`",
        "`replay_fixture`",
        "one process",
        "non-root",
        "`/data`",
        "concurrent multi-container SQLite",
    ):
        assert phrase in architecture

    protocol = _read("docs/protocol.md")
    for phrase in (
        "Detect → Prove → Negotiate → Authorize → Execute → Verify & seal",
        "remedyId",
        "remedyDigest",
        "toolCallId",
        "clientDecisionId",
        "durable claim",
        "lease",
        "version",
        "outcome_unknown",
        "idempotency",
        "Last-Event-ID",
        "[OpenAPI](openapi.json)",
    ):
        assert phrase in protocol

    validation = _read("docs/validation.md")
    assert "`codex/backchannel-v0.3`" in validation
    assert "9bda860a45efaf7ecc061810cbbc54562cc9c338" in validation
    assert "29868045880" in validation
    assert "17 capture contracts" in validation
    assert "198 web tests" in validation
    assert "465 Python tests" in validation
    assert "1440×1024" in validation
    assert "390×844" in validation
    for lane in (
        "Local source and tests",
        "Local final-build captures",
        "Live OpenAI",
        "Local Docker",
        "GitHub CI/container",
        "Public deployment",
        "Tag/release",
    ):
        assert lane in validation
    assert "Unverified" in validation


def test_judge_checklist_separates_three_paths_and_every_external_blocker() -> None:
    checklist = _read("docs/judge-checklist.md")

    assert "Origin URL" in checklist
    assert "Public-access status" in checklist
    assert "Public demo: **Blocked / not deployed**" in checklist
    for timed_step in ("0:00", "0:20", "0:50", "1:30", "2:10", "2:40"):
        assert timed_step in checklist
    assert "Primary: live OpenAI" in checklist
    assert "Local SDK-QA" in checklist
    assert "Replay fallback" in checklist
    assert "[invalid-key blocker](live-validation.md)" in checklist
    assert "Measured/tested" in checklist
    assert "Simulator facts" in checklist
    for blocker in (
        "public deployment",
        "live three-run",
        "target-host durability/networking",
        "abrupt host-loss/backup",
        "concurrent multi-container SQLite",
        "tag/release",
        "real provider execution",
    ):
        assert blocker in checklist


def test_docs_do_not_import_unproved_replacement_container_evidence() -> None:
    evidence_docs = "\n".join(
        _read(path)
        for path in (
            "docs/architecture.md",
            "docs/validation.md",
            "docs/judge-checklist.md",
        )
    ).lower()

    for unsupported in (
        "replacement-container",
        "named volume",
        "planned replacement persistence",
    ):
        assert unsupported not in evidence_docs
    for proven in (
        "image build",
        "non-root",
        "two offline packaged smoke profiles",
    ):
        assert proven in evidence_docs


def test_architecture_has_a_concrete_single_process_sse_deployment_recipe() -> None:
    architecture = _read("docs/architecture.md")

    for phrase in (
        "docker volume create backchannel-data",
        "--mount type=volume,src=backchannel-data,dst=/data",
        "--env-file /etc/backchannel/runtime.env",
        "BACKCHANNEL_IDENTITY_HMAC_SECRET=<random-value-from-secret-manager>",
        "BACKCHANNEL_CORS_ORIGINS=https://demo.example.com",
        "one process and one worker",
        "proxy_buffering off",
        "proxy_read_timeout 75s",
        "HTTPS",
        "Public deployment remains unverified",
    ):
        assert phrase in architecture


def test_root_only_container_env_recipe_uses_privileged_docker_commands() -> None:
    architecture = _read("docs/architecture.md")
    recipe = architecture.split("```bash", maxsplit=1)[1].split("```", maxsplit=1)[0]

    for command in (
        "sudo docker build --tag backchannel:v0.3 .",
        "sudo docker volume create backchannel-data",
        "sudo docker run --detach --name backchannel",
    ):
        assert command in recipe
    assert "--env-file /etc/backchannel/runtime.env" in recipe
    assert "\ndocker " not in recipe


def test_fidelity_ledger_distinguishes_concepts_from_runtime_proof() -> None:
    ledger = _read("docs/design/README.md")

    expected_pairs = (
        ("desktop-consent.png", "desktop-consent.png"),
        ("desktop-completed.png", "desktop-completed.png"),
        ("desktop-declined.png", "desktop-cancelled.png"),
        ("mobile-consent.png", "mobile-consent.png"),
    )
    for final_name, concept_name in expected_pairs:
        assert (
            f"docs/assets/final/{final_name}` ↔ `docs/design/{concept_name}" in ledger
        )
    assert "mobile-completed.png` — no concept counterpart" in ledger
    assert "mobile-declined.png` — no concept counterpart" in ledger
    for classification in (
        "Matched",
        "Intentional",
        "Accessibility",
        "Remaining judge-impacting variance",
    ):
        assert classification in ledger
    for variance in (
        "light top bar",
        "navy",
        "vertical lifecycle",
        "horizontal",
        "portal dialog",
        "combined concept",
        "richer receipts",
        "source dimensions differ",
    ):
        assert variance in ledger
    assert "keyless local final build" in ledger
    assert "not live OpenAI" in ledger
    assert "not a public deployment" in ledger
    assert "not container proof" in ledger
    assert "not a release" in ledger
    assert "not real-provider execution" in ledger
