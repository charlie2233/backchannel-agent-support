"""Logging helpers that reject secret- or payload-shaped messages."""

from __future__ import annotations

import logging
import re

_UNSAFE_LOG_CONTENT = re.compile(
    r"(?i)(?:"
    r"\bsk-[a-z0-9_-]+|"
    r"authorization\s*:|"
    r"\bbearer\s+[a-z0-9._~+/-]+|"
    r"\bcookie\s*:|"
    r"\bprompt\s*=|"
    r"\bstate_json\s*=|"
    r"\b(?:runstate|run_state)\b|"
    r"\btool_(?:args|arguments|results?)\s*="
    r")"
)


class SafeLogFilter(logging.Filter):
    """Replace a whole unsafe record instead of attempting partial disclosure."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:
            rendered = ""
        if _UNSAFE_LOG_CONTENT.search(rendered):
            record.msg = "[REDACTED]"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
        return True


def get_safe_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not any(isinstance(item, SafeLogFilter) for item in logger.filters):
        logger.addFilter(SafeLogFilter())
    return logger


def log_safe_exception(
    logger: logging.Logger,
    *,
    request_id: str,
    error: BaseException,
    recovery_id: str | None = None,
) -> None:
    """Log only allowlisted correlation and exception-type fields."""

    logger.error(
        "request_failed request_id=%s recovery_id=%s error_type=%s",
        request_id,
        recovery_id or "none",
        type(error).__name__,
    )
