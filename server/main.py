"""FastAPI entry point for truthful replay recovery persistence and streaming."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from openai import AsyncOpenAI

from server.agents.live_models import ResponseMetadataRecorder, SafeOpenAIResponsesProvider
from server.config import RuntimeSettings
from server.events import stream_recovery_events
from server.models import (
    ApprovalDecisionRequest,
    CreateRecoveryRequest,
    DecisionResponse,
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
    LiveUnavailableError,
    RecoveryOrchestrator,
    ResumeIncompatibleError,
    UnsupportedOrchestrationError,
)
from server.providers.hotel_simulator import HotelSimulator
from server.replay.engine import ReplayEngine, UnsupportedExecutionModeError
from server.replay.loader import ScenarioLoader, ScenarioNotFoundError
from server.store import ApprovalDecisionError, RecoveryNotFoundError, SQLiteStore


def create_app(
    settings: RuntimeSettings | None = None,
    *,
    store: SQLiteStore | None = None,
    hotel_provider: HotelSimulator | None = None,
    orchestrator: RecoveryOrchestrator | None = None,
) -> FastAPI:
    runtime_settings = settings or RuntimeSettings.from_environment()
    recovery_store = store or SQLiteStore(runtime_settings.database_path)
    scenario_loader = ScenarioLoader()
    replay_engine = ReplayEngine(recovery_store, scenario_loader)
    recovery_orchestrator = orchestrator
    if recovery_orchestrator is None:
        provider = hotel_provider or HotelSimulator(store=recovery_store)
        live_client = AsyncOpenAI() if runtime_settings.live_ready else None

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
            live_ready=runtime_settings.live_ready,
            live_model_provider_factory=(
                live_provider_factory if runtime_settings.live_ready else None
            ),
        )
    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        await recovery_orchestrator.startup()
        try:
            yield
        finally:
            await recovery_orchestrator.shutdown()

    application = FastAPI(
        title="Backchannel API",
        version="0.3.0",
        lifespan=lifespan,
    )
    application.state.recovery_store = recovery_store
    application.state.recovery_orchestrator = recovery_orchestrator
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(runtime_settings.development_cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Accept", "Content-Type", "Last-Event-ID"],
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
    async def create_recovery(payload: CreateRecoveryRequest) -> RecoverySnapshot:
        if payload.execution_mode is ExecutionMode.REPLAY_FIXTURE:
            try:
                return replay_engine.start(
                    payload.scenario_id,
                    execution_mode=payload.execution_mode,
                )
            except (ScenarioNotFoundError, UnsupportedExecutionModeError) as error:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=str(error),
                ) from error
        try:
            pending = await recovery_orchestrator.start(
                payload.scenario_id,
                execution_mode=payload.execution_mode,
            )
            return pending.recovery
        except UnsupportedOrchestrationError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(error),
            ) from error
        except LiveUnavailableError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": error.code},
            ) from error

    @application.post(
        "/api/recoveries/{recovery_id}/decisions",
        response_model=DecisionResponse,
    )
    async def decide_recovery(
        recovery_id: UUID,
        payload: ApprovalDecisionRequest,
    ) -> DecisionResponse:
        recovery_key = str(recovery_id)
        try:
            return await recovery_orchestrator.decide(recovery_key, payload)
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
        except LiveUnavailableError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": error.code, "recoveryId": recovery_key},
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
