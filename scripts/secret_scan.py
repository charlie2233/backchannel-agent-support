"""Scan release surfaces for credential material without printing matched data."""

from __future__ import annotations

import argparse
import io
import os
import re
import selectors
import stat
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Final, NoReturn

MEBIBYTE: Final = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ScanLimits:
    max_paths: int = 20_000
    max_path_bytes: int = 4_096
    max_file_bytes: int = 8 * MEBIBYTE
    max_aggregate_bytes: int = 128 * MEBIBYTE
    max_archive_bytes: int = 128 * MEBIBYTE
    max_archive_members: int = 20_000
    max_objects: int = 20_000
    max_plumbing_bytes: int = 64 * MEBIBYTE
    max_findings: int = 100
    command_timeout_seconds: int = 30
    overall_timeout_seconds: float = 120


DEFAULT_LIMITS: Final = ScanLimits()


@dataclass(frozen=True, order=True, slots=True)
class Finding:
    surface: str
    path: str
    rule: str

    def diagnostic(self) -> str:
        return f"{self.surface}:{self.path}:{self.rule}"


class ScanAborted(RuntimeError):
    """A fail-closed operational result with a redacted diagnostic."""

    def __init__(self, finding: Finding) -> None:
        self.finding = finding
        super().__init__(finding.diagnostic())


_OPENAI_TOKEN = re.compile(
    rb"(?<![A-Za-z0-9_])sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}"
)
_GITHUB_TOKEN = re.compile(
    rb"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})"
)
_AWS_ACCESS_KEY = re.compile(rb"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])")
_PRIVATE_KEY = re.compile(
    rb"-----BEGIN[ ]+(?:RSA[ ]+|EC[ ]+|DSA[ ]+|OPENSSH[ ]+|PGP[ ]+)?"
    rb"(?:ENCRYPTED[ ]+)?PRIVATE[ ]+KEY(?:[ ]+BLOCK)?-----"
)
_AUTHORIZATION_VALUE = re.compile(
    rb"(?im)(?<![A-Za-z0-9_])authorization[\"']?[ \t]*[:=][ \t]*"
    rb"(?P<value>[^\r\n,;}]+)"
)
_SENSITIVE_ASSIGNMENT = re.compile(
    rb"(?im)^[ \t]*[\"']?(?:OPENAI_API_KEY|GITHUB_TOKEN|GH_TOKEN|AWS_ACCESS_KEY_ID|"
    rb"AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN|BACKCHANNEL_IDENTITY_HMAC_SECRET|"
    rb"DATABASE_URL|DB_PASSWORD|CLIENT_SECRET|ACCESS_TOKEN|API_KEY|SECRET_KEY|"
    rb"PRIVATE_KEY|PASSWORD)[\"']?[ \t]*[:=][ \t]*"
    rb"(?P<value>[^\r\n#]*)"
)
_STRICT_BEARER = re.compile(
    rb"(?i)(?<![A-Za-z0-9_])authorization[\"']?[ \t]*[:=][ \t]*[\"']?bearer\b"
)
_SERIALIZED_STATE_MARKER = re.compile(
    rb"(?i)(?<![A-Za-z0-9_])(?:state_json|serialized_state|pending_state_json|"
    rb"model_metadata_json|run_state)(?![A-Za-z0-9_])"
)
_PROOF_MARKER = re.compile(
    rb"(?i)(?<![A-Za-z0-9_])(?:proof_payload|proof_json|internal_proof|consumer_proof|"
    rb"provider_proof)(?![A-Za-z0-9_])"
)
_SECRET_MARKER = re.compile(
    rb"(?i)(?<![A-Za-z0-9_])(?:OPENAI_API_KEY|BACKCHANNEL_IDENTITY_HMAC_SECRET|"
    rb"identity_hmac_secret|AWS_SECRET_ACCESS_KEY|GITHUB_TOKEN|client_secret|access_token|"
    rb"api_key|private_key)(?![A-Za-z0-9_])"
)
_OBJECT_ID = re.compile(rb"[0-9a-f]{40}(?:[0-9a-f]{24})?")

