"""Fail-closed release scan that reports rule IDs without echoing secret values."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
from collections.abc import Iterable, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import NamedTuple


class Finding(NamedTuple):
    path: Path
    rule: str


_RULES = (
    (
        "provider-key",
        re.compile(rb"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}\b"),
    ),
    (
        "github-token",
        re.compile(
            rb"\b(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{30,})\b"
        ),
    ),
    (
        "authorization-header",
        re.compile(
            rb"(?i)\bauthorization\s*[:=]\s*(?:bearer|basic)\s+"
            rb"[A-Za-z0-9._~+/=-]{24,}"
        ),
    ),
    (
        "private-key",
        re.compile(rb"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"),
    ),
    (
        "serialized-sdk-state",
        re.compile(rb"[\"']state_json[\"']\s*:\s*[\[{]"),
    ),
)


def _relative(path: Path, root: Path) -> Path:
    try:
        return path.resolve().relative_to(root.resolve())
    except ValueError:
        return Path(path.name)


def scan_paths(paths: Sequence[Path], *, root: Path) -> list[Finding]:
    """Return deduplicated, safe findings for regular files in ``paths``."""

    findings: set[Finding] = set()
    for candidate in paths:
        path = Path(candidate)
        relative = _relative(path, root)
        if path.is_symlink():
            findings.add(Finding(relative, "unsafe-symlink"))
            continue
        if not path.is_file():
            findings.add(Finding(relative, "unreadable-file"))
            continue
        if path.name != ".env.example" and (
            path.name == ".env" or path.name.startswith(".env.")
        ):
            findings.add(Finding(relative, "tracked-env-file"))
        try:
            payload = path.read_bytes()
        except OSError:
            findings.add(Finding(relative, "unreadable-file"))
            continue
        for rule, pattern in _RULES:
            if pattern.search(payload) is not None:
                findings.add(Finding(relative, rule))
    return sorted(findings, key=lambda finding: (finding.path.as_posix(), finding.rule))


def format_findings(findings: Sequence[Finding]) -> str:
    """Format diagnostics without including any matched content."""

    return "\n".join(f"{finding.rule}: {finding.path.as_posix()}" for finding in findings)


def _git_candidate_paths(root: Path) -> list[Path]:
    completed = subprocess.run(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return [
        root / entry.decode("utf-8")
        for entry in completed.stdout.split(b"\0")
        if entry
    ]


def _files_under(paths: Iterable[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_file() or path.is_symlink():
            files.append(path)
        elif path.is_dir():
            files.extend(candidate for candidate in path.rglob("*") if candidate.is_file())
    return files


def _local_env_files(root: Path) -> list[Path]:
    ignored_roots = {".git", ".venv", "node_modules"}
    return [
        path
        for path in root.rglob(".env*")
        if not any(part in ignored_roots for part in path.relative_to(root).parts)
    ]


def _materialize_candidate_tree(
    source_paths: Sequence[Path],
    *,
    root: Path,
    destination: Path,
) -> list[Path]:
    copied: list[Path] = []
    for source in source_paths:
        if not source.is_file() or source.is_symlink():
            continue
        relative = _relative(source, root)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        copied.append(target)
    return copied


def release_findings(root: Path) -> list[Finding]:
    """Scan source, ignored local envs, build/capture/log outputs, and a candidate tree."""

    root = root.resolve()
    candidates = _git_candidate_paths(root)
    explicit_outputs = _files_under(
        (
            root / "web" / "dist",
            root / "docs" / "assets" / "final",
            root / "output" / "playwright",
            root / "logs",
        )
    )
    direct = list(dict.fromkeys([*candidates, *_local_env_files(root), *explicit_outputs]))
    findings = scan_paths(direct, root=root)
    with TemporaryDirectory(prefix="backchannel-release-candidate-") as temporary:
        candidate_root = Path(temporary) / "candidate"
        copied = _materialize_candidate_tree(candidates, root=root, destination=candidate_root)
        candidate_findings = scan_paths(copied, root=candidate_root)
    return sorted(
        set(findings).union(candidate_findings),
        key=lambda finding: (finding.path.as_posix(), finding.rule),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    arguments = parser.parse_args()
    findings = release_findings(arguments.root)
    if findings:
        print(f"Secret scan failed with {len(findings)} finding(s).")
        print(format_findings(findings))
        raise SystemExit(1)
    print("Secret scan passed: source, build, captures, logs, and candidate tree are clean.")


if __name__ == "__main__":
    main()
