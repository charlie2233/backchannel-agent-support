from __future__ import annotations

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


def test_task35_current_transition_marker_evidence_contract() -> None:
    validation = _read("docs/validation.md")
    historical_heading = "## Superseded historical hosted baseline"
    current_evidence, historical_evidence = validation.split(
        historical_heading,
        maxsplit=1,
    )
    normalized_current_evidence = " ".join(current_evidence.split())

    for required_current_fact in (
        "## Current Task35 final evidence activation",
        "`21d9b0f8dbeb59454e3f3b3d3d9138af28af02ad`",
        "`60fe1243a5eeb760984d78d667f7efdc33630adc`",
        "`6533a3f2e212d9a7c4e1af9f1d2dd77f7bd45a18`",
        "`eabc2743f9475e384fe7a49344f42a9635d2f57277f788708d23425edd845385`",
        "2 failed, 17 deselected",
        "DID NOT RAISE `ReceiptTransitionError`",
        "3 passed, 17 deselected",
        "20 passed",
        "951 passed",
        "24.57s",
        "exact event-row and text-byte stability",
        "one-time `terminal` add/backfill",
        "exactly one final marker",
        "exact unrelated-row preservation",
        "exact database-byte stability across a second reopen",
        "foreign-key check was empty",
        "integrity check returned `ok`",
        "Independent Task35 specification and quality reviews passed with no P0-P3",
        "explicit second-reopen proof",
        "19 web test files / 369 tests",
        "strict MyPy over 35 source files",
        "`957 passed`, 3 warnings, in 25.36s",
        "local deterministic stub smoke reported `PASS`",
        "local production `deterministic-qa` smoke reported `PASS`",
        "`missing_openai_api_key`",
        "`attempted=1` of `target=3`",
        "`approvals=0`",
        "30598406930",
        "91055708186",
        "957 passed, 1 warning",
        "46.45s",
        "91055968348",
        "runtime user `10001:10001`",
        "offline network-none `deterministic-qa` and `deployed-readonly` profiles",
        "Both job annotation APIs returned `[]`",
    ):
        assert required_current_fact in normalized_current_evidence

    actions_base = (
        "https://github.com/charlie2233/backchannel-agent-support/actions/runs"
    )
    assert tuple(
        re.findall(
            r"https://github\.com/[^)\s]+/actions/runs/\d+(?:/job/\d+)?",
            current_evidence,
        )
    ) == (
        f"{actions_base}/30595724271",
        f"{actions_base}/30595724271/job/91047571247",
        f"{actions_base}/30595724271/job/91047810594",
        f"{actions_base}/30598406930",
        f"{actions_base}/30598406930/job/91055708186",
        f"{actions_base}/30598406930/job/91055968348",
    )
    for forbidden_current_fact in (
        "8942517f43045a124e11a8e79296f6e4de875936",
        "4ca13252e05357ff98023f9290375cd1718bcce6",
        "e208c767646af920d90aa9396f441fb328c999c3235aff646ac20a5611543f27",
        "e208c76…",
        "5364dd4f9f825f5d68c85ea005ceb277ab943e4c",
        "9d7e850cbf6761524e9a51359801944cdf7fabf4",
        "30593858167",
        "91041814602",
        "91042106744",
        "30594431983",
        "91043601366",
        "91043886907",
    ):
        assert forbidden_current_fact not in current_evidence

    task34_heading = (
        "### Task34 terminal-snapshot capture and hosted baselines (superseded)"
    )
    task33_heading = "### Client-terminal capture and hosted baselines (superseded)"
    assert historical_evidence.index(task34_heading) < historical_evidence.index(
        task33_heading
    )
    task34_history = historical_evidence.split(task34_heading, maxsplit=1)[1].split(
        task33_heading,
        maxsplit=1,
    )[0]
    normalized_task34_history = " ".join(task34_history.split())
    for required_task34_fact in (
        "8942517f43045a124e11a8e79296f6e4de875936",
        "4ca13252e05357ff98023f9290375cd1718bcce6",
        "e208c767646af920d90aa9396f441fb328c999c3235aff646ac20a5611543f27",
        "5364dd4f9f825f5d68c85ea005ceb277ab943e4c",
        "9d7e850cbf6761524e9a51359801944cdf7fabf4",
        "30593858167",
        "91041814602",
        "91042106744",
        "30594431983",
        "91043601366",
        "91043886907",
        "944 Python tests with 1 warning in 43.54s",
        "948 Python tests with 1 warning in 42.45s",
        "Both activation job annotation APIs returned `[]`",
        "Both final-tip job annotation APIs returned `[]`",
        "935 / 935 tests with 3 warnings in 24.53s",
        "944 / 944 Python tests with 3 warnings in 25.73s",
        "missing-`OPENAI_API_KEY` preflight",
    ):
        assert required_task34_fact in normalized_task34_history
    _assert_validation_normalized_digests(validation)
    _assert_validation_matches_current_capture(validation)


def _normalized_digest(evidence: str) -> str:
    return hashlib.sha256(" ".join(evidence.split()).encode("utf-8")).hexdigest()


def _assert_validation_normalized_digests(validation: str) -> None:
    historical_heading = "## Superseded historical hosted baseline"
    task34_heading = (
        "### Task34 terminal-snapshot capture and hosted baselines (superseded)"
    )
    task33_heading = "### Client-terminal capture and hosted baselines (superseded)"
    assert historical_heading in validation, "digest section structure"
    current_evidence, historical_evidence = validation.split(
        historical_heading,
        maxsplit=1,
    )
    assert task34_heading in historical_evidence, "digest section structure"
    assert task33_heading in historical_evidence, "digest section structure"
    task34_history = historical_evidence.split(task34_heading, maxsplit=1)[1].split(
        task33_heading,
        maxsplit=1,
    )[0]
    older_history = task33_heading + historical_evidence.split(
        task33_heading,
        maxsplit=1,
    )[1]

    assert _normalized_digest(current_evidence) == (
        "d1403b1fb4d69a5953aab6e040760be5912b35f37ebb4ac9cf632a9322c224dd"
    ), "current normalized digest"
    assert _normalized_digest(task34_history) == (
        "86d6128f55b21219641cd1b4f62da4a4567a352557b7ef9f30755c30dea71d7c"
    ), "Task34 normalized digest"
    assert _normalized_digest(older_history) == (
        "75818dd3d27072d3e0443a6e314cfc28a3ea524787ff855bff47d5f3df6bbaf2"
    ), "older-history normalized digest"


def test_validation_normalized_digests_match_current_document() -> None:
    _assert_validation_normalized_digests(_read("docs/validation.md"))


@pytest.mark.parametrize(
    ("needle", "expected_guard"),
    (
        (
            "This file records only `codex/backchannel-v0.3` evidence.",
            "current normalized digest",
        ),
        (
            "This Task34 source, capture, activation, hosted, packaged-container",
            "Task34 normalized digest",
        ),
        (
            "The prior Task33 source gate covered",
            "older-history normalized digest",
        ),
    ),
)
def test_validation_normalized_digest_rejects_section_mutation(
    needle: str,
    expected_guard: str,
) -> None:
    validation = _read("docs/validation.md")
    assert needle in validation
    polluted_validation = validation.replace(needle, f"{needle} mutated", 1)

    with pytest.raises(AssertionError, match=expected_guard):
        _assert_validation_normalized_digests(polluted_validation)


def _extract_current_section(
    current_evidence: str,
    start_heading: str,
    end_heading: str,
    guard: str,
) -> str:
    assert current_evidence.count(start_heading) == 1, guard
    assert current_evidence.count(end_heading) == 1, guard
    return current_evidence.split(start_heading, maxsplit=1)[1].split(
        end_heading,
        maxsplit=1,
    )[0]


