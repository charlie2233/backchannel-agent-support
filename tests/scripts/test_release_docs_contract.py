from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

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


def _assert_validation_matches_current_capture(validation: str) -> None:
    manifest = json.loads(_read("docs/assets/final/manifest.json"))
    runtime_input = manifest["runtimeInput"]
    browser = manifest["browser"]
    environment = manifest["environment"]
    artifacts = manifest["artifacts"]

    historical_heading = "## Superseded historical hosted baseline"
    assert historical_heading in validation
    current_evidence, historical_evidence = validation.split(
        historical_heading,
        maxsplit=1,
    )
    normalized_current_evidence = " ".join(current_evidence.split())

    assert manifest["sourceCommit"] in current_evidence
    current_activation = "98dae414b3cdd36ee25d0dad3fe78257f3f4c135"
    superseded_failed_attempt = (
        "30206582233",
        "89805619343",
        "89805788728",
    )
    assert current_activation in current_evidence
    assert runtime_input["digest"] in current_evidence
    for current_identifier in (
        manifest["sourceCommit"],
        "96543ff…",
        current_activation,
        "98dae41…",
        runtime_input["digest"],
        "e22c420…",
    ):
        assert current_identifier not in historical_evidence
    assert f"{len(runtime_input['paths'])} runtime paths" in current_evidence
    assert f"{browser['name']} {browser['version']}" in current_evidence
    browser_mode = "headless" if browser["headless"] else "headed"
    assert f"`{browser['channel']}` channel in {browser_mode} mode" in current_evidence
    assert f"capture profile `{manifest['captureProfile']}`" in current_evidence
    environment_summary = " / ".join(
        f"`{environment[key]}`"
        for key in ("locale", "timezone", "reducedMotion", "colorScheme")
    )
    assert environment_summary in current_evidence
    for width, height in {
        (artifact["width"], artifact["height"]) for artifact in artifacts
    }:
        assert f"{width}×{height}" in current_evidence

    assert "`npm run capture:judge` reported `29 passed`" in current_evidence
    assert (
        "Five PNGs changed and `mobile-consent.png` remained byte-identical"
        in current_evidence
    )

    def current_bullet(prefix: str) -> str:
        match = re.search(
            rf"^- {re.escape(prefix)}.*?(?=^- |\n## |\Z)",
            current_evidence,
            flags=re.MULTILINE | re.DOTALL,
        )
        assert match is not None
        return " ".join(match.group(0).split())

    pre_capture_gate = current_bullet("Pre-capture gates on clean source")
    assert manifest["sourceCommit"] in pre_capture_gate
    for pre_capture_fact in (
        "targeted Task30 matrix passed 142 tests",
        "19 web test files / 315 tests",
        "TypeScript/Vite",
    ):
        assert pre_capture_fact in pre_capture_gate
    for post_activation_only_fact in (
        "671 Python tests",
        "3 warnings",
        "Ruff",
        "strict MyPy",
        "`npm run check`",
        "`capture_manifest_valid`",
    ):
        assert post_activation_only_fact not in pre_capture_gate

    post_activation_gate = current_bullet("After activation")
    assert current_activation in post_activation_gate
    for post_activation_fact in (
        "current pre-CI check on the identical working tree",
        "`npm run check`",
        "29 capture contracts",
        "`capture_manifest_valid`",
        "19 web test files / 315 tests",
        "TypeScript/Vite build",
        "Ruff",
        "strict MyPy over 35 source files",
        "671 Python tests with 3 warnings in 106.82s",
        "one Starlette TestClient deprecation and two multiprocessing fork warnings",
    ):
        assert post_activation_fact in post_activation_gate
    assert "targeted Task30 matrix passed 142 tests" not in post_activation_gate

    separate_current_gate = current_bullet("Separate current pre-CI gates")
    for separate_gate_fact in (
        "deterministic stub and local single-process production smokes",
        "OpenAPI freshness",
        "history-aware secret scan",
        "diff checks",
        "focused release-doc contract passed 88/88",
        "build, static/API, SSE, decision, session, and bounded shutdown",
        "independent specification and quality reviews passed with no P0-P2",
    ):
        assert separate_gate_fact in separate_current_gate

    for observed_current_fact in (
        "29 capture contracts",
        "in-app Browser capture attempt failed closed as unavailable",
    ):
        assert observed_current_fact in normalized_current_evidence

    terminal_heading = "## Terminal evidence retry proof boundary"
    terminal_end_heading = "## Async SQLite proof boundary"
    assert terminal_heading in current_evidence
    assert terminal_end_heading in current_evidence
    terminal_evidence = current_evidence.split(terminal_heading, maxsplit=1)[1].split(
        terminal_end_heading,
        maxsplit=1,
    )[0]
    normalized_terminal_evidence = " ".join(terminal_evidence.split())
    for required_terminal_fact in (
        "HTTP `503`, code `internal_error`, `recoveryId` equal to the requested "
        "recovery UUID, `retryAfterSeconds: 1`, `fallback: null`, a matching "
        "`Retry-After: 1` header, and `application/json` media",
        "Null, foreign, or substituted recovery IDs",
        "malformed status, header, body, or media fail closed",
        "one initial authoritative read pair plus exactly three one-second "
        "automatic retries",
        "one accessible manual retry begins a fresh equally bounded "
        "initial-plus-three cycle",
        "already verified snapshot, receipt, event, and cursor state remains visible",
        "No POST, create, decision, reset, or fallback request is issued by either "
        "automatic or manual terminal evidence retry",
    ):
        assert required_terminal_fact in normalized_terminal_evidence
    observed_action_urls = re.findall(
        r"https://github\.com/[^)\s]+/actions/runs/\d+(?:/job/\d+)?",
        current_evidence,
    )
    assert observed_action_urls == []
    assert re.findall(r"/actions/runs/(\d+)", current_evidence) == []
    assert re.findall(r"/job/(\d+)", current_evidence) == []
    hosted_successor_matches = re.findall(
        r"Hosted validation successor:\s+`([0-9a-f]{40})`",
        current_evidence,
    )
    assert hosted_successor_matches == []
    for required_current_boundary in (
        "No green current Task30 GitHub Actions run or job has been observed.",
        "No current Task30 packaged container smoke was executed.",
        "**Unverified** for this source/capture checkpoint",
    ):
        assert required_current_boundary in current_evidence
    for forbidden_current_claim in (
        "Both job annotation APIs returned `[]`",
        "reported `PASS`",
        "reported `FAILURE`",
        "reported `SKIPPED`",
        "Hosted validation successor:",
    ):
        assert forbidden_current_claim not in current_evidence
    for superseded_identifier in superseded_failed_attempt:
        assert superseded_identifier not in current_evidence
    for unproved_current_boundary in (
        "does not prove local Docker",
        "public deployment or browser URL",
        "live OpenAI",
        "real provider execution",
        "container replacement or restart",
        "long-lived `/data` volume persistence",
        "abrupt host-loss or backup",
        "concurrent multi-container SQLite",
        "tag or release",
    ):
        assert unproved_current_boundary in normalized_current_evidence
    assert "CI did not execute the browser capture" in current_evidence

    task29_capture = "8d0a896c753c4c60894301a20a3866bbbfa1e76f"
    task29_activation = "cca97a8e75d52a26889d3bbb66740756041d9caf"
    task29_successor = "e188202a4b0612a66503fda2b6ee1a3c89ec7b65"
    task29_digest = (
        "a1e0ecaf69926044419e29c7359102188c80550834ddb9351021aae411705054"
    )
    task29_run = "30208188300"
    task29_jobs = ("89809811619", "89809998273")
    request_boundary_capture = "8823d29d7de93d44f4843a2fa4db1adec4e452bd"
    request_boundary_activation = "55af1e6d68f11542b1c5cc5e3465b87dc158ec08"
    request_boundary_successor = "117e4ebe40efea36f89bbb737143de9c918f938f"
    request_boundary_digest = (
        "4ad336eaf2d304906e939fc0eb433c6633519e73d9855207ae72c4daea42eed2"
    )
    request_boundary_run = "30182741263"
    request_boundary_jobs = ("89742014836", "89742159778")
    immediate_prior_capture = "742e3caf2af5a9cce3cd8de242cf113424e8528f"
    immediate_prior_activation = "4a317e563c8d45bc45f676e465b01780b2b0be78"
    immediate_prior_successor = "1289b773abe92bb2f5842f77e3c7f50c432352f4"
    immediate_prior_digest = (
        "1bde9788d551e5f76b895b977ad69291c6b1f44d26095b242d531f1bf289418c"
    )
    immediate_prior_run = "30174822102"
    immediate_prior_jobs = ("89721793096", "89721989116")
    prior_capture = "7d9128a8171ddb4978d8f7b0debb9effce997e27"
    prior_activation = "576ba5e3dfd3d13b9f797c1c516c2352c4e40688"
    prior_run = "30162644778"
    prior_jobs = ("89690352979", "89690513333")
    older_activation = "b57868005a3fe0869136f54472ee0098035a9099"
    older_run = "30140554792"
    older_jobs = ("89632837699", "89633002245")
    for historical_identifier in (
        "85e1e8ec9147242adca311c4ba10ea8c1c3008dc",
        "85e1e8e…",
        task29_capture,
        "8d0a896…",
        task29_activation,
        "cca97a8…",
        task29_successor,
        "e188202…",
        task29_digest,
        "a1e0eca…",
        task29_run,
        *task29_jobs,
        request_boundary_capture,
        "8823d29…",
        request_boundary_activation,
        "55af1e6…",
        request_boundary_successor,
        "117e4eb…",
        request_boundary_digest,
        request_boundary_run,
        *request_boundary_jobs,
        immediate_prior_capture,
        "742e3ca…",
        immediate_prior_activation,
        "4a317e5…",
        immediate_prior_successor,
        "1289b77…",
        immediate_prior_digest,
        immediate_prior_run,
        *immediate_prior_jobs,
        prior_capture,
        "7d9128a…",
        prior_activation,
        "576ba5e…",
        prior_run,
        *prior_jobs,
        older_activation,
        "b578680…",
        older_run,
        *older_jobs,
    ):
        assert historical_identifier not in current_evidence
    for required_historical_identifier in (
        task29_capture,
        "8d0a896…",
        task29_activation,
        "cca97a8…",
        task29_successor,
        "e188202…",
        task29_digest,
        "a1e0eca…",
        task29_run,
        *task29_jobs,
        request_boundary_capture,
        "8823d29…",
        request_boundary_activation,
        "55af1e6…",
        request_boundary_successor,
        "117e4eb…",
        request_boundary_digest,
        request_boundary_run,
        *request_boundary_jobs,
        immediate_prior_capture,
        "742e3ca…",
        immediate_prior_activation,
        "4a317e5…",
        immediate_prior_successor,
        "1289b77…",
        immediate_prior_digest,
        immediate_prior_run,
        *immediate_prior_jobs,
        prior_capture,
        "7d9128a…",
        prior_activation,
        "576ba5e…",
        prior_run,
        *prior_jobs,
        older_activation,
        older_run,
        *older_jobs,
    ):
        assert required_historical_identifier in historical_evidence
    for obsolete in (
        "85e1e8ec9147242adca311c4ba10ea8c1c3008dc",
        "eeb6b87ce35ad04cb4c53d048c39fcbc8caf33bb62e142526e0f9aba544afb30",
        "81 runtime paths",
    ):
        assert obsolete not in validation


