# Deployment and Operations Guide

---

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.10+ | Required (image ships 3.12) |
| Docker & Docker Compose | Docker 24+, Compose 2.20+ | Optional, recommended for production |
| Bybit USDT Perpetual Account | -- | API keys with trading permissions (V5 API, category=linear; testnet at https://api-testnet.bybit.com) |
| Telegram Bot Token | -- | From @BotFather (required for Telegram interface) |
| NTP Sync | -- | Clock must be within 1 second of UTC |
| Memory | 256 MB minimum | 512 MB recommended (compose caps at 512M) |
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
# Create data dir
mkdir -p data

# Copy environment template
cp .env.example .env

# Edit .env with your Bybit API keys (BYBIT_API_KEY / BYBIT_API_SECRET)
# (Only needed for live or testnet trading)

# Edit the single config file in place
$EDITOR config/config.yaml
```

There is exactly **one** config file, `config/config.yaml` — no
`config.local.yaml` overlay and no per-domain split files. Local, uncommitted
overrides go in `.env.local` (a dotenv file, not a YAML overlay). See
[docs/configuration.md](configuration.md).

### Running

```bash
# Start in dry-run mode (safest first step) — runs in the FOREGROUND
quad start --dry-run

# ...or equivalently
quad start

# Inspect while it runs (separate shell)
quad status      # config snapshot: mode, dry-run, testnet, leverage
quad health      # HTTP query of the running bot's /health endpoint
quad balance
quad positions

# Stop: press Ctrl+C (SIGINT) or send SIGTERM.
# `quad stop` explains how; it is not a daemon kill.
quad stop
```

The bot has **no daemon mode and no PID file**. It runs in the foreground and
blocks until interrupted, then shuts down gracefully (see
[Foreground operation and shutdown](#foreground-operation-and-shutdown)).

### Docker Deployment (Recommended for Production)

```bash
# Build and start (bot + postgres)
docker compose up -d

# Check logs
docker compose logs -f quad

# Stop (sends SIGTERM; 30s grace period)
docker compose stop quad

# Tear down
docker compose down
```

---

## Start Modes

`quad start` exposes a single `--dry-run/--live` pair (`--dry-run` is the
default, `-n` is the short dry-run flag) — `src/quad/cli/app.py:574-610`:

```bash
quad start --dry-run    # default; the only safe first step
quad start --live       # permit real orders
```

`--live` is **refused** unless both safety switches are already off. Each
failure exits with code 1 and an explicit message (`app.py:594-608`):

| Condition | Message |
|---|---|
| `exchange.testnet: true` | `❌ --live refused: exchange.testnet is still true.` |
| `_dry_run: true` | `❌ --live refused: _dry_run is still true.` |

So `--live` requires **both** of these first:

```yaml
exchange:
  testnet: false
_dry_run: false
```

or the equivalent env vars `BYBIT_TESTNET=false` and `QUAD_DRY_RUN=false`.
`quad start --live` only *permits* orders; it does not override config, and the
engine/adapter guards still fail closed on `_dry_run` and on testnet. Reaching
real money also requires the Go-Live gates in
[docs/go-live-plan.md](go-live-plan.md) — `_mode`/`QUAD_MODE` must also be
`bybit` or `dry_run`; any other value is a hard startup error
(`src/quad/orchestrator/orchestrator.py:518`).

`quad run` is the same foreground launcher without the `--dry-run/--live` check
(`app.py:561`).

---

## Foreground Operation and Shutdown

`quad start` blocks in `asyncio.run(orchestrator.run_forever())`
(`src/quad/cli/app.py:144`). There is no PID file, no background daemon, and
nothing to `kill` by number.

| Environment | How to stop |
|---|---|
| Terminal | Ctrl+C (SIGINT) |
| systemd / supervisor | `systemctl stop quad` (sends SIGTERM) |
| Docker | `docker compose stop quad` (SIGTERM, `stop_grace_period: 30s`) |
| Remote kill switch | Telegram `/kill` — halts new entries and cancels open orders; **open positions remain** |

SIGINT and SIGTERM are both handled (`orchestrator.py:353` `_setup_signal_handlers`;
on Windows, where `loop.add_signal_handler` is unsupported, asyncio's default
Ctrl+C `CancelledError` path is used). Shutdown is **reverse dependency order**
(`orchestrator.py:1344` `_shutdown_all`), each step individually try/excepted so
one failure cannot strand the rest:

health server → Telegram bot → strategies → execution engine → risk manager →
market data engine → exchange adapter → error-log sink → database → Groq client

The error sink flushes *before* the database disconnects (`orchestrator.py:1400`)
so queued `error_logs` rows are not lost. Allow the 30s compose grace period to
elapse before escalating to `SIGKILL`.

---

## Docker Deployment

> The image is **Python-only** — there is no Node.js runtime and no MCP server.
> Bybit V5 is accessed via the `pybit` SDK (see `requirements.txt`).
> Proxy support is via `HTTP_PROXY` / `HTTPS_PROXY` environment variables
> passed through `start.sh`; no VPN or `NET_ADMIN` capability is required.

### Dockerfile

`Dockerfile` is a **multi-stage** build with a third alias stage for deploy
tools:

| Stage | Base | Purpose |
|---|---|---|
| `builder` | `python:3.12-slim` | Installs `gcc` + `libc6-dev`, then `pip install --user -r requirements.txt` |
| `runtime` | `python:3.12-slim` | Copies the built site-packages, installs `curl` + `ca-certificates`, installs the package, runs as non-root `quad` |
| `production` | `runtime` | Alias for Dokploy / third-party deploy tooling (`Dockerfile:85`) |

Key runtime properties:

| Property | Value | Source |
|---|---|---|
| Working dir | `/app` | `Dockerfile:30` |
| Source copied | `src/`, `pyproject.toml`, `requirements.txt`, `config/config.yaml`, `start.sh` | `Dockerfile:47-50` |
| Entrypoint | `./start.sh` → `exec python -m quad "$@"` | `Dockerfile:80`, `start.sh:13` |
| User | non-root `quad` (system user, `/sbin/nologin`) | `Dockerfile:59-67` |
| Env | `PYTHONUNBUFFERED=1`, `PYTHONDONTWRITEBYTECODE=1` | `Dockerfile:43` |
| Exposed port | `9090` | `Dockerfile:70` |
| Volumes | `/app/data`, `/app/config`, `/app/logs` | `Dockerfile:73` |
| Healthcheck | `curl -f http://localhost:${QUAD_HEALTH_PORT:-9090}/health`, 30s interval, 5s timeout, 15s start period, 3 retries | `Dockerfile:76-77` |
| Privileges | none (`NET_ADMIN` not needed) | `Dockerfile:66` |

`start.sh` is a 13-line wrapper that execs `python -m quad "$@"` so any extra
args are passed through and `python` becomes PID 1's process (correct signal
delivery).