_PUBLIC_DIRECTORIES: Final = (
    "web/dist",
    "docs/assets/final",
    "playwright-report",
    "test-results",
    "blob-report",
    "logs",
)
_WALK_PRUNE: Final = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
}


def _diagnostic_locator(locator: str) -> str:
    return "".join(
        character
        if character.isprintable() and not 0xD800 <= ord(character) <= 0xDFFF
        else f"\\x{ord(character):02x}"
        for character in locator
    )


def _placeholder(raw_value: bytes) -> bool:
    value = raw_value.strip(b" \t\"',;}")
    lowered = value.lower()
    for scheme in (b"bearer", b"basic"):
        if lowered == scheme:
            return True
        prefix = scheme + b" "
        if lowered.startswith(prefix):
            lowered = lowered[len(prefix) :].strip(b" \t\"',;}")
            break
    if not lowered:
        return True
    if lowered in {
        b"redacted",
        b"placeholder",
        b"example",
        b"test",
        b"test-token",
        b"canary",
        b"dummy",
        b"fake",
        b"none",
        b"null",
        b"changeme",
        b"backchannel-local-identity-secret-v0.3-only",
    }:
        return True
    if lowered.startswith((b"${", b"$", b"<")):
        return True
    if lowered.startswith((b"test-", b"example-", b"placeholder-", b"dummy-", b"fake-")):
        return True
    return lowered.endswith(b"-canary")


def _content_rules(content: bytes, *, strict: bool) -> set[str]:
    rules: set[str] = set()
    for match in _OPENAI_TOKEN.finditer(content):
        if not _placeholder(match.group()[3:]):
            rules.add("openai_token")
            break
    if _GITHUB_TOKEN.search(content):
        rules.add("github_token")
    if _AWS_ACCESS_KEY.search(content):
        rules.add("aws_access_key")
    if _PRIVATE_KEY.search(content):
        rules.add("private_key")
    for match in _AUTHORIZATION_VALUE.finditer(content):
        if not _placeholder(match.group("value")):
            rules.add("authorization_value")
            break
    for match in _SENSITIVE_ASSIGNMENT.finditer(content):
        if not _placeholder(match.group("value")):
            rules.add("sensitive_assignment")
            break
    if strict:
        if _STRICT_BEARER.search(content):
            rules.add("authorization_bearer")
        if _SERIALIZED_STATE_MARKER.search(content):
            rules.add("serialized_state_marker")
        if _PROOF_MARKER.search(content):
            rules.add("proof_marker")
        if _SECRET_MARKER.search(content):
            rules.add("secret_marker")
    return rules


