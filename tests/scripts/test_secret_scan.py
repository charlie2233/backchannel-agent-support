from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _scanner() -> ModuleType:
    path = ROOT / "scripts" / "secret_scan.py"
    assert path.is_file()
    spec = importlib.util.spec_from_file_location("release_secret_scan", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git_environment(repo: Path) -> dict[str, str]:
    return {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": str(repo),
        "LC_ALL": "C",
        "PATH": os.environ["PATH"],
    }


def _git(
    repo: Path,
    *arguments: str,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgSign=false",
            "-c",
            "user.name=Secret Scan Test",
            "-c",
            "user.email=secret-scan@example.invalid",
            *arguments,
        ],
        cwd=repo,
        input=input_bytes,
        check=True,
        capture_output=True,
        env=_git_environment(repo),
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True)
    _git(repo, "init", "--quiet", "--initial-branch=main")


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "--no-gpg-sign", "--no-verify", "-m", message)


def _commit_index(repo: Path, message: str) -> None:
    _git(repo, "commit", "--quiet", "--no-gpg-sign", "--no-verify", "-m", message)


def _history_canary() -> bytes:
    return b"sk-" + b"history-canary-" + (b"7" * 36)


def _run_cli(root: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "secret_scan.py"), "--root", str(root)],
        check=False,
        capture_output=True,
        env={"LC_ALL": "C", "PATH": os.environ["PATH"]},
    )


def test_scanner_fails_closed_without_echoing_secret_values(tmp_path: Path) -> None:
    scanner = _scanner()
    secret = "sk-" + "release-canary-" + ("7" * 36)
    bearer = "Bearer " + ("opaque" * 8)
    env_file = tmp_path / ".env.local"
    env_file.write_text(f"OPENAI_API_KEY={secret}\n", encoding="utf-8")
    log_file = tmp_path / "browser.log"
    log_file.write_text(f"Authorization: {bearer}\n", encoding="utf-8")
    state_file = tmp_path / "capture.json"
    state_file.write_text('{"state_' + 'json":{"private":"payload"}}', encoding="utf-8")

    findings = scanner.scan_paths([env_file, log_file, state_file], root=tmp_path)

    assert {finding.rule for finding in findings} >= {
        "tracked-env-file",
        "provider-key",
        "authorization-header",
        "serialized-sdk-state",
    }
    report = scanner.format_findings(findings)
    assert secret not in report
    assert bearer not in report
    assert "payload" not in report
    assert ".env.local" not in report
    assert "browser.log" not in report
    assert all(
        re.fullmatch(r"[a-z-]+: path-sha256:[0-9a-f]{64}", line) for line in report.splitlines()
    )


def test_scanner_accepts_placeholders_and_binary_png(tmp_path: Path) -> None:
    scanner = _scanner()
    example = tmp_path / ".env.example"
    example.write_text("OPENAI_API_KEY=\n", encoding="utf-8")
    capture = tmp_path / "pending.png"
    capture.write_bytes(b"\x89PNG\r\n\x1a\n" + (b"\x00" * 64))

    assert scanner.scan_paths([example, capture], root=tmp_path) == []


def test_current_file_size_ceiling_fails_safely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanner = _scanner()
    payload = tmp_path / "oversize.txt"
    payload.write_bytes(b"nine-byte")
    monkeypatch.setattr(scanner, "_MAX_RELEASE_FILE_BYTES", 8)

    findings = scanner.scan_paths([payload], root=tmp_path)

    assert [(finding.path, finding.rule) for finding in findings] == [
        (b"oversize.txt", "oversize-file")
    ]
    assert "oversize.txt" not in scanner.format_findings(findings)


def test_release_path_and_total_byte_budgets_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanner = _scanner()
    directory = tmp_path / "inputs"
    directory.mkdir()
    paths = []
    for index in range(3):
        path = directory / f"input-{index}.txt"
        path.write_bytes(b"four")
        paths.append(path)

    monkeypatch.setattr(scanner, "_MAX_RELEASE_PATHS", 2)
    with pytest.raises(scanner.SecretScanError) as path_error:
        scanner._files_under([directory], budget=scanner._ReleaseBudget())
    assert path_error.value.__cause__ is None
    assert path_error.value.__context__ is None

    monkeypatch.setattr(scanner, "_MAX_RELEASE_PATHS", 100)
    monkeypatch.setattr(scanner, "_MAX_RELEASE_TOTAL_BYTES", 7)
    with pytest.raises(scanner.SecretScanError) as byte_error:
        scanner.scan_paths(paths, root=tmp_path)
    assert byte_error.value.__cause__ is None
    assert byte_error.value.__context__ is None