@pytest.mark.parametrize(
    "superseded_identifier",
    (
        "85e1e8ec9147242adca311c4ba10ea8c1c3008dc",
        "85e1e8e…",
        "8d0a896c753c4c60894301a20a3866bbbfa1e76f",
        "8d0a896…",
        "cca97a8e75d52a26889d3bbb66740756041d9caf",
        "cca97a8…",
        "e188202a4b0612a66503fda2b6ee1a3c89ec7b65",
        "e188202…",
        "a1e0ecaf69926044419e29c7359102188c80550834ddb9351021aae411705054",
        "a1e0eca…",
        "30208188300",
        "89809811619",
        "89809998273",
        "8823d29d7de93d44f4843a2fa4db1adec4e452bd",
        "8823d29…",
        "55af1e6d68f11542b1c5cc5e3465b87dc158ec08",
        "55af1e6…",
        "117e4ebe40efea36f89bbb737143de9c918f938f",
        "117e4eb…",
        "4ad336eaf2d304906e939fc0eb433c6633519e73d9855207ae72c4daea42eed2",
        "30182741263",
        "89742014836",
        "89742159778",
        "30206582233",
        "89805619343",
        "89805788728",
        "742e3caf2af5a9cce3cd8de242cf113424e8528f",
        "742e3ca…",
        "4a317e563c8d45bc45f676e465b01780b2b0be78",
        "4a317e5…",
        "1289b773abe92bb2f5842f77e3c7f50c432352f4",
        "1289b77…",
        "1bde9788d551e5f76b895b977ad69291c6b1f44d26095b242d531f1bf289418c",
        "30174822102",
        "89721793096",
        "89721989116",
        "7d9128a8171ddb4978d8f7b0debb9effce997e27",
        "7d9128a…",
        "576ba5e3dfd3d13b9f797c1c516c2352c4e40688",
        "576ba5e…",
        "30162644778",
        "89690352979",
        "89690513333",
        "b57868005a3fe0869136f54472ee0098035a9099",
        "b578680…",
        "30140554792",
        "89632837699",
        "89633002245",
    ),
)
def test_validation_rejects_superseded_provenance_in_current_evidence(
    superseded_identifier: str,
) -> None:
    validation = _read("docs/validation.md")
    historical_heading = "## Superseded historical hosted baseline"
    polluted_validation = validation.replace(
        historical_heading,
        f"Injected stale provenance: {superseded_identifier}\n\n{historical_heading}",
        1,
    )

    with pytest.raises(AssertionError):
        _assert_validation_matches_current_capture(polluted_validation)


