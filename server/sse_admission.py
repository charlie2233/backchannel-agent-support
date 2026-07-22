"""Race-safe process-local admission and lifetime ownership for SSE responses."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from threading import Lock

from starlette.background import BackgroundTask
from starlette.responses import ContentStream, StreamingResponse
from starlette.types import Receive, Scope, Send


class StreamCapacityReachedError(RuntimeError):
    """Stable fail-fast outcome that does not reveal the saturated dimension."""

    code = "stream_capacity_reached"

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class SSEAdmissionSnapshot:
    """Aggregate-only state safe for diagnostics and tests."""

    active: int
    max_concurrent: int
    max_per_session: int
    max_per_recovery: int


class SSEStreamLease:
    """Idempotent ownership token for one admitted event stream."""

    def __init__(
        self,
        gate: SSEAdmissionGate,
        *,
        session_key: str,
        recovery_id: str,
    ) -> None:
        self._gate = gate
        self._session_key = session_key
        self._recovery_id = recovery_id
        self._released = False
        self._release_lock = Lock()

    def release(self) -> None:
        """Return capacity once; repeated and racing calls are harmless."""

        with self._release_lock:
            if self._released:
                return
            self._gate._release(  # noqa: SLF001 - lease is the gate's ownership token
                session_key=self._session_key,
                recovery_id=self._recovery_id,
            )
            self._released = True


class SSEAdmissionGate:
    """Fail-fast process-local caps for global, session, and recovery fan-out."""

    def __init__(
        self,
        *,
        max_concurrent: int,
        max_per_session: int,
        max_per_recovery: int,
    ) -> None:
        if min(max_concurrent, max_per_session, max_per_recovery) < 1:
            raise ValueError("SSE stream limits must be positive")
        if not max_per_recovery <= max_per_session <= max_concurrent:
            raise ValueError(
                "SSE stream limits must order per recovery <= per session <= global"
            )
        self._max_concurrent = max_concurrent
        self._max_per_session = max_per_session
        self._max_per_recovery = max_per_recovery
        self._active = 0
        self._sessions: dict[str, int] = {}
        self._recoveries: dict[str, int] = {}
        self._lock = Lock()

    def acquire(self, *, session_key: str, recovery_id: str) -> SSEStreamLease:
        """Atomically reserve capacity or fail without changing any counter."""

        with self._lock:
            session_active = self._sessions.get(session_key, 0)
            recovery_active = self._recoveries.get(recovery_id, 0)
            if (
                self._active >= self._max_concurrent
                or session_active >= self._max_per_session
                or recovery_active >= self._max_per_recovery
            ):
                raise StreamCapacityReachedError
            self._active += 1
            self._sessions[session_key] = session_active + 1
            self._recoveries[recovery_id] = recovery_active + 1
        return SSEStreamLease(
            self,
            session_key=session_key,
            recovery_id=recovery_id,
        )

    def snapshot(self) -> SSEAdmissionSnapshot:
        """Return aggregate capacity state without identity or recovery keys."""

        with self._lock:
            return SSEAdmissionSnapshot(
                active=self._active,
                max_concurrent=self._max_concurrent,
                max_per_session=self._max_per_session,
                max_per_recovery=self._max_per_recovery,
            )

    def _release(self, *, session_key: str, recovery_id: str) -> None:
        with self._lock:
            session_active = self._sessions.get(session_key, 0)
            recovery_active = self._recoveries.get(recovery_id, 0)
            if self._active < 1 or session_active < 1 or recovery_active < 1:
                raise RuntimeError("SSE admission counters are inconsistent")
            self._active -= 1
            if session_active == 1:
                del self._sessions[session_key]
            else:
                self._sessions[session_key] = session_active - 1
            if recovery_active == 1:
                del self._recoveries[recovery_id]
            else:
                self._recoveries[recovery_id] = recovery_active - 1


class LeasedStreamingResponse(StreamingResponse):
    """Streaming response that owns an SSE lease through its full ASGI lifetime."""

    def __init__(
        self,
        content: ContentStream,
        *,
        lease: SSEStreamLease,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
        media_type: str | None = None,
        background: BackgroundTask | None = None,
    ) -> None:
        self._lease = lease
        super().__init__(
            content,
            status_code=status_code,
            headers=headers,
            media_type=media_type,
            background=background,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._lease.release()
