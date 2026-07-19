FROM node:22.22.0-bookworm-slim AS frontend-build

WORKDIR /app
COPY package.json package-lock.json ./
COPY web/package.json ./web/package.json
RUN npm ci
COPY web ./web
RUN npm run build


FROM ghcr.io/astral-sh/uv:0.10.1 AS uv-bin


FROM python:3.12.12-slim-bookworm AS python-build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
COPY --from=uv-bin /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
COPY server ./server
RUN uv sync --frozen --no-dev


FROM python:3.12.12-slim-bookworm AS runtime

ENV BACKCHANNEL_DB_PATH=/data/backchannel.sqlite3 \
    BACKCHANNEL_DEPLOYED_MODE=true \
    PATH=/app/.venv/bin:$PATH \
    PORT=8000 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 backchannel \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin backchannel \
    && install -d -o backchannel -g backchannel /data

WORKDIR /app
COPY --from=python-build /app/.venv ./.venv
COPY server ./server
COPY scripts/start.py scripts/docker_smoke.py ./scripts/
COPY --from=frontend-build /app/web/dist ./web/dist

USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=2).read()"]

CMD ["python", "scripts/start.py"]
