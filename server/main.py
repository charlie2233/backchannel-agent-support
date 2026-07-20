"""FastAPI entry point for truthful recovery persistence and streaming."""

import asyncio
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any, cast
from uuid import UUID, uuid4

from agents.models.interface import ModelProvider
from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import Receive, Scope, Send

from server.cleanup import cleanup_terminal_recoveries, expire_pending_approvals
from server.config import RuntimeSettings
from server.controls import (
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
    UnsupportedOrchestrationError,
)
from server.providers.hotel_simulator import HotelSimulator
from server.providers.quota_simulator import QuotaSimulator
from server.replay.engine import ReplayEngine, UnsupportedExecutionModeError
from server.replay.loader import ScenarioLoader, ScenarioNotFoundError
from server.store import ApprovalDecisionError, RecoveryNotFoundError, SQLiteStore

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
            "created_at",
            "updated_at",
        }
    ),
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
    "usage_ledger": frozenset(
        {"id", "recovery_id", "category", "amount", "recorded_at"}
    ),
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
}
_SPA_SEGMENT = re.compile(r"^[A-Za-z0-9_-]+$")
_RESERVED_ROUTE_ROOTS = frozenset(
    {"api", "assets", "docs", "health", "openapi.json", "readyz", "redoc"}
)
_PRIVATE_RECOVERY_DESCRIPTION = (
    "Requires the signed opaque demo session issued when the recovery was created. "
    "Missing, expired, tampered, unrelated, and unknown sessions receive the same "
    "generic not-found response."
)
_PRIVATE_NOT_FOUND_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {"description": "Recovery not found."}
}
_INVALID_REQUEST_MESSAGE = "The request did not match the public API contract."
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
_EVENT_STREAM_DESCRIPTION = (
    f"{_PRIVATE_RECOVERY_DESCRIPTION} Streams are admitted by bounded, "
    "process-local capacity. At capacity, the endpoint returns a finite "
    "stream.capacity control event with a native EventSource retry interval."
)
_EVENT_STREAM_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
}


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
        return {
            key: expand(item)
            for key, item in value.items()
            if key != "$defs"
        }

    expanded = expand(schema)
    if not isinstance(expanded, dict):
        raise ValueError("Pydantic schema must expand to an object")
    return expanded


class _LeaseReleasingStreamingResponse(StreamingResponse):
    """Release an admitted stream even when the ASGI send path fails."""

    media_type = "text/event-stream"

    def __init__(
        self,
        content: AsyncIterator[str],
        *,
        lease: EventStreamLease,
        headers: Mapping[str, str],
    ) -> None:
        self._event_stream_lease = lease
        super().__init__(content, media_type=self.media_type, headers=headers)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._event_stream_lease.release()


def _build_admitted_event_stream_response(
    content: AsyncIterator[str],
    lease: EventStreamLease,
) -> StreamingResponse:
    """Construct a leased response without leaking admission on failure."""

    try:
        return _LeaseReleasingStreamingResponse(
            content,
            lease=lease,
            headers=_EVENT_STREAM_HEADERS,
        )
    except BaseException:
        lease.release()
        raise


