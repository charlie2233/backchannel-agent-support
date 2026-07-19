from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]


def _scanner() -> ModuleType:
    path = ROOT / "scripts" / "secret_scan.py"
    assert path.is_file()
    spec = importlib.util.spec_from_file_location("release_secret_scan", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def test_scanner_accepts_placeholders_and_binary_png(tmp_path: Path) -> None:
    scanner = _scanner()
    example = tmp_path / ".env.example"
    example.write_text("OPENAI_API_KEY=\n", encoding="utf-8")
    capture = tmp_path / "pending.png"
    capture.write_bytes(b"\x89PNG\r\n\x1a\n" + (b"\x00" * 64))

    assert scanner.scan_paths([example, capture], root=tmp_path) == []
