"""Runtime configuration that exposes capability booleans, never secret values."""

from __future__ import annotations

import os
from dataclasses import dataclass

DEVELOPMENT_CORS_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    """Non-secret runtime capabilities used by public health responses."""

    live_ready: bool
    development_cors_origins: tuple[str, ...] = DEVELOPMENT_CORS_ORIGINS

    @classmethod
    def from_environment(cls) -> RuntimeSettings:
        # Collapse configuration to a boolean immediately. The value is not retained.
        live_ready = bool(os.environ.get("OPENAI_API_KEY", "").strip())
        return cls(live_ready=live_ready)
