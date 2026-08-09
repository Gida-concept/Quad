# Quad

_Production-Grade USD-M Futures Trading Bot for Binance_

<p align="center">
  <img src="https://img.shields.io/badge/python-3.12%2B-blue" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/license-MIT-blue" alt="License">
  <img src="https://img.shields.io/badge/docker-ready-2496ED" alt="Docker Ready">
</p>

---

## Executive Summary

Quad is a production-grade, open-source USD-M futures trading bot purpose-built for Binance Futures. It is a **single-process Python 3.12+ asyncio application** that provides a complete trading system: exchange connectivity, market data streaming, futures strategy execution, risk management, backtesting, and both Telegram and CLI interfaces.

Unlike the previous Quadrant project (Node.js/Python dual-runtime, Binance Futures), Quad is:

- **Python-only** -- One language, one process, one deployment
- **Futures-native** -- Built from the ground up for USD-M perpetual and delivery futures
- **Telegram-first** -- Primary user interface via Telegram bot (python-telegram-bot v20+), with CLI for secondary debugging
- **Plugin-based** -- Strategies are pluggable via setuptools entry points
- **Optional AI** -- Groq/LLaMA integration for market analysis and decision support. Deterministic fallback when AI is disabled or unavailable.

Quad is designed for personal use by individual traders who want a self-hosted, reliable, and understandable futures trading system.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                    TELEGRAM INTERFACE (python-telegram-bot)           │
│              (/start, /status, /positions, /balance, /risk, etc.)        │
│                         PRIMARY USER INTERFACE                        │
└─────────────────────────────────┬───────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         CLI INTERFACE (Typer)                         │
│                (start, stop, status, config, backtest)                │
│                        SECONDARY DEBUG INTERFACE                      │
└─────────────────────────────────┬───────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         CONFIG MANAGER                                │
│              (YAML -> env vars -> CLI overrides)                      │
└─────────────────────────────────┬───────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│                            ORCHESTRATOR                                │
│   (State Machine, Trading Cycle, Module Coordination)                │
│                                                                       │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐               │
│  │    State     │  │   Position   │  │    Order     │               │
│  │   Machine    │  │   Tracker    │  │  Lifecycle   │               │
│  └──────────────┘  └──────────────┘  └──────────────┘               │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐               │
│  │    Fill      │  │   Strategy   │  │    Market    │               │
│  │  Reconciler  │  │   Registry   │  │   Data Mgr   │               │
│  └──────────────┘  └──────────────┘  └──────────────┘               │
└─────────────────────────────────┬───────────────────────────────────┘
                                  │
         ┌───────────────────────┬┴┬───────────────────────┐
         │                       │                         │
         ▼                       ▼                         ▼
┌──────────────────┐  ┌──────────────────┐  ┌──────────────────────┐
│   RISK MANAGER   │  │    STRATEGY      │  │  EXCHANGE ADAPTER    │
│                  │  │    PLUGIN        │  │                      │
│  9 Pre-Trade     │  │                  │  │ Binance USD-M       │
│   Gates          │  │  Trend Following │  │  Futures API         │
│  7 Circuit       │  │  Grid Trading    │  │  (REST + WebSocket)  │
│   Breakers       │  │  Mean Reversion  │  │                      │
│  Position Sizing │  │  DCA Bot         │  │  Paper Trading       │
│  (Leverage-Adj.) │  │  Market Making   │  │  Mock Exchange       │
└──────────────────┘  └──────────────────┘  └──────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│                        PERSISTENCE (SQLite)                             │
│    accounts, positions, orders, trades, decisions, contracts, stats   │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│                         BACKTEST ENGINE                                │
│            (Tick/bar replay, historical data, reporting)              │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Key Features

