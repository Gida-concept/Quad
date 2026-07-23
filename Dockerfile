# =============================================================================
# Dockerfile — Quad Options Trading Bot
#
# Multi-stage build:
#   Stage 1 (builder): Install build dependencies, pip install requirements
#   Stage 2 (runtime): Minimal runtime image with curl for health checks
#
# Multi-arch: linux/amd64, linux/arm64
# =============================================================================

# ---------------------------------------------------------------------------
# Stage 1: Builder
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build dependencies (gcc for compiling native extensions)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libc6-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy and install pinned dependencies first (leverage Docker layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# ---------------------------------------------------------------------------
# Stage 2: Runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim

WORKDIR /app

# Install runtime-only system dependencies
# curl is required for HEALTHCHECK; tini ensures proper signal handling
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from builder stage
COPY --from=builder /root/.local /root/.local

# Ensure installed scripts are in PATH
ENV PATH=/root/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Copy application source and configuration
COPY src/ ./src/
COPY pyproject.toml requirements.txt setup.py ./
COPY config/config.default.yaml ./config/config.default.yaml

# Install the package in editable mode to register entry points
RUN pip install --no-cache-dir --no-deps -e .

# Create non-root user for security
RUN groupadd -r quad && useradd -r -g quad -d /app -s /sbin/nologin quad \
    && chown -R quad:quad /app

# Create data and log directories (mounted as volumes at runtime)
RUN mkdir -p /app/data /app/logs && chown -R quad:quad /app/data /app/logs

# Switch to non-root user
USER quad

# Volumes for persistent data
VOLUME ["/app/data", "/app/config", "/app/logs"]

# Health check — verifies that the HTTP health server is responsive
# The health server listens on port 9090 by default (configurable via QUAD_HEALTH_PORT)
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:${QUAD_HEALTH_PORT:-9090}/health || exit 1

# Default command: run the quad module entry point
# Override with `quad` CLI by passing: ["quad", "start"]
CMD ["python", "-m", "quad"]
