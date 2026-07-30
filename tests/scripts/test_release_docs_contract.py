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
    assert "718" not in current_evidence

    hosted_heading = "## Current Task32 hosted CI and packaged-container evidence"
    hosted_end_heading = "## Per-SHA proof matrix"
    assert hosted_heading in current_evidence
    assert hosted_end_heading in current_evidence
    current_before_hosted, current_from_hosted = current_evidence.split(
        hosted_heading,
        maxsplit=1,
    )
    hosted_evidence, current_after_hosted = current_from_hosted.split(
        hosted_end_heading,
        maxsplit=1,
    )
    normalized_hosted_evidence = " ".join(hosted_evidence.split())
    current_without_hosted = (
        current_before_hosted + hosted_end_heading + current_after_hosted
    )
    normalized_current_without_hosted = " ".join(current_without_hosted.split())

    hosted_scope_pattern = re.compile(
        r"\b(?:CI|GitHub\s+Actions|workflow|verify\s+job|"
        r"container(?:-smoke|\s+smoke|\s+job)?|packaged\s+Docker)\b",
        flags=re.IGNORECASE,
    )
    positive_word_pattern = re.compile(
        r"\b(?:pass|passed|passing|green|healthy|clean|cleanly|clear|"
        r"succeed|succeeded|succeeding|success|successful|successfully|verified)\b"
        r"|\b(?:completed|finished)\s+(?:cleanly|successfully)\b"
        r"|\bwithout\s+(?:any\s+)?(?:failures?|errors?)\b"
        r"|\b(?:no|zero)\s+(?:failures?|errors?)\b"
        r"|\b(?:failure|error)-free\b",
        flags=re.IGNORECASE,
    )
    current_evidence_sequences = (
        tuple(
            line.strip()
            for line in current_without_hosted.splitlines()
            if line.strip()
        ),
        tuple(
            sentence.strip()
            for sentence in re.split(
                r"(?<=[.!?])\s+",
                normalized_current_without_hosted,
            )
            if sentence.strip()
        ),
    )
    task32_pattern = re.compile(r"\bTask32\b", flags=re.IGNORECASE)
    for evidence_sequence in current_evidence_sequences:
        for index, evidence_unit in enumerate(evidence_sequence):
            if (
                task32_pattern.search(evidence_unit)
                and hosted_scope_pattern.search(evidence_unit)
            ):
                adjacent_window = " ".join(
                    evidence_sequence[max(0, index - 1) : index + 2]
                )
                assert not positive_word_pattern.search(
                    adjacent_window
                ), f"positive adjacent hosted-evidence claim: {adjacent_window.strip()}"

    assert manifest["sourceCommit"] in current_evidence
    current_activation = "9de054e5131ad3f610902d0a7bd4bd97c5968c7a"
    current_hosted_successor = "c6c60d4354eba7348aef3245d19e787661544fa7"
    current_run = "30588357735"
    current_jobs = ("91024909140", "91025281220")
    superseded_failed_attempt = (
        "30206582233",
        "89805619343",
        "89805788728",
    )
    assert current_activation in current_evidence
    assert runtime_input["digest"] in current_evidence
    for current_identifier in (
        manifest["sourceCommit"],
        "a8057a8…",
        current_activation,
        "9de054e…",
        current_hosted_successor,
        "c6c60d4…",
        current_run,
        *current_jobs,
        runtime_input["digest"],
        "a5fdc8a…",
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

    assert "`npm run test:e2e` reported `29 passed`" in current_evidence
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

    pre_capture_gate = current_bullet("Pre-capture Task32 gates on clean source")
    assert manifest["sourceCommit"] in pre_capture_gate
    for pre_capture_fact in (
        "focused SQLite permission matrix passed 23 tests",
        "independent security review passed with no P0-P3 findings",
    ):
        assert pre_capture_fact in pre_capture_gate
    for post_activation_only_fact in (
        "778 passed",
        "778 / 778 Python tests",
        "3 warnings",
        "Ruff",
        "strict MyPy",
        "`capture_manifest_valid`",
    ):
        assert post_activation_only_fact not in pre_capture_gate

    local_exact_gate = current_bullet(
        "Local exact clean gate on hosted validation successor"
    )
    assert current_hosted_successor in local_exact_gate
    for local_exact_fact in (
        "29 capture contracts",
        "`capture_manifest_valid`",
        "19 web test files / 345 tests",
        "TypeScript/Vite build",
        "Ruff",
        "strict MyPy over 35 source files",
        "778 / 778 Python tests with 3 warnings in 28.51s",
        "deterministic stub and local single-process production smokes",
        "OpenAPI verification",
        "history-aware secret scan all passed",
    ):
        assert local_exact_fact in local_exact_gate
    assert "focused SQLite permission matrix passed 23 tests" not in local_exact_gate

    review_gate = current_bullet("Independent Task32 visual review")
    assert "passed with no P0-P3 findings" in review_gate

    for observed_current_fact in (
        "29 capture contracts",
        "Google Chrome 150.0.7871.187",
    ):
        assert observed_current_fact in normalized_current_evidence

    sqlite_permission_heading = "## SQLite file-permission proof boundary"
    sqlite_permission_end_heading = "## JSON response media proof boundary"
    assert sqlite_permission_heading in current_evidence
    assert sqlite_permission_end_heading in current_evidence
    sqlite_permission_evidence = current_evidence.split(
        sqlite_permission_heading,
        maxsplit=1,
    )[1].split(
        sqlite_permission_end_heading,
        maxsplit=1,
    )[0]
    normalized_sqlite_permission_evidence = " ".join(
        sqlite_permission_evidence.split()
    )
    for required_sqlite_permission_fact in (
        "database and its `-journal`, `-wal`, and `-shm` sidecars as owner-only "
        "`0600` regular files",
        "Constructor-time initialization is the only lane allowed to harden",
        "connection and readiness checks are nonmutating and fail closed instead "
        "of repairing permission drift",
        "Every SQLite connection uses a URI with `mode=rw`",
        "Unsupported POSIX capabilities",
        "unexpected owner",
        "unprotected directory ancestry",
        "symlink or other non-regular file",
        "changed device/inode identity",
        "insecure database or sidecar permissions all fail closed",
        "packaged image creates `/data` as owner-only `0700`",
        "23 focused tests",
    ):
        assert required_sqlite_permission_fact in normalized_sqlite_permission_evidence

    json_media_heading = "## JSON response media proof boundary"
    json_media_end_heading = "## Terminal evidence retry proof boundary"
    assert json_media_heading in current_evidence
    assert json_media_end_heading in current_evidence
    json_media_evidence = current_evidence.split(
        json_media_heading,
        maxsplit=1,
    )[1].split(
        json_media_end_heading,
        maxsplit=1,
    )[0]
    normalized_json_media_evidence = " ".join(json_media_evidence.split())
    for required_json_media_fact in (
        "exactly one `Content-Type` field",
        "case-insensitive `application/json` media type",
        "valid no-comma token or quoted-string parameters",
        "missing, wrong, malformed, comma-joined, duplicate parameters, and "
        "split-quote values",
        "cancels the unlocked wrong-media response body without waiting",
        "typed IDs, fallback, retry metadata, and provenance remain inactive",
        "SSE behavior is unchanged",
        "ordinary `500` handling and the Task30 exact `503` retry contract remain "
        "preserved",
    ):
        assert required_json_media_fact in normalized_json_media_evidence

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
    actions_base = (
        "https://github.com/charlie2233/backchannel-agent-support/actions/runs"
    )
    assert observed_action_urls == [
        f"{actions_base}/{current_run}",
        f"{actions_base}/{current_run}/job/{current_jobs[0]}",
        f"{actions_base}/{current_run}/job/{current_jobs[1]}",
    ]
    hosted_successor_matches = re.findall(
        r"Hosted validation successor:\s+`([0-9a-f]{40})`",
        current_evidence,
    )
    assert hosted_successor_matches == [current_hosted_successor]
    expected_hosted_bullets = (
        f"- Hosted validation successor: `{current_hosted_successor}`",
        f"- GitHub Actions: [run {current_run}]({actions_base}/{current_run}).",
        (
            f"- Result: `verify` ([job {current_jobs[0]}]"
            f"({actions_base}/{current_run}/job/{current_jobs[0]})) "
            "reported `PASS`; `container-smoke` "
            f"([job {current_jobs[1]}]"
            f"({actions_base}/{current_run}/job/{current_jobs[1]})) "
            "reported `PASS`."
        ),
        "- Both current job annotation APIs returned `[]`.",
        (
            "- Hosted `verify` passed `capture_manifest_valid`, 19 web test files / "
            "345 tests, Ruff, strict MyPy over 35 source files, 778 Python tests "
            "with 1 warning in 43.35s, deterministic stub smoke, OpenAPI "
            "verification, and the history-aware secret scan."
        ),
        (
            "- Hosted `container-smoke` built the packaged image with "
            "`install -d -m 0700 -o backchannel -g backchannel /data`, confirmed "
            "runtime user `10001:10001`, and passed the offline network-none "
            "`deterministic-qa` and `deployed-readonly` profiles."
        ),
    )
    hosted_bullets = tuple(
        " ".join(match.group(0).split())
        for match in re.finditer(
            r"^- .*?(?=^- |\Z)",
            hosted_evidence,
            flags=re.MULTILINE | re.DOTALL,
        )
    )
    assert hosted_bullets == expected_hosted_bullets
    assert normalized_hosted_evidence == " ".join(expected_hosted_bullets)
    hosted_job_bindings = re.findall(
        r"`(verify|container-smoke)` "
        r"\(\[job (\d+)\]\((https://github\.com/[^)\s]+/job/\d+)\)\) "
        r"reported `(PASS)`",
        hosted_bullets[2],
    )
    assert hosted_job_bindings == [
        (
            "verify",
            current_jobs[0],
            f"{actions_base}/{current_run}/job/{current_jobs[0]}",
            "PASS",
        ),
        (
            "container-smoke",
            current_jobs[1],
            f"{actions_base}/{current_run}/job/{current_jobs[1]}",
            "PASS",
        ),
    ]
    for required_current_boundary in (
        "**Verified in GitHub CI** for the exact hosted and packaged-container lanes",
        "does not prove local Docker",
        "browser capture in CI",
        "public deployment or browser URL",
        "live OpenAI",
        "real provider execution",
        "container replacement or restart",
        "long-lived `/data` volume persistence",
        "target-host durability/networking",
        "abrupt host-loss or backup",
        "concurrent multi-container SQLite",
        "tag or release",
    ):
        assert required_current_boundary in normalized_current_evidence
    for forbidden_current_claim in (
        "No current Task32 GitHub Actions run or job has been observed.",
        "No current Task32 packaged container smoke was executed.",
        "reported `FAILURE`",
        "reported `SKIPPED`",
    ):
        assert forbidden_current_claim not in current_evidence
    for superseded_identifier in superseded_failed_attempt:
        assert superseded_identifier not in current_evidence
    assert "CI did not execute the browser capture" in current_evidence

    task31_capture = "c446f05adfb836539ddbaa74a41502034c910092"
    task31_activation = "b4c1bcbd70ff22ce3ac2b8a1de3828b7ce691afe"
    task31_digest = (
        "3bb9737a483fc90e50d5a6158bf0c61c1807e6510d22c9bf6d88e142d21ff5d9"
    )
    task31_pre_ci_successor = "62c37d6261257ea3800335257e43ae45ed839c47"
    task31_pre_ci_run = "30501109833"
    task31_pre_ci_jobs = ("90740755548", "90741063564")
    task31_final_docs_tip = "3abd059087e216fb4fed613b45927f27a3af627f"
    task31_final_run = "30501426777"
    task31_final_jobs = ("90741727313", "90742037347")
    task31_history_heading = (
        "### JSON-media capture and hosted baselines (superseded)"
    )
    task31_history_end_heading = (
        "### Terminal-retry capture and hosted baselines (superseded)"
    )
    assert task31_history_heading in historical_evidence
    assert task31_history_end_heading in historical_evidence
    task31_history = historical_evidence.split(
        task31_history_heading,
        maxsplit=1,
    )[1].split(
        task31_history_end_heading,
        maxsplit=1,
    )[0]
    normalized_task31_history = " ".join(task31_history.split())
    for exact_historical_fact in (
        "120-test client matrix",
        "19 web test files / 345 tests",
        "695 Python tests with 3 warnings in 179.85s",
        "Both pre-CI jobs passed and both pre-CI job annotation APIs returned `[]`",
        "Both final-tip jobs passed and both final-tip job annotation APIs returned `[]`",
    ):
        assert exact_historical_fact in normalized_task31_history
    task31_historical_action_urls = {
        f"{actions_base}/{task31_pre_ci_run}",
        *(
            f"{actions_base}/{task31_pre_ci_run}/job/{job}"
            for job in task31_pre_ci_jobs
        ),
        f"{actions_base}/{task31_final_run}",
        *(
            f"{actions_base}/{task31_final_run}/job/{job}"
            for job in task31_final_jobs
        ),
    }
    assert set(
        re.findall(
            r"https://github\.com/[^)\s]+/actions/runs/\d+(?:/job/\d+)?",
            task31_history,
        )
    ) == task31_historical_action_urls

    task30_capture = "96543fff62bb5d0a3c0f8a0464e9a07bf9c54568"
    task30_activation = "98dae414b3cdd36ee25d0dad3fe78257f3f4c135"
    task30_digest = (
        "e22c42085703fcdfaaa3f994334cc418a4e81e858187757edb5a33b8e2221e13"
    )
    task30_pre_ci_successor = "a3e3179bafb8050598cc5512d56e0c7661318c64"
    task30_pre_ci_run = "30499178838"
    task30_pre_ci_jobs = ("90734790003", "90735121608")
    task30_final_docs_tip = "9e19b12de072e55932d1b32e24987576284ac4f0"
    task30_final_run = "30499500934"
    task30_final_jobs = ("90735804461", "90736149754")
    task30_history_heading = (
        "### Terminal-retry capture and hosted baselines (superseded)"
    )
    task30_history_end_heading = (
        "### Async SQLite capture and hosted baseline (superseded)"
    )
    assert task30_history_heading in historical_evidence
    assert task30_history_end_heading in historical_evidence
    task30_history = historical_evidence.split(
        task30_history_heading,
        maxsplit=1,
    )[1].split(
        task30_history_end_heading,
        maxsplit=1,
    )[0]
    for exact_historical_fact in (
        "Both pre-CI jobs passed and both pre-CI job annotation APIs returned `[]`",
        "Both final-tip jobs passed and both final-tip job annotation APIs returned `[]`",
    ):
        assert exact_historical_fact in task30_history
    historical_action_urls = {
        f"{actions_base}/{task30_pre_ci_run}",
        *(f"{actions_base}/{task30_pre_ci_run}/job/{job}" for job in task30_pre_ci_jobs),
        f"{actions_base}/{task30_final_run}",
        *(f"{actions_base}/{task30_final_run}/job/{job}" for job in task30_final_jobs),
    }
    assert set(
        re.findall(
            r"https://github\.com/[^)\s]+/actions/runs/\d+(?:/job/\d+)?",
            task30_history,
        )
    ) == historical_action_urls
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
        task31_capture,
        "c446f05…",
        task31_activation,
        "b4c1bcb…",
        task31_digest,
        "3bb9737…",
        task31_pre_ci_successor,
        "62c37d6…",
        task31_pre_ci_run,
        *task31_pre_ci_jobs,
        task31_final_docs_tip,
        "3abd059…",
        task31_final_run,
        *task31_final_jobs,
        task30_capture,
        "96543ff…",
        task30_activation,
        "98dae41…",
        task30_digest,
        "e22c420…",
        task30_pre_ci_successor,
        "a3e3179…",
        task30_pre_ci_run,
        *task30_pre_ci_jobs,
        task30_final_docs_tip,
        "9e19b12…",
        task30_final_run,
        *task30_final_jobs,
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
        task31_capture,
        "c446f05…",
        task31_activation,
        "b4c1bcb…",
        task31_digest,
        "3bb9737…",
        task31_pre_ci_successor,
        "62c37d6…",
        task31_pre_ci_run,
        *task31_pre_ci_jobs,
        task31_final_docs_tip,
        "3abd059…",
        task31_final_run,
        *task31_final_jobs,
        task30_capture,
        "96543ff…",
        task30_activation,
        "98dae41…",
        task30_digest,
        "e22c420…",
        task30_pre_ci_successor,
        "a3e3179…",
        task30_pre_ci_run,
        *task30_pre_ci_jobs,
        task30_final_docs_tip,
        "9e19b12…",
        task30_final_run,
        *task30_final_jobs,
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
        "c446f05adfb836539ddbaa74a41502034c910092",
        "c446f05…",
        "b4c1bcbd70ff22ce3ac2b8a1de3828b7ce691afe",
        "b4c1bcb…",
        "3bb9737a483fc90e50d5a6158bf0c61c1807e6510d22c9bf6d88e142d21ff5d9",
        "3bb9737…",
        "62c37d6261257ea3800335257e43ae45ed839c47",
        "62c37d6…",
        "30501109833",
        "90740755548",
        "90741063564",
        "3abd059087e216fb4fed613b45927f27a3af627f",
        "3abd059…",
        "30501426777",
        "90741727313",
        "90742037347",
        "96543fff62bb5d0a3c0f8a0464e9a07bf9c54568",
        "96543ff…",
        "98dae414b3cdd36ee25d0dad3fe78257f3f4c135",
        "98dae41…",
        "e22c42085703fcdfaaa3f994334cc418a4e81e858187757edb5a33b8e2221e13",
        "e22c420…",
        "a3e3179bafb8050598cc5512d56e0c7661318c64",
        "a3e3179…",
        "30499178838",
        "90734790003",
        "90735121608",
        "9e19b12de072e55932d1b32e24987576284ac4f0",
        "9e19b12…",
        "30499500934",
        "90735804461",
        "90736149754",
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
        "a8057a8e0c29bdcc95e35949819c64005e5ee064",
        "a8057a8…",
        "9de054e5131ad3f610902d0a7bd4bd97c5968c7a",
        "9de054e…",
        "c6c60d4354eba7348aef3245d19e787661544fa7",
        "c6c60d4…",
        "30588357735",
        "91024909140",
        "91025281220",
        "a5fdc8adc9788f181ace5f4de9cce7974344af02312cef2caa9e123193744503",
        "a5fdc8a…",
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


def test_validation_requires_exact_current_hosted_run() -> None:
    validation = _read("docs/validation.md")
    polluted_validation = validation.replace(
        "30588357735",
        "99999999999",
    )

    with pytest.raises(AssertionError):
        _assert_validation_matches_current_capture(polluted_validation)


@pytest.mark.parametrize(
    ("original", "replacement"),
    (
        (
            "c6c60d4354eba7348aef3245d19e787661544fa7",
            "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        ),
        ("91024909140", "99999999998"),
        ("91025281220", "99999999997"),
        ("reported `PASS`", "reported `FAILURE`"),
        (
            "Both current job annotation APIs returned `[]`",
            "Current job annotations were not inspected",
        ),
        ("778 Python tests with 1 warning in 43.35s", "777 Python tests"),
        (
            "`install -d -m 0700 -o backchannel -g backchannel /data`",
            "`install -d /data`",
        ),
        ("runtime user `10001:10001`", "runtime user was not inspected"),
        (
            "offline network-none `deterministic-qa` and `deployed-readonly` profiles",
            "one online profile",
        ),
    ),
)
def test_validation_rejects_current_hosted_fact_mutation(
    original: str,
    replacement: str,
) -> None:
    validation = _read("docs/validation.md")
    hosted_heading = "## Current Task32 hosted CI and packaged-container evidence"
    hosted_end_heading = "## Per-SHA proof matrix"
    hosted_evidence = validation.split(hosted_heading, maxsplit=1)[1].split(
        hosted_end_heading,
        maxsplit=1,
    )[0]
    original_pattern = re.compile(re.escape(original).replace(r"\ ", r"\s+"))
    assert original_pattern.search(hosted_evidence)
    polluted_hosted_evidence = original_pattern.sub(
        replacement,
        hosted_evidence,
        count=1,
    )
    polluted_validation = validation.replace(
        hosted_evidence,
        polluted_hosted_evidence,
        1,
    )

    with pytest.raises(AssertionError):
        _assert_validation_matches_current_capture(polluted_validation)


@pytest.mark.parametrize(
    "unsupported_positive_claim",
    (
        "Public deployment passed.",
        "Live OpenAI passed.",
        "Container restart and replacement persistence passed.",
    ),
)
def test_validation_rejects_extra_positive_claim_in_current_hosted_section(
    unsupported_positive_claim: str,
) -> None:
    validation = _read("docs/validation.md")
    hosted_end_heading = "## Per-SHA proof matrix"
    polluted_validation = validation.replace(
        hosted_end_heading,
        f"- {unsupported_positive_claim}\n\n{hosted_end_heading}",
        1,
    )

    with pytest.raises(AssertionError):
        _assert_validation_matches_current_capture(polluted_validation)


def test_validation_rejects_swapped_current_hosted_job_bindings() -> None:
    validation = _read("docs/validation.md")
    hosted_heading = "## Current Task32 hosted CI and packaged-container evidence"
    hosted_end_heading = "## Per-SHA proof matrix"
    hosted_evidence = validation.split(hosted_heading, maxsplit=1)[1].split(
        hosted_end_heading,
        maxsplit=1,
    )[0]
    verify_job = "91024909140"
    container_job = "91025281220"
    swapped_hosted_evidence = (
        hosted_evidence.replace(verify_job, "__VERIFY_JOB__")
        .replace(container_job, verify_job)
        .replace("__VERIFY_JOB__", container_job)
    )
    assert swapped_hosted_evidence != hosted_evidence
    polluted_validation = validation.replace(
        hosted_evidence,
        swapped_hosted_evidence,
        1,
    )

    with pytest.raises(AssertionError):
        _assert_validation_matches_current_capture(polluted_validation)


@pytest.mark.parametrize(
    "unexpected_positive_claim",
    (
        "Current Task32 CI passed.",
        "Current Task32 CI. Passed.",
        "Current Task32 CI.\nPassed.",
        "Passed. Current Task32 CI.",
        "Current Task32 container job passed.",
        "Current Task32 verify job passed.",
        "Task32 GitHub Actions passed successfully.",
        "The current Task32 GitHub workflow passed.",
        "Task32 container smoke. Result: verified.",
        "Task32 container smoke.\nResult: verified.",
        "Status: verified. Task32 container smoke.",
        "Healthy. Current Task32 workflow.",
        "Completed cleanly. Current Task32 packaged Docker.",
        "No failures. Current Task32 verify job.",
        "Current Task32 CI completed cleanly.",
        "Current Task32 CI is passing.",
        "Current Task32 workflow is healthy.",
        "Current Task32 container smoke completed without failures.",
        "Current Task32 CI has no failures.",
        "Current Task32 CI has zero failures.",
        "Current Task32 CI is error-free.",
        (
            "- No current Task32 GitHub Actions run or job has been observed.\n"
            "Passed."
        ),
        (
            "Passed.\n"
            "- No current Task32 GitHub Actions run or job has been observed."
        ),
        "The full Python suite passed all 718 tests.",
        "Canonical backend: 718 passed.",
        "Current Task32 GitHub Actions passed successfully.",
        "Current Task32 GitHub Actions run is green.",
        "Current Task32 hosted CI succeeded.",
        "Current Task32 packaged container smoke passed successfully.",
        "Current Task32 container-smoke job is green.",
        "Current Task32 container smoke succeeded.",
        "Current Task32 workflow green.",
        "Current Task32 packaged Docker successful.",
        "Current Task32 container-smoke verified.",
        "Full canonical Python gate passed 718 tests.",
        "Full canonical gate succeeded with 718 tests.",
        "Final full canonical gate passed 718 tests.",
        "Current full canonical Python gate passed 718 tests.",
    ),
)
def test_validation_rejects_generic_current_positive_claims(
    unexpected_positive_claim: str,
) -> None:
    validation = _read("docs/validation.md")
    historical_heading = "## Superseded historical hosted baseline"
    polluted_validation = validation.replace(
        historical_heading,
        f"{unexpected_positive_claim}\n\n{historical_heading}",
        1,
    )

    with pytest.raises(AssertionError):
        _assert_validation_matches_current_capture(polluted_validation)


@pytest.mark.parametrize(
    ("bullet_prefix", "original", "replacement"),
    (
        (
            "Pre-capture Task32 gates on clean source",
            "P0-P3 findings.",
            "P0-P3 findings and 778 passed.",
        ),
        (
            "Local exact clean gate on hosted validation successor",
            "778 / 778 Python tests with 3 warnings in 28.51s",
            "777 / 778 Python tests with 3 warnings in 28.51s",
        ),
        (
            "Local exact clean gate on hosted validation successor",
            "c6c60d4354eba7348aef3245d19e787661544fa7",
            "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        ),
        (
            "Local exact clean gate on hosted validation successor",
            "deterministic stub and local single-process production smokes",
            "deterministic stub smoke only",
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
        ("owner-only `0600` regular files", "group-readable `0640` files"),
        (
            "Constructor-time initialization is the only lane allowed to harden",
            "Runtime readiness may also harden",
        ),
        (
            "connection and readiness checks are nonmutating and fail closed instead "
            "of repairing permission drift",
            "runtime checks repair permission drift",
        ),
        ("URI with `mode=rw`", "URI with `mode=rwc`"),
        ("Unsupported POSIX capabilities", "Unsupported capabilities are ignored"),
        ("unexpected owner", "any owner"),
        ("unprotected directory ancestry", "untrusted directory ancestry"),
        ("changed device/inode identity", "changed pathname only"),
        (
            "insecure database or sidecar permissions all fail closed",
            "insecure sidecars remain usable",
        ),
        ("owner-only `0700`", "group-writable `0770`"),
        ("23 focused tests", "an unspecified test count"),
    ),
)
def test_validation_rejects_sqlite_permission_contract_mutation(
    original: str,
    replacement: str,
) -> None:
    validation = _read("docs/validation.md")
    sqlite_heading = "## SQLite file-permission proof boundary"
    sqlite_end_heading = "## JSON response media proof boundary"
    sqlite_evidence = validation.split(sqlite_heading, maxsplit=1)[1].split(
        sqlite_end_heading,
        maxsplit=1,
    )[0]
    original_pattern = re.compile(re.escape(original).replace(r"\ ", r"\s+"))
    assert original_pattern.search(sqlite_evidence)
    polluted_sqlite_evidence = original_pattern.sub(
        replacement,
        sqlite_evidence,
        count=1,
    )
    polluted_validation = validation.replace(
        sqlite_evidence,
        polluted_sqlite_evidence,
        1,
    )

    with pytest.raises(AssertionError):
        _assert_validation_matches_current_capture(polluted_validation)


@pytest.mark.parametrize(
    ("original", "replacement"),
    (
        ("exactly one `Content-Type` field", "multiple `Content-Type` fields"),
        (
            "case-insensitive `application/json` media type",
            "case-sensitive `application/json` media type",
        ),
        (
            "valid no-comma token or quoted-string parameters",
            "arbitrary comma-joined parameters",
        ),
        (
            "missing, wrong, malformed, comma-joined, duplicate parameters, and "
            "split-quote values",
            "only wrong media types",
        ),
        (
            "cancels the unlocked wrong-media response body without waiting",
            "waits for the wrong-media response body",
        ),
        (
            "typed IDs, fallback, retry metadata, and provenance remain inactive",
            "typed IDs and retry metadata activate",
        ),
        ("SSE behavior is unchanged", "SSE behavior also uses this parser"),
        (
            "ordinary `500` handling and the Task30 exact `503` retry contract remain "
            "preserved",
            "ordinary errors share the new media failure",
        ),
    ),
)
def test_validation_rejects_json_media_contract_mutation(
    original: str,
    replacement: str,
) -> None:
    validation = _read("docs/validation.md")
    media_heading = "## JSON response media proof boundary"
    media_end_heading = "## Terminal evidence retry proof boundary"
    media_evidence = validation.split(media_heading, maxsplit=1)[1].split(
        media_end_heading,
        maxsplit=1,
    )[0]
    original_pattern = re.compile(re.escape(original).replace(r"\ ", r"\s+"))
    assert original_pattern.search(media_evidence)
    polluted_media_evidence = original_pattern.sub(
        replacement,
        media_evidence,
        count=1,
    )
    polluted_validation = validation.replace(
        media_evidence,
        polluted_media_evidence,
        1,
    )

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
    assert "## SQLite file-permission proof boundary" in validation
    assert "## JSON response media proof boundary" in validation
    assert "## Superseded historical hosted baseline" in validation
    assert "### JSON-media capture and hosted baselines (superseded)" in validation
    assert (
        "### Terminal-retry capture and hosted baselines (superseded)" in validation
    )
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
