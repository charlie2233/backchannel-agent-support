"""FastAPI entry point for truthful runtime capability reporting."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from server.config import RuntimeSettings
from server.models import HealthResponse, ProviderBoundary, ReadinessResponse, RuntimeBackend


def create_app(settings: RuntimeSettings | None = None) -> FastAPI:
    runtime_settings = settings or RuntimeSettings.from_environment()
    application = FastAPI(title="Backchannel API", version="0.3.0")
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(runtime_settings.development_cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "OPTIONS"],
        allow_headers=["Accept", "Content-Type"],
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

    return application


app = create_app()
