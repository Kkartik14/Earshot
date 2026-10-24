# 1) Build the viewer SPA (static assets served by the API in the runtime stage).
FROM node:26-slim AS web

WORKDIR /repo
RUN corepack enable
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY packages/viewer-ui/package.json ./packages/viewer-ui/package.json
COPY apps/viewer/package.json ./apps/viewer/package.json
RUN pnpm install --frozen-lockfile \
    --filter @earshot/viewer... \
    --filter earshot
COPY apps/viewer/index.html \
    apps/viewer/tsconfig.json \
    apps/viewer/tsconfig.app.json \
    apps/viewer/tsconfig.node.json \
    apps/viewer/vite.config.ts \
    ./apps/viewer/
COPY apps/viewer/src ./apps/viewer/src
COPY packages/viewer-ui/src ./packages/viewer-ui/src
RUN pnpm --filter @earshot/viewer build

# 2) Build the Python wheel.
FROM python:3.14-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY pyproject.toml README.md ./
COPY packages/sdk-python ./packages/sdk-python
# The server image runs on SQLite or Postgres, stores blobs on disk or in S3, and
# analyses recordings (signals: the audible gap at the agent).
RUN python -m pip wheel --wheel-dir /wheels '.[server,postgres,s3,signals]'

# 3) Runtime: one process serving the SPA and the API from a single port.
FROM python:3.14-slim AS runtime

ENV EARSHOT_DATA_DIR=/data \
    ORT_DISABLE_TELEMETRY=1 \
    EARSHOT_PORT=4319 \
    EARSHOT_WEB_DIR=/app/web \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --system --gid 999 earshot \
    && useradd --system --uid 999 --gid earshot --home-dir /nonexistent --shell /usr/sbin/nologin earshot \
    && install --directory --owner earshot --group earshot --mode 0700 /data
# Install from the builder's wheels through a bind mount, so they never become a layer.
RUN --mount=type=bind,from=builder,source=/wheels,target=/wheels \
    python -m pip install --no-index --find-links=/wheels \
    'earshot-observability[server,postgres,s3,signals]'
# The built viewer, baked in and served from EARSHOT_WEB_DIR.
COPY --from=web /repo/apps/viewer/dist /app/web

USER earshot:earshot
VOLUME ["/data"]
EXPOSE 4319
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:4319/readyz', timeout=2).read()"]

ENTRYPOINT ["earshot"]
CMD ["serve"]

# Declared last so a new version only changes image metadata, not cached layers.
ARG EARSHOT_VERSION=dev
LABEL org.opencontainers.image.title="Earshot" \
    org.opencontainers.image.description="Self-hosted Earshot server and viewer for voice-agent observability" \
    org.opencontainers.image.source="https://github.com/Kkartik14/Earshot" \
    org.opencontainers.image.licenses="Apache-2.0" \
    org.opencontainers.image.version="${EARSHOT_VERSION}"
