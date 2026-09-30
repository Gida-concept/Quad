# Configuration Reference

---

## Configuration Layers

Quad resolves configuration from exactly **three** layers. Lower number = lower
priority; each layer overrides the one below it
(`src/quad/config/manager.py:352` `_load_all_layers`):

```
Layer 1 (lowest):  <config_dir>/config.yaml      single YAML file, schema-validated
        │
        ▼
Layer 2:           Environment variables        ENV_VAR_MAP first, then heuristic
        │
        ▼
Layer 3 (highest): Runtime overrides via set()  in-process only
```

There is **no** `config.local.yaml` overlay, no per-domain split files
(`risk.yaml`, `strategy.yaml`, `exchange.yaml`, `logging.yaml` are not read), and
no CLI-flag configuration layer. The file name is a single constant:

```python
CONFIG_FILE = "config.yaml"   # src/quad/config/manager.py:76
```

A missing `config.yaml` raises `FileNotFoundError` at startup
(`manager.py:360`); a malformed one raises `yaml.YAMLError`. There is no partial
load.

### Config directory resolution

`ConfigManager` resolves the directory in this order (`manager.py:277`
`_resolve_config_dir`):

1. The explicit `config_dir` argument
2. `$QUAD_CONFIG_DIR`
3. `~/.quad/config/` (if it contains `config.yaml`)
4. `./config/` (project root, the shipped default)
5. Fallback: `./config/` even when `config.yaml` is absent (errors at load)

### Dotenv files

`.env` / `.env.local` are loaded into `os.environ` **before** the config layers
are merged (`manager.py:306` `_load_env_file`), which is why dotenv values are
indistinguishable from real environment variables in layer 2.

Search order: `<config_dir>/.env.local`, `<config_dir>/../.env.local`,
`./.env.local`; then `dotenv.find_dotenv(".env")`; then
`<config_dir>/.env`, `<config_dir>/../.env`, `./.env`. Loading uses
`override=False` (`manager.py:343`), so real environment variables always win
over the dotenv file.

`.env.local` is a dotenv file, **not** a config overlay. It has nothing to do
with the (non-existent) `config/config.local.yaml`.

---

## The Schema

`src/quad/config/schema.py` defines `QuadConfig` and its section models. After
merging, the layers are validated through Pydantic (`manager.py:383`), so every
schema key exists with its default even if the YAML omits it. Unknown
**top-level** keys are logged as `config_unknown_key_ignored` and are otherwise
ignored (`schema.py:1493`); `extra="ignore"` applies to nested models too
(`schema.py:1467`).

