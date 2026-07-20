from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]


def _exporter() -> ModuleType:
    path = ROOT / "scripts" / "export_openapi.py"
    assert path.is_file()
    spec = importlib.util.spec_from_file_location("release_openapi_export", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_openapi_export_is_deterministic_and_secret_free() -> None:
    exporter = _exporter()

    first = exporter.render_openapi()
    second = exporter.render_openapi()

    assert first == second
    assert first.endswith("\n")
    assert "OPENAI_API_KEY" not in first
    assert "state_json" not in first


def test_private_recovery_operations_document_one_generic_not_found_boundary() -> None:
    schema = json.loads(_exporter().render_openapi())
    operations = (
        ("/api/recoveries/{recovery_id}", "get"),
        ("/api/recoveries/{recovery_id}/events", "get"),
        ("/api/recoveries/{recovery_id}/receipt", "get"),
        ("/api/recoveries/{recovery_id}/decisions", "post"),
    )
    for path, method in operations:
        operation = schema["paths"][path][method]
        assert operation["responses"]["404"] == {
            "description": "Recovery not found."
        }
        description = operation["description"].lower()
        assert "signed opaque demo session" in description
        assert "generic not-found" in description


def test_event_stream_success_response_is_documented_as_sse() -> None:
    schema = json.loads(_exporter().render_openapi())

    content = schema["paths"]["/api/recoveries/{recovery_id}/events"]["get"][
        "responses"
    ]["200"]["content"]

    assert "text/event-stream" in content
    assert "application/json" not in content


def test_export_never_imports_global_app_or_mutates_caller_database(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sentinel_database = tmp_path / "caller.sqlite3"
    sentinel_bytes = b"caller-database-must-remain-byte-for-byte-unchanged"
    sentinel_database.write_bytes(sentinel_bytes)
    monkeypatch.setenv("BACKCHANNEL_DB_PATH", str(sentinel_database))
    monkeypatch.setenv("BACKCHANNEL_DEPLOYED_MODE", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "caller-environment-sentinel")
    server_main_was_loaded = "server.main" in sys.modules

    exporter = _exporter()
    assert sentinel_database.read_bytes() == sentinel_bytes
    assert server_main_was_loaded or "server.main" not in sys.modules

    rendered = exporter.render_openapi()

    assert '"title": "Backchannel API"' in rendered
    assert sentinel_database.read_bytes() == sentinel_bytes
    assert os.environ["BACKCHANNEL_DB_PATH"] == str(sentinel_database)
    assert os.environ["BACKCHANNEL_DEPLOYED_MODE"] == "true"
    assert os.environ["OPENAI_API_KEY"] == "caller-environment-sentinel"
    assert server_main_was_loaded or "server.main" not in sys.modules