### docker-compose.yml

The shipped compose file defines **two** services and three named volumes.

`quad` service:

| Setting | Value |
|---|---|
| `container_name` | `quad-bot` |
| `restart` | `unless-stopped` |
| `depends_on` | `postgres` with `condition: service_healthy` |
| Volumes | `./config:/app/config:ro` (read-only), `quad_data:/app/data`, `quad_logs:/app/logs` |
| Healthcheck | `curl -f http://localhost:${QUAD_HEALTH_PORT:-9090}/health` (30s / 5s / 3 retries / 30s start period) |
| Logging | `json-file`, `max-size: 10m`, `max-file: 3` |
| Other | `stop_grace_period: 30s`, `init: true`, memory cap `512M`, CPU cap `1.0` |

**Required** environment variables — compose fails fast with `:?` if unset:

| Variable | Used for |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram bot (`docker-compose.yml:46`) |
| `QUAD_CREDENTIAL_KEY` | Fernet key for Bybit credential encryption (`docker-compose.yml:55`) |
| `POSTGRES_PASSWORD` | The `postgres` service (`docker-compose.yml:100`) |

`QUAD_CREDENTIAL_KEY` can be generated with:

```bash
python -c "from quad.security.secrets import generate_key; print(generate_key())"
```

**Optional but important** environment variables:

