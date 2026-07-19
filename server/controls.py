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
from uuid import UUID

from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from server.config import RuntimeSettings
from server.logging import get_safe_logger, log_safe_exception

if TYPE_CHECKING:
    from server.store import SQLiteStore


logger = get_safe_logger(__name__)

_SESSION_COOKIE_VERSION = "v1"
_SESSION_COOKIE_DOMAIN = "backchannel.demo-session-cookie"
_SESSION_COOKIE_MAX_LENGTH = 160
_SESSION_NONCE_LENGTH = 43
_SESSION_SIGNATURE_LENGTH = 64


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

DECISION_CAPACITY_MESSAGE = (
    "Live decision processing is currently at capacity. "
    "Retry the same decision shortly."
)


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


class LiveDecisionCapacityError(RuntimeError):
    """Stable decision-endpoint retry signal with no replay implication."""

    code = "decision_capacity"
    public_message = DECISION_CAPACITY_MESSAGE
    status_code = 429


class RequestBodyTooLarge(StarletteHTTPException):
    """Internal control signal for a cumulatively oversized HTTP request."""

    def __init__(self) -> None:
        super().__init__(status_code=413, detail="Request body too large")


class SanitizedApplicationError(RuntimeError):
    """Opaque exception re-raised to preserve ASGI and TestClient semantics."""

    def __init__(self) -> None:
        super().__init__("Unhandled application error")


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
        self._trusted_proxy_networks = tuple(
            ipaddress.ip_network(cidr, strict=False)
            for cidr in settings.trusted_proxy_cidrs
        )

    def _correlation_key(self, namespace: str, value: str) -> str:
        return hmac.new(
            self._identity_secret,
            f"{namespace}:{value}".encode(),
            hashlib.sha256,
        ).hexdigest()

    def _session_cookie_signature(self, payload: str) -> str:
        return hmac.new(
            self._identity_secret,
            f"{_SESSION_COOKIE_DOMAIN}:{payload}".encode(),
            hashlib.sha256,
        ).hexdigest()

    def _issue_session_cookie(self, current: datetime) -> tuple[str, str]:
        nonce = secrets.token_urlsafe(32)
        expires_at = int(current.timestamp()) + self._settings.demo_session_lifetime_seconds
        payload = f"{_SESSION_COOKIE_VERSION}.{expires_at}.{nonce}"
        signature = self._session_cookie_signature(payload)
        return f"{payload}.{signature}", nonce

    def _verified_session_nonce(
        self,
        raw_cookie: str,
        *,
        current: datetime,
    ) -> str | None:
        if not 1 <= len(raw_cookie) <= _SESSION_COOKIE_MAX_LENGTH:
            return None
        parts = raw_cookie.split(".")
        if len(parts) != 4:
            return None
        version, expiry_text, nonce, supplied_signature = parts
        if (
            version != _SESSION_COOKIE_VERSION
            or not expiry_text.isascii()
            or not expiry_text.isdigit()
            or not 1 <= len(expiry_text) <= 12
            or len(nonce) != _SESSION_NONCE_LENGTH
            or not all(
                (character.isascii() and character.isalnum()) or character in "-_"
                for character in nonce
            )
            or len(supplied_signature) != _SESSION_SIGNATURE_LENGTH
            or not all(character in "0123456789abcdef" for character in supplied_signature)
        ):
            return None

        payload = f"{version}.{expiry_text}.{nonce}"
        expected_signature = self._session_cookie_signature(payload)
        if not hmac.compare_digest(supplied_signature, expected_signature):
            return None

        expires_at = int(expiry_text)
        now_timestamp = int(current.timestamp())
        remaining_lifetime = expires_at - now_timestamp
        if not 0 < remaining_lifetime <= self._settings.demo_session_lifetime_seconds:
            return None
        return nonce

    def _client_ip(self, request: Request) -> str:
        direct = request.client.host if request.client is not None else "unknown"
        normalized_direct = _normalize_ip(direct) or "unknown"
        if (
            not self._settings.trusted_proxy_enabled
            or normalized_direct == "unknown"
            or not self._ip_is_trusted_proxy(normalized_direct)
        ):
            return normalized_direct
        forwarded = request.headers.get("x-forwarded-for")
        if not forwarded:
            return normalized_direct
        forwarded_hops = [_normalize_ip(value) for value in forwarded.split(",")]
        if any(hop is None for hop in forwarded_hops):
            return normalized_direct
        current = normalized_direct
        for hop in reversed(cast(list[str], forwarded_hops)):
            if not self._ip_is_trusted_proxy(current):
                break
            current = hop
        return current

    def _ip_is_trusted_proxy(self, value: str) -> bool:
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            return False
        return any(address in network for network in self._trusted_proxy_networks)

    def resolve_client_identity(
        self,
        request: Request,
        *,
        now: datetime | None = None,
    ) -> ClientIdentity:
        current = now or datetime.now(UTC)
        raw_cookie = request.cookies.get(self._settings.demo_session_cookie_name)
        new_session_cookie: str | None = None
        session_nonce = (
            self._verified_session_nonce(raw_cookie, current=current)
            if raw_cookie is not None
            else None
        )
        if session_nonce is None:
            new_session_cookie, session_nonce = self._issue_session_cookie(current)

        return ClientIdentity(
            ip_key=self._correlation_key("ip", self._client_ip(request)),
            session_key=self._correlation_key("session", session_nonce),
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
            lease_ttl=timedelta(
                seconds=self._settings.live_admission_lease_seconds
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

    def guard_live_resume(
        self,
        recovery_id: str,
        *,
        now: datetime | None = None,
    ) -> None:
        guarded = self._store.renew_or_reacquire_live_admission(
            recovery_id,
            max_active=self._settings.max_concurrent_live_recoveries,
            lease_ttl=timedelta(
                seconds=self._settings.live_admission_lease_seconds
            ),
            now=now or datetime.now(UTC),
        )
        if not guarded:
            raise LiveAdmissionError(LiveAdmissionCode.LIVE_CAPACITY)

    def _renew_live_start(self, recovery_id: str) -> None:
        renewed = self._store.renew_live_admission_lease(
            recovery_id,
            lease_ttl=timedelta(
                seconds=self._settings.live_admission_lease_seconds
            ),
            now=datetime.now(UTC),
        )
        if not renewed:
            raise LiveAdmissionError(LiveAdmissionCode.LIVE_CAPACITY)

    async def _heartbeat_live_lease(
        self,
        recovery_id: str,
        stop: asyncio.Event,
    ) -> None:
        interval = max(0.25, self._settings.live_admission_lease_seconds / 3)
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                self._renew_live_start(recovery_id)
            else:
                return

    @asynccontextmanager
    async def live_model_slot(
        self,
        recovery_id: str | None = None,
    ) -> AsyncIterator[None]:
        async with self._live_semaphore:
            if recovery_id is None:
                yield
                return
            self._renew_live_start(recovery_id)
            stop = asyncio.Event()
            heartbeat = asyncio.create_task(
                self._heartbeat_live_lease(recovery_id, stop)
            )
            try:
                yield
            finally:
                stop.set()
                await heartbeat


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
        sanitized_error: SanitizedApplicationError | None = None
        try:
            await self._handle_http(
                scope,
                receive,
                send,
                request_id=request_id,
                state=state,
            )
        except Exception as error:
            recovery_id = state.get("recovery_id")
            validated_recovery_id: str | None = None
            if isinstance(recovery_id, str):
                try:
                    validated_recovery_id = str(UUID(recovery_id))
                except ValueError:
                    validated_recovery_id = None
            log_safe_exception(
                logger,
                request_id=request_id,
                recovery_id=validated_recovery_id,
                error=error,
            )
            sanitized_error = SanitizedApplicationError()
        if sanitized_error is not None:
            # Raise only after leaving the raw exception context, so the opaque
            # boundary error carries neither a cause nor a hidden context chain.
            raise sanitized_error

    async def _handle_http(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        request_id: str,
        state: dict[str, object],
    ) -> None:
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
