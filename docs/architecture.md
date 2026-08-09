# System Architecture

---

## Architecture Overview

Quad is designed around a **layered, pluggable architecture** that separates concerns across five distinct layers. Each layer has a single responsibility and communicates with adjacent layers through well-defined interfaces (abstract base classes and protocols). The design prioritizes safety (risk management before execution), extensibility (plugin-based strategies and exchange adapters), and simplicity (single-process Python, SQLite persistence via aiosqlite).

The **Telegram bot** is the primary user-facing layer, providing real-time trading control via chat commands. The **CLI (Typer)** serves as a secondary interface for debugging and local operations.

```
┌──────────────────────────────────────────────────────────────┐
│               TELEGRAM INTERFACE (python-telegram-bot)         │
│     (/start /status /positions /pnl /risk /strategies /help)  │
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
│ (WebSocket    │ │ (plugabble)│ │ (order gateway, │
│  manager,     │ │            │ │  TWAP splitter, │
│  data store,  │ │ Binance    │ │  slippage est., │
│  normalizer)  │ │ USD-M      │ │  post-trade     │
└────────────────┘ │ Futures    │ │  analysis)      │
                   │ adapter    │ └────────────────┘
                   │ Paper      │
                   │ trading    │
                   │ adapter    │
                   │            │
                   │ Mock       │
                   │ adapter    │
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
│  (16 tables, repository pattern, migrations, snapshot/recovery     │
└────────────────────────┬─────────────────────────────────────┘
                         │
┌────────────────────────▼─────────────────────────────────────┐
│                   BACKTESTING ENGINE                           │
│  (Historical data loader, tick replay, metrics, reports)      │
└──────────────────────────────────────────────────────────────┘
```

---

## Trading Cycle Data Flow

Each trading cycle executes the following sequence:

### Step 1: Market Data Ingestion

The Market Data module maintains persistent WebSocket connections to the Binance USD-M Futures API for real-time data:
- **Mini Ticker Stream:** Real-time 24hr ticker data for all traded symbols (`!miniTicker@arr`)
- **Mark Price Stream:** Real-time mark prices and funding rates for all symbols (`!markPrice@arr@1s`)
- **Book Ticker Stream:** Real-time best bid/ask for all symbols (`!bookTicker`)
- **Force Order Stream:** Real-time liquidation order events (`!forceOrder@arr`)
- **User Data Stream:** Account balance updates, order status, position changes

A REST fallback polls the Binance USD-M Futures API periodically if any WebSocket stream disconnects. All incoming data is validated for sequence numbers and timestamp freshness before being passed to the Data Store.

### Step 2: Strategy Evaluation

The Orchestrator calls the active strategy's `analyze()` method, passing the current market context. The strategy:
1. Examines current market data (funding rates, order book depth, mark prices, 24h ticker)
2. Evaluates existing positions for management actions (close, adjust, reduce)
3. Identifies new opportunities based on its logic
4. Returns a list of suggested actions (open_long, open_short, close_long, close_short, hold, adjust_stop, reduce_position) with parameters

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
4. Submits to Binance USD-M Futures via the adapter
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

Every step is recorded to SQLite via aiosqlite:
- **Orders Table:** Every order submitted with full lifecycle (including futures-specific fields: working_type, position_side, price_protect, avg_fill_price)
- **Trades Table:** Filled trades with complete details
- **Positions Table:** Open and closed positions (including leverage, margin_type, position_side, liquidation_price, initial_margin, maintenance_margin, funding_paid)
- **Decisions Table:** Every strategy decision
- **Risk Events Table:** All risk check results
- **System Events Table:** App-level events, errors, state changes
- **Funding Table:** Funding rate payments and cumulative costs
- **Liquidation Table:** Liquidation events and forced orders
- **Funding Rate Records Table:** Historical funding rate snapshots

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
| **Decision** | Build Quad entirely in Python 3.12+ with asyncio, running as a single process |
| **Rationale** | Options trading requires deterministic strategy execution with access to mathematical libraries (pandas, numpy, scipy). Python's asyncio provides excellent I/O performance for WebSocket streams and API calls. A single process eliminates serialization overhead, simplifies deployment, and avoids the operational complexity of multi-service architectures. |
| **Trade-offs** | No language-level parallelism for CPU-heavy tasks. GIL limits concurrent computation. Backtesting and live trading cannot run simultaneously in the same process. |