- **Futures-Native** -- Built for Binance USD-M perpetual and delivery futures with dual position mode (HEDGE/ONE_WAY), isolated or cross margin, and full leverage control
- **Telegram Commands** -- 18+ user commands (/start, /status, /help, /balance, /positions, /orders, /funding_rate, /book, /strategies, /execute, /risk, /kill, /cancel, /settings, /set, /analyze, /ai_strategy, /ai_status, /ai_decision)
- **5 Built-in Strategies** -- Trend following, grid trading, mean reversion, DCA bot, market making, each with leverage-aware sizing and configurable position limits
- **Plugin Architecture** -- Write custom strategies as Python classes with setuptools entry point registration
- **9-Gate Risk Pipeline** -- Every trade validated against max positions, portfolio risk, daily loss, drawdown, liquidation risk, funding rate cost, leverage limit, position concentration, and correlation
- **7 Circuit Breakers** -- P&L drawdown, daily loss, consecutive losses, position growth, liquidation cascade, funding rate spike, and volatility with graduated responses
- **Leverage-Adjusted Position Sizing** -- Position sizing adapted for futures with min position size checks, leverage-aware notional, and margin utilization tracking
- **Backtesting Engine** -- Test strategies against historical data before risking capital
- **Telegram + CLI Interface** -- Primary control via Telegram bot (python-telegram-bot v20+), with Typer CLI for secondary debugging
- **SQLite Persistence** -- 16-table schema with aiosqlite for zero-config operation
- **Docker Deployable** -- Single-container deployment with health checks and Prometheus metrics
- **Hot-Reload Configuration** -- Risk and strategy parameters update without restart
- **Structured Logging** -- JSON-formatted logs for easy parsing and analysis

---

## Technology Stack

| Component | Technology | Purpose |
|---|---|---|
| Runtime | Python 3.12+ asyncio | Single-process event-driven architecture |
| Telegram Bot | python-telegram-bot v20+ | Primary user interface via Telegram |
| CLI Framework | Typer | Secondary command-line interface for debugging |
| Exchange API | Binance USD-M Futures (REST + WebSocket) | Market data, account, order execution |
| Persistence | SQLite + aiosqlite | Single file, zero config |
| Configuration | PyYAML + python-dotenv | Layered config with hot-reload |
| Logging | structlog | Structured JSON logging |
| Async HTTP | aiohttp / httpx | REST API calls to Binance |
| Containerization | Docker | Single-container deployment |
| Monitoring | Built-in HTTP server | Health checks, Prometheus metrics |

---

## Quick Start

> **Python version**: Quad targets **Python 3.12+** (matching the Docker images
> and CI). The codebase also runs on **3.10+** (`requires-python = ">=3.10"`), so
> existing 3.10 environments work, but 3.12 is the reference runtime.

```bash
# Clone and install
git clone https://github.com/your-org/quad.git
cd quad
pip install -e .

# Configure
cp .env.example .env
# Edit .env with your Binance API keys (optional for dry-run)
# Set TELEGRAM_BOT_TOKEN from @BotFather (required for Telegram interface)
cp config/config.default.yaml config/config.local.yaml
# Edit config.local.yaml with your preferences

# Run in dry-run mode (safest first step)
quad start --dry-run

# Check status
quad status

# View available strategies
quad strategies

# Run a backtest
quad backtest --strategy covered_call --symbol BTC

# Full command reference
quad --help
```

---

## Telegram Commands

Quad's primary user interface is a Telegram bot. All bot operations are available through Telegram commands, with the CLI serving as a secondary interface for debugging.

### Commands

| Command | Description |
|---|---|
| `/start` | Welcome message with available commands |
| `/status` | Show bot health, position summary, PnL, risk status |
| `/positions` | List all open positions with P&L |
| `/orders` | Show open or pending orders |
| `/balance` | Account balances, total USDT value |
| `/funding_rate` | Current funding rates across tracked symbols |
| `/book <symbol>` | Show order book depth for a symbol |
| `/strategies` | List available strategies |
| `/execute` | Interactive multi-step strategy execution flow |
| `/risk` | Show risk status and circuit breaker state |
| `/kill` | Emergency kill switch (requires confirmation) |
| `/cancel <id>` | Cancel an order by its ID |
| `/settings` | Current configuration overview |
| `/set <key> <value>` | Set a configuration value at runtime |
| `/leverage` | Set or view leverage for a symbol |
| `/position_mode` | Toggle between ONE_WAY and HEDGE position mode |
| `/liquidation_warnings` | Show liquidation risk warnings for open positions |
| `/market_regime` | Show detected market regime (trending, ranging, volatile) |
| `/analyze` | AI analysis of current market conditions |
| `/ai_strategy` | AI strategy recommendation |
| `/ai_status` | AI trading system status and metrics |
| `/ai_decision` | AI-driven trading decision (ENTER/EXIT/HOLD) |
| `/help` | Show available commands |

