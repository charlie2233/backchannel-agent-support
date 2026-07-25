"""FastAPI entry point with a bounded, identity-safe public demo boundary."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, NoReturn
from uuid import UUID

from fastapi import FastAPI, Header, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from openai import AsyncOpenAI
from starlette.exceptions import HTTPException as StarletteHTTPException

from server.agents.live_models import (
    LiveModelRequestError,
    ResponseMetadataRecorder,
    SafeOpenAIResponsesProvider,
)
from server.cleanup import RecoveryCleanupService
from server.config import RuntimeSettings
from server.controls import (
    LiveConcurrencyGate,
    LiveConcurrencyLimitError,
    PublicApiException,
    PublicBoundaryMiddleware,
    PublicCreationAdmissionError,
    PublicIdentityHasher,
    PublicLiveAdmissionError,
    identity_from_scope,
    optional_identity_from_scope,
    public_error_detail,
    recovery_id_from_scope,
    request_id_from_scope,
)
from server.events import stream_recovery_events
from server.logging import log_public_event
from server.models import (
    ApprovalDecisionRequest,
    CreateRecoveryRequest,
    DecisionResponse,
    DemoResetResponse,
    ExecutionMode,
    HealthResponse,
    ProviderBoundary,
    PublicErrorResponse,
    ReadinessResponse,
    RecoveryReceipt,
    RecoverySnapshot,
    RuntimeBackend,
    ScenarioId,
    ScenarioResponse,
)
from server.orchestrator import (
    LiveOperationTimeoutError,
    LiveUnavailableError,
    RecoveryOrchestrator,
    ResumeIncompatibleError,
    UnsupportedOrchestrationError,
)
from server.providers.hotel_simulator import HotelSimulator
from server.providers.quota_simulator import QuotaSimulator
from server.replay.engine import ReplayEngine, UnsupportedExecutionModeError
from server.replay.loader import ScenarioLoader, ScenarioNotFoundError
from server.sse_admission import (
    LeasedStreamingResponse,
    SSEAdmissionGate,
    StreamCapacityReachedError,
)
from server.static import FrontendBundle
from server.store import ApprovalDecisionError, RecoveryNotFoundError, SQLiteStore


def _raise_public(
    *,
    status_code: int,
    code: str,
    recovery_id: str | None = None,
    retry_after_seconds: int | None = None,
    replay_offer: bool = False,
) -> NoReturn:
    raise PublicApiException(
        status_code=status_code,
        code=code,
        recovery_id=recovery_id,
        retry_after_seconds=retry_after_seconds,
        replay_offer=replay_offer,
    )


def _public_error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    recovery_id: str | None = None,
    retry_after_seconds: int | None = None,
    replay_offer: bool = False,
) -> JSONResponse:
    request_id = request_id_from_scope(request.scope)
    resolved_recovery_id = recovery_id or recovery_id_from_scope(request.scope)
    safe_detail = public_error_detail(
        code=code,
        request_id=request_id,
        recovery_id=resolved_recovery_id,
        retry_after_seconds=retry_after_seconds,
        replay_offer=replay_offer,
    )
    safe_code = str(safe_detail["code"])
    log_public_event(
        event="request_rejected",
        request_id=request_id,
        code=safe_code,
        status_code=status_code,
        recovery_id=resolved_recovery_id,
    )
    headers = {"Cache-Control": "no-store"}
    if retry_after_seconds is not None:
        headers["Retry-After"] = str(retry_after_seconds)
    return JSONResponse(
        status_code=status_code,
        content={"error": safe_detail},
        headers=headers,
    )


def _decision_error_code(code: str) -> str:
    return {
        "constraint_denied": "constraint_denied",
        "remedy_expired": "remedy_expired",
        "resume_owner_lost": "decision_in_progress",
        "model_metadata_conflict": "model_metadata_conflict",
    }.get(code, code)


def _raise_live_failure(
    error: Exception,
    *,
    recovery_id: str | None = None,
    replay_offer: bool = False,
) -> NoReturn:
    if isinstance(error, LiveOperationTimeoutError) or (
        isinstance(error, LiveModelRequestError) and error.code == "timeout"
    ):
        _raise_public(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            code="live_timeout",
            recovery_id=recovery_id,
            replay_offer=replay_offer,
        )
    _raise_public(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        code="live_unavailable",
        recovery_id=recovery_id,
        replay_offer=replay_offer,
    )


def create_app(
    settings: RuntimeSettings | None = None,
    *,
    store: SQLiteStore | None = None,
    hotel_provider: HotelSimulator | None = None,
    quota_provider: QuotaSimulator | None = None,
    orchestrator: RecoveryOrchestrator | None = None,
) -> FastAPI:
    runtime_settings = settings or RuntimeSettings.from_environment()
    recovery_store = store or SQLiteStore(runtime_settings.database_path)
    frontend_bundle = (
        FrontendBundle(runtime_settings.frontend_dist_path)
        if runtime_settings.frontend_dist_path is not None
        else None
    )
    scenario_loader = ScenarioLoader()
    replay_engine = ReplayEngine(recovery_store, scenario_loader)
    live_gate = LiveConcurrencyGate(
        max_concurrent=runtime_settings.live_max_concurrent
    )
    sse_gate = SSEAdmissionGate(
        max_concurrent=runtime_settings.sse_max_concurrent,
        max_per_session=runtime_settings.sse_max_per_session,
        max_per_recovery=runtime_settings.sse_max_per_recovery,
    )
    recovery_orchestrator = orchestrator
    live_client: AsyncOpenAI | None = None
    if recovery_orchestrator is None:
        provider = hotel_provider or HotelSimulator(store=recovery_store)
        live_client = (
            AsyncOpenAI(
                timeout=runtime_settings.live_operation_timeout.total_seconds(),
                max_retries=0,
            )
            if runtime_settings.live_ready
            else None
        )

        def live_provider_factory(
            recorder: ResponseMetadataRecorder,
        ) -> SafeOpenAIResponsesProvider:
            if live_client is None:
                raise LiveUnavailableError
            return SafeOpenAIResponsesProvider(
                client=live_client,
                recorder=recorder,
            )

        recovery_orchestrator = RecoveryOrchestrator(
            store=recovery_store,
            hotel_provider=provider,
            quota_provider=quota_provider,
            live_ready=runtime_settings.live_ready,
            live_model_provider_factory=(
                live_provider_factory if runtime_settings.live_ready else None
            ),
            live_operation_timeout=runtime_settings.live_operation_timeout,
        )

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        cleanup_service = RecoveryCleanupService(
            store=recovery_store,
            ttl=runtime_settings.recovery_ttl,
            interval=runtime_settings.cleanup_interval,
            batch_size=runtime_settings.cleanup_batch_size,
            creation_usage_retention=runtime_settings.creation_usage_retention,
        )
        _application.state.cleanup_service = cleanup_service
        try:
            await recovery_orchestrator.startup()
            await cleanup_service.startup()
            yield
        finally:
            try:
                await cleanup_service.shutdown()
            finally:
                try:
                    await recovery_orchestrator.shutdown()
                finally:
                    if live_client is not None:
                        await live_client.close()

    application = FastAPI(
        title="Backchannel API",
        version="0.3.0",
        lifespan=lifespan,
        docs_url=None if runtime_settings.deployed else "/docs",
        redoc_url=None,
        responses={
            422: {
                "model": PublicErrorResponse,
                "description": "The request was rejected with a public error envelope.",
            }
        },
    )
    application.state.recovery_store = recovery_store
    application.state.recovery_orchestrator = recovery_orchestrator
    application.state.live_gate = live_gate
    application.state.sse_gate = sse_gate
    application.state.frontend_bundle = frontend_bundle
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(runtime_settings.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Accept", "Content-Type", "Last-Event-ID"],
    )
    application.add_middleware(
        PublicBoundaryMiddleware,
        max_body_bytes=runtime_settings.max_request_body_bytes,
        identity_hasher=PublicIdentityHasher(runtime_settings.identity_hmac_secret),
        session_ttl_seconds=int(runtime_settings.demo_session_ttl.total_seconds()),
        trusted_proxy_cidrs=runtime_settings.trusted_proxy_cidrs,
        deployed=runtime_settings.deployed,
        allowed_origins=runtime_settings.cors_origins,
    )

    @application.exception_handler(PublicApiException)
    async def public_api_exception_handler(
        request: Request,
        error: PublicApiException,
    ) -> JSONResponse:
        return _public_error_response(
            request,
            status_code=error.status_code,
            code=error.code,
            recovery_id=error.recovery_id,
            retry_after_seconds=error.retry_after_seconds,
            replay_offer=error.replay_offer,
        )

    @application.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request,
        _error: RequestValidationError,
    ) -> JSONResponse:
        return _public_error_response(
            request,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="invalid_request",
        )

    @application.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request,
        error: StarletteHTTPException,
    ) -> JSONResponse:
        if error.status_code == status.HTTP_404_NOT_FOUND:
            code = "not_found"
        elif error.status_code == status.HTTP_405_METHOD_NOT_ALLOWED:
            code = "method_not_allowed"
        else:
            code = "invalid_request"
        return _public_error_response(
            request,
            status_code=error.status_code,
            code=code,
        )

    @application.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request,
        _error: Exception,
    ) -> JSONResponse:
        return _public_error_response(
            request,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="internal_error",
        )

    @application.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        backend = (
            RuntimeBackend.OPENAI if runtime_settings.live_ready else RuntimeBackend.STUB
        )
        return HealthResponse(
            backend=backend,
            liveReady=runtime_settings.live_ready,
            sdkStubReady=runtime_settings.sdk_stub_ready,
            providerBoundary=ProviderBoundary.DEMO_ADAPTER_ONLY,
        )

    @application.get("/readyz", response_model=ReadinessResponse)
    def ready() -> ReadinessResponse:
        if not recovery_store.is_ready() or (
            (runtime_settings.deployed or frontend_bundle is not None)
            and (frontend_bundle is None or not frontend_bundle.is_ready())
        ):
            _raise_public(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="internal_error",
            )
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
        responses={
            429: {
                "model": PublicErrorResponse,
                "description": (
                    "The request exceeded the public creation budget or a live-only "
                    "capacity, cooldown, or daily-budget admission limit."
                ),
                "headers": {
                    "Retry-After": {
                        "description": (
                            "Retry delay in seconds when provided. Creation-budget "
                            "exhaustion uses 1..86400 seconds until the next UTC "
                            "midnight; live-only outcomes may use a longer or otherwise "
                            "different delay."
                        ),
                        "schema": {
                            "type": "integer",
                            "minimum": 1,
                        },
                    }
                },
            },
            504: {
                "model": PublicErrorResponse,
                "description": (
                    "Live processing exceeded the configured server deadline."
                ),
            },
        },
    )
    async def create_recovery(
        request: Request,
        payload: CreateRecoveryRequest,
    ) -> RecoverySnapshot:
        identity = identity_from_scope(request.scope)
        try:
            scenario_loader.get(payload.scenario_id)
        except ScenarioNotFoundError:
            _raise_public(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                code="invalid_request",
            )
        if (
            payload.execution_mode is ExecutionMode.SDK_STUB
            and not runtime_settings.sdk_stub_ready
        ):
            _raise_public(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                code="invalid_request",
            )
        if payload.execution_mode is ExecutionMode.OPENAI_LIVE:
            if payload.scenario_id is not ScenarioId.HOTEL:
                _raise_public(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    code="invalid_request",
                )
            if not runtime_settings.live_ready:
                _raise_public(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    code="live_unavailable",
                    replay_offer=True,
                )
        try:
            recovery_store.claim_public_creation_admission(
                session_hash=identity.session_hash,
                ip_hash=identity.ip_hash,
                session_daily_budget=(
                    runtime_settings.creation_session_daily_budget
                ),
                ip_daily_budget=runtime_settings.creation_ip_daily_budget,
                global_daily_budget=runtime_settings.creation_global_daily_budget,
            )
        except PublicCreationAdmissionError as error:
            _raise_public(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                code=error.code,
                retry_after_seconds=error.retry_after_seconds,
            )
        if payload.execution_mode is ExecutionMode.REPLAY_FIXTURE:
            try:
                return replay_engine.start(
                    payload.scenario_id,
                    execution_mode=payload.execution_mode,
                    session_hash=identity.session_hash,
                )
            except (ScenarioNotFoundError, UnsupportedExecutionModeError):
                _raise_public(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    code="invalid_request",
                )
        if (
            payload.scenario_id is ScenarioId.API_QUOTA
            and payload.execution_mode is ExecutionMode.SDK_STUB
        ):
            return await recovery_orchestrator.run_quota_stub(
                session_hash=identity.session_hash,
            )
        if payload.execution_mode is ExecutionMode.OPENAI_LIVE:
            try:
                async with live_gate.slot():
                    recovery_store.claim_public_live_admission(
                        session_hash=identity.session_hash,
                        ip_hash=identity.ip_hash,
                        cooldown=runtime_settings.live_cooldown,
                        daily_budget=runtime_settings.live_daily_budget,
                        session_expires_at=identity.session_expires_at,
                    )
                    pending = await recovery_orchestrator.start(
                        payload.scenario_id,
                        execution_mode=payload.execution_mode,
                        session_hash=identity.session_hash,
                    )
                    return pending.recovery
            except LiveConcurrencyLimitError:
                _raise_public(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    code="live_capacity_reached",
                    retry_after_seconds=1,
                    replay_offer=True,
                )
            except PublicLiveAdmissionError as error:
                _raise_public(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    code=error.code,
                    retry_after_seconds=error.retry_after_seconds,
                    replay_offer=True,
                )
            except (
                LiveOperationTimeoutError,
                LiveUnavailableError,
                LiveModelRequestError,
            ) as error:
                _raise_live_failure(error, replay_offer=True)
        try:
            pending = await recovery_orchestrator.start(
                payload.scenario_id,
                execution_mode=payload.execution_mode,
                session_hash=identity.session_hash,
            )
            return pending.recovery
        except UnsupportedOrchestrationError:
            _raise_public(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                code="invalid_request",
            )
        except LiveUnavailableError:
            _raise_public(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="live_unavailable",
                replay_offer=True,
            )

    @application.post(
        "/api/recoveries/{recovery_id}/decisions",
        response_model=DecisionResponse,
        responses={
            504: {
                "model": PublicErrorResponse,
                "description": (
                    "Live decision processing exceeded the configured server deadline."
                ),
            }
        },
    )
    async def decide_recovery(
        recovery_id: UUID,
        request: Request,
        payload: ApprovalDecisionRequest,
    ) -> DecisionResponse:
        recovery_key = str(recovery_id)
        identity = optional_identity_from_scope(request.scope)
        if identity is None:
            _raise_public(
                status_code=status.HTTP_404_NOT_FOUND,
                code="not_found",
                recovery_id=recovery_key,
            )
        try:
            claim = recovery_store.claim_decision_for_session(
                recovery_key,
                payload,
                session_hash=identity.session_hash,
            )
        except RecoveryNotFoundError:
            _raise_public(
                status_code=status.HTTP_404_NOT_FOUND,
                code="not_found",
                recovery_id=recovery_key,
            )
        except ApprovalDecisionError as error:
            _raise_public(
                status_code=error.status_code,
                code=_decision_error_code(error.code),
                recovery_id=recovery_key,
            )
        try:
            if claim.response is not None:
                return await recovery_orchestrator.decide(
                    recovery_key,
                    payload,
                    session_hash=identity.session_hash,
                    claimed_decision=claim,
                )
            snapshot = recovery_store.get_recovery(recovery_key)
            if snapshot.execution_mode is ExecutionMode.OPENAI_LIVE:
                async with live_gate.slot():
                    return await recovery_orchestrator.decide(
                        recovery_key,
                        payload,
                        session_hash=identity.session_hash,
                        claimed_decision=claim,
                    )
            return await recovery_orchestrator.decide(
                recovery_key,
                payload,
                session_hash=identity.session_hash,
                claimed_decision=claim,
            )
        except LiveConcurrencyLimitError:
            _raise_public(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                code="live_capacity_reached",
                recovery_id=recovery_key,
                retry_after_seconds=1,
            )
        except ApprovalDecisionError as error:
            _raise_public(
                status_code=error.status_code,
                code=_decision_error_code(error.code),
                recovery_id=recovery_key,
            )
        except RecoveryNotFoundError:
            _raise_public(
                status_code=status.HTTP_404_NOT_FOUND,
                code="not_found",
                recovery_id=recovery_key,
            )
        except ResumeIncompatibleError:
            _raise_public(
                status_code=status.HTTP_409_CONFLICT,
                code="resume_incompatible",
                recovery_id=recovery_key,
            )
        except (
            LiveOperationTimeoutError,
            LiveUnavailableError,
            LiveModelRequestError,
        ) as error:
            _raise_live_failure(error, recovery_id=recovery_key)

    @application.get(
        "/api/recoveries/{recovery_id}", response_model=RecoverySnapshot
    )
    def get_recovery(recovery_id: UUID, request: Request) -> RecoverySnapshot:
        recovery_key = str(recovery_id)
        identity = optional_identity_from_scope(request.scope)
        if identity is None:
            _raise_public(
                status_code=status.HTTP_404_NOT_FOUND,
                code="not_found",
                recovery_id=recovery_key,
            )
        try:
            return recovery_store.get_recovery_for_session(
                recovery_key,
                identity.session_hash,
            )
        except RecoveryNotFoundError:
            _raise_public(
                status_code=status.HTTP_404_NOT_FOUND,
                code="not_found",
                recovery_id=recovery_key,
            )

    public_error_response: dict[str, Any] = {
        "model": PublicErrorResponse,
        "description": "The request was rejected with a public error envelope.",
    }

    @application.get(
        "/api/recoveries/{recovery_id}/events",
        response_class=StreamingResponse,
        responses={
            200: {
                "description": "Server-sent recovery event stream.",
                "content": {"text/event-stream": {}},
            },
            400: public_error_response,
            404: public_error_response,
            429: public_error_response,
        },
    )
    async def recovery_events(
        recovery_id: UUID,
        request: Request,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> Response:
        recovery_key = str(recovery_id)
        identity = optional_identity_from_scope(request.scope)
        if identity is None:
            _raise_public(
                status_code=status.HTTP_404_NOT_FOUND,
                code="not_found",
                recovery_id=recovery_key,
            )
        try:
            recovery_store.get_recovery_for_session(
                recovery_key,
                identity.session_hash,
            )
        except RecoveryNotFoundError:
            _raise_public(
                status_code=status.HTTP_404_NOT_FOUND,
                code="not_found",
                recovery_id=recovery_key,
            )
        cursor = 0
        if last_event_id is not None:
            if len(last_event_id) > 20:
                _raise_public(status_code=400, code="invalid_request")
            try:
                cursor = int(last_event_id)
            except ValueError:
                _raise_public(status_code=400, code="invalid_request")
            if cursor < 0:
                _raise_public(status_code=400, code="invalid_request")
        try:
            lease = sse_gate.acquire(
                session_key=identity.session_hash,
                recovery_id=recovery_key,
            )
        except StreamCapacityReachedError:
            _raise_public(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                code="stream_capacity_reached",
                recovery_id=recovery_key,
                retry_after_seconds=1,
            )
        try:
            return LeasedStreamingResponse(
                stream_recovery_events(
                    recovery_store,
                    recovery_key,
                    after_seq=cursor,
                    is_disconnected=request.is_disconnected,
                    session_hash=identity.session_hash,
                    session_expires_at=identity.session_expires_at,
                ),
                lease=lease,
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )
        except BaseException:
            lease.release()
            raise

    @application.get(
        "/api/recoveries/{recovery_id}/receipt", response_model=RecoveryReceipt
    )
    def get_receipt(recovery_id: UUID, request: Request) -> RecoveryReceipt:
        recovery_key = str(recovery_id)
        identity = optional_identity_from_scope(request.scope)
        if identity is None:
            _raise_public(
                status_code=status.HTTP_404_NOT_FOUND,
                code="not_found",
                recovery_id=recovery_key,
            )
        try:
            return recovery_store.get_receipt_for_session(
                recovery_key,
                identity.session_hash,
            )
        except RecoveryNotFoundError:
            _raise_public(
                status_code=status.HTTP_404_NOT_FOUND,
                code="not_found",
                recovery_id=recovery_key,
            )

    @application.post("/api/demo/reset", response_model=DemoResetResponse)
    def reset_demo(request: Request) -> DemoResetResponse:
        if not runtime_settings.demo_reset_enabled:
            _raise_public(
                status_code=status.HTTP_403_FORBIDDEN,
                code="invalid_request",
            )
        identity = optional_identity_from_scope(request.scope)
        if identity is not None:
            recovery_store.reset_for_session(identity.session_hash)
        return DemoResetResponse(reset=True)

    if frontend_bundle is not None:

        @application.api_route(
            "/{frontend_path:path}",
            methods=["GET", "HEAD"],
            include_in_schema=False,
            response_class=FileResponse,
        )
        def frontend(
            request: Request,
            frontend_path: str,
        ) -> Response:
            raw_path = request.scope.get("raw_path", b"")
            if not isinstance(raw_path, bytes):
                raw_path = b""
            response = frontend_bundle.response_for(
                path=frontend_path,
                raw_path=raw_path,
                accept=request.headers.get("accept", "*/*"),
            )
            if response is None:
                _raise_public(
                    status_code=status.HTTP_404_NOT_FOUND,
                    code="not_found",
                )
            return response

    return application


app = create_app()
