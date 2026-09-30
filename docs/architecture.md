# System Architecture

---

## Architecture Overview

Quad is designed around a **layered, pluggable architecture** that separates concerns across five distinct layers. Each layer has a single responsibility and communicates with adjacent layers through well-defined interfaces (abstract base classes and protocols). The design prioritizes safety (risk management before execution), extensibility (plugin-based strategies and exchange adapters), and simplicity (single-process Python, SQLite persistence via aiosqlite).

The **Telegram bot** is the primary user-facing layer, providing real-time trading control via chat commands. The **CLI (Typer)** serves as a secondary interface for debugging and local operations.

```
┌──────────────────────────────────────────────────────────────┐
│               TELEGRAM INTERFACE (python-telegram-bot)         │
│  /start /help /status /balance /positions /orders /risk        │
│  /strategies /execute /kill /analyze /ai_* /exchange           │
│                     PRIMARY USER INTERFACE                      │
└────────────────────────┬─────────────────────────────────────┘
                         │
┌────────────────────────▼─────────────────────────────────────┐
│                      CLI (Typer)                                │
│              (start/stop/status/config commands)                │
│                    SECONDARY DEBUG INTERFACE                     │
└────────────────────────┬─────────────────────────────────────┘
                         │
┌────────────────────────▼─────────────────────────────────────┐
│                    CONFIG MANAGER                              │
│         (YAML config files, env vars, hot-reload support)     │
└────────────────────────┬─────────────────────────────────────┘
                         │
┌────────────────────────▼─────────────────────────────────────┐
│                    TRADING ORCHESTRATOR (asyncio)              │
│         (Main loop, state machine, component wiring)          │
└────────────────────────┬─────────────────────────────────────┘
                         │
         ┌───────────────┼───────────────┐
         ▼               ▼               ▼
┌────────────────┐ ┌────────────┐ ┌────────────────┐
│ MARKET DATA   │ │ EXCHANGE   │ │ EXECUTION      │
│ MODULE        │ │ ADAPTER    │ │ ENGINE          │
│ (WebSocket    │ │ (pybit     │ │ (order gateway, │
│  manager,     │ │  SDK)      │ │  TWAP splitter, │
│  data store,  │ │ Bybit      │ │  slippage est., │
│  normalizer)  │ │ USDT       │ │  post-trade     │
└────────────────┘ │ Perpetual  │ │  analysis)      │
                   │ adapter    │ └────────────────┘
                   │            │
                   └────────────┘
                         │
┌────────────────────────▼─────────────────────────────────────┐
│                     RISK MANAGER                               │
│  (9 pre-trade checks, margin monitor, 7 circuit breakers,     │
│   stop-loss/take-profit, kill switch, liquidation risk)       │
└────────────────────────┬─────────────────────────────────────┘
                         │
┌────────────────────────▼─────────────────────────────────────┐
│                  STRATEGY FRAMEWORK (plugin-based)             │
│  (Abstract base, plugin registry, strategy context,            │
│   1 built-in strategy: trend_following)                         │
└────────────────────────┬─────────────────────────────────────┘
                         │
┌────────────────────────▼─────────────────────────────────────┐
│                  PERSISTENCE LAYER (SQLite)                         │
│  (20 tables, schema v10, repository pattern, migrations)          │
└────────────────────────┬─────────────────────────────────────┘
                         │
┌────────────────────────▼─────────────────────────────────────┐
│                   BACKTESTING ENGINE                           │
│  (Historical data loader, tick replay, metrics, reports)      │
└──────────────────────────────────────────────────────────────┘
```

---

## Startup Order

`QuadOrchestrator.start()` (`src/quad/orchestrator/orchestrator.py`) wires the
subsystems in a fixed dependency order. If any step raises, everything already
initialised is shut down in reverse order and the exception propagates.

