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
    current_evidence_digest = hashlib.sha256(
        normalized_current_evidence.encode("utf-8")
    ).hexdigest()
    assert (
        current_evidence_digest
        == "76ab31bccc2a5b38a79dfe775a65676a1631e52430c9db5ab3fe0764e89d538b"
    )
    assert "718" not in current_evidence

    hosted_scope_pattern = re.compile(
        r"\b(?:CI|GitHub\s+Actions|workflow|verify\s+job|canonical|backend|"
        r"container(?:-smoke|\s+smoke|\s+job)?|packaged\s+Docker|"
        r"public\s+deployment|live\s+OpenAI|provider|release|docs-tip)\b",
        flags=re.IGNORECASE,
    )
    positive_word_pattern = re.compile(
        r"\b(?:pass|passed|passing|green|healthy|clean|cleanly|clear|"
        r"succeed|succeeded|succeeding|success|successful|successfully|verified|"
        r"validat(?:e|ed|es|ing)|confirm(?:ed|s|ing)?|ok|complete(?:d|ly)?|"
        r"operational|live|approv(?:e|ed|ing)|prov(?:e|ed|en|ing))\b"
        r"|\b(?:completed|finished)\s+(?:cleanly|successfully)\b"
        r"|\bwithout\s+(?:any\s+)?(?:failures?|errors?)\b"
        r"|\b(?:no|zero)\s+(?:failures?|errors?)\b"
        r"|\b(?:failure|error)-free\b"
        r"|\b(?:returned\s+)?exit(?:\s+code)?\s*(?:[:=]\s*)?0\b"
        r"|\bstatus\s*(?:[:=]\s*)?0\b",
        flags=re.IGNORECASE,
    )
    current_evidence_lines = tuple(
        line.strip() for line in current_evidence.splitlines() if line.strip()
    )
    expected_current_scope_lines = (
        "container, live OpenAI, deployment, or release evidence.",
        "- Hosted verification and a full canonical/backend gate are intentionally",
        "- No current Task33 full canonical/backend, GitHub Actions, or packaged",
        "container result has been observed.",
        (
            "| Local source and tests | Focused and full web source gates on "
            "`84af413…`; no current full canonical/backend result | **Verified "
            "locally** only in the named frontend lanes; not backend, hosted CI, "
            "container, browser deployment, live OpenAI, or release proof |"
        ),
        (
            "| Local final-build captures | "
            "[`docs/assets/final/manifest.json`](assets/final/manifest.json); six "
            "provenance-checked PNGs under [`docs/assets/final`](assets/final) | "
            "**Local capture evidence only** on source `84af413…` in Google Chrome "
            "at 1440×1024 and 390×844; not container, deployment, live OpenAI, or "
            "release proof |"
        ),
        (
            "| Live OpenAI | [`live-validation.md`](live-validation.md) records only "
            "a redacted invalid-key result | **Blocked / Unverified**; no valid "
            "`OPENAI_API_KEY`, one-run trace, or three consecutive successes |"
        ),
        (
            "| GitHub CI/container | No current Task33 GitHub Actions run or packaged "
            "container job has been observed | **Unverified** for this source/capture "
            "checkpoint; superseded Task32 proof is retained only below and is not "
            "inherited |"
        ),
        "| Public deployment | No public application origin | **Blocked / Unverified** |",
        (
            "| Tag/release | No release tag or GitHub release | **Blocked / "
            "Unverified** |"
        ),
        "## Hosted container evidence boundary",
        (
            "There is no current Task33 hosted-container result. The superseded "
            "historical section"
        ),
        "records prior packaged Docker evidence, but it is not current for this source/capture",
        "browser capture in CI, public deployment or browser URL, live OpenAI, real provider",
        "execution, container replacement or restart, long-lived `/data` volume persistence,",
        "multi-container SQLite, tag or release. The browser capture remained local; CI did",
        (
            "files. CI did not execute the browser capture. The six images prove "
            "only the local keyless"
        ),
        "SDK-stub final build; they do not prove live OpenAI, a public deployment, a container,",
        "a release, or a real provider mutation.",
        "This is local source/test evidence only. It does not prove a backend, hosted,",
        "container, public-network, live OpenAI, or release result, and the ordinary final",
        "container-replacement, abrupt-host-loss, or concurrent multi-container SQLite proof.",
        "container proof. It does not prove multi-process or concurrent multi-container SQLite,",
        "target-host scheduling, abrupt host loss, public latency, or live provider behavior.",
        "`84af413…` and is not public deployment, live OpenAI, container-host durability, tag,",
        "or release proof.",
        "route-gate release, retained admission charges, exact public `504 live_timeout`",
        "envelopes, retryable approve/decline claims, and no duplicate dispatch when provider",
        "process-termination proof, public deployment, or release claim.",
    )
    observed_current_scope_lines = tuple(
        line for line in current_evidence_lines if hosted_scope_pattern.search(line)
    )
    assert observed_current_scope_lines == expected_current_scope_lines, (
        "unexpected current scoped evidence lines: "
        f"{observed_current_scope_lines!r}"
    )
    expected_current_positive_lines = (
        "container, live OpenAI, deployment, or release evidence.",
        "- `npm run test:e2e` reported `29 passed` against the clean capture source",
        "- Pre-capture Task33 gates on clean source",
        (
            "`84af413c3a7833daa635e6a0b84e8f331cc1f7f2`: the focused client "
            "matrix passed"
        ),
        (
            "144 / 144 tests, and the full web source gate passed 369 / 369 "
            "tests plus the"
        ),
        "- Independent Task33 specification and quality reviews passed with no P0-P3",
        (
            "- Independent Task33 combined before/after visual review passed "
            "with no P0-P3"
        ),
        (
            "| Local source and tests | Focused and full web source gates on "
            "`84af413…`; no current full canonical/backend result | **Verified "
            "locally** only in the named frontend lanes; not backend, hosted CI, "
            "container, browser deployment, live OpenAI, or release proof |"
        ),
        (
            "| Local final-build captures | "
            "[`docs/assets/final/manifest.json`](assets/final/manifest.json); six "
            "provenance-checked PNGs under [`docs/assets/final`](assets/final) | "
            "**Local capture evidence only** on source `84af413…` in Google Chrome "
            "at 1440×1024 and 390×844; not container, deployment, live OpenAI, or "
            "release proof |"
        ),
        (
            "| Live OpenAI | [`live-validation.md`](live-validation.md) records only "
            "a redacted invalid-key result | **Blocked / Unverified**; no valid "
            "`OPENAI_API_KEY`, one-run trace, or three consecutive successes |"
        ),
        "checkpoint. Current local source and capture evidence does not prove local Docker,",
        "browser capture in CI, public deployment or browser URL, live OpenAI, real provider",
        "validates API and DOM provenance before each viewport screenshot.",
        (
            "produced against the clean source SHA, and the activation successor "
            "added the captured"
        ),
        (
            "files. CI did not execute the browser capture. The six images prove "
            "only the local keyless"
        ),
        "SDK-stub final build; they do not prove live OpenAI, a public deployment, a container,",
        "different clean frozen tree.",
        "The Task33 client validator accepts `completed`, `closed_without_action`, and",
        "This is local source/test evidence only. It does not prove a backend, hosted,",
        "container, public-network, live OpenAI, or release result, and the ordinary final",
        "cases prove these source-level parser boundaries; the screenshots do not prove",
        (
            "fresh equally bounded initial-plus-three cycle. Throughout retry, "
            "already verified"
        ),
        "The ordinary final-state screenshots do not prove transient store failure, retry",
        "container proof. It does not prove multi-process or concurrent multi-container SQLite,",
        "target-host scheduling, abrupt host loss, public latency, or live provider behavior.",
        "are source and raw-ASGI contract properties. The six screenshots do not prove",
        "second/third write failure, validation-before-charge, live-ledger separation, reset",
        "`84af413…` and is not public deployment, live OpenAI, container-host durability, tag,",
        "## Live deadline proof boundary",
        "The source contract bounds the complete live pre-approval graph and each live",
        "envelopes, retryable approve/decline claims, and no duplicate dispatch when provider",
        "execution committed before the timeout. The OpenAI client and the redacted live-smoke",
    )
    observed_current_positive_lines = tuple(
        line for line in current_evidence_lines if positive_word_pattern.search(line)
    )
    assert observed_current_positive_lines == expected_current_positive_lines, (
        "unexpected current positive evidence lines: "
        f"{observed_current_positive_lines!r}"
    )

    assert manifest["sourceCommit"] in current_evidence
    current_activation = "7a8092f9ac0be03274358bb9814db3d8e6a34507"
    excluded_nonproof_attempts = (
        "30206582233",
        "89805619343",
        "89805788728",
        "30589885023",
        "91029626933",
        "91029704182",
        "30590117346",
        "91030339125",
        "91030653603",
    )
    assert current_activation in current_evidence
    assert runtime_input["digest"] in current_evidence
    for current_identifier in (
        manifest["sourceCommit"],
        "84af413…",
        current_activation,
        "7a8092f…",
        runtime_input["digest"],
        "00d4636…",
    ):
        assert current_identifier not in historical_evidence
    for excluded_identifier in excluded_nonproof_attempts:
        assert excluded_identifier not in validation
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

    activation_heading = "## Current capture and evidence activation"
    activation_end_heading = "## Per-SHA proof matrix"
    activation_evidence = current_evidence.split(
        activation_heading,
        maxsplit=1,
    )[1].split(
        activation_end_heading,
        maxsplit=1,
    )[0]
    activation_bullets = tuple(
        " ".join(match.group(0).split())
        for match in re.finditer(
            r"^- .*?(?=^- |\Z)",
            activation_evidence,
            flags=re.MULTILINE | re.DOTALL,
        )
    )
    expected_activation_bullets = (
        f"- Capture source / frozen runtime: `{manifest['sourceCommit']}`",
        f"- Evidence activation: `{current_activation}`",
        (
            "- `npm run test:e2e` reported `29 passed` against the clean capture "
            "source in Google Chrome 150.0.7871.187 and produced six manifest-bound "
            "PNGs at 1440×1024 and 390×844. The run covered 29 capture contracts."
        ),
        (
            "- Five PNGs changed and `mobile-consent.png` remained byte-identical "
            "against the immediately prior capture set."
        ),
        (
            f"- Pre-capture Task33 gates on clean source `{manifest['sourceCommit']}`: "
            "the focused client matrix passed 144 / 144 tests, and the full web "
            "source gate passed 369 / 369 tests plus the TypeScript/Vite build."
        ),
        (
            "- Independent Task33 specification and quality reviews passed with no "
            "P0-P3 findings after the quality loop replaced one vacuous "
            "pending-consent test with a fully valid pending-approval fixture."
        ),
        (
            "- Independent Task33 combined before/after visual review passed with no "
            "P0-P3 findings."
        ),
        (
            "- Hosted verification and a full canonical/backend gate are "
            "intentionally excluded from this activation checkpoint."
        ),
        (
            "- No current Task33 full canonical/backend, GitHub Actions, or packaged "
            "container result has been observed."
        ),
    )
    assert activation_bullets == expected_activation_bullets

    pre_capture_gate = current_bullet("Pre-capture Task33 gates on clean source")
    assert manifest["sourceCommit"] in pre_capture_gate
    for pre_capture_fact in (
        "focused client matrix passed 144 / 144 tests",
        "full web source gate passed 369 / 369 tests",
        "TypeScript/Vite build",
    ):
        assert pre_capture_fact in pre_capture_gate
    for post_activation_only_fact in (
        "Python",
        "Ruff",
        "strict MyPy",
        "`capture_manifest_valid`",
        "GitHub Actions",
        "container",
    ):
        assert post_activation_only_fact not in pre_capture_gate

    source_review_gate = current_bullet(
        "Independent Task33 specification and quality reviews"
    )
    for source_review_fact in (
        "passed with no P0-P3 findings",
        "replaced one vacuous pending-consent test",
        "fully valid pending-approval fixture",
    ):
        assert source_review_fact in source_review_gate

    visual_review_gate = current_bullet(
        "Independent Task33 combined before/after visual review"
    )
    assert "passed with no P0-P3 findings" in visual_review_gate

    excluded_gate = current_bullet(
        "Hosted verification and a full canonical/backend gate"
    )
    assert "intentionally excluded from this activation checkpoint" in excluded_gate

    unobserved_gate = current_bullet(
        "No current Task33 full canonical/backend"
    )
    assert (
        "GitHub Actions, or packaged container result has been observed"
        in unobserved_gate
    )

    matrix_heading = "## Per-SHA proof matrix"
    hosted_boundary_heading = "## Hosted container evidence boundary"
    matrix_evidence = current_evidence.split(matrix_heading, maxsplit=1)[1].split(
        hosted_boundary_heading,
        maxsplit=1,
    )[0]
    matrix_rows = tuple(
        line.strip()
        for line in matrix_evidence.splitlines()
        if line.strip().startswith("|")
    )
    assert matrix_rows == (
        "| Lane | Exact evidence | Status and boundary |",
        "| --- | --- | --- |",
        (
            "| Local source and tests | Focused and full web source gates on "
            "`84af413…`; no current full canonical/backend result | **Verified "
            "locally** only in the named frontend lanes; not backend, hosted CI, "
            "container, browser deployment, live OpenAI, or release proof |"
        ),
        (
            "| Local final-build captures | "
            "[`docs/assets/final/manifest.json`](assets/final/manifest.json); six "
            "provenance-checked PNGs under [`docs/assets/final`](assets/final) | "
            "**Local capture evidence only** on source `84af413…` in Google Chrome "
            "at 1440×1024 and 390×844; not container, deployment, live OpenAI, or "
            "release proof |"
        ),
        (
            "| Live OpenAI | [`live-validation.md`](live-validation.md) records only "
            "a redacted invalid-key result | **Blocked / Unverified**; no valid "
            "`OPENAI_API_KEY`, one-run trace, or three consecutive successes |"
        ),
        (
            "| Local Docker | No Docker-family runtime is installed on this Mac | "
            "**Unverified locally** |"
        ),
        (
            "| GitHub CI/container | No current Task33 GitHub Actions run or packaged "
            "container job has been observed | **Unverified** for this source/capture "
            "checkpoint; superseded Task32 proof is retained only below and is not "
            "inherited |"
        ),
        (
            "| Public deployment | No public application origin | **Blocked / "
            "Unverified** |"
        ),
        (
            "| Tag/release | No release tag or GitHub release | **Blocked / "
            "Unverified** |"
        ),
    )

    hosted_boundary_end_heading = "## Capture contract"
    hosted_boundary_evidence = current_evidence.split(
        hosted_boundary_heading,
        maxsplit=1,
    )[1].split(
        hosted_boundary_end_heading,
        maxsplit=1,
    )[0]
    normalized_hosted_boundary_evidence = " ".join(
        hosted_boundary_evidence.split()
    )
    assert normalized_hosted_boundary_evidence == (
        "There is no current Task33 hosted-container result. The superseded "
        "historical section records prior packaged Docker evidence, but it is not "
        "current for this source/capture checkpoint. Current local source and "
        "capture evidence does not prove local Docker, browser capture in CI, "
        "public deployment or browser URL, live OpenAI, real provider execution, "
        "container replacement or restart, long-lived `/data` volume persistence, "
        "target-host durability/networking, abrupt host-loss or backup, concurrent "
        "multi-container SQLite, tag or release. The browser capture remained "
        "local; CI did not execute the browser capture."
    )

    for observed_current_fact in (
        "29 capture contracts",
        "Google Chrome 150.0.7871.187",
    ):
        assert observed_current_fact in normalized_current_evidence

    terminal_snapshot_heading = "## Terminal snapshot consistency proof boundary"
    terminal_snapshot_end_heading = "## SQLite file-permission proof boundary"
    assert terminal_snapshot_heading in current_evidence
    assert terminal_snapshot_end_heading in current_evidence
    terminal_snapshot_evidence = current_evidence.split(
        terminal_snapshot_heading,
        maxsplit=1,
    )[1].split(
        terminal_snapshot_end_heading,
        maxsplit=1,
    )[0]
    normalized_terminal_snapshot_evidence = " ".join(
        terminal_snapshot_evidence.split()
    )
    assert normalized_terminal_snapshot_evidence == (
        "The Task33 client validator accepts `completed`, `closed_without_action`, "
        "and `outcome_unknown` recovery snapshots only at terminal `currentStep: 5`. "
        "Each of those terminal statuses is rejected at steps 0 through 4, and all "
        "terminal snapshots continue to reject even a structurally valid pending "
        "approval. Nonterminal `in_progress` and `pending_approval` snapshots retain "
        "their previously valid steps, including a fully valid pending-consent "
        "fixture. The focused 144-test client matrix covers every terminal status at "
        "every step 0 through 5, the retained nonterminal cases, and the "
        "valid-pending-consent boundary. The full web source gate covered 369 tests "
        "plus the TypeScript/Vite build. This is local source/test evidence only. It "
        "does not prove a backend, hosted, container, public-network, live OpenAI, or "
        "release result, and the ordinary final state screenshots do not exercise "
        "malformed terminal snapshots."
    )
    for required_terminal_snapshot_fact in (
        "`completed`, `closed_without_action`, and `outcome_unknown` recovery "
        "snapshots only at terminal `currentStep: 5`",
        "terminal statuses is rejected at steps 0 through 4",
        "terminal snapshots continue to reject even a structurally valid pending "
        "approval",
        "Nonterminal `in_progress` and `pending_approval` snapshots retain their "
        "previously valid steps",
        "fully valid pending-consent fixture",
        "focused 144-test client matrix",
        "every terminal status at every step 0 through 5",
        "full web source gate covered 369 tests plus the TypeScript/Vite build",
        "local source/test evidence only",
        "does not prove a backend, hosted, container, public-network, live OpenAI, "
        "or release result",
        "screenshots do not exercise malformed terminal snapshots",
    ):
        assert (
            required_terminal_snapshot_fact in normalized_terminal_snapshot_evidence
        )

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
    assert observed_action_urls == []
    assert "Hosted validation successor:" not in current_evidence
    for required_current_boundary in (
        "No current Task33 GitHub Actions run or packaged container job has been observed",
        "**Unverified** for this source/capture checkpoint",
        "There is no current Task33 hosted-container result",
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
        "Current Task33 GitHub Actions passed",
        "Current Task33 hosted CI passed",
        "Current Task33 packaged container smoke passed",
        "Current Task33 full canonical gate passed",
        "Current Task33 backend gate passed",
        "reported `FAILURE`",
        "reported `SKIPPED`",
    ):
        assert forbidden_current_claim not in current_evidence
    assert "CI did not execute the browser capture" in current_evidence

    task32_capture = "a8057a8e0c29bdcc95e35949819c64005e5ee064"
    task32_activation = "9de054e5131ad3f610902d0a7bd4bd97c5968c7a"
    task32_digest = (
        "a5fdc8adc9788f181ace5f4de9cce7974344af02312cef2caa9e123193744503"
    )
    task32_pre_ci_successor = "c6c60d4354eba7348aef3245d19e787661544fa7"
    task32_pre_ci_run = "30588357735"
    task32_pre_ci_jobs = ("91024909140", "91025281220")
    task32_final_docs_tip = "abdf644b4c35ecfaee2d22917c4c621f9d123a5b"
    task32_final_run = "30589435325"
    task32_final_jobs = ("91028250060", "91028596793")
    task32_history_heading = (
        "### SQLite-permission capture and hosted baselines (superseded)"
    )
    task32_history_end_heading = (
        "### JSON-media capture and hosted baselines (superseded)"
    )
    assert task32_history_heading in historical_evidence
    assert task32_history_end_heading in historical_evidence
    task32_history = historical_evidence.split(
        task32_history_heading,
        maxsplit=1,
    )[1].split(
        task32_history_end_heading,
        maxsplit=1,
    )[0]
    normalized_task32_history = " ".join(task32_history.split())
    expected_task32_bullets = (
        f"- Prior capture source: `{task32_capture}`",
        f"- Prior evidence activation: `{task32_activation}`",
        (
            f"- Prior manifest digest: `{task32_digest}` across 84 runtime paths."
        ),
        (
            "- The prior local exact gate on the pre-CI documentation successor "
            f"`{task32_pre_ci_successor}` covered 29 capture contracts, "
            "`capture_manifest_valid`, 19 web test files / 345 tests and "
            "TypeScript/Vite, Ruff, strict MyPy over 35 source files, 778 / 778 "
            "Python tests with 3 warnings in 28.51s, deterministic stub and local "
            "single-process production smokes, OpenAPI verification, and the "
            "history-aware secret scan."
        ),
        (
            f"- Prior pre-CI GitHub Actions: [run {task32_pre_ci_run}]"
            f"({actions_base}/{task32_pre_ci_run}), including `verify` "
            f"([job {task32_pre_ci_jobs[0]}]({actions_base}/{task32_pre_ci_run}/job/"
            f"{task32_pre_ci_jobs[0]})) and `container-smoke` "
            f"([job {task32_pre_ci_jobs[1]}]({actions_base}/{task32_pre_ci_run}/job/"
            f"{task32_pre_ci_jobs[1]}))."
        ),
        (
            "- Both pre-CI jobs passed and both pre-CI job annotation APIs returned "
            "`[]`."
        ),
        f"- Prior final documentation tip: `{task32_final_docs_tip}`",
        (
            f"- Prior final-tip GitHub Actions: [run {task32_final_run}]"
            f"({actions_base}/{task32_final_run}), including `verify` "
            f"([job {task32_final_jobs[0]}]({actions_base}/{task32_final_run}/job/"
            f"{task32_final_jobs[0]})) and `container-smoke` "
            f"([job {task32_final_jobs[1]}]({actions_base}/{task32_final_run}/job/"
            f"{task32_final_jobs[1]}))."
        ),
        (
            "- Both final-tip jobs passed and both final-tip job annotation APIs "
            "returned `[]`."
        ),
        (
            "- Final-tip hosted `verify` covered `capture_manifest_valid`, 19 web "
            "test files / 345 tests, Ruff, strict MyPy over 35 source files, 796 "
            "Python tests, deterministic stub smoke, OpenAPI verification, and the "
            "history-aware secret scan."
        ),
        (
            "- Both prior `container-smoke` jobs built the packaged image with "
            "owner-only `/data` mode `0700`, confirmed runtime user `10001:10001`, "
            "and passed the offline network-none `deterministic-qa` and "
            "`deployed-readonly` profiles."
        ),
    )
    task32_bullets = tuple(
        " ".join(match.group(0).split())
        for match in re.finditer(
            r"^- .*?(?=^- |\n\nPrior capture source|\Z)",
            task32_history,
            flags=re.MULTILINE | re.DOTALL,
        )
    )
    assert task32_bullets == expected_task32_bullets
    expected_task32_summary = (
        "Prior capture source `a8057a8…`; activation `9de054e…`; manifest digest "
        "`a5fdc8a…`; pre-CI successor `c6c60d4…`; final docs tip `abdf644…`. This "
        "evidence remains truthful historical proof, but is superseded and is not "
        "current Task33 source, capture, hosted CI, or container proof."
    )
    assert normalized_task32_history == (
        " ".join(expected_task32_bullets) + " " + expected_task32_summary
    )
    for exact_historical_fact in (
        "29 capture contracts",
        "`capture_manifest_valid`",
        "19 web test files / 345 tests",
        "778 / 778 Python tests with 3 warnings in 28.51s",
        "Both pre-CI jobs passed and both pre-CI job annotation APIs returned `[]`",
        "Both final-tip jobs passed and both final-tip job annotation APIs returned `[]`",
        "796 Python tests",
        "owner-only `/data` mode `0700`",
        "runtime user `10001:10001`",
        "offline network-none `deterministic-qa` and `deployed-readonly` profiles",
    ):
        assert exact_historical_fact in normalized_task32_history
    task32_historical_action_urls = {
        f"{actions_base}/{task32_pre_ci_run}",
        *(
            f"{actions_base}/{task32_pre_ci_run}/job/{job}"
            for job in task32_pre_ci_jobs
        ),
        f"{actions_base}/{task32_final_run}",
        *(
            f"{actions_base}/{task32_final_run}/job/{job}"
            for job in task32_final_jobs
        ),
    }
    assert set(
        re.findall(
            r"https://github\.com/[^)\s]+/actions/runs/\d+(?:/job/\d+)?",
            task32_history,
        )
    ) == task32_historical_action_urls
    for run, jobs in (
        (task32_pre_ci_run, task32_pre_ci_jobs),
        (task32_final_run, task32_final_jobs),
    ):
        for job in jobs:
            assert (
                f"([job {job}]({actions_base}/{run}/job/{job}))"
                in task32_history
            )
        expected_job_binding = (
            f"including `verify` ([job {jobs[0]}]"
            f"({actions_base}/{run}/job/{jobs[0]})) and `container-smoke` "
            f"([job {jobs[1]}]({actions_base}/{run}/job/{jobs[1]}))."
        )
        assert expected_job_binding in normalized_task32_history

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
        task32_capture,
        "a8057a8…",
        task32_activation,
        "9de054e…",
        task32_digest,
        "a5fdc8a…",
        task32_pre_ci_successor,
        "c6c60d4…",
        task32_pre_ci_run,
        *task32_pre_ci_jobs,
        task32_final_docs_tip,
        "abdf644…",
        task32_final_run,
        *task32_final_jobs,
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
        task32_capture,
        "a8057a8…",
        task32_activation,
        "9de054e…",
        task32_digest,
        "a5fdc8a…",
        task32_pre_ci_successor,
        "c6c60d4…",
        task32_pre_ci_run,
        *task32_pre_ci_jobs,
        task32_final_docs_tip,
        "abdf644…",
        task32_final_run,
        *task32_final_jobs,
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

    with pytest.raises(AssertionError):
        _assert_validation_matches_current_capture(polluted_validation)


