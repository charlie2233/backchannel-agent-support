"""FastAPI entry point for truthful recovery persistence and streaming."""

import asyncio
import hashlib
import json
import re
import sqlite3
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Any, cast
from uuid import UUID, uuid4

from agents.models.interface import ModelProvider
from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import Receive, Scope, Send

from server.cleanup import (
    cleanup_expired_recovery_creations,
    cleanup_terminal_recoveries,
    expire_pending_approvals,
)
from server.config import RuntimeSettings
from server.controls import (
    DECISION_CAPACITY_MESSAGE,
    REQUEST_BODY_TOO_LARGE_MESSAGE,
    ClientIdentity,
    LiveAdmissionCode,
    LiveAdmissionError,
    LiveDecisionCapacityError,
    PublicBoundaryMiddleware,
    PublicDemoControls,
    RequestBodyTooLarge,
    SanitizedApplicationError,
)
from server.events import (
    EventStreamAdmissionController,
    EventStreamLease,
    encode_stream_capacity_event,
    lease_event_stream,
    stream_recovery_events,
)
from server.logging import get_safe_logger, install_server_log_safety, log_safe_exception
from server.models import (
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    CreateRecoveryRequest,
    DecisionResumeRequest,
    DecisionResumeResponse,
    DemoResetResponse,
    ExecutionMode,
    HealthResponse,
    ProviderBoundary,
    ReadinessResponse,
    RecoveryReceipt,
    RecoverySnapshot,
    RuntimeBackend,
    ScenarioResponse,
)
from server.orchestrator import (
    RecoveryOrchestrator,
    ResumeIncompatibleError,
)
from server.providers.hotel_simulator import HotelSimulator
from server.providers.quota_simulator import QuotaSimulator
from server.replay.engine import (
    ReplayEngine,
    replay_recovery_id,
)
from server.replay.loader import ScenarioLoader, ScenarioNotFoundError
from server.store import (
    ApprovalDecisionError,
    RecoveryCreationClaim,
    RecoveryNotFoundError,
    ResetCreationPendingError,
    SQLiteStore,
)

logger = get_safe_logger(__name__)

_PRODUCTION_STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "dist"
_READY_SCHEMA_COLUMNS = {
    "recoveries": frozenset(
        {
            "id",
            "scenario_id",
            "execution_mode",
            "status",
            "current_step",
            "current_step_summary",
            "model_ids_json",
            "root_trace_id",
            "model_call",
            "sdk_version",
            "protocol_version",
            "agent_graph_version",
            "definition_digest",
            "quota_execution_contract",
            "created_at",
            "updated_at",
        }
    ),
    "quota_legacy_completions": frozenset({"recovery_id", "recovery_fingerprint", "recorded_at"}),
    "remedies": frozenset(
        {
            "id",
            "recovery_id",
            "terms_json",
            "digest",
            "cost_delta_minor",
            "changed_fields_json",
            "provider_commitments_json",
            "expiry",
            "hard_constraint_satisfied",
            "delegated_authority_satisfied",
            "evidence_json",
            "status",
            "created_at",
        }
    ),
    "pending_approvals": frozenset(
        {
            "tool_call_id",
            "recovery_id",
            "sdk_version",
            "protocol_version",
            "agent_graph_version",
            "definition_digest",
            "root_trace_id",
            "model_ids_json",
            "execution_mode",
            "action_digest",
            "remedy_id",
            "consent_digest",
            "state_json",
            "status",
            "created_at",
            "updated_at",
        }
    ),
    "approval_decisions": frozenset(
        {
            "recovery_id",
            "client_decision_id",
            "action",
            "remedy_id",
            "remedy_digest",
            "tool_call_id",
            "request_fingerprint",
            "status",
            "result_json",
            "claimed_at",
            "completed_at",
        }
    ),
    "executions": frozenset(
        {
            "id",
            "recovery_id",
            "idempotency_key",
            "status",
            "provider_execution",
            "request_digest",
            "tool_call_id",
            "remedy_digest",
            "result_json",
            "created_at",
            "updated_at",
        }
    ),
    "events": frozenset(
        {"id", "recovery_id", "seq", "type", "terminal", "data_json", "created_at"}
    ),
    "receipts": frozenset({"recovery_id", "receipt_json", "created_at"}),
    "usage_ledger": frozenset({"id", "recovery_id", "category", "amount", "recorded_at"}),
    "demo_sessions": frozenset({"id", "created_at", "expires_at"}),
    "recovery_access": frozenset({"recovery_id", "session_key"}),
    "live_admissions": frozenset(
        {
            "recovery_id",
            "ip_key",
            "session_key",
            "budget_units",
            "admitted_at",
            "expires_at",
            "released_at",
        }
    ),
    "recovery_creations": frozenset(
        {
            "request_key",
            "request_fingerprint",
            "session_key",
            "ip_key",
            "scenario_id",
            "execution_mode",
            "recovery_id",
            "status",
            "created_at",
            "updated_at",
            "expires_at",
        }
    ),
    "expiry_scan_state": frozenset({"candidate_kind", "last_expiry", "last_recovery_id"}),
    "readiness_probe": frozenset({"id", "generation"}),
}
_READY_PROBE_COLUMNS = [
    (0, "id", "INTEGER", 0, None, 1, 0),
    (1, "generation", "INTEGER", 1, None, 0, 0),
]
_READY_DB_BUSY_TIMEOUT_MS = 350
_READY_DB_CACHE_SECONDS = 5.0
_CREATION_PENDING_RETRY_SECONDS = 2
_CREATION_ERROR_MESSAGES = {
    "idempotency_conflict": (
        "This recovery start no longer matches its original request. No additional run was started."
    ),
    "creation_pending": ("Recovery creation is unresolved. Retry the same start shortly."),
    "creation_outcome_unknown": (
        "The recovery start outcome could not be confirmed. No replacement run was started."
    ),
    "creation_capacity": (
        "Recovery creation is temporarily at capacity. Existing starts can still "
        "be retried; try a new start later."
    ),
}
_RECOVERY_CREATION_CONFLICT_RESPONSE: dict[str, Any] = {
    "description": (
        "The session-scoped recovery creation key is already in use, still being "
        "resolved, or has an outcome that cannot be confirmed. Retry-After is "
        "returned only for creation_pending."
    ),
    "headers": {
        "Retry-After": {
            "description": (
                "Seconds to wait before retrying the same clientRequestId. "
                "Present only when code is creation_pending."
            ),
            "schema": {
                "enum": [str(_CREATION_PENDING_RETRY_SECONDS)],
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
                                "enum": [code],
                            },
                            "message": {
                                "type": "string",
                                "enum": [message],
                            },
                            "requestId": {
                                "type": "string",
                                "pattern": "^[0-9a-f]{32}$",
                            },
                        },
                    }
                    for code, message in _CREATION_ERROR_MESSAGES.items()
                    if code != "creation_capacity"
                ]
            }
        }
    },
}
_RECOVERY_CREATION_CAPACITY_RESPONSE: dict[str, Any] = {
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
                                "enum": [_CREATION_ERROR_MESSAGES["creation_capacity"]],
                            },
                            "requestId": {
                                "type": "string",
                                "pattern": "^[0-9a-f]{32}$",
                            },
                        },
                    },
                    *[
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
                                    "enum": [live_code.value],
                                },
                                "message": {
                                    "type": "string",
                                    "enum": [LiveAdmissionError(live_code).public_message],
                                },
                                "requestId": {
                                    "type": "string",
                                    "pattern": "^[0-9a-f]{32}$",
                                },
                                "fallbackExecutionMode": {
                                    "type": "string",
                                    "enum": [ExecutionMode.REPLAY_FIXTURE.value],
                                },
                            },
                        }
                        for live_code in (
                            LiveAdmissionCode.LIVE_CAPACITY,
                            LiveAdmissionCode.COOLDOWN,
                            LiveAdmissionCode.DAILY_BUDGET,
                        )
                    ],
                ]
            }
        }
    },
}
_RESET_CREATION_PENDING_MESSAGE = (
    "Demo reset is unavailable while a recovery start is unresolved. "
    "Retry reset after the start resolves or the signed session expires."
)
_DEMO_RESET_CONFLICT_RESPONSE: dict[str, Any] = {
    "description": (
        "Reset is refused without mutation while this session owns an unresolved recovery start."
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
                        "enum": [_RESET_CREATION_PENDING_MESSAGE],
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
_SPA_SEGMENT = re.compile(r"^[A-Za-z0-9_-]+$")
_RESERVED_ROUTE_ROOTS = frozenset(
    {"api", "assets", "docs", "health", "openapi.json", "readyz", "redoc"}
)


class _RecoveryCreationPublicError(RuntimeError):
    """Stable public creation error without a raw client request token."""

    def __init__(self, code: str, *, retry_after: int | None = None) -> None:
        self.code = code
        self.retry_after = retry_after
        super().__init__(code)


def _recovery_creation_fingerprint(payload: CreateRecoveryRequest) -> str:
    canonical = json.dumps(
        {
            "executionMode": payload.execution_mode.value,
            "scenarioId": payload.scenario_id.value,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


_PRIVATE_RECOVERY_DESCRIPTION = (
    "Requires the signed opaque demo session issued when the recovery was created. "
    "Missing, expired, tampered, unrelated, and unknown sessions receive the same "
    "generic not-found response."
)
_PRIVATE_NOT_FOUND_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {"description": "Recovery not found."}
}
_INVALID_REQUEST_MESSAGE = "The request did not match the public API contract."
_INVALID_REQUEST_SCHEMA: dict[str, Any] = {
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
            "enum": [_INVALID_REQUEST_MESSAGE],
        },
        "requestId": {
            "type": "string",
            "pattern": "^[0-9a-f]{32}$",
        },
    },
}
_INVALID_RECOVERY_PATH_RESPONSE: dict[str, Any] = {
    "description": "The recovery path is not a valid UUID.",
    "content": {
        "application/json": {
            "schema": _INVALID_REQUEST_SCHEMA,
        }
    },
}
_PRIVATE_RECOVERY_READ_RESPONSES: dict[int | str, dict[str, Any]] = {
    **_PRIVATE_NOT_FOUND_RESPONSE,
    status.HTTP_422_UNPROCESSABLE_CONTENT: _INVALID_RECOVERY_PATH_RESPONSE,
}


def _decision_conflict_response(
    *,
    description: str,
    codes: list[str],
) -> dict[str, Any]:
    return {
        "description": description,
        "content": {
            "application/json": {
                "schema": {
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
                                    "enum": codes,
                                },
                                "recoveryId": {
                                    "type": "string",
                                    "format": "uuid",
                                },
                            },
                        }
                    },
                }
            }
        },
    }


