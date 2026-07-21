"""Fail-closed release scan that reports rule IDs without echoing secret values."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import select
import subprocess
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory, TemporaryFile
from typing import BinaryIO, NamedTuple


class SecretScanError(RuntimeError):
    """A repository or Git protocol failure that must not expose its payload."""


class Finding(NamedTuple):
    path: bytes
    rule: str


SCAN_ERROR_MESSAGE = (
    "Secret scan failed closed: repository and release inputs could not be fully verified."
)

_RULES = (
    (
        "provider-key",
        re.compile(rb"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}\b"),
    ),
    (
        "github-token",
        re.compile(rb"\b(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{30,})\b"),
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

_BLOB_MODES = {b"100644", b"100755", b"120000"}
_ZERO_MODE = b"000000"
_GITLINK_MODE = b"160000"
_VALID_STATUSES = {b"A", b"D", b"M", b"T"}
_OID_LENGTHS = {40, 64}
_MAX_RELEASE_PATHS = 100_000
_MAX_RELEASE_TOTAL_BYTES = 512 * 1024 * 1024
_MAX_RELEASE_FILE_BYTES = 64 * 1024 * 1024
_MAX_CANDIDATE_TREE_BYTES = 256 * 1024 * 1024
_MAX_GIT_CAPTURE_BYTES = 64 * 1024 * 1024
_MAX_GIT_BLOB_BYTES = 64 * 1024 * 1024
_GIT_COMMAND_TIMEOUT_SECONDS = 60.0
_CAT_FILE_HEADER_BYTES = 256
_READ_CHUNK_BYTES = 64 * 1024
_PROCESS_POLL_SECONDS = 0.01


class _ReleaseBudget:
    def __init__(self) -> None:
        self.path_count = 0
        self.scanned_bytes = 0

    def count_path(self) -> None:
        if _MAX_RELEASE_PATHS <= 0 or self.path_count >= _MAX_RELEASE_PATHS:
            raise SecretScanError
        self.path_count += 1

    def consume_bytes(self, amount: int) -> None:
        if (
            amount < 0
            or _MAX_RELEASE_TOTAL_BYTES < 0
            or self.scanned_bytes > _MAX_RELEASE_TOTAL_BYTES - amount
        ):
            raise SecretScanError
        self.scanned_bytes += amount

    def remaining_bytes(self) -> int:
        if _MAX_RELEASE_TOTAL_BYTES < self.scanned_bytes:
            raise SecretScanError
        return _MAX_RELEASE_TOTAL_BYTES - self.scanned_bytes


def _relative_bytes(path: Path, root: Path) -> bytes:
    try:
        relative = os.path.relpath(os.fspath(path), os.fspath(root))
    except (OSError, ValueError):
        relative = path.name
    return os.fsencode(relative)


def _is_env_path(path: bytes) -> bool:
    name = path.rsplit(b"/", 1)[-1]
    return name != b".env.example" and name.startswith(b".env")


def _payload_rules(payload: bytes) -> tuple[str, ...]:
    return tuple(rule for rule, pattern in _RULES if pattern.search(payload) is not None)


def scan_paths(
    paths: Sequence[Path],
    *,
    root: Path,
    budget: _ReleaseBudget | None = None,
) -> list[Finding]:
    """Return deduplicated, safe findings for regular files in ``paths``."""

    if budget is None:
        budget = _ReleaseBudget()
    findings: set[Finding] = set()
    for candidate in paths:
        budget.count_path()
        path = Path(candidate)
        relative = _relative_bytes(path, root)
        if path.is_symlink():
            findings.add(Finding(relative, "unsafe-symlink"))
            continue
        if not path.is_file():
            findings.add(Finding(relative, "unreadable-file"))
            continue
        if _is_env_path(relative):
            findings.add(Finding(relative, "tracked-env-file"))
        payload: bytes | None = None
        remaining_total = budget.remaining_bytes()
        allowed = min(_MAX_RELEASE_FILE_BYTES, remaining_total)
        if allowed < 0:
            raise SecretScanError
        try:
            with path.open("rb") as stream:
                payload = stream.read(allowed + 1)
        except OSError:
            findings.add(Finding(relative, "unreadable-file"))
            continue
        if payload is None:
            findings.add(Finding(relative, "unreadable-file"))
            continue
        if len(payload) > remaining_total:
            raise SecretScanError
        budget.consume_bytes(len(payload))
        if len(payload) > _MAX_RELEASE_FILE_BYTES:
            findings.add(Finding(relative, "oversize-file"))
            continue
        for rule in _payload_rules(payload):
            findings.add(Finding(relative, rule))
    return sorted(findings, key=lambda finding: (finding.path, finding.rule))


def _path_identifier(path: bytes) -> str:
    return hashlib.sha256(path).hexdigest()


def format_findings(findings: Sequence[Finding]) -> str:
    """Format diagnostics using only rule IDs and opaque path identifiers."""

    return "\n".join(
        f"{finding.rule}: path-sha256:{_path_identifier(finding.path)}" for finding in findings
    )


def _git_environment() -> dict[str, str]:
    environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        }
    )
    return environment


def _close_quietly(stream: BinaryIO | None) -> None:
    if stream is None:
        return
    try:
        stream.close()
    except OSError:
        pass


def _stop_process_quietly(process: subprocess.Popen[bytes]) -> None:
    running = True
    try:
        running = process.poll() is None
    except OSError:
        pass
    if running:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _run_git(
    root: Path,
    arguments: Sequence[str],
    *,
    input_bytes: bytes | None = None,
) -> bytes:
    if (
        _GIT_COMMAND_TIMEOUT_SECONDS <= 0
        or _MAX_GIT_CAPTURE_BYTES < 0
        or (input_bytes is not None and len(input_bytes) > _MAX_GIT_CAPTURE_BYTES)
    ):
        raise SecretScanError

    capture: BinaryIO | None = None
    input_stream: BinaryIO | None = None
    temporary_failed = False
    try:
        capture = TemporaryFile(prefix="backchannel-git-output-")
        if input_bytes is not None:
            input_stream = TemporaryFile(prefix="backchannel-git-input-")
            if input_stream.write(input_bytes) != len(input_bytes):
                temporary_failed = True
            input_stream.flush()
            input_stream.seek(0)
    except OSError:
        temporary_failed = True
    if temporary_failed or capture is None:
        _close_quietly(input_stream)
        _close_quietly(capture)
        raise SecretScanError

    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            ["git", *arguments],
            cwd=root,
            stdin=input_stream if input_stream is not None else subprocess.DEVNULL,
            stdout=capture,
            stderr=subprocess.DEVNULL,
            env=_git_environment(),
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    if process is None:
        _close_quietly(input_stream)
        _close_quietly(capture)
        raise SecretScanError

    deadline = time.monotonic() + _GIT_COMMAND_TIMEOUT_SECONDS
    returncode: int | None = None
    failed = False
    while not failed:
        now = time.monotonic()
        if now >= deadline:
            failed = True
            break
        size: int | None = None
        try:
            size = os.fstat(capture.fileno()).st_size
            returncode = process.poll()
        except OSError:
            failed = True
        if size is None or size > _MAX_GIT_CAPTURE_BYTES:
            failed = True
        if failed or returncode is not None:
            break
        time.sleep(min(_PROCESS_POLL_SECONDS, deadline - now))

    if returncode != 0:
        failed = True
    if failed:
        _stop_process_quietly(process)

    payload: bytes | None = None
    if not failed:
        try:
            capture.seek(0)
            payload = capture.read(_MAX_GIT_CAPTURE_BYTES + 1)
        except OSError:
            pass
        if payload is None or len(payload) > _MAX_GIT_CAPTURE_BYTES:
            failed = True

    _close_quietly(input_stream)
    _close_quietly(capture)
    if failed or payload is None:
        raise SecretScanError
    return payload


def _validated_oid(payload: bytes, *, expected_length: int | None = None) -> bytes:
    oid = payload.removesuffix(b"\n")
    if b"\n" in oid or len(oid) not in _OID_LENGTHS:
        raise SecretScanError
    if expected_length is not None and len(oid) != expected_length:
        raise SecretScanError
    if re.fullmatch(rb"[0-9a-f]+", oid) is None:
        raise SecretScanError
    return oid


def _record_blob_path(
    blobs: dict[bytes, set[bytes]],
    *,
    mode: bytes,
    oid: bytes,
    path: bytes,
    budget: _ReleaseBudget,
) -> None:
    zero_oid = b"0" * len(oid)
    if mode == _ZERO_MODE:
        if oid != zero_oid:
            raise SecretScanError
        return
    if oid == zero_oid:
        raise SecretScanError
    if mode == _GITLINK_MODE:
        raise SecretScanError
    if mode not in _BLOB_MODES:
        raise SecretScanError
    paths = blobs.get(oid)
    if paths is not None and path in paths:
        return
    budget.count_path()
    if paths is None:
        blobs[oid] = {path}
    else:
        paths.add(path)


def _parse_diff_tree_output(
    payload: bytes,
    *,
    oid_length: int,
    budget: _ReleaseBudget | None = None,
) -> dict[bytes, set[bytes]]:
    """Map blobs to paths, charging each unique OID/path pair before insertion."""

    if budget is None:
        budget = _ReleaseBudget()
    if not payload:
        return {}
    fields = payload.split(b"\0")
    if fields[-1] != b"" or len(fields) % 2 != 1:
        raise SecretScanError
    blobs: dict[bytes, set[bytes]] = {}
    oid_pattern = rb"[0-9a-f]{" + str(oid_length).encode("ascii") + rb"}"
    metadata_pattern = re.compile(
        rb":([0-7]{6}) ([0-7]{6}) (" + oid_pattern + rb") (" + oid_pattern + rb") ([A-Z][0-9]*)"
    )
    records = fields[:-1]
    for index in range(0, len(records), 2):
        metadata = records[index]
        path = records[index + 1]
        match = metadata_pattern.fullmatch(metadata)
        if match is None or not path or path.startswith(b"/"):
            raise SecretScanError
        old_mode, new_mode, old_oid, new_oid, status = match.groups()
        if status not in _VALID_STATUSES:
            raise SecretScanError
        _record_blob_path(blobs, mode=old_mode, oid=old_oid, path=path, budget=budget)
        _record_blob_path(blobs, mode=new_mode, oid=new_oid, path=path, budget=budget)
    return blobs


def _parse_cat_file_header(header: bytes, *, expected_oid: bytes) -> int:
    if not header.endswith(b"\n"):
        raise SecretScanError
    fields = header[:-1].split(b" ")
    if len(fields) != 3:
        raise SecretScanError
    oid, object_type, raw_size = fields
    if oid != expected_oid or object_type != b"blob":
        raise SecretScanError
    if re.fullmatch(rb"(?:0|[1-9][0-9]*)", raw_size) is None:
        raise SecretScanError
    size: int | None = None
    try:
        size = int(raw_size)
    except ValueError:
        pass
    if size is None:
        raise SecretScanError
    return size


class _DeadlineReader:
    def __init__(self, stream: BinaryIO, *, deadline: float) -> None:
        descriptor: int | None = None
        try:
            descriptor = stream.fileno()
        except (OSError, ValueError):
            pass
        if descriptor is None:
            raise SecretScanError
        self._descriptor = descriptor
        self._deadline = deadline
        self._buffer = bytearray()

    def _read_available(self) -> bytes:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise SecretScanError
        ready: list[int] | None = None
        try:
            ready = select.select([self._descriptor], [], [], remaining)[0]
        except (OSError, ValueError):
            pass
        if not ready:
            raise SecretScanError
        chunk: bytes | None = None
        try:
            chunk = os.read(self._descriptor, _READ_CHUNK_BYTES)
        except OSError:
            pass
        if chunk is None:
            raise SecretScanError
        return chunk

    def read_line(self, *, maximum_bytes: int) -> bytes:
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                if newline + 1 > maximum_bytes:
                    raise SecretScanError
                line = bytes(self._buffer[: newline + 1])
                del self._buffer[: newline + 1]
                return line
            if len(self._buffer) >= maximum_bytes:
                raise SecretScanError
            chunk = self._read_available()
            if not chunk:
                raise SecretScanError
            self._buffer.extend(chunk)

    def read_exact(self, size: int) -> bytes:
        if size < 0:
            raise SecretScanError
        payload = bytearray()
        while len(payload) < size:
            if self._buffer:
                take = min(size - len(payload), len(self._buffer))
                payload.extend(self._buffer[:take])
                del self._buffer[:take]
                continue
            chunk = self._read_available()
            if not chunk:
                raise SecretScanError
            take = min(size - len(payload), len(chunk))
            payload.extend(chunk[:take])
            self._buffer.extend(chunk[take:])
        return bytes(payload)

    def expect_eof(self) -> None:
        if self._buffer or self._read_available() != b"":
            raise SecretScanError


def _read_git_blob_rules(
    root: Path,
    oids: Sequence[bytes],
    *,
    budget: _ReleaseBudget | None = None,
) -> dict[bytes, tuple[str, ...]]:
    if not oids:
        return {}
    if budget is None:
        budget = _ReleaseBudget()
    if _GIT_COMMAND_TIMEOUT_SECONDS <= 0 or _MAX_GIT_BLOB_BYTES < 0:
        raise SecretScanError
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            ["git", "cat-file", "--batch"],
            cwd=root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_git_environment(),
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    if process is None:
        raise SecretScanError
    rules_by_oid: dict[bytes, tuple[str, ...]] = {}
    deadline = time.monotonic() + _GIT_COMMAND_TIMEOUT_SECONDS
    failed = False
    try:
        if process.stdin is None or process.stdout is None:
            raise SecretScanError
        reader = _DeadlineReader(process.stdout, deadline=deadline)
        for oid in oids:
            _validated_oid(oid)
            if time.monotonic() >= deadline:
                raise SecretScanError
            process.stdin.write(oid + b"\n")
            process.stdin.flush()
            header = reader.read_line(maximum_bytes=_CAT_FILE_HEADER_BYTES)
            size = _parse_cat_file_header(header, expected_oid=oid)
            if size > _MAX_GIT_BLOB_BYTES:
                raise SecretScanError
            budget.consume_bytes(size)
            payload = reader.read_exact(size)
            if reader.read_exact(1) != b"\n":
                raise SecretScanError
            rules_by_oid[oid] = _payload_rules(payload)
            del payload
        process.stdin.close()
        reader.expect_eof()
        remaining = deadline - time.monotonic()
        if remaining <= 0 or process.wait(timeout=remaining) != 0:
            raise SecretScanError
    except Exception:
        failed = True
    if failed:
        _stop_process_quietly(process)
    _close_quietly(process.stdin)
    _close_quietly(process.stdout)
    if failed:
        raise SecretScanError
    return rules_by_oid


def _history_blob_paths(
    root: Path,
    *,
    budget: _ReleaseBudget,
) -> dict[bytes, set[bytes]]:
    shallow = _run_git(root, ["rev-parse", "--is-shallow-repository"])
    if shallow != b"false\n":
        raise SecretScanError
    head = _validated_oid(_run_git(root, ["rev-parse", "--verify", "HEAD^{commit}"]))
    raw_commits = _run_git(root, ["rev-list", "HEAD"])
    if not raw_commits.endswith(b"\n"):
        raise SecretScanError
    commits = raw_commits.splitlines()
    if not commits:
        raise SecretScanError
    for commit in commits:
        _validated_oid(commit, expected_length=len(head))
    if commits[0] != head:
        raise SecretScanError
    diff_output = _run_git(
        root,
        [
            "diff-tree",
            "--stdin",
            "--root",
            "-m",
            "-r",
            "--raw",
            "-z",
            "--no-abbrev",
            "--no-renames",
            "--no-commit-id",
            "--no-ext-diff",
        ],
        input_bytes=b"\n".join(commits) + b"\n",
    )
    return _parse_diff_tree_output(diff_output, oid_length=len(head), budget=budget)


def scan_git_history(
    root: Path,
    *,
    budget: _ReleaseBudget | None = None,
) -> list[Finding]:
    """Scan every blob and raw path reachable through ``HEAD`` ancestry."""

    if budget is None:
        budget = _ReleaseBudget()
    root = root.resolve()
    blob_paths = _history_blob_paths(root, budget=budget)
    rules_by_oid = _read_git_blob_rules(root, sorted(blob_paths), budget=budget)
    if rules_by_oid.keys() != blob_paths.keys():
        raise SecretScanError
    findings: set[Finding] = set()
    for oid, paths in blob_paths.items():
        rules = rules_by_oid[oid]
        for path in paths:
            if _is_env_path(path):
                findings.add(Finding(path, "tracked-env-file"))
            for rule in rules:
                findings.add(Finding(path, rule))
    return sorted(findings, key=lambda finding: (finding.path, finding.rule))


def _git_candidate_paths(root: Path, *, budget: _ReleaseBudget) -> list[Path]:
    output = _run_git(
        root,
        [
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
    )
    if output and not output.endswith(b"\0"):
        raise SecretScanError
    candidates: list[Path] = []
    for entry in output.split(b"\0"):
        if not entry:
            continue
        budget.count_path()
        candidates.append(root / os.fsdecode(entry))
    return candidates


def _directory_files(
    root: Path,
    *,
    budget: _ReleaseBudget,
    ignored_directories: set[str],
) -> list[Path]:
    files: list[Path] = []
    directories = [root]
    failed = False
    try:
        while directories:
            directory = directories.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    budget.count_path()
                    path = Path(entry.path)
                    if entry.is_symlink():
                        files.append(path)
                    elif entry.is_dir(follow_symlinks=False):
                        if entry.name not in ignored_directories:
                            directories.append(path)
                    else:
                        files.append(path)
    except OSError:
        failed = True
    if failed:
        raise SecretScanError
    return files


def _files_under(
    paths: Iterable[Path],
    *,
    budget: _ReleaseBudget,
    ignored_directories: set[str] | None = None,
) -> list[Path]:
    files: list[Path] = []
    ignored = ignored_directories or set()
    for path in paths:
        budget.count_path()
        if path.is_file() or path.is_symlink():
            files.append(path)
        elif path.is_dir():
            files.extend(_directory_files(path, budget=budget, ignored_directories=ignored))
    return files


def _local_env_files(root: Path, *, budget: _ReleaseBudget) -> list[Path]:
    ignored_roots = {".git", ".venv", "node_modules"}
    return [
        path
        for path in _files_under(
            [root],
            budget=budget,
            ignored_directories=ignored_roots,
        )
        if path.name.startswith(".env")
    ]


def _copy_candidate_file(
    source: Path,
    target: Path,
    *,
    candidate_bytes: int,
    budget: _ReleaseBudget,
) -> int:
    copied_bytes = 0
    failed = False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb", buffering=0) as reader, target.open("xb", buffering=0) as writer:
            while True:
                remaining_total = budget.remaining_bytes()
                allowed = min(
                    _MAX_RELEASE_FILE_BYTES - copied_bytes,
                    _MAX_CANDIDATE_TREE_BYTES - candidate_bytes - copied_bytes,
                    remaining_total,
                )
                if allowed < 0:
                    raise SecretScanError
                read_size = min(_READ_CHUNK_BYTES, allowed + 1)
                payload = reader.read(read_size)
                if not payload:
                    break
                if len(payload) > remaining_total:
                    raise SecretScanError
                budget.consume_bytes(len(payload))
                if len(payload) > allowed:
                    raise SecretScanError
                offset = 0
                while offset < len(payload):
                    written = writer.write(payload[offset:])
                    if written is None or written <= 0:
                        raise SecretScanError
                    offset += written
                copied_bytes += len(payload)
    except (OSError, SecretScanError):
        failed = True
    if failed:
        raise SecretScanError
    return copied_bytes


def _materialize_candidate_tree(
    source_paths: Sequence[Path],
    *,
    root: Path,
    destination: Path,
    budget: _ReleaseBudget,
) -> list[Path]:
    if _MAX_RELEASE_FILE_BYTES < 0 or _MAX_CANDIDATE_TREE_BYTES < 0:
        raise SecretScanError
    copied: list[Path] = []
    candidate_bytes = 0
    for source in source_paths:
        budget.count_path()
        if not source.is_file() or source.is_symlink():
            continue
        relative = Path(os.fsdecode(_relative_bytes(source, root)))
        if relative.is_absolute() or ".." in relative.parts:
            raise SecretScanError
        target = destination / relative
        candidate_bytes += _copy_candidate_file(
            source,
            target,
            candidate_bytes=candidate_bytes,
            budget=budget,
        )
        copied.append(target)
    return copied


def release_findings(root: Path) -> list[Finding]:
    """Scan current release material plus every Git blob reachable from ``HEAD``."""

    root = root.resolve()
    budget = _ReleaseBudget()
    candidates = _git_candidate_paths(root, budget=budget)
    explicit_outputs = _files_under(
        (
            root / "web" / "dist",
            root / "docs" / "assets" / "final",
            root / "output" / "playwright",
            root / "logs",
        ),
        budget=budget,
    )
    direct = list(
        dict.fromkeys([*candidates, *_local_env_files(root, budget=budget), *explicit_outputs])
    )
    findings = scan_paths(direct, root=root, budget=budget)
    with TemporaryDirectory(prefix="backchannel-release-candidate-") as temporary:
        candidate_root = Path(temporary) / "candidate"
        copied = _materialize_candidate_tree(
            candidates,
            root=root,
            destination=candidate_root,
            budget=budget,
        )
        candidate_findings = scan_paths(copied, root=candidate_root, budget=budget)
    return sorted(
        set(findings).union(
            candidate_findings,
            scan_git_history(root, budget=budget),
        ),
        key=lambda finding: (finding.path, finding.rule),
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    arguments = parser.parse_args(argv)
    failed = False
    findings: list[Finding] = []
    try:
        findings = release_findings(arguments.root)
    except Exception:
        failed = True
    if failed:
        print(SCAN_ERROR_MESSAGE)
        raise SystemExit(2)
    if findings:
        print(f"Secret scan failed with {len(findings)} finding(s).")
        print(format_findings(findings))
        raise SystemExit(1)
    print(
        "Secret scan passed: configured rules found no matches in current release inputs or "
        "HEAD-ancestry file blobs."
    )


if __name__ == "__main__":
    main()
