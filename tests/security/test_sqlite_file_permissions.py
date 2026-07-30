from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest

import server.store as store_module
from server.store import SQLiteStore

pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="The deployed SQLite file-mode boundary is POSIX-only.",
)

PRIVATE_FILE_MODE = 0o600


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)


def _create_database(path: Path, *, mode: int = 0o644) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE permission_marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO permission_marker VALUES ('preserved')")
    path.chmod(mode)


def test_unsupported_filesystem_primitives_fail_before_touching_the_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "unsupported.sqlite3"
    monkeypatch.setattr(
        store_module,
        "_secure_sqlite_filesystem_primitives_available",
        lambda: False,
    )

    with pytest.raises(RuntimeError, match="requires supported POSIX primitives"):
        SQLiteStore(database_path)

    assert not database_path.exists()


def test_new_database_and_live_rollback_journal_are_owner_only(tmp_path: Path) -> None:
    database_path = tmp_path / "private.sqlite3"
    previous_umask = os.umask(0o022)

    try:
        SQLiteStore(database_path)

        assert _mode(database_path) == PRIVATE_FILE_MODE
        with sqlite3.connect(database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("CREATE TABLE rollback_permission_probe (value INTEGER)")
            journal_path = Path(f"{database_path}-journal")
            assert journal_path.is_file()
            assert _mode(journal_path) == PRIVATE_FILE_MODE
            connection.rollback()
    finally:
        os.umask(previous_umask)


def test_wal_sidecars_inherit_the_owner_only_database_mode(tmp_path: Path) -> None:
    database_path = tmp_path / "private-wal.sqlite3"
    SQLiteStore(database_path)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
        connection.execute("CREATE TABLE wal_permission_probe (value INTEGER)")
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO wal_permission_probe VALUES (1)")

        for suffix in ("-wal", "-shm"):
            sidecar_path = Path(f"{database_path}{suffix}")
            assert sidecar_path.is_file()
            assert _mode(sidecar_path) == PRIVATE_FILE_MODE
        connection.rollback()


def test_existing_loose_database_and_sidecar_are_hardened_before_sqlite_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "existing.sqlite3"
    journal_path = Path(f"{database_path}-journal")
    _create_database(database_path)
    journal_path.write_bytes(b"not a hot journal")
    journal_path.chmod(0o644)
    real_connect = sqlite3.connect
    observed_modes: list[tuple[int, int]] = []

    def checked_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        observed_modes.append((_mode(database_path), _mode(journal_path)))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(store_module.sqlite3, "connect", checked_connect)
    SQLiteStore(database_path)

    assert observed_modes
    assert observed_modes[0] == (PRIVATE_FILE_MODE, PRIVATE_FILE_MODE)
    with real_connect(database_path) as connection:
        assert connection.execute("SELECT value FROM permission_marker").fetchone() == (
            "preserved",
        )


def test_database_symlink_fails_closed_without_mutating_target_or_parent(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "storage"
    storage_path.mkdir(mode=0o750)
    target_path = tmp_path / "target.sqlite3"
    _create_database(target_path)
    database_path = storage_path / "backchannel.sqlite3"
    database_path.symlink_to(target_path)
    parent_mode = _mode(storage_path)
    target_mode = _mode(target_path)

    with pytest.raises(RuntimeError, match="owner-only regular file"):
        SQLiteStore(database_path)

    assert database_path.is_symlink()
    assert _mode(target_path) == target_mode
    assert _mode(storage_path) == parent_mode


def test_sidecar_symlink_fails_closed_without_mutating_its_target(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "backchannel.sqlite3"
    store = SQLiteStore(database_path)
    target_path = tmp_path / "unrelated.txt"
    target_path.write_text("not a journal", encoding="utf-8")
    target_path.chmod(0o644)
    Path(f"{database_path}-journal").symlink_to(target_path)

    assert store.is_ready() is False
    assert _mode(target_path) == 0o644


@pytest.mark.parametrize("suffix", ("", "-journal", "-wal", "-shm"))
def test_non_regular_database_or_sidecar_path_fails_closed(
    tmp_path: Path,
    suffix: str,
) -> None:
    database_path = tmp_path / "backchannel.sqlite3"
    if suffix:
        _create_database(database_path, mode=PRIVATE_FILE_MODE)
    non_regular_path = Path(f"{database_path}{suffix}")
    non_regular_path.mkdir()

    with pytest.raises(RuntimeError, match="owner-only regular file"):
        SQLiteStore(database_path)


def test_database_path_replacement_during_connect_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "backchannel.sqlite3"
    replacement_path = tmp_path / "replacement.sqlite3"
    _create_database(replacement_path, mode=PRIVATE_FILE_MODE)
    real_connect = sqlite3.connect

    def replacing_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        connection = real_connect(*args, **kwargs)
        os.replace(replacement_path, database_path)
        return connection

    monkeypatch.setattr(store_module.sqlite3, "connect", replacing_connect)

    with pytest.raises(RuntimeError, match="changed while SQLite opened it"):
        SQLiteStore(database_path)


def test_runtime_replacement_is_rejected_without_chmodding_the_new_file(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "backchannel.sqlite3"
    replacement_path = tmp_path / "unrelated.sqlite3"
    store = SQLiteStore(database_path)
    _create_database(replacement_path, mode=0o644)
    os.replace(replacement_path, database_path)

    assert store.is_ready() is False
    assert _mode(database_path) == 0o644


def test_readiness_does_not_repair_runtime_permission_drift(tmp_path: Path) -> None:
    database_path = tmp_path / "backchannel.sqlite3"
    store = SQLiteStore(database_path)
    database_path.chmod(0o644)

    assert store.is_ready() is False
    assert _mode(database_path) == 0o644


def test_delete_during_connect_is_not_recreated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "backchannel.sqlite3"
    store = SQLiteStore(database_path)
    real_connect = sqlite3.connect

    def deleting_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        database_path.unlink()
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(store_module.sqlite3, "connect", deleting_connect)

    assert store.is_ready() is False
    assert not database_path.exists()


def test_runtime_rejects_state_owned_by_a_different_effective_uid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "backchannel.sqlite3"
    store = SQLiteStore(database_path)
    original_mode = _mode(database_path)
    monkeypatch.setattr(store_module.os, "geteuid", lambda: os.getuid() + 1)

    assert store.is_ready() is False
    assert _mode(database_path) == original_mode


def test_parent_and_unrelated_file_permissions_are_unchanged(tmp_path: Path) -> None:
    storage_path = tmp_path / "storage"
    storage_path.mkdir(mode=0o750)
    unrelated_path = storage_path / "operator-note.txt"
    unrelated_path.write_text("not SQLite state", encoding="utf-8")
    unrelated_path.chmod(0o640)
    parent_mode = _mode(storage_path)
    unrelated_mode = _mode(unrelated_path)

    SQLiteStore(storage_path / "backchannel.sqlite3")

    assert _mode(storage_path) == parent_mode
    assert _mode(unrelated_path) == unrelated_mode


def test_group_writable_parent_is_rejected_without_changing_its_mode(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "shared-storage"
    storage_path.mkdir()
    storage_path.chmod(0o770)

    with pytest.raises(RuntimeError, match="protected directory"):
        SQLiteStore(storage_path / "backchannel.sqlite3")

    assert _mode(storage_path) == 0o770
    assert not (storage_path / "backchannel.sqlite3").exists()
