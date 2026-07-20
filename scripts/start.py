"""Start one production-compatible Backchannel ASGI process."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

import uvicorn


def bind_host(environment: Mapping[str, str]) -> str:
    """Keep local and deterministic-QA processes loopback-only."""

    deployed = environment.get("BACKCHANNEL_DEPLOYED", "").strip().lower()
    return "0.0.0.0" if deployed == "true" else "127.0.0.1"


def port_from_environment(environment: Mapping[str, str]) -> int:
    """Parse the conventional deployment port without accepting partial values."""

    raw_port = environment.get("PORT", "8000").strip()
    if re.fullmatch(r"[0-9]+", raw_port, flags=re.ASCII) is None:
        raise ValueError("PORT must be an ASCII integer between 1 and 65535")
    port = int(raw_port)
    if not 1 <= port <= 65_535:
        raise ValueError("PORT must be between 1 and 65535")
    return port


def main(environment: Mapping[str, str] | None = None) -> None:
    """Run one worker so process-local admission and SSE limits stay authoritative."""

    resolved_environment = os.environ if environment is None else environment
    uvicorn.run(
        "server.main:app",
        host=bind_host(resolved_environment),
        port=port_from_environment(resolved_environment),
        workers=1,
        proxy_headers=False,
        access_log=False,
        server_header=False,
        timeout_graceful_shutdown=5,
    )


if __name__ == "__main__":
    main()