def test_candidate_copy_never_writes_beyond_per_file_ceiling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanner = _scanner()
    source_root = tmp_path / "source"
    destination = tmp_path / "candidate"
    source_root.mkdir()
    source = source_root / "oversize.bin"
    source.write_bytes(b"nine-byte")
    monkeypatch.setattr(scanner, "_MAX_RELEASE_FILE_BYTES", 8)
    monkeypatch.setattr(scanner, "_MAX_CANDIDATE_TREE_BYTES", 64)

    with pytest.raises(scanner.SecretScanError) as exit_info:
        scanner._materialize_candidate_tree(
            [source],
            root=source_root,
            destination=destination,
            budget=scanner._ReleaseBudget(),
        )

    written = sum(path.stat().st_size for path in destination.rglob("*") if path.is_file())
    assert written <= 8
    assert exit_info.value.__cause__ is None
    assert exit_info.value.__context__ is None


def test_candidate_copy_never_writes_beyond_aggregate_ceiling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanner = _scanner()
    source_root = tmp_path / "source"
    destination = tmp_path / "candidate"
    source_root.mkdir()
    sources = []
    for index in range(3):
        source = source_root / f"part-{index}.bin"
        source.write_bytes(b"four")
        sources.append(source)
    monkeypatch.setattr(scanner, "_MAX_RELEASE_FILE_BYTES", 8)
    monkeypatch.setattr(scanner, "_MAX_CANDIDATE_TREE_BYTES", 8)

    with pytest.raises(scanner.SecretScanError) as exit_info:
        scanner._materialize_candidate_tree(
            sources,
            root=source_root,
            destination=destination,
            budget=scanner._ReleaseBudget(),
        )

    written = sum(path.stat().st_size for path in destination.rglob("*") if path.is_file())
    assert written <= 8
    assert exit_info.value.__cause__ is None
    assert exit_info.value.__context__ is None


def test_history_scans_deleted_env_and_secret_without_disclosing_them(tmp_path: Path) -> None:
    scanner = _scanner()
    repo = tmp_path / "repo"
    _init_repo(repo)
    historical_path = repo / ".env.local"
    canary = _history_canary()
    historical_path.write_bytes(b"OPENAI_API_KEY=" + canary + b"\n")
    _commit(repo, "add historical secret")
    historical_path.write_text("OPENAI_API_KEY=\n", encoding="utf-8")
    _commit(repo, "replace historical secret")
    historical_path.unlink()
    (repo / ".env.example").write_text("OPENAI_API_KEY=\n", encoding="utf-8")
    _commit(repo, "delete historical secret")

    findings = scanner.scan_git_history(repo)

    matching = [finding for finding in findings if finding.path == b".env.local"]
    assert [finding.rule for finding in matching] == ["provider-key", "tracked-env-file"]
    report = scanner.format_findings(findings)
    assert canary.decode("ascii") not in report
    assert ".env.local" not in report


def test_history_preserves_every_raw_path_but_formats_only_opaque_ids(tmp_path: Path) -> None:
    scanner = _scanner()
    repo = tmp_path / "repo"
    _init_repo(repo)
    payload = b"TOKEN=" + _history_canary() + b"\n"
    paths = (b"path with spaces.txt", b"control-\n-\x1b.txt")
    for raw_path in paths:
        (repo / os.fsdecode(raw_path)).write_bytes(payload)
    _commit(repo, "reuse one blob at adversarial paths")

    findings = scanner.scan_git_history(repo)

    provider_paths = {finding.path for finding in findings if finding.rule == "provider-key"}
    assert provider_paths == set(paths)
    report = scanner.format_findings(findings)
    assert _history_canary().decode("ascii") not in report
    assert "path with spaces.txt" not in report
    assert "control-" not in report
    assert "\x1b" not in report
    assert all(
        re.fullmatch(r"[a-z-]+: path-sha256:[0-9a-f]{64}", line) for line in report.splitlines()
    )


def test_history_ignores_unreachable_orphan_blob(tmp_path: Path) -> None:
    scanner = _scanner()
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "README.md").write_text("clean\n", encoding="utf-8")
    _commit(repo, "clean root")
    _git(repo, "hash-object", "-w", "--stdin", input_bytes=_history_canary())

    assert scanner.scan_git_history(repo) == []