def _store_schema_is_ready(store: SQLiteStore) -> bool:
    """Probe the configured SQLite database without exposing failure details."""

    try:
        with store._connect() as connection:
            integrity = connection.execute("PRAGMA quick_check(1)").fetchone()
            if integrity is None or integrity[0] != "ok":
                return False
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                return False
            for table_name, required_columns in _READY_SCHEMA_COLUMNS.items():
                actual_columns = {
                    str(row[1])
                    for row in connection.execute(
                        f'PRAGMA table_info("{table_name}")'
                    ).fetchall()
                }
                if not required_columns.issubset(actual_columns):
                    return False
            access_columns = [
                (str(row[1]), int(row[5]))
                for row in connection.execute(
                    'PRAGMA table_info("recovery_access")'
                ).fetchall()
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
    except Exception:
        return False
    return True


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
) -> FastAPI:
    install_server_log_safety()
    runtime_settings = settings or RuntimeSettings.from_environment()
    recovery_store = store or SQLiteStore(runtime_settings.database_path)
    scenario_loader = ScenarioLoader()
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
        configured_static_dir / "index.html"
        if configured_static_dir is not None
        else None
    )
    terminal_ttl = timedelta(
        seconds=runtime_settings.terminal_recovery_ttl_seconds
    )

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
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
            message="The request body is too large.",
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
        database_ready = _store_schema_is_ready(recovery_store)
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
    )
    async def create_recovery(
        payload: CreateRecoveryRequest,
        request: Request,
    ) -> RecoverySnapshot:
        identity = cast(ClientIdentity, request.state.demo_identity)
        expire_pending_approvals(
            recovery_store,
            batch_size=25,
        )
        cleanup_terminal_recoveries(
            recovery_store,
            terminal_ttl=timedelta(
                seconds=runtime_settings.terminal_recovery_ttl_seconds
            ),
            batch_size=25,
        )
        if payload.execution_mode is ExecutionMode.REPLAY_FIXTURE:
            try:
                replay = replay_engine.start(
                    payload.scenario_id,
                    execution_mode=payload.execution_mode,
                    session_key=identity.session_key,
                )
                if not recovery_store.recovery_is_accessible(
                    replay.recovery_id,
                    identity.session_key,
                ):
                    raise RuntimeError("Recovery access binding was not persisted")
                return replay
            except (ScenarioNotFoundError, UnsupportedExecutionModeError) as error:
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
        if (
            payload.execution_mode is ExecutionMode.OPENAI_LIVE
            and not runtime_settings.live_ready
        ):
            raise LiveAdmissionError(LiveAdmissionCode.LIVE_UNAVAILABLE)
        if payload.execution_mode not in {
            ExecutionMode.SDK_STUB,
            ExecutionMode.OPENAI_LIVE,
        }:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Unsupported execution mode",
            )
        try:
            if payload.execution_mode is ExecutionMode.OPENAI_LIVE:
                recovery_id = str(uuid4())
                request.state.recovery_id = recovery_id
                public_controls.admit_live(
                    recovery_id=recovery_id,
                    ip_key=identity.ip_key,
                    session_key=identity.session_key,
                )
                try:
                    async with public_controls.live_model_slot(recovery_id):
                        pending = await recovery_orchestrator.start(
                            payload.scenario_id,
                            execution_mode=payload.execution_mode,
                            recovery_id=recovery_id,
                            session_key=identity.session_key,
                        )
                    if not recovery_store.recovery_is_accessible(
                        pending.recovery.recovery_id,
                        identity.session_key,
                    ):
                        raise RuntimeError("Recovery access binding was not persisted")
                except Exception:
                    public_controls.release_live(recovery_id)
                    raise
            else:
                pending = await recovery_orchestrator.start(
                    payload.scenario_id,
                    execution_mode=payload.execution_mode,
                    session_key=identity.session_key,
                )
                if not recovery_store.recovery_is_accessible(
                    pending.recovery.recovery_id,
                    identity.session_key,
                ):
                    raise RuntimeError("Recovery access binding was not persisted")
            return pending.recovery
        except UnsupportedOrchestrationError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"code": "orchestration_unavailable"},
            ) from error

    @application.post(
        "/api/recoveries/{recovery_id}/decisions",
        response_model=ApprovalDecisionResponse,
        description=_PRIVATE_RECOVERY_DESCRIPTION,
        responses={
            **_PRIVATE_NOT_FOUND_RESPONSE,
            status.HTTP_422_UNPROCESSABLE_CONTENT: _DECISION_UNPROCESSABLE_RESPONSE,
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
            if (
                recovery.execution_mode is ExecutionMode.OPENAI_LIVE
                and recovery.status.terminal
            ):
                public_controls.release_live(recovery_key)
            return response
        except ApprovalDecisionError as error:
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
        responses=_PRIVATE_NOT_FOUND_RESPONSE,
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
            if (
                recovery.execution_mode is ExecutionMode.OPENAI_LIVE
                and recovery.status.terminal
            ):
                public_controls.release_live(recovery_key)
            return DecisionResumeResponse.from_decision(
                response,
                remedy_digest=claim.request.remedy_digest,
            )
        except ApprovalDecisionError as error:
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
        responses=_PRIVATE_NOT_FOUND_RESPONSE,
    )
    def get_recovery(recovery_id: UUID, request: Request) -> RecoverySnapshot:
        recovery_key = str(recovery_id)
        _require_recovery_access(request, recovery_store, recovery_key)
        expire_pending_approvals(
            recovery_store,
            batch_size=1,
            recovery_id=recovery_key,
        )
        try:
            return recovery_store.get_recovery(recovery_key)
        except RecoveryNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Not found"
            ) from error

    @application.get(
        "/api/recoveries/{recovery_id}/events",
        status_code=status.HTTP_200_OK,
        response_class=_LeaseReleasingStreamingResponse,
        description=_EVENT_STREAM_DESCRIPTION,
        responses=_PRIVATE_NOT_FOUND_RESPONSE,
    )
    async def recovery_events(
        recovery_id: UUID,
        request: Request,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> Response:
        recovery_key = str(recovery_id)
        _require_recovery_access(request, recovery_store, recovery_key)
        expire_pending_approvals(
            recovery_store,
            batch_size=1,
            recovery_id=recovery_key,
        )
        cursor = 0
        if last_event_id is not None:
            try:
                cursor = int(last_event_id)
            except ValueError as error:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Last-Event-ID must be a non-negative integer",
                ) from error
            if cursor < 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Last-Event-ID must be a non-negative integer",
                )
        try:
            recovery_store.get_recovery(recovery_key)
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
                ),
                lease,
            ),
            lease,
        )

    @application.get(
        "/api/recoveries/{recovery_id}/receipt",
        response_model=RecoveryReceipt,
        description=_PRIVATE_RECOVERY_DESCRIPTION,
        responses=_PRIVATE_NOT_FOUND_RESPONSE,
    )
    def get_receipt(recovery_id: UUID, request: Request) -> RecoveryReceipt:
        recovery_key = str(recovery_id)
        _require_recovery_access(request, recovery_store, recovery_key)
        expire_pending_approvals(
            recovery_store,
            batch_size=1,
            recovery_id=recovery_key,
        )
        try:
            return recovery_store.get_receipt(recovery_key)
        except RecoveryNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Not found"
            ) from error

    @application.post("/api/demo/reset", response_model=DemoResetResponse)
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
