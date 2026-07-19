"""Public-demo request, identity, admission, and local concurrency controls."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from http.cookies import CookieError, SimpleCookie
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from server.logging import log_public_event

DEMO_SESSION_COOKIE = "backchannel_demo_session"
HASH_PREFIX = "hmac-sha256:"
PublicErrorCode = Literal[
    "invalid_request",
    "method_not_allowed",
    "not_found",
    "unsupported_media_type",
    "request_too_large",
    "live_unavailable",
    "live_cooldown",
    "live_daily_budget_exceeded",
    "live_capacity_reached",
    "resume_incompatible",
    "already_decided",
    "decision_id_conflict",
    "remedy_mismatch",
    "remedy_digest_mismatch",
    "tool_call_mismatch",
    "consent_expired",
    "hard_constraint_denied",
    "authority_denied",
    "constraint_denied",
    "remedy_expired",
    "decision_unavailable",
    "decision_in_progress",
    "model_metadata_conflict",
    "internal_error",
]

PUBLIC_ERROR_MESSAGES: Mapping[str, str] = {
    "invalid_request": "The request is invalid.",
    "method_not_allowed": "The method is not allowed.",
    "not_found": "The requested resource was not found.",
    "unsupported_media_type": "Content-Type must be application/json.",
    "request_too_large": "The request body is too large.",
    "live_unavailable": "Live mode is unavailable on this server.",
    "live_cooldown": "Live mode is cooling down for this demo identity.",
    "live_daily_budget_exceeded": "The live demo budget is exhausted for today.",
    "live_capacity_reached": "The live demo is currently at capacity.",
    "resume_incompatible": "The saved decision cannot be resumed safely.",
    "already_decided": "This recovery already has a terminal decision.",
    "decision_id_conflict": "This decision identifier was already used.",
    "remedy_mismatch": "The decision does not match the pending remedy.",
    "remedy_digest_mismatch": "The decision does not match the displayed terms.",
    "tool_call_mismatch": "The decision does not match the pending tool call.",
    "consent_expired": "The displayed remedy has expired.",
    "hard_constraint_denied": "The remedy no longer satisfies hard constraints.",
    "authority_denied": "The remedy exceeds delegated authority.",
    "constraint_denied": "The remedy no longer satisfies hard constraints.",
    "remedy_expired": "The displayed remedy has expired.",
    "decision_unavailable": "The pending decision is unavailable.",
    "decision_in_progress": "The durable decision is already being resumed.",
    "model_metadata_conflict": "The saved decision cannot be resumed safely.",
    "internal_error": "The request could not be completed.",
}


class PublicLiveAdmissionError(RuntimeError):
    """Stable durable limit outcome; it contains no raw public identity."""

    def __init__(
        self,
        code: Literal["live_cooldown", "live_daily_budget_exceeded"],
        *,
        retry_after_seconds: int | None = None,
    ) -> None:
        self.code = code
        self.retry_after_seconds = retry_after_seconds
        super().__init__(code)


class LiveConcurrencyLimitError(RuntimeError):
    """Raised before entry when this single process has no free live slot."""

    code = "live_capacity_reached"

    def __init__(self) -> None:
        super().__init__(self.code)


class PublicApiException(RuntimeError):
    """Typed route failure carrying only allowlisted public fields."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        recovery_id: str | None = None,
        retry_after_seconds: int | None = None,
        replay_offer: bool = False,
    ) -> None:
        self.status_code = status_code
        self.code = code if code in PUBLIC_ERROR_MESSAGES else "internal_error"
        self.recovery_id = recovery_id
        self.retry_after_seconds = retry_after_seconds
        self.replay_offer = replay_offer
        super().__init__(self.code)


class InvalidForwardedHeader(ValueError):
    """Trusted forwarding metadata was malformed or unreasonably large."""


@dataclass(frozen=True, slots=True)
class PublicIdentity:
    session_hash: str
    ip_hash: str


@dataclass(frozen=True, slots=True)
class ReplayOffer:
    kind: Literal["show_replay_fixture"] = "show_replay_fixture"
    scenario_id: Literal["hotel"] = "hotel"
    execution_mode: Literal["replay_fixture"] = "replay_fixture"

    def to_json(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "scenarioId": self.scenario_id,
            "executionMode": self.execution_mode,
        }