@pytest.mark.parametrize("repository_kind", ["shallow", "empty"])
def test_cli_fails_closed_for_incomplete_history(
    tmp_path: Path,
    repository_kind: str,
) -> None:
    scanner = _scanner()
    source = tmp_path / "source"
    _init_repo(source)
    if repository_kind == "shallow":
        secret = source / "secret.txt"
        secret.write_bytes(_history_canary())
        _commit(source, "secret only in old commit")
        secret.write_text("clean\n", encoding="utf-8")
        _commit(source, "clean head")
        repo = tmp_path / "clone"
        subprocess.run(
            ["git", "clone", "--quiet", "--depth=1", source.as_uri(), str(repo)],
            check=True,
            capture_output=True,
            env=_git_environment(tmp_path),
        )
    else:
        repo = source

    completed = _run_cli(repo)

    assert completed.returncode == 2
    assert completed.stdout.decode("utf-8") == scanner.SCAN_ERROR_MESSAGE + "\n"
    assert completed.stderr == b""
    assert _history_canary() not in completed.stdout


def test_malformed_git_payloads_fail_without_reaching_cli_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scanner = _scanner()
    canary = _history_canary().decode("ascii")
    with pytest.raises(scanner.SecretScanError):
        scanner._parse_diff_tree_output(canary.encode("ascii"), oid_length=40)
    with pytest.raises(scanner.SecretScanError):
        scanner._parse_cat_file_header(
            ("malformed " + canary).encode("ascii"),
            expected_oid=b"1" * 40,
        )

    def fail_with_sensitive_git_stderr(_root: Path) -> list[object]:
        raise RuntimeError("git stderr: " + canary)

    monkeypatch.setattr(scanner, "release_findings", fail_with_sensitive_git_stderr)
    with pytest.raises(SystemExit) as exit_info:
        scanner.main(["--root", str(tmp_path)])

    captured = capsys.readouterr()
    assert exit_info.value.code == 2
    assert captured.out == scanner.SCAN_ERROR_MESSAGE + "\n"
    assert captured.err == ""
    assert canary not in captured.out


def test_clean_history_passes(tmp_path: Path) -> None:
    scanner = _scanner()
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "README.md").write_text("clean\n", encoding="utf-8")
    _commit(repo, "clean root")
    (repo / "README.md").write_text("still clean\n", encoding="utf-8")
    _commit(repo, "clean update")

    assert scanner.scan_git_history(repo) == []


def test_inherited_git_dir_cannot_redirect_history_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanner = _scanner()
    target = tmp_path / "target"
    _init_repo(target)
    historical_path = target / "deleted.txt"
    historical_path.write_bytes(_history_canary())
    _commit(target, "secret history")
    historical_path.unlink()
    (target / "README.md").write_text("clean head\n", encoding="utf-8")
    _commit(target, "clean head")

    decoy = tmp_path / "decoy"
    _init_repo(decoy)
    (decoy / "README.md").write_text("clean decoy\n", encoding="utf-8")
    _commit(decoy, "clean decoy")
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))

    findings = scanner.scan_git_history(target)

    assert [finding.rule for finding in findings] == ["provider-key"]
    assert findings[0].path == b"deleted.txt"


