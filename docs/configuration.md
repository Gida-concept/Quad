# Configuration Reference

---

## Environment Variables

The `.env` file at the project root holds secrets and environment-specific values. An `.env.example` template is provided.

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes* | -- | Telegram bot token from @BotFather (required for Telegram interface) |
| `TELEGRAM_ADMIN_IDS` | No | `--` | Legacy -- left empty. Previously comma-separated Telegram chat IDs (no longer used) |
| `BYBIT_API_KEY` | Yes* | -- | Bybit V5 API key (USDT perpetual / category=linear; not needed for dry-run) |
| `BYBIT_API_SECRET` | Yes* | -- | Bybit V5 API secret |
| `BYBIT_TESTNET` | No | `true` | Use Bybit testnet (set to `false` for live trading) |
| `QUAD_DATA_DIR` | No | `./data` | Data directory (databases, logs, cache) |
| `QUAD_CONFIG_DIR` | No | `./config` | Configuration directory |
| `QUAD_LOG_LEVEL` | No | `INFO` | Logging level: DEBUG, INFO, WARN, ERROR |
| `QUAD_LOG_FORMAT` | No | `json` | Log format: json or text |
| `QUAD_LOG_FILE` | No | `./data/logs/quad.log` | Log file path |
| `DATABASE_URL` | No | `postgresql://quad:quad@localhost:5432/quad` | PostgreSQL DSN (overrides persistence.dsn in config) |
| `QUAD_HEALTH_PORT` | No | `9090` | Health check HTTP server port |
| `QUAD_MODE` | No | `bybit` | Exchange mode: `bybit` (USDT perpetual). `dry_run` runs testnet in dry-run mode. |
| `QUAD_DEFAULT_STRATEGY` | No | `cash_secured_put` | Default strategy to load on start |
| `QUAD_MAX_CYCLE_INTERVAL` | No | `60` | Trading cycle interval in seconds |
| `QUAD_DRY_RUN` | No | `true` | Run in dry-run mode (no real orders) |
| `QUAD_CONFIG_PATH` | No | `config/config.local.yaml` | Path to local config YAML file |
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
| `config.default.yaml` | All configuration keys with default values | N/A |
| `config.local.yaml` | Local overrides (not committed to git) | No |
| `risk.yaml` | Risk management parameters | Yes |
| `strategy.yaml` | Strategy-specific settings | Yes |
| `exchange.yaml` | Exchange connection settings | No |
| `logging.yaml` | Logging configuration | Yes |

### config.default.yaml

```yaml
# Trading
trading:
  default_strategy: trend_following
  max_positions: 5
  max_cycle_interval: 60  # seconds
  serial_trade_mode: false  # close all positions before new ENTER
  leverage: 3  # default leverage for new positions
  margin_mode: isolated  # isolated, cross
  position_mode: hedge  # one_way, hedge

# Exchange
exchange:
  name: bybit
  testnet: true  # testnet is the default safety environment; set false for live

# Risk Management
risk:
  max_positions: 5
  max_portfolio_risk: 0.02  # 2% of portfolio per trade
  max_daily_loss: 0.05  # 5% of portfolio per day
  max_drawdown: 0.15  # 15% circuit breaker
  min_distance_to_liquidation_pct: 0.20  # 20% min distance to liquidation price
  max_funding_rate_cost: 0.001  # max funding rate cost per 8h cycle (0.1%)
  max_leverage: 10  # maximum leverage allowed
  max_position_concentration: 0.4  # 40% max per symbol
  max_correlation: 0.7  # max portfolio correlation coefficient
  circuit_breakers:
    pnl_drawdown:
      enabled: true
      threshold: 0.15  # 15% portfolio drawdown
      cooldown: 3600  # seconds before auto-reset
    daily_loss:
      enabled: true
      threshold: 0.10  # 10% daily loss
    consecutive_losses:
      enabled: true
      threshold: 3  # consecutive losing trades
    position_growth:
      enabled: true
      threshold: 0.50  # 50% position growth in 24h
    liquidation_cascade:
      enabled: true
      proximity_threshold: 0.10  # 10% from liquidation
    funding_rate_spike:
      enabled: true
      change_threshold: 0.002  # 0.2% funding rate change
      window: 24  # hours
    volatility:
      enabled: true
      change_threshold: 0.50  # 50% volatility change
      window: 24  # hours
  stop_loss:
    enabled: true
    type: fixed  # fixed, trail
    fixed_loss_per_contract: 100  # USDT
    trail_activation_pnl: 50  # USDT profit
    trail_distance: 0.5  # multiple of position value
  take_profit:
    enabled: true
    target_pnl_percent: 50  # % of position value
    target_pnl_fixed: 200  # USDT

# Strategy
strategy:
  trend_following:
    enabled: true
    ema_fast: 9
    ema_slow: 21
    atr_period: 14
    atr_multiplier: 2.0
    stop_loss_atr: 2.0
    take_profit_atr: 4.0
    max_position_size: 1.0  # contracts

  grid_trading:
    enabled: false
    grid_levels: 10
    grid_spread_pct: 0.5  # % between grid levels
    grid_size_pct: 10  # % of capital per grid
    take_profit_pct: 1.0  # % per grid level

  mean_reversion:
    enabled: false
    rsi_period: 14
    rsi_oversold: 30
    rsi_overbought: 70
    bb_period: 20
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
  dsn: "${DATABASE_URL:-postgresql://quad:quad@localhost:5432/quad}"
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
Layer 1: config.default.yaml (packaged defaults)
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

**Layer 1 -- config.default.yaml:** Contains every configuration key with safe default values. Ships with the package.

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
| 1 | Binance API keys are valid | `quad health` shows "Exchange: connected" |
| 2 | API permissions are correct | Disable withdrawal permission; enable trading only |
| 3 | Telegram bot token is set | Verify `TELEGRAM_BOT_TOKEN` is set in `.env` |
| 4 | Testnet mode is enabled for initial runs | Set `BYBIT_TESTNET=true` or use `quad start --dry-run` |
| 5 | Database path is writable | `quad health` shows "Database: connected" |
| 6 | Configuration syntax is valid | `quad config` shows expected values |
| 7 | Data directory has sufficient space | Check `./data/` free space (500 MB minimum) |
| 8 | Time sync is accurate | NTP should be within 1 second of UTC |
| 9 | Strategy configuration is valid | `quad strategies` lists expected strategies |

---

## Security Warnings

**Never commit your `.env` file.** The `.env` file contains API keys and secrets (Bybit API keys, Telegram bot token) that would compromise your trading account and Telegram bot. The `.gitignore` explicitly excludes `.env` from version control. Always use `.env.example` as a template.

**Protect your Telegram bot token.** The `TELEGRAM_BOT_TOKEN` gives full control of your Telegram bot. Anyone with this token can send messages as your bot and intercept bot commands. Never share it or commit it to version control. If compromised, regenerate immediately via @BotFather.

**Use dedicated API keys.** Create Bybit V5 API keys specifically for this bot with only minimum required permissions: enable trading, disable withdrawals. Never use keys from your main account or keys with withdrawal permissions.

**Rotate keys regularly.** Change your Bybit API keys every 90 days.

**Start in dry-run or testnet mode.** Before risking real capital, run the bot with `quad start --dry-run` or `BYBIT_TESTNET=true` (testnet is the default).

**Restrict database access.** The PostgreSQL database contains your trading history and configuration. Use a strong database password, restrict network access via `pg_hba.conf`, and never expose the database port to the public internet.

**Monitor log files.** Regularly check logs for suspicious activity, unexpected errors, or authorization failures. Configure log rotation to prevent disk exhaustion.
