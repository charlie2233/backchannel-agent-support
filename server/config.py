"""Runtime configuration that exposes capability booleans, never secret values."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEVELOPMENT_CORS_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)

# Serialized SDK approvals are intentionally bound to these application contracts.
APPROVAL_PROTOCOL_VERSION = "backchannel.approval.v1"
HOTEL_AGENT_GRAPH_VERSION = "backchannel.hotel-agent.v1"
LIVE_HOTEL_AGENT_GRAPH_VERSION = "backchannel.hotel-agent.live.v1"


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    """Non-secret runtime capabilities used by public health responses."""

    live_ready: bool
    development_cors_origins: tuple[str, ...] = DEVELOPMENT_CORS_ORIGINS
    database_path: Path = Path("backchannel.sqlite3")
    demo_reset_enabled: bool = False

    @classmethod
    def from_environment(cls) -> RuntimeSettings:
        # Collapse configuration to a boolean immediately. The value is not retained.
        live_ready = bool(os.environ.get("OPENAI_API_KEY", "").strip())
        database_path = Path(os.environ.get("BACKCHANNEL_DB_PATH", "backchannel.sqlite3"))
        demo_reset_enabled = (
            os.environ.get("BACKCHANNEL_DEMO_RESET_ENABLED", "").strip().lower()
            == "true"
        )
        return cls(
            live_ready=live_ready,
            database_path=database_path,
            demo_reset_enabled=demo_reset_enabled,
        )
