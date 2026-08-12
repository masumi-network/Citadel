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
    HOME=/home/citadel \
    CITADEL_LITE_DATA_ROOT=/data \
    CITADEL_BUILD_ID_PATH=/opt/citadel/build-id \
    HF_HOME=/opt/hf-cache \
    FASTEMBED_CACHE_PATH=/opt/fastembed-cache

RUN groupadd --gid 10001 citadel \
    && useradd --uid 10001 --gid 10001 --home-dir /home/citadel --create-home citadel \
    && install -d -o citadel -g citadel /data
COPY --from=builder /wheels /wheels
RUN install -d /opt/citadel \
    && install -m 0444 /wheels/citadel-build-id /opt/citadel/build-id \
    && python -m pip install /wheels/cognee-1.4.1-py3-none-any.whl \
    "/wheels/citadel_archive-0.5.0-py3-none-any.whl[server]" \
    && python -m pip check \
    && python -c "from importlib.metadata import version; assert (version('cognee'), version('ladybug'), version('qdrant-client')) == ('1.4.1', '0.18.2', '1.19.0')" \
    && rm -rf /wheels
# Bake the BGE tokenizer into the image so chunk sizing uses the real
# tokenizer under a read-only rootfs with no runtime network fetch.
RUN python -c "from transformers import AutoTokenizer; AutoTokenizer.from_pretrained('BAAI/bge-small-en-v1.5')" \
    && chmod -R a+rX /opt/hf-cache
# Bake the fastembed ONNX embedding weights (HF repo
# qdrant/bge-small-en-v1.5-onnx-q, ~64 MiB) so runtime embedding needs no
# network. TextEmbedding() both downloads and writes fastembed's
# files_metadata.json, which the offline load path verifies.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='BAAI/bge-small-en-v1.5')" \
    && chmod -R a+rX /opt/fastembed-cache
# Build-time proof the image embeds offline before it ships. This is the gate
# that would have caught the 2026-08-12 outage: HF_HUB_OFFLINE=1 with only the
# tokenizer baked fails here, at build, not in production.
RUN HF_HUB_OFFLINE=1 python -c "from fastembed import TextEmbedding; v = list(TextEmbedding(model_name='BAAI/bge-small-en-v1.5').embed(['smoke']))[0]; assert len(v) == 384"
# Offline may only be pinned together with BOTH bakes above; the tokenizer
# cache alone froze vector and graph projection in production on 2026-08-12.
ENV HF_HUB_OFFLINE=1
# Bake the Ladybug json extension into $HOME/.lbdb. Ladybug resolves its
# extension directory from $HOME when a Database opens, and it ignores
# LADYBUG_HOME_DIRECTORY: that name is a Citadel convention which reaches
# Ladybug only as a connection-level `CALL home_directory`, far too late for
# ladybug.Database(). cognee_db_workers' OP_OPEN_DATABASE carries no
# home_directory field either, so the open path can never be redirected off
# $HOME. Caching the extension under /data/ladybug-home therefore leaves
# Database() reading an empty $HOME/.lbdb, which is how graph projection froze
# on 2026-08-12 ("Failed to load library ... libjson.lbug_extension: cannot
# open shared object file"). Baking it here also removes the runtime download.
RUN python -c "import ladybug; ladybug.Connection(ladybug.Database('/tmp/lbbake')).execute('INSTALL JSON;')" \
    && rm -rf /tmp/lbbake \
    && chown -R 10001:10001 /home/citadel/.lbdb \
    && chmod -R a+rX /home/citadel/.lbdb
# Build-time proof the graph extension is present and loadable, mirroring the
# embedding gate above. LOAD EXTENSION never installs, so this fails the build
# when the bake is missing instead of failing cognify in production.
RUN python -c "import glob; assert glob.glob('/home/citadel/.lbdb/extension/*/*/json/libjson.lbug_extension'), 'ladybug json extension is not baked'" \
    && python -c "import ladybug; ladybug.Connection(ladybug.Database('/tmp/lbproof')).execute('LOAD EXTENSION JSON;')" \
    && rm -rf /tmp/lbproof

EXPOSE 8000
# No VOLUME instruction: Railway rejects `docker VOLUME` (it uses Railway
# Volumes mounted at /data instead), and for Compose/OCI the data volume is
# declared by the compose file or mounted explicitly, so the anonymous volume
# a bare VOLUME would create is unwanted. Persistence at /data is provided by
# the platform mount, not the image.
HEALTHCHECK --interval=15s --timeout=15s --start-period=120s --retries=5 \
  CMD ["python", "-c", "import os; from urllib.request import Request, urlopen; request = Request('http://127.0.0.1:8000/readyz', headers={'Authorization': 'Bearer ' + os.environ['CITADEL_ADMIN_KEY']}); assert urlopen(request, timeout=12).status == 200"]
USER 10001:10001
ENTRYPOINT ["python", "-m", "kb.lite_runtime"]

FROM runtime AS test

USER root
RUN apt-get update \
    && apt-get install --no-install-recommends -y git nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install "pytest==9.1.1" "pytest-asyncio==1.4.0" "ruff==0.15.15"
COPY --from=builder --chown=citadel:citadel /src /src
WORKDIR /src
USER 10001:10001
# The inherited runtime probe reads CITADEL_ADMIN_KEY and calls /readyz, and a
# test container has neither, so it could only ever report unhealthy while
# re-running a doomed exec every interval. Production keeps its probe.
HEALTHCHECK NONE
ENTRYPOINT []
CMD ["python", "-m", "pytest", "-q", "-m", "not live"]

FROM runtime AS production

USER 10001:10001