### Setup

1. Create a bot via [@BotFather](https://t.me/botfather) on Telegram
2. Set the bot token as `TELEGRAM_BOT_TOKEN` in your `.env` file
3. The bot uses **polling mode** (no webhook configuration needed)

---

## Documentation

| Document | Description |
|---|---|
| [Architecture](docs/architecture.md) | System architecture, data flow, 12 design decisions |
| [API Reference](docs/api.md) | Plugin interfaces, repository pattern, health server |
| [Interface Commands](docs/interface-commands.md) | Full command reference for Telegram bot (primary) and Typer CLI (secondary) |
| [Configuration](docs/configuration.md) | YAML config files, env vars, hierarchy, hot-reload |
| [Deployment](docs/deployment.md) | Docker and direct deployment, backup, security |
| [Risk Management](docs/risk-management.md) | Pre-trade gates, circuit breakers, position sizing |
| [Strategy Development](docs/strategy-development.md) | Writing custom strategy plugins |
| [Troubleshooting](docs/troubleshooting.md) | Common issues and solutions |
| [Changelog](docs/changelog.md) | Version history |

---

## Project Structure

```
quad/
├── .env.example              # Environment template
├── docker-compose.yml        # Docker orchestration
├── Dockerfile                # Container definition
├── README.md                 # This file
├── pyproject.toml            # Python project config
├── setup.py                  # Package installation
├── requirements.txt          # Python dependencies
│
├── config/                   # Configuration files
│   └── config.default.yaml   # Default configuration
│
├── data/                     # Runtime data directory
│   ├── logs/                 # Log files
│   └── backups/              # Database backups
│
├── src/quad/                 # Source code
│   ├── __init__.py
│   ├── main.py               # Application entry point (Typer + Telegram)
│   ├── cli/                  # Typer CLI commands (secondary interface)
│   ├── telegram/             # Telegram bot interface (primary interface)
│   ├── config/               # Configuration manager
│   ├── engine/               # Orchestrator, state machine
│   ├── exchange/             # Exchange adapters (Binance, paper, mock)
│   ├── strategy/             # Strategy base class + built-in strategies
│   ├── risk/                 # Risk manager, gates, circuit breakers
│   ├── execution/            # Order gateway, TWAP
│   ├── market_data/          # WebSocket streaming, data cache
│   ├── persistence/          # SQLite database, repositories, models
│   ├── monitoring/           # Health server, metrics
│   ├── backtesting/          # Backtest engine
│   └── types/                # Shared type definitions
│
└── docs/                     # Documentation
    ├── architecture.md
    ├── api.md
    ├── interface-commands.md
    ├── configuration.md
    ├── deployment.md
    ├── risk-management.md
    ├── strategy-development.md
    ├── troubleshooting.md
    └── changelog.md
```

---

## Bot Architecture & Data Flow

Quad is a **single-process, event-driven, asyncio Python application**.  All subsystems run inside one process, coordinated by the `QuadOrchestrator`.

### Startup Sequence (`python -m quad`)

When the user runs `python -m quad`, the following happens in order:

```
__main__.py  -->  QuadOrchestrator()  -->  orchestrator.run_forever()
```

1. **Logging configuration** -- structlog is configured (JSON format by default, log level from `QUAD_LOG_LEVEL`).
2. **QuadOrchestrator constructor** -- lightweight, stores config path.  No subsystems are created yet (lazy initialisation).
3. **`run_forever()`** -- Registers signal handlers (SIGTERM/SIGINT), then calls `start()`.
4. **`start()`** creates all subsystems in strict dependency order:

   | Order | Subsystem | What happens |
   |-------|-----------|-------------|
   | 1 | ConfigManager | Loads `config.default.yaml`, overlays `config.local.yaml`, overlays env vars. Resolves `${VAR}` substitutions. |
   | 2 | DatabaseManager | Connects to SQLite, runs DDL and migrations. |
   | 3 | ExchangeAdapter | Created via factory (`binance`/`paper`/`mock` based on mode). Connects and authenticates. |
   | 4 | MarketDataEngine | Starts WebSocket subscriptions, initialises price buffers, funding rate cache, order book cache, and mark price cache. |
   | 5 | RiskManager | Initialises 9 pre-trade gates, 7 circuit breakers, leverage-adjusted position sizer. |
   | 6 | ExecutionEngine | Starts order gateway (UUID idempotency), background reconciliation loop. |
   | 7 | Strategies | Loads all auto-registered strategies (5 built-in) via `__init_subclass__`. |
   | 8 | QuadBot (Telegram) | Initialises PTB v20+ Application, registers 18+ command handlers, 5 recurring jobs. Starts polling. |
   | 9 | HealthServer | Starts aiohttp HTTP server on port 9090 (configurable) with `/health`, `/readiness`, `/liveness`, `/metrics`. |
   | 10 | MetricsCollector | Creates in-memory metrics registry (gauges, counters, histograms). |
   | 11 | Groq AI Client | Lazy initialisation -- only created if `GROQ_API_KEY` is set. Wraps `groq.AsyncGroq`. |
   | 12 | TradingView Webhook | Registers `POST /webhook/tradingview` route on HealthServer (if `tradingview_webhook.enabled: true`). |

5. **`run_forever()`** creates a background task for the main trading cycle, then waits for a stop signal.

Shutdown is the **reverse order**, with each subsystem given individual try/except protection so a failure in one does not prevent the others from stopping.

### Trading Loop Cycle

The main cycle runs as a background `asyncio.Task` at a configurable interval (default 60s):

```
+-----------------------------------------------------------------+
|                     TRADING CYCLE (every ~60s)                   |
|                                                                   |
|  1. Account State --- ExchangeAdapter.get_account()              |
|     |                ExchangeAdapter.get_positions()              |
|     |                ExchangeAdapter.get_open_orders()            |
|     v                                                            |
|  2. Market Data --- MarketDataEngine.get_funding_rate()          |
|     |               MarketDataEngine.get_order_book()            |
|     |               MarketDataEngine.get_mark_price()            |
|     |               MarketDataEngine.get_ticker()                |
|     |                (for BTCUSDT, ETHUSDT, ...)                  |
|     v                                                            |
|  3. Evaluate ------- StrategyContext(account, positions,         |
|     Strategies        funding_rates, order_books, mark_prices,   |
|     |                 config)                                     |
|     |                For each active strategy:                    |
|     |                  strategy.evaluate(context) -> Action[]     |
|     v                                                            |
|  4. Risk Check ----- For each Action:                            |
|     |                RiskManager.evaluate(action, context)        |
|     |                  -> 9 pre-trade gates                       |
|     |                  -> Circuit breaker check                   |
|     |                  -> Leverage-adjusted position sizing       |
|     v                                                            |
|  5. Execute -------- ExecutionEngine.execute(action)             |
|     |                  -> OrderGateway.submit()                   |
|     |                  -> (optional) TwapSlicer for large orders  |
|     v                                                            |
|  6. Monitor -------- UpdateMetrics() -> gauges, counters         |
|     |                RiskManager.update_monitoring()              |
|     v                                                            |
|  7. Sleep ---------- asyncio.sleep(remaining cycle time)          |
+-----------------------------------------------------------------+
```

Errors in any single cycle step are caught and logged -- the cycle continues on the next interval.  A `CancelledError` cleanly exits the loop.

### Telegram Command Flow

All commands (except `/execute` which is a multi-step `ConversationHandler`) follow this path:

```
Telegram User --- /command --- Telegram Servers
                                     |
                               HTTPS POST (polling)
                                     |
                              PTB Application
                                     |
                          +----------+----------+
                          |                     |
                    CommandHandler          Job Queue
                          |                     |
                   QuadBotCommands         QuadBotJobs
                          |                     |
                          |                     |
                    Subsystem calls -----------> Notifications
                    (market_data, risk,         (status, alerts,
                     execution, db, groq)        daily report)
                          |
                    Markdown response
                          |
                    update.message.reply_text()
                          |
                   ------> User
```



### Data Flow: WebSocket to SQLite

```
Binance USD-M Futures API
        |
   WebSocket Streams
   (!miniTicker@arr, !markPrice@arr@1s, !bookTicker, !forceOrder@arr, user data)
        |
        v
  WebSocketManager
   +-- Route by stream name
   +-- Exponential backoff reconnection
   +-- Dispatch to handlers
        |
        v
  Data Buffers (ring buffers, deque maxlen=1000 per symbol)
   +-- asyncio.Lock for thread-safe access
   +-- Order books, funding rates, mark prices, 24h tickers
        |
        v
  MarketDataEngine
   +-- Coordinates all data subsystems
   +-- get_funding_rate()  --- FundingRateCache (TTL + stampede prevention)
   +-- get_order_book()    --- OrderBookCache
   +-- get_mark_price()    --- MarkPriceCache
   +-- get_ticker()        --- TickerCache
        |
        v
  StrategyContext --- Strategy.evaluate() --- Action[]
        |
        v
  RiskManager.evaluate()
   +-- GatePipeline (9 sequential gates, short-circuits on first failure)
   +-- CircuitBreakerManager (7 tiers)
   +-- PositionSizer (Leverage-adjusted with min position size checks)
        |
        v
  ExecutionEngine.execute()
   +-- OrderGateway.submit() --- Binance Futures REST API
   +-- Background reconciliation loop (60s)
   +-- FillReconciler (detects missed fills, stale orders)
        |
        v
  DatabaseManager / Repositories
   +-- 16 tables: accounts, positions, orders, trades, decisions,
   |              futures_contracts, sessions, performance_snapshots,
   |              circuit_breaker_events, config_changes, error_logs,
   |              funding_payments, liquidation_events, funding_rate_records
   +-- SQLite (aiosqlite)
   +-- Automatic backups (hourly, max 24)
   +-- Automatic snapshots (60s)
```

### Groq AI Integration

The Groq AI subsystem is **optional** -- it only activates when a `GROQ_API_KEY` is set in the environment.

```
            QuadOrchestrator
                  |
          GroqClient (wraps groq.AsyncGroq)
           +-- Model: llama-3.1-8b-instant (default)
           +-- 131K context window
           +-- Automatic retry with exponential backoff + jitter
           +-- Rate-limit handling (RateLimitError -> backoff)
                  |
     +------------+------------+
     |            |            |
     v            v            v
analysis.py  rationale.py  strategist.py
     |            |            |
     v            v            v
  Telegram     Execution    Telegram
  /analyze     Engine       /ai_strategy
  (market      (trade       (strategy
   analysis)    rationale)   recommendation)

Integration points:
- Telegram commands `/analyze` and `/ai_strategy` -- on-demand market analysis and strategy recommendation
- `describe_action()` -- called when the bot enters/exits a trade to generate a natural-language explanation of the reasoning
- `analyze_chart_data()` -- analyses OHLCV price data (e.g., from TradingView alerts) for support/resistance, trends, patterns
```

### TradingView Webhook Flow

TradingView webhook alerts are received via a `POST /webhook/tradingview` endpoint on the HealthServer:

```
TradingView Alert (Pine Script strategy)
        |
   Webhook POST --------> HealthServer (port 9090)
   Content-Type:          |
   application/json       |  POST /webhook/tradingview
        |                 |
        v                 v
   parse_alert() ----> convert_to_action()
   +-- JSON parsing    +-- Extracts: symbol, side, quantity, price
   +-- Key normalise   +-- Maps TV actions (buy/sell/flat) to Quad sides
   +-- Inner message   +-- Returns TradingViewSignal
        |
        v
   ExecutionEngine.execute()
   +-- Risk check via RiskManager
   +-- Order placement via OrderGateway
   +-- Logging + metrics
```

The webhook receiver:
- Validates an optional shared secret (config `tradingview_webhook.secret`)
- Accepts standard TradingView JSON alert formats (`{{ticker}}`, `{{strategy.order.action}}`, etc.)
- Routes approved signals through the full risk pipeline before execution
- Logs all received alerts with payload previews for debugging

---

## Project Principles

1. **Safety first** -- Risk checks run before every trade. Circuit breakers protect against catastrophic loss.
2. **Pluggable architecture** -- Exchange adapters and strategy plugins enable extensibility without core changes.
3. **Deterministic strategies** -- Futures trading uses defined logic, not ML models. Strategies are code, not black boxes.
4. **Backtesting-first** -- Every strategy can be backtested against historical data before going live.
5. **Self-hosted and simple** -- SQLite file-backed database, single Python process, no external database server required.

---

## License

MIT License

Copyright (c) 2026 Quad

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
