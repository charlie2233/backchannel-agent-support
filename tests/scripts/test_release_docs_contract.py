from __future__ import annotations

import ast
import hashlib
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


def _normalized_digest(evidence: str) -> str:
    return hashlib.sha256(" ".join(evidence.split()).encode("utf-8")).hexdigest()


@pytest.mark.parametrize(
    "superseded_identifier",
    (
        "85e1e8ec9147242adca311c4ba10ea8c1c3008dc",
        "85e1e8e…",
        "84af413c3a7833daa635e6a0b84e8f331cc1f7f2",
        "84af413…",
        "7a8092f9ac0be03274358bb9814db3d8e6a34507",
        "7a8092f…",
        "00d4636da32836689202464fc55deb5f4b2a8f5e6ffd02831d27d22a9146bc90",
        "00d4636…",
        "0dee3af822edc164b06be6cff61441023ae703dc",
        "0dee3af…",
        "30591690973",
        "91035189804",
        "91035490238",
        "d02ed41e25d1ffa8a43ee8d74583a1008a4d8026",
        "d02ed41…",
        "30592336018",
        "91037154486",
        "91037472406",
        "a8057a8e0c29bdcc95e35949819c64005e5ee064",
        "a8057a8…",
        "9de054e5131ad3f610902d0a7bd4bd97c5968c7a",
        "9de054e…",
        "a5fdc8adc9788f181ace5f4de9cce7974344af02312cef2caa9e123193744503",
        "a5fdc8a…",
        "c6c60d4354eba7348aef3245d19e787661544fa7",
        "c6c60d4…",
        "30588357735",
        "91024909140",
        "91025281220",
        "abdf644b4c35ecfaee2d22917c4c621f9d123a5b",
        "abdf644…",
        "30589435325",
        "91028250060",
        "91028596793",
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

    with pytest.raises(
        AssertionError,
        match=(
            r"(?:superseded provenance exclusion|"
            r"provenance token allowlist|"
            r"Actions provenance identifier allowlist)"
        ),
    ):
        _assert_validation_matches_current_capture(polluted_validation)


@pytest.mark.parametrize(
    "unexpected_current_claim",
    (
        "- Hosted validation successor:\n  `deadbeefdeadbeefdeadbeefdeadbeefdeadbeef`",
        ("- GitHub Actions:\n  [run 999999](https://github.com/example/repo/actions/runs/999999)"),
        (
            "- Result: `verify`\n"
            "  ([job 999998](https://github.com/example/repo/actions/runs/"
            "999999/job/999998)) reported `PASS`."
        ),
        ("- GitHub Actions:\n  [run 999999](https://github.com/example/repo/actions/runs/999999)"),
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

    with pytest.raises(
        AssertionError,
        match=(
            r"(?:current hosted provenance|Actions Markdown label binding|"
            r"provenance token allowlist|current (?:hosted provenance and "
            r"scope|positive) allowlist)"
        ),
    ):
        _assert_validation_matches_current_capture(polluted_validation)


@pytest.mark.parametrize(
    ("original", "replacement"),
    (
        (
            "5364dd4f9f825f5d68c85ea005ceb277ab943e4c",
            "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        ),
        ("30593858167", "99999999999"),
        ("91041814602", "99999999998"),
        ("91042106744", "99999999997"),
        (
            "Both activation job annotation APIs returned `[]`",
            "Activation annotations were not inspected",
        ),
        ("19 web test files / 369 tests", "19 web test files / 368 tests"),
        ("strict MyPy over 35 source files", "strict MyPy over 34 source files"),
        (
            "944 Python tests with 1 warning in 43.54s",
            "943 Python tests with 2 warnings in 44.54s",
        ),
        ("runtime user `10001:10001`", "runtime user was not inspected"),
        (
            "offline network-none `deterministic-qa` and `deployed-readonly` profiles",
            "one online profile",
        ),
    ),
)
def test_validation_rejects_task34_historical_hosted_fact_mutation(
    original: str,
    replacement: str,
) -> None:
    validation = _read("docs/validation.md")
    hosted_heading = "### Task34 terminal-snapshot capture and hosted baselines (superseded)"
    hosted_end_heading = "### Client-terminal capture and hosted baselines (superseded)"
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

    expected_guard = (
        r"(?:provenance token allowlist|"
        r"Task34 (?:ordered Actions bindings|historical facts))"
    )
    if original.isdigit():
        expected_guard = rf"(?:Actions Markdown label binding|{expected_guard})"
    with pytest.raises(AssertionError, match=expected_guard):
        _assert_validation_matches_current_capture(polluted_validation)


def test_validation_rejects_swapped_task34_hosted_job_bindings() -> None:
    validation = _read("docs/validation.md")
    hosted_heading = "### Task34 terminal-snapshot capture and hosted baselines (superseded)"
    hosted_end_heading = "### Client-terminal capture and hosted baselines (superseded)"
    hosted_evidence = validation.split(hosted_heading, maxsplit=1)[1].split(
        hosted_end_heading,
        maxsplit=1,
    )[0]
    verify_job = "91041814602"
    container_job = "91042106744"
    swapped_hosted_evidence = (
        hosted_evidence.replace(verify_job, "__VERIFY_JOB__")
        .replace(container_job, verify_job)
        .replace("__VERIFY_JOB__", container_job)
    )
    polluted_validation = validation.replace(
        hosted_evidence,
        swapped_hosted_evidence,
        1,
    )

    with pytest.raises(
        AssertionError,
        match=(
            r"(?:Actions Markdown label binding|"
            r"Task34 ordered Actions bindings)"
        ),
    ):
        _assert_validation_matches_current_capture(polluted_validation)


@pytest.mark.parametrize(
    "unqualified_positive_claim",
    (
        "Hosted CI passed.",
        "GitHub Actions verified.",
        "Backend passed.",
        "Full canonical verified.",
        "Container passed.",
        "Public deployment passed.",
        "Live OpenAI verified.",
        "Release passed.",
        "Provider execution verified.",
        "Current Task35 full canonical gate returned exit 0.",
        "Current Task35 hosted CI status: 0.",
        "Hosted CI validated.",
        "GitHub Actions confirmed.",
        "Backend OK.",
        "Full canonical complete.",
        "Container operational.",
        "Public deployment live.",
        "Live OpenAI approved.",
        "Provider execution proven.",
        "Release validated.",
        "Docs-tip OK.",
        "Hosted CI.\nPassed.",
        "Passed.\nGitHub Actions.",
        "Current Task35 CI.\nA neutral evidence sentence.\nPassed.",
        (
            "Hosted CI.\nNeutral evidence one.\nNeutral evidence two.\n"
            "Neutral evidence three.\nPassed."
        ),
        (
            "Live OpenAI.\nNeutral evidence one.\nNeutral evidence two.\n"
            "Neutral evidence three.\nStatus: 0."
        ),
    ),
)
def test_validation_rejects_unqualified_current_positive_claims(
    unqualified_positive_claim: str,
) -> None:
    validation = _read("docs/validation.md")
    historical_heading = "## Superseded historical hosted baseline"
    polluted_validation = validation.replace(
        historical_heading,
        f"{unqualified_positive_claim}\n\n{historical_heading}",
        1,
    )

    with pytest.raises(
        AssertionError,
        match=r"current (?:hosted provenance and scope|positive) allowlist",
    ):
        _assert_validation_matches_current_capture(polluted_validation)


@pytest.mark.parametrize(
    "generic_positive_claim",
    (
        "Passed.",
        "All checks passed.",
        "Verified.",
        "Status: 0.",
        "All tests work.",
        "Everything is good.",
        "The server suite is done.",
        "The build checks are ready.",
        "The pipeline is red-free.",
    ),
)
def test_validation_rejects_generic_positive_after_json_media_section(
    generic_positive_claim: str,
) -> None:
    validation = _read("docs/validation.md")
    next_heading = "## Terminal evidence retry proof boundary"
    polluted_validation = validation.replace(
        next_heading,
        f"{generic_positive_claim}\n\n{next_heading}",
        1,
    )

    with pytest.raises(AssertionError, match="current positive allowlist"):
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
        (
            "abdf644b4c35ecfaee2d22917c4c621f9d123a5b",
            "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        ),
        ("91028250060", "99999999996"),
        ("91028596793", "99999999995"),
        (
            "Both pre-CI jobs passed and both pre-CI job annotation APIs returned `[]`",
            "Pre-CI annotations were not inspected",
        ),
        (
            "Both final-tip jobs passed and both final-tip job annotation APIs returned `[]`",
            "Final-tip annotations were not inspected",
        ),
        ("796 Python tests", "795 Python tests"),
        ("owner-only `/data` mode `0700`", "`/data` mode was not inspected"),
        ("runtime user `10001:10001`", "runtime user was not inspected"),
        (
            "offline network-none `deterministic-qa` and `deployed-readonly` profiles",
            "one online profile",
        ),
    ),
)
def test_validation_rejects_task32_historical_fact_mutation(
    original: str,
    replacement: str,
) -> None:
    validation = _read("docs/validation.md")
    task32_heading = "### SQLite-permission capture and hosted baselines (superseded)"
    task32_end_heading = "### JSON-media capture and hosted baselines (superseded)"
    task32_evidence = validation.split(task32_heading, maxsplit=1)[1].split(
        task32_end_heading,
        maxsplit=1,
    )[0]
    original_pattern = re.compile(re.escape(original).replace(r"\ ", r"\s+"))
    assert original_pattern.search(task32_evidence)
    polluted_task32_evidence = original_pattern.sub(
        replacement,
        task32_evidence,
        count=1,
    )
    polluted_validation = validation.replace(
        task32_evidence,
        polluted_task32_evidence,
        1,
    )

    expected_guard = (
        r"(?:provenance token allowlist|"
        r"older history (?:ordered Actions bindings|semantic facts))"
    )
    if original.isdigit():
        expected_guard = rf"(?:Actions Markdown label binding|{expected_guard})"
    with pytest.raises(AssertionError, match=expected_guard):
        _assert_validation_matches_current_capture(polluted_validation)


@pytest.mark.parametrize(
    "contradiction",
    (
        "The pre-CI verify job failed.",
        "The final container-smoke job was skipped.",
        "The hosted verify gate covered 344 web tests.",
        "The final hosted gate covered 795 Python tests.",
        "The local exact gate completed in 29.51s.",
    ),
)
def test_validation_rejects_task32_historical_contradictions(
    contradiction: str,
) -> None:
    validation = _read("docs/validation.md")
    task32_end_heading = "### JSON-media capture and hosted baselines (superseded)"
    polluted_validation = validation.replace(
        task32_end_heading,
        f"{contradiction}\n\n{task32_end_heading}",
        1,
    )

    with pytest.raises(AssertionError, match="older history semantic contract"):
        _assert_validation_matches_current_capture(polluted_validation)


@pytest.mark.parametrize(
    "unsupported_positive_claim",
    (
        "Current Task35 public deployment passed.",
        "Current Task35 live OpenAI passed.",
        "Current Task35 container restart and replacement persistence passed.",
    ),
)
def test_validation_rejects_extra_positive_claim_in_current_checkpoint(
    unsupported_positive_claim: str,
) -> None:
    validation = _read("docs/validation.md")
    historical_heading = "## Superseded historical hosted baseline"
    polluted_validation = validation.replace(
        historical_heading,
        f"- {unsupported_positive_claim}\n\n{historical_heading}",
        1,
    )

    with pytest.raises(
        AssertionError,
        match=r"current (?:hosted provenance and scope|positive) allowlist",
    ):
        _assert_validation_matches_current_capture(polluted_validation)


def test_validation_rejects_swapped_task32_historical_job_bindings() -> None:
    validation = _read("docs/validation.md")
    task32_heading = "### SQLite-permission capture and hosted baselines (superseded)"
    task32_end_heading = "### JSON-media capture and hosted baselines (superseded)"
    task32_evidence = validation.split(task32_heading, maxsplit=1)[1].split(
        task32_end_heading,
        maxsplit=1,
    )[0]
    verify_job = "91024909140"
    container_job = "91025281220"
    swapped_task32_evidence = (
        task32_evidence.replace(verify_job, "__VERIFY_JOB__")
        .replace(container_job, verify_job)
        .replace("__VERIFY_JOB__", container_job)
    )
    assert swapped_task32_evidence != task32_evidence
    polluted_validation = validation.replace(
        task32_evidence,
        swapped_task32_evidence,
        1,
    )

    with pytest.raises(
        AssertionError,
        match=(
            r"(?:Actions Markdown label binding|"
            r"older history ordered Actions bindings)"
        ),
    ):
        _assert_validation_matches_current_capture(polluted_validation)


@pytest.mark.parametrize(
    "unexpected_positive_claim",
    (
        "Current Task35 CI passed.",
        "Current Task35 CI. Passed.",
        "Current Task35 CI.\nPassed.",
        "Passed. Current Task35 CI.",
        "Current Task35 container job passed.",
        "Current Task35 verify job passed.",
        "Task35 GitHub Actions passed successfully.",
        "The current Task35 GitHub workflow passed.",
        "Task35 container smoke. Result: verified.",
        "Task35 container smoke.\nResult: verified.",
        "Status: verified. Task35 container smoke.",
        "Healthy. Current Task35 workflow.",
        "Completed cleanly. Current Task35 packaged Docker.",
        "No failures. Current Task35 verify job.",
        "Current Task35 CI completed cleanly.",
        "Current Task35 CI is passing.",
        "Current Task35 workflow is healthy.",
        "Current Task35 container smoke completed without failures.",
        "Current Task35 CI has no failures.",
        "Current Task35 CI has zero failures.",
        "Current Task35 CI is error-free.",
        (
            "- No current Task35 full canonical, GitHub Actions, "
            "or packaged container result has been observed.\n"
            "Passed."
        ),
        (
            "Passed.\n"
            "- No current Task35 full canonical, GitHub Actions, "
            "or packaged container result has been observed."
        ),
        "The full Python suite passed all 718 tests.",
        "Canonical backend: 718 passed.",
        "Current Task35 GitHub Actions passed successfully.",
        "Current Task35 GitHub Actions run is green.",
        "Current Task35 hosted CI succeeded.",
        "Current Task35 packaged container smoke passed successfully.",
        "Current Task35 container-smoke job is green.",
        "Current Task35 container smoke succeeded.",
        "Current Task35 workflow green.",
        "Current Task35 packaged Docker successful.",
        "Current Task35 container-smoke verified.",
        "Current Task35 full canonical gate passed 936 tests.",
        "Current Task35 backend gate succeeded.",
        "Current Task35 docs-tip workflow passed.",
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

    with pytest.raises(
        AssertionError,
        match=r"current (?:hosted provenance and scope|positive) allowlist",
    ):
        _assert_validation_matches_current_capture(polluted_validation)


TASK36_FEATURE_SOURCE = "9239410042de1477c15bdf1a4c72fb1bf00dd831"
TASK36_CAPTURE_SOURCE = "a01f0a361f2c45f762496a0c6af298a4473fde96"
TASK36_CAPTURE_COMMIT = "1030d197ce2801be2abe17bea43522a97736adb8"
TASK36_RUNTIME_DIGEST = "168e315936fe58a2f2371c0122dbf0c5d5a4f0be93f7f2a1021e4f84dd90a6f4"
TASK36_CURRENT_DIGEST = "045e22ebba86886a986be343370738cd6d24d1f427a6c5d4d10e5ae40f5157ac"
TASK35_HISTORY_DIGEST = "9ca719ab22ff4aa4de79288f2a1ce70c403b59cb464d72061394fdc76eef0cc4"
OLDER_HISTORY_DIGEST = "1cb4d31a3ffc3132fdaf7321f30fd1e5761b20436e1da28c082d6424a3d98d4e"
TASK34_HISTORY_DIGEST = "86d6128f55b21219641cd1b4f62da4a4567a352557b7ef9f30755c30dea71d7c"
TASK32_HISTORY_DIGEST = "0ce4b9e4e486b50b24bb3e05fd22746c7b86b5f29dd0266234736232728e900c"
HISTORICAL_HEADING = "## Superseded historical hosted baseline"
TASK35_HEADING = "### Task35 terminal-marker capture and hosted baselines (superseded)"
TASK34_HEADING = "### Task34 terminal-snapshot capture and hosted baselines (superseded)"

ACTIONS_BASE = "https://github.com/charlie2233/backchannel-agent-support/actions/runs"
ALLOWED_ACTION_RUNS = (
    ("30606996966", "91081238197", "91081310037"),
    ("30607860082", "91083947213", "91084012386"),
    ("30608202642", "91084965375", "91085278851"),
    ("30595724271", "91047571247", "91047810594"),
    ("30598406930", "91055708186", "91055968348"),
    ("30603151491", "91069887646", "91070162086"),
    ("30593858167", "91041814602", "91042106744"),
    ("30594431983", "91043601366", "91043886907"),
    ("30591690973", "91035189804", "91035490238"),
    ("30592336018", "91037154486", "91037472406"),
    ("30588357735", "91024909140", "91025281220"),
    ("30589435325", "91028250060", "91028596793"),
    ("30501109833", "90740755548", "90741063564"),
    ("30501426777", "90741727313", "90742037347"),
    ("30499178838", "90734790003", "90735121608"),
    ("30499500934", "90735804461", "90736149754"),
    ("30208188300", "89809811619", "89809998273"),
    ("30182741263", "89742014836", "89742159778"),
    ("30174822102", "89721793096", "89721989116"),
    ("30162644778", "89690352979", "89690513333"),
    ("30140554792", "89632837699", "89633002245"),
)
ALLOWED_ACTION_REFERENCE_IDS = (
    "30606996966",
    "91081238197",
    "91081310037",
    "30607860082",
    "91083947213",
    "91084012386",
    "30608202642",
    "91084965375",
    "91085278851",
    "30595724271",
    "91047571247",
    "91047810594",
    "30598406930",
    "91055708186",
    "91055968348",
    "30603151491",
    "91069887646",
    "91070162086",
    "30603151491",
    "91069887646",
    "91070162086",
    "30595724271",
    "30593858167",
    "91041814602",
    "91042106744",
    "30594431983",
    "91043601366",
    "91043886907",
    "30591690973",
    "91035189804",
    "91035490238",
    "30592336018",
    "91037154486",
    "91037472406",
    "30588357735",
    "91024909140",
    "91025281220",
    "30589435325",
    "91028250060",
    "91028596793",
    "30501109833",
    "90740755548",
    "90741063564",
    "30501426777",
    "90741727313",
    "90742037347",
    "30499178838",
    "90734790003",
    "90735121608",
    "30499500934",
    "90735804461",
    "90736149754",
    "30208188300",
    "89809811619",
    "89809998273",
    "30182741263",
    "89742014836",
    "89742159778",
    "30174822102",
    "89721793096",
    "89721989116",
    "30162644778",
    "89690352979",
    "89690513333",
    "30140554792",
    "89632837699",
    "89633002245",
)
ALLOWED_ACTION_MARKDOWN_BINDINGS = tuple(
    binding
    for run_id, verify_job_id, container_job_id in ALLOWED_ACTION_RUNS
    for binding in (
        ("run", run_id, f"{ACTIONS_BASE}/{run_id}"),
        (
            "job",
            verify_job_id,
            f"{ACTIONS_BASE}/{run_id}/job/{verify_job_id}",
        ),
        (
            "job",
            container_job_id,
            f"{ACTIONS_BASE}/{run_id}/job/{container_job_id}",
        ),
    )
)
ALLOWED_ACTION_URLS = tuple(url for _kind, _label_id, url in ALLOWED_ACTION_MARKDOWN_BINDINGS)
ALLOWED_PROVENANCE_TOKEN_OCCURRENCES = (
    "9239410042de1477c15bdf1a4c72fb1bf00dd831",
    "a01f0a361f2c45f762496a0c6af298a4473fde96",
    "1030d197ce2801be2abe17bea43522a97736adb8",
    "9239410…",
    "a01f0a3…",
    "1030d19…",
    "a01f0a361f2c45f762496a0c6af298a4473fde96",
    "168e315936fe58a2f2371c0122dbf0c5d5a4f0be93f7f2a1021e4f84dd90a6f4",
    "9239410…",
    "a01f0a3…",
    "1030d19…",
    "168e315…",
    "a01f0a3…",
    "a01f0a3…",
    "21d9b0f8dbeb59454e3f3b3d3d9138af28af02ad",
    "60fe1243a5eeb760984d78d667f7efdc33630adc",
    "6533a3f2e212d9a7c4e1af9f1d2dd77f7bd45a18",
    "6533a3f2e212d9a7c4e1af9f1d2dd77f7bd45a18",
    "60fe1243a5eeb760984d78d667f7efdc33630adc",
    "6cbafce70fa1ea60cf00e315062e4e14a5ffb934",
    "6533a3f…",
    "21d9b0f…",
    "6cbafce…",
    "eabc2743f9475e384fe7a49344f42a9635d2f57277f788708d23425edd845385",
    "21d9b0f…",
    "60fe124…",
    "6533a3f…",
    "6cbafce…",
    "eabc274…",
    "21d9b0f…",
    "8942517f43045a124e11a8e79296f6e4de875936",
    "4ca13252e05357ff98023f9290375cd1718bcce6",
    "e208c767646af920d90aa9396f441fb328c999c3235aff646ac20a5611543f27",
    "5364dd4f9f825f5d68c85ea005ceb277ab943e4c",
    "9d7e850cbf6761524e9a51359801944cdf7fabf4",
    "84af413c3a7833daa635e6a0b84e8f331cc1f7f2",
    "7a8092f9ac0be03274358bb9814db3d8e6a34507",
    "00d4636da32836689202464fc55deb5f4b2a8f5e6ffd02831d27d22a9146bc90",
    "0dee3af822edc164b06be6cff61441023ae703dc",
    "d02ed41e25d1ffa8a43ee8d74583a1008a4d8026",
    "84af413…",
    "7a8092f…",
    "00d4636…",
    "0dee3af…",
    "d02ed41…",
    "a8057a8e0c29bdcc95e35949819c64005e5ee064",
    "9de054e5131ad3f610902d0a7bd4bd97c5968c7a",
    "a5fdc8adc9788f181ace5f4de9cce7974344af02312cef2caa9e123193744503",
    "c6c60d4354eba7348aef3245d19e787661544fa7",
    "abdf644b4c35ecfaee2d22917c4c621f9d123a5b",
    "a8057a8…",
    "9de054e…",
    "a5fdc8a…",
    "c6c60d4…",
    "abdf644…",
    "c446f05adfb836539ddbaa74a41502034c910092",
    "b4c1bcbd70ff22ce3ac2b8a1de3828b7ce691afe",
    "3bb9737a483fc90e50d5a6158bf0c61c1807e6510d22c9bf6d88e142d21ff5d9",
    "62c37d6261257ea3800335257e43ae45ed839c47",
    "3abd059087e216fb4fed613b45927f27a3af627f",
    "c446f05…",
    "b4c1bcb…",
    "3bb9737…",
    "62c37d6…",
    "3abd059…",
    "96543fff62bb5d0a3c0f8a0464e9a07bf9c54568",
    "98dae414b3cdd36ee25d0dad3fe78257f3f4c135",
    "e22c42085703fcdfaaa3f994334cc418a4e81e858187757edb5a33b8e2221e13",
    "a3e3179bafb8050598cc5512d56e0c7661318c64",
    "9e19b12de072e55932d1b32e24987576284ac4f0",
    "96543ff…",
    "98dae41…",
    "e22c420…",
    "a3e3179…",
    "9e19b12…",
    "8d0a896c753c4c60894301a20a3866bbbfa1e76f",
    "cca97a8e75d52a26889d3bbb66740756041d9caf",
    "e188202a4b0612a66503fda2b6ee1a3c89ec7b65",
    "a1e0ecaf69926044419e29c7359102188c80550834ddb9351021aae411705054",
    "8d0a896…",
    "cca97a8…",
    "e188202…",
    "a1e0eca…",
    "8823d29d7de93d44f4843a2fa4db1adec4e452bd",
    "55af1e6d68f11542b1c5cc5e3465b87dc158ec08",
    "117e4ebe40efea36f89bbb737143de9c918f938f",
    "4ad336eaf2d304906e939fc0eb433c6633519e73d9855207ae72c4daea42eed2",
    "8823d29…",
    "55af1e6…",
    "117e4eb…",
    "742e3caf2af5a9cce3cd8de242cf113424e8528f",
    "4a317e563c8d45bc45f676e465b01780b2b0be78",
    "1289b773abe92bb2f5842f77e3c7f50c432352f4",
    "1289b77…",
    "1bde9788d551e5f76b895b977ad69291c6b1f44d26095b242d531f1bf289418c",
    "742e3ca…",
    "4a317e5…",
    "1289b77…",
    "7d9128a8171ddb4978d8f7b0debb9effce997e27",
    "576ba5e3dfd3d13b9f797c1c516c2352c4e40688",
    "7d9128a…",
    "576ba5e…",
    "b57868005a3fe0869136f54472ee0098035a9099",
)
EXPECTED_TASK36_HOSTED_SCOPE_LINES = (
    "container, live OpenAI, deployment, or release evidence.",
    "execute three live runs or prove a real provider mutation.",
    "`container-smoke`",
    "`container-smoke`",
    "release-document contract still named Task35 and the removed `pretest:e2e`",
    "contract. The generic exit-code annotation count was 1. `container-smoke`",
    "workflow steps were not reached after the canonical failure.",
    "- No successful current Task36 hosted `verify`, packaged `container-smoke`, or",
    "documentation-tip CI result is claimed.",
    "decision, session owner, generation, policy, scope, expiry, and canonical",
    "provider result before it writes execution evidence.",
    "`authorization_expired_before_dispatch` with zero provider dispatch and zero",
    "successful terminal provider result.",
    "`terminalReason` as null; it does not invent an expiry reason or provider",
    "skipped container jobs. A successful activation `verify`, packaged",
    "`container-smoke`, and documentation-tip CI remain pending.",
    "- There is no current Task36 live OpenAI, public deployment/browser URL, target",
    "multi-container SQLite, real external-provider execution, tag, or release",
    "| Local final-build captures | "
    "[`docs/assets/final/manifest.json`](assets/final/manifest.json); six "
    "provenance-checked PNGs under [`docs/assets/final`](assets/final) | **Local capture "
    "evidence only** on clean manifest source `a01f0a3…`, capture commit `1030d19…`, and "
    "Google Chrome 150.0.7871.187 at 1440×1024 and 390×844; not CI, container, "
    "deployment, live OpenAI, or release proof |",
    "| Live OpenAI | Three-run command stopped at `missing_openai_api_key` after attempt "
    "1 of 3, with zero approvals, model IDs `[]`, tools `[]`, and null root trace | "
    "**Blocked / Unverified**; three live runs did not execute |",
    "| GitHub CI/container | Runs `30606996966`, `30607860082`, and `30608202642`: all "
    "`verify` jobs failed; all `container-smoke` jobs were skipped; latest annotations 1 "
    "/ 0 | **Failed / Unverified**; bounded nonproof only, with no current Task36 hosted "
    "canonical or packaged-container success |",
    "| Public deployment | No public application origin or browser URL | **Blocked / "
    "Unverified** |",
    "| Target host/durability | No target-host networking, abrupt-loss, backup, or "
    "concurrent multi-container SQLite proof | **Blocked / Unverified** |",
    "| Tag/release | No release tag or GitHub release | **Blocked / Unverified** |",
    "## Hosted container evidence boundary",
    "`container-smoke` jobs were skipped. The latest capture-commit run reached",
    "manifest verification and the web suite before the stale release-document",
    "workflow steps. These failures are bounded nonproof and are not converted into",
    "hosted or container success by local gates.",
    "browser captures do not prove packaged Docker, browser capture in CI, public",
    "deployment, live OpenAI, real provider execution, target-host",
    "durability/networking, abrupt host loss or backup, concurrent multi-container",
    "SQLite, documentation-tip CI, tag, or release.",
    "capture remained local; CI did not execute it. The six images prove only the",
    "local keyless SDK-stub final build. They do not prove live OpenAI, a public",
    "deployment, a container, a release, or a real provider mutation.",
    "container-replacement, abrupt-host-loss, or concurrent multi-container SQLite proof.",
    "multi-container SQLite, target-host scheduling, abrupt host loss, public latency,",
    "or live provider behavior.",
    "`a01f0a3…` and is not public deployment, live OpenAI, container-host durability, tag,",
    "or release proof.",
    "route-gate release, retained admission charges, exact public `504 live_timeout`",
    "envelopes, retryable approve/decline claims, and no duplicate dispatch when provider",
    "process-termination proof, public deployment, or release claim.",
)
EXPECTED_TASK36_POSITIVE_LINES = (
    "container, live OpenAI, deployment, or release evidence.",
    "- The focused Task36 approval-expiry, restart, migration, and API matrix passed",
    "`93 passed`. The full Python suite passed `1022 passed`; the web suite passed",
    "the history-aware secret scan, and the diff check all passed.",
    "- Three focused public terminal-evidence tamper cases passed `3 passed`: owner",
    "- The deterministic stub smoke passed. The local one-process production smoke",
    "passed health, readiness, static serving, API, SSE/reconnect, approval,",
    "decline, session isolation, and bounded clean SIGTERM.",
    "- Independent source/specification and security reviews passed with no P0-P3",
    "exact-at-expiry execution truth, live UI truth, preauthorization chronology,",
    "ignored `web/dist` could have been served while the manifest named a clean",
    "path, and re-verifies unchanged clean source identity after the build. The",
    "- `npm run test:e2e` then reported `35 passed` against the clean manifest source",
    "passed with no P0-P3 findings.",
    "- The live three-run command remained blocked at its",
    "execute three live runs or prove a real provider mutation.",
    "`FAILURE` with `264 failed, 758 passed, 1 warning` in 58.16s because the",
    "- No successful current Task36 hosted `verify`, packaged `container-smoke`, or",
    "execution. If the system cannot prove whether dispatch occurred, it records",
    "successful terminal provider result.",
    "terminal snapshot, receipt, and SSE readers validate the complete sealed receipt",
    "SDK-stub and OpenAI-live responses and receipts require the truthful",
    "skipped container jobs. A successful activation `verify`, packaged",
    "- There is no current Task36 live OpenAI, public deployment/browser URL, target",
    "| Local source and tests | Feature source `9239410…`: focused 93, public-terminal "
    "tamper 3, 19 web files / 382 tests, 1022 Python tests, build, Ruff, strict MyPy, "
    "OpenAPI, secret scan, diff, stub smoke, and local production smoke | **Verified "
    "locally** in the exact named lanes |",
    "| Local final-build captures | "
    "[`docs/assets/final/manifest.json`](assets/final/manifest.json); six "
    "provenance-checked PNGs under [`docs/assets/final`](assets/final) | **Local capture "
    "evidence only** on clean manifest source `a01f0a3…`, capture commit `1030d19…`, and "
    "Google Chrome 150.0.7871.187 at 1440×1024 and 390×844; not CI, container, "
    "deployment, live OpenAI, or release proof |",
    "| Live OpenAI | Three-run command stopped at `missing_openai_api_key` after attempt "
    "1 of 3, with zero approvals, model IDs `[]`, tools `[]`, and null root trace | "
    "**Blocked / Unverified**; three live runs did not execute |",
    "| GitHub CI/container | Runs `30606996966`, `30607860082`, and `30608202642`: all "
    "`verify` jobs failed; all `container-smoke` jobs were skipped; latest annotations 1 "
    "/ 0 | **Failed / Unverified**; bounded nonproof only, with no current Task36 hosted "
    "canonical or packaged-container success |",
    "hosted or container success by local gates.",
    "browser captures do not prove packaged Docker, browser capture in CI, public",
    "deployment, live OpenAI, real provider execution, target-host",
    "`chrome` channel in headless mode. It validates API and DOM provenance before",
    "capture remained local; CI did not execute it. The six images prove only the",
    "local keyless SDK-stub final build. They do not prove live OpenAI, a public",
    "on a different clean frozen tree.",
    "cases prove these source-level parser boundaries; the screenshots do not prove",
    "fresh equally bounded initial-plus-three cycle. Throughout retry, already verified",
    "The ordinary final-state screenshots do not prove transient store failure, retry",
    "capture, and smoke lanes; it does not prove multi-process or concurrent",
    "or live provider behavior.",
    "are source and raw-ASGI contract properties. The six screenshots do not prove",
    "second/third write failure, validation-before-charge, live-ledger separation, reset",
    "`a01f0a3…` and is not public deployment, live OpenAI, container-host durability, tag,",
    "## Live deadline proof boundary",
    "The source contract bounds the complete live pre-approval graph and each live",
    "envelopes, retryable approve/decline claims, and no duplicate dispatch when provider",
    "execution committed before the timeout. The OpenAI client and the redacted live-smoke",
)

FORBIDDEN_CURRENT_POSITIVE_CLAIMS = (
    "Hosted CI passed.",
    "GitHub Actions verified.",
    "Backend passed.",
    "Full canonical verified.",
    "Container passed.",
    "Public deployment passed.",
    "Live OpenAI verified.",
    "Release passed.",
    "Provider execution verified.",
    "Hosted CI validated.",
    "GitHub Actions confirmed.",
    "Backend OK.",
    "Full canonical complete.",
    "Container operational.",
    "Public deployment live.",
    "Live OpenAI approved.",
    "Provider execution proven.",
    "Release validated.",
    "Docs-tip OK.",
    "Hosted CI. Passed.",
    "Passed. GitHub Actions.",
    "Passed.",
    "All checks passed.",
    "Verified.",
    "Status: 0.",
    "All tests work.",
    "Everything is good.",
    "The server suite is done.",
    "The build checks are ready.",
    "The pipeline is red-free.",
    "Current Task35 full canonical gate returned exit 0.",
    "Current Task35 hosted CI status: 0.",
    "Current Task35 CI. A neutral evidence sentence. Passed.",
    "Hosted CI. Neutral evidence one. Neutral evidence two. Neutral evidence three. Passed.",
    "Live OpenAI. Neutral evidence one. Neutral evidence two. Neutral evidence three. Status: 0.",
    "Current Task35 CI passed.",
    "Current Task35 CI. Passed.",
    "Passed. Current Task35 CI.",
    "Current Task35 container job passed.",
    "Current Task35 verify job passed.",
    "Task35 GitHub Actions passed successfully.",
    "The current Task35 GitHub workflow passed.",
    "Task35 container smoke. Result: verified.",
    "Status: verified. Task35 container smoke.",
    "Healthy. Current Task35 workflow.",
    "Completed cleanly. Current Task35 packaged Docker.",
    "No failures. Current Task35 verify job.",
    "Current Task35 CI completed cleanly.",
    "Current Task35 CI is passing.",
    "Current Task35 workflow is healthy.",
    "Current Task35 container smoke completed without failures.",
    "Current Task35 CI has no failures.",
    "Current Task35 CI has zero failures.",
    "Current Task35 CI is error-free.",
    (
        "- No current Task35 full canonical, GitHub Actions, or packaged "
        "container result has been observed. Passed."
    ),
    (
        "Passed. - No current Task35 full canonical, GitHub Actions, or "
        "packaged container result has been observed."
    ),
    "The full Python suite passed all 718 tests.",
    "Canonical backend: 718 passed.",
    "Current Task35 GitHub Actions passed successfully.",
    "Current Task35 GitHub Actions run is green.",
    "Current Task35 hosted CI succeeded.",
    "Current Task35 packaged container smoke passed successfully.",
    "Current Task35 container-smoke job is green.",
    "Current Task35 container smoke succeeded.",
    "Current Task35 workflow green.",
    "Current Task35 packaged Docker successful.",
    "Current Task35 container-smoke verified.",
    "Current Task35 full canonical gate passed 936 tests.",
    "Current Task35 backend gate succeeded.",
    "Current Task35 docs-tip workflow passed.",
    "Full canonical Python gate passed 718 tests.",
    "Full canonical gate succeeded with 718 tests.",
    "Final full canonical gate passed 718 tests.",
    "Current full canonical Python gate passed 718 tests.",
    "Current Task36 public deployment passed.",
    "Current Task36 live OpenAI passed.",
    "Current Task36 container restart and replacement persistence passed.",
    "Current Task36 GitHub Actions passed successfully.",
    "Current Task36 GitHub Actions run is green.",
    "Current Task36 hosted CI succeeded.",
    "Current Task36 packaged container smoke passed successfully.",
    "Current Task36 container-smoke job is green.",
    "Current Task36 container smoke succeeded.",
    "Current Task36 workflow green.",
    "Current Task36 packaged Docker successful.",
    "Current Task36 container-smoke verified.",
    "Current Task36 backend gate succeeded.",
    "Current Task36 docs-tip workflow passed.",
    "Current Task36 hosted CI passed.",
    "Current Task36 container-smoke passed.",
    "Current Task36 release passed.",
    "Current Task36 hosted CI and public deployment succeeded.",
    "Current Task35 public deployment passed.",
    "Current Task35 live OpenAI passed.",
    "Current Task35 container restart and replacement persistence passed.",
)
ADDITIONAL_SUPERSEDED_IDENTIFIERS = (
    "85e1e8ec9147242adca311c4ba10ea8c1c3008dc",
    "85e1e8e…",
    "30206582233",
    "89805619343",
    "89805788728",
    "b578680…",
)
FORBIDDEN_UNEXPECTED_HOSTED_CLAIMS = (
    "Hosted validation successor:",
    "GitHub Actions: [run 999999]",
    "Result: `verify` ([job 999998]",
    "Result: `verify` reported `FAILURE`; `container-smoke` reported `SKIPPED`.",
)


def _task36_current_and_history(validation: str) -> tuple[str, str]:
    assert validation.count(HISTORICAL_HEADING) == 1, "Task36 document structure"
    return validation.split(HISTORICAL_HEADING, maxsplit=1)


def _task36_section(
    current: str,
    start_heading: str,
    end_heading: str,
    guard: str,
) -> str:
    assert current.count(start_heading) == 1, guard
    assert current.count(end_heading) == 1, guard
    return current.split(start_heading, maxsplit=1)[1].split(
        end_heading,
        maxsplit=1,
    )[0]


def _require_normalized_facts(
    section: str,
    facts: tuple[str, ...],
    guard: str,
) -> None:
    normalized = " ".join(section.split())
    for fact in facts:
        assert fact in normalized, f"{guard}: missing {fact}"


def _assert_actions_link_bindings(
    section: str,
    expected: tuple[tuple[str, str, str, str | None], ...],
    guard: str,
) -> None:
    observed = tuple(
        (kind, label_id, run_id, job_id or None)
        for kind, label_id, run_id, job_id in re.findall(
            r"\[(run|job) (\d+)\]\("
            r"https://github\.com/[^)\s]+/actions/runs/(\d+)"
            r"(?:/job/(\d+))?\)",
            section,
        )
    )
    assert observed == expected, guard
    for kind, label_id, run_id, job_id in observed:
        if kind == "run":
            assert label_id == run_id and job_id is None, guard
        else:
            assert job_id is not None and label_id == job_id, guard


def _assert_all_actions_links_correlated(validation: str) -> None:
    markdown_bindings = tuple(
        (kind, label_id, url)
        for kind, label_id, url in re.findall(
            r"\[(run|job)\s+(\d+)\]\(([^)\r\n]*)\)",
            validation,
            flags=re.IGNORECASE,
        )
    )
    assert markdown_bindings == ALLOWED_ACTION_MARKDOWN_BINDINGS, "Actions Markdown label binding"

    action_urls = tuple(
        re.findall(
            (
                r"(?i)(?:(?:[a-z][a-z0-9+.-]*:)?//)[^\s<>()]*"
                r"/actions/runs/\d+(?:/job/\d+)?[^\s<>()]*"
            ),
            validation,
        )
    )
    assert action_urls == ALLOWED_ACTION_URLS, "Actions Markdown label binding"

    referenced_action_ids = tuple(
        re.findall(
            r"(?i)(?<![A-Za-z])(?:run|job)\s+`?(\d{6,})`?",
            validation,
        )
    )
    assert referenced_action_ids == ALLOWED_ACTION_REFERENCE_IDS, (
        "Actions provenance identifier allowlist"
    )

    provenance_tokens = tuple(
        match.group(0)
        for match in re.finditer(
            (
                r"(?<![0-9A-Za-z_])(?:[0-9A-Fa-f]{64}|"
                r"[0-9A-Fa-f]{40}|[0-9A-Fa-f]{7}…)"
                r"(?![0-9A-Za-z_])"
            ),
            validation,
        )
    )
    assert provenance_tokens == ALLOWED_PROVENANCE_TOKEN_OCCURRENCES, "provenance token allowlist"


def _assert_current_claim_line_allowlists(current: str) -> None:
    current_lines = tuple(line.strip() for line in current.splitlines() if line.strip())
    hosted_scope_pattern = re.compile(
        r"\b(?:CI|GitHub\s+Actions|workflow|verify\s+job|canonical|backend|"
        r"container(?:-smoke|\s+smoke|\s+job)?|packaged\s+Docker|"
        r"public\s+deployment|live\s+OpenAI|provider|release|docs-tip)\b",
        flags=re.IGNORECASE,
    )
    observed_hosted_scope_lines = tuple(
        line for line in current_lines if hosted_scope_pattern.search(line)
    )
    assert observed_hosted_scope_lines == EXPECTED_TASK36_HOSTED_SCOPE_LINES, (
        "current hosted provenance and scope allowlist"
    )

    positive_word_pattern = re.compile(
        r"\b(?:pass|passed|passing|green|healthy|clean|cleanly|clear|"
        r"succeed|succeeded|succeeding|success|successful|successfully|verified|"
        r"validat(?:e|ed|es|ing)|confirm(?:ed|s|ing)?|ok|complete(?:d|ly)?|"
        r"operational|live|approv(?:e|ed|ing)|prov(?:e|ed|en|ing)|works?|good|"
        r"done|ready|red-free)\b"
        r"|\b(?:completed|finished)\s+(?:cleanly|successfully)\b"
        r"|\bwithout\s+(?:any\s+)?(?:failures?|errors?)\b"
        r"|\b(?:no|zero)\s+(?:failures?|errors?)\b"
        r"|\b(?:failure|error)-free\b"
        r"|\b(?:returned\s+)?exit(?:\s+code)?\s*(?:[:=]\s*)?0\b"
        r"|\bstatus\s*(?:[:=]\s*)?0\b",
        flags=re.IGNORECASE,
    )
    observed_positive_lines = tuple(
        line for line in current_lines if positive_word_pattern.search(line)
    )
    assert observed_positive_lines == EXPECTED_TASK36_POSITIVE_LINES, "current positive allowlist"


def _assert_task36_common_boundaries(current: str) -> None:
    sqlite = _task36_section(
        current,
        "## SQLite file-permission proof boundary",
        "## JSON response media proof boundary",
        "SQLite permission semantic contract",
    )
    _require_normalized_facts(
        sqlite,
        (
            "owner-only `0600` regular files",
            "Constructor-time initialization is the only lane allowed to harden",
            (
                "connection and readiness checks are nonmutating and fail closed "
                "instead of repairing permission drift"
            ),
            "URI with `mode=rw`",
            "Unsupported POSIX capabilities",
            "unexpected owner",
            "unprotected directory ancestry",
            "changed device/inode identity",
            "insecure database or sidecar permissions all fail closed",
            "owner-only `0700`",
            "23 focused tests",
        ),
        "SQLite permission semantic contract",
    )

    media = _task36_section(
        current,
        "## JSON response media proof boundary",
        "## Terminal evidence retry proof boundary",
        "JSON media semantic contract",
    )
    _require_normalized_facts(
        media,
        (
            "exactly one `Content-Type` field",
            "case-insensitive `application/json` media type",
            "valid no-comma token or quoted-string parameters",
            (
                "missing, wrong, malformed, comma-joined, duplicate parameters, "
                "and split-quote values"
            ),
            "cancels the unlocked wrong-media response body without waiting",
            "typed IDs, fallback, retry metadata, and provenance remain inactive",
            "SSE behavior is unchanged",
            ("ordinary `500` handling and the Task30 exact `503` retry contract remain preserved"),
        ),
        "JSON media semantic contract",
    )

    retry = _task36_section(
        current,
        "## Terminal evidence retry proof boundary",
        "## Async SQLite proof boundary",
        "terminal retry semantic contract",
    )
    _require_normalized_facts(
        retry,
        (
            "HTTP `503`",
            "code `internal_error`",
            "`recoveryId` equal to the requested recovery UUID",
            "`retryAfterSeconds: 1`",
            "`fallback: null`",
            "`Retry-After: 1`",
            "`application/json` media",
            "exactly three",
            "one-second automatic retries",
            "one accessible manual retry",
            "fresh equally bounded initial-plus-three cycle",
            ("already verified snapshot, receipt, event, and cursor state remains visible"),
            "No POST, create, decision, reset, or fallback request",
        ),
        "terminal retry semantic contract",
    )

    async_sqlite = _task36_section(
        current,
        "## Async SQLite proof boundary",
        "## Request-body availability proof boundary",
        "Async SQLite semantic contract",
    )
    _require_normalized_facts(
        async_sqlite,
        (
            "retains the bounded-worker SQLite design",
            "superseded Task29 section",
            (
                "current Task36 activation verifies only its exact local "
                "source, capture, and smoke lanes"
            ),
            "does not prove multi-process or concurrent multi-container SQLite",
            "target-host scheduling",
            "abrupt host loss",
            "public latency",
            "live provider behavior",
        ),
        "Async SQLite semantic contract",
    )

    request_body = _task36_section(
        current,
        "## Request-body availability proof boundary",
        "## Creation-admission proof boundary",
        "request-body availability semantic contract",
    )
    _require_normalized_facts(
        request_body,
        (
            "manifest source rejects framed `GET` and `HEAD` requests before body reads",
            "preserves ordinary bodyless receives",
            "bounds allowed-body pre-buffering with a configurable cooperative deadline",
            "source and raw-ASGI contract properties",
            "screenshots do not prove adversarial transport timing",
            "public-host availability",
            "hard cancellation of a non-cooperative dependency",
        ),
        "request-body availability semantic contract",
    )

    creation = _task36_section(
        current,
        "## Creation-admission proof boundary",
        "## Live deadline proof boundary",
        "creation-admission semantic contract",
    )
    _require_normalized_facts(
        creation,
        (
            "separate durable UTC-day creation ledger with session, IP, and global limits",
            "atomic race admission",
            "rollback on an injected second/third write failure",
            "validation-before-charge",
            "live-ledger separation",
            "reset and restart persistence",
            "bounded retention",
            "generic `429`/`Retry-After` contract",
            "final-build capture lane from `a01f0a3…`",
            "not public deployment, live OpenAI, container-host durability, tag, or release proof",
        ),
        "creation-admission semantic contract",
    )

    live_deadline = current.split("## Live deadline proof boundary", maxsplit=1)[1]
    _require_normalized_facts(
        live_deadline,
        (
            "complete live pre-approval graph",
            "configurable 1..300-second cooperative deadline",
            "one shared graph budget",
            "external cancellation propagation",
            "no partial recovery",
            "route-gate release",
            "retained admission charges",
            "exact public `504 live_timeout` envelopes",
            "retryable approve/decline claims",
            "no duplicate dispatch when provider execution committed before the timeout",
            "transport retries disabled",
            "not a real OpenAI trace",
            "hard process-termination proof",
            "public deployment",
            "release claim",
        ),
        "live deadline semantic contract",
    )


def _assert_validation_matches_current_capture(validation: str) -> None:
    manifest = json.loads(_read("docs/assets/final/manifest.json"))
    runtime_input = manifest["runtimeInput"]
    current, history = _task36_current_and_history(validation)
    normalized_current = " ".join(current.split())
    _assert_all_actions_links_correlated(validation)

    assert current.count("## Current Task36 authorization-expiry evidence activation") == 1, (
        "Task36 document structure"
    )
    assert current.count("## Current Task36 authorization-expiry proof boundary") == 1, (
        "Task36 document structure"
    )
    assert current.count("## Current Task36 pending external gates") == 1, (
        "Task36 document structure"
    )
    for unique_heading in (
        "## Per-SHA proof matrix",
        "## Hosted container evidence boundary",
        "## Capture contract",
        "## SQLite file-permission proof boundary",
        "## JSON response media proof boundary",
        "## Terminal evidence retry proof boundary",
        "## Async SQLite proof boundary",
        "## Request-body availability proof boundary",
        "## Creation-admission proof boundary",
        "## Live deadline proof boundary",
    ):
        assert validation.count(unique_heading) == 1, "Task36 document structure"
    assert not re.search(r"^## ", history, flags=re.MULTILINE), (
        "Task36 historical heading structure"
    )
    historical_sections = tuple(line for line in history.splitlines() if line.startswith("### "))
    assert historical_sections == (
        TASK35_HEADING,
        TASK34_HEADING,
        "### Client-terminal capture and hosted baselines (superseded)",
        "### SQLite-permission capture and hosted baselines (superseded)",
        "### JSON-media capture and hosted baselines (superseded)",
        "### Terminal-retry capture and hosted baselines (superseded)",
        "### Async SQLite capture and hosted baseline (superseded)",
        "### Request-boundary capture and hosted baseline (superseded)",
        "### Immediate prior capture and hosted baseline (superseded)",
        "### Prior capture and hosted baseline (superseded)",
        "### Older hosted baseline (superseded)",
    ), "Task36 historical heading structure"
    assert history.splitlines()[2] == TASK35_HEADING, "Task36 historical heading structure"

    activation = _task36_section(
        current,
        "## Current Task36 authorization-expiry evidence activation",
        "## Current Task36 authorization-expiry proof boundary",
        "Task36 current activation facts",
    )
    _require_normalized_facts(
        activation,
        (
            TASK36_FEATURE_SOURCE,
            TASK36_CAPTURE_SOURCE,
            TASK36_CAPTURE_COMMIT,
            "`93 passed`",
            "`1022 passed`",
            "3 warnings in 26.36s",
            "19 files / 382 tests",
            "production TypeScript/Vite build",
            "Ruff",
            "strict MyPy",
            "OpenAPI verification",
            "history-aware secret scan",
            "diff check all passed",
            "public terminal-evidence tamper cases passed `3 passed`",
            "snapshot, receipt, and SSE each failed closed",
            "deterministic stub smoke passed",
            "local one-process production smoke passed",
            "Independent source/specification and security reviews passed with no P0-P3",
            "first Task36 capture audit found a P2 provenance gap",
            "fresh capture-owned temporary directory",
            "allowlisted secret-safe environment",
            "rejects Vite environment files",
            "discarded pre-fix capture is not claimed",
            "`npm run test:e2e` then reported `35 passed`",
            "Google Chrome 150.0.7871.187",
            "35 capture contracts",
            "Five PNGs changed from Task35",
            "`mobile-consent.png` was byte-identical",
            "both passed with no P0-P3 findings",
            "`missing_openai_api_key`",
            "`attempted=1` of `target=3`",
            "`approvals=0`",
            "model IDs `[]`",
            "tools `[]`",
            "`rootTraceId=null`",
            "run 30606996966",
            "job 91081238197",
            "job 91081310037",
            "run 30607860082",
            "job 91083947213",
            "job 91084012386",
            "run 30608202642",
            "job 91084965375",
            "`264 failed, 758 passed, 1 warning` in 58.16s",
            "job 91085278851",
            "No successful current Task36 hosted `verify`",
        ),
        "Task36 current activation facts",
    )
    actions_base = "https://github.com/charlie2233/backchannel-agent-support/actions/runs"
    _assert_actions_link_bindings(
        current,
        (
            ("run", "30606996966", "30606996966", None),
            ("job", "91081238197", "30606996966", "91081238197"),
            ("job", "91081310037", "30606996966", "91081310037"),
            ("run", "30607860082", "30607860082", None),
            ("job", "91083947213", "30607860082", "91083947213"),
            ("job", "91084012386", "30607860082", "91084012386"),
            ("run", "30608202642", "30608202642", None),
            ("job", "91084965375", "30608202642", "91084965375"),
            ("job", "91085278851", "30608202642", "91085278851"),
        ),
        "current hosted provenance",
    )
    assert tuple(
        re.findall(
            r"https://github\.com/[^)\s]+/actions/runs/\d+(?:/job/\d+)?",
            current,
        )
    ) == (
        f"{actions_base}/30606996966",
        f"{actions_base}/30606996966/job/91081238197",
        f"{actions_base}/30606996966/job/91081310037",
        f"{actions_base}/30607860082",
        f"{actions_base}/30607860082/job/91083947213",
        f"{actions_base}/30607860082/job/91084012386",
        f"{actions_base}/30608202642",
        f"{actions_base}/30608202642/job/91084965375",
        f"{actions_base}/30608202642/job/91085278851",
    ), "current hosted provenance"
    for forbidden_hosted_claim in FORBIDDEN_UNEXPECTED_HOSTED_CLAIMS:
        assert " ".join(forbidden_hosted_claim.split()) not in normalized_current, (
            f"current hosted provenance: {forbidden_hosted_claim}"
        )

    expiry_section = _task36_section(
        current,
        "## Current Task36 authorization-expiry proof boundary",
        "## Current Task36 pending external gates",
        "Task36 authorization-expiry semantic contract",
    )
    _require_normalized_facts(
        expiry_section,
        (
            "claimed approval retains the immutable authorization expiry",
            (
                "atomically rechecks the claimed decision, session owner, "
                "generation, policy, scope, expiry, and canonical provider result"
            ),
            (
                "`closed_without_action` + `executionStarted: false` + "
                "`authorization_expired_before_dispatch`"
            ),
            "zero provider dispatch and zero execution",
            (
                "`outcome_unknown` + `executionStarted: true` + "
                "`authorization_expired_with_unresolved_dispatch`"
            ),
            "does not infer a successful terminal provider result",
            "Cancellation after a committed claim wakes the durable lifecycle",
            "`claimed_at == scope.activated_at <= execution.created_at < expiry`",
            (
                "snapshot, receipt, and SSE readers validate the complete "
                "sealed receipt graph in one transaction"
            ),
            "fail closed on tamper or preauthorization evidence",
            "Task5 migration restores activation from durable `decision.claimed_at`",
            (
                "Typed SDK-stub and OpenAI-live responses and receipts require "
                "the truthful status/reason pairing"
            ),
            "Rolling-receipt normalization only fills an omitted `terminalReason` as null",
            "does not invent an expiry reason or provider execution",
        ),
        "Task36 authorization-expiry semantic contract",
    )

    pending = _task36_section(
        current,
        "## Current Task36 pending external gates",
        "## Per-SHA proof matrix",
        "Task36 pending external gates",
    )
    _require_normalized_facts(
        pending,
        (
            "three exact current Task36 hosted runs above are bounded failures",
            "skipped container jobs",
            (
                "successful activation `verify`, packaged `container-smoke`, "
                "and documentation-tip CI remain pending"
            ),
            "no current Task36 live OpenAI",
            "public deployment/browser URL",
            "target host durability/networking",
            "abrupt-host-loss or backup",
            "concurrent multi-container SQLite",
            "real external-provider execution",
            "tag, or release proof or claim",
        ),
        "Task36 pending external gates",
    )

    matrix = _task36_section(
        current,
        "## Per-SHA proof matrix",
        "## Hosted container evidence boundary",
        "Task36 current matrix",
    )
    rows = tuple(line.strip() for line in matrix.splitlines() if line.strip().startswith("|"))
    assert rows == (
        "| Lane | Exact evidence | Status and boundary |",
        "| --- | --- | --- |",
        (
            "| Local source and tests | Feature source `9239410…`: focused 93, "
            "public-terminal tamper 3, 19 web files / 382 tests, 1022 Python "
            "tests, build, Ruff, strict MyPy, OpenAPI, secret scan, diff, stub "
            "smoke, and local production smoke | **Verified locally** in the "
            "exact named lanes |"
        ),
        (
            "| Local final-build captures | "
            "[`docs/assets/final/manifest.json`](assets/final/manifest.json); six "
            "provenance-checked PNGs under [`docs/assets/final`](assets/final) | "
            "**Local capture evidence only** on clean manifest source `a01f0a3…`, "
            "capture commit `1030d19…`, and Google Chrome 150.0.7871.187 at "
            "1440×1024 and 390×844; not CI, container, deployment, live OpenAI, "
            "or release proof |"
        ),
        (
            "| Live OpenAI | Three-run command stopped at "
            "`missing_openai_api_key` after attempt 1 of 3, with zero approvals, "
            "model IDs `[]`, tools `[]`, and null root trace | **Blocked / "
            "Unverified**; three live runs did not execute |"
        ),
        (
            "| Local Docker | No Docker-family runtime is installed on this Mac | "
            "**Unverified locally** |"
        ),
        (
            "| GitHub CI/container | Runs `30606996966`, `30607860082`, and "
            "`30608202642`: all `verify` jobs failed; all `container-smoke` jobs "
            "were skipped; latest annotations 1 / 0 | **Failed / Unverified**; "
            "bounded nonproof only, with no current Task36 hosted canonical or "
            "packaged-container success |"
        ),
        (
            "| Public deployment | No public application origin or browser URL | "
            "**Blocked / Unverified** |"
        ),
        (
            "| Target host/durability | No target-host networking, abrupt-loss, "
            "backup, or concurrent multi-container SQLite proof | **Blocked / "
            "Unverified** |"
        ),
        ("| Tag/release | No release tag or GitHub release | **Blocked / Unverified** |"),
    ), "Task36 current matrix"

    hosted = _task36_section(
        current,
        "## Hosted container evidence boundary",
        "## Capture contract",
        "Task36 hosted evidence boundary",
    )
    _require_normalized_facts(
        hosted,
        (
            "three exact current Task36 hosted `verify` jobs failed",
            "`container-smoke` jobs were skipped",
            "latest capture-commit run reached manifest verification and the web suite",
            "did not reach the later stub, OpenAPI, or secret-scan workflow steps",
            "bounded nonproof",
            (
                "superseded Task35 hosted result below is historical evidence "
                "for its own exact SHA only"
            ),
            "not inherited by Task36",
            "do not prove packaged Docker",
            "target-host durability/networking",
            "concurrent multi-container SQLite",
            "documentation-tip CI, tag, or release",
        ),
        "Task36 hosted evidence boundary",
    )

    capture = _task36_section(
        current,
        "## Capture contract",
        "## SQLite file-permission proof boundary",
        "Task36 capture semantic contract",
    )
    _require_normalized_facts(
        capture,
        (
            "deliberately omits `OPENAI_API_KEY`",
            "rejects Vite environment files",
            "secret-safe build/server environment allowlists",
            "Google Chrome 150.0.7871.187",
            "stable `chrome` channel in headless mode",
            "builds the frontend into a newly owned temporary directory",
            "rejects a pre-existing or symlinked destination",
            "serves that exact directory",
            "rechecks that source commit and runtime input did not change",
            "stale ignored repository `web/dist` therefore cannot supply the named capture",
            TASK36_CAPTURE_SOURCE,
            TASK36_RUNTIME_DIGEST,
            "across 85 runtime paths",
            "capture profile `keyless_sdk_stub`",
            "`en-US` / `UTC` / `reduce` / `light`",
            "Feature source `9239410…`",
            "capture evidence commit `1030d19…`",
            "browser capture remained local; CI did not execute it",
        ),
        "Task36 capture semantic contract",
    )

    _assert_task36_common_boundaries(current)

    assert manifest["sourceCommit"] == TASK36_CAPTURE_SOURCE, "Task36 manifest source contract"
    assert runtime_input["digest"] == TASK36_RUNTIME_DIGEST, (
        "Task36 manifest runtime digest contract"
    )
    assert len(runtime_input["paths"]) == 85, "Task36 manifest runtime paths contract"
    assert runtime_input["paths"] == sorted(runtime_input["paths"]), (
        "Task36 manifest runtime paths contract"
    )
    assert len(runtime_input["paths"]) == len(set(runtime_input["paths"])), (
        "Task36 manifest runtime paths contract"
    )
    assert manifest["browser"] == {
        "name": "Google Chrome",
        "version": "150.0.7871.187",
        "channel": "chrome",
        "headless": True,
    }, "Task36 manifest browser contract"
    assert manifest["environment"] == {
        "locale": "en-US",
        "timezone": "UTC",
        "reducedMotion": "reduce",
        "colorScheme": "light",
    }, "Task36 manifest environment contract"
    assert tuple(
        (
            item["filename"],
            item["sha256"],
            item["state"],
            item["scenarioId"],
            item["executionMode"],
            item["width"],
            item["height"],
        )
        for item in manifest["artifacts"]
    ) == (
        (
            "desktop-consent.png",
            "94966bc9013aef5ed86e757f217b08c429c0a4783720908d154b5a6ac8bb59a5",
            "consent",
            "hotel",
            "sdk_stub",
            1440,
            1024,
        ),
        (
            "desktop-completed.png",
            "2eb641903f8fbe12f5c50e613b7b5aae1a89670e3199665da3485a16ec59db6b",
            "completed",
            "hotel",
            "sdk_stub",
            1440,
            1024,
        ),
        (
            "desktop-declined.png",
            "c5e1a2c9a9ae44750b19b7c10d957a6274e4c62795cd0a6f3c5be11af08f0427",
            "declined",
            "hotel",
            "sdk_stub",
            1440,
            1024,
        ),
        (
            "mobile-consent.png",
            "654d6ea317bd132489d7369df9699ccf7cdd0d420829a47a75e65204201dfcb8",
            "consent",
            "hotel",
            "sdk_stub",
            390,
            844,
        ),
        (
            "mobile-completed.png",
            "1254dab752c3652cb1d8148261036bbc39bfedac1ff3d3e8993de934f018f44d",
            "completed",
            "hotel",
            "sdk_stub",
            390,
            844,
        ),
        (
            "mobile-declined.png",
            "03d184a8be747dee880ba5d2a63d805335881bde7273b26b071695edb3a735f7",
            "declined",
            "hotel",
            "sdk_stub",
            390,
            844,
        ),
    ), "Task36 manifest artifacts contract"

    task35, older = history.split(TASK34_HEADING, maxsplit=1)
    normalized_task35 = " ".join(task35.split())
    for task35_fact in (
        "21d9b0f8dbeb59454e3f3b3d3d9138af28af02ad",
        "60fe1243a5eeb760984d78d667f7efdc33630adc",
        "6533a3f2e212d9a7c4e1af9f1d2dd77f7bd45a18",
        "eabc2743f9475e384fe7a49344f42a9635d2f57277f788708d23425edd845385",
        "29 capture contracts",
        "2 failed, 17 deselected",
        "DID NOT RAISE `ReceiptTransitionError`",
        "3 passed, 17 deselected",
        "20 passed",
        "951 passed",
        "957 passed",
        "Independent Task35 specification and quality reviews passed with no P0-P3",
        "one-time `terminal` add/backfill",
        "exact database-byte stability across a second reopen",
        "foreign-key check was empty",
        "integrity check returned `ok`",
        "run 30595724271",
        "job 91047571247",
        "job 91047810594",
        "run 30598406930",
        "job 91055708186",
        "job 91055968348",
        "Both job annotation APIs returned `[]`",
        "6cbafce70fa1ea60cf00e315062e4e14a5ffb934",
        "run 30603151491",
        "job 91069887646",
        "job 91070162086",
        "`992 passed, 1 warning` in 44.62s",
        "Both final-tip job annotation APIs returned `[]`",
        (
            "Task35 final documentation-tip CI passed only the exact hosted "
            "canonical and packaged-container lanes"
        ),
        (
            "Task35 retained no live OpenAI, public deployment/browser URL, "
            "local-Docker, target-host durability/networking, abrupt-host-loss "
            "or backup, concurrent multi-container SQLite, real "
            "external-provider execution, tag, or release proof or claim"
        ),
        "final documentation tip `6cbafce…`",
        "final documentation tip adds no new browser claim",
        "across 84 runtime paths",
    ):
        assert task35_fact in normalized_task35, (
            f"Task35 historical factual preservation: missing {task35_fact}"
        )
    _assert_actions_link_bindings(
        task35,
        (
            ("run", "30595724271", "30595724271", None),
            ("job", "91047571247", "30595724271", "91047571247"),
            ("job", "91047810594", "30595724271", "91047810594"),
            ("run", "30598406930", "30598406930", None),
            ("job", "91055708186", "30598406930", "91055708186"),
            ("job", "91055968348", "30598406930", "91055968348"),
            ("run", "30603151491", "30603151491", None),
            ("job", "91069887646", "30603151491", "91069887646"),
            ("job", "91070162086", "30603151491", "91070162086"),
        ),
        "Task35 historical hosted provenance",
    )
    assert tuple(
        re.findall(
            r"https://github\.com/[^)\s]+/actions/runs/\d+(?:/job/\d+)?",
            task35,
        )
    ) == (
        f"{actions_base}/30595724271",
        f"{actions_base}/30595724271/job/91047571247",
        f"{actions_base}/30595724271/job/91047810594",
        f"{actions_base}/30598406930",
        f"{actions_base}/30598406930/job/91055708186",
        f"{actions_base}/30598406930/job/91055968348",
        f"{actions_base}/30603151491",
        f"{actions_base}/30603151491/job/91069887646",
        f"{actions_base}/30603151491/job/91070162086",
    ), "Task35 historical hosted provenance"
    assert "#### Task35 SQLite file-permission proof boundary" not in task35, (
        "Task35 historical scope"
    )
    assert "#### Task35 JSON response media proof boundary" not in task35, "Task35 historical scope"
    for current_identifier in (
        TASK36_FEATURE_SOURCE,
        TASK36_CAPTURE_SOURCE,
        TASK36_CAPTURE_COMMIT,
        TASK36_RUNTIME_DIGEST,
        "9239410…",
        "a01f0a3…",
        "1030d19…",
        "168e315…",
    ):
        assert current_identifier not in history, (
            f"Task36 current identifier historical exclusion: {current_identifier}"
        )

    task33_heading = "### Client-terminal capture and hosted baselines (superseded)"
    task34 = (
        (TASK34_HEADING + older)
        .split(TASK34_HEADING, maxsplit=1)[1]
        .split(
            task33_heading,
            maxsplit=1,
        )[0]
    )
    _assert_actions_link_bindings(
        task34,
        (
            ("run", "30593858167", "30593858167", None),
            ("job", "91041814602", "30593858167", "91041814602"),
            ("job", "91042106744", "30593858167", "91042106744"),
            ("run", "30594431983", "30594431983", None),
            ("job", "91043601366", "30594431983", "91043601366"),
            ("job", "91043886907", "30594431983", "91043886907"),
        ),
        "Task34 ordered Actions bindings",
    )
    assert tuple(
        re.findall(
            r"https://github\.com/[^)\s]+/actions/runs/\d+(?:/job/\d+)?",
            task34,
        )
    ) == (
        f"{actions_base}/30593858167",
        f"{actions_base}/30593858167/job/91041814602",
        f"{actions_base}/30593858167/job/91042106744",
        f"{actions_base}/30594431983",
        f"{actions_base}/30594431983/job/91043601366",
        f"{actions_base}/30594431983/job/91043886907",
    ), "Task34 ordered Actions bindings"
    _require_normalized_facts(
        task34,
        (
            "5364dd4f9f825f5d68c85ea005ceb277ab943e4c",
            "19 web test files / 369 tests",
            "strict MyPy over 35 source files",
            "944 Python tests with 1 warning in 43.54s",
            "runtime user `10001:10001`",
            "offline network-none `deterministic-qa` and `deployed-readonly` profiles",
            "Both activation job annotation APIs returned `[]`",
            "Both final-tip job annotation APIs returned `[]`",
        ),
        "Task34 historical facts",
    )
    assert _normalized_digest(task34) == TASK34_HISTORY_DIGEST, "Task34 historical facts"

    task32_heading = "### SQLite-permission capture and hosted baselines (superseded)"
    task32_end_heading = "### JSON-media capture and hosted baselines (superseded)"
    task32 = history.split(task32_heading, maxsplit=1)[1].split(
        task32_end_heading,
        maxsplit=1,
    )[0]
    _assert_actions_link_bindings(
        task32,
        (
            ("run", "30588357735", "30588357735", None),
            ("job", "91024909140", "30588357735", "91024909140"),
            ("job", "91025281220", "30588357735", "91025281220"),
            ("run", "30589435325", "30589435325", None),
            ("job", "91028250060", "30589435325", "91028250060"),
            ("job", "91028596793", "30589435325", "91028596793"),
        ),
        "older history ordered Actions bindings",
    )
    assert tuple(
        re.findall(
            r"https://github\.com/[^)\s]+/actions/runs/\d+(?:/job/\d+)?",
            task32,
        )
    ) == (
        f"{actions_base}/30588357735",
        f"{actions_base}/30588357735/job/91024909140",
        f"{actions_base}/30588357735/job/91025281220",
        f"{actions_base}/30589435325",
        f"{actions_base}/30589435325/job/91028250060",
        f"{actions_base}/30589435325/job/91028596793",
    ), "older history ordered Actions bindings"
    _require_normalized_facts(
        task32,
        (
            "c6c60d4354eba7348aef3245d19e787661544fa7",
            "abdf644b4c35ecfaee2d22917c4c621f9d123a5b",
            "Both pre-CI jobs passed and both pre-CI job annotation APIs returned `[]`",
            "Both final-tip jobs passed and both final-tip job annotation APIs returned `[]`",
            "796 Python tests",
            "owner-only `/data` mode `0700`",
            "runtime user `10001:10001`",
            "offline network-none `deterministic-qa` and `deployed-readonly` profiles",
        ),
        "older history semantic facts",
    )
    for forbidden_history_text in (
        "The pre-CI verify job failed.",
        "The final container-smoke job was skipped.",
        "The hosted verify gate covered 344 web tests.",
        "The final hosted gate covered 795 Python tests.",
        "The local exact gate completed in 29.51s.",
    ):
        assert forbidden_history_text not in task32, (
            f"older history semantic contract: {forbidden_history_text}"
        )
    assert _normalized_digest(task32) == TASK32_HISTORY_DIGEST, "older history semantic facts"

    historical_identifiers = set(
        re.findall(
            r"(?<![0-9A-Za-z])(?:[0-9a-f]{40,64}|[0-9a-f]{7}…|\d{11})"
            r"(?![0-9A-Za-z])",
            history,
        )
    )
    for historical_identifier in historical_identifiers:
        assert historical_identifier not in current, (
            f"superseded provenance exclusion: {historical_identifier}"
        )
    for superseded_identifier in ADDITIONAL_SUPERSEDED_IDENTIFIERS:
        assert superseded_identifier not in current, (
            f"superseded provenance exclusion: {superseded_identifier}"
        )
    for old_identifier in (
        "21d9b0f8dbeb59454e3f3b3d3d9138af28af02ad",
        "60fe1243a5eeb760984d78d667f7efdc33630adc",
        "6533a3f2e212d9a7c4e1af9f1d2dd77f7bd45a18",
        "eabc2743f9475e384fe7a49344f42a9635d2f57277f788708d23425edd845385",
    ):
        assert old_identifier not in current, (
            f"Task35 superseded identifier current exclusion: {old_identifier}"
        )
    for unsupported_claim in (
        "Current Task36 hosted CI passed",
        "Current Task36 container-smoke passed",
        "Current Task36 live OpenAI passed",
        "Current Task36 public deployment passed",
        "Current Task36 release passed",
    ):
        assert unsupported_claim not in current, (
            f"Task36 unsupported positive claim: {unsupported_claim}"
        )
    normalized_current_claims = " ".join(current.split())
    for forbidden_claim in FORBIDDEN_CURRENT_POSITIVE_CLAIMS:
        assert " ".join(forbidden_claim.split()) not in normalized_current_claims, (
            f"current positive allowlist: {forbidden_claim}"
        )
    _assert_current_claim_line_allowlists(current)

    assert _normalized_digest(current) == TASK36_CURRENT_DIGEST, "Task36 current normalized digest"
    assert _normalized_digest(task35) == TASK35_HISTORY_DIGEST, (
        "Task35 historical normalized digest"
    )
    assert "not current Task35" not in older, "older historical current-task labels"
    assert older.count("not current Task36") == 7, "older historical current-task labels"
    assert _normalized_digest(TASK34_HEADING + older) == OLDER_HISTORY_DIGEST, (
        "older historical normalized digest"
    )
    assert TASK36_FEATURE_SOURCE in normalized_current


def test_task36_current_authorization_expiry_evidence_contract() -> None:
    _assert_validation_matches_current_capture(_read("docs/validation.md"))


@pytest.mark.parametrize(
    ("original", "replacement", "expected_guard"),
    (
        (
            TASK36_FEATURE_SOURCE,
            "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
            "Task36 current activation facts",
        ),
        (
            TASK36_CAPTURE_SOURCE,
            "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
            "Task36 current activation facts",
        ),
        (
            "`1022 passed`",
            "`1021 passed`",
            "Task36 current activation facts",
        ),
        (
            "19 files / 382 tests",
            "19 files / 381 tests",
            "Task36 current activation facts",
        ),
        (
            (
                "`closed_without_action` + `executionStarted: false` + "
                "`authorization_expired_before_dispatch`"
            ),
            "`completed` / `authorization_extended`",
            "Task36 authorization-expiry semantic contract",
        ),
        (
            (
                "`outcome_unknown` + `executionStarted: true` + "
                "`authorization_expired_with_unresolved_dispatch`"
            ),
            "`completed` / `dispatch_assumed`",
            "Task36 authorization-expiry semantic contract",
        ),
        (
            "`claimed_at == scope.activated_at <= execution.created_at < expiry`",
            "`execution.created_at <= expiry`",
            "Task36 authorization-expiry semantic contract",
        ),
        (
            "three exact current Task36 hosted runs above are bounded failures",
            "current Task36 hosted runs passed",
            "Task36 pending external gates",
        ),
        (
            "No successful current Task36 hosted `verify`",
            "Current Task36 hosted `verify` passed",
            "Task36 current activation facts",
        ),
        (
            "stale ignored repository `web/dist` therefore cannot supply the named capture",
            "repository `web/dist` may supply the named capture",
            "Task36 capture semantic contract",
        ),
    ),
)
def test_task36_current_fact_mutation_fails_closed(
    original: str,
    replacement: str,
    expected_guard: str,
) -> None:
    validation = _read("docs/validation.md")
    current, history = _task36_current_and_history(validation)
    pattern = re.compile(re.escape(original).replace(r"\ ", r"\s+"))
    assert pattern.search(current)
    polluted, replacement_count = pattern.subn(replacement, current, count=1)
    assert replacement_count == 1
    if replacement.startswith("deadbeef"):
        expected_guard = rf"(?:provenance token allowlist|{expected_guard})"
    with pytest.raises(AssertionError, match=expected_guard):
        _assert_validation_matches_current_capture(polluted + HISTORICAL_HEADING + history)


@pytest.mark.parametrize(
    "unsupported_claim",
    (
        "Current Task36 hosted CI passed",
        "Current Task36 container-smoke passed",
        "Current Task36 live OpenAI passed",
        "Current Task36 public deployment passed",
        "Current Task36 release passed",
    ),
)
def test_task36_rejects_unobserved_positive_claim(unsupported_claim: str) -> None:
    validation = _read("docs/validation.md")
    polluted = validation.replace(
        HISTORICAL_HEADING,
        f"{unsupported_claim}\n\n{HISTORICAL_HEADING}",
        1,
    )
    with pytest.raises(AssertionError, match="Task36 unsupported positive claim"):
        _assert_validation_matches_current_capture(polluted)


def test_task36_rejects_duplicate_current_matrix_and_history_reordering() -> None:
    validation = _read("docs/validation.md")
    duplicate_matrix = validation.replace(
        HISTORICAL_HEADING,
        f"## Per-SHA proof matrix\n\n{HISTORICAL_HEADING}",
        1,
    )
    with pytest.raises(AssertionError, match="Task36 document structure"):
        _assert_validation_matches_current_capture(duplicate_matrix)

    reordered = validation.replace(
        TASK35_HEADING,
        "### Task35 moved after Task34 (superseded)",
        1,
    )
    with pytest.raises(AssertionError, match="Task36 historical heading structure"):
        _assert_validation_matches_current_capture(reordered)


@pytest.mark.parametrize(
    ("original", "replacement"),
    (
        ("957 passed, 1 warning", "956 passed, 2 warnings"),
        ("91069887646", "99999999998"),
        (
            "Both final-tip job annotation APIs returned `[]`",
            "Final-tip annotations were not inspected",
        ),
    ),
)
def test_task35_historical_fact_mutation_fails_closed(
    original: str,
    replacement: str,
) -> None:
    validation = _read("docs/validation.md")
    current, history = _task36_current_and_history(validation)
    task35, older = history.split(TASK34_HEADING, maxsplit=1)
    pattern = re.compile(re.escape(original).replace(r"\ ", r"\s+"))
    assert pattern.search(task35)
    polluted_task35, replacement_count = pattern.subn(
        replacement,
        task35,
        count=1,
    )
    assert replacement_count == 1
    polluted_history = polluted_task35 + TASK34_HEADING + older
    expected_guard = r"Task35 historical (?:factual preservation|normalized digest)"
    if original.isdigit():
        expected_guard = rf"(?:Actions Markdown label binding|{expected_guard})"
    with pytest.raises(AssertionError, match=expected_guard):
        _assert_validation_matches_current_capture(current + HISTORICAL_HEADING + polluted_history)


def _replace_whitespace_flexible_once(
    text: str,
    original: str,
    replacement: str,
) -> str:
    pattern = re.compile(re.escape(original).replace(r"\ ", r"\s+"))
    assert pattern.search(text)
    mutated, count = pattern.subn(replacement, text, count=1)
    assert count == 1
    return mutated


def _task36_current_slice(validation: str, slice_name: str) -> tuple[str, str, str]:
    current, history = _task36_current_and_history(validation)
    boundaries = {
        "activation": (
            "## Current Task36 authorization-expiry evidence activation",
            "## Current Task36 authorization-expiry proof boundary",
        ),
        "expiry": (
            "## Current Task36 authorization-expiry proof boundary",
            "## Current Task36 pending external gates",
        ),
        "pending": (
            "## Current Task36 pending external gates",
            "## Per-SHA proof matrix",
        ),
        "matrix": (
            "## Per-SHA proof matrix",
            "## Hosted container evidence boundary",
        ),
        "hosted": (
            "## Hosted container evidence boundary",
            "## Capture contract",
        ),
        "capture": (
            "## Capture contract",
            "## SQLite file-permission proof boundary",
        ),
        "async": (
            "## Async SQLite proof boundary",
            "## Request-body availability proof boundary",
        ),
        "request": (
            "## Request-body availability proof boundary",
            "## Creation-admission proof boundary",
        ),
        "creation": (
            "## Creation-admission proof boundary",
            "## Live deadline proof boundary",
        ),
    }
    start, end = boundaries[slice_name]
    prefix, remainder = current.split(start, maxsplit=1)
    body, suffix = remainder.split(end, maxsplit=1)
    return prefix + start, body, end + suffix + HISTORICAL_HEADING + history


def test_task36_current_evidence_and_task35_history_contract() -> None:
    # Retained function name preserves the prior collection surface; Task36 is
    # now current and Task35 is verified as the first historical section.
    _assert_validation_matches_current_capture(_read("docs/validation.md"))


def test_validation_normalized_digests_match_current_document() -> None:
    validation = _read("docs/validation.md")
    current, history = _task36_current_and_history(validation)
    task35, older = history.split(TASK34_HEADING, maxsplit=1)
    assert _normalized_digest(current) == TASK36_CURRENT_DIGEST
    assert _normalized_digest(task35) == TASK35_HISTORY_DIGEST
    assert _normalized_digest(TASK34_HEADING + older) == OLDER_HISTORY_DIGEST


@pytest.mark.parametrize(
    ("needle", "expected_guard"),
    (
        (
            "This file records only `codex/backchannel-v0.3` evidence.",
            "Task36 current normalized digest",
        ),
        (
            "Reopen performs no repair and preserves exact event-row",
            "Task35 historical normalized digest",
        ),
        (
            "The prior Task33 source gate covered",
            "older historical normalized digest",
        ),
    ),
)
def test_validation_normalized_digest_rejects_section_mutation(
    needle: str,
    expected_guard: str,
) -> None:
    validation = _read("docs/validation.md")
    assert needle in validation
    polluted = validation.replace(needle, f"{needle} mutated", 1)
    with pytest.raises(AssertionError, match=expected_guard):
        _assert_validation_matches_current_capture(polluted)


@pytest.mark.parametrize(
    "history_heading",
    (
        TASK35_HEADING,
        TASK34_HEADING,
    ),
)
@pytest.mark.parametrize(
    "current_identifier",
    (
        TASK36_FEATURE_SOURCE,
        "9239410…",
        TASK36_CAPTURE_SOURCE,
        "a01f0a3…",
        TASK36_CAPTURE_COMMIT,
        "1030d19…",
        TASK36_RUNTIME_DIGEST,
        "168e315…",
    ),
)
def test_validation_current_identifier_loop_rejects_history_body_leakage(
    history_heading: str,
    current_identifier: str,
) -> None:
    validation = _read("docs/validation.md")
    polluted = validation.replace(
        history_heading,
        f"{history_heading}\n\nInjected current provenance: {current_identifier}",
        1,
    )
    with pytest.raises(
        AssertionError,
        match=(
            r"(?:provenance token allowlist|"
            r"Task36 current identifier historical exclusion)"
        ),
    ):
        _assert_validation_matches_current_capture(polluted)


@pytest.mark.parametrize(
    ("slice_name", "original", "replacement", "expected_guard"),
    (
        (
            "activation",
            TASK36_FEATURE_SOURCE,
            "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
            "Task36 current activation facts",
        ),
        (
            "activation",
            TASK36_CAPTURE_SOURCE,
            "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
            "Task36 current activation facts",
        ),
        (
            "activation",
            TASK36_CAPTURE_COMMIT,
            "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
            "Task36 current activation facts",
        ),
        (
            "activation",
            "`93 passed`",
            "`92 passed`",
            "Task36 current activation facts",
        ),
        (
            "activation",
            "`1022 passed`",
            "`1021 passed`",
            "Task36 current activation facts",
        ),
        (
            "activation",
            "3 warnings in 26.36s",
            "4 warnings in 27.36s",
            "Task36 current activation facts",
        ),
        (
            "activation",
            "19 files / 382 tests",
            "19 files / 381 tests",
            "Task36 current activation facts",
        ),
        (
            "activation",
            "public terminal-evidence tamper cases passed `3 passed`",
            "public terminal evidence was not checked",
            "Task36 current activation facts",
        ),
        (
            "activation",
            "Independent source/specification and security reviews passed with no P0-P3",
            "reviews had unresolved findings",
            "Task36 current activation facts",
        ),
        (
            "activation",
            "first Task36 capture audit found a P2 provenance gap",
            "first capture audit was clean",
            "Task36 current activation facts",
        ),
        (
            "activation",
            "rejects Vite environment files",
            "loads Vite environment files",
            "Task36 current activation facts",
        ),
        (
            "activation",
            "`npm run test:e2e` then reported `35 passed`",
            "`npm run test:e2e` reported `34 passed`",
            "Task36 current activation facts",
        ),
        (
            "activation",
            "Google Chrome 150.0.7871.187",
            "Chromium 150",
            "Task36 current activation facts",
        ),
        (
            "activation",
            "`mobile-consent.png` was byte-identical",
            "`mobile-consent.png` was not reviewed",
            "Task36 current activation facts",
        ),
        (
            "activation",
            "`missing_openai_api_key`",
            "`live_provider_passed`",
            "Task36 current activation facts",
        ),
        (
            "activation",
            "run 30606996966",
            "run 99999999991",
            "Task36 current activation facts",
        ),
        (
            "activation",
            "job 91081238197",
            "job 99999999992",
            "Task36 current activation facts",
        ),
        (
            "activation",
            "run 30607860082",
            "run 99999999993",
            "Task36 current activation facts",
        ),
        (
            "activation",
            "job 91083947213",
            "job 99999999994",
            "Task36 current activation facts",
        ),
        (
            "activation",
            "run 30608202642",
            "run 99999999995",
            "Task36 current activation facts",
        ),
        (
            "activation",
            "job 91084965375",
            "job 99999999996",
            "Task36 current activation facts",
        ),
        (
            "activation",
            "`264 failed, 758 passed, 1 warning` in 58.16s",
            "`1022 passed`",
            "Task36 current activation facts",
        ),
        (
            "activation",
            "job 91085278851",
            "job 99999999997",
            "Task36 current activation facts",
        ),
        (
            "expiry",
            (
                "`closed_without_action` + `executionStarted: false` + "
                "`authorization_expired_before_dispatch`"
            ),
            "`completed` + `executionStarted: true`",
            "Task36 authorization-expiry semantic contract",
        ),
        (
            "expiry",
            (
                "`outcome_unknown` + `executionStarted: true` + "
                "`authorization_expired_with_unresolved_dispatch`"
            ),
            "`completed` + `executionStarted: false`",
            "Task36 authorization-expiry semantic contract",
        ),
        (
            "expiry",
            "`claimed_at == scope.activated_at <= execution.created_at < expiry`",
            "`execution.created_at <= expiry`",
            "Task36 authorization-expiry semantic contract",
        ),
        (
            "capture",
            "stale ignored repository `web/dist` therefore cannot supply the named capture",
            "repository `web/dist` may supply the named capture",
            "Task36 capture semantic contract",
        ),
        (
            "matrix",
            "latest annotations 1 / 0",
            "latest annotations 0 / 0",
            "Task36 current matrix",
        ),
        (
            "hosted",
            "bounded nonproof",
            "hosted success",
            "Task36 hosted evidence boundary",
        ),
        (
            "pending",
            "documentation-tip CI remain pending",
            "documentation-tip CI passed",
            "Task36 pending external gates",
        ),
    ),
)
def test_validation_rejects_current_activation_fact_mutation(
    slice_name: str,
    original: str,
    replacement: str,
    expected_guard: str,
) -> None:
    prefix, body, suffix = _task36_current_slice(
        _read("docs/validation.md"),
        slice_name,
    )
    polluted_body = _replace_whitespace_flexible_once(body, original, replacement)
    if original.startswith(("run ", "job ")):
        expected_guard = rf"(?:Actions Markdown label binding|{expected_guard})"
    if replacement.startswith("deadbeef"):
        expected_guard = rf"(?:provenance token allowlist|{expected_guard})"
    with pytest.raises(AssertionError, match=expected_guard):
        _assert_validation_matches_current_capture(prefix + polluted_body + suffix)


@pytest.mark.parametrize(
    ("label", "replacement"),
    (
        ("[run 30606996966]", "[run 99999999990]"),
        ("[job 91081238197]", "[job 99999999991]"),
        ("[job 91069887646]", "[job 99999999992]"),
        ("[job 91041814602]", "[job 99999999993]"),
        ("[job 91024909140]", "[job 99999999994]"),
    ),
)
def test_actions_markdown_label_only_mutation_fails_closed(
    label: str,
    replacement: str,
) -> None:
    validation = _read("docs/validation.md")
    assert validation.count(label) == 1
    polluted = validation.replace(label, replacement, 1)
    with pytest.raises(
        AssertionError,
        match="Actions Markdown label binding",
    ):
        _assert_validation_matches_current_capture(polluted)


@pytest.mark.parametrize(
    ("url", "replacement"),
    (
        (
            ("https://github.com/charlie2233/backchannel-agent-support/actions/runs/30606996966)"),
            ("https://github.com/charlie2233/backchannel-agent-support/actions/runs/99999999990)"),
        ),
        (
            (
                "https://github.com/charlie2233/backchannel-agent-support/"
                "actions/runs/30606996966/job/91081238197"
            ),
            (
                "https://github.com/charlie2233/backchannel-agent-support/"
                "actions/runs/30606996966/job/99999999991"
            ),
        ),
        (
            (
                "https://github.com/charlie2233/backchannel-agent-support/"
                "actions/runs/30603151491/job/91069887646"
            ),
            (
                "https://github.com/charlie2233/backchannel-agent-support/"
                "actions/runs/30603151491/job/99999999992"
            ),
        ),
        (
            (
                "https://github.com/charlie2233/backchannel-agent-support/"
                "actions/runs/30593858167/job/91041814602"
            ),
            (
                "https://github.com/charlie2233/backchannel-agent-support/"
                "actions/runs/30593858167/job/99999999993"
            ),
        ),
        (
            (
                "https://github.com/charlie2233/backchannel-agent-support/"
                "actions/runs/30588357735/job/91024909140"
            ),
            (
                "https://github.com/charlie2233/backchannel-agent-support/"
                "actions/runs/30588357735/job/99999999994"
            ),
        ),
    ),
)
def test_actions_markdown_url_only_mutation_fails_closed(
    url: str,
    replacement: str,
) -> None:
    validation = _read("docs/validation.md")
    assert validation.count(url) == 1
    polluted = validation.replace(url, replacement, 1)
    with pytest.raises(
        AssertionError,
        match="Actions Markdown label binding",
    ):
        _assert_validation_matches_current_capture(polluted)


@pytest.mark.parametrize(
    ("original", "replacement"),
    (
        (
            "[run 30606996966](https://github.com/",
            "[run 30606996966](https://evil.example/",
        ),
        (
            "[job 91081238197]",
            "[job 91081310037]",
        ),
        (
            "actions/runs/30606996966/job/91081238197)",
            "actions/runs/30606996966/job/91081238197?view=1)",
        ),
        (
            "actions/runs/30606996966/job/91081238197)",
            "actions/workflows/30606996966/job/91081238197)",
        ),
        (
            "[job 91081238197]",
            "[JOB 91081238197]",
        ),
    ),
)
def test_actions_markdown_domain_path_and_binding_mutations_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    original: str,
    replacement: str,
) -> None:
    validation = _read("docs/validation.md")
    current, history = _task36_current_and_history(validation)
    assert current.count(original) == 1
    polluted_current = current.replace(original, replacement, 1)
    monkeypatch.setitem(
        globals(),
        "TASK36_CURRENT_DIGEST",
        _normalized_digest(polluted_current),
    )
    with pytest.raises(
        AssertionError,
        match="Actions Markdown label binding",
    ):
        _assert_validation_matches_current_capture(polluted_current + HISTORICAL_HEADING + history)


@pytest.mark.parametrize(
    ("injection", "expected_guard"),
    (
        (
            (
                "Additional evidence: [job 999998]"
                "(https://evil.example/actions/runs/999999/job/999998)."
            ),
            "Actions Markdown label binding",
        ),
        (
            "Additional evidence job 999998 from run 999999.",
            "Actions provenance identifier allowlist",
        ),
        (
            ("Additional source evidence: deadbeefdeadbeefdeadbeefdeadbeefdeadbeef."),
            "provenance token allowlist",
        ),
        (
            ("Additional source evidence: DEADBEEFDEADBEEFDEADBEEFDEADBEEFDEADBEEF."),
            "provenance token allowlist",
        ),
        (
            "Additional source evidence: deadbee….",
            "provenance token allowlist",
        ),
        (
            f"Repeated source evidence: {TASK36_FEATURE_SOURCE}.",
            "provenance token allowlist",
        ),
        (
            "Repeated source evidence: 9239410….",
            "provenance token allowlist",
        ),
        (
            "Repeated run 30606996966.",
            "Actions provenance identifier allowlist",
        ),
        (
            "Repeated job 91081238197.",
            "Actions provenance identifier allowlist",
        ),
        (
            "Target-host backup evidence: 9239410….",
            "provenance token allowlist",
        ),
        (
            "Abrupt-loss evidence job 91081238197.",
            "Actions provenance identifier allowlist",
        ),
    ),
)
def test_unknown_provenance_with_recomputed_digest_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    injection: str,
    expected_guard: str,
) -> None:
    validation = _read("docs/validation.md")
    current, history = _task36_current_and_history(validation)
    polluted_current = f"{current.rstrip()}\n\n{injection}\n\n"
    monkeypatch.setitem(
        globals(),
        "TASK36_CURRENT_DIGEST",
        _normalized_digest(polluted_current),
    )
    with pytest.raises(AssertionError, match=expected_guard):
        _assert_validation_matches_current_capture(polluted_current + HISTORICAL_HEADING + history)


@pytest.mark.parametrize(
    ("injection", "expected_guard"),
    (
        (
            "The Task36 workflow completed successfully.",
            "current hosted provenance and scope allowlist",
        ),
        (
            "All hosted validation is successful.",
            "current positive allowlist",
        ),
        (
            "Task36 is ready for release.",
            "current hosted provenance and scope allowlist",
        ),
        (
            "Everything completed cleanly.",
            "current positive allowlist",
        ),
    ),
)
def test_current_claim_line_allowlists_survive_digest_recomputation(
    monkeypatch: pytest.MonkeyPatch,
    injection: str,
    expected_guard: str,
) -> None:
    validation = _read("docs/validation.md")
    current, history = _task36_current_and_history(validation)
    polluted_current = f"{current.rstrip()}\n\n{injection}\n\n"
    monkeypatch.setitem(
        globals(),
        "TASK36_CURRENT_DIGEST",
        _normalized_digest(polluted_current),
    )
    with pytest.raises(AssertionError, match=expected_guard):
        _assert_validation_matches_current_capture(polluted_current + HISTORICAL_HEADING + history)


def test_provenance_boundary_decoys_do_not_match() -> None:
    validation = _read("docs/validation.md")
    decoys = (
        "\nNeutral metadata: "
        "xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefy; "
        "_deadbeefdeadbeefdeadbeefdeadbeefdeadbeef_; "
        "runner 999999; jobless 999998; "
        "https://example.test/actions-not/runs/999997.\n"
    )
    _assert_all_actions_links_correlated(validation + decoys)


@pytest.mark.parametrize(
    ("start", "end", "first_job", "second_job", "expected_guard"),
    (
        (
            "## Current Task36 authorization-expiry evidence activation",
            "## Current Task36 authorization-expiry proof boundary",
            "91081238197",
            "91081310037",
            "current hosted provenance",
        ),
        (
            TASK35_HEADING,
            TASK34_HEADING,
            "91069887646",
            "91070162086",
            "Task35 historical hosted provenance",
        ),
        (
            TASK34_HEADING,
            "### Client-terminal capture and hosted baselines (superseded)",
            "91041814602",
            "91042106744",
            "Task34 ordered Actions bindings",
        ),
        (
            "### SQLite-permission capture and hosted baselines (superseded)",
            "### JSON-media capture and hosted baselines (superseded)",
            "91024909140",
            "91025281220",
            "older history ordered Actions bindings",
        ),
    ),
)
def test_actions_markdown_per_section_job_swap_fails_closed(
    start: str,
    end: str,
    first_job: str,
    second_job: str,
    expected_guard: str,
) -> None:
    validation = _read("docs/validation.md")
    prefix, remainder = validation.split(start, maxsplit=1)
    section, suffix = remainder.split(end, maxsplit=1)
    assert section.count(first_job) >= 2
    assert section.count(second_job) >= 2
    swapped = (
        section.replace(first_job, "__FIRST_ACTIONS_JOB__")
        .replace(second_job, first_job)
        .replace("__FIRST_ACTIONS_JOB__", second_job)
    )
    assert swapped != section
    with pytest.raises(
        AssertionError,
        match=rf"(?:Actions Markdown label binding|{expected_guard})",
    ):
        _assert_validation_matches_current_capture(prefix + start + swapped + end + suffix)


@pytest.mark.parametrize(
    ("slice_name", "original", "replacement", "expected_guard"),
    (
        (
            "activation",
            "`1022 passed`",
            "`1021 passed`",
            "Task36 current activation facts",
        ),
        (
            "expiry",
            "zero provider dispatch and zero execution",
            "one provider dispatch",
            "Task36 authorization-expiry semantic contract",
        ),
        (
            "pending",
            "documentation-tip CI remain pending",
            "documentation-tip CI passed",
            "Task36 pending external gates",
        ),
        (
            "hosted",
            "bounded nonproof",
            "hosted success",
            "Task36 hosted evidence boundary",
        ),
        (
            "capture",
            "rejects Vite environment files",
            "loads Vite environment files",
            "Task36 capture semantic contract",
        ),
    ),
)
def test_current_dedicated_guard_is_nonvacuous(
    monkeypatch: pytest.MonkeyPatch,
    slice_name: str,
    original: str,
    replacement: str,
    expected_guard: str,
) -> None:
    prefix, body, suffix = _task36_current_slice(
        _read("docs/validation.md"),
        slice_name,
    )
    polluted = (
        prefix
        + _replace_whitespace_flexible_once(
            body,
            original,
            replacement,
        )
        + suffix
    )
    original_require = _require_normalized_facts

    def bypass_one_guard(section: str, facts: tuple[str, ...], guard: str) -> None:
        if guard != expected_guard:
            original_require(section, facts, guard)

    monkeypatch.setitem(globals(), "_require_normalized_facts", bypass_one_guard)
    try:
        _assert_validation_matches_current_capture(polluted)
    except AssertionError as error:
        assert not str(error).startswith(expected_guard), (
            f"dedicated guard {expected_guard} was vacuous"
        )
    else:
        raise AssertionError(f"dedicated guard {expected_guard} was vacuous")


@pytest.mark.parametrize(
    ("bullet_prefix", "original", "replacement"),
    (
        (
            "The focused regression was first observed RED",
            "2 failed, 17 deselected",
            "1 failed, 18 deselected",
        ),
        (
            "The focused transition-marker gate then passed",
            "3 passed, 17 deselected",
            "2 passed, 18 deselected",
        ),
        ("The full Python suite passed", "951 passed", "950 passed"),
        (
            "Independent Task35 specification and quality reviews",
            "passed with no P0-P3 findings",
            "had unresolved findings",
        ),
    ),
)
def test_validation_rejects_evidence_timing_mutation(
    bullet_prefix: str,
    original: str,
    replacement: str,
) -> None:
    validation = _read("docs/validation.md")
    current, history = _task36_current_and_history(validation)
    task35, older = history.split(TASK34_HEADING, maxsplit=1)
    bullet = re.search(
        rf"^- {re.escape(bullet_prefix)}.*?(?=^- |\n#### |\Z)",
        task35,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert bullet is not None
    mutated_bullet = _replace_whitespace_flexible_once(
        bullet.group(0),
        original,
        replacement,
    )
    polluted_task35 = task35.replace(bullet.group(0), mutated_bullet, 1)
    with pytest.raises(
        AssertionError,
        match=r"Task35 historical (?:factual preservation|normalized digest)",
    ):
        _assert_validation_matches_current_capture(
            current + HISTORICAL_HEADING + polluted_task35 + TASK34_HEADING + older
        )


@pytest.mark.parametrize(
    ("original", "replacement"),
    (
        ("clearing the sole `terminal = 1` marker", "retaining a terminal marker"),
        (
            "fail closed with `ReceiptTransitionError`",
            "reopens successfully",
        ),
        ("Reopen performs no repair", "Reopen repairs the marker"),
        (
            "exact event-row and text-byte stability",
            "event rows may change",
        ),
        ("one-time `terminal` add/backfill", "repeated schema rebuilds"),
        ("exact database-byte stability", "database bytes may change"),
        (
            "foreign-key check was empty and the integrity check returned `ok`",
            "database checks were not observed",
        ),
    ),
)
def test_validation_transition_marker_guard_rejects_contract_mutation(
    original: str,
    replacement: str,
) -> None:
    validation = _read("docs/validation.md")
    current, history = _task36_current_and_history(validation)
    task35, older = history.split(TASK34_HEADING, maxsplit=1)
    polluted_task35 = _replace_whitespace_flexible_once(
        task35,
        original,
        replacement,
    )
    with pytest.raises(
        AssertionError,
        match=r"Task35 historical (?:factual preservation|normalized digest)",
    ):
        _assert_validation_matches_current_capture(
            current + HISTORICAL_HEADING + polluted_task35 + TASK34_HEADING + older
        )


@pytest.mark.parametrize(
    "contradiction",
    (
        "Terminal steps 0 through 4 are accepted.",
        "`completed` at `currentStep: 2` is valid.",
        "Nonterminal snapshots are forced to `currentStep: 5`.",
        "Invalid transitions may update the event ledger.",
        "Malformed legacy rows are rewritten.",
        "OpenAPI allows any terminal currentStep.",
    ),
)
def test_validation_rejects_transition_marker_contradictions(
    contradiction: str,
) -> None:
    validation = _read("docs/validation.md")
    current, history = _task36_current_and_history(validation)
    task35, older = history.split(TASK34_HEADING, maxsplit=1)
    polluted_task35 = task35 + f"\n{contradiction}\n"
    with pytest.raises(AssertionError, match="Task35 historical normalized digest"):
        _assert_validation_matches_current_capture(
            current + HISTORICAL_HEADING + polluted_task35 + TASK34_HEADING + older
        )


@pytest.mark.parametrize(
    ("slice_name", "replacement", "expected_guard"),
    (
        (
            "async",
            "Current Task36 hosted CI and public deployment succeeded.",
            "Async SQLite semantic contract",
        ),
        (
            "request",
            "Request bodies are accepted without bounds.",
            "request-body availability semantic contract",
        ),
        (
            "creation",
            "Creation admission is in-memory and unbounded.",
            "creation-admission semantic contract",
        ),
    ),
)
def test_task36_retained_boundary_body_replacement_fails_closed(
    slice_name: str,
    replacement: str,
    expected_guard: str,
) -> None:
    prefix, _body, suffix = _task36_current_slice(
        _read("docs/validation.md"),
        slice_name,
    )
    with pytest.raises(
        AssertionError,
        match=rf"(?:provenance token allowlist|{expected_guard})",
    ):
        _assert_validation_matches_current_capture(prefix + f"\n\n{replacement}\n\n" + suffix)


def test_release_docs_contract_forbids_disabled_test_functions() -> None:
    source = _read("tests/scripts/test_release_docs_contract.py")
    disabled_assignment = ".__test__" + " = False"
    assert disabled_assignment not in source
    disabled = tuple(
        name
        for name, value in globals().items()
        if name.startswith("test_") and getattr(value, "__test__", True) is False
    )
    assert disabled == ()
    module = ast.parse(source)
    definition_names = tuple(
        node.name
        for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
    duplicate_definitions = tuple(
        sorted(name for name in set(definition_names) if definition_names.count(name) > 1)
    )
    assert duplicate_definitions == ()
    reference_definitions = tuple(name for name in definition_names if name.endswith("_reference"))
    assert reference_definitions == ()

    functions = {
        node.name: node
        for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    call_graph = {
        name: {
            candidate.id
            for candidate in ast.walk(node)
            if isinstance(candidate, ast.Name)
            and isinstance(candidate.ctx, ast.Load)
            and candidate.id in functions
        }
        for name, node in functions.items()
    }
    reachable: set[str] = set()
    pending = [name for name in functions if name.startswith("test_")]
    while pending:
        name = pending.pop()
        if name in reachable:
            continue
        reachable.add(name)
        pending.extend(call_graph[name] - reachable)
    unreachable_private_helpers = tuple(
        sorted(name for name in functions if name.startswith("_") and name not in reachable)
    )
    assert unreachable_private_helpers == ()


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

    with pytest.raises(AssertionError, match="SQLite permission semantic contract"):
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
            "missing, wrong, malformed, comma-joined, duplicate parameters, and split-quote values",
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
            "ordinary `500` handling and the Task30 exact `503` retry contract remain preserved",
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

    with pytest.raises(AssertionError, match="JSON media semantic contract"):
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

    with pytest.raises(AssertionError, match="terminal retry semantic contract"):
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
    assert "pretest:e2e" not in scripts
    assert scripts["test:e2e"] == ("npm run test:capture-contract && node e2e/judge-flow.mjs")
    assert scripts["capture:judge"] == "npm run test:e2e"
    assert scripts["check"] == (
        "npm run test:capture-contract && npm run capture:verify && "
        "npm --workspace web run check && uv run ruff check server scripts tests && "
        "uv run mypy server scripts/start.py scripts/docker_smoke.py "
        "scripts/export_openapi.py scripts/secret_scan.py && uv run pytest -q"
    )
    assert ("      - name: Run canonical checks\n        run: npm run check\n") in workflow

    ignored = _read(".gitignore").splitlines()
    assert "output/playwright/" in ignored
    assert "docs/assets/final/" not in ignored


def test_capture_runner_is_final_build_chrome_only_and_never_added_to_ci() -> None:
    runner = _read("e2e/judge-flow.mjs")
    workflow = _read(".github/workflows/ci.yml")

    assert 'from "playwright-core"' in runner
    assert 'channel: "chrome"' in runner
    assert "headless: true" in runner
    assert 'new URL("http://127.0.0.1' in runner
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
    assert 'locale: "en-US"' in runner
    assert 'timezoneId: "UTC"' in runner
    assert 'colorScheme: "light"' in runner
    assert 'reducedMotion: "reduce"' in runner
    assert 'page.route("**/*"' in runner
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
    assert 'credentials: "same-origin"' in runner
    assert "assertResetResponse" in runner
    assert "sessionStorage.clear()" in runner
    assert 'getByRole("link", { name: "Review exact remedy" })' in runner
    assert 'getByRole("button", { name: "Approve remedy" })' in runner
    assert 'getByRole("button", { name: "Decline" })' in runner
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
    assert first_screen.index("Google Chrome Stable") < first_screen.index("npm run capture:judge")
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
    assert "## Current Task36 authorization-expiry evidence activation" in validation
    assert TASK35_HEADING in validation
    assert "## SQLite file-permission proof boundary" in validation
    assert "## JSON response media proof boundary" in validation
    assert "## Superseded historical hosted baseline" in validation
    assert "### JSON-media capture and hosted baselines (superseded)" in validation
    assert "### Terminal-retry capture and hosted baselines (superseded)" in validation
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
        assert f"docs/assets/final/{final_name}` ↔ `docs/design/{concept_name}" in ledger
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
