# Configuration Reference

---

## Environment Variables

The `.env` file at the project root holds secrets and environment-specific values. An `.env.example` template is provided.

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes* | -- | Telegram bot token from @BotFather (required for Telegram interface) |
| `TELEGRAM_ADMIN_IDS` | No | `--` | Legacy -- left empty. Previously comma-separated Telegram chat IDs (no longer used) |
| `OKX_API_KEY` | Yes* | -- | OKX V5 API key (USDT perpetual / instType=SWAP; not needed for dry-run) |
| `OKX_API_SECRET` | Yes* | -- | OKX V5 API secret |
| `OKX_PASSPHRASE` | Yes* | -- | OKX V5 API passphrase (required for OKX) |
| `OKX_TESTNET` | No | `true` | Use OKX demo trading (set to `false` for live trading) |
| `QUAD_DATA_DIR` | No | `./data` | Data directory (databases, logs, cache) |
| `QUAD_CONFIG_DIR` | No | `./config` | Configuration directory |
| `QUAD_LOG_LEVEL` | No | `INFO` | Logging level: DEBUG, INFO, WARN, ERROR |
| `QUAD_LOG_FORMAT` | No | `json` | Log format: json or text |
| `QUAD_LOG_FILE` | No | `./data/logs/quad.log` | Log file path |
| `DATABASE_URL` | No | `data/quad.db` | SQLite database path (overrides persistence.dsn in config) |
| `QUAD_HEALTH_PORT` | No | `9090` | Health check HTTP server port |
| `QUAD_MODE` | No | `okx` | Exchange mode: `okx` (USDT perpetual). `dry_run` runs demo trading in dry-run mode. |
| `QUAD_DEFAULT_STRATEGY` | No | `trend_following` | Default strategy to load on start |
| `QUAD_MAX_CYCLE_INTERVAL` | No | `60` | Trading cycle interval in seconds |
| `QUAD_DRY_RUN` | No | `true` | Run in dry-run mode (no real orders) |
| `QUAD_CONFIG_PATH` | No | `config/config.yaml` | Path to local config YAML file |
| `QUAD_LOG_DIR` | No | `./data/logs` | Log output directory |
| `QUAD_AI_ENABLED` | No | `false` | Enable AI-driven trading analysis |
| `QUAD_AI_MODEL` | No | `groq/compound-mini` | Groq LLM model identifier for AI analysis |
| `QUAD_AI_TIMEOUT` | No | `30` | LLM API request timeout in seconds |
| `QUAD_AI_MAX_REQUESTS_PER_DAY` | No | `950` | Maximum LLM API requests per day |
| `TELEGRAM_NOTIFICATION_CHAT_ID` | No | `--` | Chat ID for automated Telegram notifications |
| `QUAD_TRADINGVIEW_WEBHOOK_ENABLED` | No | `false` | Enable the TradingView webhook receiver |
| `QUAD_TRADINGVIEW_WEBHOOK_PORT` | No | `8081` | Port for the webhook HTTP server |
| `QUAD_TRADINGVIEW_WEBHOOK_SECRET` | No | `--` | Shared secret for webhook HMAC signature verification |

*Required only for live trading or testnet access.

---

## YAML Configuration Files

The `config/` directory contains the main configuration files. Each file is optional; missing files use sensible defaults.

| File | Purpose | Hot-Reloadable |
|---|---|---|
| `config.yaml` | All configuration keys with default values | N/A |
| `config.local.yaml` | Local overrides (not committed to git) | No |
| `risk.yaml` | Risk management parameters | Yes |
| `strategy.yaml` | Strategy-specific settings | Yes |
| `exchange.yaml` | Exchange connection settings | No |
| `logging.yaml` | Logging configuration | Yes |

### config.yaml

