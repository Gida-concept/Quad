# Changelog

All notable changes to the Quad project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [2.0.0] - 2026-07-26 -- Futures Migration

### Added

- **Phase 1: Core Types & Exchange Adapter** -- Replaced `OptionContract` with `FuturesContract`, `PositionSide` with `FuturesPositionSide` (LONG/SHORT/BOTH). New `MarginType` (ISOLATED/CROSS), `PositionMode` (ONE_WAY/HEDGE), `FundingRate`, `FundingRecord` types. Exchange adapter targets `fapi.binance.com` (futures API) with WebSocket connecting to `fstream.binance.com`. New `Action` type values: "open_long" | "open_short" | "close_long" | "close_short" | "hold" | "adjust_stop" | "reduce_position".
- **Phase 2: Market Data** -- WebSocket streams: `!miniTicker@arr`, `!markPrice@arr@1s`, `!bookTicker`, `!forceOrder@arr`. New caches for order books, funding rates, mark prices, 24h tickers. Endpoints: `get_funding_rate()`, `get_order_book()`, `get_mark_price()`, `get_ticker()`.
- **Phase 3: Execution Engine** -- New order types: MARKET, LIMIT, STOP, TAKE_PROFIT, STOP_MARKET, TAKE_PROFIT_MARKET, TRAILING_STOP_MARKET. Futures-specific params: position_side, working_type, reduce_only, price_protect, closePosition. Leverage/margin type management via `set_leverage()`, `set_margin_type()`, `set_position_mode()`.
- **Phase 4: Strategy System** -- 5 futures strategies (trend_following, grid_trading, mean_reversion, dca_bot, market_making). Auto-registered via `StrategyBase.__init_subclass__`. Strategy signals use `Action` dataclass with futures-relevant types.
- **Phase 5: Risk System** -- 9 gates (MAX_POSITIONS, PORTFOLIO_RISK, DAILY_LOSS, DRAWDOWN, LIQUIDATION_RISK, FUNDING_RATE_COST, LEVERAGE_LIMIT, POSITION_CONCENTRATION, CORRELATION). 7 circuit breakers (PNL_DRAWDOWN, DAILY_LOSS, CONSECUTIVE_LOSSES, POSITION_GROWTH, LIQUIDATION_CASCADE, FUNDING_RATE_SPIKE, VOLATILITY). `FuturesPositionTracker` replaces `ExposureLimiter` with notional/leverage/liquidation proximity/margin utilization/funding rate snapshot tracking. Sizing is leverage-adjusted with min position size check.
- **Phase 6: AI System** -- Context builder fetches funding rates, order books, mark prices from futures data. Prompts use futures-relevant terminology (funding rate analysis, order book imbalance, liquidation risk). 5 strategy recommendations aligned with futures strategies.
- **Phase 7: Persistence** -- SCHEMA_VERSION 3. 16 models (new: FundingPaymentModel, LiquidationEventModel, FundingRateRecordModel). PositionModel: leverage, margin_type, position_side, liquidation_price, initial_margin, maintenance_margin, funding_paid. OrderModel: working_type, position_side, price_protect, avg_fill_price. DecisionModel: symbol field (was contract_symbol). New repositories: FundingRepository, LiquidationRepository.
- **Phase 8: Bot & CLI** -- New Telegram commands: /funding_rate, /book, /leverage, /position_mode, /liquidation_warnings, /market_regime. Removed: /chain, /greeks, /expiry, /opstra. Updated: /status, /positions, /risk, /analyze, /ai_strategy, /settings, /help, /start. Added jobs: funding_rate_countdown, liquidation_warning, funding_cost_report.

### Changed

- **Exchange adapter** now targets `fapi.binance.com` (futures API) instead of `api.binance.com` (spot/options API)
- **WebSocket** connects to `fstream.binance.com` instead of `stream.binance.com`
- **`StrategyContext`** now uses `futures_positions`, `futures_contracts`, `funding_rates`, `mark_prices` instead of `positions`, `option_chain`
- **All documentation** updated to reflect futures migration across all 8 phases
- **Strategy defaults** changed from options strategies (covered_call, CSP, iron_condor, etc.) to futures strategies (trend_following, grid_trading, mean_reversion, dca_bot, market_making)

### Removed

