"""Allowlisted public-boundary logging with no request or provider payloads."""

from __future__ import annotations

import logging
import re
from uuid import UUID

PUBLIC_LOGGER = logging.getLogger("server.public")
_SAFE_TOKEN = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_SAFE_REQUEST_ID = re.compile(r"req_[0-9a-f]{32}\Z")


def _safe_recovery_id(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(UUID(value))
    except ValueError:
        return None


def safe_recovery_log_id(value: str | None) -> str:
    """Return a correlation-safe UUID token for non-public operational logs."""

    return _safe_recovery_id(value) or "invalid"


def log_public_event(
    *,
    event: str,
    request_id: str,
    code: str,
    status_code: int,
    recovery_id: str | None = None,
    level: int = logging.INFO,
) -> None:
    """Log only fixed event/code tokens and server-generated correlation identifiers."""

    if _SAFE_TOKEN.fullmatch(event) is None:
        event = "boundary_event"
    if _SAFE_TOKEN.fullmatch(code) is None:
        code = "internal_error"
    if _SAFE_REQUEST_ID.fullmatch(request_id) is None:
        request_id = "req_invalid"
    fields = [
        "public_event",
        f"event={event}",
        f"request_id={request_id}",
        f"code={code}",
        f"status={status_code}",
    ]
    safe_recovery_id = _safe_recovery_id(recovery_id)
    if safe_recovery_id is not None:
        fields.append(f"recovery_id={safe_recovery_id}")
    PUBLIC_LOGGER.log(level, " ".join(fields))