| # | Step | Notes |
|---|---|---|
| 1 | `ConfigManager` | 3-layer merge: `config.yaml` → `QUAD_*` / `BYBIT_*` env vars → runtime `set()` overrides. Resolves `_mode` and the cycle interval. |
| 2 | `HealthServer` | Started **second, on purpose**. It has no dependency beyond config, so `/liveness` and `/health` stay reachable while database and exchange init are slow or failing. |
| 3 | `DatabaseManager` | `connect()` → `initialize()` → `migrate()`. |
| 3b | `ErrorLogSink` | Attached immediately after the database, because it needs a live connection. |
| 4 | `ExchangeAdapter` | `create_exchange()` + `connect()`. Bybit V5, `category="linear"`. |
| 4b | Futures account setup | Leverage, margin mode, and position mode per configured symbol, then a **read-back verification** against `get_positions()`. A leverage mismatch or per-symbol failure aborts startup in live mode rather than trading on numbers the exchange never accepted. |
| 5 | `MarketDataEngine` | WebSocket subscriptions + REST fallback. |
| 6 | `RiskManager` | Gates and circuit breakers constructed from the validated config. |
| 7 | `ExecutionEngine` | Requires the risk manager. |
| 7b | Orphan-position flatten | Any position left open by a previous run is closed so a fresh cycle starts flat. Failure is logged, not fatal. |
| 8 | Strategies | `create_default_strategies()` — instantiates every registered strategy that is `enabled` in config. |
| 9 | Groq AI client | Skipped when there is no API key or `ai.enabled` is false. |
| 10 | Optimizer | Requires the Groq client, the database, and `retrain.enabled`. |
| 11 | Telegram bot | Failure is non-fatal: the orchestrator continues without the Telegram interface. |
| 12 | `MetricsCollector` | **Attached to the already-running health server** via `set_metrics_collector()`; it does not start a server of its own. |
| 13 | TradingView webhook | Registered on the health server, then verified (see below). |

### TradingView Webhook Route Registration

`POST /webhook/tradingview` is mounted on the health server, not on a separate
HTTP server. aiohttp freezes its `UrlDispatcher` when the application starts
(`Application.pre_freeze()` → `router.freeze()`) and offers no way to unfreeze
it, so a route added to a live router raises
`RuntimeError: Cannot register a resource into frozen router`.

`HealthServer.start()` therefore pre-registers a **catch-all dispatcher** for
`GET/POST/PUT/PATCH/DELETE` on `/{tail:.*}`
(`src/quad/monitoring/health.py`). `HealthServer.add_route()` only mutates the
`_dynamic_routes` dict that the dispatcher consults, so registration order no
longer matters. The fixed health routes are registered first and aiohttp
resolves in registration order, so they always win over the catch-all.

After registering, the orchestrator calls
`has_route("POST", "/webhook/tradingview")` as a startup self-test. If the
route is not mounted it logs `tradingview_webhook_route_missing` at `critical`
and **disables the webhook**, so status output never reports an "armed" state
for automation that would 404. An enabled webhook with no secret is likewise
fail-closed and disabled outright.

---

## Persistence Schema

`src/quad/persistence/models.py` defines `SCHEMA_VERSION = 10` and registers
**20 models** in `ALL_MODELS`. There is no `contracts` table and no `stats`
table. (`funding_rate_records` DDL exists only inside a migration and is not in
`ALL_MODELS`, so a fresh install does not create it.)

### Trading

| Table | Purpose |
|---|---|
| `accounts` | Exchange balance snapshot per tenant. |
| `positions` | Open and closed positions, including the futures fields (leverage, margin_type, position_side, liquidation_price, margins, funding_paid). |
| `orders` | Every order with its full lifecycle, including working_type, position_side, price_protect, avg_fill_price. |
| `trades` | Individual fills with fees and realised P&L. |
| `decisions` | Every strategy/AI decision plus the outcome-reconciliation columns (predicted_direction, confidence, gate_result, entry/exit price, realized_pnl, outcome). |
| `funding_payments` | Funding settlements; positive = paid, negative = received. |
| `liquidation_events` | Liquidations and forced closes. |
| `strategy_state` | Per-strategy enable flag, params, and status. Unique on `(strategy_name, tenant_id)`. |

### Operations

| Table | Purpose |
|---|---|
| `sessions` | Trading session records (start/end, mode, state, P&L, trade count). |
| `performance_snapshots` | Periodic portfolio value, drawdown, position count, and daily P&L. |
| `circuit_breaker_events` | Breaker trigger events with severity tier and resolution time. |
| `config_changes` | Audit log of configuration changes (old value, new value, source). |
| `error_logs` | Persisted application error events — see below. |

### Self-Optimisation