> **There is no `logging:` section in `QuadConfig`.** Logging is configured
> entirely by `QUAD_LOG_LEVEL` / `QUAD_LOG_FORMAT`, read straight from the
> environment before config load (`src/quad/__main__.py:76-77`). A `logging:`
> block in YAML is not a schema key; it only survives as an unvalidated
> pass-through if you set one (see [Heuristic mapping](#heuristic-mapping)).

Sample YAML with the **real** schema defaults:

```yaml
# config/config.yaml — validated against QuadConfig (src/quad/config/schema.py)

# Top-level safety switches. These are real schema fields
# (validation_alias=AliasChoices("_mode","mode")), see "Safety Switches" below.
_mode: bybit            # bybit | dry_run
_dry_run: true

trading:
  default_strategy: trend_following
  serial_trade_mode: true
  max_cycle_interval: 60
  ai_cycle_interval: 3600
  underlyings: [BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT, DOGEUSDT]
  leverage: 1           # 1-125 (schema default 1)
  margin_mode: isolated # isolated | cross
  position_mode: one_way # one_way | hedge

exchange:
  name: bybit           # only "bybit" is accepted
  testnet: true         # default True; live is opt-in
  api_key: null         # set via BYBIT_API_KEY
  api_secret: null      # set via BYBIT_API_SECRET
  rate_limit: {max_weight: 2000, max_orders: 900}
  bybit:
    base_url: https://api.bybit.com
    testnet_base_url: https://api-testnet.bybit.com
    ws_public_url: wss://stream.bybit.com/v5/public
    ws_private_url: wss://stream.bybit.com/v5/private
    recv_window: 5000
    exchange_info_ttl_seconds: 60.0
  gateway:
    confirmation_timeout_seconds: 30.0
    max_retries: 3
    completed_ids_maxlen: 1000
    backoff_base_seconds: 2.0
  reconciler:
    max_discrepancy_history: 500
    stale_order_hours: 24
    recent_discrepancies_default_count: 20

risk:
  max_positions: 1
  max_position_size: 1000.0
  max_portfolio_risk_pct: 20.0     # NOT max_portfolio_risk
  max_daily_loss_usd: 500.0        # NOT max_daily_loss
  max_drawdown_pct: 25.0           # NOT max_drawdown
  correlation_threshold_pct: 60.0  # NOT max_correlation
  max_leverage: 10                 # NOT 50
  min_distance_to_liquidation_pct: 0.2
  liquidation_distance_fraction: 0.5
  funding_rate_periods: 3
  max_funding_rate_cost: 0.001
  max_position_concentration: 0.4
  min_position_size_usd: 10.0
  max_position_size_pct: 0.10
  max_position_size_usd: 10000.0
  kelly: {fraction: 0.25, default_fraction: 0.02}
  circuit_breakers:
    daily_loss: {}
    drawdown: {}
    consecutive_losses: {max_consecutive: 5}
    liquidation_cascade: {min_cascade_distance_pct: 0.05}
    funding_rate_spike:
      funding_rate_spike_threshold: 0.001
      max_consecutive_spikes: 3
    volatility: {volatility_breaker_atr_pct: 0.05}
    drawdown_tiers: [5.0, 10.0, 15.0]   # must be strictly increasing
  per_position_sl: {enabled: true, type: fixed, capital_pct: 30.0}
  per_position_tp: {enabled: true, type: fixed, capital_pct: 50.0}

market_data:
  buffer_sizes: {ticks: 1000}
  cache_ttl:
    order_book: 5
    funding_rate: 10
    mark_price: 2
    open_interest: 3600
    order_book_limit: 20
  engine: {shutdown_timeout_seconds: 10.0}
  websocket:
    url: wss://stream.bybit.com/v5/public
    heartbeat_interval_seconds: 20.0
    backoff: {base_seconds: 1.0, max_seconds: 30.0, multiplier: 2.0, jitter_fraction: 0.1}

persistence:
  dsn: quad.db            # dev default is SQLite; "data/quad.db" in the shipped config
  database:
    min_pool_size: 1
    max_pool_size: 5
    connect_retry_count: 5
    command_timeout_seconds: 60

telegram:
  enabled: true
  job_intervals:
    status_summary_seconds: 3600
    risk_alert_seconds: 300
    funding_rate_countdown_seconds: 1800
    liquidation_warning_seconds: 300
    status_summary_first_seconds: 60
    risk_alert_first_seconds: 120
    funding_rate_countdown_first_seconds: 300
    liquidation_warning_first_seconds: 180
  daily_report: {hour: 23, minute: 0}
  funding_cost_report: {hour: 22, minute: 0}

monitoring:
  health_server:
    enabled: true
    port: 9090
    bind_address: "127.0.0.1"
    version: "0.1.0"
  metrics: {enabled: true}

# Trend-following params: fast_ema / slow_ema (NOT ema_fast / ema_slow).
strategy:
  trend_following:
    enabled: false
    fast_ema: 9
    slow_ema: 21
    adx_period: 14
    adx_threshold: 25
    atr_period: 14
    atr_default_pct: 0.02
    trade_capital_usd: 5
    tp_capital_pct: 50.0
    confidence_default: 0.7
    confidence_high: 0.9

ai:
  enabled: true
  model: groq/compound-mini
  timeout: 30
  temperature: 0.0
  max_tokens: 2048
  default_confidence: 0.0
  max_requests_per_day: 950
  pairs: [BTCUSDT, ETHUSDT, BNBUSDT, SOLUSDT]
  timeframes: [15m, 1h]
  candle_count: 300
  api_key: null           # set via GROQ_API_KEY
  prompt: {order_book_depth: 5, max_candles: 20}
  rotation:
    enabled: false
    retry_sleep_seconds: 30.0
    close_positions_on_start: true
    close_open_position_each_cycle: true
    max_hold_seconds: 0.0
    price_bracket_check: true
    price_bracket_tolerance_pct: 0.5
  validator: {gate_mode: warn, min_confidence_to_trade: 0.0}
  metrics: {enabled: true, interval_cycles: 1, min_resolved: 5, only_directional: true}
  groq:
    timeout_seconds: 30.0
    max_retries: 3
    base_backoff_seconds: 1.0
    fallback_action: HOLD
    valid_actions: [ENTER, EXIT, HOLD, adjust_stop, reduce_position]
    rate_limiter: {window_seconds: 86400, warning_level_1: 800, warning_level_2: 900, warning_level_3: 950}
    token_budget:
      enabled: true
      max_tokens_per_day: 100000
      window_seconds: 86400
      warning_level_1: 80000
      warning_level_2: 90000
      warning_level_3: 95000

execution:
  reconcile_interval_seconds: 60
  twap_window_seconds: 300
  default_order_type: MARKET   # MARKET only; limit orders disabled
  reduce_only: false
  post_only: false
  twap:
    min_slices: 3
    max_slices: 10
    jitter_seconds: 5
    min_slice_quantity: 0.01
    fill_urgency_threshold: 0.8

backtesting:
  starting_capital: 10000.0
  commission_pct: 0.001
  slippage_pct: 0.0005
  max_trades_per_day: 10

retrain:
  enabled: false
  interval_days: 7
  initial_delay_hours: 1
  min_trades_for_analysis: 10
  max_history_days: 90
  confidence_threshold: 0.7
  auto_apply: false
  max_recommendations_per_cycle: 5
  groq_temperature: 0.2
  groq_max_tokens: 2048

tradingview_webhook:
  enabled: false
  port: 9090            # INFORMATIONAL ONLY — see below
  secret: ""             # required, >= 16 chars, when enabled

error_sink: {}           # see "error_sink" below
```

> The shipped `config/config.yaml` is a deliberately small override file, not a
> copy of the defaults. It sets `trading.leverage: 10`, `risk.max_leverage: 50`,
> `monitoring.health_server.bind_address: "127.0.0.1"`, `persistence.dsn:
> "data/quad.db"`, and enables `ai.rotation` / `retrain` / `strategy.trend_following`.
> Everything else falls through to the schema defaults above.

---

## Safety Switches: `_mode` and `_dry_run`

`_mode` and `_dry_run` are **real validated fields on `QuadConfig`**
(`schema.py:1449-1460`), not ad-hoc pass-through extras:

```python
mode: str = Field(default="bybit", validation_alias=AliasChoices("_mode", "mode"))
dry_run: bool = Field(default=True, validation_alias=AliasChoices("_dry_run", "dry_run"))
```

They were declared as fields specifically so a typo in either key would fail
validation instead of being silently swallowed by the `extra="ignore"` loop
(`schema.py:1444-1448`). After validation, `ConfigManager` re-exposes them under
the underscore names the runtime reads (`manager.py:388-389`), so the resolved
config contains both `mode`/`dry_run` and `_mode`/`_dry_run` with the same value.

| Setting | Values | Default | Effect |
|---|---|---|---|
| `_mode` | `bybit`, `dry_run` **only** | `bybit` | `bybit` honours `exchange.testnet`; `dry_run` forces `testnet=True` |
| `_dry_run` | bool | `true` | Blocks every real order (execution engine, Bybit adapter, CLI) |

An unknown `_mode` is a **hard startup error**, not a fallback
(`src/quad/orchestrator/orchestrator.py:69` `_VALID_MODES = frozenset({"bybit",
"dry_run"})`, raised at `orchestrator.py:518`):

```
ValueError: Unknown _mode / QUAD_MODE 'okx'. Expected one of: bybit, dry_run.
```

`dry_run` is fail-closed in three places: `ExecutionEngine._is_dry_run`
(`src/quad/execution/engine.py:959`), `BybitFuturesAdapter` order guard
(`src/quad/exchange/bybit.py:162`), and `QuadOrchestrator._is_dry_run`
(`orchestrator.py:744`). A dry-run guard is only *armed* when `_dry_run=true`
**and** `testnet=false`; on testnet the exchange itself is the safety net.

---

## `error_sink`

The optional `error_sink` section (`schema.py:1461`) controls the persistent
`error_logs` writer — a structlog processor that batches qualifying log events
into the `error_logs` table (`src/quad/monitoring/error_sink.py:58`). It is
installed by the orchestrator (`orchestrator.py:488`) and flushed on shutdown
while the database is still open (`orchestrator.py:1400`).

| Key | Default | Description |
|---|---|---|
| `enabled` | unset (i.e. on) | Only `false` disables the sink (`orchestrator.py:493`). Any other value, including absent, leaves it enabled. |
| `min_level` | `error` | Lowest level name to persist (`error_sink.py:92`). Compared on a debug→fatal rank scale (`error_sink.py:155`). |
| `batch_size` | `20` | Flush once this many events are queued (`error_sink.py:93`). |
| `flush_interval_seconds` | `5.0` | Maximum time an event waits in the queue (`error_sink.py:94`). |
| `max_queue` | `1000` | Bounded queue; **oldest events are dropped** when full so a DB outage cannot exhaust memory (`error_sink.py:97`). Dropped events are logged as `error_sink_dropped_events`. |

```yaml
error_sink:
  enabled: true
  min_level: warning
  batch_size: 50
  flush_interval_seconds: 10.0
  max_queue: 5000
```

Passing `None` as the database manager makes the sink a no-op, so it is safe to
wire unconditionally (`error_sink.py:65`).

---

## `tradingview_webhook`

| Key | Default | Notes |
|---|---|---|
| `enabled` | `false` | Registers `POST /webhook/tradingview` on the health server |
| `port` | `9090` | **INFORMATIONAL ONLY** |
| `secret` | `""` | Required, `>= 16` characters, when enabled |

**`port` is not bound.** The webhook is a *route* on the health server, so the
port it actually listens on is `monitoring.health_server.port` (env
`QUAD_HEALTH_PORT`). Nothing binds `tradingview_webhook.port`; it exists so
existing configs validate and is echoed in the webhook's startup log for
correlation (`schema.py:1196-1207`). Do not point a proxy at it.

**The webhook requires a secret.** There is no unauthenticated mode: enabling it
with an empty or short secret is a validation error
(`schema.py:1217` `validate_secret_when_enabled`):

```
tradingview_webhook.secret must be at least 16 characters
when tradingview_webhook.enabled is true
```

---

## Environment Variables

### Mapped into the config tree

`ENV_VAR_MAP` in `src/quad/config/manager.py:43-64` is the **single source of
truth**. These are the only well-known variables, and each maps to an exact
config key. Anything not in this table falls through to the heuristic below.

| Variable | Config key | Default | Description |
|---|---|---|---|
| `QUAD_HEALTH_PORT` | `monitoring.health_server.port` | `9090` | Health server HTTP port |
| `QUAD_MODE` | `_mode` | `bybit` | `bybit` or `dry_run`; anything else fails startup |
| `QUAD_DRY_RUN` | `_dry_run` | `true` | Block all real orders |
| `QUAD_DEFAULT_STRATEGY` | `trading.default_strategy` | `trend_following` | Strategy loaded on start |
| `QUAD_DSN` | `persistence.dsn` | `quad.db` | Database DSN / SQLite path |
| `DATABASE_URL` | `persistence.dsn` | `quad.db` | **Same key as `QUAD_DSN`** — the two are aliases, last-writer-wins on `os.environ` iteration order. Pick one. |
| `QUAD_CONFIG_DIR` | `config_dir` | -- | Config directory (also read directly by `_resolve_config_dir`) |
| `BYBIT_API_KEY` | `exchange.api_key` | `null` | Bybit V5 API key (USDT perpetual, `category=linear`) |
| `BYBIT_API_SECRET` | `exchange.api_secret` | `null` | Bybit V5 API secret |
| `BYBIT_TESTNET` | `exchange.testnet` | `true` | Testnet endpoints; `false` selects live |
| `QUAD_AI_ENABLED` | `ai.enabled` | `true` | Enable AI analysis |
| `QUAD_AI_MODEL` | `ai.model` | `groq/compound-mini` | Groq model id |
| `QUAD_AI_TIMEOUT` | `ai.timeout` | `30` | LLM request timeout (s) |
| `QUAD_AI_MAX_REQUESTS_PER_DAY` | `ai.max_requests_per_day` | `950` | Request-based daily cap |
| `GROQ_API_KEY` | `ai.api_key` | `null` | Groq API key |
| `QUAD_TRADINGVIEW_WEBHOOK_ENABLED` | `tradingview_webhook.enabled` | `false` | Enable the webhook route |
| `QUAD_TRADINGVIEW_WEBHOOK_PORT` | `tradingview_webhook.port` | `9090` | **Informational only — not bound** |
| `QUAD_TRADINGVIEW_WEBHOOK_SECRET` | `tradingview_webhook.secret` | `""` | Webhook secret, `>= 16` chars when enabled |
| `QUAD_HEALTH_API_KEY` | `monitoring.health_server.api_key` | `""` | `X-API-Key` for the health server |

`QUAD_HEALTH_API_KEY` is a special case worth knowing: `monitoring.health_server.api_key`
is **not** a field on `HealthServerConfig` (`schema.py:769`), so the config-tree
copy is dropped by `extra="ignore"`. It still works because `HealthServer`
reads the variable straight from the environment first and only falls back to
config (`src/quad/monitoring/health.py:296` `_resolve_api_key`). Set it as an
environment variable; putting the key in YAML has no effect.

### Read directly from the environment (not part of the config tree)

These are consumed by code before or outside the config merge. They are not in
`ENV_VAR_MAP`.

| Variable | Default | Consumer |
|---|---|---|
| `QUAD_CONFIG_PATH` | `config/config.yaml` | `python -m quad` launcher path (`__main__.py:167`). The `quad` subcommands use their own `--config/-c` option instead and ignore it. |
| `QUAD_LOG_LEVEL` | `INFO` | structlog level (`__main__.py:76`) |
| `QUAD_LOG_FORMAT` | `json` | `json` or `text` (`__main__.py:77`) |
| `TELEGRAM_BOT_TOKEN` | -- | Injected into `telegram.bot_token` (`orchestrator.py:425`) |
| `TELEGRAM_NOTIFICATION_CHAT_ID` | -- | Injected after config load (`orchestrator.py:430`) |
| `GROQ_API_KEYS` | -- | Comma-separated key pool for rotation (`src/quad/ai/groq.py:285`) |
| `QUAD_CREDENTIAL_KEY` | -- | Fernet key for Bybit credential encryption (required by `docker-compose.yml`) |
| `QUAD_SUPERVISOR_ENABLED` | `false` | Per-tenant worker supervisor (`src/quad/api/app.py:113`) |
| `QUAD_ALLOW_LIVE` | `false` | Operator live-trading kill switch (`src/quad/api/routes_exchange.py:41`) |

`QUAD_DATA_DIR` and `TELEGRAM_ADMIN_IDS` appear in older docs but are **not read
anywhere in the codebase**. Drop them.

### Heuristic mapping

Any remaining `QUAD_*` or `BYBIT_*` variable is converted by `_env_to_config_key`
(`manager.py:524`): strip the prefix, lowercase, split on `_`, join with `.`, and
apply a small set of section renames (`log`→`logging`, `db`→`persistence`,
`health`→`monitoring.health_server`, `telegram`→`telegram`). Values are coerced
to bool/int/float where possible (`manager.py:570`).

The heuristic is **unreliable** — `QUAD_LOG_LEVEL` becomes `logging.level`, but
`logging` is not a `QuadConfig` section, and `QUAD_RISK_MAX_POSITION_SIZE`
becomes `risk.max.position.size` (four levels) rather than
`risk.max_position_size`. Top-level sections created this way survive only as
unvalidated pass-through dicts (`manager.py:391-393`); nested paths inside a real
section are dropped by Pydantic without any warning.

**Prefer the mapped names.** If a variable is not in `ENV_VAR_MAP`, it is not
supported.

---

## Env Var Expansion in YAML

`${VAR}` placeholders in `config.yaml` string values are expanded with
`os.path.expandvars` before the layers merge (`manager.py:367`, implementation
at `manager.py:654`). `~` is also expanded with `os.path.expanduser`.

**Only the plain `${VAR}` form is supported.** The shell-style
`${VAR:-default}` default syntax is **not** — `os.path.expandvars` leaves
`${VAR:-default}` unexpanded or mangles it depending on the platform, so
`dsn: "${DATABASE_URL:-data/quad.db}"` does not do what it looks like it does.
Write the fallback as a real YAML default:

```yaml
persistence:
  dsn: ${DATABASE_URL}     # or just a literal: dsn: "data/quad.db"
```

Layer 2 then overrides it anyway when `DATABASE_URL` is set, so a plain
`${DATABASE_URL}` is the correct form.

---

## Runtime Overrides and Reload

`ConfigManager` exposes two runtime APIs:

```python
config.set("risk.max_positions", 3)   # highest-priority layer
config.reload()                        # re-read config.yaml, re-apply all layers
```

Be accurate about what they do:

| Aspect | Reality |
|---|---|
| File watcher | **None.** Nothing polls `config.yaml`; edits are not picked up until `reload()` is called. |
| Who calls them | **Nothing in `src/` calls `set()` or `reload()`.** They are a library API only. |
| Validation | `set()` writes the raw value into the resolved dict (`manager.py:158`). It does **not** re-run `QuadConfig.model_validate`, so a bad value is not rejected and does not gain a schema default. |
| `reload()` | Re-runs the full merge **including** Pydantic validation (`manager.py:199` → `_load_all_layers`), so it *does* re-validate. Runtime overrides are re-applied on top and survive. |
| Callbacks | `on_change(cb)` fires `cb(key, old, new)` on both `set()` and `reload()` (`manager.py:396`). No component in `src/` registers one. |
| CLI | There is **no** `quad config set` or `quad config reload` command. `quad config` is a read-only overview of the resolved config (`src/quad/cli/app.py:465`). |

Because nothing subscribes, the orchestrator snapshots the config once at
startup (`orchestrator.py:402`) and never re-reads it. **A config change requires
a restart.** Do not rely on hot reload for risk, exchange, database, or mode
settings.

---

## Key Settings Reference

| Setting | Default | Env override |
|---|---|---|
| `_mode` | `bybit` | `QUAD_MODE` |
| `_dry_run` | `true` | `QUAD_DRY_RUN` |
| `trading.leverage` | `1` | -- |
| `trading.default_strategy` | `trend_following` | `QUAD_DEFAULT_STRATEGY` |
| `trading.max_cycle_interval` | `60` | -- |
| `trading.ai_cycle_interval` | `3600` | -- |
| `trading.margin_mode` | `isolated` | -- |
| `trading.position_mode` | `one_way` | -- |
| `exchange.name` | `bybit` | -- |
| `exchange.testnet` | `true` | `BYBIT_TESTNET` |
| `exchange.api_key` / `api_secret` | `null` | `BYBIT_API_KEY` / `BYBIT_API_SECRET` |
| `exchange.rate_limit.max_weight` | `2000` | -- |
| `risk.max_positions` | `1` | -- |
| `risk.max_position_size` | `1000.0` | -- |
| `risk.max_portfolio_risk_pct` | `20.0` | -- |
| `risk.max_daily_loss_usd` | `500.0` | -- |
| `risk.max_drawdown_pct` | `25.0` | -- |
| `risk.correlation_threshold_pct` | `60.0` | -- |
| `risk.max_leverage` | `10` | -- |
| `risk.min_distance_to_liquidation_pct` | `0.2` | -- |
| `risk.liquidation_distance_fraction` | `0.5` | -- |
| `risk.per_position_sl.capital_pct` | `30.0` | -- |
| `risk.per_position_tp.capital_pct` | `50.0` | -- |
| `persistence.dsn` | `quad.db` | `DATABASE_URL` or `QUAD_DSN` |
| `market_data.cache_ttl.order_book` | `5` | -- |
| `market_data.buffer_sizes.ticks` | `1000` | -- |
| `monitoring.health_server.port` | `9090` | `QUAD_HEALTH_PORT` |
| `monitoring.health_server.bind_address` | `0.0.0.0` (forced to `127.0.0.1` when no API key) | -- |
| `monitoring.health_server.api_key` | `""` | `QUAD_HEALTH_API_KEY` |
| `telegram.enabled` | `true` | -- |
| `ai.enabled` | `true` | `QUAD_AI_ENABLED` |
| `ai.model` | `groq/compound-mini` | `QUAD_AI_MODEL` |
| `ai.timeout` | `30` | `QUAD_AI_TIMEOUT` |
| `ai.max_requests_per_day` | `950` | `QUAD_AI_MAX_REQUESTS_PER_DAY` |
| `ai.groq.token_budget.max_tokens_per_day` | `100000` | -- |
| `ai.validator.gate_mode` | `warn` | -- |
| `ai.validator.min_confidence_to_trade` | `0.0` | -- |
| `ai.rotation.enabled` | `false` | -- |
| `strategy.trend_following.fast_ema` | `9` | -- |
| `strategy.trend_following.slow_ema` | `21` | -- |
| `strategy.trend_following.trade_capital_usd` | `5` | -- |
| `tradingview_webhook.enabled` | `false` | `QUAD_TRADINGVIEW_WEBHOOK_ENABLED` |
| `tradingview_webhook.secret` | `""` | `QUAD_TRADINGVIEW_WEBHOOK_SECRET` |
| `execution.reconcile_interval_seconds` | `60` | -- |
| `log level` | `INFO` | `QUAD_LOG_LEVEL` |
| `log format` | `json` | `QUAD_LOG_FORMAT` |

`monitoring.health_server.bind_address` deserves a note: the schema default is
`0.0.0.0`, but `HealthServer` overrides it to `127.0.0.1` unless an API key is
configured (`src/quad/monitoring/health.py:85-92`). Setting `0.0.0.0` alone will
not expose the port — you must also set `QUAD_HEALTH_API_KEY`. See
[docs/deployment.md](deployment.md#security-hardening).

---

## Validation Checklist

Before starting the bot, verify the following:

| # | Check | How to Verify |
|---|---|---|
| 1 | Bybit API keys are valid | `quad balance` queries the exchange, or check the `exchange_adapter_initialized` log event |
| 2 | API permissions are correct | Disable withdrawal permission; enable trading only |
| 3 | Telegram bot token is set | Verify `TELEGRAM_BOT_TOKEN` is set in `.env` |
| 4 | Testnet mode is enabled for initial runs | Set `BYBIT_TESTNET=true` (testnet is the default) |
| 5 | Database path is writable | `quad db-info` prints the file size and per-table row counts |
| 6 | Configuration syntax and schema are valid | `quad config` shows the expected resolved values |
| 7 | Data directory has sufficient space | Check free space (500 MB minimum) |
| 8 | Time sync is accurate | NTP should be within 1 second of UTC |
| 9 | Strategy configuration is valid | `quad strategies` lists the expected strategies |
| 10 | `_mode` is valid | Only `bybit` / `dry_run`; anything else aborts startup |
| 11 | Safety switches are in the expected state | `quad status` shows `Mode`, `Dry Run`, `Testnet`, and `Leverage` |

---

## Security Warnings

**Never commit your `.env` file.** The `.env` file contains API keys and secrets (Bybit API keys, Telegram bot token) that would compromise your trading account and Telegram bot. The `.gitignore` explicitly excludes `.env` from version control. Always use `.env.example` as a template.

**Protect your Telegram bot token.** The `TELEGRAM_BOT_TOKEN` gives full control of your Telegram bot. Anyone with this token can send messages as your bot and intercept bot commands. Never share it or commit it to version control. If compromised, regenerate immediately via @BotFather.

**Use dedicated API keys.** Create Bybit V5 API keys specifically for this bot with only minimum required permissions: enable trading, disable withdrawals. Never use keys from your main account or keys with withdrawal permissions.

**Rotate keys regularly.** Change your Bybit API keys every 90 days.

**Start in dry-run or testnet mode.** Before risking real capital, run the bot with `quad start --dry-run` or `BYBIT_TESTNET=true` (testnet is the default). See [docs/go-live-plan.md](go-live-plan.md) for the full gate list.

**Restrict database access.** The SQLite database contains your trading history and configuration. Use file permissions to restrict access, and never expose the data directory to the public internet.

**Set `QUAD_HEALTH_API_KEY` before exposing the health server.** The health server carries position, balance, and readiness detail. Loopback-only is the default, but behind a reverse proxy the loopback bypass is refused when forwarding headers are present — an exposed health server with no key returns `403` for every probe. See [docs/deployment.md](deployment.md#security-hardening).

**Monitor log files.** Regularly check logs for suspicious activity, unexpected errors, or authorization failures. Configure log rotation to prevent disk exhaustion.