@pytest.mark.parametrize(
    "current_identifier",
    (
        "84af413c3a7833daa635e6a0b84e8f331cc1f7f2",
        "84af413…",
        "7a8092f9ac0be03274358bb9814db3d8e6a34507",
        "7a8092f…",
        "00d4636da32836689202464fc55deb5f4b2a8f5e6ffd02831d27d22a9146bc90",
        "00d4636…",
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
        "Current Task33 full canonical/backend gate returned exit 0.",
        "Current Task33 hosted CI status: 0.",
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
        "Current Task33 CI.\nA neutral evidence sentence.\nPassed.",
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

    with pytest.raises(AssertionError):
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

    with pytest.raises(AssertionError):
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

    with pytest.raises(AssertionError):
        _assert_validation_matches_current_capture(polluted_validation)


@pytest.mark.parametrize(
    "unsupported_positive_claim",
    (
        "Current Task33 public deployment passed.",
        "Current Task33 live OpenAI passed.",
        "Current Task33 container restart and replacement persistence passed.",
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

    with pytest.raises(AssertionError):
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

    with pytest.raises(AssertionError):
        _assert_validation_matches_current_capture(polluted_validation)


@pytest.mark.parametrize(
    "unexpected_positive_claim",
    (
        "Current Task33 CI passed.",
        "Current Task33 CI. Passed.",
        "Current Task33 CI.\nPassed.",
        "Passed. Current Task33 CI.",
        "Current Task33 container job passed.",
        "Current Task33 verify job passed.",
        "Task33 GitHub Actions passed successfully.",
        "The current Task33 GitHub workflow passed.",
        "Task33 container smoke. Result: verified.",
        "Task33 container smoke.\nResult: verified.",
        "Status: verified. Task33 container smoke.",
        "Healthy. Current Task33 workflow.",
        "Completed cleanly. Current Task33 packaged Docker.",
        "No failures. Current Task33 verify job.",
        "Current Task33 CI completed cleanly.",
        "Current Task33 CI is passing.",
        "Current Task33 workflow is healthy.",
        "Current Task33 container smoke completed without failures.",
        "Current Task33 CI has no failures.",
        "Current Task33 CI has zero failures.",
        "Current Task33 CI is error-free.",
        (
            "- No current Task33 full canonical/backend, GitHub Actions, "
            "or packaged container result has been observed.\n"
            "Passed."
        ),
        (
            "Passed.\n"
            "- No current Task33 full canonical/backend, GitHub Actions, "
            "or packaged container result has been observed."
        ),
        "The full Python suite passed all 718 tests.",
        "Canonical backend: 718 passed.",
        "Current Task33 GitHub Actions passed successfully.",
        "Current Task33 GitHub Actions run is green.",
        "Current Task33 hosted CI succeeded.",
        "Current Task33 packaged container smoke passed successfully.",
        "Current Task33 container-smoke job is green.",
        "Current Task33 container smoke succeeded.",
        "Current Task33 workflow green.",
        "Current Task33 packaged Docker successful.",
        "Current Task33 container-smoke verified.",
        "Current Task33 full canonical gate passed 799 tests.",
        "Current Task33 backend gate succeeded.",
        "Current Task33 docs-tip workflow passed.",
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
            "Pre-capture Task33 gates on clean source",
            "focused client matrix passed 144 / 144 tests",
            "focused client matrix passed 143 / 144 tests",
        ),
        (
            "Pre-capture Task33 gates on clean source",
            "full web source gate passed 369 / 369 tests",
            "full web source gate passed 368 / 369 tests",
        ),
        (
            "Independent Task33 specification and quality reviews",
            "fully valid pending-approval fixture",
            "placeholder fixture",
        ),
        (
            "No current Task33 full canonical/backend",
            "result has been observed",
            "result passed",
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
        (
            "only at terminal `currentStep: 5`",
            "at any `currentStep`",
        ),
        (
            "terminal statuses is rejected at steps 0 through 4",
            "terminal statuses remain accepted at steps 0 through 4",
        ),
        (
            "terminal snapshots continue to reject even a structurally valid pending "
            "approval",
            "terminal snapshots accept a pending approval",
        ),
        (
            "Nonterminal `in_progress` and `pending_approval` snapshots retain their "
            "previously valid steps",
            "nonterminal snapshots are also forced to step 5",
        ),
        ("fully valid pending-consent fixture", "placeholder pending consent"),
        ("focused 144-test client matrix", "an unspecified focused client matrix"),
        (
            "every terminal status at every step 0 through 5",
            "one terminal status at step 5",
        ),
        (
            "full web source gate covered 369 tests plus the TypeScript/Vite build",
            "a partial web run",
        ),
    ),
)
def test_validation_rejects_terminal_snapshot_contract_mutation(
    original: str,
    replacement: str,
) -> None:
    validation = _read("docs/validation.md")
    terminal_heading = "## Terminal snapshot consistency proof boundary"
    terminal_end_heading = "## SQLite file-permission proof boundary"
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


@pytest.mark.parametrize(
    "contradiction",
    (
        "Terminal steps 0 through 4 are accepted.",
        "`completed` at `currentStep: 2` is valid.",
        "Terminal snapshots may retain a pending approval.",
        "Nonterminal snapshots are forced to `currentStep: 5`.",
        "The matrix covers only `completed` at step 5.",
        "The full web source gate failed.",
    ),
)
def test_validation_rejects_terminal_snapshot_contradictions(
    contradiction: str,
) -> None:
    validation = _read("docs/validation.md")
    terminal_end_heading = "## SQLite file-permission proof boundary"
    polluted_validation = validation.replace(
        terminal_end_heading,
        f"{contradiction}\n\n{terminal_end_heading}",
        1,
    )

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
