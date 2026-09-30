# Quad

_USDT Perpetual Futures Trading Bot for Bybit_

<p align="center">
  <img src="https://img.shields.io/badge/python-3.10%2B-blue" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/license-MIT-blue" alt="License">
  <img src="https://img.shields.io/badge/docker-ready-2496ED" alt="Docker Ready">
</p>

---

## Executive Summary

Quad is a production-grade, open-source USDT perpetual futures trading bot purpose-built for Bybit (USDT perpetual / category=linear). It is a **single-process Python 3.10+ asyncio application** that provides a complete trading system: exchange connectivity, market data streaming, futures strategy execution, risk management, backtesting, and both Telegram and CLI interfaces.

Quad is **Python-only** — one language, one process, one deployment:

- **Python-only** -- One language, one process, one deployment
- **Futures-native** -- Built from the ground up for USDT perpetual futures
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
│  9 Pre-Trade     │  │                  │  │ Bybit USDT Perp    │
│   Gates          │  │  Trend Following │  │  (category=linear) │
│  7 Circuit       │  │                  │  │  REST + WebSocket   │
│   Breakers       │  │                  │  │                     │
│  Position Sizing │  │                  │  │  Testnet / Live     │
│  (Leverage-Adj.) │  │                  │  │  only               │
└──────────────────┘  └──────────────────┘  └──────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│                        PERSISTENCE (SQLite)                             │
│  20 tables — see docs/architecture.md for the full schema map          │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│                         BACKTEST ENGINE                                │
│            (Tick/bar replay, historical data, reporting)              │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Key Features

