####################################
# Cultivation Intelligence — Production Dockerfile
# Base: python:3.11-slim (Debian Bookworm)
# Target: linux/amd64 (on-premises NUC / server)
####################################

FROM python:3.11-slim AS base

# ── Build arguments ──────────────────────────────────────────────────────────
ARG APP_VERSION=0.1.0
ARG BUILD_DATE
ARG GIT_COMMIT

# ── OCI image labels ──────────────────────────────────────────────────────────
LABEL org.opencontainers.image.title="Cultivation Intelligence API"
LABEL org.opencontainers.image.description="AI-driven cultivation intelligence platform for Legacy Ag Limited"
LABEL org.opencontainers.image.vendor="Legacy Ag Limited"
LABEL org.opencontainers.image.version="${APP_VERSION}"
LABEL org.opencontainers.image.created="${BUILD_DATE}"
LABEL org.opencontainers.image.revision="${GIT_COMMIT}"
LABEL org.opencontainers.image.licenses="MIT"

# ── System packages ────────────────────────────────────────────────────────────
# curl          : used by HEALTHCHECK
# build-essential: required to compile LightGBM C extensions from source
# libgomp1      : OpenMP runtime — required by LightGBM at runtime
# libpq-dev     : PostgreSQL client headers for psycopg2 / asyncpg
# git           : required by some pip installs that reference VCS URLs
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        build-essential \
        libgomp1 \
        libpq-dev \
        git \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ── Working directory ──────────────────────────────────────────────────────────
WORKDIR /app

# ── Python environment settings ────────────────────────────────────────────────
# PYTHONDONTWRITEBYTECODE : don't write .pyc files (saves space in container)
# PYTHONUNBUFFERED        : force stdout/stderr flush (critical for log streaming)
# PIP_NO_CACHE_DIR        : don't cache pip downloads in the image layer
# PIP_DISABLE_PIP_VERSION_CHECK: suppress upgrade nag in build output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ── Install Python dependencies (layer-cached separately from source code) ─────
# Copy only the project metadata first so that a change to src/ does NOT
# invalidate this expensive layer.
COPY pyproject.toml ./

# Install production dependencies only (no dev extras)
RUN pip install --upgrade pip setuptools wheel \
    && pip install -e ".[production]"

# ── Copy application source ────────────────────────────────────────────────────
COPY src/ ./src/

# ── Runtime environment defaults ───────────────────────────────────────────────
# These are sane defaults; all should be overridden in docker-compose / k8s
# via actual environment variables or secrets.
ENV ENVIRONMENT=production \
    LOG_LEVEL=INFO \
    PORT=8000 \
    WORKERS=2 \
    # Uvicorn settings
    UVICORN_HOST=0.0.0.0 \
    UVICORN_PORT=8000 \
    UVICORN_WORKERS=2 \
    UVICORN_ACCESS_LOG=1 \
    # Application
    APP_VERSION=${APP_VERSION} \
    # Timezone (NZST/NZDT)
    TZ=Pacific/Auckland

# ── Non-root user for security ─────────────────────────────────────────────────
# Running as root inside a container is a security anti-pattern.
# Create a system user 'app' with no login shell and no home directory.
RUN groupadd --system app && useradd --system --no-create-home --gid app app
USER app

# ── Expose application port ────────────────────────────────────────────────────
EXPOSE 8000

# ── Health check ───────────────────────────────────────────────────────────────
# Polls /health every 30 s; allows 10 s startup before first check.
# 3 consecutive failures → container marked unhealthy.
HEALTHCHECK \
    --interval=30s \
    --timeout=10s \
    --start-period=15s \
    --retries=3 \
    CMD curl --fail --silent --show-error \
        "http://localhost:${UVICORN_PORT}/health" \
        || exit 1

# ── Default command ────────────────────────────────────────────────────────────
# Use exec form to ensure signals (SIGTERM) are delivered directly to uvicorn.
# Workers are set to 2 for the on-premises NUC; scale via UVICORN_WORKERS env var.
CMD ["uvicorn", \
     "src.app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "2", \
     "--access-log", \
     "--log-level", "info", \
     "--timeout-keep-alive", "30"]