_DECISION_CONFLICT_RESPONSE = _decision_conflict_response(
    description=(
        "The authenticated decision conflicts with authoritative recovery state."
    ),
    codes=[
        "already_decided",
        "decision_id_conflict",
        "decision_resume_unavailable",
        "decision_unavailable",
        "resume_incompatible",
    ],
)
_DECISION_RESUME_CONFLICT_RESPONSE = _decision_conflict_response(
    description=(
        "The authenticated resume conflicts with authoritative recovery state."
    ),
    codes=[
        "decision_id_conflict",
        "decision_resume_unavailable",
        "decision_unavailable",
        "resume_incompatible",
    ],
)
_DECISION_CAPACITY_RESPONSE: dict[str, Any] = {
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
                        "enum": [DECISION_CAPACITY_MESSAGE],
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
_REQUEST_BODY_TOO_LARGE_RESPONSE: dict[str, Any] = {
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
                        "enum": [REQUEST_BODY_TOO_LARGE_MESSAGE],
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
_RECOVERY_CREATION_UNPROCESSABLE_RESPONSE: dict[str, Any] = {
    "description": (
        "The creation request is invalid, the selected scenario does not support "
        "live execution, or live recovery is unavailable."
    ),
    "content": {
        "application/json": {
            "schema": {
                "oneOf": [
                    _INVALID_REQUEST_SCHEMA,
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
                                "enum": [LiveAdmissionCode.LIVE_UNAVAILABLE.value],
                            },
                            "message": {
                                "type": "string",
                                "enum": [
                                    LiveAdmissionError(
                                        LiveAdmissionCode.LIVE_UNAVAILABLE
                                    ).public_message
                                ],
                            },
                            "requestId": {
                                "type": "string",
                                "pattern": "^[0-9a-f]{32}$",
                            },
                            "fallbackExecutionMode": {
                                "type": "string",
                                "enum": [ExecutionMode.REPLAY_FIXTURE.value],
                            },
                        },
                    },
                ]
            }
        }
    },
}

_DECISION_UNPROCESSABLE_RESPONSE: dict[str, Any] = {
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
                                "enum": [_INVALID_REQUEST_MESSAGE],
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
_DECISION_RESUME_UNPROCESSABLE_RESPONSE: dict[str, Any] = {
    "description": (
        "The authenticated resume body, stored decision claim, or consent is "
        "invalid; the exact claim may also be expired, or the recovery path "
        "may not be a valid UUID."
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
                                            "decision_resume_body_invalid",
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
                                "enum": [_INVALID_REQUEST_MESSAGE],
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
_EVENT_STREAM_DESCRIPTION = (
    f"{_PRIVATE_RECOVERY_DESCRIPTION} Streams are admitted by bounded, "
    "process-local capacity. At capacity, the endpoint returns a finite "
    "stream.capacity control event with a native EventSource retry interval. "
    "An admitted stream is bound to the exact verified signed-session expiry "
    "and emits no later buffered events, polled events, or heartbeats. A "
    "non-empty ASGI body send still blocked at expiry is cancelled before "
    "completion; final teardown is bounded. If that session loses recovery "
    "access after admission, the stream closes without a synthetic event and "
    "a later reconnect receives the ordinary generic not-found response."
)
_EVENT_STREAM_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
}
_EVENT_STREAM_FINAL_SEND_GRACE_SECONDS = 0.1
_MAX_EVENT_CURSOR = 2**63 - 1
_MAX_EVENT_CURSOR_TEXT = str(_MAX_EVENT_CURSOR)
_EVENT_CURSOR_PATTERN = re.compile(r"^[0-9]+$")
_INVALID_EVENT_CURSOR_DETAIL = (
    f"Last-Event-ID must contain only ASCII digits and be between 0 and {_MAX_EVENT_CURSOR_TEXT}"
)
_EVENT_CURSOR_HEADER_DESCRIPTION = (
    "Optional durable event cursor. When present, use ASCII decimal digits only "
    f"and a value from 0 through {_MAX_EVENT_CURSOR_TEXT}. Authorization is "
    "evaluated before this header, which must occur at most once."
)
_EVENT_STREAM_RESPONSES: dict[int | str, dict[str, Any]] = {
    **_PRIVATE_RECOVERY_READ_RESPONSES,
    status.HTTP_400_BAD_REQUEST: {
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
                            "enum": [_INVALID_EVENT_CURSOR_DETAIL],
                        }
                    },
                }
            }
        },
    },
}
_EVENT_STREAM_OPENAPI_EXTRA: dict[str, Any] = {
    "parameters": [
        {
            "description": _EVENT_CURSOR_HEADER_DESCRIPTION,
            "in": "header",
            "name": "Last-Event-ID",
            "required": False,
            "schema": {
                "pattern": _EVENT_CURSOR_PATTERN.pattern,
                "type": "string",
            },
        }
    ]
}


