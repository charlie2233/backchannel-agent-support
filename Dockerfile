FROM node:22.22.0-bookworm-slim AS frontend-build

WORKDIR /app
COPY package.json package-lock.json ./
COPY web/package.json ./web/package.json
RUN npm ci
COPY web/index.html web/tsconfig.json web/vite.config.ts ./web/
COPY web/src ./web/src
RUN npm run build


FROM ghcr.io/astral-sh/uv:0.10.1 AS uv-bin


FROM python:3.12.12-slim-bookworm AS python-dependencies

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
COPY --from=uv-bin /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project


FROM python:3.12.12-slim-bookworm AS runtime

# Public runtime defaults stay hardened. Deterministic SDK smoke may override
# deployed=false only for an unpublished loopback process and execute this
# image's smoke client inside the same isolated container namespace.
ENV BACKCHANNEL_DB_PATH=/data/backchannel.sqlite3 \
    BACKCHANNEL_DEPLOYED=true \
    BACKCHANNEL_FRONTEND_DIST_PATH=/app/web/dist \
    PATH=/app/.venv/bin:$PATH \
    PORT=8000 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 backchannel \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin backchannel \
    && install -d -o backchannel -g backchannel /data

WORKDIR /app
COPY --from=python-dependencies /app/.venv ./.venv
COPY server ./server
COPY scripts/start.py scripts/docker_smoke.py ./scripts/
COPY --from=frontend-build /app/web/dist ./web/dist

USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; port = int(os.environ.get('PORT', '8000')); urllib.request.urlopen(f'http://127.0.0.1:{port}/readyz', timeout=2).read()"]

CMD ["python", "scripts/start.py"]
