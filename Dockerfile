# Update each readable tag and its reviewed OCI index digest together with
# tests/release/test_container_provenance.py.
FROM node:22.22.0-bookworm-slim@sha256:dd9d21971ec4395903fa6143c2b9267d048ae01ca6d3ea96f16cb30df6187d94 AS frontend-build

WORKDIR /app
COPY package.json package-lock.json ./
COPY web/package.json ./web/package.json
RUN npm ci
COPY web ./web
RUN npm run build


FROM ghcr.io/astral-sh/uv:0.10.1@sha256:452e02b117acd2d4eb3ba81a607bed9733b101b6c49492e352b1973463389012 AS uv-bin


FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c AS python-build

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY --from=uv-bin /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
COPY server ./server
RUN uv sync --frozen --no-dev


FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c AS runtime

ENV BACKCHANNEL_DB_PATH=/data/backchannel.sqlite3 BACKCHANNEL_DEPLOYED_MODE=true PATH=/app/.venv/bin:$PATH PORT=8000 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 backchannel && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin backchannel && install -d -o backchannel -g backchannel /data

WORKDIR /app
COPY --from=python-build /app/.venv ./.venv
COPY server ./server
COPY scripts/start.py scripts/docker_smoke.py ./scripts/
COPY --from=frontend-build /app/web/dist ./web/dist

USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=2).read()"]

CMD ["python", "scripts/start.py"]