def _parse_event_cursor(last_event_id: str | None) -> int:
    """Parse an SSE cursor without exceeding SQLite's signed integer domain."""

    if last_event_id is None:
        return 0
    if _EVENT_CURSOR_PATTERN.fullmatch(last_event_id) is None:
        raise ValueError("Event cursor must contain only ASCII decimal digits")
    normalized = last_event_id.lstrip("0") or "0"
    if (
        len(normalized) > len(_MAX_EVENT_CURSOR_TEXT)
        or len(normalized) == len(_MAX_EVENT_CURSOR_TEXT)
        and normalized > _MAX_EVENT_CURSOR_TEXT
    ):
        raise ValueError("Event cursor exceeds SQLite's signed integer range")
    return int(normalized)


def _inline_local_schema_definitions(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a self-contained copy of a Pydantic schema with local refs expanded."""

    definitions = schema.get("$defs", {})
    if not isinstance(definitions, Mapping):
        raise ValueError("Pydantic schema definitions must be an object")

    def expand(value: Any) -> Any:
        if isinstance(value, list):
            return [expand(item) for item in value]
        if not isinstance(value, Mapping):
            return value
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            if set(value) != {"$ref"}:
                raise ValueError("Local schema references cannot have siblings")
            definition = definitions.get(reference.removeprefix("#/$defs/"))
            if not isinstance(definition, Mapping):
                raise ValueError("Local schema reference target is missing")
            return expand(definition)
        return {key: expand(item) for key, item in value.items() if key != "$defs"}

    expanded = expand(schema)
    if not isinstance(expanded, dict):
        raise ValueError("Pydantic schema must expand to an object")
    return expanded


class _LeaseReleasingStreamingResponse(StreamingResponse):
    """Bind evidence sends to session expiry and always release admission."""

    media_type = "text/event-stream"

    def __init__(
        self,
        content: AsyncIterator[str],
        *,
        lease: EventStreamLease,
        headers: Mapping[str, str],
        session_expires_at: datetime,
        session_clock: Callable[[], datetime],
    ) -> None:
        if (
            session_expires_at.tzinfo is None
            or session_expires_at.utcoffset() != timedelta(0)
        ):
            raise ValueError("Event stream session expiry must be a UTC timestamp")
        self._event_stream_lease = lease
        self._session_expires_at = session_expires_at
        self._session_clock = session_clock
        super().__init__(content, media_type=self.media_type, headers=headers)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            session_expired = self._remaining_session_seconds() <= 0

            async def send_before_session_expiry(message: dict[str, Any]) -> None:
                nonlocal session_expired
                if message["type"] == "http.response.start":
                    remaining_seconds = self._remaining_session_seconds()
                    if remaining_seconds <= 0:
                        session_expired = True
                        self._event_stream_lease.release()
                        await send(message)
                        return

                    async def forward_response_start() -> None:
                        await send(message)

                    send_task = asyncio.create_task(forward_response_start())
                    deadline_task = asyncio.create_task(
                        asyncio.sleep(remaining_seconds)
                    )
                    try:
                        completed, _pending = await asyncio.wait(
                            (send_task, deadline_task),
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if send_task in completed:
                            await send_task
                            return
                        session_expired = True
                        self._event_stream_lease.release()
                        await send_task
                    finally:
                        for task in (send_task, deadline_task):
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(
                            send_task,
                            deadline_task,
                            return_exceptions=True,
                        )
                    return
                body = message.get("body", b"")
                if message["type"] != "http.response.body":
                    await send(message)
                    return
                if (
                    isinstance(body, bytes | memoryview)
                    and not body
                    and message.get("more_body") is False
                ):
                    try:
                        async with asyncio.timeout(
                            _EVENT_STREAM_FINAL_SEND_GRACE_SECONDS
                        ):
                            await send(message)
                    except TimeoutError:
                        return
                    return
                if session_expired:
                    return
                remaining_seconds = self._remaining_session_seconds()
                if remaining_seconds <= 0:
                    session_expired = True
                    return
                try:
                    async with asyncio.timeout(remaining_seconds):
                        await send(message)
                except TimeoutError:
                    if self._remaining_session_seconds() > 0:
                        raise
                    session_expired = True

            await super().__call__(
                scope,
                receive,
                cast(Send, send_before_session_expiry),
            )
        finally:
            self._event_stream_lease.release()

    def _remaining_session_seconds(self) -> float:
        current = self._session_clock()
        if current.tzinfo is None or current.utcoffset() != timedelta(0):
            raise ValueError("Event stream session clock must return a UTC timestamp")
        return (self._session_expires_at - current).total_seconds()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _build_admitted_event_stream_response(
    content: AsyncIterator[str],
    lease: EventStreamLease,
    *,
    session_expires_at: datetime,
    session_clock: Callable[[], datetime] = _utc_now,
) -> StreamingResponse:
    """Construct a leased response without leaking admission on failure."""

    try:
        return _LeaseReleasingStreamingResponse(
            content,
            lease=lease,
            headers=_EVENT_STREAM_HEADERS,
            session_expires_at=session_expires_at,
            session_clock=session_clock,
        )
    except BaseException:
        lease.release()
        raise


def _store_schema_is_ready(
    store: SQLiteStore,
    *,
    connection_factory: Callable[[], sqlite3.Connection] | None = None,
    commit: Callable[[sqlite3.Connection], None] | None = None,
) -> bool:
    """Commit one bounded SQLite write after required schema and probe checks."""

    connection: sqlite3.Connection | None = None
    try:
        connection = (
            connection_factory()
            if connection_factory is not None
            else store._connect_readiness(timeout_seconds=_READY_DB_BUSY_TIMEOUT_MS / 1_000)
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {_READY_DB_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")

        integrity = connection.execute("PRAGMA quick_check(1)").fetchone()
        if integrity is None or integrity[0] != "ok":
            return False
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            return False
        for table_name, required_columns in _READY_SCHEMA_COLUMNS.items():
            actual_columns = {
                str(row[1])
                for row in connection.execute(f'PRAGMA table_info("{table_name}")').fetchall()
            }
            if not required_columns.issubset(actual_columns):
                return False
        quota_contract_column = next(
            (
                row
                for row in connection.execute('PRAGMA table_xinfo("recoveries")').fetchall()
                if str(row[1]) == "quota_execution_contract"
            ),
            None,
        )
        recoveries_schema_row = connection.execute(
            """
            SELECT sql
            FROM main.sqlite_master
            WHERE type = 'table' AND name = 'recoveries'
            """
        ).fetchone()
        normalized_recoveries_schema = (
            "".join(str(recoveries_schema_row[0]).lower().split())
            if recoveries_schema_row is not None and recoveries_schema_row[0] is not None
            else ""
        )
        if (
            quota_contract_column is None
            or str(quota_contract_column[2]).upper() != "INTEGER"
            or int(quota_contract_column[3]) != 1
            or str(quota_contract_column[4]) != "0"
            or int(quota_contract_column[6]) != 0
            or (
                "quota_execution_contractintegernotnulldefault0check("
                "quota_execution_contractin(0,1))" not in normalized_recoveries_schema
            )
            or connection.execute(
                """
                SELECT 1
                FROM recoveries
                WHERE typeof(quota_execution_contract) != 'integer'
                   OR quota_execution_contract NOT IN (0, 1)
                LIMIT 1
                """
            ).fetchone()
            is not None
        ):
            return False
        legacy_quota_columns = [
            (
                str(row[1]),
                str(row[2]).upper(),
                int(row[3]),
                int(row[5]),
            )
            for row in connection.execute(
                'PRAGMA table_info("quota_legacy_completions")'
            ).fetchall()
        ]
        if legacy_quota_columns != [
            ("recovery_id", "TEXT", 1, 1),
            ("recovery_fingerprint", "TEXT", 1, 0),
            ("recorded_at", "TEXT", 1, 0),
        ]:
            return False
        legacy_quota_foreign_keys = connection.execute(
            'PRAGMA foreign_key_list("quota_legacy_completions")'
        ).fetchall()
        if len(legacy_quota_foreign_keys) != 1:
            return False
        legacy_quota_foreign_key = legacy_quota_foreign_keys[0]
        if (
            str(legacy_quota_foreign_key[2]),
            str(legacy_quota_foreign_key[3]),
            str(legacy_quota_foreign_key[4]),
            str(legacy_quota_foreign_key[6]).upper(),
        ) != ("recoveries", "recovery_id", "id", "CASCADE"):
            return False
        if (
            connection.execute(
                """
                SELECT 1
                FROM quota_legacy_completions
                WHERE typeof(recovery_fingerprint) != 'text'
                   OR length(recovery_fingerprint) != 64
                   OR recovery_fingerprint GLOB '*[^0-9a-f]*'
                   OR typeof(recorded_at) != 'text'
                LIMIT 1
                """
            ).fetchone()
            is not None
        ):
            return False
        creation_ip_column = next(
            (
                row
                for row in connection.execute('PRAGMA table_xinfo("recovery_creations")').fetchall()
                if str(row[1]) == "ip_key"
            ),
            None,
        )
        creation_schema_row = connection.execute(
            """
            SELECT sql
            FROM main.sqlite_master
            WHERE type = 'table' AND name = 'recovery_creations'
            """
        ).fetchone()
        normalized_creation_schema = (
            "".join(str(creation_schema_row[0]).lower().split())
            if creation_schema_row is not None and creation_schema_row[0] is not None
            else ""
        )
        if (
            creation_ip_column is None
            or str(creation_ip_column[2]).upper() != "TEXT"
            or int(creation_ip_column[3]) != 1
            or int(creation_ip_column[6]) != 0
            or ("ip_keytextnotnullcheck(length(ip_key)=64)" not in normalized_creation_schema)
        ):
            return False
        if (
            connection.execute(
                """
                SELECT 1
                FROM recovery_creations
                WHERE typeof(ip_key) <> 'text'
                   OR length(ip_key) <> 64
                   OR ip_key GLOB '*[^0-9a-f]*'
                LIMIT 1
                """
            ).fetchone()
            is not None
        ):
            return False
        access_columns = [
            (str(row[1]), int(row[5]))
            for row in connection.execute('PRAGMA table_info("recovery_access")').fetchall()
        ]
        if access_columns != [("recovery_id", 1), ("session_key", 2)]:
            return False
        access_foreign_keys = connection.execute(
            'PRAGMA foreign_key_list("recovery_access")'
        ).fetchall()
        if len(access_foreign_keys) != 1:
            return False
        access_foreign_key = access_foreign_keys[0]
        if (
            str(access_foreign_key[2]),
            str(access_foreign_key[3]),
            str(access_foreign_key[4]),
            str(access_foreign_key[6]).upper(),
        ) != ("recoveries", "recovery_id", "id", "CASCADE"):
            return False
        if not store.quota_evidence_is_ready(connection):
            return False

        probe_objects = connection.execute(
            """
            SELECT type
            FROM main.sqlite_master
            WHERE name = 'readiness_probe'
            """
        ).fetchall()
        if len(probe_objects) != 1 or str(probe_objects[0][0]) != "table":
            return False
        if (
            connection.execute(
                """
                SELECT 1
                FROM main.sqlite_master
                WHERE type = 'trigger'
                  AND tbl_name = 'readiness_probe' COLLATE NOCASE
                LIMIT 1
                """
            ).fetchone()
            is not None
        ):
            return False

        probe_columns = [
            (
                int(row[0]),
                str(row[1]),
                str(row[2]).upper(),
                int(row[3]),
                row[4],
                int(row[5]),
                int(row[6]),
            )
            for row in connection.execute('PRAGMA main.table_xinfo("readiness_probe")').fetchall()
        ]
        if probe_columns != _READY_PROBE_COLUMNS:
            return False
        probe_rows = connection.execute(
            "SELECT id, generation FROM main.readiness_probe ORDER BY id"
        ).fetchall()
        if len(probe_rows) != 1:
            return False
        probe_id = probe_rows[0][0]
        generation = probe_rows[0][1]
        if type(probe_id) is not int or type(generation) is not int:
            return False
        if probe_id != 1 or generation not in (0, 1):
            return False

        update = connection.execute(
            """
            UPDATE main.readiness_probe
            SET generation = CASE generation WHEN 0 THEN 1 ELSE 0 END
            WHERE id = 1 AND generation IN (0, 1)
            """
        )
        if update.rowcount != 1:
            return False
        toggled_rows = connection.execute(
            "SELECT id, generation FROM main.readiness_probe ORDER BY id"
        ).fetchall()
        if len(toggled_rows) != 1:
            return False
        toggled_id = toggled_rows[0][0]
        toggled_generation = toggled_rows[0][1]
        if type(toggled_id) is not int or type(toggled_generation) is not int:
            return False
        if (toggled_id, toggled_generation) != (1, 1 - generation):
            return False

        if commit is None:
            connection.commit()
        else:
            commit(connection)
        if connection.in_transaction:
            return False
    except Exception:
        return False
    finally:
        if connection is not None:
            try:
                if connection.in_transaction:
                    connection.rollback()
            except Exception:
                pass
            try:
                connection.close()
            except Exception:
                pass
    return True


class _CachedReadinessProbe:
    """Coalesce and briefly cache success or failure for one application process."""

    def __init__(
        self,
        probe: Callable[[], bool],
        *,
        clock: Callable[[], float] = monotonic,
        cache_seconds: float = _READY_DB_CACHE_SECONDS,
        available: Callable[[], bool] | None = None,
    ) -> None:
        self._probe = probe
        self._clock = clock
        self._cache_seconds = cache_seconds
        self._available = available
        self._lock = Lock()
        self._checked_at: float | None = None
        self._cached_result: bool | None = None

    def is_ready(self) -> bool:
        with self._lock:
            if self._available is not None and not self._available():
                self._cached_result = False
                self._checked_at = self._clock()
                return False
            now = self._clock()
            if self._checked_at is not None and self._cached_result is not None:
                elapsed = now - self._checked_at
                if 0 <= elapsed < self._cache_seconds:
                    return self._cached_result
            result = self._probe()
            if self._available is not None and not self._available():
                result = False
            self._cached_result = result
            self._checked_at = self._clock()
            return result


def _is_route_like_spa_path(path: str) -> bool:
    if not path:
        return True
    segments = path.split("/")
    if (
        segments[0].lower() in _RESERVED_ROUTE_ROOTS
        or any(segment in {"", ".", ".."} for segment in segments)
        or any(_SPA_SEGMENT.fullmatch(segment) is None for segment in segments)
    ):
        return False
    return True


def _request_id(request: Request) -> str:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else uuid4().hex


def _validated_recovery_id(request: Request) -> str | None:
    value = getattr(request.state, "recovery_id", None)
    if not isinstance(value, str):
        return None
    try:
        return str(UUID(value))
    except ValueError:
        return None


def _require_recovery_access(
    request: Request,
    store: SQLiteStore,
    recovery_id: str,
) -> None:
    identity = cast(ClientIdentity, request.state.demo_identity)
    if not store.recovery_is_accessible(recovery_id, identity.session_key):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Not found",
        )


def _public_error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    fallback_execution_mode: ExecutionMode | None = None,
) -> JSONResponse:
    request_id = _request_id(request)
    content: dict[str, str] = {
        "code": code,
        "message": message,
        "requestId": request_id,
    }
    if fallback_execution_mode is not None:
        content["fallbackExecutionMode"] = fallback_execution_mode.value
    headers = {
        "X-Request-ID": request_id,
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
        "Cache-Control": "no-store",
    }
    runtime_settings = cast(RuntimeSettings, request.app.state.runtime_settings)
    if runtime_settings.deployed_mode:
        headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response = JSONResponse(status_code=status_code, content=content, headers=headers)
    identity = getattr(request.state, "demo_identity", None)
    if isinstance(identity, ClientIdentity) and identity.new_session_cookie is not None:
        response.set_cookie(
            runtime_settings.demo_session_cookie_name,
            identity.new_session_cookie,
            max_age=runtime_settings.demo_session_lifetime_seconds,
            httponly=True,
            secure=runtime_settings.effective_demo_session_cookie_secure,
            samesite="lax",
            path="/",
        )
    return response


def create_app(
    settings: RuntimeSettings | None = None,
    *,
    store: SQLiteStore | None = None,
    hotel_provider: HotelSimulator | None = None,
    quota_provider: QuotaSimulator | None = None,
    orchestrator: RecoveryOrchestrator | None = None,
    model_provider: ModelProvider | None = None,
    static_dir: Path | None = None,
    readiness_clock: Callable[[], float] | None = None,
    readiness_connection_factory: Callable[[], sqlite3.Connection] | None = None,
    readiness_commit: Callable[[sqlite3.Connection], None] | None = None,
) -> FastAPI:
    install_server_log_safety()
    runtime_settings = settings or RuntimeSettings.from_environment()
    recovery_store = store or SQLiteStore(runtime_settings.database_path)
    scenario_loader = ScenarioLoader()
    public_replay_scenarios = {scenario.id: scenario for scenario in scenario_loader.list()}
    replay_engine = ReplayEngine(recovery_store, scenario_loader)
    recovery_orchestrator = orchestrator
    if recovery_orchestrator is None:
        provider = hotel_provider or HotelSimulator(store=recovery_store)
        recovery_orchestrator = RecoveryOrchestrator(
            store=recovery_store,
            hotel_provider=provider,
            quota_provider=quota_provider,
            live_ready=runtime_settings.live_ready,
            model_provider=model_provider,
        )
    public_controls = PublicDemoControls(recovery_store, runtime_settings)
    event_stream_admission = EventStreamAdmissionController(
        max_active=runtime_settings.max_concurrent_event_streams,
        max_per_recovery=runtime_settings.max_event_streams_per_recovery,
        retry_seconds=runtime_settings.event_stream_retry_seconds,
    )
    configured_static_dir = static_dir.resolve() if static_dir is not None else None
    static_index = (
        configured_static_dir / "index.html" if configured_static_dir is not None else None
    )
    terminal_ttl = timedelta(seconds=runtime_settings.terminal_recovery_ttl_seconds)
    database_readiness = _CachedReadinessProbe(
        lambda: _store_schema_is_ready(
            recovery_store,
            connection_factory=readiness_connection_factory,
            commit=readiness_commit,
        ),
        clock=readiness_clock if readiness_clock is not None else monotonic,
        available=lambda: not recovery_store.closed,
    )

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        cleanup_expired_recovery_creations(
            recovery_store,
            batch_size=100,
        )
        expire_pending_approvals(
            recovery_store,
            batch_size=100,
        )
        cleanup_terminal_recoveries(
            recovery_store,
            terminal_ttl=terminal_ttl,
            batch_size=100,
        )
        stop_cleanup = asyncio.Event()

        async def run_periodic_cleanup() -> None:
            while True:
                try:
                    await asyncio.wait_for(
                        stop_cleanup.wait(),
                        timeout=runtime_settings.terminal_cleanup_interval_seconds,
                    )
                except TimeoutError:
                    try:
                        cleanup_expired_recovery_creations(
                            recovery_store,
                            batch_size=100,
                        )
                        expire_pending_approvals(
                            recovery_store,
                            batch_size=100,
                        )
                        cleanup_terminal_recoveries(
                            recovery_store,
                            terminal_ttl=terminal_ttl,
                            batch_size=100,
                        )
                    except Exception as error:
                        log_safe_exception(
                            logger,
                            request_id="retention_cleanup",
                            error=error,
                        )
                else:
                    return

        cleanup_task = asyncio.create_task(run_periodic_cleanup())
        try:
            yield
        finally:
            stop_cleanup.set()
            await cleanup_task

    application = FastAPI(
        title="Backchannel API",
        version="0.3.0",
        lifespan=lifespan,
    )
    application.state.recovery_store = recovery_store
    application.state.recovery_orchestrator = recovery_orchestrator
    application.state.public_demo_controls = public_controls
    application.state.event_stream_admission = event_stream_admission
    application.state.runtime_settings = runtime_settings
    application.state.static_dir = configured_static_dir
    application.state.database_readiness = database_readiness
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(runtime_settings.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Accept", "Content-Type", "Last-Event-ID"],
    )
    application.add_middleware(
        PublicBoundaryMiddleware,
        controls=public_controls,
        settings=runtime_settings,
    )

    @application.exception_handler(_RecoveryCreationPublicError)
    async def recovery_creation_error(
        request: Request,
        error: _RecoveryCreationPublicError,
    ) -> JSONResponse:
        if error.code == "creation_outcome_unknown" and error.__cause__ is not None:
            log_safe_exception(
                logger,
                request_id=_request_id(request),
                recovery_id=_validated_recovery_id(request),
                error=error.__cause__,
            )
        response = _public_error_response(
            request,
            status_code=(
                status.HTTP_429_TOO_MANY_REQUESTS
                if error.code == "creation_capacity"
                else status.HTTP_409_CONFLICT
            ),
            code=error.code,
            message=_CREATION_ERROR_MESSAGES[error.code],
        )
        if error.retry_after is not None:
            response.headers["Retry-After"] = str(error.retry_after)
        return response

    @application.exception_handler(ResetCreationPendingError)
    async def reset_creation_pending(
        request: Request,
        _error: ResetCreationPendingError,
    ) -> JSONResponse:
        return _public_error_response(
            request,
            status_code=status.HTTP_409_CONFLICT,
            code="reset_creation_pending",
            message=_RESET_CREATION_PENDING_MESSAGE,
        )

    @application.exception_handler(LiveAdmissionError)
    async def live_admission_error(
        request: Request,
        error: LiveAdmissionError,
    ) -> JSONResponse:
        return _public_error_response(
            request,
            status_code=error.status_code,
            code=error.code.value,
            message=error.public_message,
            fallback_execution_mode=ExecutionMode.REPLAY_FIXTURE,
        )

    @application.exception_handler(LiveDecisionCapacityError)
    async def live_decision_capacity(
        request: Request,
        error: LiveDecisionCapacityError,
    ) -> JSONResponse:
        return _public_error_response(
            request,
            status_code=error.status_code,
            code=error.code,
            message=error.public_message,
        )

    @application.exception_handler(RequestValidationError)
    async def invalid_request(
        request: Request,
        _error: RequestValidationError,
    ) -> JSONResponse:
        return _public_error_response(
            request,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="invalid_request",
            message=_INVALID_REQUEST_MESSAGE,
        )

    @application.exception_handler(RequestBodyTooLarge)
    async def request_too_large(
        request: Request,
        _error: RequestBodyTooLarge,
    ) -> JSONResponse:
        return _public_error_response(
            request,
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            code="request_too_large",
            message=REQUEST_BODY_TOO_LARGE_MESSAGE,
        )

    @application.exception_handler(Exception)
    async def internal_error(request: Request, error: Exception) -> JSONResponse:
        if not isinstance(error, SanitizedApplicationError):
            log_safe_exception(
                logger,
                request_id=_request_id(request),
                recovery_id=_validated_recovery_id(request),
                error=error,
            )
        return _public_error_response(
            request,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="internal_error",
            message="The request could not be completed.",
        )

    @application.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        backend = RuntimeBackend.OPENAI if runtime_settings.live_ready else RuntimeBackend.STUB
        return HealthResponse(
            backend=backend,
            liveReady=runtime_settings.live_ready,
            providerBoundary=ProviderBoundary.DEMO_ADAPTER_ONLY,
        )

    @application.get("/readyz", response_model=ReadinessResponse)
    def ready() -> Response:
        database_ready = database_readiness.is_ready()
        static_ready = static_index is None or static_index.is_file()
        readiness_status = "ready" if database_ready and static_ready else "not_ready"
        return JSONResponse(
            status_code=(
                status.HTTP_200_OK
                if readiness_status == "ready"
                else status.HTTP_503_SERVICE_UNAVAILABLE
            ),
            content={"status": readiness_status},
        )

    @application.get("/api/scenarios", response_model=list[ScenarioResponse])
    def scenarios() -> list[ScenarioResponse]:
        return [
            ScenarioResponse(
                id=scenario.id,
                title=scenario.title,
                summary=scenario.summary,
                executionMode=scenario.execution_mode,
            )
            for scenario in scenario_loader.list()
        ]

    @application.post(
        "/api/recoveries",
        response_model=RecoverySnapshot,
        status_code=status.HTTP_201_CREATED,
        responses={
            status.HTTP_409_CONFLICT: _RECOVERY_CREATION_CONFLICT_RESPONSE,
            status.HTTP_413_CONTENT_TOO_LARGE: _REQUEST_BODY_TOO_LARGE_RESPONSE,
            status.HTTP_422_UNPROCESSABLE_CONTENT: (
                _RECOVERY_CREATION_UNPROCESSABLE_RESPONSE
            ),
            status.HTTP_429_TOO_MANY_REQUESTS: (_RECOVERY_CREATION_CAPACITY_RESPONSE),
        },
    )
    async def create_recovery(
        payload: CreateRecoveryRequest,
        request: Request,
    ) -> RecoverySnapshot:
        identity = cast(ClientIdentity, request.state.demo_identity)
        cleanup_expired_recovery_creations(
            recovery_store,
            batch_size=25,
        )
        expire_pending_approvals(
            recovery_store,
            batch_size=25,
        )
        cleanup_terminal_recoveries(
            recovery_store,
            terminal_ttl=timedelta(seconds=runtime_settings.terminal_recovery_ttl_seconds),
            batch_size=25,
        )
        try:
            scenario = scenario_loader.get(payload.scenario_id)
        except ScenarioNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"code": "invalid_scenario"},
            ) from error
        if (
            payload.scenario_id.value == "api-quota"
            and payload.execution_mode is ExecutionMode.OPENAI_LIVE
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"code": "unsupported_scenario_mode"},
            )
        if payload.execution_mode not in {
            ExecutionMode.REPLAY_FIXTURE,
            ExecutionMode.SDK_STUB,
            ExecutionMode.OPENAI_LIVE,
        }:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Unsupported execution mode",
            )

        request_key = public_controls.recovery_creation_request_key(
            session_key=identity.session_key,
            client_request_id=payload.client_request_id,
        )
        request_fingerprint = _recovery_creation_fingerprint(payload)
        reserved_recovery_id = (
            replay_recovery_id(scenario)
            if payload.execution_mode is ExecutionMode.REPLAY_FIXTURE
            else str(uuid4())
        )
        claim = recovery_store.claim_recovery_creation(
            request_key=request_key,
            request_fingerprint=request_fingerprint,
            scenario_id=payload.scenario_id,
            execution_mode=payload.execution_mode,
            reserved_recovery_id=reserved_recovery_id,
            session_key=identity.session_key,
            ip_key=identity.ip_key,
            expires_at=identity.session_expires_at,
            max_per_session=(runtime_settings.max_recovery_creations_per_session),
            max_per_ip=runtime_settings.max_recovery_creations_per_ip,
            max_global=runtime_settings.max_recovery_creations_global,
        )

        if claim.disposition == "capacity":
            raise _RecoveryCreationPublicError("creation_capacity")
        request.state.recovery_id = claim.recovery_id
        if claim.disposition == "conflict":
            raise _RecoveryCreationPublicError("idempotency_conflict")
        if claim.disposition == "pending":
            raise _RecoveryCreationPublicError(
                "creation_pending",
                retry_after=_CREATION_PENDING_RETRY_SECONDS,
            )
        if claim.disposition == "unknown":
            raise _RecoveryCreationPublicError("creation_outcome_unknown")

        def authoritative_snapshot(ready_claim: RecoveryCreationClaim) -> RecoverySnapshot:
            if (
                ready_claim.scenario_id is not payload.scenario_id
                or ready_claim.execution_mode is not payload.execution_mode
            ):
                raise _RecoveryCreationPublicError("idempotency_conflict")
            if payload.execution_mode is not ExecutionMode.REPLAY_FIXTURE:
                try:
                    expire_pending_approvals(
                        recovery_store,
                        batch_size=1,
                        recovery_id=ready_claim.recovery_id,
                    )
                except Exception as error:
                    raise _RecoveryCreationPublicError("creation_outcome_unknown") from error
            try:
                if payload.execution_mode is ExecutionMode.REPLAY_FIXTURE:
                    snapshot = replay_engine.start(
                        payload.scenario_id,
                        execution_mode=payload.execution_mode,
                        session_key=identity.session_key,
                    )
                else:
                    snapshot = recovery_store.get_authoritative_recovery_creation(
                        recovery_id=ready_claim.recovery_id,
                        scenario_id=payload.scenario_id,
                        execution_mode=payload.execution_mode,
                        session_key=identity.session_key,
                    )
                if (
                    snapshot.recovery_id != ready_claim.recovery_id
                    or snapshot.scenario_id is not payload.scenario_id
                    or snapshot.execution_mode is not payload.execution_mode
                ):
                    raise RuntimeError("Recovery creation result changed identity")
                return snapshot
            except Exception as error:
                try:
                    if payload.execution_mode is ExecutionMode.REPLAY_FIXTURE:
                        recovery_store.mark_recovery_creation_unknown(
                            request_key=request_key,
                            request_fingerprint=request_fingerprint,
                        )
                    else:
                        resolution = recovery_store.mark_recovery_creation_unknown_or_reconcile(
                            request_key=request_key,
                            request_fingerprint=request_fingerprint,
                            session_key=identity.session_key,
                        )
                        if resolution.disposition == "ready":
                            return recovery_store.get_authoritative_recovery_creation(
                                recovery_id=resolution.recovery_id,
                                scenario_id=payload.scenario_id,
                                execution_mode=payload.execution_mode,
                                session_key=identity.session_key,
                            )
                except Exception:
                    pass
                raise _RecoveryCreationPublicError("creation_outcome_unknown") from error

        if claim.disposition == "ready":
            return authoritative_snapshot(claim)

        started = False
        live_admitted = False
        try:
            if payload.execution_mode is ExecutionMode.REPLAY_FIXTURE:
                recovery_store.mark_recovery_creation_started(
                    request_key=request_key,
                    request_fingerprint=request_fingerprint,
                )
                started = True
                created = replay_engine.start(
                    payload.scenario_id,
                    execution_mode=payload.execution_mode,
                    session_key=identity.session_key,
                )
            else:
                if (
                    payload.execution_mode is ExecutionMode.OPENAI_LIVE
                    and not runtime_settings.live_ready
                ):
                    recovery_store.abandon_reserved_recovery_creation(
                        request_key=request_key,
                        request_fingerprint=request_fingerprint,
                    )
                    raise LiveAdmissionError(LiveAdmissionCode.LIVE_UNAVAILABLE)
                if payload.execution_mode is ExecutionMode.OPENAI_LIVE:
                    try:
                        public_controls.admit_live(
                            recovery_id=claim.recovery_id,
                            ip_key=identity.ip_key,
                            session_key=identity.session_key,
                        )
                    except LiveAdmissionError:
                        recovery_store.abandon_reserved_recovery_creation(
                            request_key=request_key,
                            request_fingerprint=request_fingerprint,
                        )
                        raise
                    live_admitted = True
                recovery_store.mark_recovery_creation_started(
                    request_key=request_key,
                    request_fingerprint=request_fingerprint,
                )
                started = True
                if payload.execution_mode is ExecutionMode.OPENAI_LIVE:
                    async with public_controls.live_model_slot(claim.recovery_id):
                        pending = await recovery_orchestrator.start(
                            payload.scenario_id,
                            execution_mode=payload.execution_mode,
                            recovery_id=claim.recovery_id,
                            session_key=identity.session_key,
                        )
                else:
                    pending = await recovery_orchestrator.start(
                        payload.scenario_id,
                        execution_mode=payload.execution_mode,
                        recovery_id=claim.recovery_id,
                        session_key=identity.session_key,
                    )
                created = pending.recovery

            ready_claim = recovery_store.mark_recovery_creation_ready(
                request_key=request_key,
                request_fingerprint=request_fingerprint,
                recovery_id=created.recovery_id,
                session_key=identity.session_key,
            )
            if payload.execution_mode is ExecutionMode.REPLAY_FIXTURE:
                return created
            return authoritative_snapshot(ready_claim)
        except asyncio.CancelledError:
            if live_admitted:
                public_controls.release_live(claim.recovery_id)
            if started:
                try:
                    recovery_store.mark_recovery_creation_unknown_or_reconcile(
                        request_key=request_key,
                        request_fingerprint=request_fingerprint,
                        session_key=identity.session_key,
                    )
                except Exception:
                    pass
            raise
        except _RecoveryCreationPublicError:
            if live_admitted:
                public_controls.release_live(claim.recovery_id)
            raise
        except Exception as error:
            if live_admitted:
                public_controls.release_live(claim.recovery_id)
            if not started:
                raise
            try:
                resolution = recovery_store.mark_recovery_creation_unknown_or_reconcile(
                    request_key=request_key,
                    request_fingerprint=request_fingerprint,
                    session_key=identity.session_key,
                )
            except Exception:
                resolution = None
            if resolution is not None and resolution.disposition == "ready":
                return authoritative_snapshot(resolution)
            raise _RecoveryCreationPublicError("creation_outcome_unknown") from error

    @application.post(
        "/api/recoveries/{recovery_id}/decisions",
        response_model=ApprovalDecisionResponse,
        description=_PRIVATE_RECOVERY_DESCRIPTION,
        responses={
            **_PRIVATE_NOT_FOUND_RESPONSE,
            status.HTTP_409_CONFLICT: _DECISION_CONFLICT_RESPONSE,
            status.HTTP_413_CONTENT_TOO_LARGE: _REQUEST_BODY_TOO_LARGE_RESPONSE,
            status.HTTP_422_UNPROCESSABLE_CONTENT: _DECISION_UNPROCESSABLE_RESPONSE,
            status.HTTP_429_TOO_MANY_REQUESTS: _DECISION_CAPACITY_RESPONSE,
        },
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _inline_local_schema_definitions(
                            ApprovalDecisionRequest.model_json_schema(by_alias=True)
                        )
                    }
                },
            }
        },
    )
    async def approve_recovery(
        recovery_id: UUID,
        request: Request,
    ) -> ApprovalDecisionResponse:
        recovery_key = str(recovery_id)
        _require_recovery_access(request, recovery_store, recovery_key)
        request.state.recovery_id = recovery_key
        try:
            media_type = request.headers.get("content-type", "").partition(";")[0]
            if media_type.strip().lower() != "application/json":
                raise ValueError("Decision requires an application/json body")
            raw_payload = await request.json()
            payload = ApprovalDecisionRequest.model_validate(raw_payload)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "code": "decision_body_invalid",
                    "recoveryId": recovery_key,
                },
            ) from None
        try:
            expire_pending_approvals(
                recovery_store,
                batch_size=1,
                recovery_id=recovery_key,
            )
            if recovery_store.recovery_has_expiration_evidence(recovery_key):
                raise ApprovalDecisionError(
                    "remedy_expired",
                    recovery_key,
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                )
            try:
                recovery_before = recovery_store.get_recovery(recovery_key)
            except RecoveryNotFoundError:
                recovery_before = None
            if (
                recovery_before is not None
                and recovery_before.execution_mode is ExecutionMode.OPENAI_LIVE
                and not recovery_before.status.terminal
            ):
                try:
                    public_controls.guard_live_resume(recovery_key)
                    async with public_controls.live_model_slot(recovery_key):
                        response = await recovery_orchestrator.approve_decision(
                            recovery_key,
                            payload,
                        )
                except LiveAdmissionError as error:
                    if error.code is LiveAdmissionCode.LIVE_CAPACITY:
                        raise LiveDecisionCapacityError from None
                    raise
            else:
                response = await recovery_orchestrator.approve_decision(
                    recovery_key,
                    payload,
                )
            recovery = recovery_store.get_recovery(recovery_key)
            if recovery.execution_mode is ExecutionMode.OPENAI_LIVE and recovery.status.terminal:
                public_controls.release_live(recovery_key)
            return response
        except ApprovalDecisionError as error:
            if error.code == "remedy_expired":
                expire_pending_approvals(
                    recovery_store,
                    batch_size=1,
                    recovery_id=recovery_key,
                )
            raise HTTPException(
                status_code=error.status_code,
                detail=error.public_detail,
            ) from error
        except ResumeIncompatibleError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=error.public_detail,
            ) from error

    @application.post(
        "/api/recoveries/{recovery_id}/decisions/resume",
        response_model=DecisionResumeResponse,
        description=(
            f"{_PRIVATE_RECOVERY_DESCRIPTION} Explicitly continues only the existing "
            "durable decision claim; the empty request cannot create or alter consent."
        ),
        responses={
            **_PRIVATE_NOT_FOUND_RESPONSE,
            status.HTTP_409_CONFLICT: _DECISION_RESUME_CONFLICT_RESPONSE,
            status.HTTP_413_CONTENT_TOO_LARGE: _REQUEST_BODY_TOO_LARGE_RESPONSE,
            status.HTTP_422_UNPROCESSABLE_CONTENT: (_DECISION_RESUME_UNPROCESSABLE_RESPONSE),
            status.HTTP_429_TOO_MANY_REQUESTS: _DECISION_CAPACITY_RESPONSE,
        },
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": False,
                            "maxProperties": 0,
                        }
                    }
                },
            }
        },
    )
    async def resume_recovery_decision(
        recovery_id: UUID,
        request: Request,
    ) -> DecisionResumeResponse:
        recovery_key = str(recovery_id)
        _require_recovery_access(request, recovery_store, recovery_key)
        request.state.recovery_id = recovery_key
        try:
            try:
                media_type = request.headers.get("content-type", "").partition(";")[0]
                if media_type.strip().lower() != "application/json":
                    raise ValueError("Resume requires an application/json body")
                raw_payload = await request.json()
                DecisionResumeRequest.model_validate(raw_payload)
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail={
                        "code": "decision_resume_body_invalid",
                        "recoveryId": recovery_key,
                    },
                ) from None

            expire_pending_approvals(
                recovery_store,
                batch_size=1,
                recovery_id=recovery_key,
            )
            if recovery_store.recovery_has_expiration_evidence(recovery_key):
                raise ApprovalDecisionError(
                    "remedy_expired",
                    recovery_key,
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                )
            claim = recovery_orchestrator.preflight_decision_resume(recovery_key)
            if claim.response is not None:
                response = claim.response
            else:
                recovery_before = recovery_store.get_recovery(recovery_key)
                if recovery_before.execution_mode is ExecutionMode.OPENAI_LIVE:
                    try:
                        public_controls.guard_live_resume(recovery_key)
                        async with public_controls.live_model_slot(recovery_key):
                            response = await recovery_orchestrator.resume_decision(claim)
                    except LiveAdmissionError as error:
                        if error.code is LiveAdmissionCode.LIVE_CAPACITY:
                            raise LiveDecisionCapacityError from None
                        raise
                else:
                    response = await recovery_orchestrator.resume_decision(claim)
            recovery = recovery_store.get_recovery(recovery_key)
            if recovery.execution_mode is ExecutionMode.OPENAI_LIVE and recovery.status.terminal:
                public_controls.release_live(recovery_key)
            return DecisionResumeResponse.from_decision(
                response,
                remedy_digest=claim.request.remedy_digest,
            )
        except ApprovalDecisionError as error:
            if error.code == "remedy_expired":
                expire_pending_approvals(
                    recovery_store,
                    batch_size=1,
                    recovery_id=recovery_key,
                )
            raise HTTPException(
                status_code=error.status_code,
                detail=error.public_detail,
            ) from error
        except ResumeIncompatibleError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=error.public_detail,
            ) from error

    @application.get(
        "/api/recoveries/{recovery_id}",
        response_model=RecoverySnapshot,
        description=_PRIVATE_RECOVERY_DESCRIPTION,
        responses=_PRIVATE_RECOVERY_READ_RESPONSES,
    )
    def get_recovery(recovery_id: UUID, request: Request) -> RecoverySnapshot:
        recovery_key = str(recovery_id)
        _require_recovery_access(request, recovery_store, recovery_key)
        session_key = cast(ClientIdentity, request.state.demo_identity).session_key
        try:
            return recovery_store.get_public_recovery(
                recovery_key,
                session_key=session_key,
                replay_scenarios=public_replay_scenarios,
            )
        except RecoveryNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Not found"
            ) from error

    @application.get(
        "/api/recoveries/{recovery_id}/events",
        status_code=status.HTTP_200_OK,
        response_class=_LeaseReleasingStreamingResponse,
        description=_EVENT_STREAM_DESCRIPTION,
        responses=_EVENT_STREAM_RESPONSES,
        openapi_extra=_EVENT_STREAM_OPENAPI_EXTRA,
    )
    async def recovery_events(
        recovery_id: UUID,
        request: Request,
    ) -> Response:
        recovery_key = str(recovery_id)
        _require_recovery_access(request, recovery_store, recovery_key)
        identity = cast(ClientIdentity, request.state.demo_identity)
        session_key = identity.session_key
        try:
            cursor_values = request.headers.getlist("last-event-id")
            if len(cursor_values) > 1:
                raise ValueError("Event cursor header must occur at most once")
            cursor = _parse_event_cursor(cursor_values[0] if cursor_values else None)
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=_INVALID_EVENT_CURSOR_DETAIL,
            ) from error
        try:
            initial_batch = recovery_store.read_initial_public_event_batch(
                recovery_key,
                after_seq=cursor,
                session_key=session_key,
                replay_scenarios=public_replay_scenarios,
            )
        except RecoveryNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Not found"
            ) from error

        lease = event_stream_admission.try_acquire(recovery_key)
        if lease is None:
            return Response(
                content=encode_stream_capacity_event(
                    request_id=_request_id(request),
                    retry_seconds=event_stream_admission.retry_seconds,
                ),
                media_type="text/event-stream",
                headers=_EVENT_STREAM_HEADERS,
            )

        return _build_admitted_event_stream_response(
            lease_event_stream(
                stream_recovery_events(
                    recovery_store,
                    recovery_key,
                    after_seq=cursor,
                    is_disconnected=request.is_disconnected,
                    initial_batch=initial_batch,
                    public_session_key=session_key,
                    public_replay_scenarios=public_replay_scenarios,
                    public_session_expires_at=identity.session_expires_at,
                ),
                lease,
            ),
            lease,
            session_expires_at=identity.session_expires_at,
        )

    @application.get(
        "/api/recoveries/{recovery_id}/receipt",
        response_model=RecoveryReceipt,
        description=_PRIVATE_RECOVERY_DESCRIPTION,
        responses=_PRIVATE_RECOVERY_READ_RESPONSES,
    )
    def get_receipt(recovery_id: UUID, request: Request) -> RecoveryReceipt:
        recovery_key = str(recovery_id)
        _require_recovery_access(request, recovery_store, recovery_key)
        session_key = cast(ClientIdentity, request.state.demo_identity).session_key
        try:
            return recovery_store.get_public_receipt(
                recovery_key,
                session_key=session_key,
                replay_scenarios=public_replay_scenarios,
            )
        except RecoveryNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Not found"
            ) from error

    @application.post(
        "/api/demo/reset",
        response_model=DemoResetResponse,
        responses={
            status.HTTP_409_CONFLICT: _DEMO_RESET_CONFLICT_RESPONSE,
        },
    )
    def reset_demo(request: Request) -> DemoResetResponse:
        if not runtime_settings.demo_reset_enabled:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden",
            )
        identity = cast(ClientIdentity, request.state.demo_identity)
        recovery_store.reset(identity.session_key)
        return DemoResetResponse(reset=True)

    if configured_static_dir is not None:
        assets_dir = configured_static_dir / "assets"
        if assets_dir.is_dir():
            application.mount(
                "/assets",
                StaticFiles(directory=assets_dir, check_dir=True),
                name="production-assets",
            )

        @application.api_route(
            "/{spa_path:path}",
            methods=["GET", "HEAD"],
            include_in_schema=False,
        )
        def spa_fallback(spa_path: str) -> Response:
            if static_index is None or not static_index.is_file():
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Not found",
                )
            if not _is_route_like_spa_path(spa_path):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Not found",
                )
            return FileResponse(static_index, media_type="text/html")

    return application


app = create_app(static_dir=_PRODUCTION_STATIC_DIR)
