from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_PARSER_DIRECTIVE = re.compile(r"#\s*(?:syntax|escape|check)\s*=", re.IGNORECASE)
_C_STYLE_SYNTAX_DIRECTIVE = re.compile(r"//\s*syntax\s*=", re.IGNORECASE)
_REVIEWED_CONTAINER_INPUT_DIGESTS = {
    "Dockerfile": "d3f6a544655af6cb7075658f8b40e65e50507af9ef46277d1f29cff94d2bde1f",
    ".dockerignore": "996f2172e36bce0e306651d5f9a7880457f5f5e604a880f8b122eef9a23cfdc0",
}
_EXPECTED_STAGE_BASES = {
    "frontend-build": (
        "node:22.22.0-bookworm-slim@"
        "sha256:dd9d21971ec4395903fa6143c2b9267d048ae01ca6d3ea96f16cb30df6187d94"
    ),
    "uv-bin": (
        "ghcr.io/astral-sh/uv:0.10.1@"
        "sha256:452e02b117acd2d4eb3ba81a607bed9733b101b6c49492e352b1973463389012"
    ),
    "python-build": (
        "python:3.12.12-slim-bookworm@"
        "sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c"
    ),
    "runtime": (
        "python:3.12.12-slim-bookworm@"
        "sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c"
    ),
}
_EXPECTED_COPY_INSTRUCTIONS = (
    "COPY package.json package-lock.json ./",
    "COPY web/package.json ./web/package.json",
    "COPY web ./web",
    "COPY --from=uv-bin /uv /usr/local/bin/uv",
    "COPY pyproject.toml uv.lock ./",
    "COPY server ./server",
    "COPY --from=python-build /app/.venv ./.venv",
    "COPY server ./server",
    "COPY scripts/start.py scripts/docker_smoke.py ./scripts/",
    "COPY --from=frontend-build /app/web/dist ./web/dist",
)
_EXPECTED_RUN_INSTRUCTIONS = (
    "RUN npm ci",
    "RUN npm run build",
    "RUN uv sync --frozen --no-dev",
    (
        "RUN groupadd --gid 10001 backchannel && useradd --uid 10001 --gid 10001 "
        "--create-home --shell /usr/sbin/nologin backchannel && install -d "
        "-o backchannel -g backchannel /data"
    ),
)
_EXPECTED_DOCKERIGNORE_PATTERNS = frozenset(
    {
        ".git",
        ".github",
        ".env*",
        "**/.env",
        "**/.env.*",
        ".venv",
        "__pycache__",
        "*.py[cod]",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "**/.uv-cache",
        "node_modules",
        "web/dist",
        "coverage",
        "**/output",
        "tests",
        "docs",
        "e2e",
        "playwright-report",
        "test-results",
        "*.sqlite",
        "*.sqlite3",
        "*.sqlite3-*",
        "*.db",
        "*.db-*",
        "*.log",
        "*.pid",
        "**/*.pem",
        "**/*.key",
        "**/*.p8",
        "**/*.p12",
        "**/*.pfx",
        "**/.npmrc",
        "**/.pypirc",
        "**/.netrc",
        ".DS_Store",
    }
)


def _instructions(dockerfile: str) -> list[str]:
    assert "\ufeff" not in dockerfile
    instructions: list[str] = []
    for line in dockerfile.splitlines():
        assert not line.rstrip().endswith("\\")
        stripped = line.strip()
        if not stripped:
            continue
        assert _C_STYLE_SYNTAX_DIRECTIVE.match(stripped) is None
        if stripped.startswith("#"):
            assert not stripped.startswith("#!")
            assert _PARSER_DIRECTIVE.match(stripped) is None
            continue
        instructions.append(stripped)
    return instructions


def _stage_bases(dockerfile: str) -> dict[str, str]:
    stages: dict[str, str] = {}
    instructions = _instructions(dockerfile)
    assert instructions and instructions[0].startswith("FROM ")
    for instruction in instructions:
        if not re.match(r"(?i:FROM)(?:\s|$)", instruction):
            continue
        match = re.fullmatch(r"FROM ([^ ]+) AS ([a-z0-9-]+)", instruction)
        assert match is not None
        image, stage = match.groups()
        assert stage not in stages
        stages[stage] = image
    return stages


