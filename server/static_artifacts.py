"""Fail-closed verification for the packaged production web artifact."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterable
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

STATIC_ARTIFACT_MANIFEST = ".backchannel-static-manifest.json"

_HASH_CHUNK_BYTES = 64 * 1024
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_FILE_COUNT = 256
_MAX_DIRECTORY_COUNT = 256
_MAX_TREE_ENTRY_COUNT = 512
_MAX_PATH_BYTES = 255
_MAX_FILE_BYTES = 16 * 1024 * 1024
_MAX_TOTAL_BYTES = 32 * 1024 * 1024
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_PATH_SEGMENT_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class _DuplicateManifestKey(ValueError):
    pass


class _AssetReferenceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.asset_references: set[str] = set()
        self.invalid_asset_reference = False

    def handle_starttag(
        self,
        _tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        for name, value in attrs:
            if name not in {"href", "src"} or value is None:
                continue
            if value.startswith(("assets/", "./assets/")):
                self.invalid_asset_reference = True
            elif value.startswith("/assets/"):
                self.asset_references.add(value.removeprefix("/"))


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateManifestKey(key)
        result[key] = value
    return result


def _read_regular_file(path: Path, *, maximum_bytes: int) -> bytes:
    path_stat = path.lstat()
    if not stat.S_ISREG(path_stat.st_mode) or stat.S_ISLNK(path_stat.st_mode):
        raise ValueError("Static artifact entry is not a regular file.")
    if path_stat.st_size <= 0 or path_stat.st_size > maximum_bytes:
        raise ValueError("Static artifact entry exceeds its size boundary.")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if no_follow == 0:
        raise ValueError("Static artifact verification requires no-follow file access.")
    descriptor = os.open(path, flags | no_follow)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size != path_stat.st_size
            or (before.st_dev, before.st_ino) != (path_stat.st_dev, path_stat.st_ino)
        ):
            raise ValueError("Static artifact entry changed before verification.")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(_HASH_CHUNK_BYTES, maximum_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum_bytes:
                raise ValueError("Static artifact entry exceeds its size boundary.")
        after = os.fstat(descriptor)
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
            raise ValueError("Static artifact entry changed during verification.")
        content = b"".join(chunks)
        if len(content) != before.st_size:
            raise ValueError("Static artifact entry was not read completely.")
        return content
    finally:
        os.close(descriptor)


def _has_safe_relative_path(value: str) -> bool:
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        return False
    if not encoded or len(encoded) > _MAX_PATH_BYTES or "\\" in value:
        return False
    parts = value.split("/")
    if (
        any(
            part in {"", ".", ".."} or _PATH_SEGMENT_PATTERN.fullmatch(part) is None
            for part in parts
        )
        or value == STATIC_ARTIFACT_MANIFEST
    ):
        return False
    return True


def _is_safe_artifact_path(value: str) -> bool:
    if not _has_safe_relative_path(value):
        return False
    parts = value.split("/")
    return value == "index.html" or (parts[0] == "assets" and len(parts) >= 2)


def _is_safe_asset_directory_path(value: str) -> bool:
    return _has_safe_relative_path(value) and value.split("/")[0] == "assets"


def _iter_static_files(static_dir: Path) -> Iterable[Path]:
    root_entries: dict[str, os.DirEntry[str]] = {}
    with os.scandir(static_dir) as entries:
        for entry in entries:
            if len(root_entries) >= 3:
                raise ValueError("Static artifact root contains too many entries.")
            root_entries[entry.name] = entry
    if set(root_entries) != {"assets", "index.html", STATIC_ARTIFACT_MANIFEST}:
        raise ValueError("Static artifact root contains an unexpected entry.")
    assets_entry = root_entries["assets"]
    if assets_entry.is_symlink() or not assets_entry.is_dir(follow_symlinks=False):
        raise ValueError("Static artifact assets entry is not a real directory.")

    index_path = static_dir / "index.html"
    index_stat = index_path.lstat()
    if stat.S_ISLNK(index_stat.st_mode) or not stat.S_ISREG(index_stat.st_mode):
        raise ValueError("Static artifact index is not a real file.")
    yield index_path

    pending = [static_dir / "assets"]
    directory_count = 0
    file_count = 1
    tree_entry_count = 0
    while pending:
        directory = pending.pop()
        directory_count += 1
        if directory_count > _MAX_DIRECTORY_COUNT:
            raise ValueError("Static artifact contains too many directories.")
        relative_directory = directory.relative_to(static_dir).as_posix()
        if not _is_safe_asset_directory_path(relative_directory):
            raise ValueError("Static artifact contains an unsafe directory.")
        saw_entry = False
        with os.scandir(directory) as entries:
            for entry in entries:
                saw_entry = True
                tree_entry_count += 1
                if tree_entry_count > _MAX_TREE_ENTRY_COUNT:
                    raise ValueError("Static artifact tree exceeds its entry budget.")
                if entry.is_symlink():
                    raise ValueError("Static artifact contains a symbolic link.")
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                elif entry.is_file(follow_symlinks=False):
                    file_count += 1
                    if file_count > _MAX_FILE_COUNT:
                        raise ValueError("Static artifact contains too many files.")
                    yield path
                else:
                    raise ValueError("Static artifact contains a special file.")
        if not saw_entry:
            raise ValueError("Static artifact contains an empty directory.")


def _load_manifest(static_dir: Path) -> tuple[list[dict[str, Any]], bytes]:
    manifest_path = static_dir / STATIC_ARTIFACT_MANIFEST
    manifest_bytes = _read_regular_file(
        manifest_path,
        maximum_bytes=_MAX_MANIFEST_BYTES,
    )
    try:
        manifest_text = manifest_bytes.decode("utf-8")
        manifest = json.loads(
            manifest_text,
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"Invalid JSON constant: {value}")
            ),
        )
    except (UnicodeError, json.JSONDecodeError, _DuplicateManifestKey) as error:
        raise ValueError("Static artifact manifest is invalid.") from error
    if not isinstance(manifest, dict) or list(manifest) != ["version", "files"]:
        raise ValueError("Static artifact manifest shape is invalid.")
    if type(manifest["version"]) is not int or manifest["version"] != 1:
        raise ValueError("Static artifact manifest version is invalid.")
    files = manifest["files"]
    if not isinstance(files, list) or not 2 <= len(files) <= _MAX_FILE_COUNT:
        raise ValueError("Static artifact manifest file count is invalid.")
    canonical = json.dumps(manifest, indent=2, ensure_ascii=True) + "\n"
    if manifest_bytes != canonical.encode("ascii"):
        raise ValueError("Static artifact manifest is not canonical.")
    return files, manifest_bytes


def static_artifact_bundle_is_ready(static_dir: Path) -> bool:
    """Return whether a configured static root exactly matches its build manifest."""

    try:
        root_stat = static_dir.lstat()
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            return False
        files, _manifest_bytes = _load_manifest(static_dir)
        paths: list[str] = []
        declared: dict[str, tuple[int, str]] = {}
        total_size = 0
        for entry in files:
            if not isinstance(entry, dict) or list(entry) != ["path", "size", "sha256"]:
                return False
            path_value = entry["path"]
            size_value = entry["size"]
            digest_value = entry["sha256"]
            if (
                not isinstance(path_value, str)
                or not _is_safe_artifact_path(path_value)
                or type(size_value) is not int
                or not 0 < size_value <= _MAX_FILE_BYTES
                or not isinstance(digest_value, str)
                or _SHA256_PATTERN.fullmatch(digest_value) is None
            ):
                return False
            paths.append(path_value)
            if path_value in declared:
                return False
            declared[path_value] = (size_value, digest_value)
            total_size += size_value
            if total_size > _MAX_TOTAL_BYTES:
                return False
        if paths != sorted(paths) or "index.html" not in declared:
            return False

        actual_paths: dict[str, Path] = {}
        for path in _iter_static_files(static_dir):
            relative_path = path.relative_to(static_dir).as_posix()
            if not _is_safe_artifact_path(relative_path) or relative_path in actual_paths:
                return False
            actual_paths[relative_path] = path
        if set(actual_paths) != set(declared):
            return False

        index_content: bytes | None = None
        for relative_path, path in actual_paths.items():
            expected_size, expected_digest = declared[relative_path]
            content = _read_regular_file(path, maximum_bytes=_MAX_FILE_BYTES)
            if (
                len(content) != expected_size
                or hashlib.sha256(content).hexdigest() != expected_digest
            ):
                return False
            if relative_path == "index.html":
                index_content = content
        if index_content is None:
            return False

        parser = _AssetReferenceParser()
        parser.feed(index_content.decode("utf-8"))
        parser.close()
        references = parser.asset_references
        if (
            parser.invalid_asset_reference
            or not references
            or not any(reference.endswith(".js") for reference in references)
            or not references.issubset(declared)
            or any(not _is_safe_artifact_path(reference) for reference in references)
        ):
            return False
        return True
    except (OSError, RecursionError, UnicodeError, ValueError):
        return False


def read_static_index(static_dir: Path) -> bytes | None:
    """Read bounded no-follow index bytes for a previously verified static root."""

    try:
        root_stat = static_dir.lstat()
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            return None
        return _read_regular_file(
            static_dir / "index.html",
            maximum_bytes=_MAX_FILE_BYTES,
        )
    except (OSError, ValueError):
        return None