| Table | Purpose |
|---|---|
| `optimization_runs` | One execution of the self-optimisation cycle. |
| `optimization_recommendations` | Individual recommendations produced by a run. |

### Tenancy

| Table | Purpose |
|---|---|
| `tenants` | One human user / Bybit account. Carries `token_version` for JWT revocation. |
| `exchange_credentials` | Fernet-encrypted Bybit keys, never plaintext. |
| `tenant_config` | Per-tenant market, capital %, leverage, TP/SL, strategy, AI tier, and budget. |
| `telegram_bindings` | `chat_id` ↔ tenant binding. |
| `pairing_codes` | Single-use codes that link an extra chat to a tenant. |

Every per-user table carries a `tenant_id` column. The models are created from
`ALL_MODELS`; migrations then bring an existing database up to
`SCHEMA_VERSION`.

### Error Log Writer

`error_logs` previously had a schema, a model, and read-only repository
readers, but **no writer** — every failure went to stdout and was lost as soon
as logs rotated. `src/quad/monitoring/error_sink.py` closes the loop:
`ErrorLogSink` is a structlog processor, so it observes every log event without
any call site having to remember to report an error. Events at `ERROR` and
above are queued in memory and written by a background flusher, keeping the hot
logging path free of database round-trips. The orchestrator installs it into
the live structlog processor chain right after the database is connected and
flushes it during shutdown while the connection is still open.

---

## Correlation IDs

A single trading cycle fans out over several pairs while the TradingView
webhook can fire concurrently and the Telegram job queue runs independently —
all writing into one log stream. `src/quad/monitoring/correlation.py` binds a
`correlation_id` in a `ContextVar`, so concurrent asyncio tasks each see their
own value and a scope exit restores exactly what was bound before.

| Scope | Format | Source |
|---|---|---|
| Trading cycle | `cycle-<16 hex>` | `new_correlation_id("cycle")` in `_main_cycle_loop` |
| TradingView webhook request | `tv-<16 hex>` | `new_correlation_id("tv")` in the webhook handler |

`structlog_context_processor` injects the ambient id into every event; an id
already bound on the event (via `logger.bind(...)`) wins, so an explicitly
scoped component can override it.

---

## Trading Cycle Data Flow

Each trading cycle executes the following sequence:

### Step 1: Market Data Ingestion

The Market Data module maintains persistent WebSocket connections to the Bybit V5 API for real-time data:
- **Ticker Stream:** Real-time 24hr ticker data for all traded symbols
- **Mark Price Stream:** Real-time mark prices and funding rates for all symbols
- **Order Book Stream:** Real-time best bid/ask for all symbols
- **Liquidation Stream:** Real-time liquidation order events
- **User Data Stream:** Account balance updates, order status, position changes

A REST fallback polls the Bybit V5 API periodically if any WebSocket stream disconnects. All incoming data is validated for sequence numbers and timestamp freshness before being passed to the Data Store.

### Step 2: Strategy Evaluation

The Orchestrator calls the active strategy's `evaluate(context)` coroutine, passing the current `StrategyContext`. The strategy:
1. Examines current market data (funding rates, order book depth, mark prices, 24h ticker)
2. Evaluates existing positions for management actions (close, adjust, reduce)
3. Identifies new opportunities based on its logic
4. Returns a `list[Action]` — `ENTER`, `EXIT`, `HOLD`, `set_stop_loss`, `set_take_profit`, `adjust_stop`, `reduce_position`, plus the legacy aliases `open_long` / `open_short` / `close_long` / `close_short` that the execution engine still accepts

### Step 3: Risk Management Validation

Each suggested action enters the Risk Manager where it must pass nine gates:
1. **Max Positions** -- Total open positions don't exceed configured limit
2. **Portfolio Risk** -- Total portfolio risk stays within bounds
3. **Daily Loss** -- Daily loss hasn't exceeded the configured threshold
4. **Drawdown** -- Portfolio drawdown within acceptable range
5. **Liquidation Risk** -- Position is not too close to liquidation price
6. **Funding Rate Cost** -- Funding rate cost is within acceptable range
7. **Leverage Limit** -- Leverage doesn't exceed configured maximum
8. **Position Concentration** -- No single position too concentrated
9. **Correlation** -- Positions aren't overly correlated

