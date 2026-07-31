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


def test_pending_approval_schema_exposes_only_policy_eligible_consent() -> None:
    schema = json.loads(_exporter().render_openapi())

    properties = schema["components"]["schemas"]["PendingApprovalView"]["properties"]
    for field in ("hardConstraintSatisfied", "delegatedAuthoritySatisfied"):
        assert properties[field] == {
            "const": True,
            "title": properties[field]["title"],
            "type": "boolean",
        }


def test_recovery_creation_requires_a_strict_client_request_id() -> None:
    schema = json.loads(_exporter().render_openapi())

    request_body = schema["paths"]["/api/recoveries"]["post"]["requestBody"]
    assert request_body == {
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "$ref": "#/components/schemas/CreateRecoveryRequest",
                }
            }
        },
    }

    request_schema = schema["components"]["schemas"]["CreateRecoveryRequest"]

    assert request_schema["required"] == [
        "scenarioId",
        "executionMode",
        "clientRequestId",
    ]
    assert request_schema["additionalProperties"] is False
    assert request_schema["properties"]["clientRequestId"] == {
        "maxLength": 128,
        "minLength": 1,
        "pattern": "^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$",
        "title": "Clientrequestid",
        "type": "string",
    }


def test_recovery_creation_documents_exact_idempotency_conflict_envelopes() -> None:
    schema = json.loads(_exporter().render_openapi())

    response = schema["paths"]["/api/recoveries"]["post"]["responses"]["409"]

    assert response == {
        "description": (
            "The session-scoped recovery creation key is already in use, still "
            "being resolved, or has an outcome that cannot be confirmed. "
            "Retry-After is returned only for creation_pending."
        ),
        "headers": {
            "Retry-After": {
                "description": (
                    "Seconds to wait before retrying the same clientRequestId. "
                    "Present only when code is creation_pending."
                ),
                "schema": {
                    "enum": ["2"],
                    "type": "string",
                },
            }
        },
        "content": {
            "application/json": {
                "schema": {
                    "oneOf": [
                        {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["code", "message", "requestId"],
                            "properties": {
                                "code": {
                                    "type": "string",
                                    "enum": ["idempotency_conflict"],
                                },
                                "message": {
                                    "type": "string",
                                    "enum": [
                                        "This recovery start no longer matches its "
                                        "original request. No additional run was started."
                                    ],
                                },
                                "requestId": {
                                    "type": "string",
                                    "pattern": "^[0-9a-f]{32}$",
                                },
                            },
                        },
                        {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["code", "message", "requestId"],
                            "properties": {
                                "code": {
                                    "type": "string",
                                    "enum": ["creation_pending"],
                                },
                                "message": {
                                    "type": "string",
                                    "enum": [
                                        "Recovery creation is unresolved. Retry the "
                                        "same start shortly."
                                    ],
                                },
                                "requestId": {
                                    "type": "string",
                                    "pattern": "^[0-9a-f]{32}$",
                                },
                            },
                        },
                        {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["code", "message", "requestId"],
                            "properties": {
                                "code": {
                                    "type": "string",
                                    "enum": ["creation_outcome_unknown"],
                                },
                                "message": {
                                    "type": "string",
                                    "enum": [
                                        "The recovery start outcome could not be "
                                        "confirmed. No replacement run was started."
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


def test_recovery_creation_documents_only_exact_reachable_429_envelopes() -> None:
    schema = json.loads(_exporter().render_openapi())

    response = schema["paths"]["/api/recoveries"]["post"]["responses"]["429"]

    assert response == {
        "description": (
            "A new recovery start would exceed creation-ledger capacity, or a live "
            "start would exceed live admission, cooldown, or daily-budget policy."
        ),
        "content": {
            "application/json": {
                "schema": {
                    "oneOf": [
                        {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["code", "message", "requestId"],
                            "properties": {
                                "code": {
                                    "type": "string",
                                    "enum": ["creation_capacity"],
                                },
                                "message": {
                                    "type": "string",
                                    "enum": [
                                        "Recovery creation is temporarily at capacity. "
                                        "Existing starts can still be retried; try a "
                                        "new start later."
                                    ],
                                },
                                "requestId": {
                                    "type": "string",
                                    "pattern": "^[0-9a-f]{32}$",
                                },
                            },
                        },
                        {
                            "type": "object",
                            "additionalProperties": False,
                            "required": [
                                "code",
                                "message",
                                "requestId",
                                "fallbackExecutionMode",
                            ],
                            "properties": {
                                "code": {
                                    "type": "string",
                                    "enum": ["live_capacity"],
                                },
                                "message": {
                                    "type": "string",
                                    "enum": [
                                        "Live recovery is currently at capacity. A "
                                        "replay fixture is starting automatically; "
                                        "you can rerun it explicitly."
                                    ],
                                },
                                "requestId": {
                                    "type": "string",
                                    "pattern": "^[0-9a-f]{32}$",
                                },
                                "fallbackExecutionMode": {
                                    "type": "string",
                                    "enum": ["replay_fixture"],
                                },
                            },
                        },
                        {
                            "type": "object",
                            "additionalProperties": False,
                            "required": [
                                "code",
                                "message",
                                "requestId",
                                "fallbackExecutionMode",
                            ],
                            "properties": {
                                "code": {
                                    "type": "string",
                                    "enum": ["cooldown"],
                                },
                                "message": {
                                    "type": "string",
                                    "enum": [
                                        "Please wait before starting another live "
                                        "recovery. A replay fixture is starting "
                                        "automatically; you can rerun it explicitly."
                                    ],
                                },
                                "requestId": {
                                    "type": "string",
                                    "pattern": "^[0-9a-f]{32}$",
                                },
                                "fallbackExecutionMode": {
                                    "type": "string",
                                    "enum": ["replay_fixture"],
                                },
                            },
                        },
                        {
                            "type": "object",
                            "additionalProperties": False,
                            "required": [
                                "code",
                                "message",
                                "requestId",
                                "fallbackExecutionMode",
                            ],
                            "properties": {
                                "code": {
                                    "type": "string",
                                    "enum": ["daily_budget"],
                                },
                                "message": {
                                    "type": "string",
                                    "enum": [
                                        "The daily live demo budget is currently "
                                        "reached. A replay fixture is starting "
                                        "automatically; you can rerun it explicitly."
                                    ],
                                },
                                "requestId": {
                                    "type": "string",
                                    "pattern": "^[0-9a-f]{32}$",
                                },
                                "fallbackExecutionMode": {
                                    "type": "string",
                                    "enum": ["replay_fixture"],
                                },
                            },
                        },
                    ]
                }
            }
        },
    }
    assert "headers" not in response
    alternatives = response["content"]["application/json"]["schema"]["oneOf"]
    assert [alternative["properties"]["code"]["enum"] for alternative in alternatives] == [
        ["creation_capacity"],
        ["live_capacity"],
        ["cooldown"],
        ["daily_budget"],
    ]


def test_recovery_creation_documents_exact_413_and_422_envelopes() -> None:
    schema = json.loads(_exporter().render_openapi())
    responses = schema["paths"]["/api/recoveries"]["post"]["responses"]

    assert set(responses) == {"201", "409", "413", "422", "429"}
    assert responses["413"] == {
        "description": "The bounded public request body is too large.",
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["code", "message", "requestId"],
                    "properties": {
                        "code": {
                            "type": "string",
                            "enum": ["request_too_large"],
                        },
                        "message": {
                            "type": "string",
                            "enum": ["The request body is too large."],
                        },
                        "requestId": {
                            "type": "string",
                            "pattern": "^[0-9a-f]{32}$",
                        },
                    },
                }
            }
        },
    }
    assert responses["422"] == {
        "description": (
            "The creation request is invalid, the selected scenario does not support "
            "live execution, or live recovery is unavailable."
        ),
        "content": {
            "application/json": {
                "schema": {
                    "oneOf": [
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
                        {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["detail"],
                            "properties": {
                                "detail": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": ["code"],
                                    "properties": {
                                        "code": {
                                            "type": "string",
                                            "enum": ["unsupported_scenario_mode"],
                                        }
                                    },
                                }
                            },
                        },
                        {
                            "type": "object",
                            "additionalProperties": False,
                            "required": [
                                "code",
                                "message",
                                "requestId",
                                "fallbackExecutionMode",
                            ],
                            "properties": {
                                "code": {
                                    "type": "string",
                                    "enum": ["live_unavailable"],
                                },
                                "message": {
                                    "type": "string",
                                    "enum": [
                                        "Live recovery is unavailable in this demo. "
                                        "A replay fixture is starting automatically; "
                                        "you can rerun it explicitly."
                                    ],
                                },
                                "requestId": {
                                    "type": "string",
                                    "pattern": "^[0-9a-f]{32}$",
                                },
                                "fallbackExecutionMode": {
                                    "type": "string",
                                    "enum": ["replay_fixture"],
                                },
                            },
                        },
                    ]
                }
            }
        },
    }


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
        assert operation["responses"]["404"] == {"description": "Recovery not found."}
        description = operation["description"].lower()
        assert "signed opaque demo session" in description
        assert "generic not-found" in description


def test_owner_read_invalid_uuid_documents_correlated_public_error() -> None:
    schema = json.loads(_exporter().render_openapi())
    expected = {
        "description": "The recovery path is not a valid UUID.",
        "content": {
            "application/json": {
                "schema": {
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
                }
            }
        },
    }

    for path in (
        "/api/recoveries/{recovery_id}",
        "/api/recoveries/{recovery_id}/events",
        "/api/recoveries/{recovery_id}/receipt",
    ):
        response = schema["paths"][path]["get"]["responses"]["422"]
        assert response == expected
        assert "HTTPValidationError" not in json.dumps(response)


def test_decision_resume_documents_only_an_exact_empty_json_request() -> None:
    schema = json.loads(_exporter().render_openapi())

    operation = schema["paths"]["/api/recoveries/{recovery_id}/decisions/resume"]["post"]
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

    response = schema["paths"]["/api/recoveries/{recovery_id}/decisions"]["post"]["responses"][
        "422"
    ]

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
                                    "enum": ["The request did not match the public API contract."],
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


def test_decision_documents_exact_conflict_and_capacity_responses() -> None:
    schema = json.loads(_exporter().render_openapi())

    responses = schema["paths"]["/api/recoveries/{recovery_id}/decisions"]["post"][
        "responses"
    ]

    assert set(responses) == {"200", "404", "409", "413", "422", "429"}
    conflict = responses["409"]
    assert conflict["description"] == (
        "The authenticated decision conflicts with authoritative recovery state."
    )
    assert conflict["content"]["application/json"]["schema"] == {
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
                            "already_decided",
                            "decision_id_conflict",
                            "decision_resume_unavailable",
                            "decision_unavailable",
                            "resume_incompatible",
                        ],
                    },
                    "recoveryId": {
                        "type": "string",
                        "format": "uuid",
                    },
                },
            }
        },
    }
    assert responses["429"] == {
        "description": "Live decision processing is currently at capacity.",
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["code", "message", "requestId"],
                    "properties": {
                        "code": {
                            "type": "string",
                            "enum": ["decision_capacity"],
                        },
                        "message": {
                            "type": "string",
                            "enum": [
                                "Live decision processing is currently at capacity. "
                                "Retry the same decision shortly."
                            ],
                        },
                        "requestId": {
                            "type": "string",
                            "pattern": "^[0-9a-f]{32}$",
                        },
                    },
                }
            }
        },
    }
    assert responses["413"] == {
        "description": "The bounded public request body is too large.",
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["code", "message", "requestId"],
                    "properties": {
                        "code": {
                            "type": "string",
                            "enum": ["request_too_large"],
                        },
                        "message": {
                            "type": "string",
                            "enum": ["The request body is too large."],
                        },
                        "requestId": {
                            "type": "string",
                            "pattern": "^[0-9a-f]{32}$",
                        },
                    },
                }
            }
        },
    }