- **Futures-Native** -- Built for Bybit USDT perpetual futures with one-way and hedge position modes, isolated or cross margin, and full leverage control
- **Telegram Commands** -- 25 command handlers (see [Telegram Commands](#telegram-commands)): `/start`, `/status`, `/help`, `/balance`, `/positions`, `/orders`, `/funding_rate`, `/book`, `/strategies`, `/execute`, `/risk`, `/kill`, `/cancel`, `/settings`, `/set`, `/leverage`, `/position_mode`, `/liquidation_warnings`, `/market_regime`, `/analyze`, `/ai_strategy`, `/ai_status`, `/ai_decision`, `/exchange`
- **1 Built-in Strategy** -- Trend following with leverage-aware sizing and configurable position limits
- **Plugin Architecture** -- Write custom strategies as Python classes with setuptools entry point registration
- **9-Gate Risk Pipeline** -- Every trade validated against max positions, portfolio risk, daily loss, drawdown, liquidation risk, funding rate cost, leverage limit, position concentration, and correlation
- **7 Circuit Breakers** -- P&L drawdown, daily loss, consecutive losses, position growth, liquidation cascade, funding rate spike, and volatility with graduated responses
- **Leverage-Adjusted Position Sizing** -- Position sizing adapted for futures with min position size checks, leverage-aware notional, and margin utilization tracking
- **Backtesting Engine** -- Test strategies against historical data before risking capital
- **Telegram + CLI Interface** -- Primary control via Telegram bot (python-telegram-bot v20+), with Typer CLI for secondary debugging
- **SQLite Persistence** -- 20-table schema with aiosqlite for zero-config operation, plus optional Postgres via asyncpg
- **Docker Deployable** -- Single-container deployment with health checks and Prometheus metrics
- **Hot-Reload Configuration** -- Risk and strategy parameters update without restart
- **Structured Logging** -- JSON-formatted logs, with per-cycle correlation IDs and persistent `error_logs` storage

---

## Technology Stack

| Component | Technology | Purpose |
|---|---|---|
| Runtime | Python 3.10+ asyncio | Single-process event-driven architecture |
| Telegram Bot | python-telegram-bot v20+ | Primary user interface via Telegram |
| CLI Framework | Typer | Secondary command-line interface for debugging |
| Exchange API | Bybit V5 via pybit SDK (`BybitFuturesAdapter`, category=linear) | Market data, account, order execution |
| Persistence | SQLite + aiosqlite | Single file, zero config |
| Configuration | PyYAML + python-dotenv | Layered config with hot-reload |
| Logging | structlog | Structured JSON logging |
| Containerization | Docker | Single-container deployment (Python-only, no Node.js) |
| Monitoring | Built-in HTTP server | Health checks, Prometheus metrics |

---

## Quick Start

> **Python version**: Quad targets **Python 3.10+** (matching the Docker images
> and CI). The codebase runs on **3.10+** (`requires-python = ">=3.10"`).

```bash
# Clone and install
git clone https://github.com/your-org/quad.git
cd quad
pip install -e ".[dev]"

# Configure
cp .env.example .env
# Edit .env with your Bybit API keys (BYBIT_API_KEY / BYBIT_API_SECRET)
# Set TELEGRAM_BOT_TOKEN from @BotFather (required for Telegram interface)

# Run in dry-run mode against Bybit testnet (safest first step)
quad start --dry-run
# ...or equivalently:
python -m quad

# Check status of a running instance
quad status
quad health

# View available strategies
quad strategies

# Full command reference
quad --help
```

> **Live trading is a deliberate, multi-step opt-in.** Setting
> `exchange.testnet: false` alone is not enough: `quad start --live`
> additionally refuses to run while `_dry_run: true`, and the exchange
> adapter, the execution engine and the orchestrator each block orders
> independently. See [docs/go-live-plan.md](docs/go-live-plan.md).

---

## Telegram Commands

Quad's primary user interface is a Telegram bot. All bot operations are available through Telegram commands, with the CLI serving as a secondary interface for debugging.

### Commands

| Command | Description | Access |
|---|---|---|
| `/start` | Welcome message, or link a chat with `/start <pairing-code>` | public |
| `/status` | Show bot health, position summary, PnL, risk status | bound |
| `/positions` | List all open positions with P&L | bound |
| `/orders` | Show open or pending orders | bound |
| `/balance` | Account balances, total USDT value | bound |
| `/funding_rate` | Current funding rates across tracked symbols | bound |
| `/book <symbol>` | Show order book depth for a symbol | bound |
| `/strategies` | List available strategies | bound |
| `/execute` | Interactive multi-step strategy execution flow | **operator only** |
| `/risk` | Show risk status and circuit breaker state | bound |
| `/kill` | Emergency kill switch: halts entries, cancels open orders | **operator only** |
| `/cancel <id>` | Cancel an order by its ID | bound |
| `/settings` | Current configuration overview | bound |
| `/set <key> <value>` | Set a configuration value at runtime | bound (subset: **operator only**) |
| `/leverage [symbol] [value]` | View or set leverage for a symbol | view: bound; set: **operator only** |
| `/position_mode [mode]` | View or set ONE_WAY / HEDGE position mode | view: bound; set: **operator only** |
| `/liquidation_warnings` | Show liquidation risk warnings for open positions | bound |
| `/market_regime` | Show detected market regime (trending, ranging, volatile) | bound |
| `/analyze` | AI analysis of current market conditions | bound |
| `/ai_strategy` | AI strategy recommendation | bound |
| `/ai_status` | AI trading system status and metrics | bound |
| `/ai_decision` | AI-driven trading decision (ENTER/EXIT/HOLD) | bound |
| `/exchange` | Exchange connection status | bound |
| `/help` | Show available commands | public |

"bound" means the chat is linked to a tenant via a pairing code.
"**operator only**" additionally requires the deployment owner's chat
(`TELEGRAM_NOTIFICATION_CHAT_ID`, or any chat when running single-tenant
without a database) — these commands act on the operator's own exchange
account, so a control-plane customer's chat is refused.

> `/kill` halts new entries and **cancels open orders on the exchange**,
> reporting exactly how many were cancelled and how many could not be. It
> deliberately does **not** close open positions.

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
| [Go-Live Plan](docs/go-live-plan.md) | Checklist and gates for enabling live trading |
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
├── requirements.txt          # Pinned Python dependencies
│
├── config/                   # Configuration files
│   └── config.yaml           # Single source of truth
│
├── data/                     # Runtime data directory
│   ├── quad.db               # SQLite database (default)
│   └── workers/              # Per-tenant worker configs (multi-tenant mode)
│
├── src/quad/                 # Source code
│   ├── __init__.py
│   ├── __main__.py           # Entry point (python -m quad, quad-run)
│   ├── supervisor.py         # Multi-tenant per-tenant process manager
│   ├── worker.py             # Per-tenant worker config builder
│   ├── api/                  # quad-api: FastAPI control plane (multi-tenant)
│   ├── cli/                  # Typer CLI commands (secondary interface)
│   ├── bot/                  # Telegram bot interface (primary interface)
│   ├── config/               # Configuration manager + pydantic schema
│   ├── orchestrator/         # Orchestrator, lifecycle, AI rotation
│   ├── exchange/             # Exchange adapters (Bybit USDT perpetual via pybit)
│   ├── strategy/             # Strategy base class + built-in strategies
│   ├── risk/                 # Risk manager, gates, circuit breakers
│   ├── execution/            # Order gateway, reconciler, TWAP
│   ├── market_data/          # WebSocket streaming, buffers, caches
│   ├── persistence/          # SQLite/Postgres models, repositories, migrations
│   ├── monitoring/           # Health server, metrics, error sink, correlation ids
│   ├── security/             # Fernet credential encryption
│   ├── tradingview/          # Webhook parser + signal conversion
│   ├── backtesting/          # Backtest engine
│   └── types/                # Shared type definitions
│
├── tests/                    # Pytest suite (no network / no live keys needed)
│
└── docs/                     # Documentation
    ├── architecture.md
    ├── api.md
    ├── interface-commands.md
    ├── configuration.md
    ├── deployment.md
    ├── go-live-plan.md
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
   | 1 | ConfigManager | Loads `config/config.yaml`, overlays env vars (`QUAD_*`, `BYBIT_*`, `DATABASE_URL`) and runtime `set()` overrides. Resolves `${VAR}` substitutions. |
   | 2 | HealthServer | Starts aiohttp HTTP server (port 9090 by default, `QUAD_HEALTH_PORT`) with `/health`, `/readiness`, `/liveness`, `/metrics` (+ `/ready`, `/live` aliases). Started early so liveness probes answer even while later steps are slow. |
   | 3 | DatabaseManager | Connects to SQLite (or Postgres via `DATABASE_URL`), runs DDL and migrations, then attaches the `error_logs` writer. |
   | 4 | ExchangeAdapter | Created via factory (`bybit` mode; an unknown mode is a hard error). Connects to Bybit — testnet unless `exchange.testnet: false`. Sets leverage/margin/position mode per symbol and **verifies** the exchange agrees; a mismatch aborts startup in live mode. |
   | 5 | MarketDataEngine | Starts WebSocket subscriptions, initialises price buffers, funding rate cache, order book cache, and mark price cache. |
   | 6 | RiskManager | Initialises 9 pre-trade gates, 7 circuit breakers, leverage-adjusted position sizer. |
   | 7 | ExecutionEngine | Starts order gateway (UUID idempotency), background reconciliation loop. Flattens positions left open by a previous run. |
   | 8 | Strategies | Loads all auto-registered strategies (1 built-in: trend_following) via `__init_subclass__`. |
   | 9 | Groq AI Client | Lazy initialisation -- only created if `GROQ_API_KEY` is set. Wraps `groq.AsyncGroq`. |
   | 10 | Optimizer | Created only when Groq, the DB and `retrain.enabled` are all present. |
   | 11 | QuadBot (Telegram) | Initialises PTB v20+ Application, registers 25 command handlers + the `/execute` conversation flow, recurring jobs. Starts polling. Non-fatal if the token is missing. |
   | 12 | MetricsCollector | Creates the in-memory metrics registry and attaches it to the already-running health server. |
   | 13 | TradingView Webhook | Registers `POST /webhook/tradingview` on the running HealthServer (if `tradingview_webhook.enabled: true`), then **verifies the route is actually mounted** — if not, the webhook reports itself disabled rather than silently 404-ing. |

   > The health server starts *before* the TradingView webhook, which is why
   > route registration goes through a catch-all dispatcher
   > (`HealthServer.add_route`): aiohttp freezes its router at application
   > startup, so a route added afterwards would otherwise never exist.

5. **`run_forever()`** creates a background task for the main trading cycle, then waits for a stop signal. Each cycle binds a **correlation id** (`cycle-<hex>`) that is attached to every log line it emits; TradingView webhook requests get their own (`tv-<hex>`), so concurrent activity stays separable in the log stream.

Shutdown is the **reverse order**, with each subsystem given individual try/except protection so a failure in one does not prevent the others from stopping. The `error_logs` writer is flushed *before* the database disconnects.

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
Bybit V5 API (https://api-testnet.bybit.com by default)
        |
   WebSocket Streams
   (tickers, mark prices, funding rates, order book, user data)
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
   +-- OrderGateway.submit() --- Bybit V5 REST API (category=linear)
   +-- Background reconciliation loop (60s)
   +-- FillReconciler (detects missed fills, stale orders)
        |
        v
  DatabaseManager / Repositories
   +-- 20 tables, schema version 10 (SCHEMA_VERSION in persistence/models.py):
   |     trading   (6): accounts, positions, orders, trades, decisions,
   |                    funding_payments
   |     ops      (8): sessions, performance_snapshots, circuit_breaker_events,
   |                    config_changes, error_logs, funding_rate_records,
   |                    liquidation_events, strategy_state
   |     self-opt (2): optimization_runs, optimization_recommendations
   |     tenancy  (5): tenants, exchange_credentials, tenant_config,
   |                    telegram_bindings, pairing_codes
   +-- SQLite (aiosqlite) by default; Postgres via DATABASE_URL (asyncpg)
   +-- Automatic backups (hourly, max 24)
   +-- Automatic snapshots (60s)
```

### Groq AI Integration

The Groq AI subsystem is **optional** -- it only activates when a `GROQ_API_KEY` is set in the environment.

```
            QuadOrchestrator
                  |
          GroqClient (wraps groq.AsyncGroq)
           +-- Model: groq/compound-mini (default)
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
- `ai/validator.py` -- every model response passes through
  `normalize_decision()` before it can reach the risk pipeline. This is the
  guard against a direction/side inversion in the model's output: the
  direction is re-derived from the symbol's own market data, implausible
  quantities are rejected, and `min_confidence_to_trade` gates the rest.
  A malformed or unparseable response becomes HOLD, never a trade.
```

**Operational caveat:** the AI is a single point of control. A Groq outage,
a sustained 429 wall, or a prompt regression leaves the portfolio flat or
stale rather than trading badly. The key pool rotates and a fallback model
exists, but both are the same provider. Monitor `/ai_status`.

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
- **Requires** a shared secret (`tradingview_webhook.secret`, >= 16 chars, or
  `QUAD_TRADINGVIEW_WEBHOOK_SECRET`). There is no unauthenticated mode: if the
  secret is missing the schema rejects the config and the orchestrator
  refuses to arm the webhook (fail-closed).
- Accepts two credential methods: an HMAC-SHA256 `X-Webhook-Signature`
  header (preferred) or a `secret` field in the JSON payload.
- Accepts standard TradingView JSON alert formats (`{{ticker}}`, `{{strategy.order.action}}`, etc.)
- Enforces a 5-minute dedupe window and rejects alerts older than 60 seconds
- Routes approved signals through the same serialised `_trade_lock` path and
  full risk pipeline as AI decisions
- Logs all received alerts with payload previews, each with its own
  `tv-<hex>` correlation id
- Verifies at startup that the route is actually mounted; if it is not, the
  webhook reports itself disabled rather than silently 404-ing

Note: the webhook is a **route on the health server**, so it listens on
`monitoring.health_server.port` (default 9090), not on
`tradingview_webhook.port` — that field is informational only.

---

## Project Principles

1. **Safety first** -- Risk checks run before every trade. Circuit breakers protect against catastrophic loss.
2. **Pluggable architecture** -- Exchange adapters and strategy plugins enable extensibility without core changes.
3. **Deterministic strategies** -- Futures trading uses defined logic, not ML models. Strategies are code, not black boxes.
4. **Backtesting-first** -- Every strategy can be backtested against historical data before going live.
5. **Self-hosted and simple** -- SQLite file-backed database, single Python process, no external database server required for dev (Postgres on Hetzner for production).

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