If any check fails, the action is rejected with a specific reason code, logged, and reported.

### Step 4: Order Execution

For approved actions, the Execution Engine:
1. Constructs the appropriate order(s) via the Exchange Adapter (MARKET, LIMIT, STOP, TAKE_PROFIT, STOP_MARKET, TAKE_PROFIT_MARKET, TRAILING_STOP_MARKET)
2. Sets futures-specific order parameters (position_side, working_type, reduce_only, price_protect, closePosition)
3. Applies rate limiting and TWAP splitting for large orders
4. Submits to Bybit V5 API (USDT perpetual, category=linear) via the adapter
5. Sets or verifies leverage and margin type for the symbol
6. Tracks fill status and updates local position state
7. Logs the order to the database

### Step 5: Position Tracking

The Orchestrator tracks all open positions:
- Monitors liquidation prices and margin utilization in real-time via WebSocket
- Tracks funding rate payments and cumulative funding costs
- Updates unrealized P&L each cycle
- Evaluates stop-loss and take-profit conditions
- Triggers position management actions (close, reduce, adjust_stop)

### Step 6: Persistence

Recorded to SQLite (20 tables, schema v10 — see [Persistence Schema](#persistence-schema)):
- **`orders`:** Every order submitted with full lifecycle (including futures-specific fields: working_type, position_side, price_protect, avg_fill_price)
- **`trades`:** Filled trades with complete details
- **`positions`:** Open and closed positions (including leverage, margin_type, position_side, liquidation_price, initial_margin, maintenance_margin, funding_paid)
- **`decisions`:** Every strategy/AI decision, with the direction, confidence, gate result, and resolved outcome
- **`circuit_breaker_events`:** Breaker trigger events with severity tier. There is no dedicated risk-check table — individual gate results live on the decision record and in the log stream.
- **`error_logs`:** App-level ERROR+ events, written by the `ErrorLogSink` structlog processor
- **`funding_payments`:** Funding rate payments and cumulative costs
- **`liquidation_events`:** Liquidation events and forced orders
- **`performance_snapshots`:** Periodic portfolio value, drawdown, position count, daily P&L

### Step 7: Reporting

The Orchestrator periodically:
1. Calculates performance metrics (P&L, win rate, Sharpe ratio)
2. Generates status reports
3. Logs system health metrics
4. Checks for any required maintenance actions

---

## Architecture Decisions

### AD-1: Python-Only, Single-Process Architecture

| Aspect | Detail |
|---|---|
| **Decision** | Build Quad entirely in Python 3.10+ with asyncio, running as a single process |
| **Rationale** | Futures trading requires deterministic strategy execution with access to mathematical libraries (pandas, numpy, scipy). Python's asyncio provides excellent I/O performance for WebSocket streams and API calls. A single process eliminates serialization overhead, simplifies deployment, and avoids the operational complexity of multi-service architectures. |
| **Trade-offs** | No language-level parallelism for CPU-heavy tasks. GIL limits concurrent computation. Backtesting and live trading cannot run simultaneously in the same process. |

### AD-2: Pluggable Exchange Adapters

| Aspect | Detail |
|---|---|
| **Decision** | Abstract the exchange interface behind an `ExchangeAdapter` ABC, enabling plug-in adapters for different exchanges |
| **Rationale** | Decouples trading logic from exchange-specific API details. Enables testnet (simulated fills using real market data) and dry-run mode without changing core engine code. Future exchange support requires only a new adapter class. |
| **Trade-offs** | Interface design must accommodate all exchange capabilities without being overly generic. Some exchange-specific features may not map cleanly to the abstraction. Additional abstraction layer adds development overhead. |

### AD-2b: Bybit-Only Exchange Target (No MCP Server)

| Aspect | Detail |
|---|---|
| **Decision** | Bybit V5 USDT perpetual (`category="linear"`) via the official `pybit` SDK is the only exchange backend. There is no MCP (Model Context Protocol) server, no Node.js dependency, and no SDK fallback layer. |
| **Rationale** | A single exchange backend removes the operational cost of an MCP subprocess (extra memory, IPC latency, Node.js toolchain) and eliminates mode-confusion bugs. Perpetual selection is hard-coded as `CATEGORY="linear"` in `BybitFuturesAdapter`, so there is no futures-vs-perpetual toggle to misconfigure. |
| **Trade-offs** | Single exchange dependency creates counterparty risk. Bybit API changes may require adapter updates. |

### AD-3: Plugin-Based Strategy Framework

| Aspect | Detail |
|---|---|
| **Decision** | Strategies are Python classes registered purely by subclassing `StrategyBase`. `StrategyBase.__init_subclass__` calls `get_name()` and inserts the class into `StrategyBase.registry`, which `StrategyRegistry` reads. `pyproject.toml` declares an **empty** `[project.entry-points."quad.strategies"]` group, but nothing in the code reads entry points (no `importlib.metadata` usage) — it is a reserved hook, not a wired mechanism today. |
| **Rationale** | Users can write and share strategies without modifying core code. Registration by subclassing needs no discovery scan at startup, so a plugin cannot be silently skipped because its metadata is malformed. Built-in strategies serve as reference implementations and documentation. |
| **Trade-offs** | A strategy only registers if its module is imported before `create_default_strategies()` runs; a third-party package that is never imported will not appear. Plugin API must remain stable, limiting core refactoring flexibility. Malicious plugins could compromise the bot. |

### AD-4: SQLite with aiosqlite

| Aspect | Detail |
|---|---|
| **Decision** | Use SQLite via aiosqlite for all persistence, with a repository pattern abstraction layer |
| **Rationale** | SQLite provides ACID compliance, zero-configuration deployment, and is backed up with simple file-level copy or VACUUM INTO. aiosqlite provides async database access compatible with the asyncio event loop. The repository pattern abstracts the database implementation behind clean domain interfaces. |
| **Trade-offs** | No concurrent writes; single-connection pool is sufficient for a single-process bot. File-level locking requires careful WAL mode configuration. Not suitable for multi-process or distributed deployments. |

### AD-5: Telegram-First User Interface (with CLI Secondary)

| Aspect | Detail |
|---|---|
| **Decision** | The primary user interface is a Telegram bot (python-telegram-bot v20+, async polling mode). The Typer-based CLI serves as a secondary interface for debugging and local operations. |
| **Rationale** | Telegram provides push notifications, real-time status updates, and command execution from any device without SSH access. All monitoring (positions, P&L, risk status) and control (start, stop, config) are available via Telegram commands. The CLI remains available for advanced debugging, backtesting, and local operations. |
| **Trade-offs** | Requires internet access to Telegram API. Polling mode adds minimal latency. CLI-only users must set up SSH or tmux. Chat ID whitelist adds an authentication step. |

### AD-6: Bybit V5 USDT Perpetual API Integration

| Aspect | Detail |
|---|---|
| **Decision** | Target Bybit V5 USDT perpetual futures (category=linear) as the exchange, using both REST and WebSocket APIs via the official `pybit` SDK |
| **Rationale** | Bybit offers a unified V5 API for USDT perpetuals, a well-documented testnet environment (https://api-testnet.bybit.com, the default), and competitive futures liquidity. Their API supports isolated/cross margin and one-way/hedge position modes, making it ideal for automated trading. |
| **Trade-offs** | Single exchange dependency creates counterparty risk. Funding rate costs must be managed actively. API changes or deprecations may require adapter updates. |

### AD-7: WebSocket Primary with REST Fallback

| Aspect | Detail |
|---|---|
| **Decision** | WebSocket streams are the primary data source; REST API serves as fallback and for write operations |
| **Rationale** | WebSockets provide real-time futures price updates, position changes, and order status notifications. The REST fallback ensures data continuity during disconnections. Write operations (order placement) use REST for reliability. |
| **Trade-offs** | Dual code paths increase maintenance. WebSocket reconnection logic adds complexity. REST polling during fallback introduces latency and rate limit concerns. |

### AD-8: One Futures Position Per Strategy Signal

| Aspect | Detail |
|---|---|
| **Decision** | A single position maps to one futures contract in a given direction (LONG/SHORT) that is managed as a unit by one strategy |
| **Rationale** | Futures positions have direction, leverage, and margin requirements that must be managed together. HEDGE mode allows simultaneous LONG and SHORT positions in the same symbol, but each side is managed independently by its owning strategy. Each position has a clear strategy assignment. |
| **Trade-offs** | Cannot easily implement multi-leg spread strategies that require simultaneous positions on different contracts. Some cross-symbol strategies (pair trading) require coordination across positions. |

### AD-9: Pre-Trade Risk Validation (9 Gates)

| Aspect | Detail |
|---|---|
| **Decision** | Every trade must pass nine independent risk checks before execution |
| **Rationale** | Futures trading with leverage carries liquidation risk. The nine-check system ensures position limits, portfolio risk bounds, daily loss limits, drawdown constraints, liquidation proximity checks, funding rate cost evaluation, leverage limits, concentration limits, and correlation checks are all verified before capital is committed. |
| **Trade-offs** | Adds latency to each trade decision (~50-100ms per check cycle). Some checks may be too conservative for advanced strategies. Configuration tuning required for different strategy types. |

### AD-10: Circuit Breaker Tiered Response System

| Aspect | Detail |
|---|---|
| **Decision** | Seven circuit breaker types with graduated responses: P&L drawdown, daily loss, consecutive losses, position growth, liquidation cascade, funding rate spike, volatility |
| **Rationale** | Futures positions can experience rapid P&L changes due to leverage and can be liquidated if maintenance margin is breached. A single circuit breaker type is insufficient. Liquidation cascade and funding rate breakers catch risks that P&L-based breakers miss. Graduated responses prevent unnecessary shutdowns while protecting capital proportionally. |
| **Trade-offs** | Seven breaker types increase implementation complexity. Threshold tuning requires experience with futures-specific risk metrics. False positives from volatility breakers during normal market events must be managed. |

### AD-11: Hot-Reloadable Configuration

| Aspect | Detail |
|---|---|
| **Decision** | Risk parameters, strategy settings, and logging configuration are hot-reloadable without restarting the bot |
| **Rationale** | Futures market conditions can change rapidly due to leverage (liquidation risk, funding rate spikes, volatility swings). The ability to tighten risk parameters in real-time without disrupting active positions or WebSocket connections is critical. |
| **Trade-offs** | Risk of accidental misconfiguration taking effect immediately. Validation must occur before applying changes. Some parameters (exchange credentials, database path) logically require a restart. |

### AD-12: Backtesting-First Development

| Aspect | Detail |
|---|---|
| **Decision** | Every strategy should be developable and testable via the backtesting engine before live deployment |
| **Rationale** | Futures strategies have well-defined performance characteristics that can be simulated against historical data. Backtesting catches logic errors, edge cases (liquidation, funding rate costs), and performance characteristics before real capital is at risk. The backtesting engine uses the same strategy code as live trading. |
| **Trade-offs** | Historical data availability for futures may be limited for some symbols. Backtest results may not reflect live execution (slippage, liquidity). Backtesting requires significant historical data storage. |

---

## Project Structure

```
quad/
├── .env.example              # Environment template
├── docker-compose.yml        # Docker orchestration
├── Dockerfile                # Container definition
├── README.md                 # Project readme
├── pyproject.toml            # Python project config
├── setup.py                  # Package installation
├── requirements.txt          # Python dependencies
│
├── config/                   # Configuration files
│   ├── config.yaml        # Default configuration with all keys
│   └── config.local.yaml     # Local overrides (not committed)
│
├── data/                     # Runtime data directory
│   ├── quad.db               # SQLite database file
│   ├── logs/                 # Log files
│   ├── backups/              # Database backups
│   └── historical/           # Historical market data
│
├── src/quad/                 # Source code
│   ├── __init__.py           # Package init, version string
│   ├── cli/                  # Typer CLI commands
│   │   ├── __init__.py
│   │   └── app.py            # Typer app definition and commands
│   ├── config/               # Configuration manager
│   │   ├── __init__.py
│   │   ├── manager.py        # ConfigManager: load, merge, hot-reload
│   │   └── schema.py         # Config validation
│   ├── exchange/             # Exchange adapters
│   │   ├── __init__.py
│   │   ├── base.py           # ExchangeAdapter ABC + shared error hierarchy
│   │   ├── bybit.py          # Bybit V5 USDT perpetual adapter (pybit SDK, category=linear)
│   │   └── factory.py        # create_exchange factory function (bybit-only, testnet default)
│   ├── market_data/          # Market data engine
│   │   ├── __init__.py
│   │   ├── engine.py         # MarketDataEngine: subscriptions, dispatch
│   │   ├── buffers.py        # PriceBuffer, FundingRateRingBuffer
│   │   ├── historical.py     # Historical data access
│   │   └── websocket.py      # WebSocket connection manager
│   ├── strategy/             # Strategy framework
│   │   ├── __init__.py
│   │   ├── base.py           # StrategyBase ABC, ParamSpec, StrategyRegistry
│   │   ├── factory.py        # Strategy factory functions
│   │   └── trend_following.py  # Trend following strategy (the 1 built-in)
│   ├── risk/                 # Risk management system
│   │   ├── __init__.py
│   │   ├── manager.py        # RiskManager: gates, breakers, sizing
│   │   ├── gates.py          # 9 pre-trade check gates
│   │   ├── circuit_breakers.py   # Circuit breaker types
│   │   ├── sizing.py         # Position sizing
│   │   └── exposure.py       # Exposure calculations
│   ├── execution/            # Order execution engine
│   │   ├── __init__.py
│   │   ├── engine.py         # ExecutionEngine: order submission
│   │   ├── gateway.py        # OrderGateway: submit, cancel, bracket
│   │   ├── reconciler.py     # FillReconciler: missed fill detection
│   │   └── twap.py           # TWAPSplitter: large order splitting
│   ├── persistence/          # SQLite persistence layer
│   │   ├── __init__.py
│   │   ├── database.py       # DatabaseManager: connection, migration, backup
│   │   ├── models.py         # 20 table definitions (SCHEMA_VERSION = 10)
│   │   ├── pg.py             # PostgreSQL backend for quad-api
│   │   └── repositories.py   # Repository classes for all models
│   ├── monitoring/           # Health check, metrics, correlation, error sink
│   │   ├── __init__.py
│   │   ├── health.py         # HealthServer: HTTP endpoints + catch-all dispatcher
│   │   ├── metrics.py        # MetricsCollector: Prometheus metrics
│   │   ├── correlation.py    # ContextVar correlation IDs (cycle-<hex>, tv-<hex>)
│   │   └── error_sink.py     # ErrorLogSink: structlog processor -> error_logs
│   ├── ai/                   # AI trading assistant
│   │   ├── __init__.py
│   │   ├── prompt.py         # Prompt builder for AI decisions
│   │   ├── groq.py           # Groq LLM client
│   │   ├── context.py        # Market context collection
│   │   ├── ta.py             # Technical indicators
│   │   ├── analysis.py       # Market analysis helpers
│   │   ├── validator.py      # Decision plausibility validator
│   │   ├── metrics.py        # AI prediction-quality metrics
│   │   ├── optimizer.py      # Self-optimization engine
│   │   └── strategist.py     # AI strategist
│   ├── security/             # Credential handling
│   │   ├── __init__.py
│   │   └── secrets.py        # Fernet encryption for exchange credentials
│   ├── tradingview/          # TradingView webhook integration
│   │   ├── __init__.py
│   │   ├── parser.py         # Alert parser
│   │   └── signals.py        # Signal converter
│   ├── backtesting/          # Backtest engine
│   │   ├── __init__.py
│   │   ├── engine.py         # BacktestEngine: tick/bar replay
│   │   └── models.py         # Backtest models
│   ├── bot/                  # Telegram bot interface
│   │   ├── __init__.py
│   │   ├── bot.py            # QuadBot: PTB initialization + handler registration
│   │   ├── commands.py       # Command handlers
│   │   └── jobs.py           # Scheduled jobs
│   ├── orchestrator/         # Top-level application coordinator
│   │   ├── __init__.py
│   │   └── orchestrator.py   # QuadOrchestrator: start/stop ordering, main cycle
│   └── types/                # Shared type definitions
│       ├── __init__.py
│       ├── market.py         # FundingRate, MarkPrice types
│       ├── domain.py         # Account, Position, Order, Trade types
│       ├── risk.py           # RiskStatus, Action types
│       └── strategy.py       # StrategyContext type
│
└── docs/                     # Documentation
    ├── architecture.md
    ├── api.md
    ├── configuration.md
    ├── deployment.md
    ├── risk-management.md
    ├── strategy-development.md
    └── troubleshooting.md
```
