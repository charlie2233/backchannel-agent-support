"""Strict mode-bound trace ID validation shared by API and persistence layers."""

from __future__ import annotations

import re

_QA_TRACE_PATTERN = re.compile(r"qa_trace_[0-9a-f]{32}\Z")
_LIVE_TRACE_PATTERN = re.compile(r"trace_[0-9a-f]{32}\Z")


def is_valid_qa_trace_id(value: str) -> bool:
    return _QA_TRACE_PATTERN.fullmatch(value) is not None


def is_valid_live_trace_id(value: str) -> bool:
    return _LIVE_TRACE_PATTERN.fullmatch(value) is not None