- Options-specific commands: /chain, /greeks, /expiry, /opstra
- Options-specific types: `OptionContract`, `GreekTick`, `PositionSide`
- Options-specific market data: option chains, Greeks WebSocket, IV rank filtering
- Options-specific risk: Greek exposure gates, theta decay checks, volatility percentile checks
- Options-specific strategies: covered_call, cash_secured_put, iron_condor, straddle, strangle, vertical_spread

## [0.5.0] - 2026-07-25

### Added

- **Telegram trade notifications** -- Real-time alerts on trade entry, exit, roll, TP/SL hits via Telegram, wired into orchestrator execution path and deterministic strategy fallback
- **Runtime config editing** -- `/set <key> <value>` command to adjust any setting without restart (TP/SL %, position size, leverage, strategy params, etc.)
- **Technical indicator caching** -- RSI/MACD/Bollinger cached for 60s to avoid redundant computation per cycle
- **Paper position persistence** -- Positions saved to `paper_positions.json` on every change, reloaded on restart
- **Circuit breaker notifications** -- Telegram alerts when any circuit breaker triggers
- **Position sync on startup** -- Exchange positions fetched and reconciled when bot starts

### Changed

- **Parallelized option chain fetches** -- Uses `asyncio.gather()` instead of sequential calls
- **Warmed AI client** -- Groq AsyncGroq client created in `__init__` instead of first `chat()` call
- **Duplicate risk check eliminated** -- `Action.risk_checked` flag prevents double risk evaluation; orchestrator sets it after the first check, execution engine skips its own check
- **Reduced cycle logging** -- Verbose per-cycle logs (`market_context_collected`, `ai_decision_request`, `ai_decision_received`, `ai_decision_hold`, `candles_fetched`, `positions_fetched`, `account_fetched`, `option_chain_fetched`) moved from INFO to DEBUG; only trade events and errors remain at INFO
- **HTTP connection pooling** -- `aiohttp.ClientSession` reused across AI context calls instead of created per call
- **Full config display in /settings** -- Shows the entire config tree in JSON when the orchestrator is available

## [0.4.0] - 2026-07-25

### Changed

- **Removed admin/user distinction** -- Bot now treats all users equally (single-person trading bot). Removed `_is_admin()`, `_check_admin()`, and all admin-only command restrictions. All commands are available to any authenticated chat. `TELEGRAM_ADMIN_IDS` is now fully optional.

## [0.3.0] - 2026-07-25

### Added

- **Serial trade mode (`serial_trade_mode`)** -- When enabled, the bot closes all existing positions before opening a new ENTER trade, replacing the default multi-position parallel behavior
- **Strategy profitability improvements across all 6 strategies** -- Research-backed parameter changes and new logic:
  - **IV Rank filter** (`min_iv_rank`): All premium-selling strategies now check IV percentile before entry, preventing trades in low-IV environments
  - **21 DTE forced gamma exit** (`force_exit_dte`): Multi-leg strategies (iron condor, strangle, vertical spread) auto-close when DTE drops below 21 to avoid gamma risk spikes
  - **Rolling logic** (`roll_when_delta_exceeds`): All strategies can roll threatened legs to the next expiry for a net credit when delta exceeds the threshold
  - **CSP Wheel support** (`wheel_enabled`): Cash-secured puts can auto-transition to covered call on assignment
  - **Configurable deep ITM exit** (`deep_itm_exit_pct`): CSP exit threshold moved from hardcoded 0.8 to configurable 0.85
  - **Schema-code parameter sync**: Fixed mismatches where schema defaults differed from code defaults (IC delta 0.30→0.16, VS wing_width, CC allocation_pct)

### Changed

- **Default delta targets reduced** across all strategies for better risk-adjusted returns:
  - Cash-Secured Put: 0.25 → **0.16**
  - Covered Call: 0.30 → **0.25**
  - Iron Condor: 0.30 → **0.16**
  - Short Strangle: 0.25 → **0.16**
  - Vertical Spread: 0.30 → **0.20**
- **Iron Condor take_profit_pct**: 25 → **50** (backtest-proven optimal)
- **Short Strangle take_profit_pct**: 25 → **50**
- **CSP cash_reserve_pct**: 20 → **30** (safer allocation)
- **CSP stop_loss_pct**: 150 → **200** (fewer prematurely stopped trades)
- **Iron Condor DTE range**: [14, 60] → [30, 45]
- **Short Strangle DTE range**: [14, 45] → [30, 45]
- **Vertical Spread DTE range**: [14, 60] → [21, 45]

