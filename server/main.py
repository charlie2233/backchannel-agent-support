"""FastAPI entry point for truthful recovery persistence and streaming."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Annotated, cast
from uuid import UUID, uuid4

from agents.models.interface import ModelProvider
from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from server.cleanup import cleanup_terminal_recoveries
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
from server.events import stream_recovery_events
from server.logging import get_safe_logger, install_server_log_safety, log_safe_exception
from server.models import (
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    CreateRecoveryRequest,
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
from server.replay.engine import ReplayEngine, UnsupportedExecutionModeError
from server.replay.loader import ScenarioLoader, ScenarioNotFoundError
from server.store import ApprovalDecisionError, RecoveryNotFoundError, SQLiteStore

logger = get_safe_logger(__name__)


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
    orchestrator: RecoveryOrchestrator | None = None,
    model_provider: ModelProvider | None = None,
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
            live_ready=runtime_settings.live_ready,
            model_provider=model_provider,
        )
    public_controls = PublicDemoControls(recovery_store, runtime_settings)
    terminal_ttl = timedelta(
        seconds=runtime_settings.terminal_recovery_ttl_seconds
    )

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
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
    application.state.runtime_settings = runtime_settings
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
            message="The request did not match the public API contract.",
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
    def ready() -> ReadinessResponse:
        return ReadinessResponse(status="ready")

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
        cleanup_terminal_recoveries(
            recovery_store,
            terminal_ttl=timedelta(
                seconds=runtime_settings.terminal_recovery_ttl_seconds
            ),
            batch_size=25,
        )
        if payload.execution_mode is ExecutionMode.REPLAY_FIXTURE:
            try:
                return replay_engine.start(
                    payload.scenario_id,
                    execution_mode=payload.execution_mode,
                )
            except (ScenarioNotFoundError, UnsupportedExecutionModeError) as error:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail={"code": "invalid_scenario"},
                ) from error
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
                identity = cast(ClientIdentity, request.state.demo_identity)
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
                        )
                except Exception:
                    public_controls.release_live(recovery_id)
                    raise
            else:
                pending = await recovery_orchestrator.start(
                    payload.scenario_id,
                    execution_mode=payload.execution_mode,
                )
            return pending.recovery
        except UnsupportedOrchestrationError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"code": "orchestration_unavailable"},
            ) from error

    @application.post(
        "/api/recoveries/{recovery_id}/decisions",
        response_model=ApprovalDecisionResponse,
    )
    async def approve_recovery(
        recovery_id: UUID,
        payload: ApprovalDecisionRequest,
        request: Request,
    ) -> ApprovalDecisionResponse:
        recovery_key = str(recovery_id)
        request.state.recovery_id = recovery_key
        try:
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

    @application.get(
        "/api/recoveries/{recovery_id}", response_model=RecoverySnapshot
    )
    def get_recovery(recovery_id: UUID) -> RecoverySnapshot:
        try:
            return recovery_store.get_recovery(str(recovery_id))
        except RecoveryNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Not found"
            ) from error

    @application.get("/api/recoveries/{recovery_id}/events")
    async def recovery_events(
        recovery_id: UUID,
        request: Request,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> Response:
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
        recovery_key = str(recovery_id)
        try:
            recovery_store.get_recovery(recovery_key)
        except RecoveryNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Not found"
            ) from error

        return StreamingResponse(
            stream_recovery_events(
                recovery_store,
                recovery_key,
                after_seq=cursor,
                is_disconnected=request.is_disconnected,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @application.get(
        "/api/recoveries/{recovery_id}/receipt", response_model=RecoveryReceipt
    )
    def get_receipt(recovery_id: UUID) -> RecoveryReceipt:
        try:
            return recovery_store.get_receipt(str(recovery_id))
        except RecoveryNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Not found"
            ) from error

    @application.post("/api/demo/reset", response_model=DemoResetResponse)
    def reset_demo() -> DemoResetResponse:
        if not runtime_settings.demo_reset_enabled:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden",
            )
        recovery_store.reset()
        return DemoResetResponse(reset=True)

    return application


app = create_app()
