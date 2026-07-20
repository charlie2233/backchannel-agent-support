from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType

from server.models import ApprovalDecisionRequest

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
        ("/api/recoveries/{recovery_id}/decisions/resume", "post"),
    )
    for path, method in operations:
        operation = schema["paths"][path][method]
        assert operation["responses"]["404"] == {
            "description": "Recovery not found."
        }
        description = operation["description"].lower()
        assert "signed opaque demo session" in description
        assert "generic not-found" in description


def test_decision_resume_documents_only_an_exact_empty_json_request() -> None:
    schema = json.loads(_exporter().render_openapi())

    operation = schema["paths"][
        "/api/recoveries/{recovery_id}/decisions/resume"
    ]["post"]
    request_body = operation["requestBody"]

    assert request_body["required"] is True
    assert set(request_body["content"]) == {"application/json"}
    assert request_body["content"]["application/json"]["schema"] == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
        "maxProperties": 0,
    }


def test_decision_documents_the_exact_strict_pydantic_request_schema() -> None:
    schema = json.loads(_exporter().render_openapi())

    operation = schema["paths"]["/api/recoveries/{recovery_id}/decisions"]["post"]
    request_body = operation["requestBody"]
    expected = ApprovalDecisionRequest.model_json_schema(by_alias=True)
    definitions = expected.pop("$defs")
    action_schema = expected["properties"]["action"]
    assert action_schema == {"$ref": "#/$defs/DecisionAction"}
    expected["properties"]["action"] = definitions["DecisionAction"]

    assert request_body["required"] is True
    assert set(request_body["content"]) == {"application/json"}
    operation_schema = request_body["content"]["application/json"]["schema"]
    assert operation_schema == expected
    serialized = json.dumps(operation_schema)
    assert '"$defs"' not in serialized
    assert '"$ref"' not in serialized


def test_decision_422_documents_both_stable_public_error_shapes() -> None:
    schema = json.loads(_exporter().render_openapi())

    response = schema["paths"]["/api/recoveries/{recovery_id}/decisions"]["post"][
        "responses"
    ]["422"]

    assert response == {
        "description": (
            "The authenticated decision body or consent is invalid, or the recovery "
            "path is not a valid UUID."
        ),
        "content": {
            "application/json": {
                "schema": {
                    "oneOf": [
                        {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["detail"],
                            "properties": {
                                "detail": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": ["code", "recoveryId"],
                                    "properties": {
                                        "code": {
                                            "type": "string",
                                            "enum": [
                                                "authority_denied",
                                                "constraint_denied",
                                                "decision_body_invalid",
                                                "remedy_digest_mismatch",
                                                "remedy_expired",
                                                "remedy_mismatch",
                                                "tool_call_mismatch",
                                            ],
                                        },
                                        "recoveryId": {
                                            "type": "string",
                                            "format": "uuid",
                                        },
                                    },
                                }
                            },
                        },
                        {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["code", "message", "requestId"],
                            "properties": {
                                "code": {
                                    "type": "string",
                                    "enum": ["invalid_request"],
                                },
                                "message": {
                                    "type": "string",
                                    "enum": [
                                        "The request did not match the public API contract."
                                    ],
                                },
                                "requestId": {
                                    "type": "string",
                                    "pattern": "^[0-9a-f]{32}$",
                                },
                            },
                        },
                    ]
                }
            }
        },
    }


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
