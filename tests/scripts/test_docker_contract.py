from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _root_package() -> dict[str, object]:
    return json.loads((ROOT / "package.json").read_text(encoding="utf-8"))


def test_image_is_locked_multistage_and_contains_only_runtime_inputs() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "FROM node:22.22.0-bookworm-slim AS frontend-build" in dockerfile
    assert "FROM python:3.12" in dockerfile
    assert "COPY package.json package-lock.json ./" in dockerfile
    assert "RUN npm ci" in dockerfile
    assert "COPY pyproject.toml uv.lock ./" in dockerfile
    assert "uv sync --frozen --no-dev" in dockerfile
    assert "COPY --from=frontend-build /app/web/dist ./web/dist" in dockerfile
    assert "COPY . ." not in dockerfile
    assert "COPY tests" not in dockerfile
    assert "COPY docs" not in dockerfile


def test_runtime_defaults_are_deployed_non_root_and_durable() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "BACKCHANNEL_DB_PATH=/data/backchannel.sqlite3" in dockerfile
    assert "BACKCHANNEL_DEPLOYED=true" in dockerfile
    assert "BACKCHANNEL_FRONTEND_DIST_PATH=/app/web/dist" in dockerfile
    assert "install -d -o backchannel -g backchannel /data" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "EXPOSE 8000" in dockerfile


def test_healthcheck_and_command_use_bounded_exec_form() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "HEALTHCHECK" in dockerfile
    assert "urllib.request" in dockerfile
    assert "127.0.0.1" in dockerfile
    assert "/readyz" in dockerfile
    assert "timeout=2" in dockerfile
    assert 'CMD ["python", "scripts/start.py"]' in dockerfile


def test_build_context_excludes_secrets_state_and_non_runtime_artifacts() -> None:
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    for required in (
        ".git",
        ".github",
        ".env*",
        "node_modules",
        "web/dist",
        "tests",
        "docs",
        "*.sqlite3",
        "*.db",
        "*.log",
    ):
        assert required in ignored
    assert "!.env.example" not in ignored


def test_root_scripts_separate_target_smoke_from_local_launch() -> None:
    scripts = _root_package()["scripts"]
    assert isinstance(scripts, dict)

    assert scripts["start"] == "uv run python scripts/start.py"
    assert scripts["smoke:container"] == "uv run python scripts/docker_smoke.py"
    assert scripts["smoke:docker"] == "uv run python scripts/docker_smoke.py"
    assert scripts["smoke:production"] == (
        "uv run python scripts/docker_smoke.py --launch"
    )
    assert scripts["presmoke:production"] == "npm run build"


def test_canonical_check_covers_production_entrypoint_and_smoke_scripts() -> None:
    scripts = _root_package()["scripts"]
    assert isinstance(scripts, dict)
    canonical = scripts["check"]
    assert isinstance(canonical, str)

    assert "ruff check server scripts tests" in canonical
    assert "mypy server scripts/start.py scripts/docker_smoke.py" in canonical
