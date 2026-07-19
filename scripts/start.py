"""Start one production-compatible Backchannel ASGI process."""

from __future__ import annotations

import os
from collections.abc import Mapping

import uvicorn


def bind_host(environment: Mapping[str, str]) -> str:
    """Keep local development loopback-only unless deployment is explicit."""

    deployed = environment.get("BACKCHANNEL_DEPLOYED_MODE", "").strip().lower()
    return "0.0.0.0" if deployed == "true" else "127.0.0.1"


def _port(environment: Mapping[str, str]) -> int:
    raw_port = environment.get("PORT", "8000")
    try:
        port = int(raw_port)
    except ValueError as error:
        raise ValueError("PORT must be an integer") from error
    if not 1 <= port <= 65_535:
        raise ValueError("PORT must be between 1 and 65535")
    return port


def main() -> None:
    uvicorn.run(
        "server.main:app",
        host=bind_host(os.environ),
        port=_port(os.environ),
        workers=1,
        proxy_headers=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()
