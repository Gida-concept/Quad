# Deployment and Operations Guide

---

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.10+ | Required |
| Docker & Docker Compose | Docker 24+, Compose 2.20+ | Optional, recommended for production |
| OKX USDT Perpetual Account | -- | API keys with trading permissions (V5 API, instType=SWAP) |
| Telegram Bot Token | -- | From @BotFather (required for Telegram interface) |
| NTP Sync | -- | Clock must be within 1 second of UTC |
| Memory | 256 MB minimum | 512 MB recommended |
| Disk | 1 GB minimum | SSD recommended for database performance |

---

## Quick Start

### Installation

```bash
# Install from source
git clone https://github.com/your-org/quad.git
cd quad
pip install -e .

# Verify installation
quad --version
quad --help
```

### Configuration

```bash
# Create config directory
mkdir -p config data

# Copy environment template
cp .env.example .env

# Edit .env with your OKX API keys
# (Only needed for live or demo trading)

# Create local config overrides
cp config/config.yaml config/config.local.yaml
# Edit config.local.yaml with your preferences
```

### Running

```bash
# Start in dry-run mode (safest first step)
quad start --dry-run

# Check status
quad status

# Stop the bot
quad stop
```

### Docker Deployment (Recommended for Production)

```bash
# Build and start
docker-compose up -d

# Check logs
docker-compose logs -f

# Stop
docker-compose down
```

---

## Docker Deployment

### Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install the package
COPY . .
RUN pip install -e .

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:9090/health || exit 1

# Default command
CMD ["quad", "start"]
```

### docker-compose.yml

```yaml
version: "3.8"

services:
  quad:
    build: .
    container_name: quad
    restart: unless-stopped
    ports:
      - "9090:9090"  # Health check port
    volumes:
      - ./config:/app/config:ro
      - ./data:/app/data
      - ./.env:/app/.env:ro
    environment:
      - QUAD_DATA_DIR=/app/data
      - QUAD_CONFIG_DIR=/app/config
      - QUAD_HEALTH_PORT=9090
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
```

### Environment Setup by Tier

**Development:**
```bash
# Local install, dry-run mode
quad start --dry-run
```

**Staging (demo):**
```bash
# OKX demo trading (default)
# OKX_TESTNET=true in .env (or omit — demo is the default)
docker-compose up -d
```

**Production (live):**
```bash
# OKX live trading
# Set OKX_TESTNET=false in .env
# Ensure API keys have trading-only permissions
docker-compose up -d
```

---

---

## Telegram Bot Considerations

### Polling Mode (Default)

Quad uses Telegram Bot API **polling mode** by default. This is simpler than webhook mode because:

- No public HTTPS endpoint required
- No SSL certificate configuration
- Works behind NAT, firewalls, and VPNs
- Automatically reconnects on connection loss

The bot polls Telegram's API every 1 second for updates. This is handled by the `python-telegram-bot` library internally and requires no configuration.

### Webhook Mode (Alternative)

For lower latency, webhook mode can be used. This requires a public HTTPS URL where Telegram can send updates:

```bash
# Set in .env to disable polling
TELEGRAM_POLLING=false

# The bot will start an HTTPS webhook server
# Requires SSL certificate and public domain
```

### Keeping the Bot Alive

The Telegram bot is part of the Quad process -- it runs in the same asyncio event loop. As long as Quad is running, Telegram polling is active:

- **Direct deployment:** Use `tmux` or `screen` to keep the bot running in the background
- **Docker deployment:** The bot auto-restarts via `restart: unless-stopped`
- **Systemd:** A systemd service file ensures the bot starts on boot and restarts on failure

### Bot Token Management

- Always store the token in `.env` -- never hardcode it
- Rotate the token periodically via @BotFather
- If compromised, regenerate immediately -- old token is invalidated
- Use separate bot tokens for production and test instances

### Chat ID Whitelist

The bot only responds to whitelisted chat IDs (configured via `TELEGRAM_NOTIFICATION_CHAT_ID`):

```bash
# In .env
TELEGRAM_NOTIFICATION_CHAT_ID=123456789
```

You can find your chat ID by messaging [@userinfobot](https://t.me/userinfobot) on Telegram. Whitelist entries are checked by the `TelegramFilter` on every incoming message.

### docker-compose.yml Environment

When deploying with Docker, pass the Telegram configuration via environment variables:

```yaml
services:
  quad:
    build: .
    container_name: quad
    restart: unless-stopped
    ports:
      - "9090:9090"
    volumes:
      - ./config:/app/config:ro
      - ./data:/app/data
      - ./.env:/app/.env:ro
    environment:
      - QUAD_DATA_DIR=/app/data
      - QUAD_CONFIG_DIR=/app/config
      - QUAD_HEALTH_PORT=9090
      - TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