```yaml
# Quad — Minimal Configuration for BTCUSDT USDT-Perpetual Bot (OKX)
_mode: "okx"
_dry_run: true

trading:
  # One trade per cycle: force-close every open position before a new ENTER.
  # Only one position may ever be open (see also ai.rotation below).
  serial_trade_mode: true
  default_strategy: "trend_following"
  ai_cycle_interval: 3600
  underlyings:
    - "BTC-USDT-SWAP"
    - "ETH-USDT-SWAP"
    - "SOL-USDT-SWAP"
    - "BNB-USDT-SWAP"
  leverage: 50
  margin_mode: "isolated"
  position_mode: "one_way"

exchange:
  name: "okx"
  testnet: true

risk:
  max_positions: 1
  max_leverage: 50
  min_distance_to_liquidation_pct: 0.20
  liquidation_distance_fraction: 0.5
  per_position_sl:
    enabled: true
    type: "fixed"
    capital_pct: 30.0
  per_position_tp:
    enabled: true
    type: "fixed"
    capital_pct: 50.0

execution:
  reconcile_interval_seconds: 60

persistence:
  dsn: "data/quad.db"

telegram:
  enabled: true
  job_intervals:
    status_summary_seconds: 3600
    risk_alert_seconds: 300

ai:
  enabled: true
  model: "groq/compound-mini"
  pairs:
    - "BTC-USDT-SWAP"
    - "ETH-USDT-SWAP"
    - "SOL-USDT-SWAP"
    - "BNB-USDT-SWAP"
  timeframes:
    - "1h"
  candle_count: 150
  max_tokens: 4096
  prompt:
    max_candles: 20
  rotation:
    enabled: true
    retry_sleep_seconds: 30
    close_positions_on_start: true
    close_open_position_each_cycle: true
    max_hold_seconds: 21600
    price_bracket_check: true
    price_bracket_tolerance_pct: 0.5
  groq:
    token_budget:
      enabled: false
  validator:
    gate_mode: "warn"
    min_confidence_to_trade: 0.0
  metrics:
    enabled: true
    interval_cycles: 1
    min_resolved: 5
    only_directional: true

monitoring:
  health_server:
    port: 9090
    bind_address: "127.0.0.1"
    version: "0.1.0"

strategy:
  trend_following:
    enabled: true
    fast_ema: 9
    slow_ema: 21
    adx_threshold: 25
    trade_capital_usd: 5

retrain:
  enabled: true
  interval_days: 7

# OKX MCP Server — when enabled, replaces python-okx SDK for data, TA, and execution.
# Requires: npm install -g @okx_ai/okx-trade-mcp
# Set exchange.api_key, exchange.api_secret, exchange.passphrase via env vars or config.
mcp:
  enabled: true           # true = use MCP server (default); false = use python-okx SDK
  command: "okx-trade-mcp"
  modules: "all"          # market,swap,account,spot,futures,option,smartmoney,news
  profile: "default"      # OKX API profile (~/.okx/config.toml)
  request_timeout: 30.0   # seconds per tool call
  startup_timeout: 15.0   # seconds for MCP handshake
```
    bb_std: 2.0
    entry_timeout: 3600  # seconds
    max_position_size: 1.0

  dca_bot:
    enabled: false
    initial_order_size: 0.1  # contracts
    dca_order_size: 0.2  # contracts
    dca_price_distance_pct: 2.0  # % from entry
    max_dca_levels: 5
    take_profit_pct: 1.0

  market_making:
    enabled: false
    spread_pct: 0.1  # % spread from mid price
    order_size: 0.5  # contracts per side
    max_position_size: 2.0  # contracts
    order_refresh_interval: 10  # seconds

# Market Data
market_data:
  price_buffer: 1000  # ticks per symbol
  order_book_depth: 50  # levels
  cache_ttl: 300  # seconds
  funding_rate_cache_ttl: 60  # seconds
  historical:
    data_dir: ./data/historical
    max_cache_size_gb: 10

# Persistence
persistence:
  dsn: "${DATABASE_URL:-data/quad.db}"
  snapshot_interval: 300  # seconds
  backup:
    enabled: true
    interval: 3600  # seconds
    max_backups: 48
    backup_dir: ./data/backups

# Logging
logging:
  level: INFO
  format: json  # json or text
  file: ./data/logs/quad.log
  max_size_mb: 100
  max_files: 10
  include_decisions: true