def test_decision_resume_422_documents_body_and_expiry_errors() -> None:
    schema = json.loads(_exporter().render_openapi())

    response = schema["paths"]["/api/recoveries/{recovery_id}/decisions/resume"]["post"][
        "responses"
    ]["422"]
    variants = response["content"]["application/json"]["schema"]["oneOf"]

    assert response["description"] == (
        "The authenticated resume body, stored decision claim, or consent is "
        "invalid; the exact claim may also be expired, or the recovery path "
        "may not be a valid UUID."
    )
    assert variants[0]["properties"]["detail"]["properties"]["code"]["enum"] == [
        "authority_denied",
        "constraint_denied",
        "decision_resume_body_invalid",
        "remedy_digest_mismatch",
        "remedy_expired",
        "remedy_mismatch",
        "tool_call_mismatch",
    ]
    assert variants[1]["properties"]["code"]["enum"] == ["invalid_request"]


def test_decision_resume_documents_exact_conflict_and_capacity_responses() -> None:
    schema = json.loads(_exporter().render_openapi())

    responses = schema["paths"][
        "/api/recoveries/{recovery_id}/decisions/resume"
    ]["post"]["responses"]

    assert set(responses) == {"200", "404", "409", "413", "422", "429"}
    conflict = responses["409"]
    assert conflict["description"] == (
        "The authenticated resume conflicts with authoritative recovery state."
    )
    assert conflict["content"]["application/json"]["schema"]["properties"][
        "detail"
    ]["properties"]["code"]["enum"] == [
        "decision_id_conflict",
        "decision_resume_unavailable",
        "decision_unavailable",
        "resume_incompatible",
    ]
    assert conflict["content"]["application/json"]["schema"][
        "additionalProperties"
    ] is False
    assert responses["429"] == schema["paths"][
        "/api/recoveries/{recovery_id}/decisions"
    ]["post"]["responses"]["429"]
    assert responses["413"] == schema["paths"][
        "/api/recoveries/{recovery_id}/decisions"
    ]["post"]["responses"]["413"]