| Variable | Default in compose | Notes |
|---|---|---|
| `QUAD_MODE` | `bybit` | `bybit` or `dry_run` only |
| `QUAD_DRY_RUN` | `true` | Live-trading guard |
| `QUAD_LOG_LEVEL` | `INFO` | |
| `QUAD_LOG_FORMAT` | `json` | |
| `QUAD_DEFAULT_STRATEGY` | `trend_following` | |
| `QUAD_CONFIG_DIR` | `/app/config` | Matches the read-only mount |
| `QUAD_HEALTH_PORT` | `9090` | Also drives the healthcheck URL |
| `BYBIT_API_KEY` / `BYBIT_API_SECRET` | empty | Required for real exchange access |
| `BYBIT_TESTNET` | `true` | Testnet is the default |
| `TELEGRAM_NOTIFICATION_CHAT_ID` | empty | Chat allowlist |
| `GROQ_API_KEY` | empty | AI features |
| `QUAD_SUPERVISOR_ENABLED` | `true` | Per-tenant `python -m quad` worker supervisor (`src/quad/api/app.py:113`) |
| `QUAD_ALLOW_LIVE` | `false` | Operator live kill-switch; `false` means testnet only even if a tenant requests live (`src/quad/api/routes_exchange.py:41`) |
| `DATABASE_URL` | `postgresql://quad:${POSTGRES_PASSWORD}@postgres:5432/quad` | Overrides `persistence.dsn` |
| `QUAD_HEALTH_API_KEY` | *(not set)* | **Set this if the health port is published** |
| `TZ` | `UTC` | |

`postgres` service: `postgres:16-alpine`, container `quad-pg`, volume `pg_data`,
healthcheck `pg_isready -U quad -d quad` (10s / 5s / 5 retries), memory cap `1G`.

> **Which database is actually in use?** The orchestrator routes on the DSN
> scheme via `create_database()` (`src/quad/persistence/pg.py:214`): a
> `postgresql://` / `postgres://` DSN gets a `PostgresDatabaseManager`
> (asyncpg), anything else gets the aiosqlite `DatabaseManager`. Because
> compose defaults `DATABASE_URL` to the Postgres DSN, **the shipped compose
> deployment runs on Postgres**, while a local `pip install` without
> `DATABASE_URL` runs on SQLite (`persistence.dsn`, e.g. `data/quad.db`).
> `asyncpg==0.31.0` is a pinned dependency (`requirements.txt`), so the Postgres
> path is installed in the image.
>
> The comment at `docker-compose.yml:90-92` claiming `DatabaseManager` is
> "still aiosqlite-only" and that the asyncpg port "ships with Phase 2" is
> **stale** — the asyncpg manager is implemented and wired. `depends_on:
> postgres: condition: service_healthy` is therefore load-bearing, not
> decorative.

Only `./config` is bind-mounted read-only; `.env` is **not** mounted — secrets
are passed as environment variables by compose, which is the correct pattern.
Don't add a `.env` bind mount.

### Deployment by Tier

**Development (local):**
```bash
pip install -e .
quad start --dry-run
```

**Staging (testnet):**
```bash
# BYBIT_TESTNET=true is the default; leave it unset
docker compose up -d
```

**Production (live):**
```bash
# Requires the Go-Live gates in docs/go-live-plan.md
# Set BYBIT_TESTNET=false, QUAD_DRY_RUN=false, QUAD_ALLOW_LIVE=true
# Ensure API keys have trading-only permissions
docker compose up -d
```

---

## Telegram Bot Considerations

### Polling Mode (Default)

Quad uses Telegram Bot API **polling mode** by default. This is simpler than webhook mode because:

- No public HTTPS endpoint required
- No SSL certificate configuration
- Works behind NAT, firewalls, and VPNs
- Automatically reconnects on connection loss

The bot polls Telegram's API continuously. This is handled by the
`python-telegram-bot` library and requires no configuration.

### Keeping the Bot Alive

The Telegram bot is part of the Quad process -- it runs in the same asyncio
event loop and is started/stopped by the orchestrator. As long as Quad is
running, Telegram polling is active:

