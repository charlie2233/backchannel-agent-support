from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.secret_scan import (
    DEFAULT_LIMITS,
    ScanAborted,
    ScanLimits,
    _Scanner,
    scan_repository,
)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "secret_scan.py"


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )


def _repository(tmp_path: Path, *, files: dict[str, str] | None = None) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--quiet")
    _git(repository, "config", "user.email", "scanner@example.invalid")
    _git(repository, "config", "user.name", "Scanner Test")
    baseline = {".env.example": "OPENAI_API_KEY=\n", "safe.txt": "public\n"}
    baseline.update(files or {})
    for name, content in baseline.items():
        target = repository / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "--quiet", "-m", "baseline")
    return repository


def _rules(repository: Path, *, limits: ScanLimits = DEFAULT_LIMITS) -> set[str]:
    return {finding.rule for finding in scan_repository(repository, limits=limits)}


def test_only_exact_root_env_example_is_allowed(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    assert scan_repository(repository) == ()

    (repository / ".env.local").write_text("SAFE_NAME=value\n", encoding="utf-8")
    _git(repository, "add", "--force", ".env.local")

    assert "disallowed_env_file" in _rules(repository)


@pytest.mark.parametrize(
    ("content", "rule"),
    [
        ("-----BEGIN " + "PRIVATE KEY-----\nnot-real\n", "private_key"),
        ("sk-" + "A" * 48, "openai_token"),
        ("ghp_" + "B" * 40, "github_token"),
        ("AKIA" + "C" * 16, "aws_access_key"),
        ("Author" + "ization: Bearer " + "D" * 32, "authorization_value"),
        ("OPENAI_API_KEY=" + "E" * 40, "sensitive_assignment"),
    ],
)
def test_relaxed_policy_rejects_credential_shapes_and_values(
    tmp_path: Path,
    content: str,
    rule: str,
) -> None:
    repository = _repository(tmp_path)
    (repository / "candidate.txt").write_text(content, encoding="utf-8")

    assert rule in _rules(repository)


def test_strict_policy_scans_ignored_public_outputs_and_png_raw_metadata(
    tmp_path: Path,
) -> None:
    repository = _repository(
        tmp_path,
        files={
            ".gitignore": "web/dist/\ndocs/assets/final/\n*.log\nplaywright-report/\n"
            "test-results/\nblob-report/\n"
        },
    )
    marker = "state_" + "json"
    paths = (
        "web/dist/app.js",
        "docs/assets/final/frame.png",
        "runtime.log",
        "playwright-report/index.html",
        "test-results/result.txt",
        "blob-report/report.zip",
    )
    for name in paths:
        target = repository / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"PNG metadata " + marker.encode("ascii"))

    findings = scan_repository(repository)

    assert {finding.path for finding in findings if finding.surface == "public"} == set(
        paths
    )
    assert {finding.rule for finding in findings} == {"serialized_state_marker"}