### Fixed

- **Config schema mismatch**: Renamed `StrangleParams` → `ShortStrangleParams`, replaced `wing_delta_target` with explicit `call_delta_target`/`put_delta_target`, replaced `long_leg_delta` with `delta_short`/`delta_long`
- **Unused config params**: `roll_when_dte_lt` (CSP/CC) and `allocation_pct` (CC) now actually read from config
- **Docker & deps**: See production-readiness fixes in v0.2.1

## [0.2.1] - 2026-07-25

### Added

- **Database connection retry**: Exponential backoff (1s/2s/4s/8s/16s) in `DatabaseManager.connect()` for transient failures
- **SSL/TLS support**: `ssl` parameter on `connect()`, auto-detected from DSN `sslmode`
- **`is_healthy()` method**: Simple `SELECT 1` health check on `DatabaseManager`
- **Admin auth middleware**: `_is_admin()` and `_check_admin()` on `QuadBot` for centralized auth enforcement
- **PostgreSQL service**: Added to `docker-compose.yml` with healthcheck and named volume
- **3 missing repositories**: `CircuitBreakerEventRepository`, `ErrorLogRepository`, `StrategyStateRepository`
- **Missing deps**: `asyncpg>=0.29.0`, `groq>=0.4.0` added to `requirements.txt`; `typing-extensions>=4.8.0` to `pyproject.toml`

### Fixed

- **C1 — Optimizer crash**: `run_cycle()` stored `create()` return (int) overwriting model — now stores as `run_id`
- **W5 — SELECT \***: 3 repos (`OptimizationRunRepository`, `OptimizationRecommendationRepository`, `ConfigChangeRepository`) now use `self._column_list()`
- **W6 — Dead line**: Removed duplicate `set_clause` assignment in `BaseRepository.update()`
- **W4 — Account query**: `get_by_exchange()` changed from `self.list()` to direct `fetchrow`
- **W11 — Missing setup.py**: Removed from Dockerfile `COPY`
- **W3 — Dead busy_timeout**: Removed from `database.py`, `orchestrator.py`, `schema.py`, `config.default.yaml`

### Removed

- `aiosqlite==0.22.1` from `requirements.txt` (orphaned dep)

## [0.2.0] - 2026-07-14

### Changed

- **Database migration from SQLite to PostgreSQL** -- Complete persistence layer rewrite from aiosqlite to asyncpg, enabling Fly.io cloud deployment. Key changes:
  - Connection: asyncpg connection pool (`min_size=1`, `max_size=5`) replaces single aiosqlite connection
  - Parameter style: `?` placeholders replaced with `$1`, `$2` PostgreSQL numbered parameters
  - DDL: `INTEGER PRIMARY KEY` to `SERIAL PRIMARY KEY`, timestamp columns to `BIGINT`
  - INSERT pattern: `cursor.lastrowid` replaced with `RETURNING id` clause
  - UPSERT: `INSERT OR REPLACE` replaced with `ON CONFLICT DO UPDATE SET ... EXCLUDED.`
  - Schema tracking: key-value `_schema_meta` replaced with `_schema_version` table (`SERIAL PRIMARY KEY`, `version INTEGER`, `applied_at TIMESTAMPTZ`)
  - Configuration: `persistence.db_path` and `persistence.wal_mode` replaced with `persistence.dsn`
  - Environment: `QUAD_DB_PATH` replaced with `DATABASE_URL` / `QUAD_DSN`
  - Documentation: All docs updated to reflect PostgreSQL deployment, pg_dump/pg_restore backup procedures, and connection pooling

## [0.1.0] - 2026-07-07

### Added

- **Initial release** of Quad (reboot from Quadrant Trading Bot)
- **Python 3.12+ asyncio architecture** -- Single-process event-driven design with no dual-runtime complexity
- **Binance Options API integration** -- REST + WebSocket support for European-style cash-settled options
- **Pluggable ExchangeAdapter ABC** -- Abstract base class for exchange integrations (Binance, Paper Trading, Mock)
- **Plugin-based Strategy ABC** -- Register strategies via setuptools entry points; 6 built-in strategies:
  - Covered Call -- Sell OTM calls against underlying
  - Cash-Secured Put -- Sell OTM puts with cash collateral
  - Iron Condor -- Sell OTM put + call spread (low volatility)
  - Straddle -- Buy ATM call + put (high volatility)
  - Strangle -- Buy OTM call + put
  - Vertical Spread -- Buy/sell same-expiry call or put spread