def test_demo_reset_documents_unresolved_creation_conflict() -> None:
    schema = json.loads(_exporter().render_openapi())

    response = schema["paths"]["/api/demo/reset"]["post"]["responses"]["409"]

    assert response == {
        "description": (
            "Reset is refused without mutation while this session owns an unresolved "
            "recovery start."
        ),
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["code", "message", "requestId"],
                    "properties": {
                        "code": {
                            "type": "string",
                            "enum": ["reset_creation_pending"],
                        },
                        "message": {
                            "type": "string",
                            "enum": [
                                "Demo reset is unavailable while a recovery start is "
                                "unresolved. Retry reset after the start resolves or "
                                "the signed session expires."
                            ],
                        },
                        "requestId": {
                            "type": "string",
                            "pattern": "^[0-9a-f]{32}$",
                        },
                    },
                }
            }
        },
    }


def test_event_stream_success_response_is_documented_as_sse() -> None:
    schema = json.loads(_exporter().render_openapi())

    operation = schema["paths"]["/api/recoveries/{recovery_id}/events"]["get"]
    content = operation["responses"]["200"]["content"]

    assert "text/event-stream" in content
    assert "application/json" not in content
    assert (
        "An admitted stream is bound to the exact verified signed-session expiry "
        "and emits no later buffered events, polled events, or heartbeats. A "
        "non-empty ASGI body send still blocked at expiry is cancelled before "
        "completion; final teardown is bounded. If that session loses recovery "
        "access after admission, the stream closes without a synthetic event and "
        "a later reconnect receives the ordinary generic not-found response."
        in operation["description"]
    )


def test_event_stream_documents_exact_cursor_grammar_range_and_error() -> None:
    schema = json.loads(_exporter().render_openapi())

    operation = schema["paths"]["/api/recoveries/{recovery_id}/events"]["get"]
    cursor_parameters = [
        parameter
        for parameter in operation["parameters"]
        if parameter["in"] == "header" and parameter["name"] == "Last-Event-ID"
    ]
    assert cursor_parameters == [
        {
            "description": (
                "Optional durable event cursor. When present, use ASCII decimal digits "
                "only and a value from 0 through 9223372036854775807. Authorization is "
                "evaluated before this header, which must occur at most once."
            ),
            "in": "header",
            "name": "Last-Event-ID",
            "required": False,
            "schema": {
                "pattern": "^[0-9]+$",
                "type": "string",
            },
        }
    ]
    assert operation["responses"]["400"] == {
        "description": "The authorized event cursor is outside the supported contract.",
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["detail"],
                    "properties": {
                        "detail": {
                            "type": "string",
                            "enum": [
                                "Last-Event-ID must contain only ASCII digits and be "
                                "between 0 and 9223372036854775807"
                            ],
                        }
                    },
                }
            }
        },
    }


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
