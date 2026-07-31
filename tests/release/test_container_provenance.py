from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_PARSER_DIRECTIVE = re.compile(r"#\s*(?:syntax|escape|check)\s*=", re.IGNORECASE)
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


def _instructions(dockerfile: str) -> list[str]:
    instructions: list[str] = []
    pending = ""
    for line in dockerfile.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            assert _PARSER_DIRECTIVE.match(stripped) is None
            continue
        pending = f"{pending} {stripped}".strip()
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        instructions.append(pending)
        pending = ""
    assert not pending
    return instructions


def _stage_bases(dockerfile: str) -> dict[str, str]:
    stages: dict[str, str] = {}
    for instruction in _instructions(dockerfile):
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
            sources = re.findall(r"(?:^|\s)--from=([^\s]+)", instruction)
            assert "--from" not in instruction or len(sources) == 1
            assert all(source in stages for source in sources)
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


def test_every_container_stage_uses_the_reviewed_tag_and_immutable_digest() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    stages = _stage_bases(dockerfile)

    assert stages == _EXPECTED_STAGE_BASES
    _assert_internal_image_sources(dockerfile, stages=set(stages))
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
    "directive",
    [
        "# syntax=docker/dockerfile:1",
        "# escape=`",
        "# check=skip=all",
    ],
)
def test_provenance_contract_rejects_unreviewed_parser_directives(
    directive: str,
) -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    with pytest.raises(AssertionError):
        _instructions(f"{directive}\n{dockerfile}")