- **Telegram bot interface (python-telegram-bot v20+)** -- Primary user interface with 10 user commands and 4 admin commands:
  - User commands: /start, /status, /positions, /orders, /pnl, /risk, /strategies, /history, /help, /stop
  - Admin commands: /config, /kill, /logs, /backtest
  - Chat ID whitelist authentication, polling mode, formatted message output
- **Typer CLI** -- Secondary command-line interface for debugging and local operations:
  - Lifecycle commands: `start`, `stop`, `status`
  - Position management: `positions`, `position <id>`
  - Order management: `orders`, `cancel <id>`
  - Strategy management: `strategies`, `strategy set`
  - Configuration: `config`, `config set`, `config reload`
  - Backtesting: `backtest` with date range, symbol, expiry filtering
  - Risk monitoring: `risk`
  - History: `trades`, `decisions`
  - Health and diagnostics: `health`, `logs`
- **SQLite persistence (migrated to PostgreSQL in v0.2.0)** -- 12-table schema with aiosqlite, WAL mode, repository pattern
- **6-gate pre-trade risk checks** -- Margin sufficiency, max position size, max delta exposure, max theta decay, volatility check, concentration limit
- **4 circuit breaker types** -- P&L drawdown (4 tiers), Greek exposure (delta/gamma/vega thresholds), volatility spike, connection loss
- **Option Greeks monitoring** -- Delta, gamma, theta, vega per position and portfolio-level aggregation
- **Fractional Kelly position sizing** -- Adapted for options with IV Rank, DTE, liquidity, and streak adjustments
- **TWAP splitting** -- Large orders split over time to reduce market impact
- **Backtesting engine** -- Tick/bar replay with historical option price data
- **Health check HTTP server** -- Port 9090 with `/health`, `/ready`, `/live`, `/metrics` endpoints
- **Prometheus metrics** -- Uptime, positions, portfolio value, drawdown, trades, errors, cycle time
- **YAML configuration system** -- 4-layer hierarchy (default.yaml, local.yaml, .env, CLI flags) with hot-reload
- **Structured JSON logging** -- structlog with machine-parseable JSON output
- **Docker deployment** -- Single-container Dockerfile with docker-compose.yml
- **Comprehensive documentation**:
  - `docs/architecture.md` -- System architecture and design decisions (12 ADs)
  - `docs/api.md` -- Plugin interfaces, repository pattern, health server reference
  - `docs/interface-commands.md` -- Telegram + CLI command reference
  - `docs/configuration.md` -- Config files, env vars, hierarchy, hot-reload
  - `docs/deployment.md` -- Docker and direct deployment guide
  - `docs/risk-management.md` -- Risk system deep dive
  - `docs/strategy-development.md` -- Custom strategy plugin guide
  - `docs/troubleshooting.md` -- Common issues and solutions

### Changed

- Complete language transition: TypeScript/Node.js -> Python 3.12+
- Exchange transition: Binance Futures -> Binance Options
- Database transition: PostgreSQL -> SQLite (v0.1.0), then back to PostgreSQL (v0.2.0)
- UI transition: Telegram bot retained as primary interface, Typer CLI added as secondary debugging interface
- Architecture: Dual-runtime (Node.js + Python) -> Single-process pluggable Python
- Strategy approach: ML/AI model-driven -> Deterministic plugin-based strategies
- All ML/XGBoost/scikit-learn content removed in favor of rule-based option strategies

### Removed

- Node.js/TypeScript codebase and all npm dependencies
- Python ML microservice (FastAPI, XGBoost model serving)
- PostgreSQL database layer and migrations
- ML training pipeline (XGBoost, Optuna, feature engineering)
- Market regime detection classifier
- Feedback loop engine and retraining triggers
- Feature drift / concept drift monitoring
- TA-Lib technical indicator suite
- Nginx reverse proxy configuration
- Systemd service files
- Dual-container Docker setup
