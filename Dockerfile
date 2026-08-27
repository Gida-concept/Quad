# =============================================================================
# Dockerfile — Quad USD-M Futures Trading Bot
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
FROM python:3.12-slim AS runtime

WORKDIR /app

# Install runtime-only system dependencies
# curl is required for HEALTHCHECK
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from builder stage
COPY --from=builder /root/.local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /root/.local/bin /usr/local/bin

# Environment
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Copy application source and configuration
COPY src/ ./src/
COPY pyproject.toml requirements.txt ./
COPY config/config.yaml ./config/config.yaml

# Install the package to register entry points
RUN pip install --no-cache-dir --no-deps -e .

# Create non-root user for security
RUN groupadd -r quad && useradd -r -g quad -d /app -s /sbin/nologin quad \
    && chown -R quad:quad /app

# Create data and log directories
RUN mkdir -p /app/data /app/logs && chown -R quad:quad /app/data /app/logs

# Switch to non-root user
USER quad

# Expose health check port
EXPOSE 9090

# Volumes for persistent data
VOLUME ["/app/data", "/app/config", "/app/logs"]

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:${QUAD_HEALTH_PORT:-9090}/health || exit 1

CMD ["python", "-m", "quad"]

# ---------------------------------------------------------------------------
# Stage 3: Production alias (used by Dokploy / third-party deploy tools)
# ---------------------------------------------------------------------------
FROM runtime AS production