def _assert_internal_image_sources(
    dockerfile: str,
    *,
    stages: set[str],
) -> None:
    for instruction in _instructions(dockerfile):
        keyword = instruction.split(maxsplit=1)[0]
        if keyword.lower() == "copy":
            assert keyword == "COPY"
            _, remainder = instruction.split(maxsplit=1)
            options: list[str] = []
            while remainder.startswith("--"):
                option_match = re.match(r"(?P<option>--[^\s]+)(?:\s+|$)", remainder)
                assert option_match is not None
                options.append(option_match.group("option"))
                remainder = remainder[option_match.end() :].lstrip()
            assert "--from=" not in remainder
            source_options = [
                option.removeprefix("--from=")
                for option in options
                if option.startswith("--from=")
            ]
            assert not any(
                option.startswith("--from") and not option.startswith("--from=")
                for option in options
            )
            assert len(source_options) <= 1
            assert all(source in stages for source in source_options)
        elif keyword.lower() == "run":
            assert keyword == "RUN"
            for mount in re.findall(r"(?:^|\s)--mount=([^\s]+)", instruction):
                sources = [
                    option.removeprefix("from=")
                    for option in mount.split(",")
                    if option.startswith("from=")
                ]
                assert len(sources) <= 1
                assert all(source in stages for source in sources)


def _assert_bounded_local_context_sources(dockerfile: str) -> None:
    copy_instructions: list[str] = []
    run_instructions: list[str] = []
    for instruction in _instructions(dockerfile):
        keyword = instruction.split(maxsplit=1)[0]
        if keyword.lower() == "onbuild":
            raise AssertionError("ONBUILD may hide an unreviewed context source")
        if keyword.lower() == "add":
            raise AssertionError("ADD may introduce an unreviewed local or remote source")
        if keyword.lower() == "run":
            run_instructions.append(instruction)
        if keyword.lower() == "copy":
            copy_instructions.append(instruction)

    assert tuple(copy_instructions) == _EXPECTED_COPY_INSTRUCTIONS
    assert tuple(run_instructions) == _EXPECTED_RUN_INSTRUCTIONS


def _dockerignore_patterns(dockerignore: bytes) -> set[str]:
    assert b"\x00" not in dockerignore
    assert not dockerignore.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in dockerignore.replace(b"\r\n", b"")
    decoded = dockerignore.replace(b"\r\n", b"\n").decode("utf-8", errors="strict")

    patterns: list[str] = []
    for line in decoded.split("\n"):
        if not line:
            continue
        assert line == line.strip()
        if line.startswith("#"):
            continue
        assert not line.startswith("!")
        patterns.append(line)
    assert len(patterns) == len(set(patterns))
    return set(patterns)


def test_container_build_inputs_match_the_reviewed_raw_bytes() -> None:
    for relative_path, expected_digest in _REVIEWED_CONTAINER_INPUT_DIGESTS.items():
        actual_digest = hashlib.sha256((ROOT / relative_path).read_bytes()).hexdigest()
        assert actual_digest == expected_digest, relative_path


def test_every_container_stage_uses_the_reviewed_tag_and_immutable_digest() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    stages = _stage_bases(dockerfile)

    assert stages == _EXPECTED_STAGE_BASES
    _assert_internal_image_sources(dockerfile, stages=set(stages))
    _assert_bounded_local_context_sources(dockerfile)
    for image in stages.values():
        tag, separator, digest = image.rpartition("@")
        assert separator == "@"
        assert ":" in tag.rpartition("/")[2]
        assert _SHA256_DIGEST.fullmatch(digest)
        assert ":latest@" not in image


def test_python_build_and_runtime_share_one_exact_base() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    stages = _stage_bases(dockerfile)

    assert stages["python-build"] == stages["runtime"]


def test_provenance_parser_rejects_an_unaliased_or_noncanonical_stage() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    for unreviewed_stage in (
        "FROM busybox:1.37.0\n",
        "from busybox:1.37.0 AS extra\n",
        "FROM busybox:1.37.0 as extra\n",
    ):
        with pytest.raises(AssertionError):
            _stage_bases(f"{unreviewed_stage}{dockerfile}")


def test_provenance_contract_rejects_external_copy_and_mount_images() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    stages = set(_stage_bases(dockerfile))

    for external_source in (
        "COPY --from=busybox:1.37.0 /bin/true /bin/true\n",
        "COPY --from=0 /bin/true /bin/true\n",
        "COPY --from=$UNREVIEWED_IMAGE /bin/true /bin/true\n",
        "RUN --mount=type=bind,from=busybox:1.37.0,target=/source true\n",
        "COPY\t--from=busybox:1.37.0\t/bin/true\t/bin/true\n",
        "RUN\t--mount=type=bind,from=busybox:1.37.0,target=/source\ttrue\n",
        (
            "COPY \\\n"
            "# Docker removes this comment while continuing the instruction.\n"
            "--from=busybox:1.37.0 /bin/true /bin/true\n"
        ),
        (
            "RUN \\\n"
            "# Docker removes this comment while continuing the instruction.\n"
            "--mount=type=bind,from=busybox:1.37.0,target=/source true\n"
        ),
    ):
        with pytest.raises(AssertionError):
            _assert_internal_image_sources(
                f"{dockerfile}\n{external_source}",
                stages=stages,
            )


