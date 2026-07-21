# 1) Build the viewer SPA (static assets served by the API in the runtime stage).
FROM node:22-slim AS web

WORKDIR /repo
RUN corepack enable
COPY . .
RUN pnpm install --frozen-lockfile \
    && pnpm --filter @earshot/viewer build

# 2) Build the Python wheel.
FROM python:3.11-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY pyproject.toml README.md ./
COPY packages/sdk-python ./packages/sdk-python
RUN python -m pip wheel --no-deps --wheel-dir /wheels .

# 3) Runtime: one process serving the SPA and the API from a single port.
FROM python:3.11-slim AS runtime

ENV EARSHOT_DATA_DIR=/data \
    EARSHOT_PORT=4319 \
    EARSHOT_WEB_DIR=/app/web \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --system earshot \
    && useradd --system --gid earshot --home-dir /nonexistent --shell /usr/sbin/nologin earshot \
    && install --directory --owner earshot --group earshot --mode 0700 /data
COPY --from=builder /wheels /wheels
RUN python -m pip install /wheels/*.whl \
    && rm -rf /wheels
# The built viewer, baked in and served from EARSHOT_WEB_DIR.
COPY --from=web /repo/apps/viewer/dist /app/web

USER earshot:earshot
VOLUME ["/data"]
EXPOSE 4319
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:4319/readyz', timeout=2).read()"]

ENTRYPOINT ["earshot"]
CMD ["serve"]
