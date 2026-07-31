from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.export_openapi import (
    DEFAULT_OUTPUT_PATH,
    artifact_matches,
    build_openapi_bytes,
    canonical_json_bytes,
    main,
    write_artifact_atomic,
)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "export_openapi.py"


def test_exporter_import_is_lazy_and_default_is_canonical_docs_path() -> None:
    assert DEFAULT_OUTPUT_PATH == ROOT / "docs" / "openapi.json"
    program = "\n".join(
        (
            "import sys",
            "assert 'server.main' not in sys.modules",
            "import scripts.export_openapi",
            "assert 'server.main' not in sys.modules",
        )
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_cold_export_ignores_hostile_environment_and_creates_no_caller_db(
    tmp_path: Path,
) -> None:
    caller_database = tmp_path / "must-not-be-used.sqlite3"
    environment = os.environ.copy()
    environment.update(
        {
            "OPENAI_" + "API_KEY": "sk-" + "not-a-real-key" * 4,
            "BACKCHANNEL_DEPLOYED": "true",
            "BACKCHANNEL_LIVE_MAX_CONCURRENT": "invalid",
            "BACKCHANNEL_DB_PATH": str(caller_database),
            "BACKCHANNEL_FRONTEND_DIST_PATH": str(tmp_path / "missing-dist"),
            "PYTHONPATH": str(ROOT),
        }
    )
    program = "\n".join(
        (
            "import json",
            "import os",
            "from scripts.export_openapi import build_openapi_bytes",
            "before = os.environ.copy()",
            "payload = build_openapi_bytes()",
            "assert 'server.main' not in __import__('sys').modules",
            "assert os.environ == before",
            "json.loads(payload)",
        )
    )

    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not caller_database.exists()
    assert not (tmp_path / "backchannel.sqlite3").exists()


def test_check_missing_or_drifted_artifact_never_mutates(tmp_path: Path) -> None:
    expected = canonical_json_bytes({"openapi": "3.1.0"})
    destination = tmp_path / "openapi.json"

    assert artifact_matches(destination, expected) is False
    assert not destination.exists()

    drifted = b'{"openapi":"drifted"}\n'
    destination.write_bytes(drifted)
    assert artifact_matches(destination, expected) is False
    assert destination.read_bytes() == drifted


def test_canonical_export_is_utf8_sorted_newline_terminated_and_idempotent(
    tmp_path: Path,
) -> None:
    document = {"z": "caf\u00e9", "a": {"two": 2, "one": 1}}
    expected = (
        json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    destination = tmp_path / "nested" / "openapi.json"

    payload = canonical_json_bytes(document)
    write_artifact_atomic(destination, payload)
    first = destination.read_bytes()
    write_artifact_atomic(destination, payload)

    assert payload == expected
    assert first == expected
    assert destination.read_bytes() == first
    assert not list(destination.parent.glob("*.tmp"))


def test_generated_schema_matches_the_implemented_public_http_contract() -> None:
    schema = json.loads(build_openapi_bytes())
    paths = schema["paths"]
    recovery_snapshot_schema = schema["components"]["schemas"]["RecoverySnapshot"]

    assert {path: set(methods) for path, methods in paths.items()} == {
        "/health": {"get"},
        "/readyz": {"get"},
        "/api/scenarios": {"get"},
        "/api/recoveries": {"post"},
        "/api/recoveries/{recovery_id}": {"get"},
        "/api/recoveries/{recovery_id}/decisions": {"post"},
        "/api/recoveries/{recovery_id}/events": {"get"},
        "/api/recoveries/{recovery_id}/receipt": {"get"},
        "/api/demo/reset": {"post"},
    }
    assert recovery_snapshot_schema["allOf"] == [
        {
            "if": {
                "properties": {
                    "status": {
                        "enum": [
                            "completed",
                            "closed_without_action",
                            "outcome_unknown",
                        ]
                    }
                },
                "required": ["status"],
            },
            "then": {
                "properties": {"currentStep": {"const": 5}},
                "required": ["currentStep"],
            },
        }
    ]
    event_responses = paths["/api/recoveries/{recovery_id}/events"]["get"][
        "responses"
    ]
    assert event_responses["200"]["content"] == {"text/event-stream": {}}
    assert event_responses["400"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/PublicErrorResponse"
    }
    assert event_responses["429"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/PublicErrorResponse"
    }
    assert event_responses["429"]["headers"]["Retry-After"]["description"] == (
        "Retry delay in seconds for a stream-capacity response."
    )
    assert event_responses["429"]["headers"]["Retry-After"]["schema"] == {
        "maximum": 300,
        "minimum": 1,
        "type": "integer",
    }
    assert event_responses["422"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/PublicErrorResponse"
    }
    creation_responses = paths["/api/recoveries"]["post"]["responses"]
    operational_cache_control_header = {
        "description": (
            "Prevents storage of mutable public operational status in shared or "
            "persistent caches."
        ),
        "schema": {
            "enum": ["no-store"],
            "type": "string",
        },
    }
    for path in ("/health", "/readyz"):
        assert paths[path]["get"]["responses"]["200"]["headers"] == {
            "Cache-Control": operational_cache_control_header
        }
    cache_control_header = {
        "description": (
            "Prevents storage of owner-scoped recovery data in shared or persistent "
            "caches."
        ),
        "schema": {
            "enum": ["private, no-store"],
            "type": "string",
        },
    }
    for path, method, success_status in (
        ("/api/recoveries", "post", "201"),
        ("/api/recoveries/{recovery_id}", "get", "200"),
        ("/api/recoveries/{recovery_id}/decisions", "post", "200"),
        ("/api/recoveries/{recovery_id}/events", "get", "200"),
        ("/api/recoveries/{recovery_id}/receipt", "get", "200"),
    ):
        assert paths[path][method]["responses"][success_status]["headers"] == {
            "Cache-Control": cache_control_header
        }
    assert creation_responses["429"]["description"] == (
        "The request exceeded the public creation budget or a live-only capacity, "
        "cooldown, or daily-budget admission limit."
    )
    assert creation_responses["429"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/PublicErrorResponse"
    }
    assert creation_responses["429"]["headers"]["Retry-After"]["description"] == (
        "Retry delay in seconds when provided. Creation-budget exhaustion uses "
        "1..86400 seconds until the next UTC midnight; live-only outcomes may use "
        "a longer or otherwise different delay."
    )
    assert creation_responses["429"]["headers"]["Retry-After"]["schema"] == {
        "minimum": 1,
        "type": "integer",
    }
    decision_responses = paths["/api/recoveries/{recovery_id}/decisions"]["post"][
        "responses"
    ]
    snapshot_responses = paths["/api/recoveries/{recovery_id}"]["get"]["responses"]
    receipt_responses = paths["/api/recoveries/{recovery_id}/receipt"]["get"][
        "responses"
    ]
    reset_responses = paths["/api/demo/reset"]["post"]["responses"]
    retryable_store_response = {
        "description": "Retry delay in seconds for bounded SQLite access or draining.",
        "schema": {
            "maximum": 1,
            "minimum": 1,
            "type": "integer",
        },
    }
    for response in (
        creation_responses["503"],
        decision_responses["503"],
        event_responses["503"],
        snapshot_responses["503"],
        receipt_responses["503"],
        reset_responses["503"],
    ):
        assert response["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/PublicErrorResponse"
        }
        assert response["headers"]["Retry-After"] == retryable_store_response
    readiness_unavailable = paths["/readyz"]["get"]["responses"]["503"]
    assert readiness_unavailable["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/PublicErrorResponse"
    }
    assert "Retry-After" not in readiness_unavailable.get("headers", {})
    for response in (creation_responses["504"], decision_responses["504"]):
        assert response["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/PublicErrorResponse"
        }
        assert "Retry-After" not in response.get("headers", {})
    for path, method in (
        ("/health", "get"),
        ("/readyz", "get"),
        ("/api/scenarios", "get"),
        ("/api/recoveries/{recovery_id}", "get"),
        ("/api/recoveries/{recovery_id}/events", "get"),
        ("/api/recoveries/{recovery_id}/receipt", "get"),
    ):
        assert "504" not in paths[path][method]["responses"]
    assert "HTTPValidationError" not in schema["components"]["schemas"]

    serialized = json.dumps(schema, sort_keys=True)
    for internal_name in (
        "state_json",
        "serialized_state",
        "pending_state_json",
        "model_metadata_json",
        "proof_payload",
        "identity_hmac_secret",
    ):
        assert internal_name not in serialized
    for internal_path in ("/docs", "/openapi.json", "/redoc", "/{frontend_path:path}"):
        assert internal_path not in paths


def test_cli_check_uses_the_tracked_default_artifact_without_mutation() -> None:
    before = DEFAULT_OUTPUT_PATH.read_bytes()
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert DEFAULT_OUTPUT_PATH.read_bytes() == before


@pytest.mark.parametrize("failure_point", ["read", "stat"])
def test_cli_redacts_check_filesystem_failures(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure_point: str,
) -> None:
    monkeypatch.setattr(
        "scripts.export_openapi.build_openapi_bytes", lambda: b"{}\n"
    )
    if failure_point == "read":
        monkeypatch.setattr(
            "scripts.export_openapi.artifact_matches",
            lambda _destination, _expected: (_ for _ in ()).throw(
                OSError("/absolute/private/read-path")
            ),
        )
    else:
        monkeypatch.setattr(
            "scripts.export_openapi.artifact_matches",
            lambda _destination, _expected: False,
        )
        monkeypatch.setattr(
            Path,
            "exists",
            lambda _path: (_ for _ in ()).throw(
                OSError("/absolute/private/stat-path")
            ),
        )

    assert main(["--check"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "OpenAPI artifact filesystem operation failed.\n"
    assert "/absolute/private" not in captured.err


@pytest.mark.parametrize("failure_point", ["mkdir", "temp", "fsync", "replace"])
def test_cli_redacts_write_filesystem_failures(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure_point: str,
) -> None:
    monkeypatch.setattr(
        "scripts.export_openapi.build_openapi_bytes", lambda: b"{}\n"
    )

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError(f"/absolute/private/{failure_point}-path")

    if failure_point == "mkdir":
        monkeypatch.setattr(Path, "mkdir", fail)
    elif failure_point == "temp":
        monkeypatch.setattr("scripts.export_openapi.tempfile.mkstemp", fail)
    elif failure_point == "fsync":
        monkeypatch.setattr("scripts.export_openapi.os.fsync", fail)
    else:
        monkeypatch.setattr("scripts.export_openapi.os.replace", fail)

    assert main([]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "OpenAPI artifact filesystem operation failed.\n"
    assert "/absolute/private" not in captured.err