def _extract_current_bullet(section: str, prefix: str, guard: str) -> str:
    matches = tuple(
        re.finditer(
            rf"^- {re.escape(prefix)}.*?(?=^- |\Z)",
            section,
            flags=re.MULTILINE | re.DOTALL,
        )
    )
    assert len(matches) == 1, guard
    return " ".join(matches[0].group(0).split())


def _assert_current_canonical_local_evidence(current_evidence: str) -> None:
    guard = "current canonical/local evidence"
    activation = _extract_current_section(
        current_evidence,
        "## Current Task35 final evidence activation",
        "## Current Task35 transition-marker proof boundary",
        guard,
    )
    assert _extract_current_bullet(
        activation,
        "The exact local canonical gate on final evidence activation",
        guard,
    ) == (
        "- The exact local canonical gate on final evidence activation "
        "`6533a3f2e212d9a7c4e1af9f1d2dd77f7bd45a18` covered 29 capture "
        "contracts, `capture_manifest_valid`, 19 web test files / 369 tests, the "
        "production TypeScript/Vite build, Ruff, strict MyPy over 35 source files, "
        "and `957 passed`, 3 warnings, in 25.36s."
    ), guard
    assert _extract_current_bullet(
        activation,
        "The local deterministic stub smoke reported",
        guard,
    ) == (
        "- The local deterministic stub smoke reported `PASS`. The local production "
        "`deterministic-qa` smoke reported `PASS` for health, readiness, session "
        "isolation, SSE reconnect, approval, decline, and bounded SIGTERM. OpenAPI "
        "verification and the history-aware secret scan also reported `PASS`."
    ), guard
    assert _extract_current_bullet(
        activation,
        "The live three-run command remained blocked",
        guard,
    ) == (
        "- The live three-run command remained blocked at its "
        "`missing_openai_api_key` preflight after `attempted=1` of `target=3`, with "
        "`approvals=0` and no model IDs, tools, or root trace. It did not execute "
        "three live runs."
    ), guard


def _assert_current_hosted_provenance(current_evidence: str) -> None:
    guard = "current hosted provenance"
    activation = _extract_current_section(
        current_evidence,
        "## Current Task35 final evidence activation",
        "## Current Task35 transition-marker proof boundary",
        guard,
    )
    assert _extract_current_bullet(
        activation,
        "The earlier capture evidence activation",
        guard,
    ) == (
        "- The earlier capture evidence activation "
        "`60fe1243a5eeb760984d78d667f7efdc33630adc` and GitHub Actions "
        "[run 30595724271]"
        "(https://github.com/charlie2233/backchannel-agent-support/actions/runs/"
        "30595724271) remain bounded historical nonproof: `verify` "
        "([job 91047571247]"
        "(https://github.com/charlie2233/backchannel-agent-support/actions/runs/"
        "30595724271/job/91047571247)) reported `FAILURE` with `1 failed, 950 "
        "passed, 1 warning` in 41.89s and exactly one observed annotation; "
        "`container-smoke` ([job 91047810594]"
        "(https://github.com/charlie2233/backchannel-agent-support/actions/runs/"
        "30595724271/job/91047810594)) reported `SKIPPED`, with container "
        "annotations `[]`."
    ), guard
    assert _extract_current_bullet(
        activation,
        "Final evidence activation GitHub Actions",
        guard,
    ) == (
        "- Final evidence activation GitHub Actions [run 30598406930]"
        "(https://github.com/charlie2233/backchannel-agent-support/actions/runs/"
        "30598406930) passed the exact named hosted lanes. `verify` "
        "([job 91055708186]"
        "(https://github.com/charlie2233/backchannel-agent-support/actions/runs/"
        "30598406930/job/91055708186)) reported `PASS` with `957 passed, 1 "
        "warning` in 46.45s and covered the canonical gate, deterministic stub "
        "smoke, OpenAPI verification, and the history-aware secret scan. "
        "`container-smoke` ([job 91055968348]"
        "(https://github.com/charlie2233/backchannel-agent-support/actions/runs/"
        "30598406930/job/91055968348)) reported `PASS`, built the packaged image, "
        "confirmed runtime user `10001:10001`, and passed the offline network-none "
        "`deterministic-qa` and `deployed-readonly` profiles. Both job annotation "
        "APIs returned `[]`."
    ), guard
    actions_base = (
        "https://github.com/charlie2233/backchannel-agent-support/actions/runs"
    )
    assert tuple(
        re.findall(
            r"https://github\.com/[^)\s]+/actions/runs/\d+(?:/job/\d+)?",
            current_evidence,
        )
    ) == (
        f"{actions_base}/30595724271",
        f"{actions_base}/30595724271/job/91047571247",
        f"{actions_base}/30595724271/job/91047810594",
        f"{actions_base}/30598406930",
        f"{actions_base}/30598406930/job/91055708186",
        f"{actions_base}/30598406930/job/91055968348",
    ), guard
    assert "Hosted validation successor:" not in current_evidence, guard


def _assert_current_pending_bullets(current_evidence: str) -> None:
    guard = "current pending bullets"
    pending = _extract_current_section(
        current_evidence,
        "## Current Task35 pending external gates",
        "## Per-SHA proof matrix",
        guard,
    )
    bullets = tuple(
        " ".join(match.group(0).split())
        for match in re.finditer(
            r"^- .*?(?=^- |\Z)",
            pending,
            flags=re.MULTILINE | re.DOTALL,
        )
    )
    assert bullets == (
        (
            "- Within the source, capture, and hosted activation lane, only final "
            "documentation-tip CI for the resulting documentation successor remains "
            "pending."
        ),
        (
            "- There is no current Task35 live OpenAI, public deployment, tag, or "
            "release proof or claim. The blocked missing-key attempt did not execute "
            "three live runs or prove a real provider mutation."
        ),
    ), guard


def _assert_current_matrix(current_evidence: str) -> None:
    guard = "current matrix"
    matrix = _extract_current_section(
        current_evidence,
        "## Per-SHA proof matrix",
        "## Hosted container evidence boundary",
        guard,
    )
    rows = tuple(
        line.strip() for line in matrix.splitlines() if line.strip().startswith("|")
    )
    assert rows == (
        "| Lane | Exact evidence | Status and boundary |",
        "| --- | --- | --- |",
        (
            "| Local source and tests | Exact final evidence activation canonical "
            "gate on `6533a3f…`: 29 capture contracts, manifest verification, 19 web "
            "files / 369 tests, build, Ruff, strict MyPy over 35 files, and 957 "
            "Python tests | **Verified locally** in the exact named lanes |"
        ),
        (
            "| Local final-build captures | "
            "[`docs/assets/final/manifest.json`](assets/final/manifest.json); six "
            "provenance-checked PNGs under [`docs/assets/final`](assets/final) | "
            "**Local capture evidence only** on source `21d9b0f…` in Google Chrome "
            "at 1440×1024 and 390×844; not container, deployment, live OpenAI, or "
            "release proof |"
        ),
        (
            "| Live OpenAI | Three-run command stopped at "
            "`missing_openai_api_key` after attempt 1 of 3, with zero approvals and "
            "no model IDs, tools, or root trace | **Blocked / Unverified**; three "
            "live runs did not execute |"
        ),
        (
            "| Local Docker | No Docker-family runtime is installed on this Mac | "
            "**Unverified locally** |"
        ),
        (
            "| GitHub CI/container | Final activation `6533a3f…`; run `30598406930`; "
            "`verify` job `91055708186` = `PASS`; `container-smoke` job "
            "`91055968348` = `PASS`; both annotation APIs `[]` | **Verified for the "
            "exact hosted canonical and packaged-container smoke lanes**; final "
            "docs-tip CI remains pending within that activation lane |"
        ),
        (
            "| Public deployment | No public application origin | **Blocked / "
            "Unverified** |"
        ),
        (
            "| Tag/release | No release tag or GitHub release | **Blocked / "
            "Unverified** |"
        ),
    ), guard