```

---

## SQLite Database Management

### Connection

Quad uses SQLite for persistence, configured via `config/config.yaml` or the `DATABASE_URL` environment variable:

```yaml
persistence:
  dsn: "${DATABASE_URL:-data/quad.db}"
```

### Backup

```bash
# Manual backup (safe to run while bot is running)
# SQLite supports hot backups via the .backup command or file copy
cp data/quad.db data/backups/quad_$(date +%Y%m%d_%H%M%S).db

# Or using SQLite's backup command
sqlite3 data/quad.db ".backup 'data/backups/quad_$(date +%Y%m%d_%H%M%S).db'"
```

### Restore

```bash
# Stop the bot first
quad stop

# Restore from backup
cp data/backups/quad_20260707_120000.db data/quad.db

# Restart
quad start
```

### Database Maintenance

```bash
# VACUUM to reclaim space (run while bot is stopped)
sqlite3 data/quad.db "VACUUM;"

# Check database size
ls -lh data/quad.db

# List table sizes
sqlite3 data/quad.db "SELECT name, (SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=m.name) as rows FROM sqlite_master m WHERE type='table' ORDER BY name;"
```

### Connection Pooling

Quad uses aiosqlite with a single connection pool (SQLite doesn't benefit from multiple concurrent writers). The pool is created on startup and connections are acquired/released per query.

---

## Logging

### Log Files

| File | Location | Content |
|---|---|---|
| Main log | `data/logs/quad.log` | All bot operations |
| Error log | `data/logs/error.log` | Errors and warnings only |

### Log Format

By default, logs are structured JSON for easy parsing:

```json
{"timestamp": "2026-07-07T10:00:00Z", "level": "INFO", "event": "trading_cycle", "cycle_time_ms": 950, "state": "ACTIVE"}
{"timestamp": "2026-07-07T10:00:00Z", "level": "INFO", "event": "decision", "action": "ENTER", "strategy": "covered_call", "contract": "BTC-27AUG24-65000-C"}
{"timestamp": "2026-07-07T10:00:01Z", "level": "WARN", "event": "risk_check", "check": "margin_sufficiency", "result": "PASS", "available": 5000, "required": 450}
```

For human-readable output:

```bash
# Follow logs
quad logs --follow

# Filter by level
quad logs --level ERROR

# Or use shell tools on the log file
tail -f data/logs/quad.log | jq '.'
```

### Log Rotation

When configured via Docker, log rotation is handled by the Docker daemon:

```yaml
logging:
  driver: "json-file"
  options:
    max-size: "10m"
    max-file: "3"
```

For direct deployment, logs are rotated automatically when they reach the configured max size (default: 100 MB).

---

## Monitoring

### Health Check Server

Quad runs a lightweight HTTP health check server on port 9090 (configurable). This is used for Docker health checks and external monitoring.

```bash
# Health check
curl http://localhost:9090/health

# Readiness check
curl http://localhost:9090/ready

# Prometheus metrics
curl http://localhost:9090/metrics
```

### Key Metrics

| Metric | Type | Description |
|---|---|---|
| `quad_uptime_seconds` | Gauge | Bot uptime in seconds |
| `quad_positions_open` | Gauge | Currently open positions |
| `quad_portfolio_value_usdt` | Gauge | Current portfolio value |
| `quad_drawdown_percent` | Gauge | Current drawdown |
| `quad_trades_total` | Counter | Total trades executed |
| `quad_errors_total` | Counter | Total errors |
| `quad_cycle_time_ms` | Histogram | Trading cycle latency |
| `quad_decisions_total` | Counter | Total decisions made |

---

## Security Hardening

| Area | Action | Notes |
|---|---|---|
| Firewall | Allow only port 22 (SSH) and 9090 (health, internal only) | Never expose 9090 to the public internet |
| OKX API Keys | Create keys with trading only (disable withdrawals) | Rotate keys every 90 days |
| Docker Security | Run container as non-root | Add `user: "1000:1000"` to compose services |
| Database Access | Restrict SQLite file permissions to trusted users | Use filesystem permissions (chmod 600) |
| Secrets | Store API keys in `.env`, never in code | Keep `.env` out of version control |

---

## Scaling Considerations

| Scenario | Recommendation |
|---|---|
| Multiple underlyings | Increase `max_positions` in config, ensure adequate margin |
| Higher frequency trading | Reduce `max_cycle_interval`, monitor cycle time |
| Multiple bot instances | Use separate data directories and databases |
| Large historical data | Configure `market_data.historical.max_cache_size_gb` |
| Database size growth | Monitor with `pg_database_size()`, VACUUM ANALYZE periodically, archive old data |
