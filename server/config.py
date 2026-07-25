"""Runtime configuration that exposes capability booleans, never secret values."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import timedelta
from ipaddress import ip_network
from pathlib import Path
from urllib.parse import urlsplit

DEVELOPMENT_CORS_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)
LOCAL_IDENTITY_HMAC_SECRET = "backchannel-local-identity-secret-v0.3-only"

# Serialized SDK approvals are intentionally bound to these application contracts.
APPROVAL_PROTOCOL_VERSION = "backchannel.approval.v1"
HOTEL_AGENT_GRAPH_VERSION = "backchannel.hotel-agent.v1"
HOTEL_LIVE_AGENT_GRAPH_VERSION = "backchannel.hotel-live-agent.v1"
QUOTA_PROTOCOL_VERSION = "backchannel.quota.v1"
QUOTA_AGENT_GRAPH_VERSION = "backchannel.quota-agent.v1"


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    """Non-secret runtime capabilities used by public health responses."""

    live_ready: bool
    database_path: Path = Path("backchannel.sqlite3")
    frontend_dist_path: Path | None = None
    demo_reset_enabled: bool = False
    deployed: bool = False
    cors_origins: tuple[str, ...] = DEVELOPMENT_CORS_ORIGINS
    trusted_proxy_cidrs: tuple[str, ...] = ()
    identity_hmac_secret: str = field(
        default=LOCAL_IDENTITY_HMAC_SECRET,
        repr=False,
    )
    max_request_body_bytes: int = 16 * 1024
    request_body_read_timeout: timedelta = timedelta(seconds=5)
    live_max_concurrent: int = 1
    live_operation_timeout: timedelta = timedelta(seconds=60)
    live_cooldown: timedelta = timedelta(seconds=30)
    live_daily_budget: int = 12
    creation_session_daily_budget: int = 12
    creation_ip_daily_budget: int = 60
    creation_global_daily_budget: int = 120
    creation_usage_retention: timedelta = timedelta(days=8)
    sse_max_concurrent: int = 32
    sse_max_per_session: int = 4
    sse_max_per_recovery: int = 2
    demo_session_ttl: timedelta = timedelta(days=1)
    recovery_ttl: timedelta = timedelta(days=7)
    cleanup_interval: timedelta = timedelta(minutes=5)
    cleanup_batch_size: int = 100

    def __post_init__(self) -> None:
        if len(self.identity_hmac_secret.encode("utf-8")) < 32:
            raise ValueError("Identity HMAC secret must contain at least 32 UTF-8 bytes")
        if (
            self.deployed or self.live_ready
        ) and self.identity_hmac_secret == LOCAL_IDENTITY_HMAC_SECRET:
            raise ValueError(
                "Deployed or live mode requires an explicit identity HMAC secret"
            )
        if self.max_request_body_bytes < 1:
            raise ValueError("Request body limit must be positive")
        if not timedelta(seconds=1) <= self.request_body_read_timeout <= timedelta(
            seconds=30
        ):
            raise ValueError(
                "Request body read timeout must be between 1 and 30 seconds"
            )
        if self.live_max_concurrent < 1:
            raise ValueError("Live concurrency must be positive")
        if not timedelta(seconds=1) <= self.live_operation_timeout <= timedelta(
            seconds=300
        ):
            raise ValueError("Live operation timeout must be between 1 and 300 seconds")
        if self.live_cooldown < timedelta(0):
            raise ValueError("Live cooldown cannot be negative")
        if self.live_daily_budget < 0:
            raise ValueError("Live daily budget cannot be negative")
        if min(
            self.creation_session_daily_budget,
            self.creation_ip_daily_budget,
            self.creation_global_daily_budget,
        ) < 0:
            raise ValueError("Public creation daily budgets cannot be negative")
        if self.creation_usage_retention < timedelta(days=1):
            raise ValueError("Public creation usage retention must be at least one day")
        if min(
            self.sse_max_concurrent,
            self.sse_max_per_session,
            self.sse_max_per_recovery,
        ) < 1:
            raise ValueError("SSE stream limits must be positive")
        if not (
            self.sse_max_per_recovery
            <= self.sse_max_per_session
            <= self.sse_max_concurrent
        ):
            raise ValueError(
                "SSE stream limits must order per recovery <= per session <= global"
            )
        if self.demo_session_ttl <= timedelta(0):
            raise ValueError("Demo session TTL must be positive")
        if self.recovery_ttl <= timedelta(0):
            raise ValueError("Recovery TTL must be positive")
        if self.cleanup_interval <= timedelta(0):
            raise ValueError("Cleanup interval must be positive")
        if self.cleanup_batch_size < 1:
            raise ValueError("Cleanup batch size must be positive")
        for cidr in self.trusted_proxy_cidrs:
            ip_network(cidr, strict=False)
        for origin in self.cors_origins:
            parsed = urlsplit(origin)
            if (
                not parsed.scheme
                or not parsed.netloc
                or parsed.path not in ("", "/")
                or parsed.query
                or parsed.fragment
                or "*" in origin
            ):
                raise ValueError("CORS origins must be exact scheme-and-host origins")
            if self.deployed and parsed.scheme != "https":
                raise ValueError("Deployed CORS origins must use HTTPS")
        if self.deployed and not self.cors_origins:
            raise ValueError("Deployed mode requires at least one exact CORS origin")

    @property
    def development_cors_origins(self) -> tuple[str, ...]:
        """Compatibility name retained for the existing application factory."""

        return self.cors_origins

    @property
    def sdk_stub_ready(self) -> bool:
        """Keep the deterministic SDK transport lane local to QA environments."""

        return not self.deployed

    @classmethod
    def from_environment(cls) -> RuntimeSettings:
        # Collapse configuration to a boolean immediately. The value is not retained.
        live_ready = bool(os.environ.get("OPENAI_API_KEY", "").strip())
        database_path = Path(os.environ.get("BACKCHANNEL_DB_PATH", "backchannel.sqlite3"))
        configured_frontend_path = os.environ.get(
            "BACKCHANNEL_FRONTEND_DIST_PATH",
            "",
        ).strip()
        frontend_dist_path = (
            Path(configured_frontend_path) if configured_frontend_path else None
        )
        demo_reset_enabled = (
            os.environ.get("BACKCHANNEL_DEMO_RESET_ENABLED", "").strip().lower()
            == "true"
        )
        deployed = os.environ.get("BACKCHANNEL_DEPLOYED", "").strip().lower() == "true"
        configured_origins = tuple(
            origin.strip()
            for origin in os.environ.get("BACKCHANNEL_CORS_ORIGINS", "").split(",")
            if origin.strip()
        )
        cors_origins = (
            configured_origins
            if configured_origins or deployed
            else DEVELOPMENT_CORS_ORIGINS
        )
        trusted_proxy_cidrs = tuple(
            cidr.strip()
            for cidr in os.environ.get("BACKCHANNEL_TRUSTED_PROXY_CIDRS", "").split(",")
            if cidr.strip()
        )
        identity_hmac_secret = os.environ.get(
            "BACKCHANNEL_IDENTITY_HMAC_SECRET",
            "" if deployed else LOCAL_IDENTITY_HMAC_SECRET,
        )

        def integer(
            name: str,
            default: int,
            *,
            minimum: int = 0,
            maximum: int | None = None,
        ) -> int:
            raw = os.environ.get(name)
            value = default if raw is None else int(raw)
            if value < minimum:
                raise ValueError(f"{name} must be at least {minimum}")
            if maximum is not None and value > maximum:
                raise ValueError(f"{name} must be at most {maximum}")
            return value

        return cls(
            live_ready=live_ready,
            database_path=database_path,
            frontend_dist_path=frontend_dist_path,
            demo_reset_enabled=demo_reset_enabled,
            deployed=deployed,
            cors_origins=cors_origins,
            trusted_proxy_cidrs=trusted_proxy_cidrs,
            identity_hmac_secret=identity_hmac_secret,
            max_request_body_bytes=integer(
                "BACKCHANNEL_MAX_REQUEST_BODY_BYTES",
                16 * 1024,
                minimum=1,
            ),
            request_body_read_timeout=timedelta(
                seconds=integer(
                    "BACKCHANNEL_REQUEST_BODY_READ_TIMEOUT_SECONDS",
                    5,
                    minimum=1,
                    maximum=30,
                )
            ),
            live_max_concurrent=integer(
                "BACKCHANNEL_LIVE_MAX_CONCURRENT",
                1,
                minimum=1,
            ),
            live_operation_timeout=timedelta(
                seconds=integer(
                    "BACKCHANNEL_LIVE_OPERATION_TIMEOUT_SECONDS",
                    60,
                    minimum=1,
                    maximum=300,
                )
            ),
            live_cooldown=timedelta(
                seconds=integer("BACKCHANNEL_LIVE_COOLDOWN_SECONDS", 30)
            ),
            live_daily_budget=integer("BACKCHANNEL_LIVE_DAILY_BUDGET", 12),
            creation_session_daily_budget=integer(
                "BACKCHANNEL_CREATION_SESSION_DAILY_BUDGET",
                12,
            ),
            creation_ip_daily_budget=integer(
                "BACKCHANNEL_CREATION_IP_DAILY_BUDGET",
                60,
            ),
            creation_global_daily_budget=integer(
                "BACKCHANNEL_CREATION_GLOBAL_DAILY_BUDGET",
                120,
            ),
            creation_usage_retention=timedelta(
                seconds=integer(
                    "BACKCHANNEL_CREATION_USAGE_RETENTION_SECONDS",
                    8 * 24 * 60 * 60,
                    minimum=24 * 60 * 60,
                )
            ),
            sse_max_concurrent=integer(
                "BACKCHANNEL_SSE_MAX_CONCURRENT",
                32,
                minimum=1,
            ),
            sse_max_per_session=integer(
                "BACKCHANNEL_SSE_MAX_PER_SESSION",
                4,
                minimum=1,
            ),
            sse_max_per_recovery=integer(
                "BACKCHANNEL_SSE_MAX_PER_RECOVERY",
                2,
                minimum=1,
            ),
            demo_session_ttl=timedelta(
                seconds=integer(
                    "BACKCHANNEL_DEMO_SESSION_TTL_SECONDS",
                    24 * 60 * 60,
                    minimum=1,
                )
            ),
            recovery_ttl=timedelta(
                seconds=integer(
                    "BACKCHANNEL_RECOVERY_TTL_SECONDS",
                    7 * 24 * 60 * 60,
                    minimum=1,
                )
            ),
            cleanup_interval=timedelta(
                seconds=integer(
                    "BACKCHANNEL_CLEANUP_INTERVAL_SECONDS",
                    5 * 60,
                    minimum=1,
                )
            ),
            cleanup_batch_size=integer(
                "BACKCHANNEL_CLEANUP_BATCH_SIZE",
                100,
                minimum=1,
            ),
        )