class _Scanner:
    def __init__(
        self, repository: Path, limits: ScanLimits, *, started_at: float | None = None
    ) -> None:
        self.repository = repository
        self.limits = limits
        self.findings: list[Finding] = []
        self._finding_keys: set[Finding] = set()
        self._path_count = 0
        self._charged_paths: set[str] = set()
        self._aggregate_bytes = 0
        self._history_tree_entry_count = 0
        self._public_seen: set[str] = set()
        self._current_seen: set[str] = set()
        scan_started_at = time.monotonic() if started_at is None else started_at
        self._scan_deadline = scan_started_at + max(
            0, self.limits.overall_timeout_seconds
        )

    def check_deadline(self, surface: str, path: str = ".") -> None:
        if time.monotonic() >= self._scan_deadline:
            self.abort(surface, path, "scan_timeout")

    def abort(self, surface: str, path: str, rule: str) -> NoReturn:
        raise ScanAborted(Finding(surface, _diagnostic_locator(path), rule))

    def add(self, surface: str, path: str, rule: str) -> None:
        self.check_deadline(surface, path)
        finding = Finding(surface, _diagnostic_locator(path), rule)
        if finding in self._finding_keys:
            return
        if len(self.findings) >= self.limits.max_findings:
            self.abort("scan", ".", "finding_limit")
        self._finding_keys.add(finding)
        self.findings.append(finding)

    def charge_path(self, surface: str, path: str) -> None:
        self.check_deadline(surface, path)
        if path in self._charged_paths:
            return
        self._charged_paths.add(path)
        self._path_count += 1
        if self._path_count > self.limits.max_paths:
            self.abort(surface, path, "path_count_limit")

    def reserve_bytes(self, surface: str, path: str, size: int) -> None:
        self.check_deadline(surface, path)
        if size < 0 or size > self.limits.max_file_bytes:
            self.abort(surface, path, "file_size_limit")
        if self._aggregate_bytes + size > self.limits.max_aggregate_bytes:
            self.abort(surface, path, "aggregate_size_limit")
        self._aggregate_bytes += size

    def normalize_path(self, raw_path: bytes, surface: str) -> str:
        self.check_deadline(surface)
        if len(raw_path) > self.limits.max_path_bytes:
            self.abort(surface, ".", "path_size_limit")
        try:
            decoded = raw_path.decode("utf-8")
        except UnicodeDecodeError:
            decoded = os.fsdecode(raw_path)
        path = PurePosixPath(decoded)
        if (
            path.is_absolute()
            or not path.parts
            or any(part in ("", ".", "..") for part in path.parts)
        ):
            self.abort(surface, ".", "unsafe_path")
        return path.as_posix()

    def check_env_path(self, surface: str, path: str) -> None:
        if path == ".env.example":
            return
        if any(part.startswith(".env") for part in PurePosixPath(path).parts):
            self.add(surface, path, "disallowed_env_file")

    def git(
        self,
        arguments: list[str],
        *,
        surface: str,
        max_output: int,
        input_file: IO[bytes] | None = None,
        delimiter: bytes | None = None,
        max_item_bytes: int | None = None,
        max_items: int | None = None,
        item_limit_rule: str = "git_output_limit",
    ) -> bytes:
        self.check_deadline(surface)
        if delimiter is not None and len(delimiter) != 1:
            self.abort(surface, ".", "git_framing")
        environment = os.environ.copy()
        environment.update({"GIT_NO_REPLACE_OBJECTS": "1", "LC_ALL": "C"})
        if input_file is not None:
            input_file.seek(0)
        try:
            process = subprocess.Popen(
                ["git", *arguments],
                cwd=self.repository,
                env=environment,
                stdin=input_file if input_file is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError as error:
            raise ScanAborted(Finding(surface, ".", "git_command_failed")) from error
        if process.stdout is None:
            process.kill()
            self.abort(surface, ".", "git_command_failed")

        command_deadline = time.monotonic() + self.limits.command_timeout_seconds
        deadline = min(command_deadline, self._scan_deadline)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        chunks: list[bytes] = []
        total = 0
        pending_item = 0
        item_count = 0
        delimiter_byte = delimiter[0] if delimiter is not None else None
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    if time.monotonic() >= self._scan_deadline:
                        self.abort(surface, ".", "scan_timeout")
                    self.abort(surface, ".", "git_timeout")
                chunk = os.read(process.stdout.fileno(), 64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_output:
                    self.abort(surface, ".", "git_output_limit")
                if delimiter is not None and max_item_bytes is not None:
                    for byte in chunk:
                        if byte == delimiter_byte:
                            pending_item = 0
                            item_count += 1
                            if max_items is not None and item_count > max_items:
                                self.abort(surface, ".", item_limit_rule)
                        else:
                            pending_item += 1
                            if pending_item > max_item_bytes:
                                self.abort(surface, ".", "path_size_limit")
                chunks.append(chunk)
            remaining = max(0.001, deadline - time.monotonic())
            try:
                return_code = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                if time.monotonic() >= self._scan_deadline:
                    self.abort(surface, ".", "scan_timeout")
                self.abort(surface, ".", "git_timeout")
            self.check_deadline(surface)
            data = b"".join(chunks)
            if return_code != 0:
                self.abort(surface, ".", "git_command_failed")
            if delimiter is not None and data and not data.endswith(delimiter):
                self.abort(surface, ".", "git_framing")
            return data
        finally:
            selector.close()
            process.stdout.close()
            if process.poll() is None:
                process.kill()
                process.wait()

    def scan_bytes(self, surface: str, path: str, content: bytes, *, strict: bool) -> None:
        self.check_deadline(surface, path)
        for rule in _content_rules(content, strict=strict):
            self.add(surface, path, rule)
        self.check_deadline(surface, path)

    def read_worktree_file(self, surface: str, path: str) -> bytes | None:
        pure_path = PurePosixPath(path)
        candidate = self.repository
        final_stat: os.stat_result | None = None
        for index, part in enumerate(pure_path.parts):
            self.check_deadline(surface, path)
            candidate = candidate / part
            try:
                item_stat = candidate.lstat()
            except OSError:
                self.abort(surface, path, "unreadable_path")
            is_final = index == len(pure_path.parts) - 1
            if stat.S_ISLNK(item_stat.st_mode):
                self.add(surface, path, "unsafe_file_type")
                return None
            if not is_final and not stat.S_ISDIR(item_stat.st_mode):
                self.add(surface, path, "unsafe_file_type")
                return None
            if is_final:
                final_stat = item_stat
        if final_stat is None or not stat.S_ISREG(final_stat.st_mode):
            self.add(surface, path, "unsafe_file_type")
            return None
        self.reserve_bytes(surface, path, final_stat.st_size)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            descriptor = os.open(candidate, flags)
        except OSError:
            self.add(surface, path, "unsafe_file_type")
            return None
        try:
            opened_stat = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened_stat.st_mode)
                or opened_stat.st_dev != final_stat.st_dev
                or opened_stat.st_ino != final_stat.st_ino
            ):
                self.add(surface, path, "unsafe_file_type")
                return None
            content = bytearray()
            reserved_size = final_stat.st_size
            while True:
                self.check_deadline(surface, path)
                chunk = os.read(descriptor, 64 * 1024)
                if not chunk:
                    break
                content.extend(chunk)
                if len(content) > self.limits.max_file_bytes:
                    self.abort(surface, path, "file_size_limit")
                if len(content) > reserved_size:
                    growth = len(content) - reserved_size
                    if self._aggregate_bytes + growth > self.limits.max_aggregate_bytes:
                        self.abort(surface, path, "aggregate_size_limit")
                    self._aggregate_bytes += growth
                    reserved_size = len(content)
            return bytes(content)
        finally:
            os.close(descriptor)

    def scan_worktree_path(self, surface: str, path: str, *, strict: bool) -> None:
        self.charge_path(surface, path)
        self.check_env_path(surface, path)
        content = self.read_worktree_file(surface, path)
        if content is not None:
            self.scan_bytes(surface, path, content, strict=strict)

    def scan_index(self) -> None:
        index_data = self.git(
            ["ls-files", "--stage", "-z"],
            surface="index",
            max_output=self.limits.max_plumbing_bytes,
            delimiter=b"\0",
            max_item_bytes=self.limits.max_path_bytes + 160,
            max_items=self.limits.max_paths,
            item_limit_rule="path_count_limit",
        )
        paths_by_oid: dict[bytes, set[str]] = {}
        object_ids: list[bytes] = []
        seen_oids: set[bytes] = set()
        regular_oids: set[bytes] = set()
        for record in (item for item in index_data.split(b"\0") if item):
            self.check_deadline("index")
            if b"\t" not in record:
                self.abort("index", ".", "git_framing")
            metadata, raw_path = record.split(b"\t", 1)
            fields = metadata.split(b" ")
            if len(fields) != 3 or _OBJECT_ID.fullmatch(fields[1]) is None:
                self.abort("index", ".", "git_framing")
            mode, oid, stage = fields
            path = self.normalize_path(raw_path, "index")
            self.charge_path("index", path)
            self.check_env_path("index", path)
            if stage != b"0":
                self.add("index", path, "unsafe_git_stage")
            if mode not in {b"100644", b"100755"}:
                self.add("index", path, "unsafe_git_mode")
            else:
                regular_oids.add(oid)
            paths_by_oid.setdefault(oid, set()).add(path)
            if oid not in seen_oids:
                seen_oids.add(oid)
                object_ids.append(oid)
        information = self._object_info(object_ids, surface="index")
        blobs: list[tuple[bytes, str, int]] = []
        for info_record in information:
            oid, object_type, _size = info_record
            if oid in regular_oids and object_type != "blob":
                self.abort("index", oid.decode("ascii"), "git_framing")
            if object_type == "blob":
                blobs.append(info_record)
        for oid, content in self._batch_payloads(blobs, surface="index"):
            for path in sorted(paths_by_oid[oid]):
                self.scan_bytes("index", path, content, strict=False)

    def scan_current(self) -> None:
        deleted_data = self.git(
            ["ls-files", "-z", "--deleted"],
            surface="current",
            max_output=self.limits.max_plumbing_bytes,
            delimiter=b"\0",
            max_item_bytes=self.limits.max_path_bytes,
            max_items=self.limits.max_paths,
            item_limit_rule="path_count_limit",
        )
        deleted = {item for item in deleted_data.split(b"\0") if item}
        candidate_data = self.git(
            ["ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            surface="current",
            max_output=self.limits.max_plumbing_bytes,
            delimiter=b"\0",
            max_item_bytes=self.limits.max_path_bytes,
            max_items=self.limits.max_paths,
            item_limit_rule="path_count_limit",
        )
        for raw_path in candidate_data.split(b"\0"):
            if not raw_path:
                continue
            path = self.normalize_path(raw_path, "current")
            self._current_seen.add(path)
            self.charge_path("current", path)
            self.check_env_path("current", path)
            if raw_path in deleted:
                continue
            self.scan_worktree_path("current", path, strict=False)
        self._scan_filesystem_entries()

    def _filesystem_entries(self) -> Iterator[tuple[str, os.DirEntry[str]]]:
        pending = [self.repository]
        while pending:
            self.check_deadline("current")
            directory = pending.pop()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        self.check_deadline("current")
                        raw_relative = os.fsencode(
                            os.path.relpath(entry.path, self.repository)
                        ).replace(os.fsencode(os.sep), b"/")
                        path = self.normalize_path(raw_relative, "current")
                        self.charge_path("current", path)
                        yield path, entry
                        try:
                            if (
                                entry.name not in _WALK_PRUNE
                                and not entry.is_symlink()
                                and entry.is_dir(follow_symlinks=False)
                            ):
                                pending.append(Path(entry.path))
                        except OSError:
                            self.abort("current", path, "unreadable_path")
            except OSError:
                self.abort("current", ".", "unreadable_path")

    def _scan_filesystem_entries(self) -> None:
        for path, entry in self._filesystem_entries():
            try:
                is_symlink = entry.is_symlink()
                is_directory = entry.is_dir(follow_symlinks=False)
                is_regular = entry.is_file(follow_symlinks=False)
            except OSError:
                self.abort("current", path, "unreadable_path")
            if is_symlink or (not is_directory and not is_regular):
                if path not in self._current_seen:
                    self._current_seen.add(path)
                    self.scan_worktree_path("current", path, strict=False)
            if is_regular and path.endswith(".log") and path not in self._public_seen:
                self._public_seen.add(path)
                self.scan_worktree_path("public", path, strict=True)

    def _walk_public_directory(self, relative: str) -> None:
        root = self.repository / relative
        try:
            root_stat = root.lstat()
        except FileNotFoundError:
            return
        except OSError:
            self.abort("public", relative, "unreadable_path")
        self.charge_path("public", relative)
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            self.scan_worktree_path("public", relative, strict=True)
            return
        pending = [root]
        while pending:
            self.check_deadline("public", relative)
            directory = pending.pop()
            try:
                entries = os.scandir(directory)
            except OSError:
                self.abort("public", relative, "unreadable_path")
            with entries:
                for entry in entries:
                    self.check_deadline("public", relative)
                    raw_relative = os.fsencode(
                        os.path.relpath(entry.path, self.repository)
                    ).replace(os.fsencode(os.sep), b"/")
                    path = self.normalize_path(raw_relative, "public")
                    self.charge_path("public", path)
                    try:
                        if entry.is_symlink():
                            if path not in self._public_seen:
                                self._public_seen.add(path)
                                self.scan_worktree_path("public", path, strict=True)
                        elif entry.is_dir(follow_symlinks=False):
                            pending.append(Path(entry.path))
                        elif path not in self._public_seen:
                            self._public_seen.add(path)
                            self.scan_worktree_path("public", path, strict=True)
                    except OSError:
                        self.abort("public", path, "unreadable_path")

    def scan_public(self) -> None:
        for relative in _PUBLIC_DIRECTORIES:
            self.check_deadline("public", relative)
            self._walk_public_directory(relative)

    def scan_archive(self) -> None:
        archive_data = self.git(
            ["archive", "--format=tar", "HEAD"],
            surface="archive",
            max_output=self.limits.max_archive_bytes,
        )
        try:
            archive = tarfile.open(fileobj=io.BytesIO(archive_data), mode="r:")
        except tarfile.TarError as error:
            raise ScanAborted(Finding("archive", ".", "archive_format")) from error
        with archive:
            for index, member in enumerate(archive, start=1):
                self.check_deadline("archive")
                if index > self.limits.max_archive_members:
                    self.abort("archive", ".", "archive_member_limit")
                path = self.normalize_path(
                    member.name.encode("utf-8", errors="surrogateescape"), "archive"
                )
                if member.isdir():
                    continue
                if not member.isreg():
                    self.add("archive", path, "unsafe_file_type")
                    continue
                self.check_env_path("archive", path)
                self.reserve_bytes("archive", path, member.size)
                extracted = archive.extractfile(member)
                if extracted is None:
                    self.abort("archive", path, "archive_format")
                content = extracted.read(self.limits.max_file_bytes + 1)
                self.check_deadline("archive", path)
                if len(content) != member.size:
                    self.abort("archive", path, "archive_format")
                self.scan_bytes("archive", path, content, strict=False)

    def _reachable_objects(self) -> tuple[list[bytes], dict[bytes, str]]:
        data = self.git(
            ["rev-list", "--objects", "-z", "HEAD", "--tags"],
            surface="history",
            max_output=self.limits.max_plumbing_bytes,
            delimiter=b"\0",
            max_item_bytes=self.limits.max_path_bytes + 80,
            max_items=self.limits.max_objects * 2,
            item_limit_rule="object_count_limit",
        )
        object_ids: list[bytes] = []
        seen: set[bytes] = set()
        paths: dict[bytes, str] = {}
        pending: bytes | None = None
        for token in (item for item in data.split(b"\0") if item):
            self.check_deadline("history")
            if token.startswith(b"path="):
                if pending is None:
                    self.abort("history", ".", "git_framing")
                paths[pending] = self.normalize_path(token[5:], "history")
                continue
            oid = token
            raw_path: bytes | None = None
            if b" " in token:
                oid, raw_path = token.split(b" ", 1)
            if _OBJECT_ID.fullmatch(oid) is None:
                self.abort("history", ".", "git_framing")
            pending = oid
            if oid not in seen:
                seen.add(oid)
                object_ids.append(oid)
                if len(object_ids) > self.limits.max_objects:
                    self.abort("history", oid.decode("ascii"), "object_count_limit")
            if raw_path is not None:
                paths[oid] = self.normalize_path(raw_path, "history")
        for oid, path in paths.items():
            self.check_env_path("history", path)
        return object_ids, paths

    def _object_info(
        self, object_ids: list[bytes], *, surface: str
    ) -> list[tuple[bytes, str, int]]:
        with tempfile.TemporaryFile(mode="w+b") as object_input:
            for oid in object_ids:
                self.check_deadline(surface)
                object_input.write(oid + b"\0")
            data = self.git(
                [
                    "cat-file",
                    "--batch-check=%(objectname) %(objecttype) %(objectsize)",
                    "-Z",
                ],
                surface=surface,
                max_output=min(
                    self.limits.max_plumbing_bytes,
                    max(1, len(object_ids)) * 160,
                ),
                input_file=object_input,
                delimiter=b"\0",
                max_item_bytes=160,
                max_items=len(object_ids),
                item_limit_rule="git_framing",
            )
        records = [record for record in data.split(b"\0") if record]
        if len(records) != len(object_ids):
            self.abort(surface, ".", "git_framing")
        information: list[tuple[bytes, str, int]] = []
        for expected_oid, record in zip(object_ids, records, strict=True):
            self.check_deadline(surface)
            fields = record.split(b" ")
            if len(fields) != 3 or fields[0] != expected_oid:
                self.abort(surface, expected_oid.decode("ascii"), "git_framing")
            try:
                object_type = fields[1].decode("ascii")
                object_size = int(fields[2])
            except (UnicodeDecodeError, ValueError) as error:
                raise ScanAborted(
                    Finding(surface, expected_oid.decode("ascii"), "git_framing")
                ) from error
            information.append((expected_oid, object_type, object_size))
        return information

    def _batch_payloads(
        self, information: list[tuple[bytes, str, int]], *, surface: str
    ) -> list[tuple[bytes, bytes]]:
        total_size = 0
        for oid, _object_type, size in information:
            self.check_deadline(surface)
            if size > self.limits.max_file_bytes:
                self.abort(surface, oid.decode("ascii"), "file_size_limit")
            total_size += size
        if self._aggregate_bytes + total_size > self.limits.max_aggregate_bytes:
            self.abort(surface, ".", "aggregate_size_limit")
        self._aggregate_bytes += total_size
        with tempfile.TemporaryFile(mode="w+b") as object_input:
            for oid, _object_type, _size in information:
                self.check_deadline(surface)
                object_input.write(oid + b"\0")
            data = self.git(
                ["cat-file", "--batch", "-Z"],
                surface=surface,
                max_output=total_size + max(1, len(information)) * 160,
                input_file=object_input,
            )
        payloads: list[tuple[bytes, bytes]] = []
        cursor = 0
        for expected_oid, expected_type, expected_size in information:
            self.check_deadline(surface)
            header_end = data.find(b"\0", cursor)
            if header_end < 0:
                self.abort(surface, expected_oid.decode("ascii"), "git_framing")
            header = data[cursor:header_end].split(b" ")
            if (
                len(header) != 3
                or header[0] != expected_oid
                or header[1] != expected_type.encode("ascii")
                or header[2] != str(expected_size).encode("ascii")
            ):
                self.abort(surface, expected_oid.decode("ascii"), "git_framing")
            content_start = header_end + 1
            content_end = content_start + expected_size
            if content_end >= len(data) or data[content_end : content_end + 1] != b"\0":
                self.abort(surface, expected_oid.decode("ascii"), "git_framing")
            payloads.append((expected_oid, data[content_start:content_end]))
            cursor = content_end + 1
        if cursor != len(data):
            self.abort(surface, ".", "git_framing")
        return payloads

    def _check_tree_modes(self, oid: bytes, content: bytes) -> None:
        raw_oid_bytes = len(oid) // 2
        diagnostic_oid = oid.decode("ascii")
        cursor = 0
        while cursor < len(content):
            self.check_deadline("history", diagnostic_oid)
            mode_end = content.find(b" ", cursor)
            if mode_end < 0:
                self.abort("history", diagnostic_oid, "git_framing")
            name_start = mode_end + 1
            name_end = content.find(b"\0", name_start)
            if name_end < 0:
                self.abort("history", diagnostic_oid, "git_framing")
            mode = content[cursor:mode_end]
            self._history_tree_entry_count += 1
            if self._history_tree_entry_count > self.limits.max_paths:
                self.abort("history", diagnostic_oid, "path_count_limit")
            if name_end - name_start > self.limits.max_path_bytes:
                self.abort("history", diagnostic_oid, "path_size_limit")
            name = content[name_start:name_end]
            cursor = name_end + 1 + raw_oid_bytes
            if cursor > len(content):
                self.abort("history", diagnostic_oid, "git_framing")
            if mode not in {b"40000", b"040000", b"100644", b"100755"}:
                self.add("history", diagnostic_oid, "unsafe_git_mode")
            if name.startswith(b".env") and name != b".env.example":
                self.add("history", diagnostic_oid, "disallowed_env_file")

    def scan_history(self) -> None:
        object_ids, _paths = self._reachable_objects()
        information = self._object_info(object_ids, surface="history")
        expected_types = {"blob", "commit", "tag", "tree"}
        for oid, object_type, _size in information:
            if object_type not in expected_types:
                self.abort(
                    "history", oid.decode("ascii"), "unexpected_object_type"
                )
        blobs = [record for record in information if record[1] == "blob"]
        metadata = [
            record for record in information if record[1] in {"commit", "tag"}
        ]
        trees = [record for record in information if record[1] == "tree"]
        for oid, content in self._batch_payloads(blobs, surface="history"):
            self.scan_bytes("history", oid.decode("ascii"), content, strict=False)
        for oid, content in self._batch_payloads(metadata, surface="history"):
            self.scan_bytes("history", oid.decode("ascii"), content, strict=False)
        for oid, content in self._batch_payloads(trees, surface="history"):
            self._check_tree_modes(oid, content)

    def run(self) -> tuple[Finding, ...]:
        self.scan_index()
        self.scan_current()
        self.scan_public()
        self.scan_archive()
        self.scan_history()
        return tuple(sorted(self.findings))


def scan_repository(
    repository: Path, *, limits: ScanLimits = DEFAULT_LIMITS
) -> tuple[Finding, ...]:
    started_at = time.monotonic()
    root = repository.resolve()
    scanner = _Scanner(root, limits, started_at=started_at)
    scanner.check_deadline("scan")
    probe = scanner.git(
        ["rev-parse", "--show-toplevel"],
        surface="scan",
        max_output=16 * 1024,
    )
    try:
        discovered = Path(probe.decode("utf-8").strip()).resolve()
    except UnicodeDecodeError as error:
        raise ScanAborted(Finding("scan", ".", "git_framing")) from error
    if discovered != root:
        scanner.abort("scan", ".", "repository_mismatch")
    scanner.check_deadline("scan")
    return scanner.run()


def _parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(description=__doc__)


def main(argv: list[str] | None = None) -> int:
    _parser().parse_args(argv)
    try:
        findings = scan_repository(Path.cwd())
    except ScanAborted as error:
        print(error.finding.diagnostic())
        return 2
    except Exception:
        print(Finding("scan", ".", "scan_failed").diagnostic())
        return 2
    for finding in findings:
        print(finding.diagnostic())
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