def test_git_child_environment_drops_ambient_repository_and_config_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanner = _scanner()
    unsafe_names = (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        "GIT_TRACE",
        "GIT_TRACE_PACKET",
    )
    for name in unsafe_names:
        monkeypatch.setenv(name, "adversarial inherited value")

    environment = scanner._git_environment()

    expected_git_values = {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    assert {name: value for name, value in environment.items() if name.startswith("GIT_")} == (
        expected_git_values
    )
    assert environment["LC_ALL"] == "C"


def test_oserror_payload_is_not_chained_into_internal_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanner = _scanner()
    canary = _history_canary().decode("ascii")

    def fail_run(*_args: object, **_kwargs: object) -> None:
        raise OSError(str(tmp_path / canary))

    monkeypatch.setattr(scanner.subprocess, "run", fail_run)
    monkeypatch.setattr(scanner.subprocess, "Popen", fail_run)
    with pytest.raises(scanner.SecretScanError) as exit_info:
        scanner._run_git(tmp_path, ["rev-list", "HEAD"])

    error = exit_info.value
    assert error.__cause__ is None
    assert error.__context__ is None
    assert canary not in str(error)
    assert canary not in repr(error)


def test_timeout_payload_is_not_chained_into_internal_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanner = _scanner()
    canary = _history_canary().decode("ascii")

    def fail_start(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(["git", canary], timeout=1)

    monkeypatch.setattr(scanner.subprocess, "run", fail_start)
    monkeypatch.setattr(scanner.subprocess, "Popen", fail_start)
    with pytest.raises(scanner.SecretScanError) as exit_info:
        scanner._run_git(tmp_path, ["rev-list", "HEAD"])

    error = exit_info.value
    assert error.__cause__ is None
    assert error.__context__ is None
    assert canary not in str(error)
    assert canary not in repr(error)


def test_sha256_history_is_supported_when_git_supports_it(tmp_path: Path) -> None:
    scanner = _scanner()
    repo = tmp_path / "sha256-repo"
    repo.mkdir()
    initialized = subprocess.run(
        ["git", "init", "--quiet", "--initial-branch=main", "--object-format=sha256"],
        cwd=repo,
        check=False,
        capture_output=True,
        env=_git_environment(repo),
    )
    if initialized.returncode != 0:
        pytest.skip("local Git does not support SHA-256 repositories")
    secret = repo / "deleted.txt"
    secret.write_bytes(_history_canary())
    _commit(repo, "secret SHA-256 history")
    secret.unlink()
    (repo / "README.md").write_text("clean head\n", encoding="utf-8")
    _commit(repo, "clean SHA-256 head")

    findings = scanner.scan_git_history(repo)

    assert [(finding.path, finding.rule) for finding in findings] == [
        (b"deleted.txt", "provider-key")
    ]


def test_merge_rename_delete_executable_and_symlink_history_is_complete(tmp_path: Path) -> None:
    scanner = _scanner()
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "README.md").write_text("root\n", encoding="utf-8")
    _commit(repo, "root")
    _git(repo, "branch", "feature")
    (repo / "main.txt").write_text("main branch\n", encoding="utf-8")
    _commit(repo, "main branch")

    _git(repo, "switch", "--quiet", "feature")
    old_path = repo / "old name.txt"
    old_path.write_bytes(_history_canary())
    _commit(repo, "add old path")
    renamed_path = repo / "renamed.sh"
    old_path.rename(renamed_path)
    renamed_path.chmod(0o755)
    symlink_path = repo / "secret-link"
    symlink_path.symlink_to(os.fsdecode(_history_canary()))
    _commit(repo, "rename executable and add symlink")
    feature_tree = _git(repo, "ls-tree", "-r", "feature").stdout
    assert b"100755 blob" in feature_tree
    assert b"120000 blob" in feature_tree

    _git(repo, "switch", "--quiet", "main")
    _git(
        repo,
        "merge",
        "--quiet",
        "--no-ff",
        "--no-verify",
        "feature",
        "-m",
        "merge feature",
    )
    renamed_path.unlink()
    symlink_path.unlink()
    _commit(repo, "delete historical paths")

    findings = scanner.scan_git_history(repo)

    provider_paths = [finding.path for finding in findings if finding.rule == "provider-key"]
    assert provider_paths == [b"old name.txt", b"renamed.sh", b"secret-link"]


def test_gitlink_history_fails_closed(tmp_path: Path) -> None:
    scanner = _scanner()
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "README.md").write_text("root\n", encoding="utf-8")
    _commit(repo, "root")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip().decode("ascii")
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{head},vendor/dependency")
    _commit_index(repo, "add unscannable gitlink")

    with pytest.raises(scanner.SecretScanError):
        scanner.scan_git_history(repo)


def test_reachable_missing_blob_fails_closed(tmp_path: Path) -> None:
    scanner = _scanner()
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "payload.txt").write_text("clean payload\n", encoding="utf-8")
    _commit(repo, "root")
    blob_oid = _git(repo, "rev-parse", "HEAD:payload.txt").stdout.strip().decode("ascii")
    object_path = repo / ".git" / "objects" / blob_oid[:2] / blob_oid[2:]
    assert object_path.is_file()
    object_path.unlink()

    with pytest.raises(scanner.SecretScanError):
        scanner.scan_git_history(repo)


def test_multiple_blobs_use_one_cat_file_batch_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanner = _scanner()
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "one.txt").write_text("one\n", encoding="utf-8")
    (repo / "two.txt").write_text("two\n", encoding="utf-8")
    (repo / "three.txt").write_text("three\n", encoding="utf-8")
    _commit(repo, "multiple blobs")
    real_popen = scanner.subprocess.Popen
    cat_file_batches = 0

    def count_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        nonlocal cat_file_batches
        command = args[0]
        if command == ["git", "cat-file", "--batch"]:
            cat_file_batches += 1
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(scanner.subprocess, "Popen", count_popen)

    assert scanner.scan_git_history(repo) == []
    assert cat_file_batches == 1