@pytest.mark.parametrize(
    "current_identifier",
    (
        "96543fff62bb5d0a3c0f8a0464e9a07bf9c54568",
        "96543ff…",
        "98dae414b3cdd36ee25d0dad3fe78257f3f4c135",
        "98dae41…",
        "e22c42085703fcdfaaa3f994334cc418a4e81e858187757edb5a33b8e2221e13",
        "e22c420…",
    ),
)
def test_validation_rejects_current_capture_provenance_in_historical_evidence(
    current_identifier: str,
) -> None:
    validation = _read("docs/validation.md")
    historical_heading = "## Superseded historical hosted baseline"
    polluted_validation = validation.replace(
        historical_heading,
        f"{historical_heading}\n\nInjected current provenance: {current_identifier}",
        1,
    )

    with pytest.raises(AssertionError):
        _assert_validation_matches_current_capture(polluted_validation)


@pytest.mark.parametrize(
    "unexpected_current_claim",
    (
        "- Hosted validation successor:\n  `deadbeefdeadbeefdeadbeefdeadbeefdeadbeef`",
        (
            "- GitHub Actions:\n"
            "  [run 999999](https://github.com/example/repo/actions/runs/999999)"
        ),
        (
            "- Result: `verify`\n"
            "  ([job 999998](https://github.com/example/repo/actions/runs/"
            "999999/job/999998)) reported `PASS`."
        ),
        (
            "- GitHub Actions:\n"
            "  [run 999999]"
            "(https://github.com/example/repo/actions/runs/999999)"
        ),
        (
            "- Result: `verify`\n"
            "  ([job 999998](https://github.com/example/repo/actions/runs/"
            "999999/job/999998)) reported `PASS`."
        ),
        "- Result: `verify` reported `FAILURE`; `container-smoke` reported `SKIPPED`.",
    ),
)
def test_validation_rejects_unexpected_current_hosted_provenance(
    unexpected_current_claim: str,
) -> None:
    validation = _read("docs/validation.md")
    historical_heading = "## Superseded historical hosted baseline"
    polluted_validation = validation.replace(
        historical_heading,
        f"{unexpected_current_claim}\n\n{historical_heading}",
        1,
    )

    with pytest.raises(AssertionError):
        _assert_validation_matches_current_capture(polluted_validation)