def test_real_head_archive_is_scanned_not_the_worktree_substitute(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    secret = "sk-" + "F" * 48
    path = repository / "archived.txt"
    path.write_text(secret, encoding="utf-8")
    _git(repository, "add", "archived.txt")
    _git(repository, "commit", "--quiet", "-m", "archive leak")
    path.write_text("safe working tree\n", encoding="utf-8")

    findings = scan_repository(repository)

    assert any(
        finding.surface == "archive" and finding.rule == "openai_token"
        for finding in findings
    )


def test_deleted_secret_still_fails_reachable_head_history_scan(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    secret_path = repository / "deleted.txt"
    secret_path.write_text("ghp_" + "G" * 40, encoding="utf-8")
    _git(repository, "add", "deleted.txt")
    _git(repository, "commit", "--quiet", "-m", "add secret")
    _git(repository, "rm", "--quiet", "deleted.txt")
    _git(repository, "commit", "--quiet", "-m", "delete secret")

    findings = scan_repository(repository)

    assert any(
        finding.surface == "history" and finding.rule == "github_token"
        for finding in findings
    )


def test_reachable_commit_message_payload_is_scanned(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    secret = "sk-" + "M" * 48
    _git(repository, "commit", "--allow-empty", "--quiet", "-m", secret)

    findings = scan_repository(repository)

    assert any(
        finding.surface == "history" and finding.rule == "openai_token"
        for finding in findings
    )


def test_reachable_annotated_tag_payload_is_scanned(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    secret = "sk-" + "N" * 48
    _git(repository, "tag", "--annotate", "release-review", "--message", secret)

    findings = scan_repository(repository)

    assert any(
        finding.surface == "history" and finding.rule == "openai_token"
        for finding in findings
    )


def test_unexpected_reachable_object_type_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    original = _Scanner._object_info

    def include_unexpected_type(
        scanner: _Scanner,
        object_ids: list[bytes],
        *,
        surface: str,
    ) -> list[tuple[bytes, str, int]]:
        information = original(scanner, object_ids, surface=surface)
        if surface == "history":
            information.append((b"0" * 40, "unexpected", 0))
        return information

    monkeypatch.setattr(_Scanner, "_object_info", include_unexpected_type)

    with pytest.raises(ScanAborted) as error:
        scan_repository(repository)

    assert error.value.finding.rule == "unexpected_object_type"


def test_historical_tree_component_obeys_path_size_limit(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    long_alias = repository / ("historical-" + "a" * 72 + ".txt")
    long_alias.write_text("public\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "--quiet", "-m", "add historical alias")
    long_alias.unlink()
    _git(repository, "add", "--all")
    _git(repository, "commit", "--quiet", "-m", "remove historical alias")

    with pytest.raises(ScanAborted) as error:
        scan_repository(
            repository,
            limits=replace(DEFAULT_LIMITS, max_path_bytes=32),
        )

    assert error.value.finding.rule == "path_size_limit"
    assert error.value.finding.surface == "history"
    assert "historical-" not in error.value.finding.path


def test_historical_tree_entries_obey_dedicated_path_count_limit(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    aliases = [repository / f"alias-{index:02d}.txt" for index in range(20)]
    for alias in aliases:
        alias.write_text("public\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "--quiet", "-m", "add historical aliases")
    for alias in aliases:
        alias.unlink()
    _git(repository, "add", "--all")
    _git(repository, "commit", "--quiet", "-m", "remove historical aliases")

    with pytest.raises(ScanAborted) as error:
        scan_repository(
            repository,
            limits=replace(DEFAULT_LIMITS, max_paths=12),
        )

    assert error.value.finding.rule == "path_count_limit"
    assert error.value.finding.surface == "history"
    assert "alias-" not in error.value.finding.path


@pytest.mark.parametrize(
    ("limits", "expected_rule"),
    [
        (replace(DEFAULT_LIMITS, max_paths=1), "path_count_limit"),
        (replace(DEFAULT_LIMITS, max_path_bytes=3), "path_size_limit"),
        (replace(DEFAULT_LIMITS, max_archive_members=1), "archive_member_limit"),
        (replace(DEFAULT_LIMITS, max_objects=1), "object_count_limit"),
        (replace(DEFAULT_LIMITS, max_file_bytes=3), "file_size_limit"),
        (replace(DEFAULT_LIMITS, max_aggregate_bytes=3), "aggregate_size_limit"),
    ],
)
def test_budgets_abort_early_and_fail_closed(
    tmp_path: Path,
    limits: ScanLimits,
    expected_rule: str,
) -> None:
    repository = _repository(tmp_path)

    with pytest.raises(ScanAborted) as error:
        scan_repository(repository, limits=limits)

    assert error.value.finding.rule == expected_rule


def test_ignored_filesystem_entries_are_charged_before_they_are_skipped(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path, files={".gitignore": "ignored/\n"})
    ignored = repository / "ignored"
    for index in range(8):
        target = ignored / f"nested-{index}" / "safe.txt"
        target.parent.mkdir(parents=True)
        target.write_text("public\n", encoding="utf-8")

    with pytest.raises(ScanAborted) as error:
        scan_repository(repository, limits=replace(DEFAULT_LIMITS, max_paths=5))

    assert error.value.finding.rule == "path_count_limit"


def test_scan_has_one_overall_deadline_across_discovery_and_git(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    with pytest.raises(ScanAborted) as error:
        scan_repository(
            repository,
            limits=replace(DEFAULT_LIMITS, overall_timeout_seconds=0),
        )

    assert error.value.finding.rule == "scan_timeout"


def test_staged_secret_is_scanned_even_after_worktree_is_replaced(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    staged = repository / "staged.txt"
    staged.write_text("sk-" + "L" * 48, encoding="utf-8")
    _git(repository, "add", "staged.txt")
    staged.write_text("safe worktree\n", encoding="utf-8")

    findings = scan_repository(repository)

    assert any(
        finding.surface == "index"
        and finding.path == "staged.txt"
        and finding.rule == "openai_token"
        for finding in findings
    )


def test_force_added_env_is_rejected_even_when_worktree_copy_is_deleted(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    staged = repository / ".env.local"
    staged.write_text("SAFE_NAME=value\n", encoding="utf-8")
    _git(repository, "add", "--force", ".env.local")
    staged.unlink()

    findings = scan_repository(repository)

    assert any(
        finding.surface in {"index", "current"}
        and finding.path == ".env.local"
        and finding.rule == "disallowed_env_file"
        for finding in findings
    )


def test_symlinks_and_special_files_are_rejected_without_following(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("sk-" + "H" * 48, encoding="utf-8")
    (repository / "escape.txt").symlink_to(outside)
    fifo = repository / "events.pipe"
    os.mkfifo(fifo)

    findings = scan_repository(repository)

    unsafe_paths = {
        finding.path for finding in findings if finding.rule == "unsafe_file_type"
    }
    assert unsafe_paths == {"escape.txt", "events.pipe"}
    assert all(finding.rule != "openai_token" for finding in findings)


def test_control_characters_in_paths_are_read_but_escaped_in_diagnostics(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    path = repository / "line\nbreak-\u00e9.txt"
    path.write_text("sk-" + "K" * 48, encoding="utf-8")

    findings = scan_repository(repository)

    finding = next(item for item in findings if item.rule == "openai_token")
    assert finding.surface == "current"
    assert finding.path == "line\\x0abreak-\u00e9.txt"
    assert "\n" not in finding.diagnostic()


def test_strict_public_policy_rejects_bearer_and_secret_markers(tmp_path: Path) -> None:
    repository = _repository(tmp_path, files={".gitignore": "web/dist/\n"})
    artifact = repository / "web" / "dist" / "app.js"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(
        "Author" + "ization: Bearer placeholder\nclient_" + "secret\n",
        encoding="utf-8",
    )

    findings = scan_repository(repository)

    public_rules = {
        finding.rule for finding in findings if finding.surface == "public"
    }
    assert public_rules == {"authorization_bearer", "secret_marker"}


def test_cli_diagnostics_are_redacted_to_surface_path_and_rule(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    secret = "sk-" + "J" * 48
    (repository / "leak.txt").write_text("prefix=" + secret, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=repository,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    diagnostics = result.stdout + result.stderr

    assert result.returncode == 1
    assert diagnostics.strip() == "current:leak.txt:openai_token"
    assert secret not in diagnostics
    assert "prefix=" not in diagnostics