@pytest.mark.parametrize(
    "instruction",
    [
        "COPY . .",
        "COPY ./ /app",
        'COPY [\".\", \"/app\"]',
        "COPY . /tmp/--from=frontend-build",
        "COPY . --from=frontend-build",
        "COPY server --from=frontend-build /leak",
        "COPY --chown=10001:10001 . --from=frontend-build",
        "COPY local-secret.pem /tmp/local-secret.pem",
        "ADD . /app",
        "ADD https://example.invalid/archive.tgz /tmp/archive.tgz",
        "RUN --mount=type=bind,source=.,target=/context true",
        r"RUN --mou\nt=type=bind,source=.,target=/context true",
        r"RUN --mo\unt=type=bind,source=.,target=/context true",
        'RUN --mou"nt"=type=bind,source=.,target=/context true',
        "RUN --mou'nt'=type=bind,source=.,target=/context true",
        "ONBUILD COPY . /app",
        "ONBUILD ADD . /app",
        "ONBUILD RUN --mount=type=bind,source=.,target=/context true",
    ],
)
def test_provenance_contract_rejects_unreviewed_context_sources(
    instruction: str,
) -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    with pytest.raises(AssertionError):
        _assert_bounded_local_context_sources(f"{dockerfile}\n{instruction}\n")


def test_dockerignore_excludes_local_evidence_cache_and_key_material() -> None:
    dockerignore_files = sorted(
        path.name
        for path in ROOT.iterdir()
        if path.is_file() and path.name.lower().endswith("dockerignore")
    )
    assert dockerignore_files == [".dockerignore"]

    patterns = _dockerignore_patterns(
        (ROOT / ".dockerignore").read_bytes()
    )
    assert patterns == _EXPECTED_DOCKERIGNORE_PATTERNS


@pytest.mark.parametrize(
    "mutate",
    [
        lambda dockerignore: dockerignore.replace(b"\n", b"\r"),
        lambda dockerignore: b"\xef\xbb\xbf" + dockerignore,
        lambda dockerignore: dockerignore + b"\x00",
        lambda dockerignore: dockerignore + b"\xff",
        lambda dockerignore: dockerignore.replace(b"\n", "\u2028".encode()),
    ],
)
def test_dockerignore_contract_rejects_non_docker_line_boundaries(
    mutate: Callable[[bytes], bytes],
) -> None:
    dockerignore = (ROOT / ".dockerignore").read_bytes()

    with pytest.raises((AssertionError, UnicodeDecodeError)):
        _dockerignore_patterns(mutate(dockerignore))


@pytest.mark.parametrize(
    "instruction",
    [
        "ADD . /app",
        "COPY . /app",
        "RUN --mount=type=bind,source=.,target=/context true",
    ],
)
def test_provenance_contract_rejects_a_bom_before_the_first_instruction(
    instruction: str,
) -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    with pytest.raises(AssertionError):
        _assert_bounded_local_context_sources(
            f"\ufeff{instruction}\n{dockerfile}",
        )


@pytest.mark.parametrize(
    "dockerfile_prefix",
    [
        "AD\\\nD . /leak\n",
        "CO\\\nPY . /leak\n",
        "RUN --mou\\\nnt=type=bind,source=.,target=/context true\n",
        "ONBUI\\\nLD ADD . /leak\n",
        "FR\\\nOM busybox:1.37.0 AS evil\n",
        "RUN echo safe\\\\\nADD . /leak\n",
    ],
)
def test_provenance_contract_rejects_all_line_continuations(
    dockerfile_prefix: str,
) -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    with pytest.raises(AssertionError):
        _instructions(f"{dockerfile_prefix}{dockerfile}")


@pytest.mark.parametrize(
    "directive",
    [
        "# syntax=docker/dockerfile:1",
        "# escape=`",
        "# check=skip=all",
        "// syntax=evil.invalid/frontend:latest",
        "//syntax = evil.invalid/frontend:latest",
        "#!/usr/bin/env dockerfile\n// syntax=evil.invalid/frontend:latest",
    ],
)
def test_provenance_contract_rejects_unreviewed_parser_directives(
    directive: str,
) -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    with pytest.raises(AssertionError):
        _instructions(f"{directive}\n{dockerfile}")
