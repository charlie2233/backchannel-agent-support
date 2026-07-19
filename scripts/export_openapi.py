"""Render Backchannel's public OpenAPI schema without touching the caller database."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "docs" / "openapi.json"


def _isolated_environment(database_path: Path) -> dict[str, str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("BACKCHANNEL_") and name != "OPENAI_API_KEY"
    }
    environment["BACKCHANNEL_DB_PATH"] = str(database_path)
    return environment


def _render_in_isolated_process(database_path: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--render-child"],
        cwd=ROOT,
        env=_isolated_environment(database_path),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError("Isolated OpenAPI generation failed.") from None
    schema = json.loads(completed.stdout)
    if not isinstance(schema, dict):
        raise RuntimeError("Isolated OpenAPI generation returned an invalid schema.")
    return schema


def render_openapi() -> str:
    with TemporaryDirectory(prefix="backchannel-openapi-") as temporary:
        schema = _render_in_isolated_process(Path(temporary) / "schema.sqlite3")
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def _render_child() -> None:
    # The parent installs a disposable database environment before this process starts.
    from server.config import RuntimeSettings
    from server.main import create_app

    database_path = Path(os.environ["BACKCHANNEL_DB_PATH"])
    settings = RuntimeSettings(live_ready=False, database_path=database_path)
    application = create_app(settings, static_dir=None)
    try:
        print(json.dumps(application.openapi(), sort_keys=True))
    finally:
        application.state.recovery_store.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--render-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when docs/openapi.json differs from the implemented API",
    )
    arguments = parser.parse_args()
    if arguments.render_child:
        _render_child()
        return

    rendered = render_openapi()
    if arguments.check:
        if not OUTPUT.is_file() or OUTPUT.read_text(encoding="utf-8") != rendered:
            print("docs/openapi.json is stale; run npm run openapi:export")
            raise SystemExit(1)
        print("OpenAPI export is current.")
        return
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(rendered, encoding="utf-8")
    print("Wrote docs/openapi.json")


if __name__ == "__main__":
    main()