def _assert_current_hosted_boundary(current_evidence: str) -> None:
    guard = "current hosted boundary"
    boundary = _extract_current_section(
        current_evidence,
        "## Hosted container evidence boundary",
        "## Capture contract",
        guard,
    )
    assert " ".join(boundary.split()) == (
        "The final activation passed the exact hosted canonical lane and packaged "
        "container smoke lane. The container job built the packaged image, "
        "confirmed runtime user `10001:10001`, and passed only the offline "
        "network-none `deterministic-qa` and `deployed-readonly` profiles. This "
        "does not prove local Docker, browser capture in CI, public deployment or "
        "browser URL, live OpenAI, real provider execution, container replacement "
        "or restart durability beyond those exact profiles, long-lived `/data` "
        "volume persistence, target-host durability/networking, abrupt host loss "
        "or backup, concurrent multi-container SQLite, final documentation-tip CI, "
        "tag, or release. The earlier capture activation run `30595724271` remains "
        "bounded historical nonproof: `verify` failed and `container-smoke` was "
        "skipped. The successful final activation does not erase that result. "
        "Within the source, capture, and hosted activation lane, only final docs-tip "
        "CI remains pending. Live OpenAI, public deployment, local Docker, tag, and "
        "release remain separate blocked or unverified lanes. The browser capture "
        "remained local; CI did not execute the browser capture."
    ), guard


