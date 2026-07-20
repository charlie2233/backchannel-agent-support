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
    demo_reset_enabled: bool = False
    deployed: bool = False
    cors_origins: tuple[str, ...] = DEVELOPMENT_CORS_ORIGINS
    trusted_proxy_cidrs: tuple[str, ...] = ()
    identity_hmac_secret: str = field(
        default=LOCAL_IDENTITY_HMAC_SECRET,
        repr=False,
    )
    max_request_body_bytes: int = 16 * 1024
    live_max_concurrent: int = 1
    live_cooldown: timedelta = timedelta(seconds=30)
    live_daily_budget: int = 12
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
        if self.live_max_concurrent < 1:
            raise ValueError("Live concurrency must be positive")
        if self.live_cooldown < timedelta(0):
            raise ValueError("Live cooldown cannot be negative")
        if self.live_daily_budget < 0:
            raise ValueError("Live daily budget cannot be negative")
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

        def integer(name: str, default: int, *, minimum: int = 0) -> int:
            raw = os.environ.get(name)
            value = default if raw is None else int(raw)
            if value < minimum:
                raise ValueError(f"{name} must be at least {minimum}")
            return value

        return cls(
            live_ready=live_ready,
            database_path=database_path,
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
            live_max_concurrent=integer(
                "BACKCHANNEL_LIVE_MAX_CONCURRENT",
                1,
                minimum=1,
            ),
            live_cooldown=timedelta(
                seconds=integer("BACKCHANNEL_LIVE_COOLDOWN_SECONDS", 30)
            ),
            live_daily_budget=integer("BACKCHANNEL_LIVE_DAILY_BUDGET", 12),
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