class PublicIdentityHasher:
    """HMAC and domain-separate identifiers before any durable use."""

    def __init__(self, secret: str) -> None:
        encoded = secret.encode("utf-8")
        if len(encoded) < 32:
            raise ValueError("Identity HMAC secret must contain at least 32 UTF-8 bytes")
        self._secret = encoded

    def _digest(self, domain: str, value: str) -> str:
        digest = hmac.new(
            self._secret,
            f"backchannel:{domain}:v1\0{value}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return f"{HASH_PREFIX}{digest}"

    def session(self, session_id: str) -> str:
        return self._digest("session", session_id)

    def ip(self, client_ip: str) -> str:
        return self._digest("ip", client_ip)


def resolve_client_ip(
    *,
    peer_ip: str,
    forwarded_for: str | None,
    trusted_proxy_cidrs: tuple[str, ...],
) -> str:
    """Use forwarding data only when the connected peer is explicitly trusted."""

    def normalized(value: str) -> IPv4Address | IPv6Address:
        parsed = ip_address(value)
        if isinstance(parsed, IPv6Address) and parsed.ipv4_mapped is not None:
            return parsed.ipv4_mapped
        return parsed

    try:
        peer = normalized(peer_ip)
    except ValueError:
        return "0.0.0.0"
    trusted = tuple(ip_network(cidr, strict=False) for cidr in trusted_proxy_cidrs)
    if not trusted or not any(peer in network for network in trusted):
        return str(peer)
    if forwarded_for is None:
        return str(peer)
    if len(forwarded_for) > 1024:
        raise InvalidForwardedHeader("Forwarded chain is too large")
    parts = forwarded_for.split(",")
    if len(parts) > 16:
        raise InvalidForwardedHeader("Forwarded chain has too many hops")
    forwarded: list[Any] = []
    for raw in parts:
        try:
            forwarded.append(normalized(raw.strip()))
        except ValueError as error:
            raise InvalidForwardedHeader("Forwarded chain contains an invalid IP") from error
    if not forwarded:
        raise InvalidForwardedHeader("Forwarded chain is empty")
    chain = [*forwarded, peer]
    while len(chain) > 1 and any(chain[-1] in network for network in trusted):
        chain.pop()
    return str(chain[-1])


class LiveConcurrencyGate:
    """Fail-fast process-local concurrency gate for public live model work."""

    def __init__(self, *, max_concurrent: int) -> None:
        if max_concurrent < 1:
            raise ValueError("Live concurrency must be positive")
        self._max_concurrent = max_concurrent
        self._active = 0
        self._lock = asyncio.Lock()

    @property
    def active(self) -> int:
        return self._active

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        async with self._lock:
            if self._active >= self._max_concurrent:
                raise LiveConcurrencyLimitError
            self._active += 1
        try:
            yield
        finally:
            async with self._lock:
                self._active -= 1


class DemoSessionStore(Protocol):
    def is_demo_session_active(
        self,
        *,
        session_hash: str,
        now: datetime,
    ) -> bool: ...


def new_request_id() -> str:
    return f"req_{uuid4().hex}"


def public_error_detail(
    *,
    code: str,
    request_id: str,
    replay_offer: bool = False,
    recovery_id: str | None = None,
    retry_after_seconds: int | None = None,
) -> dict[str, object]:
    safe_code = code if code in PUBLIC_ERROR_MESSAGES else "internal_error"
    detail: dict[str, object] = {
        "code": safe_code,
        "message": PUBLIC_ERROR_MESSAGES[safe_code],
        "requestId": request_id,
        "recoveryId": None,
        "retryAfterSeconds": retry_after_seconds,
        "fallback": ReplayOffer().to_json() if replay_offer else None,
    }
    if recovery_id is not None:
        try:
            detail["recoveryId"] = str(UUID(recovery_id))
        except ValueError:
            pass
    return detail


def public_error_body(
    *,
    code: str,
    request_id: str,
    replay_offer: bool = False,
    recovery_id: str | None = None,
    retry_after_seconds: int | None = None,
) -> bytes:
    return json.dumps(
        {
            "error": public_error_detail(
                code=code,
                request_id=request_id,
                replay_offer=replay_offer,
                recovery_id=recovery_id,
                retry_after_seconds=retry_after_seconds,
            )
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def request_id_from_scope(scope: Scope) -> str:
    state = scope.get("state")
    if isinstance(state, dict):
        request_id = state.get("request_id")
        if isinstance(request_id, str) and request_id.startswith("req_"):
            return request_id
    return new_request_id()


def recovery_id_from_scope(scope: Scope) -> str | None:
    """Return only a normalized UUID from a recovery-scoped API path."""

    candidate: object | None = None
    path_params = scope.get("path_params")
    if isinstance(path_params, dict):
        candidate = path_params.get("recovery_id")
    if candidate is None:
        path = scope.get("path")
        if isinstance(path, str):
            matched = re.fullmatch(r"/api/recoveries/([^/]+)(?:/[^/]+)?", path)
            if matched is not None:
                candidate = matched.group(1)
    if candidate is None:
        return None
    try:
        return str(candidate if isinstance(candidate, UUID) else UUID(str(candidate)))
    except ValueError:
        return None


def identity_from_scope(scope: Scope) -> PublicIdentity:
    state = scope.get("state")
    if isinstance(state, dict):
        identity = state.get("public_identity")
        if isinstance(identity, PublicIdentity):
            return identity
    raise RuntimeError("Public identity middleware is not installed")


class PublicBoundaryMiddleware:
    """Enforce byte/media bounds and attach safe identity and response headers."""

    _JSON_ROUTES = frozenset({"/api/recoveries", "/api/demo/reset"})
    _UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int,
        identity_hasher: PublicIdentityHasher,
        session_store: DemoSessionStore,
        session_ttl_seconds: int,
        trusted_proxy_cidrs: tuple[str, ...],
        deployed: bool,
        allowed_origins: tuple[str, ...] = (),
    ) -> None:
        self.app = app
        self._max_body_bytes = max_body_bytes
        self._hasher = identity_hasher
        self._session_store = session_store
        self._session_ttl_seconds = session_ttl_seconds
        self._trusted_proxy_cidrs = trusted_proxy_cidrs
        self._deployed = deployed
        self._allowed_origins = frozenset(allowed_origins)

    @staticmethod
    def _headers(scope: Scope) -> dict[str, str]:
        return {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", ())
        }

    @staticmethod
    def _valid_session(value: str | None) -> str | None:
        if value is None or re.fullmatch(r"[A-Za-z0-9_-]{43}", value) is None:
            return None
        return value

    def _session_candidate(self, headers: Mapping[str, str]) -> str | None:
        cookie = SimpleCookie()
        try:
            cookie.load(headers.get("cookie", ""))
        except CookieError:
            cookie = SimpleCookie()
        morsel = cookie.get(DEMO_SESSION_COOKIE)
        return self._valid_session(morsel.value if morsel is not None else None)

    def _set_cookie_value(self, session_id: str) -> str:
        cookie = SimpleCookie()
        cookie[DEMO_SESSION_COOKIE] = session_id
        cookie[DEMO_SESSION_COOKIE]["path"] = "/"
        cookie[DEMO_SESSION_COOKIE]["max-age"] = str(self._session_ttl_seconds)
        cookie[DEMO_SESSION_COOKIE]["httponly"] = True
        cookie[DEMO_SESSION_COOKIE]["samesite"] = "lax"
        if self._deployed:
            cookie[DEMO_SESSION_COOKIE]["secure"] = True
        return cookie[DEMO_SESSION_COOKIE].OutputString()

    def _security_headers(self, request_id: str) -> tuple[tuple[bytes, bytes], ...]:
        headers = [
            (b"x-request-id", request_id.encode("ascii")),
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"),
            (b"referrer-policy", b"no-referrer"),
            (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
            (b"cross-origin-opener-policy", b"same-origin"),
            (
                b"content-security-policy",
                b"default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
                b"object-src 'none'; form-action 'self'",
            ),
        ]
        if self._deployed:
            headers.append(
                (b"strict-transport-security", b"max-age=31536000; includeSubDomains")
            )
        return tuple(headers)

    async def _send_error(
        self,
        send: Send,
        *,
        status_code: int,
        code: str,
        request_id: str,
        response_headers: tuple[tuple[bytes, bytes], ...],
        replay_offer: bool = False,
        recovery_id: str | None = None,
    ) -> None:
        body = public_error_body(
            code=code,
            request_id=request_id,
            replay_offer=replay_offer,
            recovery_id=recovery_id,
        )
        log_public_event(
            event="request_rejected",
            request_id=request_id,
            code=code,
            status_code=status_code,
            recovery_id=recovery_id,
        )
        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                    *response_headers,
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = new_request_id()
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        headers = self._headers(scope)
        scoped_recovery_id = recovery_id_from_scope(scope)
        security_headers = self._security_headers(request_id)
        fixed_headers = security_headers
        content_type_values = [
            value
            for key, value in scope.get("headers", ())
            if key.lower() == b"content-type"
        ]
        cookie_values = [
            value.decode("latin-1")
            for key, value in scope.get("headers", ())
            if key.lower() == b"cookie"
        ]
        origin_values = [
            value.decode("latin-1")
            for key, value in scope.get("headers", ())
            if key.lower() == b"origin"
        ]
        demo_cookie_count = sum(
            len(
                re.findall(
                    rf"(?:^|;)\s*{re.escape(DEMO_SESSION_COOKIE)}\s*=",
                    value,
                )
            )
            for value in cookie_values
        )
        if (
            len(content_type_values) > 1
            or len(cookie_values) > 1
            or demo_cookie_count > 1
            or len(origin_values) > 1
        ):
            await self._send_error(
                send,
                status_code=400,
                code="invalid_request",
                request_id=request_id,
                response_headers=fixed_headers,
                recovery_id=scoped_recovery_id,
            )
            return
        if (
            self._deployed
            and scope.get("method") in self._UNSAFE_METHODS
            and origin_values
            and origin_values[0] not in self._allowed_origins
        ):
            await self._send_error(
                send,
                status_code=403,
                code="invalid_request",
                request_id=request_id,
                response_headers=fixed_headers,
                recovery_id=scoped_recovery_id,
            )
            return

        content_lengths = [
            value.strip()
            for key, value in scope.get("headers", ())
            if key.lower() == b"content-length"
        ]
        transfer_encodings = [
            value.strip().lower()
            for key, value in scope.get("headers", ())
            if key.lower() == b"transfer-encoding"
        ]
        if content_lengths and transfer_encodings:
            await self._send_error(
                send,
                status_code=400,
                code="invalid_request",
                request_id=request_id,
                response_headers=fixed_headers,
                recovery_id=scoped_recovery_id,
            )
            return
        if len(transfer_encodings) > 1 or (
            transfer_encodings and transfer_encodings[0] != b"chunked"
        ):
            await self._send_error(
                send,
                status_code=400,
                code="invalid_request",
                request_id=request_id,
                response_headers=fixed_headers,
                recovery_id=scoped_recovery_id,
            )
            return
        if len(content_lengths) > 1:
            await self._send_error(
                send,
                status_code=400,
                code="invalid_request",
                request_id=request_id,
                response_headers=fixed_headers,
                recovery_id=scoped_recovery_id,
            )
            return
        declared_length: int | None = None
        if content_lengths:
            raw_declared_length = content_lengths[0]
            if (
                len(raw_declared_length) > 20
                or re.fullmatch(rb"[0-9]+", raw_declared_length) is None
            ):
                await self._send_error(
                    send,
                    status_code=400,
                    code="invalid_request",
                    request_id=request_id,
                    response_headers=fixed_headers,
                    recovery_id=scoped_recovery_id,
                )
                return
            declared_length = int(raw_declared_length)
            if declared_length > self._max_body_bytes:
                await self._send_error(
                    send,
                    status_code=413,
                    code="request_too_large",
                    request_id=request_id,
                    response_headers=fixed_headers,
                    recovery_id=scoped_recovery_id,
                )
                return

        messages: list[Message] = []
        total = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            total += len(message.get("body", b""))
            if total > self._max_body_bytes:
                await self._send_error(
                    send,
                    status_code=413,
                    code="request_too_large",
                    request_id=request_id,
                    response_headers=fixed_headers,
                    recovery_id=scoped_recovery_id,
                )
                return
            if not message.get("more_body", False):
                break

        if declared_length is not None and declared_length != total:
            await self._send_error(
                send,
                status_code=400,
                code="invalid_request",
                request_id=request_id,
                response_headers=fixed_headers,
                recovery_id=scoped_recovery_id,
            )
            return

        path = scope.get("path", "")
        needs_json = scope.get("method") == "POST" and (
            path in self._JSON_ROUTES or path.endswith("/decisions")
        )
        content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if needs_json and content_type != "application/json":
            await self._send_error(
                send,
                status_code=415,
                code="unsupported_media_type",
                request_id=request_id,
                response_headers=fixed_headers,
                recovery_id=scoped_recovery_id,
            )
            return

        if scope.get("method") == "POST" and path == "/api/recoveries":
            forwarded_values = [
                value.decode("latin-1")
                for key, value in scope.get("headers", ())
                if key.lower() == b"x-forwarded-for"
            ]
            client = scope.get("client")
            peer_ip = client[0] if client is not None else "0.0.0.0"
            try:
                peer = ip_address(peer_ip)
            except ValueError:
                peer_is_trusted = False
            else:
                if isinstance(peer, IPv6Address) and peer.ipv4_mapped is not None:
                    peer = peer.ipv4_mapped
                peer_is_trusted = any(
                    peer in ip_network(cidr, strict=False)
                    for cidr in self._trusted_proxy_cidrs
                )
            if peer_is_trusted and len(forwarded_values) > 1:
                await self._send_error(
                    send,
                    status_code=400,
                    code="invalid_request",
                    request_id=request_id,
                    response_headers=fixed_headers,
                    recovery_id=scoped_recovery_id,
                )
                return
            try:
                client_ip = resolve_client_ip(
                    peer_ip=peer_ip,
                    forwarded_for=(forwarded_values[0] if forwarded_values else None),
                    trusted_proxy_cidrs=self._trusted_proxy_cidrs,
                )
            except InvalidForwardedHeader:
                await self._send_error(
                    send,
                    status_code=400,
                    code="invalid_request",
                    request_id=request_id,
                    response_headers=fixed_headers,
                    recovery_id=scoped_recovery_id,
                )
                return

            now = datetime.now(UTC)
            session_id = self._session_candidate(headers)
            candidate_hash: str | None = None
            session_is_new = True
            try:
                if session_id is not None:
                    candidate_hash = self._hasher.session(session_id)
                    if self._session_store.is_demo_session_active(
                        session_hash=candidate_hash,
                        now=now,
                    ):
                        session_is_new = False
                if session_is_new:
                    session_id = secrets.token_urlsafe(32)
                    session_hash = self._hasher.session(session_id)
                else:
                    assert candidate_hash is not None
                    session_hash = candidate_hash
            except Exception:
                await self._send_error(
                    send,
                    status_code=500,
                    code="internal_error",
                    request_id=request_id,
                    response_headers=fixed_headers,
                    recovery_id=scoped_recovery_id,
                )
                return
            assert session_id is not None
            state["public_identity"] = PublicIdentity(
                session_hash=session_hash,
                ip_hash=self._hasher.ip(client_ip),
            )
            if session_is_new:
                fixed_headers = (
                    *fixed_headers,
                    (
                        b"set-cookie",
                        self._set_cookie_value(session_id).encode("latin-1"),
                    ),
                )

        message_index = 0

        async def replay_receive() -> Message:
            nonlocal message_index
            if message_index < len(messages):
                message = messages[message_index]
                message_index += 1
                return message
            return await receive()

        response_started = False
        response_complete = False

        async def wrapped_send(message: Message) -> None:
            nonlocal response_complete, response_started
            if message["type"] == "http.response.start":
                response_started = True
                canonical_names = {key for key, _value in security_headers}
                downstream_headers = [
                    (key, value)
                    for key, value in message.get("headers", ())
                    if key.lower() not in canonical_names
                ]
                session_headers = fixed_headers[len(security_headers) :]
                message["headers"] = [
                    *downstream_headers,
                    *security_headers,
                    *session_headers,
                ]
            elif (
                message["type"] == "http.response.body"
                and not message.get("more_body", False)
            ):
                response_complete = True
            await send(message)

        try:
            await self.app(scope, replay_receive, wrapped_send)
        except Exception:
            if response_started:
                log_public_event(
                    event="response_failed",
                    request_id=request_id,
                    code="internal_error",
                    status_code=500,
                    recovery_id=scoped_recovery_id,
                )
                if not response_complete:
                    try:
                        await send(
                            {
                                "type": "http.response.body",
                                "body": b"",
                                "more_body": False,
                            }
                        )
                    except Exception:
                        pass
                return
            await self._send_error(
                send,
                status_code=500,
                code="internal_error",
                request_id=request_id,
                response_headers=fixed_headers,
                recovery_id=scoped_recovery_id,
            )
