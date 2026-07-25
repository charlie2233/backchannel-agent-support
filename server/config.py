"""Runtime configuration that exposes capability booleans, never secret values."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

DEVELOPMENT_CORS_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)
DEVELOPMENT_IDENTITY_HASH_SECRET = "backchannel-local-development-identity-key"
DEFAULT_MAX_RECOVERY_CREATIONS_PER_SESSION = 32
DEFAULT_MAX_RECOVERY_CREATIONS_GLOBAL = 2_048
MAX_RECOVERY_CREATIONS_PER_SESSION = 4_096
MAX_RECOVERY_CREATIONS_GLOBAL = 100_000


def _environment_bool(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ValueError(f"{name} must be either true or false")


def _environment_int(name: str, *, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error


def _environment_origins(name: str) -> tuple[str, ...]:
    raw = os.environ.get(name, "")
    return tuple(origin.strip() for origin in raw.split(",") if origin.strip())


def _environment_cidrs(name: str) -> tuple[str, ...]:
    raw = os.environ.get(name, "")
    return tuple(value.strip() for value in raw.split(",") if value.strip())

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
    max_concurrent_live_recoveries: int = 2
    live_ip_cooldown_seconds: int = 60
    live_session_cooldown_seconds: int = 60
    daily_demo_budget_units: int = 100
    live_admission_lease_seconds: int = 300
    terminal_recovery_ttl_seconds: int = 86_400
    terminal_cleanup_interval_seconds: int = 300
    request_body_size_limit_bytes: int = 16_384
    max_concurrent_event_streams: int = 16
    max_event_streams_per_recovery: int = 4
    event_stream_retry_seconds: int = 5
    max_recovery_creations_per_session: int = (
        DEFAULT_MAX_RECOVERY_CREATIONS_PER_SESSION
    )
    max_recovery_creations_global: int = DEFAULT_MAX_RECOVERY_CREATIONS_GLOBAL
    deployed_mode: bool = False
    deployed_cors_origins: tuple[str, ...] = ()
    trusted_proxy_enabled: bool = False
    trusted_proxy_cidrs: tuple[str, ...] = ()
    demo_session_cookie_name: str = "backchannel_demo_session"
    demo_session_lifetime_seconds: int = 86_400
    demo_session_cookie_secure: bool = False
    identity_hash_secret: str = field(
        default=DEVELOPMENT_IDENTITY_HASH_SECRET,
        repr=False,
    )

    def __post_init__(self) -> None:
        bounds = {
            "max_concurrent_live_recoveries": (
                self.max_concurrent_live_recoveries,
                1,
                100,
            ),
            "live_ip_cooldown_seconds": (self.live_ip_cooldown_seconds, 1, 86_400),
            "live_session_cooldown_seconds": (
                self.live_session_cooldown_seconds,
                1,
                86_400,
            ),
            "daily_demo_budget_units": (self.daily_demo_budget_units, 1, 1_000_000),
            "live_admission_lease_seconds": (
                self.live_admission_lease_seconds,
                1,
                86_400,
            ),
            "terminal_recovery_ttl_seconds": (
                self.terminal_recovery_ttl_seconds,
                1,
                2_592_000,
            ),
            "terminal_cleanup_interval_seconds": (
                self.terminal_cleanup_interval_seconds,
                1,
                86_400,
            ),
            "request_body_size_limit_bytes": (
                self.request_body_size_limit_bytes,
                1,
                1_048_576,
            ),
            "max_concurrent_event_streams": (
                self.max_concurrent_event_streams,
                1,
                1_024,
            ),
            "max_event_streams_per_recovery": (
                self.max_event_streams_per_recovery,
                1,
                1_024,
            ),
            "event_stream_retry_seconds": (
                self.event_stream_retry_seconds,
                1,
                300,
            ),
            "max_recovery_creations_per_session": (
                self.max_recovery_creations_per_session,
                1,
                MAX_RECOVERY_CREATIONS_PER_SESSION,
            ),
            "max_recovery_creations_global": (
                self.max_recovery_creations_global,
                1,
                MAX_RECOVERY_CREATIONS_GLOBAL,
            ),
            "demo_session_lifetime_seconds": (
                self.demo_session_lifetime_seconds,
                1,
                604_800,
            ),
        }
        for name, (value, minimum, maximum) in bounds.items():
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not minimum <= value <= maximum
            ):
                raise ValueError(f"{name} must be between {minimum} and {maximum}")

        if self.max_event_streams_per_recovery > self.max_concurrent_event_streams:
            raise ValueError(
                "max_event_streams_per_recovery cannot exceed "
                "max_concurrent_event_streams"
            )
        if (
            self.max_recovery_creations_per_session
            > self.max_recovery_creations_global
        ):
            raise ValueError(
                "max_recovery_creations_per_session cannot exceed "
                "max_recovery_creations_global"
            )

        if not self.demo_session_cookie_name or any(
            character in self.demo_session_cookie_name for character in " ;,\r\n\t"
        ):
            raise ValueError("demo_session_cookie_name must be a safe cookie token")
        if len(self.identity_hash_secret.encode("utf-8")) < 32:
            raise ValueError("identity_hash_secret must contain at least 32 bytes")
        for cidr in self.trusted_proxy_cidrs:
            try:
                ipaddress.ip_network(cidr, strict=False)
            except ValueError as error:
                raise ValueError("trusted_proxy_cidrs must contain valid IP networks") from error
        if self.trusted_proxy_enabled and not self.trusted_proxy_cidrs:
            raise ValueError("trusted proxy mode requires explicit trusted_proxy_cidrs")
        if self.deployed_mode:
            if self.identity_hash_secret == DEVELOPMENT_IDENTITY_HASH_SECRET:
                raise ValueError(
                    "deployed mode requires an explicit BACKCHANNEL_IDENTITY_HASH_SECRET"
                )
            if not self.deployed_cors_origins:
                raise ValueError("deployed mode requires an explicit HTTPS origin allowlist")
            for origin in self.deployed_cors_origins:
                parsed = urlparse(origin)
                if (
                    parsed.scheme != "https"
                    or not parsed.netloc
                    or parsed.path not in {"", "/"}
                    or parsed.params
                    or parsed.query
                    or parsed.fragment
                    or "*" in origin
                ):
                    raise ValueError(
                        "deployed mode origins must be exact HTTPS origins without wildcards"
                    )

    @property
    def cors_origins(self) -> tuple[str, ...]:
        return self.deployed_cors_origins if self.deployed_mode else self.development_cors_origins

    @property
    def effective_demo_session_cookie_secure(self) -> bool:
        return self.deployed_mode or self.demo_session_cookie_secure

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
            max_concurrent_live_recoveries=_environment_int(
                "BACKCHANNEL_MAX_CONCURRENT_LIVE_RECOVERIES",
                default=2,
            ),
            live_ip_cooldown_seconds=_environment_int(
                "BACKCHANNEL_LIVE_IP_COOLDOWN_SECONDS",
                default=60,
            ),
            live_session_cooldown_seconds=_environment_int(
                "BACKCHANNEL_LIVE_SESSION_COOLDOWN_SECONDS",
                default=60,
            ),
            daily_demo_budget_units=_environment_int(
                "BACKCHANNEL_DAILY_DEMO_BUDGET_UNITS",
                default=100,
            ),
            live_admission_lease_seconds=_environment_int(
                "BACKCHANNEL_LIVE_ADMISSION_LEASE_SECONDS",
                default=300,
            ),
            terminal_recovery_ttl_seconds=_environment_int(
                "BACKCHANNEL_TERMINAL_RECOVERY_TTL_SECONDS",
                default=86_400,
            ),
            terminal_cleanup_interval_seconds=_environment_int(
                "BACKCHANNEL_TERMINAL_CLEANUP_INTERVAL_SECONDS",
                default=300,
            ),
            request_body_size_limit_bytes=_environment_int(
                "BACKCHANNEL_REQUEST_BODY_SIZE_LIMIT_BYTES",
                default=16_384,
            ),
            max_concurrent_event_streams=_environment_int(
                "BACKCHANNEL_MAX_CONCURRENT_EVENT_STREAMS",
                default=16,
            ),
            max_event_streams_per_recovery=_environment_int(
                "BACKCHANNEL_MAX_EVENT_STREAMS_PER_RECOVERY",
                default=4,
            ),
            event_stream_retry_seconds=_environment_int(
                "BACKCHANNEL_EVENT_STREAM_RETRY_SECONDS",
                default=5,
            ),
            max_recovery_creations_per_session=_environment_int(
                "BACKCHANNEL_MAX_RECOVERY_CREATIONS_PER_SESSION",
                default=DEFAULT_MAX_RECOVERY_CREATIONS_PER_SESSION,
            ),
            max_recovery_creations_global=_environment_int(
                "BACKCHANNEL_MAX_RECOVERY_CREATIONS_GLOBAL",
                default=DEFAULT_MAX_RECOVERY_CREATIONS_GLOBAL,
            ),
            deployed_mode=_environment_bool("BACKCHANNEL_DEPLOYED_MODE"),
            deployed_cors_origins=_environment_origins("BACKCHANNEL_CORS_ORIGINS"),
            trusted_proxy_enabled=_environment_bool("BACKCHANNEL_TRUSTED_PROXY_ENABLED"),
            trusted_proxy_cidrs=_environment_cidrs("BACKCHANNEL_TRUSTED_PROXY_CIDRS"),
            demo_session_cookie_name=os.environ.get(
                "BACKCHANNEL_DEMO_SESSION_COOKIE_NAME",
                "backchannel_demo_session",
            ),
            demo_session_lifetime_seconds=_environment_int(
                "BACKCHANNEL_DEMO_SESSION_LIFETIME_SECONDS",
                default=86_400,
            ),
            demo_session_cookie_secure=_environment_bool(
                "BACKCHANNEL_DEMO_SESSION_COOKIE_SECURE"
            ),
            identity_hash_secret=os.environ.get(
                "BACKCHANNEL_IDENTITY_HASH_SECRET",
                DEVELOPMENT_IDENTITY_HASH_SECRET,
            ),
        )
