# syntax=docker/dockerfile:1.7

FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /src

RUN python -m pip install build==1.5.0 hatchling==1.31.0
COPY . .
RUN python scripts/build_secure_cognee.py --output /wheels
RUN python -m build --no-isolation --wheel --outdir /wheels .
RUN sha256sum /wheels/citadel_archive-0.5.0-py3-none-any.whl \
    | cut -d ' ' -f1 > /wheels/citadel-build-id

FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c AS runtime

LABEL org.opencontainers.image.source="https://github.com/masumi-network/Citadel" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.title="Citadel Archive Lite"

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    CITADEL_LITE_DATA_ROOT=/data \
    CITADEL_BUILD_ID_PATH=/opt/citadel/build-id

RUN groupadd --gid 10001 citadel \
    && useradd --uid 10001 --gid 10001 --home-dir /home/citadel --create-home citadel \
    && install -d -o citadel -g citadel /data
COPY --from=builder /wheels /wheels
RUN install -d /opt/citadel \
    && install -m 0444 /wheels/citadel-build-id /opt/citadel/build-id \
    && python -m pip install /wheels/cognee-1.4.1-py3-none-any.whl \
    "/wheels/citadel_archive-0.5.0-py3-none-any.whl[server]" \
    && python -m pip check \
    && python -c "import cognee, kb; assert cognee.__version__ == '1.4.1'" \
    && rm -rf /wheels

EXPOSE 8000
VOLUME ["/data"]
HEALTHCHECK --interval=15s --timeout=5s --start-period=120s --retries=5 \
  CMD ["python", "-c", "from urllib.request import urlopen; assert urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200"]
ENTRYPOINT ["python", "-m", "kb.lite_runtime"]
