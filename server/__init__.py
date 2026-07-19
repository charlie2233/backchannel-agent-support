"""Backchannel API package with fail-closed Agents SDK logging defaults."""

from __future__ import annotations

import logging
import os

# The Agents SDK reads these switches at import time. Backchannel never permits an
# inherited process environment to opt model inputs, tool arguments, or trace payloads
# back into logs/traces. Individual live RunConfig values repeat the trace policy.
os.environ["OPENAI_AGENTS_DONT_LOG_MODEL_DATA"] = "true"
os.environ["OPENAI_AGENTS_DONT_LOG_TOOL_DATA"] = "true"
os.environ["OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA"] = "false"

# openai==2.46.0 emits the full request options (including JSON prompts and tools) from
# this exact logger at DEBUG. Give the actual emitting logger an explicit floor so a
# hostile root/openai DEBUG configuration cannot re-enable payload logging by inheritance.
logging.getLogger("openai._base_client").setLevel(logging.WARNING)