def _assert_validation_matches_current_capture(validation: str) -> None:
    manifest = json.loads(_read("docs/assets/final/manifest.json"))
    runtime_input = manifest["runtimeInput"]
    browser = manifest["browser"]
    environment = manifest["environment"]
    artifacts = manifest["artifacts"]

    historical_heading = "## Superseded historical hosted baseline"
    assert historical_heading in validation, "validation semantic structure"
    current_evidence, historical_evidence = validation.split(
        historical_heading,
        maxsplit=1,
    )
    actions_base = (
        "https://github.com/charlie2233/backchannel-agent-support/actions/runs"
    )
    normalized_current_evidence = " ".join(current_evidence.split())
    normalized_historical_evidence = " ".join(historical_evidence.split())
    task34_heading = (
        "### Task34 terminal-snapshot capture and hosted baselines (superseded)"
    )
    task33_heading = "### Client-terminal capture and hosted baselines (superseded)"
    assert task34_heading in historical_evidence, "validation semantic structure"
    assert task33_heading in historical_evidence, "validation semantic structure"
    assert (
        historical_evidence.split(task34_heading, maxsplit=1)[0].strip() == ""
    ), "current provenance exclusion"
    task34_history = historical_evidence.split(task34_heading, maxsplit=1)[1].split(
        task33_heading,
        maxsplit=1,
    )[0]

    transition_heading = "## Current Task35 transition-marker proof boundary"
    transition_end_heading = "## Current Task35 pending external gates"
    transition_evidence = current_evidence.split(
        transition_heading,
        maxsplit=1,
    )[1].split(transition_end_heading, maxsplit=1)[0]
    assert " ".join(transition_evidence.split()) == (
        "On modern schemas, clearing the sole `terminal = 1` marker for a "
        "completed approval or decline makes reopen fail closed with "
        "`ReceiptTransitionError`. Reopen performs no repair and preserves exact "
        "event-row and text-byte stability. On a genuine legacy events schema "
        "without `terminal`, reopen performs a one-time `terminal` add/backfill, "
        "leaves exactly one final marker, and provides exact unrelated-row "
        "preservation and exact database-byte stability across a second reopen; "
        "the foreign-key check was empty and the integrity check returned `ok`."
    ), "transition marker semantic contract"

    _assert_current_hosted_provenance(current_evidence)
    _assert_current_pending_bullets(current_evidence)
    _assert_current_matrix(current_evidence)
    _assert_current_hosted_boundary(current_evidence)
    _assert_current_canonical_local_evidence(current_evidence)

    for current_identifier in (
        manifest["sourceCommit"],
        "21d9b0f…",
        "60fe1243a5eeb760984d78d667f7efdc33630adc",
        "60fe124…",
        "6533a3f2e212d9a7c4e1af9f1d2dd77f7bd45a18",
        "6533a3f…",
        runtime_input["digest"],
        "eabc274…",
    ):
        assert (
            current_identifier not in historical_evidence
        ), f"current historical identifier exclusion: {current_identifier}"

    assert manifest["sourceCommit"] == (
        "21d9b0f8dbeb59454e3f3b3d3d9138af28af02ad"
    ), "manifest source contract"
    assert runtime_input["digest"] == (
        "eabc2743f9475e384fe7a49344f42a9635d2f57277f788708d23425edd845385"
    ), "manifest runtime digest contract"
    for required_current_fact in (
        "## Current Task35 final evidence activation",
        manifest["sourceCommit"],
        runtime_input["digest"],
        "84 runtime paths",
        "capture profile `keyless_sdk_stub`",
        "Google Chrome 150.0.7871.187",
        "stable `chrome` channel in headless mode",
        "`en-US` / `UTC` / `reduce` / `light`",
        "1440×1024",
        "390×844",
        "`npm run test:e2e` reported `29 passed`",
        "29 capture contracts",
        "Five PNGs changed from Task34; `mobile-consent.png` was byte-identical",
        "combined before/after review across all six images passed with no P0-P3",
        "2 failed, 17 deselected",
        "DID NOT RAISE `ReceiptTransitionError`",
        "3 passed, 17 deselected",
        "20 passed",
        "`951 passed`, 3 warnings, in 24.57s",
        "strict MyPy on `server/store.py`",
        "Independent Task35 specification and quality reviews passed with no P0-P3",
        "explicit second-reopen proof",
        "clearing the sole `terminal = 1` marker",
        "fail closed with `ReceiptTransitionError`",
        "Reopen performs no repair",
        "exact event-row and text-byte stability",
        "genuine legacy events schema without `terminal`",
        "one-time `terminal` add/backfill",
        "exactly one final marker",
        "exact unrelated-row preservation",
        "exact database-byte stability",
        "foreign-key check was empty",
        "integrity check returned `ok`",
    ):
        assert (
            required_current_fact in normalized_current_evidence
        ), f"current evidence facts: missing {required_current_fact}"

    for unsupported_current_claim in (
        "Current Task35 live OpenAI passed",
        "Current Task35 public deployment passed",
        "Current Task35 docs-tip CI passed",
        "Current Task35 container replacement persistence passed",
        "Current Task35 release passed",
    ):
        assert (
            unsupported_current_claim not in current_evidence
        ), f"current positive allowlist: unsupported claim {unsupported_current_claim}"

    for forbidden_current_fact in (
        "8942517f43045a124e11a8e79296f6e4de875936",
        "4ca13252e05357ff98023f9290375cd1718bcce6",
        "e208c767646af920d90aa9396f441fb328c999c3235aff646ac20a5611543f27",
        "e208c76…",
        "5364dd4f9f825f5d68c85ea005ceb277ab943e4c",
        "9d7e850cbf6761524e9a51359801944cdf7fabf4",
        "30593858167",
        "91041814602",
        "91042106744",
        "30594431983",
        "91043601366",
        "91043886907",
    ):
        assert (
            forbidden_current_fact not in current_evidence
        ), f"superseded provenance exclusion: {forbidden_current_fact}"

    for excluded_nonproof_identifier in (
        "85e1e8ec9147242adca311c4ba10ea8c1c3008dc",
        "85e1e8e…",
        "30206582233",
        "89805619343",
        "89805788728",
        "b578680…",
    ):
        assert (
            excluded_nonproof_identifier not in current_evidence
        ), f"superseded provenance exclusion: {excluded_nonproof_identifier}"

    historical_identifiers = set(
        re.findall(
            r"(?<![0-9A-Za-z])(?:[0-9a-f]{40,64}|[0-9a-f]{7}…|\d{11})"
            r"(?![0-9A-Za-z])",
            historical_evidence,
        )
    )
    for historical_identifier in historical_identifiers:
        assert (
            historical_identifier not in current_evidence
        ), f"superseded provenance exclusion: {historical_identifier}"

    task34_urls = (
        f"{actions_base}/30593858167",
        f"{actions_base}/30593858167/job/91041814602",
        f"{actions_base}/30593858167/job/91042106744",
        f"{actions_base}/30594431983",
        f"{actions_base}/30594431983/job/91043601366",
        f"{actions_base}/30594431983/job/91043886907",
    )
    assert tuple(
        re.findall(
            r"https://github\.com/[^)\s]+/actions/runs/\d+(?:/job/\d+)?",
            task34_history,
        )
    ) == task34_urls, "Task34 ordered Actions bindings"
    normalized_task34_history = " ".join(task34_history.split())
    for required_task34_fact in (
        "51 / 51 tests",
        "63 / 63",
        "e208c767646af920d90aa9396f441fb328c999c3235aff646ac20a5611543f27",
        "capture profile `keyless_sdk_stub`",
        "Google Chrome 150.0.7871.187 stable `chrome` in headless mode",
        "935 / 935 tests with 3 warnings in 24.53s",
        "944 / 944 Python tests with 3 warnings in 25.73s",
        "missing-`OPENAI_API_KEY` preflight",
        "944 Python tests with 1 warning in 43.54s",
        "948 Python tests with 1 warning in 42.45s",
        "Both activation job annotation APIs returned `[]`",
        "Both final-tip job annotation APIs returned `[]`",
        "built the packaged image",
        "runtime user `10001:10001`",
        "offline network-none `deterministic-qa` and `deployed-readonly` profiles",
        "truthful superseded history only",
        (
            "Prior pre-CI/current activation: "
            "`5364dd4f9f825f5d68c85ea005ceb277ab943e4c`"
        ),
        (
            "[run 30593858167]"
            f"({actions_base}/30593858167)"
        ),
        (
            "[job 91041814602]"
            f"({actions_base}/30593858167/job/91041814602)"
        ),
        (
            "[job 91042106744]"
            f"({actions_base}/30593858167/job/91042106744)"
        ),
        (
            "The superseded local exact activation gate covered 29 capture "
            "contracts, `capture_manifest_valid`, 19 web test files / 369 tests "
            "and TypeScript/Vite, Ruff, strict MyPy over 35 source files"
        ),
    ):
        assert (
            required_task34_fact in normalized_task34_history
        ), f"Task34 historical facts: missing {required_task34_fact}"
    expected_history_headings = (
        task34_heading,
        task33_heading,
        "### SQLite-permission capture and hosted baselines (superseded)",
        "### JSON-media capture and hosted baselines (superseded)",
        "### Terminal-retry capture and hosted baselines (superseded)",
        "### Async SQLite capture and hosted baseline (superseded)",
        "### Request-boundary capture and hosted baseline (superseded)",
        "### Immediate prior capture and hosted baseline (superseded)",
        "### Prior capture and hosted baseline (superseded)",
        "### Older hosted baseline (superseded)",
    )
    assert tuple(
        line for line in historical_evidence.splitlines() if line.startswith("### ")
    ) == expected_history_headings, "older history section order"

    expected_historical_url_suffixes = (
        (
            "30593858167",
            "30593858167/job/91041814602",
            "30593858167/job/91042106744",
            "30594431983",
            "30594431983/job/91043601366",
            "30594431983/job/91043886907",
        ),
        (
            "30591690973",
            "30591690973/job/91035189804",
            "30591690973/job/91035490238",
            "30592336018",
            "30592336018/job/91037154486",
            "30592336018/job/91037472406",
        ),
        (
            "30588357735",
            "30588357735/job/91024909140",
            "30588357735/job/91025281220",
            "30589435325",
            "30589435325/job/91028250060",
            "30589435325/job/91028596793",
        ),
        (
            "30501109833",
            "30501109833/job/90740755548",
            "30501109833/job/90741063564",
            "30501426777",
            "30501426777/job/90741727313",
            "30501426777/job/90742037347",
        ),
        (
            "30499178838",
            "30499178838/job/90734790003",
            "30499178838/job/90735121608",
            "30499500934",
            "30499500934/job/90735804461",
            "30499500934/job/90736149754",
        ),
        (
            "30208188300",
            "30208188300/job/89809811619",
            "30208188300/job/89809998273",
        ),
        (
            "30182741263",
            "30182741263/job/89742014836",
            "30182741263/job/89742159778",
        ),
        (
            "30174822102",
            "30174822102/job/89721793096",
            "30174822102/job/89721989116",
        ),
        (
            "30162644778",
            "30162644778/job/89690352979",
            "30162644778/job/89690513333",
        ),
        (
            "30140554792",
            "30140554792/job/89632837699",
            "30140554792/job/89633002245",
        ),
    )
    for index, heading in enumerate(expected_history_headings):
        section = historical_evidence.split(heading, maxsplit=1)[1]
        if index + 1 < len(expected_history_headings):
            section = section.split(expected_history_headings[index + 1], maxsplit=1)[
                0
            ]
        observed_suffixes = tuple(
            url.split("/actions/runs/", maxsplit=1)[1]
            for url in re.findall(
                r"https://github\.com/[^)\s]+/actions/runs/\d+(?:/job/\d+)?",
                section,
            )
        )
        assert (
            observed_suffixes == expected_historical_url_suffixes[index]
        ), f"older history ordered Actions bindings: {heading}"

    task32_heading = expected_history_headings[2]
    task32_end_heading = expected_history_headings[3]
    task32_history = historical_evidence.split(task32_heading, maxsplit=1)[1].split(
        task32_end_heading,
        maxsplit=1,
    )[0]
    normalized_task32_history = " ".join(task32_history.split())
    for required_task32_fact in (
        (
            "pre-CI documentation successor "
            "`c6c60d4354eba7348aef3245d19e787661544fa7`"
        ),
        (
            "[job 91024909140]"
            f"({actions_base}/30588357735/job/91024909140)"
        ),
        (
            "[job 91025281220]"
            f"({actions_base}/30588357735/job/91025281220)"
        ),
        (
            "Both pre-CI jobs passed and both pre-CI job annotation APIs "
            "returned `[]`"
        ),
        (
            "Prior final documentation tip: "
            "`abdf644b4c35ecfaee2d22917c4c621f9d123a5b`"
        ),
        (
            "[job 91028250060]"
            f"({actions_base}/30589435325/job/91028250060)"
        ),
        (
            "[job 91028596793]"
            f"({actions_base}/30589435325/job/91028596793)"
        ),
        (
            "Both final-tip jobs passed and both final-tip job annotation APIs "
            "returned `[]`"
        ),
        (
            "Final-tip hosted `verify` covered `capture_manifest_valid`, 19 web "
            "test files / 345 tests, Ruff, strict MyPy over 35 source files, "
            "796 Python tests"
        ),
        (
            "Both prior `container-smoke` jobs built the packaged image with "
            "owner-only `/data` mode `0700`, confirmed runtime user `10001:10001`, "
            "and passed the offline network-none `deterministic-qa` and "
            "`deployed-readonly` profiles"
        ),
    ):
        assert (
            required_task32_fact in normalized_task32_history
        ), f"older history semantic facts: missing Task32 fact {required_task32_fact}"

    for required_older_fact in (
        "84af413c3a7833daa635e6a0b84e8f331cc1f7f2",
        "884 Python tests with 1 warning in 43.91s",
        "a8057a8e0c29bdcc95e35949819c64005e5ee064",
        "778 / 778 Python tests with 3 warnings in 28.51s",
        "796 Python tests",
        "c446f05adfb836539ddbaa74a41502034c910092",
        "120-test client matrix",
        "695 Python tests with 3 warnings in 179.85s",
        "96543fff62bb5d0a3c0f8a0464e9a07bf9c54568",
        "8d0a896c753c4c60894301a20a3866bbbfa1e76f",
        "8823d29d7de93d44f4843a2fa4db1adec4e452bd",
        "742e3caf2af5a9cce3cd8de242cf113424e8528f",
        "7d9128a8171ddb4978d8f7b0debb9effce997e27",
        "b57868005a3fe0869136f54472ee0098035a9099",
        (
            "pre-CI documentation successor "
            "`c6c60d4354eba7348aef3245d19e787661544fa7`"
        ),
        (
            "[job 91024909140]"
            f"({actions_base}/30588357735/job/91024909140)"
        ),
        (
            "[job 91025281220]"
            f"({actions_base}/30588357735/job/91025281220)"
        ),
        (
            "Prior final documentation tip: "
            "`abdf644b4c35ecfaee2d22917c4c621f9d123a5b`"
        ),
        (
            "[job 91028250060]"
            f"({actions_base}/30589435325/job/91028250060)"
        ),
        (
            "[job 91028596793]"
            f"({actions_base}/30589435325/job/91028596793)"
        ),
        (
            "Both pre-CI jobs passed and both pre-CI job annotation APIs "
            "returned `[]`"
        ),
        (
            "Both final-tip jobs passed and both final-tip job annotation APIs "
            "returned `[]`"
        ),
        "Final-tip hosted `verify` covered `capture_manifest_valid`, 19 web test files",
        "796 Python tests",
        (
            "Both prior `container-smoke` jobs built the packaged image with "
            "owner-only `/data` mode `0700`, confirmed runtime user `10001:10001`, "
            "and passed the offline network-none `deterministic-qa` and "
            "`deployed-readonly` profiles"
        ),
    ):
        assert (
            required_older_fact in normalized_historical_evidence
        ), f"older history semantic facts: missing {required_older_fact}"

    for forbidden_history_text in (
        "not current Task29",
        "not current Task28",
        "not current Task25",
        "The pre-CI verify job failed.",
        "The final container-smoke job was skipped.",
        "The hosted verify gate covered 344 web tests.",
        "The final hosted gate covered 795 Python tests.",
        "The local exact gate completed in 29.51s.",
    ):
        assert (
            forbidden_history_text not in historical_evidence
        ), f"older history semantic contract: {forbidden_history_text}"

    sqlite_heading = "## SQLite file-permission proof boundary"
    sqlite_end_heading = "## JSON response media proof boundary"
    sqlite_evidence = current_evidence.split(sqlite_heading, maxsplit=1)[1].split(
        sqlite_end_heading,
        maxsplit=1,
    )[0]
    normalized_sqlite_evidence = " ".join(sqlite_evidence.split())
    for sqlite_fact in (
        "owner-only `0600` regular files",
        "Constructor-time initialization is the only lane allowed to harden",
        "connection and readiness checks are nonmutating and fail closed instead "
        "of repairing permission drift",
        "URI with `mode=rw`",
        "Unsupported POSIX capabilities",
        "unexpected owner",
        "unprotected directory ancestry",
        "changed device/inode identity",
        "insecure database or sidecar permissions all fail closed",
        "owner-only `0700`",
        "23 focused tests",
    ):
        assert (
            sqlite_fact in normalized_sqlite_evidence
        ), f"SQLite permission semantic contract: missing {sqlite_fact}"

    media_heading = "## JSON response media proof boundary"
    media_end_heading = "## Terminal evidence retry proof boundary"
    media_evidence = current_evidence.split(media_heading, maxsplit=1)[1].split(
        media_end_heading,
        maxsplit=1,
    )[0]
    normalized_media_evidence = " ".join(media_evidence.split())
    for media_fact in (
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
        assert (
            media_fact in normalized_media_evidence
        ), f"JSON media semantic contract: missing {media_fact}"

    retry_heading = "## Terminal evidence retry proof boundary"
    retry_end_heading = "## Async SQLite proof boundary"
    retry_evidence = current_evidence.split(retry_heading, maxsplit=1)[1].split(
        retry_end_heading,
        maxsplit=1,
    )[0]
    normalized_retry_evidence = " ".join(retry_evidence.split())
    for retry_fact in (
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
        assert (
            retry_fact in normalized_retry_evidence
        ), f"terminal retry semantic contract: missing {retry_fact}"
    current_evidence_lines = tuple(
        line.strip() for line in current_evidence.splitlines() if line.strip()
    )
    hosted_scope_pattern = re.compile(
        r"\b(?:CI|GitHub\s+Actions|workflow|verify\s+job|canonical|backend|"
        r"container(?:-smoke|\s+smoke|\s+job)?|packaged\s+Docker|"
        r"public\s+deployment|live\s+OpenAI|provider|release|docs-tip)\b",
        flags=re.IGNORECASE,
    )
    expected_current_scope_lines = (
        "container, live OpenAI, deployment, or release evidence.",
        "- The exact local canonical gate on final evidence activation",
        "`60fe1243a5eeb760984d78d667f7efdc33630adc` and GitHub Actions",
        "exactly one observed annotation; `container-smoke`",
        "reported `SKIPPED`, with container annotations `[]`.",
        "- Final evidence activation GitHub Actions",
        "canonical gate, deterministic stub smoke, OpenAPI verification, and the",
        "history-aware secret scan. `container-smoke`",
        "documentation-tip CI for the resulting documentation successor remains",
        "- There is no current Task35 live OpenAI, public deployment, tag, or release",
        "runs or prove a real provider mutation.",
        (
            "| Local source and tests | Exact final evidence activation canonical "
            "gate on `6533a3f…`: 29 capture contracts, manifest verification, 19 web "
            "files / 369 tests, build, Ruff, strict MyPy over 35 files, and 957 "
            "Python tests | **Verified locally** in the exact named lanes |"
        ),
        (
            "| Local final-build captures | "
            "[`docs/assets/final/manifest.json`](assets/final/manifest.json); six "
            "provenance-checked PNGs under [`docs/assets/final`](assets/final) | "
            "**Local capture evidence only** on source `21d9b0f…` in Google Chrome "
            "at 1440×1024 and 390×844; not container, deployment, live OpenAI, or "
            "release proof |"
        ),
        (
            "| Live OpenAI | Three-run command stopped at "
            "`missing_openai_api_key` after attempt 1 of 3, with zero approvals and "
            "no model IDs, tools, or root trace | **Blocked / Unverified**; three "
            "live runs did not execute |"
        ),
        (
            "| GitHub CI/container | Final activation `6533a3f…`; run `30598406930`; "
            "`verify` job `91055708186` = `PASS`; `container-smoke` job "
            "`91055968348` = `PASS`; both annotation APIs `[]` | **Verified for the "
            "exact hosted canonical and packaged-container smoke lanes**; final "
            "docs-tip CI remains pending within that activation lane |"
        ),
        (
            "| Public deployment | No public application origin | **Blocked / "
            "Unverified** |"
        ),
        (
            "| Tag/release | No release tag or GitHub release | **Blocked / "
            "Unverified** |"
        ),
        "## Hosted container evidence boundary",
        "The final activation passed the exact hosted canonical lane and packaged",
        "container smoke lane. The container job built the packaged image, confirmed",
        "Docker, browser capture in CI, public deployment or browser URL, live OpenAI,",
        "real provider execution, container replacement or restart durability beyond",
        "durability/networking, abrupt host loss or backup, concurrent multi-container",
        "SQLite, final documentation-tip CI, tag, or release.",
        "nonproof: `verify` failed and `container-smoke` was skipped. The successful",
        "hosted activation lane, only final docs-tip CI remains pending. Live OpenAI,",
        "public deployment, local Docker, tag, and release remain separate blocked or",
        "unverified lanes. The browser capture remained local; CI did not execute the",
        (
            "added the captured files. CI did not execute the browser capture. The "
            "six images"
        ),
        "OpenAI, a public deployment, a container, a release, or a real provider",
        (
            "container-replacement, abrupt-host-loss, or concurrent "
            "multi-container SQLite proof."
        ),
        "checkpoint. The current Task35 activation verifies only its exact canonical and",
        (
            "offline container-smoke profiles; it does not prove multi-process or "
            "concurrent"
        ),
        (
            "multi-container SQLite, target-host scheduling, abrupt host loss, "
            "public latency,"
        ),
        "or live provider behavior.",
        (
            "`21d9b0f…` and is not public deployment, live OpenAI, container-host "
            "durability, tag,"
        ),
        "or release proof.",
        (
            "route-gate release, retained admission charges, exact public "
            "`504 live_timeout`"
        ),
        (
            "envelopes, retryable approve/decline claims, and no duplicate dispatch "
            "when provider"
        ),
        "process-termination proof, public deployment, or release claim.",
    )
    observed_current_scope_lines = tuple(
        line for line in current_evidence_lines if hosted_scope_pattern.search(line)
    )
    assert (
        observed_current_scope_lines == expected_current_scope_lines
    ), "current hosted provenance and scope allowlist"

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
    expected_current_positive_lines = (
        "container, live OpenAI, deployment, or release evidence.",
        "- `npm run test:e2e` reported `29 passed` against the clean capture source",
        "combined before/after review across all six images passed with no P0-P3",
        "- The focused transition-marker gate then passed with `3 passed, 17 deselected`;",
        "the whole Task7 migration suite passed with `20 passed`.",
        "- The full Python suite passed with `951 passed`, 3 warnings, in 24.57s. Ruff,",
        "strict MyPy on `server/store.py`, and the diff check passed.",
        "- Independent Task35 specification and quality reviews passed with no P0-P3",
        "`957 passed`, 3 warnings, in 25.36s.",
        "- The local deterministic stub smoke reported `PASS`. The local production",
        "`deterministic-qa` smoke reported `PASS` for health, readiness, session",
        "verification and the history-aware secret scan also reported `PASS`.",
        "- The live three-run command remained blocked at its",
        "live runs.",
        "reported `FAILURE` with `1 failed, 950 passed, 1 warning` in 41.89s and",
        "passed the exact named hosted lanes. `verify`",
        "reported `PASS` with `957 passed, 1 warning` in 46.45s and covered the",
        "reported `PASS`, built the packaged image, confirmed runtime user",
        "`10001:10001`, and passed the offline network-none `deterministic-qa` and",
        "On modern schemas, clearing the sole `terminal = 1` marker for a completed",
        "the foreign-key check was empty and the integrity check returned `ok`.",
        "- There is no current Task35 live OpenAI, public deployment, tag, or release",
        "proof or claim. The blocked missing-key attempt did not execute three live",
        "runs or prove a real provider mutation.",
        (
            "| Local source and tests | Exact final evidence activation canonical "
            "gate on `6533a3f…`: 29 capture contracts, manifest verification, 19 web "
            "files / 369 tests, build, Ruff, strict MyPy over 35 files, and 957 "
            "Python tests | **Verified locally** in the exact named lanes |"
        ),
        (
            "| Local final-build captures | "
            "[`docs/assets/final/manifest.json`](assets/final/manifest.json); six "
            "provenance-checked PNGs under [`docs/assets/final`](assets/final) | "
            "**Local capture evidence only** on source `21d9b0f…` in Google Chrome "
            "at 1440×1024 and 390×844; not container, deployment, live OpenAI, or "
            "release proof |"
        ),
        (
            "| Live OpenAI | Three-run command stopped at "
            "`missing_openai_api_key` after attempt 1 of 3, with zero approvals and "
            "no model IDs, tools, or root trace | **Blocked / Unverified**; three "
            "live runs did not execute |"
        ),
        (
            "| GitHub CI/container | Final activation `6533a3f…`; run `30598406930`; "
            "`verify` job `91055708186` = `PASS`; `container-smoke` job "
            "`91055968348` = `PASS`; both annotation APIs `[]` | **Verified for the "
            "exact hosted canonical and packaged-container smoke lanes**; final "
            "docs-tip CI remains pending within that activation lane |"
        ),
        "The final activation passed the exact hosted canonical lane and packaged",
        "container smoke lane. The container job built the packaged image, confirmed",
        "runtime user `10001:10001`, and passed only the offline network-none",
        "`deterministic-qa` and `deployed-readonly` profiles. This does not prove local",
        "Docker, browser capture in CI, public deployment or browser URL, live OpenAI,",
        "nonproof: `verify` failed and `container-smoke` was skipped. The successful",
        "hosted activation lane, only final docs-tip CI remains pending. Live OpenAI,",
        "validates API and DOM provenance before each viewport screenshot.",
        (
            "was produced against the clean source SHA, and the capture activation "
            "successor"
        ),
        (
            "prove only the local keyless SDK-stub final build; they do not prove "
            "live"
        ),
        "different clean frozen tree.",
        (
            "cases prove these source-level parser boundaries; the screenshots do "
            "not prove"
        ),
        (
            "fresh equally bounded initial-plus-three cycle. Throughout retry, "
            "already verified"
        ),
        (
            "The ordinary final-state screenshots do not prove transient store "
            "failure, retry"
        ),
        (
            "offline container-smoke profiles; it does not prove multi-process or "
            "concurrent"
        ),
        "or live provider behavior.",
        (
            "are source and raw-ASGI contract properties. The six screenshots do "
            "not prove"
        ),
        (
            "second/third write failure, validation-before-charge, live-ledger "
            "separation, reset"
        ),
        (
            "`21d9b0f…` and is not public deployment, live OpenAI, container-host "
            "durability, tag,"
        ),
        "## Live deadline proof boundary",
        "The source contract bounds the complete live pre-approval graph and each live",
        (
            "envelopes, retryable approve/decline claims, and no duplicate dispatch "
            "when provider"
        ),
        (
            "execution committed before the timeout. The OpenAI client and the "
            "redacted live-smoke"
        ),
    )
    observed_current_positive_lines = tuple(
        line for line in current_evidence_lines if positive_word_pattern.search(line)
    )
    assert (
        observed_current_positive_lines == expected_current_positive_lines
    ), "current positive allowlist"

    assert browser == {
        "name": "Google Chrome",
        "version": "150.0.7871.187",
        "channel": "chrome",
        "headless": True,
    }, "manifest browser contract"
    assert environment == {
        "locale": "en-US",
        "timezone": "UTC",
        "reducedMotion": "reduce",
        "colorScheme": "light",
    }, "manifest environment contract"
    assert manifest["schemaVersion"] == 1, "manifest schema contract"
    assert len(runtime_input["paths"]) == 84, "manifest runtime paths contract"
    assert runtime_input["paths"] == sorted(
        runtime_input["paths"]
    ), "manifest runtime paths contract"
    assert len(runtime_input["paths"]) == len(
        set(runtime_input["paths"])
    ), "manifest runtime paths contract"
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
        for item in artifacts
    ) == (
        (
            "desktop-consent.png",
            "acdf50fb67c38b8108adf86424a53e6940046ae4588aef11549341d0e8b02f73",
            "consent",
            "hotel",
            "sdk_stub",
            1440,
            1024,
        ),
        (
            "desktop-completed.png",
            "b7b5af1c26598ac6ae1496acff082b75a320e53fdaadfd8e17064e3e8e34315e",
            "completed",
            "hotel",
            "sdk_stub",
            1440,
            1024,
        ),
        (
            "desktop-declined.png",
            "da3a0e5f030e418c93aef79d80092f1f4054d57bab9cee0a697a4a8a5d5957f2",
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
            "d2b629ee3e61314db3d525005bd566ff7af70ae90c7e036a66ec1de4fbe3a628",
            "completed",
            "hotel",
            "sdk_stub",
            390,
            844,
        ),
        (
            "mobile-declined.png",
            "3c0ddc7b6c9b8bb529f9090b5079cc10690d1d6171524c09deea32967b1a1058",
            "declined",
            "hotel",
            "sdk_stub",
            390,
            844,
        ),
    ), "manifest artifacts contract"

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

    with pytest.raises(AssertionError, match="superseded provenance exclusion"):
        _assert_validation_matches_current_capture(polluted_validation)


@pytest.mark.parametrize(
    "history_heading",
    (
        "### Task34 terminal-snapshot capture and hosted baselines (superseded)",
        "### Client-terminal capture and hosted baselines (superseded)",
    ),
)
@pytest.mark.parametrize(
    "current_identifier",
    (
        "21d9b0f8dbeb59454e3f3b3d3d9138af28af02ad",
        "21d9b0f…",
        "60fe1243a5eeb760984d78d667f7efdc33630adc",
        "60fe124…",
        "6533a3f2e212d9a7c4e1af9f1d2dd77f7bd45a18",
        "6533a3f…",
        "eabc2743f9475e384fe7a49344f42a9635d2f57277f788708d23425edd845385",
        "eabc274…",
    ),
)
def test_validation_current_identifier_loop_rejects_history_body_leakage(
    history_heading: str,
    current_identifier: str,
) -> None:
    validation = _read("docs/validation.md")
    historical_heading = "## Superseded historical hosted baseline"
    polluted_validation = validation.replace(
        history_heading,
        f"{history_heading}\n\nInjected current provenance: {current_identifier}",
        1,
    )
    polluted_history = polluted_validation.split(historical_heading, maxsplit=1)[1]
    task34_heading = (
        "### Task34 terminal-snapshot capture and hosted baselines (superseded)"
    )
    assert polluted_history.split(task34_heading, maxsplit=1)[0].strip() == ""
    assert current_identifier in polluted_history.split(history_heading, maxsplit=1)[1]

    with pytest.raises(AssertionError, match="current historical identifier exclusion"):
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

    with pytest.raises(
        AssertionError,
        match="current hosted provenance",
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
    hosted_heading = (
        "### Task34 terminal-snapshot capture and hosted baselines (superseded)"
    )
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

    with pytest.raises(
        AssertionError,
        match=r"Task34 (?:ordered Actions bindings|historical facts)",
    ):
        _assert_validation_matches_current_capture(polluted_validation)


def test_validation_rejects_swapped_task34_hosted_job_bindings() -> None:
    validation = _read("docs/validation.md")
    hosted_heading = (
        "### Task34 terminal-snapshot capture and hosted baselines (superseded)"
    )
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

    with pytest.raises(AssertionError, match="Task34 ordered Actions bindings"):
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
            "Both final-tip jobs passed and both final-tip job annotation APIs "
            "returned `[]`",
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

    with pytest.raises(
        AssertionError,
        match=r"older history (?:ordered Actions bindings|semantic facts)",
    ):
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
        match="older history ordered Actions bindings",
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
        (
            "The full Python suite passed",
            "951 passed",
            "950 passed",
        ),
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

    with pytest.raises(AssertionError, match="current evidence facts"):
        _assert_validation_matches_current_capture(polluted_validation)


def _named_current_mutation_slice(
    validation: str,
    slice_name: str,
) -> tuple[int, int]:
    activation_heading = "## Current Task35 final evidence activation"
    transition_heading = "## Current Task35 transition-marker proof boundary"
    pending_heading = "## Current Task35 pending external gates"
    matrix_heading = "## Per-SHA proof matrix"
    boundary_heading = "## Hosted container evidence boundary"
    capture_heading = "## Capture contract"
    section_map = {
        "canonical": (
            activation_heading,
            transition_heading,
            "The exact local canonical gate on final evidence activation",
            "bullet",
        ),
        "local_smoke": (
            activation_heading,
            transition_heading,
            "The local deterministic stub smoke reported",
            "bullet",
        ),
        "hosted_old": (
            activation_heading,
            transition_heading,
            "The earlier capture evidence activation",
            "bullet",
        ),
        "hosted_final": (
            activation_heading,
            transition_heading,
            "Final evidence activation GitHub Actions",
            "bullet",
        ),
        "pending_primary": (
            pending_heading,
            matrix_heading,
            "Within the source, capture, and hosted activation lane",
            "bullet",
        ),
        "matrix_live": (
            matrix_heading,
            boundary_heading,
            "| Live OpenAI |",
            "line",
        ),
        "matrix_github": (
            matrix_heading,
            boundary_heading,
            "| GitHub CI/container |",
            "line",
        ),
        "hosted_boundary": (
            boundary_heading,
            capture_heading,
            "",
            "section",
        ),
    }
    start_heading, end_heading, selector, selector_kind = section_map[slice_name]
    assert validation.count(start_heading) == 1
    assert validation.count(end_heading) == 1
    section_start = validation.index(start_heading) + len(start_heading)
    section_end = validation.index(end_heading, section_start)
    if selector_kind == "section":
        return section_start, section_end
    section = validation[section_start:section_end]
    selector_pattern = (
        rf"^- {re.escape(selector)}.*?(?=^- |\Z)"
        if selector_kind == "bullet"
        else rf"^{re.escape(selector)}.*$"
    )
    matches = tuple(
        re.finditer(
            selector_pattern,
            section,
            flags=re.MULTILINE | (re.DOTALL if selector_kind == "bullet" else 0),
        )
    )
    assert len(matches) == 1
    return section_start + matches[0].start(), section_start + matches[0].end()


def _mutate_once_in_named_current_slice(
    validation: str,
    slice_name: str,
    original: str,
    replacement: str,
) -> str:
    historical_heading = "## Superseded historical hosted baseline"
    original_history = validation.split(historical_heading, maxsplit=1)[1]
    slice_start, slice_end = _named_current_mutation_slice(validation, slice_name)
    target = validation[slice_start:slice_end]
    pattern = re.compile(re.escape(original).replace(r"\ ", r"\s+"))
    assert len(tuple(pattern.finditer(target))) == 1
    mutated_target, replacement_count = pattern.subn(replacement, target, count=1)
    assert replacement_count == 1
    polluted_validation = (
        validation[:slice_start] + mutated_target + validation[slice_end:]
    )
    assert polluted_validation[:slice_start] == validation[:slice_start]
    assert polluted_validation[
        slice_start + len(mutated_target) :
    ] == validation[slice_end:]
    assert (
        polluted_validation.split(historical_heading, maxsplit=1)[1]
        == original_history
    )
    return polluted_validation


def _assert_current_mutation_hits_guard(
    polluted_validation: str,
    expected_guard: str,
) -> None:
    try:
        _assert_validation_matches_current_capture(polluted_validation)
    except AssertionError as error:
        if re.match(rf"^{re.escape(expected_guard)}", str(error)):
            return
        raise AssertionError(
            f"expected dedicated guard {expected_guard}"
        ) from error
    raise AssertionError(f"expected dedicated guard {expected_guard}")


@pytest.mark.parametrize(
    ("slice_name", "original", "replacement", "expected_guard"),
    (
        (
            "canonical",
            "6533a3f2e212d9a7c4e1af9f1d2dd77f7bd45a18",
            "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
            "current canonical/local evidence",
        ),
        (
            "canonical",
            "`capture_manifest_valid`",
            "`capture_manifest_invalid`",
            "current canonical/local evidence",
        ),
        (
            "canonical",
            "19 web test files / 369 tests",
            "18 web test files / 368 tests",
            "current canonical/local evidence",
        ),
        (
            "canonical",
            "strict MyPy over 35 source files",
            "strict MyPy over 34 source files",
            "current canonical/local evidence",
        ),
        (
            "canonical",
            "`957 passed`, 3 warnings, in 25.36s",
            "`956 passed`, 4 warnings",
            "current canonical/local evidence",
        ),
        (
            "local_smoke",
            "local deterministic stub smoke reported `PASS`",
            "local deterministic stub smoke reported `FAILURE`",
            "current canonical/local evidence",
        ),
        (
            "local_smoke",
            "local production `deterministic-qa` smoke reported `PASS`",
            "local production `deterministic-qa` smoke reported `FAILURE`",
            "current canonical/local evidence",
        ),
        (
            "local_smoke",
            "OpenAPI verification and the history-aware secret scan also "
            "reported `PASS`",
            "OpenAPI and secret scan were not inspected",
            "current canonical/local evidence",
        ),
        (
            "matrix_live",
            "`missing_openai_api_key`",
            "`live_provider_passed`",
            "current matrix",
        ),
        (
            "matrix_live",
            "attempt 1 of 3",
            "attempt 3 of 3",
            "current matrix",
        ),
        (
            "matrix_live",
            "zero approvals and no model IDs, tools, or root trace",
            "one approval with a model ID",
            "current matrix",
        ),
        (
            "matrix_github",
            "run `30598406930`",
            "run `99999999999`",
            "current matrix",
        ),
        (
            "hosted_final",
            "[job 91055708186]",
            "[job 99999999998]",
            "current hosted provenance",
        ),
        (
            "hosted_final",
            "reported `PASS` with `957 passed, 1 warning` in 46.45s",
            "reported `FAILURE`",
            "current hosted provenance",
        ),
        (
            "hosted_final",
            "[job 91055968348]",
            "[job 99999999997]",
            "current hosted provenance",
        ),
        (
            "hosted_final",
            "reported `PASS`, built the packaged image",
            "reported `SKIPPED`",
            "current hosted provenance",
        ),
        (
            "hosted_final",
            "Both job annotation APIs returned `[]`",
            "Job annotations were not inspected",
            "current hosted provenance",
        ),
        (
            "hosted_old",
            "60fe1243a5eeb760984d78d667f7efdc33630adc",
            "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
            "current hosted provenance",
        ),
        (
            "hosted_old",
            "[job 91047571247]",
            "[job 99999999995]",
            "current hosted provenance",
        ),
        (
            "hosted_old",
            "reported `FAILURE` with `1 failed, 950 passed, 1 warning` in 41.89s",
            "reported `PASS`",
            "current hosted provenance",
        ),
        (
            "hosted_old",
            "[job 91047810594]",
            "[job 99999999994]",
            "current hosted provenance",
        ),
        (
            "hosted_old",
            "reported `SKIPPED`, with container annotations `[]`",
            "reported `PASS`",
            "current hosted provenance",
        ),
        (
            "pending_primary",
            "Within the source, capture, and hosted activation lane",
            "Across every external lane",
            "current pending bullets",
        ),
        (
            "hosted_boundary",
            "`10001:10001`",
            "`0:0`",
            "current hosted boundary",
        ),
        (
            "hosted_boundary",
            "offline network-none `deterministic-qa` and `deployed-readonly` profiles",
            "one online profile",
            "current hosted boundary",
        ),
        (
            "hosted_boundary",
            "`30595724271`",
            "`99999999996`",
            "current hosted boundary",
        ),
    ),
)
def test_validation_rejects_current_activation_fact_mutation(
    slice_name: str,
    original: str,
    replacement: str,
    expected_guard: str,
) -> None:
    polluted_validation = _mutate_once_in_named_current_slice(
        _read("docs/validation.md"),
        slice_name,
        original,
        replacement,
    )
    _assert_current_mutation_hits_guard(polluted_validation, expected_guard)


@pytest.mark.parametrize(
    ("guard_function", "slice_name", "original", "replacement", "expected_guard"),
    (
        (
            "_assert_current_hosted_provenance",
            "hosted_final",
            "[job 91055708186]",
            "[job 99999999998]",
            "current hosted provenance",
        ),
        (
            "_assert_current_pending_bullets",
            "pending_primary",
            "Within the source, capture, and hosted activation lane",
            "Across every external lane",
            "current pending bullets",
        ),
        (
            "_assert_current_matrix",
            "matrix_github",
            "run `30598406930`",
            "run `99999999999`",
            "current matrix",
        ),
        (
            "_assert_current_hosted_boundary",
            "hosted_boundary",
            "`10001:10001`",
            "`0:0`",
            "current hosted boundary",
        ),
        (
            "_assert_current_canonical_local_evidence",
            "canonical",
            "`capture_manifest_valid`",
            "`capture_manifest_invalid`",
            "current canonical/local evidence",
        ),
    ),
)
def test_current_dedicated_guard_is_nonvacuous(
    monkeypatch: pytest.MonkeyPatch,
    guard_function: str,
    slice_name: str,
    original: str,
    replacement: str,
    expected_guard: str,
) -> None:
    polluted_validation = _mutate_once_in_named_current_slice(
        _read("docs/validation.md"),
        slice_name,
        original,
        replacement,
    )
    monkeypatch.setitem(globals(), guard_function, lambda _current_evidence: None)

    with pytest.raises(
        AssertionError,
        match=rf"^expected dedicated guard {re.escape(expected_guard)}$",
    ):
        _assert_current_mutation_hits_guard(polluted_validation, expected_guard)


@pytest.mark.parametrize(
    ("original", "replacement"),
    (
        (
            "clearing the sole `terminal = 1` marker",
            "retaining a terminal marker",
        ),
        (
            "fail closed with `ReceiptTransitionError`",
            "reopens successfully",
        ),
        (
            "Reopen performs no repair",
            "Reopen repairs the marker",
        ),
        (
            "exact event-row and text-byte stability",
            "event rows may change",
        ),
        (
            "one-time `terminal` add/backfill",
            "repeated schema rebuilds",
        ),
        (
            "exact database-byte stability",
            "database bytes may change",
        ),
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
    terminal_heading = "## Current Task35 transition-marker proof boundary"
    terminal_end_heading = "## Current Task35 pending external gates"
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

    with pytest.raises(AssertionError, match="transition marker semantic contract"):
        _assert_validation_matches_current_capture(polluted_validation)


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
    terminal_end_heading = "## Current Task35 pending external gates"
    polluted_validation = validation.replace(
        terminal_end_heading,
        f"{contradiction}\n\n{terminal_end_heading}",
        1,
    )

    with pytest.raises(AssertionError, match="transition marker semantic contract"):
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
    assert "## Current Task35 final evidence activation" in validation
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