# Monitoring
monitoring:
  health_server:
    enabled: true
    port: 9090
    bind: 127.0.0.1
  metrics:
    enabled: true
    collection_interval: 60  # seconds
```

---

## Configuration Hierarchy

The bot resolves configuration from 4 layers, each overriding the previous:

```
Layer 1: config.yaml (packaged defaults)
    │
    ▼
Layer 2: config.local.yaml (user overrides, not in git)
    │
    ▼
Layer 3: Environment Variables (.env)
    │
    ▼
Layer 4: Runtime CLI flags / `quad config set` commands
```

**Layer 1 -- config.yaml:** Contains every configuration key with safe default values. Ships with the package.

**Layer 2 -- config.local.yaml:** User-specific overrides for local setup. Should not be committed to version control.

**Layer 3 -- Environment Variables:** Values from `.env` take precedence over YAML config. Secrets (API keys) live here.

**Layer 4 -- Runtime Updates:** Configuration changes made via `quad config set` at runtime. Some take effect immediately (hot-reloadable), others require a restart.

---

## Hot-Reloading

The bot supports hot-reloading for specific configuration without a full restart.

**Changes that take effect immediately (no restart needed):**
- Risk parameters: position limits, liquidation thresholds, drawdown limits, circuit breaker settings
- Strategy parameters: entry/exit conditions, take profit, stop loss levels
- Logging settings: log level, format
- Stop-loss/take-profit parameters

**Changes that require a restart:**
- Exchange configuration (API keys, endpoints)
- Database path
- Mode (paper/live)
- Default strategy
- Health server port

To reload hot-reloadable config at runtime:

```bash
quad config reload
```

Or set individual values:

```bash
quad config set risk.max_positions 3
quad config set strategy.trend_following.ema_fast 12
```

The bot logs all configuration changes:

```
2026-07-07T10:00:00Z [INFO] Config updated: risk.max_positions changed from 5 to 3
2026-07-07T10:00:00Z [INFO] Config hot-reload applied successfully. 1 key(s) updated.
```

---

## Validation Checklist

Before starting the bot, verify the following:

| # | Check | How to Verify |
|---|---|---|
| 1 | OKX API keys are valid | `quad health` shows "Exchange: connected" |
| 2 | API permissions are correct | Disable withdrawal permission; enable trading only |
| 3 | Telegram bot token is set | Verify `TELEGRAM_BOT_TOKEN` is set in `.env` |
| 4 | Demo trading mode is enabled for initial runs | Set `OKX_TESTNET=true` or use `quad start --dry-run` |
| 5 | Database path is writable | `quad health` shows "Database: connected" |
| 6 | Configuration syntax is valid | `quad config` shows expected values |
| 7 | Data directory has sufficient space | Check `./data/` free space (500 MB minimum) |
| 8 | Time sync is accurate | NTP should be within 1 second of UTC |
| 9 | Strategy configuration is valid | `quad strategies` lists expected strategies |

---

## Security Warnings

**Never commit your `.env` file.** The `.env` file contains API keys and secrets (OKX API keys, Telegram bot token) that would compromise your trading account and Telegram bot. The `.gitignore` explicitly excludes `.env` from version control. Always use `.env.example` as a template.

**Protect your Telegram bot token.** The `TELEGRAM_BOT_TOKEN` gives full control of your Telegram bot. Anyone with this token can send messages as your bot and intercept bot commands. Never share it or commit it to version control. If compromised, regenerate immediately via @BotFather.

**Use dedicated API keys.** Create OKX V5 API keys specifically for this bot with only minimum required permissions: enable trading, disable withdrawals. Never use keys from your main account or keys with withdrawal permissions.

**Rotate keys regularly.** Change your OKX API keys every 90 days.

**Start in dry-run or demo mode.** Before risking real capital, run the bot with `quad start --dry-run` or `OKX_TESTNET=true` (demo trading is the default).

**Restrict database access.** The SQLite database contains your trading history and configuration. Use file permissions to restrict access, and never expose the data directory to the public internet.

**Monitor log files.** Regularly check logs for suspicious activity, unexpected errors, or authorization failures. Configure log rotation to prevent disk exhaustion.
