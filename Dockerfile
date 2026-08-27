# =============================================================================
# Dockerfile — Quad USD-M Futures Trading Bot
#
# Multi-stage build for a lightweight Python runtime.
# Proxy support is handled via environment variables (HTTP_PROXY/HTTPS_PROXY)
# configured in docker-compose.yml — no VPN or NET_ADMIN capability required.
# =============================================================================

# ---------------------------------------------------------------------------
# Stage 1: Builder
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libc6-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# ---------------------------------------------------------------------------
# Stage 2: Runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

WORKDIR /app

# Install runtime deps: curl for health checks
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
COPY start.sh ./start.sh

# Make startup script executable
RUN chmod +x start.sh

# Install the package to register entry points
RUN pip install --no-cache-dir --no-deps -e .

# Create non-root user for security
RUN groupadd -r quad && useradd -r -g quad -d /app -s /sbin/nologin quad \
    && chown -R quad:quad /app

# Create data and log directories
RUN mkdir -p /app/data /app/logs \
    && chown -R quad:quad /app/data /app/logs

# Drop privileges — no NET_ADMIN needed without VPN
USER quad

# Expose health check port
EXPOSE 9090

# Volumes for persistent data
VOLUME ["/app/data", "/app/config", "/app/logs"]

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:${QUAD_HEALTH_PORT:-9090}/health || exit 1

# Use startup script (starts bot with optional proxy env vars)
CMD ["./start.sh"]

# ---------------------------------------------------------------------------
# Stage 3: Production alias (used by Dokploy / third-party deploy tools)
# ---------------------------------------------------------------------------
FROM runtime AS production
