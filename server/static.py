"""Conservative frontend bundle validation and single-process file serving."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from starlette.responses import FileResponse

_ENCODED_PATH_SEPARATOR = re.compile(rb"%(?:00|25|2f|5c)", re.IGNORECASE)
_QUALITY_VALUE = re.compile(r"(?:0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?)\Z")
_RESERVED_APPLICATION_PREFIXES = frozenset(
    {"api", "assets", "docs", "health", "openapi.json", "readyz", "redoc"}
)
_MAX_INDEX_BYTES = 1024 * 1024


def _accepts_html(accept: str) -> bool:
    qualities: dict[str, list[float]] = {"text/html": [], "*/*": []}
    invalid: set[str] = set()
    for item in accept.split(","):
        parts = [part.strip() for part in item.split(";")]
        media_range = parts[0].lower()
        if media_range not in qualities:
            continue
        quality_parameters: list[str] = []
        malformed_quality = False
        for parameter in parts[1:]:
            name, separator, value = parameter.partition("=")
            if name.strip().lower() != "q":
                continue
            if not separator:
                malformed_quality = True
                break
            quality_parameters.append(value.strip())
        if malformed_quality or len(quality_parameters) > 1:
            invalid.add(media_range)
            continue
        quality = quality_parameters[0] if quality_parameters else "1"
        if _QUALITY_VALUE.fullmatch(quality) is None:
            invalid.add(media_range)
            continue
        qualities[media_range].append(float(quality))
    selected = "text/html" if qualities["text/html"] or "text/html" in invalid else "*/*"
    return (
        selected not in invalid
        and bool(qualities[selected])
        and any(quality > 0 for quality in qualities[selected])
    )


class _EntrypointAssetParser(HTMLParser):
    """Collect local runtime assets that must exist for a usable bundle."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)
        if tag == "script":
            source = attributes.get("src")
            if source:
                self.references.append(source)
            return
        if tag != "link":
            return
        relationships = {
            value.lower()
            for value in (attributes.get("rel") or "").split()
        }
        reference = attributes.get("href")
        if "stylesheet" in relationships and reference:
            self.references.append(reference)


class FrontendBundle:
    """A configured Vite-style distribution directory with safe SPA fallback."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve(strict=False)

    @staticmethod
    def _segments(path: str) -> tuple[str, ...] | None:
        if "\x00" in path or "\\" in path or "%" in path:
            return None
        if path == "":
            return ()
        segments = tuple(path.split("/"))
        if any(segment in {"", ".", ".."} for segment in segments):
            return None
        return segments

    @staticmethod
    def _raw_path_is_unambiguous(raw_path: bytes) -> bool:
        return b"\x00" not in raw_path and _ENCODED_PATH_SEPARATOR.search(raw_path) is None

    @staticmethod
    def _is_public_asset_path(segments: tuple[str, ...]) -> bool:
        return (
            len(segments) > 1
            and segments[0] == "assets"
            and all(not segment.startswith(".") for segment in segments)
        )

    def _resolved_file(self, segments: tuple[str, ...]) -> Path | None:
        candidate = self._root.joinpath(*segments).resolve(strict=False)
        try:
            candidate.relative_to(self._root)
        except ValueError:
            return None
        if not candidate.is_file():
            return None
        return candidate

    def _required_asset_is_readable(self, reference: str) -> bool:
        parsed = urlsplit(reference)
        if parsed.scheme or parsed.netloc:
            return False
        if not self._raw_path_is_unambiguous(parsed.path.encode("utf-8")):
            return False
        decoded_path = unquote(parsed.path)
        segments = self._segments(decoded_path.lstrip("/"))
        if not segments or not self._is_public_asset_path(segments):
            return False
        asset = self._resolved_file(segments)
        if asset is None:
            return False
        try:
            with asset.open("rb") as stream:
                return stream.read(1) != b""
        except OSError:
            return False

    def is_ready(self) -> bool:
        """Verify the entrypoint and every local script/stylesheet it requires."""

        try:
            index = self._resolved_file(("index.html",))
            if not self._root.is_dir() or index is None:
                return False
            if index.stat().st_size > _MAX_INDEX_BYTES:
                return False
            entrypoint = index.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return False
        parser = _EntrypointAssetParser()
        try:
            parser.feed(entrypoint)
            parser.close()
        except Exception:
            return False
        return bool(parser.references) and all(
            self._required_asset_is_readable(reference)
            for reference in parser.references
        )

    def response_for(
        self,
        *,
        path: str,
        raw_path: bytes,
        accept: str,
    ) -> FileResponse | None:
        """Return a file/entrypoint only for an unambiguous, frontend-owned path."""

        if not self._raw_path_is_unambiguous(raw_path):
            return None
        segments = self._segments(path)
        if segments is None:
            return None
        if segments and segments[0].lower() in {
            "api",
            "docs",
            "health",
            "openapi.json",
            "readyz",
            "redoc",
        }:
            return None
        public_file_path = segments == ("index.html",) or self._is_public_asset_path(
            segments
        )
        requested_file = self._resolved_file(segments) if public_file_path else None
        if requested_file is not None:
            cache_control = (
                "public, max-age=31536000, immutable"
                if segments and segments[0] == "assets"
                else "no-cache"
            )
            return FileResponse(
                requested_file,
                headers={"Cache-Control": cache_control},
            )
        if segments and (
            segments[0].lower() in _RESERVED_APPLICATION_PREFIXES
            or any("." in segment for segment in segments)
        ):
            return None
        if not _accepts_html(accept):
            return None
        index = self._resolved_file(("index.html",))
        if index is None:
            return None
        return FileResponse(
            index,
            media_type="text/html",
            headers={"Cache-Control": "no-cache"},
        )
