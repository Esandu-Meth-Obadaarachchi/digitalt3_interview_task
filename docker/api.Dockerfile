# =============================================================================
# The API, and everything it needs to run offline
# =============================================================================
# Two stages. The first installs dependencies and pre-downloads the embedding
# model, the second copies only what runs. The split matters here because the
# build stage needs a compiler and the runtime does not, and shipping a
# compiler in the image is 200MB of attack surface for no benefit.
#
# Python 3.11 rather than 3.12 or 3.13: it is the newest version with settled
# wheels for faiss-cpu, sentence-transformers and ctranslate2, which is the
# same reason the README gives for the local build.
# =============================================================================

FROM python:3.11-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# build-essential for anything without a wheel on this platform. It stays in
# this stage and never reaches the image that runs.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /install

# CPU-only torch, explicitly and first. sentence-transformers pulls torch, and
# the default index serves the CUDA build: about 2.5GB of NVIDIA libraries for
# a container with no GPU. Installing the CPU wheel first means the resolver
# already has torch satisfied when it reaches sentence-transformers.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The embedding model, baked in. Retrieval is the default path and a container
# downloading 90MB on its first question is a container that fails behind a
# proxy or on an aeroplane. Whisper's weights are deliberately NOT baked: audio
# is optional, its model is 140MB, and it caches to a mounted volume on first
# use.
ENV HF_HOME=/opt/models
RUN python -c "from sentence_transformers import SentenceTransformer; \
SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')"


# =============================================================================
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend \
    HF_HOME=/opt/models \
    HF_HUB_OFFLINE=0

# curl is for the healthcheck compose depends on, nothing else.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 agent

COPY --from=build /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=build /usr/local/bin /usr/local/bin
COPY --from=build /opt/models /opt/models

WORKDIR /app
COPY backend/ backend/
COPY eval/ eval/
COPY scripts/ scripts/
COPY sample_data/ sample_data/
COPY conftest.py pytest.ini ./
# The published consumer contract, not documentation. See .dockerignore.
COPY docs/outcome_schema.json docs/

# The store and the write logs are the only things that outlive a container, so
# they are the only things on a volume.
RUN mkdir -p /app/data /app/write_log && chown -R agent:agent /app /opt/models
USER agent

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=6 \
  CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000"]