### AD-2: Pluggable Exchange Adapters

| Aspect | Detail |
|---|---|
| **Decision** | Abstract the exchange interface behind an `ExchangeAdapter` ABC, enabling swap-in adapters for different exchanges or mock implementations |
| **Rationale** | Decouples trading logic from exchange-specific API details. Enables paper trading (simulated fills using real market data) and mock testing (deterministic responses) without changing core engine code. Future exchange support requires only a new adapter class. |
| **Trade-offs** | Interface design must accommodate all exchange capabilities without being overly generic. Some exchange-specific features may not map cleanly to the abstraction. Additional abstraction layer adds development overhead. |

### AD-3: Plugin-Based Strategy Framework

| Aspect | Detail |
|---|---|
| **Decision** | Strategies are Python classes loaded dynamically via a plugin registry, discovered through setuptools entry points or a strategies directory |
| **Rationale** | Users can write, share, and install strategies without modifying core code. The registry pattern enables third-party strategy packages. Built-in strategies serve as reference implementations and documentation. |
| **Trade-offs** | Plugin API must remain stable, limiting core refactoring flexibility. Version compatibility between plugins and core must be managed. Malicious plugins could compromise the bot. |

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

### AD-6: Binance USD-M Futures API Integration

| Aspect | Detail |
|---|---|
| **Decision** | Target Binance USD-M Futures as the initial (and primary) exchange, using both REST and WebSocket APIs |
| **Rationale** | Binance has the largest futures market by volume, the most complete API, and an active testnet. Their USD-M futures API supports both perpetual and delivery futures with isolated/cross margin and HEDGE/ONE_WAY position modes, making it ideal for automated trading. |
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
│   ├── config.default.yaml   # Default configuration with all keys
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
│   │   ├── base.py           # ExchangeAdapter ABC
│   │   ├── binance.py        # Binance USD-M Futures API
│   │   ├── mock.py           # Mock adapter (testing)
│   │   └── factory.py        # create_exchange factory function
│   ├── market_data/          # Market data engine
│   │   ├── __init__.py
│   │   ├── engine.py         # MarketDataEngine: subscriptions, dispatch
│   │   ├── buffers.py        # Ring buffers for price ticks
│   │   ├── cache.py          # FundingRateCache, OrderBookCache, MarkPriceCache
│   │   ├── historical.py     # Historical data access
│   │   └── websocket.py      # WebSocket connection manager
│   ├── strategy/             # Strategy framework
│   │   ├── __init__.py
│   │   ├── base.py           # Strategy ABC and StrategyRegistry
│   │   ├── factory.py        # Strategy factory functions
│   │   └── trend_following.py  # Trend following strategy
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
│   │   ├── models.py         # 16 table definitions
│   │   └── repositories.py   # Repository classes for all models
│   ├── monitoring/           # Health check and metrics
│   │   ├── __init__.py
│   │   ├── health.py         # HealthServer: HTTP endpoints
│   │   └── metrics.py        # MetricsCollector: Prometheus metrics
│   ├── ai/                   # AI trading assistant
│   │   ├── __init__.py
│   │   ├── prompt.py         # Prompt builder for AI decisions
│   │   ├── groq.py           # Groq LLM client
│   │   ├── context.py        # Market context collection
│   │   ├── ta.py             # Technical indicators
│   │   ├── optimizer.py      # Self-optimization engine
│   │   └── strategist.py     # AI strategist
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
│   │   ├── bot.py            # TelegramBot: PTB initialization
│   │   ├── commands.py       # Command handlers
│   │   └── jobs.py           # Scheduled jobs
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
