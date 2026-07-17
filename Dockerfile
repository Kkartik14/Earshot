FROM python:3.11-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY pyproject.toml README.md ./
COPY packages/sdk-python ./packages/sdk-python
RUN python -m pip wheel --no-deps --wheel-dir /wheels .

FROM python:3.11-slim AS runtime

ENV EARSHOT_DATA_DIR=/data \
    EARSHOT_PORT=4319 \
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

USER earshot:earshot
VOLUME ["/data"]
EXPOSE 4319
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:4319/readyz', timeout=2).read()"]

ENTRYPOINT ["earshot"]
CMD ["serve"]