def test_validation_requires_current_hosted_lane_to_remain_unverified_pre_ci() -> None:
    validation = _read("docs/validation.md")
    polluted_validation = validation.replace(
        "No green current Task30 GitHub Actions run or job has been observed.",
        "A green current Task30 GitHub Actions run has been observed.",
    )

    with pytest.raises(AssertionError):
        _assert_validation_matches_current_capture(polluted_validation)


@pytest.mark.parametrize(
    ("bullet_prefix", "original", "replacement"),
    (
        (
            "Pre-capture gates on clean source",
            "TypeScript/Vite.",
            "TypeScript/Vite and 671 Python tests with 3 warnings.",
        ),
        (
            "After activation",
            "671 Python tests with 3 warnings in 106.82s",
            "an unspecified Python suite",
        ),
        (
            "After activation",
            "98dae414b3cdd36ee25d0dad3fe78257f3f4c135",
            "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        ),
    ),
)
def test_validation_rejects_evidence_timing_mutation(
    bullet_prefix: str,
    original: str,
    replacement: str,
) -> None:
    validation = _read("docs/validation.md")
    bullet_match = re.search(
        rf"^- {re.escape(bullet_prefix)}.*?(?=^- |\n## |\Z)",
        validation,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert bullet_match is not None
    bullet = bullet_match.group(0)
    original_pattern = re.compile(re.escape(original).replace(r"\ ", r"\s+"))
    assert original_pattern.search(bullet)
    polluted_bullet = original_pattern.sub(replacement, bullet, count=1)
    polluted_validation = validation.replace(bullet, polluted_bullet, 1)

    with pytest.raises(AssertionError):
        _assert_validation_matches_current_capture(polluted_validation)


@pytest.mark.parametrize(
    ("original", "replacement"),
    (
        ("HTTP `503`", "HTTP `502`"),
        ("code `internal_error`", "code `rate_limited`"),
        (
            "`recoveryId` equal to the requested recovery UUID",
            "`recoveryId` equal to null",
        ),
        ("`retryAfterSeconds: 1`", "`retryAfterSeconds: 2`"),
        ("`fallback: null`", "`fallback: {}`"),
        ("`Retry-After: 1`", "`Retry-After: 2`"),
        ("`application/json` media", "`text/plain` media"),
        ("exactly three", "exactly four"),
        ("one-second automatic retries", "immediate automatic retries"),
        ("one accessible manual retry", "two manual retries"),
        (
            "fresh equally bounded initial-plus-three cycle",
            "fresh unbounded cycle",
        ),
        (
            "already verified snapshot, receipt, event, and cursor state remains visible",
            "verified terminal state is cleared",
        ),
        (
            "No POST, create, decision, reset, or fallback request",
            "A create POST or fallback request",
        ),
    ),
)
def test_validation_rejects_terminal_retry_contract_mutation(
    original: str,
    replacement: str,
) -> None:
    validation = _read("docs/validation.md")
    terminal_heading = "## Terminal evidence retry proof boundary"
    terminal_end_heading = "## Async SQLite proof boundary"
    terminal_evidence = validation.split(terminal_heading, maxsplit=1)[1].split(
        terminal_end_heading,
        maxsplit=1,
    )[0]
    original_pattern = re.compile(re.escape(original).replace(r"\ ", r"\s+"))
    assert original_pattern.search(terminal_evidence)
    polluted_terminal_evidence = original_pattern.sub(
        replacement,
        terminal_evidence,
        count=1,
    )
    polluted_validation = validation.replace(
        terminal_evidence,
        polluted_terminal_evidence,
        1,
    )

    with pytest.raises(AssertionError):
        _assert_validation_matches_current_capture(polluted_validation)


def test_package_wires_capture_contract_and_manifest_verifier_into_check() -> None:
    package = json.loads(_read("package.json"))
    scripts = package["scripts"]
    workflow = _read(".github/workflows/ci.yml")

    assert package["devDependencies"]["playwright-core"] == "1.61.1"
    assert scripts["test:capture-contract"] == (
        "node --test e2e/judge-flow.test.mjs e2e/capture-manifest.test.mjs"
    )
    assert scripts["capture:verify"] == "node e2e/capture-manifest.mjs"
    assert scripts["pretest:e2e"] == "npm run build"
    assert scripts["test:e2e"] == (
        "npm run test:capture-contract && node e2e/judge-flow.mjs"
    )
    assert scripts["capture:judge"] == "npm run test:e2e"
    assert scripts["check"] == (
        "npm run test:capture-contract && npm run capture:verify && "
        "npm --workspace web run check && uv run ruff check server scripts tests && "
        "uv run mypy server scripts/start.py scripts/docker_smoke.py "
        "scripts/export_openapi.py scripts/secret_scan.py && uv run pytest -q"
    )
    assert (
        "      - name: Run canonical checks\n"
        "        run: npm run check\n"
    ) in workflow

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
    assert "## Current capture and evidence activation" in validation
    assert "## Superseded historical hosted baseline" in validation
    assert "### Async SQLite capture and hosted baseline (superseded)" in validation
    assert "### Immediate prior capture and hosted baseline (superseded)" in validation
    assert "### Prior capture and hosted baseline (superseded)" in validation
    assert "### Older hosted baseline (superseded)" in validation
    assert (
        "Local exact-tree gate before activation and the hosted canonical gate covered"
        not in validation
    )
    assert "[`docs/assets/final/manifest.json`](assets/final/manifest.json)" in validation
    assert "CI did not execute the browser capture" in validation
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
    _assert_validation_matches_current_capture(validation)


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