def test_blob_batch_retains_only_matched_rule_ids(tmp_path: Path) -> None:
    scanner = _scanner()
    repo = tmp_path / "repo"
    _init_repo(repo)
    payload_path = repo / "payload.txt"
    canary = _history_canary()
    payload_path.write_bytes(canary)
    _commit(repo, "secret blob")
    oid = _git(repo, "rev-parse", "HEAD:payload.txt").stdout.strip()

    rules_by_oid = scanner._read_git_blob_rules(repo, [oid])

    assert rules_by_oid == {oid: ("provider-key",)}
    assert canary not in repr(rules_by_oid).encode("utf-8")


def test_blob_size_ceiling_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scanner = _scanner()
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "payload.txt").write_bytes(b"nine-byte")
    _commit(repo, "oversize blob")
    oid = _git(repo, "rev-parse", "HEAD:payload.txt").stdout.strip()
    monkeypatch.setattr(scanner, "_MAX_GIT_BLOB_BYTES", 8)

    with pytest.raises(scanner.SecretScanError):
        scanner._read_git_blob_rules(repo, [oid])


def test_cat_file_pipes_close_after_success_and_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanner = _scanner()
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "payload.txt").write_text("clean\n", encoding="utf-8")
    _commit(repo, "blob")
    oid = _git(repo, "rev-parse", "HEAD:payload.txt").stdout.strip()
    real_popen = scanner.subprocess.Popen
    cat_processes: list[subprocess.Popen[bytes]] = []

    def capture_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        process = real_popen(*args, **kwargs)
        if args[0] == ["git", "cat-file", "--batch"]:
            cat_processes.append(process)
        return process

    monkeypatch.setattr(scanner.subprocess, "Popen", capture_popen)
    assert scanner._read_git_blob_rules(repo, [oid]) == {oid: ()}
    monkeypatch.setattr(scanner, "_MAX_GIT_BLOB_BYTES", 0)
    with pytest.raises(scanner.SecretScanError):
        scanner._read_git_blob_rules(repo, [oid])

    assert len(cat_processes) == 2
    for process in cat_processes:
        assert process.stdin is not None and process.stdin.closed
        assert process.stdout is not None and process.stdout.closed


def test_git_stdout_uses_bounded_file_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanner = _scanner()
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "README.md").write_text("clean\n", encoding="utf-8")
    _commit(repo, "root")
    real_popen = scanner.subprocess.Popen
    observed_stdout: list[object] = []

    def inspect_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        observed_stdout.append(kwargs.get("stdout"))
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(scanner.subprocess, "Popen", inspect_popen)

    assert scanner._run_git(repo, ["rev-parse", "HEAD"]).strip()
    assert observed_stdout
    assert observed_stdout[-1] != subprocess.PIPE
    assert hasattr(observed_stdout[-1], "fileno")


def test_git_capture_and_deadline_limits_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanner = _scanner()
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "README.md").write_text("clean\n", encoding="utf-8")
    _commit(repo, "root")

    monkeypatch.setattr(scanner, "_MAX_GIT_CAPTURE_BYTES", 1)
    with pytest.raises(scanner.SecretScanError) as capture_error:
        scanner._run_git(repo, ["rev-parse", "HEAD"])
    assert capture_error.value.__cause__ is None
    assert capture_error.value.__context__ is None

    monkeypatch.setattr(scanner, "_MAX_GIT_CAPTURE_BYTES", 64 * 1024 * 1024)
    monkeypatch.setattr(scanner, "_GIT_COMMAND_TIMEOUT_SECONDS", 0.0)
    with pytest.raises(scanner.SecretScanError) as deadline_error:
        scanner._run_git(repo, ["rev-parse", "HEAD"])
    assert deadline_error.value.__cause__ is None
    assert deadline_error.value.__context__ is None


def test_cli_claims_configured_rules_and_head_file_blobs(tmp_path: Path) -> None:
    scanner = _scanner()
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "README.md").write_text("clean\n", encoding="utf-8")
    _commit(repo, "root")

    completed = _run_cli(repo)

    assert scanner.SCAN_ERROR_MESSAGE == (
        "Secret scan failed closed: repository and release inputs could not be fully verified."
    )
    assert completed.returncode == 0
    assert completed.stdout.decode("utf-8") == (
        "Secret scan passed: configured rules found no matches in current release inputs or "
        "HEAD-ancestry file blobs.\n"
    )
    assert completed.stderr == b""
