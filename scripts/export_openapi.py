"""Export the public FastAPI schema without starting the application lifespan."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Final

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_PATH: Final = REPOSITORY_ROOT / "docs" / "openapi.json"
GENERATION_TIMEOUT_SECONDS: Final = 30

_CHILD_PROGRAM: Final = """
import json
import sys

import server.main as server_main

try:
    document = server_main.app.openapi()
    json.dump(document, sys.stdout, ensure_ascii=False, sort_keys=True)
finally:
    server_main.app.state.recovery_store.close()
"""


class OpenAPIExportError(RuntimeError):
    """A redacted schema generation failure safe to show in CI."""


def canonical_json_bytes(document: object) -> bytes:
    """Return the repository's canonical UTF-8 JSON representation."""

    return (
        json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _isolated_environment(database_path: Path) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key != "OPENAI_API_KEY" and not key.startswith("BACKCHANNEL_")
    }
    current_python_path = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = os.pathsep.join(
        item for item in (str(REPOSITORY_ROOT), current_python_path) if item
    )
    environment["BACKCHANNEL_DB_PATH"] = str(database_path)
    return environment


def build_openapi_bytes() -> bytes:
    """Generate the schema in a keyless, API-only child process."""

    with tempfile.TemporaryDirectory(prefix="backchannel-openapi-") as temporary:
        database_path = Path(temporary) / "schema.sqlite3"
        try:
            result = subprocess.run(
                [sys.executable, "-c", _CHILD_PROGRAM],
                cwd=REPOSITORY_ROOT,
                env=_isolated_environment(database_path),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=GENERATION_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise OpenAPIExportError("OpenAPI schema generation failed") from error
        if result.returncode != 0:
            raise OpenAPIExportError("OpenAPI schema generation failed")
        try:
            document = json.loads(result.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OpenAPIExportError("OpenAPI schema generation failed") from error
    return canonical_json_bytes(document)


def artifact_matches(destination: Path, expected: bytes) -> bool:
    """Compare exact bytes without creating or mutating the destination."""

    try:
        return destination.read_bytes() == expected
    except FileNotFoundError:
        return False


def write_artifact_atomic(destination: Path, payload: bytes) -> None:
    """Atomically replace an artifact using a same-directory temporary file."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temporary_path = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(raw_temporary_path)
    try:
        with os.fdopen(descriptor, "wb") as temporary_file:
            os.fchmod(temporary_file.fileno(), 0o644)
            temporary_file.write(payload)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, destination)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def run(*, check: bool, destination: Path = DEFAULT_OUTPUT_PATH) -> int:
    payload = build_openapi_bytes()
    if check:
        if artifact_matches(destination, payload):
            return 0
        status = "missing" if not destination.exists() else "out of date"
        print(f"OpenAPI artifact is {status}.", file=sys.stderr)
        return 1
    write_artifact_atomic(destination, payload)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if docs/openapi.json differs without rewriting it",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        return run(check=arguments.check)
    except OpenAPIExportError:
        print("OpenAPI schema generation failed.", file=sys.stderr)
        return 2
    except OSError:
        print("OpenAPI artifact filesystem operation failed.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