- **Direct deployment:** use `tmux`/`screen`, or a systemd unit (both deliver
  SIGTERM on stop, which the orchestrator handles gracefully)
- **Docker:** `restart: unless-stopped` brings the container back after a crash
  or a host reboot. Note it deliberately does **not** restart after an explicit
  `docker compose stop` — that is what "unless-stopped" means. `init: true` runs
  tini as PID 1 so zombie children are reaped, and `start.sh` uses `exec` so
  `python` inherits PID 1's signal handling.

### Bot Token Management

- Always store the token in `.env` -- never hardcode it
- Rotate the token periodically via @BotFather
- If compromised, regenerate immediately -- old token is invalidated
- Use separate bot tokens for production and test instances

### Chat ID Whitelist

The bot only responds to whitelisted chat IDs, injected from
`TELEGRAM_NOTIFICATION_CHAT_ID` (`src/quad/orchestrator/orchestrator.py:430`):

```bash
# In .env
TELEGRAM_NOTIFICATION_CHAT_ID=123456789
```

You can find your chat ID by messaging [@userinfobot](https://t.me/userinfobot) on
Telegram. Whitelist entries are checked on every incoming message.

### docker-compose Environment

Do **not** add a second, divergent compose service for Telegram — the shipped
`docker-compose.yml` already passes `TELEGRAM_BOT_TOKEN` (required) and
`TELEGRAM_NOTIFICATION_CHAT_ID` to the `quad` service. Use it as-is:

```bash
# In .env
TELEGRAM_BOT_TOKEN=123456:ABC-your-token
TELEGRAM_NOTIFICATION_CHAT_ID=123456789
```

---

## Database Management

### Connection

Persistence is configured via a single `persistence.dsn` key, overridable by
`DATABASE_URL` or `QUAD_DSN`:

```yaml
persistence:
  dsn: "data/quad.db"   # SQLite file — the local/dev default
```

Do **not** write `dsn: "${DATABASE_URL:-data/quad.db}"`. The `${VAR:-default}`
shell-default syntax is not supported — expansion uses `os.path.expandvars`,
which only handles plain `${VAR}` (see
[docs/configuration.md](configuration.md#env-var-expansion-in-yaml)). Use a
literal path, or `${DATABASE_URL}` with the env var always set.

### Maintenance — SQLite (local / dev default)

```bash
# VACUUM reclaims space; run while the bot is STOPPED
sqlite3 data/quad.db "VACUUM;"

# Size on disk
ls -lh data/quad.db

# Page/row stats
sqlite3 data/quad.db "PRAGMA page_count;"
sqlite3 data/quad.db "PRAGMA freelist_count;"
```

### Maintenance — Postgres (compose default)

```bash
# Database size
psql -U quad -d quad -c "SELECT pg_size_pretty(pg_database_size('quad'));"

# Largest tables
psql -U quad -d quad -c \
  "SELECT relname, pg_size_pretty(pg_total_relation_size(relid)) AS size
   FROM pg_catalog.pg_statio_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 10;"

# Reclaim dead tuples and refresh planner stats
VACUUM ANALYZE;
```

`pg_database_size()` and `VACUUM ANALYZE` are **Postgres-only** and do **not**
apply to the SQLite default. For SQLite use `PRAGMA page_count` /
`PRAGMA freelist_count` and `VACUUM;` instead. Check which one you are on with
`quad db-info` (it prints the DSN and file size for SQLite and skips the file
line for Postgres, `src/quad/cli/app.py:500-515`).

### Backups — both engines

The persistence layer exposes a consistent online backup through aiosqlite's
`.backup()` API (`src/quad/persistence/database.py:467-496`), so SQLite backups
are safe while the bot runs:

```bash
# SQLite: use the .backup command rather than cp (safe with a live writer)
mkdir -p data/backups
sqlite3 data/quad.db ".backup 'data/backups/quad_$(date +%Y%m%d_%H%M%S).db'"

# Postgres: pg_dump / pg_dumpall
pg_dump -U quad -d quad -Fc -f pgdata/quad_$(date +%Y%m%d_%H%M%S).dump
```

For Postgres, the durable artifact is the `pg_data` named volume — snapshot or
back that up rather than the container filesystem.

### Restore

```bash
# Stop the bot first (SIGINT/SIGTERM — see Foreground Operation)
docker compose stop quad      # or Ctrl+C for a local run

# SQLite
cp data/backups/quad_20260707_120000.db data/quad.db

# Postgres
pg_restore -U quad -d quad -c --clean pgdata/quad_20260707_120000.dump

# Restart
docker compose up -d quad
```

The bot runs `initialize()` and `migrate()` on connect (`orchestrator.py:477-479`),
so the schema is created/updated on restart.

### Connection Pooling

Pool sizing is passed explicitly by the orchestrator
(`src/quad/orchestrator/orchestrator.py:472-476`):

| Engine | Pool | Notes |
|---|---|---|
| SQLite | single aiosqlite connection | SQLite does not benefit from multiple concurrent writers; `min_pool_size` / `max_pool_size` are effectively ignored |
| Postgres | asyncpg pool, `min_pool_size: 1`, `max_pool_size: 5` | Override via `persistence.database.min_pool_size` / `max_pool_size` (`schema.py:620-644`) |

`persistence.database.connect_retry_count` (5) and `command_timeout_seconds` (60)
tune startup retries and statement timeouts.

---

## Logging

### Log Output

| Stream | Where | Content |
|---|---|---|
| stdout/stderr | container logs, `docker compose logs` | All bot operations (structlog) — this is the primary log stream |
| `error_logs` table | database (SQLite or Postgres) | Batched error/warning events, if `error_sink` is enabled |
| json-file rotation | Docker daemon | `10m` x `3` files (compose `logging` block) |

Quad writes to stdout. The `quad_logs` volume mounted at `/app/logs` exists for
operators who redirect the stream to a file, but no schema option enables a log
file — `QUAD_LOG_FILE` is not read anywhere in the code. The `logging` driver
block in compose controls **Docker's** log rotation, not Quad's, and must live in
the compose service definition. There is no `logging:` section in the
`QuadConfig` schema; `QUAD_LOG_LEVEL` and `QUAD_LOG_FORMAT` are read directly
from the environment before config load.

### Log Format

By default, logs are structured JSON for easy parsing, at the level set by
`QUAD_LOG_LEVEL`:

```json
{"timestamp": "2026-07-07T10:00:00Z", "level": "INFO", "event": "trading_cycle", "cycle_time_ms": 950, "state": "ACTIVE"}
{"timestamp": "2026-07-07T10:00:00Z", "level": "INFO", "event": "decision", "action": "ENTER", "strategy": "trend_following", "symbol": "BTCUSDT"}
{"timestamp": "2026-07-07T10:00:01Z", "level": "WARN", "event": "risk_check", "check": "margin_sufficiency", "result": "PASS", "available": 5000, "required": 450}
```

Set `QUAD_LOG_FORMAT=text` for human-readable output.

```bash
# Follow container logs (the primary log stream)
docker compose logs -f quad

# Or tail a log file, if you have configured one
quad logs --path logs/quad.log --lines 200
```

`quad logs` only tails a **file**; it has no `--follow` or `--level` flags
(`src/quad/cli/app.py:801-805`). Because Quad writes to stdout and no log-file
option exists in the schema, the command exits 1 with an explanatory message
when the file is absent. Use `docker compose logs` or your container runtime for
the real stream.

### Error Log Sink

`error_sink` persists qualifying structlog events to the `error_logs` table in
batches. Configure it in `config/config.yaml`:

```yaml
error_sink:
  enabled: true
  min_level: warning      # default: error
  batch_size: 20
  flush_interval_seconds: 5.0
  max_queue: 1000         # oldest dropped when full
```

See [docs/configuration.md](configuration.md#error_sink) for full semantics.

---

## Monitoring

### Health Check Server

Quad runs an aiohttp health server on port 9090 by default
(`monitoring.health_server.port`, env `QUAD_HEALTH_PORT`). It is used for the
Docker healthcheck and for external monitoring.

| Path | Handler | Returns |
|---|---|---|
| `GET /health` | `_handle_health` | Overall status, uptime, version, per-component health |
| `GET /` | `_handle_health` | Alias of `/health` |
| `GET /readiness` | `_handle_readiness` | `{"ready": bool, "components": {...}}` |
| `GET /ready` | `_handle_readiness` | Short alias of `/readiness` |
| `GET /liveness` | `_handle_liveness` | `{"alive": true}` |
| `GET /live` | `_handle_liveness` | Short alias of `/liveness` |
| `GET /metrics` | `_handle_metrics` | Prometheus text exposition |
| `POST /webhook/tradingview` | registered only when `tradingview_webhook.enabled` | TradingView alert receiver |

All seven built-in paths are registered in `src/quad/monitoring/health.py:192-209`.
`/ready` and `/live` exist as k8s-style probe aliases (also documented in
[docs/api.md](api.md)) — both spellings are correct.

```bash
curl http://localhost:9090/health
curl http://localhost:9090/ready
curl http://localhost:9090/live
curl http://localhost:9090/metrics
```

`/health` and `/readiness` iterate a `components` registry. No subsystem calls
`register_component()` today, so `components` is `{}` and `status` is `"ok"`
unless something is registered in future. Use the `degraded` field — it lists
the names of any registered component that failed its check.

The `quad health` CLI command is a separate client for the same endpoint: it
reads the port and bind address from `config/config.yaml` and issues an HTTP
`GET /health` (`src/quad/cli/app.py:654-683`). It cannot authenticate — it looks
for the key in config, where the schema does not keep it — so use `curl` when
`QUAD_HEALTH_API_KEY` is set.

### Key Metrics

`/metrics` is served by `MetricsCollector` (`src/quad/monitoring/metrics.py`).
Names are stored verbatim — the collector does **not** add a `quad_` prefix — so
scrape for the names below, not prefixed variants.

| Metric | Type | Set by | Description |
|---|---|---|---|
| `quad_uptime_seconds` | Gauge | hard-coded in the collector | Bot uptime; the only metric present in the no-collector fallback (`health.py:439`) |
| `orchestrator_started` | Gauge | `orchestrator.py:981` | `1` once the orchestrator is up |
| `dry_run` | Gauge | `orchestrator.py:982` | `1` in dry-run, `0` otherwise |
| `dry_run_guard_active` | Gauge | `orchestrator.py:1643` | `1` only when `_dry_run=true` **and** `testnet=false` |
| `active_positions` | Gauge | `orchestrator.py:1638` | Open position count |
| `active_strategies` | Gauge | `orchestrator.py:1639` | Loaded strategy count |
| `portfolio_value` | Gauge | `orchestrator.py:1656` | Account `total_usdt` |
| `ai_cycle_time_ms` | Gauge | `orchestrator.py:1651` | Last AI cycle duration |
| `ai_hit_rate` | Gauge | `orchestrator.py:3801` | Resolved-decision hit rate |
| `ai_ece` | Gauge | `orchestrator.py:3805` | Expected Calibration Error |
| `ai_brier` | Gauge | `orchestrator.py:3809` | Brier score |
| `ai_decisions_resolved` | Gauge | `orchestrator.py:3813` | Resolved decisions in the metrics window |
| `ai_decisions_directional` | Gauge | `orchestrator.py:3816` | LONG/SHORT resolved decisions |
| `trading_cycles` | Counter | `orchestrator.py:1647` | Completed main trading cycles |
| `ai_decisions` | Counter | `orchestrator.py:1650` | AI cycles that produced a decision |

AI metrics require `ai.metrics.enabled: true` (the default) and at least
`ai.metrics.min_resolved` resolved rows (default 5); until then they are absent
or `NaN`. Structured log events carry more operational detail than these gauges
— `cycle_status` in particular reports mode, positions, `ai_used`, and
`dry_run_guard_active` on every cycle.

---

## Security Hardening

| Area | Action | Notes |
|---|---|---|
| Health server | Keep on loopback, or set `QUAD_HEALTH_API_KEY` | See below — this is not optional behind a proxy |
| Firewall | Allow only 22 (SSH) and the health port from trusted sources | Never expose the health port to the public internet unauthenticated |
| Bybit API Keys | Create keys with trading only (disable withdrawals) | Rotate keys every 90 days |
| Docker Security | Non-root `quad` user | Already baked into the image (`Dockerfile:67`); no `user:` override needed |
| Config mount | `./config:/app/config:ro` | Already read-only in compose |
| Database Access | Restrict SQLite file permissions to trusted users | Use filesystem permissions (chmod 600) |
| Secrets | Store API keys in `.env` / compose env, never in code | Keep `.env` out of version control; `.env` is not mounted into the container |

### Health Server Authentication

The health server exposes position, balance, and readiness detail, so it has an
auth model — do not simply "open all interfaces".

**Bind behaviour** (`src/quad/monitoring/health.py:82-92`):

| Condition | Effective bind address |
|---|---|
| No `QUAD_HEALTH_API_KEY` | Forced to `127.0.0.1` — the configured `monitoring.health_server.bind_address` is **ignored** |
| `QUAD_HEALTH_API_KEY` set | The configured `bind_address` is honoured (so `0.0.0.0` now takes effect) |

**Auth behaviour** (`health.py:302` `_check_api_key`):

| Case | Result |
|---|---|
| No key configured, request from `127.0.0.1` / `::1`, **no** forwarding headers | Allowed (loopback bypass) |
| No key configured, request from any other address | `403 Forbidden` |
| No key configured, **forwarding headers present** (`X-Forwarded-For`, `X-Real-IP`, `X-Forwarded-Host`, `Forwarded`) | `403 Forbidden` — bypass refused |
| Key configured, `X-API-Key` header matches | Allowed |
| Key configured, header missing or wrong | `403 Forbidden` |

The third row is the important one. Behind a reverse proxy, `request.remote` is
the *proxy's* loopback address, so a naive loopback check would let any
forwarded internet request through. Quad therefore refuses the bypass whenever
forwarding headers are present (`health.py:328` `_has_forwarding_headers`).

**Consequence: if you expose the health server through a proxy — nginx, Caddy,
Traefik, a cloud load balancer — you MUST set `QUAD_HEALTH_API_KEY`.** Without
it every probe returns `403`, including the Docker healthcheck, and the container
will be reported unhealthy. With it, clients must send `X-API-Key: <key>`:

```bash
QUAD_HEALTH_API_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
docker compose up -d
curl -H "X-API-Key: $QUAD_HEALTH_API_KEY" http://localhost:9090/health
```

Two further notes:

- `monitoring.health_server.api_key` is **not** a schema field, so the key cannot
  be set in `config/config.yaml`. It must be an environment variable
  (`HealthServer` reads it directly at `health.py:296`).
- The shipped `docker-compose.yml` does not publish any ports, so the health
  port stays inside the compose network by default. If you add
  `ports: - "9090:9090"`, add `QUAD_HEALTH_API_KEY` in the same breath.
- `/webhook/tradingview` is mounted on this same server, so it is subject to the
  same auth and the same loopback logic. TradingView cannot send an
  `X-API-Key`; plan the proxying accordingly (its own HMAC secret requirement is
  separate — see
  [docs/configuration.md](configuration.md#tradingview_webhook)).

---

## Scaling Considerations

| Scenario | Recommendation |
|---|---|
| Multiple underlyings | Increase `risk.max_positions` in config, ensure adequate margin |
| Higher frequency trading | Reduce `trading.max_cycle_interval`, monitor `ai_cycle_time_ms` and the `trading_cycles` counter rate |
| Multiple bot instances | Use separate data directories and databases; each instance binds its own health port (`QUAD_HEALTH_PORT`) |
| Large historical data | Monitor disk; the `market_data` section only configures buffers, cache TTLs, and the WebSocket (`buffer_sizes` / `cache_ttl` / `engine` / `websocket`) — there is no `market_data.historical` section |
| Database size growth | **SQLite (default):** `VACUUM;` periodically, archive old rows. **Postgres:** `SELECT pg_size_pretty(pg_database_size('quad'));` and `VACUUM ANALYZE` |
| Memory | The compose service is capped at 512M / 1.0 CPU; raise in `deploy.resources.limits` if cycle time degrades |
