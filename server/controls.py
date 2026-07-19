"""Public-demo identity and live-admission controls.

Only opaque, keyed correlation values cross the durable boundary. Raw client
addresses, forwarding headers, and session cookies are never persisted.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from http.cookies import SimpleCookie
from typing import TYPE_CHECKING, cast

from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from server.config import RuntimeSettings

if TYPE_CHECKING:
    from server.store import SQLiteStore


class LiveAdmissionCode(StrEnum):
    LIVE_UNAVAILABLE = "live_unavailable"
    LIVE_CAPACITY = "live_capacity"
    COOLDOWN = "cooldown"
    DAILY_BUDGET = "daily_budget"


_PUBLIC_MESSAGES = {
    LiveAdmissionCode.LIVE_UNAVAILABLE: (
        "Live recovery is unavailable in this demo. "
        "A replay fixture is starting automatically; you can rerun it explicitly."
    ),
    LiveAdmissionCode.LIVE_CAPACITY: (
        "Live recovery is currently at capacity. "
        "A replay fixture is starting automatically; you can rerun it explicitly."
    ),
    LiveAdmissionCode.COOLDOWN: (
        "Please wait before starting another live recovery. "
        "A replay fixture is starting automatically; you can rerun it explicitly."
    ),
    LiveAdmissionCode.DAILY_BUDGET: (
        "The daily live demo budget is currently reached. "
        "A replay fixture is starting automatically; you can rerun it explicitly."
    ),
}


class LiveAdmissionError(RuntimeError):
    """Stable public live-mode rejection with no internal quota identifiers."""

    def __init__(self, code: LiveAdmissionCode) -> None:
        self.code = code
        super().__init__(code.value)

    @property
    def public_message(self) -> str:
        return _PUBLIC_MESSAGES[self.code]

    @property
    def status_code(self) -> int:
        if self.code is LiveAdmissionCode.LIVE_UNAVAILABLE:
            return 422
        return 429


class RequestBodyTooLarge(StarletteHTTPException):
    """Internal control signal for a cumulatively oversized HTTP request."""

    def __init__(self) -> None:
        super().__init__(status_code=413, detail="Request body too large")


@dataclass(frozen=True, slots=True)
class ClientIdentity:
    ip_key: str
    session_key: str
    new_session_cookie: str | None


def _normalize_ip(value: str) -> str | None:
    candidate = value.strip().strip('"')
    if candidate.startswith("[") and "]" in candidate:
        candidate = candidate[1 : candidate.index("]")]
    elif candidate.count(":") == 1 and "." in candidate:
        candidate = candidate.split(":", 1)[0]
    try:
        return ipaddress.ip_address(candidate).compressed
    except ValueError:
        return None


class PublicDemoControls:
    """Coordinates in-process model slots with durable public admission."""

    def __init__(self, store: SQLiteStore, settings: RuntimeSettings) -> None:
        self._store = store
        self._settings = settings
        self._live_semaphore = asyncio.Semaphore(settings.max_concurrent_live_recoveries)
        self._identity_secret = settings.identity_hash_secret.encode("utf-8")

    def _correlation_key(self, namespace: str, value: str) -> str:
        return hmac.new(
            self._identity_secret,
            f"{namespace}:{value}".encode(),
            hashlib.sha256,
        ).hexdigest()

    def _client_ip(self, request: Request) -> str:
        direct = request.client.host if request.client is not None else "unknown"
        normalized_direct = _normalize_ip(direct) or "unknown"
        if not self._settings.trusted_proxy_enabled:
            return normalized_direct
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            normalized_forwarded = _normalize_ip(forwarded.split(",", 1)[0])
            if normalized_forwarded is not None:
                return normalized_forwarded
        return normalized_direct

    def resolve_client_identity(
        self,
        request: Request,
        *,
        now: datetime | None = None,
    ) -> ClientIdentity:
        current = now or datetime.now(UTC)
        raw_cookie = request.cookies.get(self._settings.demo_session_cookie_name)
        session_key: str | None = None
        if raw_cookie is not None and 32 <= len(raw_cookie) <= 256:
            candidate_key = self._correlation_key("session", raw_cookie)
            if self._store.demo_session_is_active(candidate_key, now=current):
                session_key = candidate_key

        new_session_cookie: str | None = None
        if session_key is None:
            new_session_cookie = secrets.token_urlsafe(32)
            session_key = self._correlation_key("session", new_session_cookie)
            self._store.create_demo_session(
                session_key,
                created_at=current,
                expires_at=current
                + timedelta(seconds=self._settings.demo_session_lifetime_seconds),
            )

        return ClientIdentity(
            ip_key=self._correlation_key("ip", self._client_ip(request)),
            session_key=session_key,
            new_session_cookie=new_session_cookie,
        )

    def session_cookie_header(self, value: str) -> str:
        cookie = SimpleCookie()
        cookie[self._settings.demo_session_cookie_name] = value
        morsel = cookie[self._settings.demo_session_cookie_name]
        morsel["path"] = "/"
        morsel["max-age"] = str(self._settings.demo_session_lifetime_seconds)
        morsel["httponly"] = True
        morsel["samesite"] = "Lax"
        if self._settings.effective_demo_session_cookie_secure:
            morsel["secure"] = True
        return morsel.OutputString()

    def admit_live(
        self,
        *,
        recovery_id: str,
        ip_key: str,
        session_key: str,
        now: datetime | None = None,
    ) -> None:
        rejected_code = self._store.try_admit_live_recovery(
            recovery_id=recovery_id,
            ip_key=ip_key,
            session_key=session_key,
            max_active=self._settings.max_concurrent_live_recoveries,
            ip_cooldown=timedelta(seconds=self._settings.live_ip_cooldown_seconds),
            session_cooldown=timedelta(
                seconds=self._settings.live_session_cooldown_seconds
            ),
            daily_budget_units=self._settings.daily_demo_budget_units,
            active_ttl=timedelta(
                seconds=self._settings.terminal_recovery_ttl_seconds
            ),
            now=now or datetime.now(UTC),
        )
        if rejected_code is not None:
            raise LiveAdmissionError(LiveAdmissionCode(rejected_code))

    def release_live(self, recovery_id: str, *, now: datetime | None = None) -> None:
        self._store.release_live_admission(
            recovery_id,
            now=now or datetime.now(UTC),
        )

    @asynccontextmanager
    async def live_model_slot(self) -> AsyncIterator[None]:
        async with self._live_semaphore:
            yield


class PublicBoundaryMiddleware:
    """Pure ASGI hardening that never buffers request or response streams."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        controls: PublicDemoControls,
        settings: RuntimeSettings,
    ) -> None:
        self.app = app
        self._controls = controls
        self._settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = secrets.token_hex(16)
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        identity = self._controls.resolve_client_identity(Request(scope))
        state["demo_identity"] = identity
        headers = Headers(scope=scope)
        declared_length = headers.get("content-length")
        declared_too_large = False
        if declared_length is not None:
            try:
                declared_too_large = (
                    int(declared_length)
                    > self._settings.request_body_size_limit_bytes
                )
            except ValueError:
                # An invalid length is never trusted; cumulative byte counting remains active.
                declared_too_large = False

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                response_headers["x-request-id"] = request_id
                response_headers["x-content-type-options"] = "nosniff"
                response_headers["x-frame-options"] = "DENY"
                response_headers["referrer-policy"] = "no-referrer"
                response_headers["permissions-policy"] = (
                    "camera=(), microphone=(), geolocation=()"
                )
                response_headers["content-security-policy"] = (
                    "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
                )
                if "cache-control" not in response_headers:
                    response_headers["cache-control"] = "no-store"
                if self._settings.deployed_mode:
                    response_headers["strict-transport-security"] = (
                        "max-age=31536000; includeSubDomains"
                    )
                if (
                    identity.new_session_cookie is not None
                    and "set-cookie" not in response_headers
                ):
                    response_headers.append(
                        "set-cookie",
                        self._controls.session_cookie_header(identity.new_session_cookie),
                    )
            await send(message)

        async def send_too_large() -> None:
            payload = json.dumps(
                {
                    "code": "request_too_large",
                    "message": "The request body is too large.",
                    "requestId": request_id,
                },
                separators=(",", ":"),
            ).encode("utf-8")
            await send_with_headers(
                {
                    "type": "http.response.start",
                    "status": 413,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(payload)).encode("ascii")),
                    ],
                }
            )
            await send_with_headers(
                {"type": "http.response.body", "body": payload, "more_body": False}
            )

        if declared_too_large:
            await send_too_large()
            return

        received_bytes = 0

        async def limited_receive() -> Message:
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(cast(bytes, message.get("body", b"")))
                if received_bytes > self._settings.request_body_size_limit_bytes:
                    raise RequestBodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send_with_headers)
        except RequestBodyTooLarge:
            await send_too_large()
