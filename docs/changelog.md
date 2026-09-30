# Changelog

All notable changes to the Quad project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased] -- type-safety and duplication cleanup

Follow-up to the full-codebase audit below. Drives `mypy src` to **zero
errors** (from 69) and removes the duplicated retry / cache logic. Adds
`src/quad/common/retry.py` and `tests/test_retry.py` (441 tests total).

### Fixed
- **`quad-api`'s `GET /health` never failed.** It returned an unconditional
  `{"status": "ok"}` and contacted no dependency, so a load balancer or
  orchestrator kept routing traffic to an API that could not reach Postgres --
  every real endpoint would fail while the probe said "ok". It now runs
  `DatabaseManager.is_healthy()` (`SELECT 1`), reports a per-component map
  (`components` / `degraded`, matching the bot's `HealthServer` payload) and
  returns **503** with `error.code = "unhealthy"` when a component is down.
  The check is fail-closed: an exception inside the check counts as unhealthy.
- **Added `GET /live` to `quad-api`** -- a liveness probe that checks *no*
  dependency, so a transient database blip does not cause a restart loop.
  Point `readinessProbe` at `/health` and `livenessProbe` at `/live`. Both are
  unauthenticated. Previously the API had no liveness endpoint that could
  safely be separated from readiness.
- **`trades.order_id` could never join back to its order.** The column is an
  `INTEGER` foreign key to `orders.id`, but the engine passed
  `OrderResult.order_id` -- the *exchange* id, which Bybit returns as a UUID
  string. SQLite stored the UUID as text in an INTEGER-affinity column, so the
  fill was permanently unlinkable. Non-integer ids now record `0`, the same
  "unknown" sentinel already used for `id` / `position_id`.
- **`_last_trend_roll_ts` was annotated `dict[str, int]`** but stores
  `time.monotonic()`, i.e. floats. Corrected to `dict[str, float]`.
- **`/start <pairing-code>` could raise `AttributeError`.** The guard checked
  `self._pairing is not None` and then dereferenced `self._bindings`, which is
  separately optional. Both are now checked.
- **`close_tasks` was annotated `list[asyncio.Task]`** while every element is a
  `(position, action, task)` tuple, so the annotation described nothing real.
- **`structlog_context_processor` did not satisfy structlog's `Processor`
  protocol** (it took a `dict`, which is contravariantly too narrow), and would
  have been rejected at the point the processor chain is assembled. Its
  signature now uses `MutableMapping`.
- **`asyncio.gather(..., return_exceptions=True)` results were filtered with
  `isinstance(result, Exception)`**, which does not exclude `BaseException`
  (`CancelledError`, `KeyboardInterrupt`) -- those would have fallen through
  and been unpacked as a `(key, klines)` tuple.
- **`aiosqlite` import guard carried a `# type: ignore[no-redef]`** for an
  error that is actually reported as `assignment`, so the ignore was both
  wrong and unused.

### Changed
- **God modules split by concern, behaviour preserved.** `orchestrator.py`
  (4251 lines / 54 methods) and `commands.py` (2602 / 37) were split into
  mixins. Every method was moved as *text at its original indentation* and then
  proven unchanged by comparing its `ast.dump` before and after, so the
  refactor is provably code movement with no logic edits. All method names and
  signatures are unchanged, and `quad.orchestrator.orchestrator.QuadOrchestrator`
  / `quad.bot.commands.QuadBotCommands` remain the public entry points.
  - `orchestrator/`: `orchestrator.py` keeps lifecycle (`__init__`, `start`,
    `stop`, `run_forever`, `_setup_signal_handlers`, `_shutdown_all`) and the
    public API (`_build_strategy_context`, `execute_strategy`, `status`) --
    706 lines / 9 methods. The rest moved to `_bootstrap_mixin` (subsystem
    wiring, 18), `_rotation_mixin` (the cycle and the scalp/trend rotations,
    11), `_positions_mixin` (close / bracket / AI actions under `_trade_lock`,
    7), `_tv_mixin` (webhook receiver, 1), `_decisions_mixin` (decision
    journal, 3) and `_notify_mixin` (Telegram sends, 5).
  - `bot/`: `commands.py` keeps `__init__`, the reply/narrow helpers, the
    operator gate, `/start`, `/help`, `/status`, `/strategies`, `/risk`,
    `/cancel`, `/exchange`, the `/execute` conversation handler and
    `error_handler` -- 1058 lines / 18 methods. The rest moved to
    `_config_cmds_mixin` (`/leverage`, `/position_mode`, `/settings`, `/set`),
    `_market_cmds_mixin` (the seven read-only views), `_ai_cmds_mixin` (the AI
    commands) and `_safety_mixin` (`/kill` and its confirm callback).
  - Each mixin declares the state it consumes as class-level annotations, so
    the contract with its host class is explicit without the mixin owning (or
    being able to clobber) the state.
  - The `/set` allowlist constants and `_escape_md` moved to the modules that
    are now their only callers, and are re-exported from `quad.bot.commands`
    (including `__all__`) so the existing import path keeps working -- a test
    imports `BLOCKED_SET_KEYS` from there.
- `ALL_MODELS` is typed `list[type[ModelSurface]]` via a new structural
  `ModelSurface` `Protocol`, so schema bootstrap and `BaseRepository` no
  longer need `Any` / `getattr` escape hatches to reach `create_table_ddl()`.
- `aiohttp.ClientWebSocketResponse` is now parameterised `[bool]` on the
  WebSocket manager's helpers, matching what `ws_connect()` actually yields.
- `ExchangeStatusResponse.worker_pid` is coerced to `int`; the supervisor can
  report a pid as a string (it is read back from a file written by another
  process) and a malformed value now degrades to "unknown" instead of failing
  the whole status response.
- The Bybit adapter annotates its client with the real `pybit` types via a
  `TYPE_CHECKING` import, so `self._client` / `self._ws` keep full attribute
  checking while the SDK stays an optional runtime dependency.
- `cmd_start`'s pairing guard and the bybit `repositories.py` tenant stamping
  now narrow `Optional` explicitly instead of relying on a correlation between
  two attributes that a type checker cannot see.

### Added
- `quad.common.retry` -- `exponential_backoff()` (geometric growth with an
  optional cap and jitter) and `retry_async()` (bounded retry loop with
  injected retryable-predicate, delay policy, per-attempt hook and retry
  callback). 16 tests pin the schedule, the cap, the jitter bound and every
  exit path, including "re-raise immediately when not retryable" and "re-raise
  the last error on exhaustion".
- `quad.exchange.base._ttl_fresh` / `_ttl_store` -- the TTL freshness rule
  (monotonic clock, half-open `[0, ttl)` window) that three filter caches had
  each re-implemented.

### Refactored (duplication removed)
- **TTL caches: 3 copies -> 1.** `ExchangeAdapter._get_lot_filters`,
  `ExchangeAdapter.get_tick_size` and the Bybit override each re-implemented
  the same `(monotonic_ts, value)` lookup. They now share `_ttl_fresh` /
  `_ttl_store`. These still operate on a plain `dict` on purpose: adapters and
  tests seed `_exchange_info_cache` directly, so the mapping is the contract.
- **Retry backoff: 4 copies -> 1.** `groq.py` (twice), `bybit._retry_delay` and
  the order-submission gateway each hand-rolled
  `base * 2 ** (attempt - 1)`; they now call `exponential_backoff()`.
- **Bounded retry loop: 2 copies -> 1.** `bybit._request` and the gateway's
  `submit()` shared the same attempt/sleep/log scaffolding and now use
  `retry_async()`. Each keeps its own policy: Bybit normalises the error and
  honours `Retry-After`; the gateway treats only `TimeoutError` /
  `ConnectionError` as transient and wraps everything else in
  `OrderRejectedError`.
- `groq._chat`'s loop and the market-data WebSocket reconnect supervisor were
  deliberately **not** folded in: the first is a three-exception-type state
  machine with recursive model fallback and key rotation, the second an
  unbounded reconnect loop. Neither is a duplicate of the bounded retry.

### Known issues (reported, not changed)
- **`gateway.backoff_base_seconds` is dead config.** The submit retry schedule
  is hardcoded to 1s/2s/4s (capped at 30s) and never consults the configured
  base (default 2.0). Behaviour is preserved deliberately -- changing live
  order-retry timing is a decision, not a refactor -- and the property now
  documents this. Wiring it through needs an explicit sign-off.
- **The two HTTP servers are not duplication.** `quad.api.app` (FastAPI /
  uvicorn) serves the multi-tenant API process; `quad.monitoring.health.HealthServer`
  (aiohttp) runs inside the trading-bot process and additionally hosts the
  TradingView webhook. They are different processes with different contracts
  and auth, and must not be merged.
- **The three "rate limiters" are three different algorithms,** not copies: a
  sliding-window counter that raises HTTP 429 (`api.deps.RateLimiter`), a
  minimum-interval async pacer (`BybitFuturesAdapter._throttle`), and a
  per-user/per-command cooldown (`QuadBotCommands._check_rate_limit`). No
  shared abstraction would fit all three.

---

## [Unreleased] -- full-codebase audit fixes

Result of an end-to-end review of every module in `src/quad/`, the test
suite, the configuration layer and the documentation. Adds
`tests/test_scan_fixes.py`, `tests/test_cli.py` and
`tests/test_docs_consistency.py` (419 tests total).

### Fixed (safety-critical)
- **TradingView webhook route was never mounted.** The orchestrator starts
  the health server *before* initialising the webhook, and `add_route()` only
  queued the route, so every alert returned 404 while the log reported
  `tradingview_webhook_initialized`. aiohttp freezes its router at
  application startup, so `HealthServer` now pre-registers a catch-all
  dispatcher and `add_route()` mutates the backing dict. The orchestrator
  additionally verifies the route is live and disables the webhook (with a
  `critical` log) if it is not.
- **`/kill` cancelled nothing.** It set the kill-switch flag and replied
  "Open orders have been cancelled". It now cancels open orders on the
  exchange (union of the exchange view and the gateway's in-memory view) and
  reports the real `cancelled` / `failed` counts with per-order reasons.
  Positions still remain open, and the message now says so accurately.
- **`/execute` hardcoded `dry_run=False`,** so a dry-run bot still attempted
  real submissions. It now forwards the bot's actual dry-run state, states
  the execution environment (testnet / live / dry-run) in the confirmation
  card, has a 60s cooldown, and is **operator-only** — as is `/kill`.
- **Webhook secret env-var mismatch.** `tradingview/signals.py` read
  `QUAD_TV_WEBHOOK_SECRET` while the config mapped
  `QUAD_TRADINGVIEW_WEBHOOK_SECRET`, so the in-alert credential check never
  ran. One env var now, passed explicitly by the orchestrator. The
  `allow_without_secret` escape hatch is removed entirely — the schema
  already required a >=16-char secret when the webhook is enabled.
- **Protective brackets bypassed the risk pipeline.** Brackets are
  submitted with `risk_checked=True` and used the exchange-reported
  `filled_qty` unchecked. They are now bounded by the pre-sizing quantity
  and validated against `risk.max_position_size_usd`; an over-cap bracket
  leaves the position unprotected and says so loudly rather than silently
  over-ordering.
- **Min-quantity floor-up could exceed an approved notional cap.**
  `_prepare_quantity` floors a sized-but-sub-minimum order up to the
  exchange minimum, potentially past what risk had approved. The floor-up
  is now re-validated against `risk.max_position_size_usd` and rejected
  with a clear reason if it would breach the cap.
- **`/leverage SYMBOL VALUE` and `/position_mode MODE` silently did
  nothing** — they echoed the requested value back. Both now really call the
  adapter, are operator-only, clamp to `risk.max_leverage`, and report
  exchange rejections (e.g. an open position blocking a leverage change).
- **Futures account setup swallowed per-symbol failures,** so the bot could
  size brackets and liquidation distances from a leverage the exchange
  never accepted. Leverage is now read back and verified, clamped to
  `risk.max_leverage`, and a mismatch **aborts startup in live mode**.
- **Unknown `QUAD_MODE` values fell through silently** (a leftover
  `QUAD_MODE=okx` traded Bybit while `quad status` advertised otherwise).
  Only `bybit` and `dry_run` are accepted; anything else is a hard error.
- **`_mode` / `_dry_run` were not schema fields,** so the two safety
  switches emitted `config_unknown_key_ignored` on every validation and
  survived only via an ad-hoc copy loop. They are now declared,
  validated `QuadConfig` fields.
- **Health-server auth bypass behind a reverse proxy.** The no-key
  loopback bypass trusted `request.remote`, which is the *proxy's* address
  when proxied, so any forwarded internet request was accepted. The bypass
  is now refused when forwarding headers are present.
- **Windows console encoding.** Printing any status glyph raised
  `UnicodeEncodeError` and aborted the CLI mid-command. The console is now
  reconfigured to UTF-8 (shared helper used by both the entry point and the
  CLI). `quad execute --no-dry-run` was also advertised but never generated
  by Typer (exit code 2); the real flag is `--live`.
- **Latent `NameError` in the trading cycle.** `_reconcile_decision_outcomes`
  imported `DecisionRepository` but not `make_repo`, so every cycle raised
  `NameError` on that line. Also fixed an undefined `logger` in
  `api/deps.py` and an undefined `OrderResult` annotation in the
  orchestrator.
- **`error_logs` had a schema and readers but no writer,** so every recorded
  error was lost when stdout rotated. `monitoring/error_sink.py` is a
  structlog processor that persists `ERROR`+ events through a bounded queue
  and a background flusher, flushed before the database disconnects.
- Brittle `[]` config indexing that raised `KeyError` on partial configs
  (`WebSocketManager.__init__` required `market_data` and
  `market_data.websocket`; `GroqClient.__init__` required
  `ai.groq.rate_limiter`). All fail soft with working defaults.
- Unawaited WebSocket task cancellation: `stop()` and `resubscribe_all()`
  left the connection task pending, logging "Task was destroyed but it is
  pending" and allowing a second `_run_connection` to overlap the first.
  Both now cancel *and await* via a shared `_cancel_connection_task()`.
- `asyncio.get_event_loop()` (deprecated outside a coroutine) in Groq key
  rotation, which also leaked the old client's HTTP pool via a
  garbage-collectable fire-and-forget task. Now uses
  `get_running_loop()` and a retained task set. The rate-limit stamp mixed
  loop time with wall time; both are now `time.time()`.
- `PriceBuffer._buffers` was read directly (bypassing the class's own
  accessors) from `MarketDataEngine.status()`; a public `snapshot_counts()`
  replaces it.
- **API-key material in `Account.id`.** `bybit-{api_key[:8]}` embedded 8
  characters of the live secret into every log line, status message and
  persisted row. Replaced with a salted SHA-256 fingerprint.

### Added
- **Correlation IDs** (`monitoring/correlation.py`): each trading cycle binds
  `cycle-<hex>` and each TradingView alert `tv-<hex>`, attached to every log
  event, so concurrent pair scans, webhooks and Telegram jobs are separable.
  The scan found zero correlation-id usage before this.
- **Persistent error sink** (`monitoring/error_sink.py`), configurable via
  the new `error_sink` config section.
- **CLI commands that were documented but missing:** `start`, `stop`,
  `strategies`, `health`, `trades`, `decisions`, `logs`. `start` is a real
  foreground launcher (README's quickstart `quad start --dry-run` previously
  did not exist). `balance`/`positions`/`orders` now read the local database
  and state plainly that they are not live exchange queries. `db-info` shows
  per-table row counts.
- `[tool.pytest.ini_options]` in `pyproject.toml`. `pytest-asyncio` was only
  installed in CI, so a local `pytest` run failed or skipped every async
  test.
- Health-server aliases `GET /ready` and `GET /live` (the documented probe
  paths, which did not exist), plus `set_metrics_collector()` so `/metrics`
  serves real gauges instead of an uptime-only fallback.
- `quad-run` console script and a synchronous `run()` entry point. The old
  `quad-bot` gui-script pointed at an `async def`, so the generated wrapper
  returned an un-awaited coroutine and the bot never started.
- `pyproject.toml`: `dev` / `api` optional-dependency extras, and explicit
  `testpaths` / asyncio mode.
- Tests: `tests/test_scan_fixes.py`, `tests/test_cli.py`,
  `tests/test_docs_consistency.py` (docs-vs-code drift guards), plus new
  coverage in `tests/test_authz_ops_fixes.py`.

### Changed
- `pyproject.toml` metadata no longer describes the project as an
  "USD-M futures trading bot for OKX"; the project is Bybit-only.
- Package dependencies raise floors to the tested versions
  (`pybit>=5.17`, `groq>=1.5`) — the old floors (`pybit>=5.7`,
  `groq>=0.4.0`) were unsatisfiable alongside the pinned httpx: a local
  install resolved `groq 0.4.1`, which calls the removed
  `AsyncClient(proxies=...)` argument and fails at import.
- **The ruff rule set is now pinned** (`select = ["E4","E7","E9","F"]`).
  It was previously implicit, so `ruff check` results depended on the
  installed ruff version — ruff 0.16 enabled ~800 rules and reported 316
  findings; the intended set reports none. `ruff check src tests` is clean.
- `DATABASE_URL` is now mapped to `persistence.dsn` in
  `ConfigManager.ENV_VAR_MAP`, so the documented override actually works
  and there is one source of truth (the orchestrator no longer reads the
  env var directly).
- `tradingview_webhook.port` is documented as informational only — the
  webhook is a route on the health server, so the port it binds is
  `monitoring.health_server.port`.
- Binance-specific error codes (`-1113` / `-1111` / `-4164`) removed from
  comments in the ABC and execution engine; the bot is Bybit-only.

### Documentation
Corrected drift between the docs and the code, with
`tests/test_docs_consistency.py` added to prevent it returning:
- Schema count: "16 tables" -> **20 tables**, `SCHEMA_VERSION = 10`; the
  documented `contracts` and `stats` tables never existed.
- Telegram: removed the never-implemented `/pnl`, documented `/exchange`,
  corrected the handler count, and documented the public / bound /
  operator-only access tiers and precisely what `/kill` does.
- CLI: removed every fabricated command and flag (`quad stop --emergency`,
  `quad position <id>`, `quad cancel`, `quad strategy set`, `quad
  config set/reload`, `quad backtest --report`, `quad logs --level`, ...)
  and replaced invented sample output with real behaviour.
- `docs/strategy-development.md` taught a nonexistent `Strategy` class and
  `analyze()` method; rewritten against the real `StrategyBase.evaluate()`
  API (the example is verified executable), and it now states honestly that
  the `quad.strategies` entry-point group is declared but never read —
  strategies register by subclassing.
- `docs/configuration.md` described a 4-layer config with split
  `risk.yaml`/`strategy.yaml` files and a `config.local.yaml` overlay;
  reality is one `config.yaml` plus 3 layers. Its YAML sample was
  regenerated from the real schema (it previously set keys silently dropped
  by `extra="ignore"`, including a nonexistent `logging:` section).
- `docs/risk-management.md` used config keys that do not exist
  (`max_portfolio_risk`, `max_daily_loss`, `max_drawdown`,
  `max_correlation`, `risk.stop_loss.fixed_loss_per_contract`); corrected to
  the real names and validated against `RiskConfig`.
- `docs/troubleshooting.md` listed options-era gates (delta, theta, IV,
  expiry) that were removed in v2.0.0; replaced with the 9 real futures
  gates. Also corrected the Python floor (3.10+, not 3.12+) and the
  backoff cap.
- `docs/api.md`: replaced a fabricated `/health` payload and a fabricated
  metrics list (invented `quad_`-prefixed names) with the real response
  shape and the 9 metrics actually emitted; documented the full probe path
  set and the auth model.
- `docs/deployment.md`: rewrote the Docker/compose sections against the real
  `Dockerfile` and `docker-compose.yml`, documented foreground operation and
  graceful shutdown, and documented the reverse-proxy auth requirement.
- `README.md`: corrected the startup ordering, the TradingView flow
  (auth is now mandatory, port clarified), the AI subsystem (added the
  `ai/validator.py` direction/side inversion guard and its single point of
  control caveat), and the project structure.

### Fixed (dependencies)
- Local development environment was out of sync with `requirements.txt`
  (`fastapi 0.109.2` against a `starlette 1.6.0` that removed the
  `on_startup` kwarg, breaking every `quad-api` import and failing 4 test
  modules at collection). Aligned to the pinned set.

---

## [Unreleased] -- v8: server-owned symbols

### Changed
- Trade universe is server-owned: `symbols` removed from `PUT /v1/config`
  (sent lists ignored; GET returns the bot universe for display).
- `tenant_config.symbols_json` dropped (v8 migration); new tenants default
  to Balanced (10x, 2%/trade, TP 50/SL 30, trend).
- v8 backfills pre-canonical 5x rows to 10x. Tenants who deliberately chose
  5x move too — re-tune with `PUT /v1/config`.

## [Unreleased] -- AI-judged scalping + strategy modes

### Added
- `strategy_mode` per tenant (`trend`/`scalp`/`both`, `PUT /v1/config`):
  users pick trend, scalp, or both; leverage is theirs (capped at 10x when
  scalp is active — spread-noise liquidates higher).
- AI-judged scalp loop: 5m candles -> deterministic reversion signals ->
  gate -> ONE batch judge call for all symbols (`decide_batch`, strict
  per-symbol schema, HOLD-filled omissions) on qwen/qwen3-32b.
- Separate scalp budget (`scalp_max_calls_per_day`, default 120), tight
  scalp brackets (TP 15%/SL 8% defaults), 15-min time-stop, shared
  position slots with trend.
- Trend roll-gate: hourly rolls survive 5-minute loops (`roll_min_seconds`).
- Schema v7: `strategy_mode`, `scalp_tp_pct/sl_pct/max_calls_per_day`.
- Groq free-tier capacity model documented in code (per-org RPD is the
  binding constraint: 250/day compound-mini, 1,000/day qwen/gpt-oss).

## [Unreleased] -- AI cost controls + trade policy

### Added
- Judge gate (`ai.judge_gate`): the Groq final-judgement call fires only on a
  fresh closed candle AND (a local setup at `min_strength`, default 0.5, OR an
  open position needing a decision). Dead markets return a local HOLD free.
- Per-tenant daily judge budget (`ai.judge_max_calls_per_day`, default 24;
  tenant field `ai_max_calls_per_day`, 1-500 via `PUT /v1/config`). Exhausted
  budgets run local-only; exchange-native TP/SL brackets still protect.
- Model tiers (`ai.tier`, tenant field `ai_tier`: `cheap`/`smart` via
  `PUT /v1/config`). Cheap judges on the primary model; disagreement between
  a strong local (default 0.7) and the cheap verdict triggers one smart-model
  re-judge, higher confidence wins.
- Groq key pool (`GROQ_API_KEYS`, comma-separated): rotates past daily-quota
  429 walls before falling back to the fallback model.
- Rotation hourly roll made explicit: workers default
  `close_open_position_each_cycle=true` + `max_hold_seconds=3600` (one trade
  per cycle; stale positions force-closed; TP/SL are real exchange brackets,
  PnL read from `unrealisedPnl`/`realisedPnl`, never mocked).
- Schema v6: `tenant_config.ai_tier`, `tenant_config.ai_max_calls_per_day`.

## [Unreleased] -- Bybit-Only User Docs

### Changed
- All user-facing docs are now Bybit-only: `configuration.md`,
  `deployment.md`, `architecture.md`, `interface-commands.md`,
  `troubleshooting.md`, `go-live-plan.md`, `risk-management.md`,
  `strategy-development.md`, and `README.md` describe the Bybit V5 USDT
  perpetual backend (`pybit` SDK, `category="linear"`, symbols like
  `BTCUSDT`, Bybit V5 kline intervals like `"60"`).
- Env vars documented as `BYBIT_API_KEY` / `BYBIT_API_SECRET` /
  `BYBIT_TESTNET` with `QUAD_MODE=bybit`; testnet
  (`https://api-testnet.bybit.com`) documented as the default.
- Docker notes: Python-only image (no Node.js), compose snippet carries the
  `BYBIT_*` vars, Postgres-on-Hetzner vs SQLite-dev topology noted.

### Removed
- `OKX_*` env vars, passphrase, `instType=SWAP` / `BTC-USDT-SWAP` symbol
  format, `python-okx` SDK, and the OKX MCP server (`mcp/` was deleted from
  source) from all user docs. History below is preserved as-is.
- `MockAdapter` / mock-mode references from user docs (only testnet and live
  environments remain).

---

## [2.2.0] - 2025-08-29 -- OKX MCP Server Integration

### Added
- **OKX MCP server integration** (`src/quad/mcp/`) — async `OkxMcpClient` class
  communicating with the `okx-trade-mcp` binary over stdio (JSON-RPC 2.0).
  Provides 228 built-in TA indicators, smart money signals, news sentiment,
  and advanced order types (TWAP, iceberg, chase).
- **MCP exchange adapter** (`src/quad/exchange/mcp_adapter.py`) — drop-in
  `ExchangeAdapter` implementation that routes all OKX API calls through the
  MCP server subprocess. Maps market data, account, swap orders, algo orders,
  and configuration endpoints.
- **MCP config schema** (`src/quad/config/schema.py`) — `McpConfig` model with
  `enabled`, `command`, `modules`, `profile`, `request_timeout`, `startup_timeout`.
- **MCP-powered context collection** (`src/quad/ai/context.py`) — `collect_market_context()`
  now accepts `mcp_client` parameter for direct MCP data fetching, bypassing
  WebSocket entirely.
- **MCP-powered indicator computation** (`src/quad/ai/ta.py`) — `compute_indicators_via_mcp()`
  function using MCP server's 228 built-in indicators.
- **Historical data fix** (`src/quad/market_data/historical.py`) — `get_candles()` stub
  replaced with MCP-powered implementation + exchange adapter fallback.
- **Backtesting engine MCP support** (`src/quad/backtesting/engine.py`) — `_build_context()`
  now fetches real candle and indicator data via MCP.
- **Optimizer MCP enrichment** (`src/quad/ai/optimizer.py`) — accepts `mcp_client`
  for market context during retraining cycles.
- **Telegram `/mcp_status` command** — shows MCP server uptime, tool count,
  call statistics, and error rate.
- **Dual-mode factory** (`src/quad/exchange/factory.py`) — `create_exchange()`
  accepts `mcp_client` parameter; returns `McpExchangeAdapter` when MCP is enabled.
- **Orchestrator MCP lifecycle** (`src/quad/orchestrator/orchestrator.py`) —
  `_init_mcp_client()` method, MCP client passed to exchange adapter, cleanup
  in `_shutdown_all()`.

### Changed
- `/status` command now shows MCP server state (Active/Disabled).
- `/start` command lists `/mcp_status` in available commands.
- Exchange adapter factory accepts optional `mcp_client` parameter (backward compatible).

### Architecture Decision
- AD-2b: OKX MCP Server Integration (optional) — feature-flagged behind
  `config.mcp.enabled` with instant rollback to python-okx SDK.

---

## [2.1.0] - 2025-08-25 -- Bybit USDT-Perpetual Switch

### Added
- **Bybit exchange adapter** (`src/quad/exchange/bybit.py`) — full implementation
  of the `ExchangeAdapter` ABC over Bybit's V5 API using the official `pybit` SDK.
  Targets **USDT perpetual** exclusively via `category="linear"` (hard-coded as a
  class constant, eliminating any futures-vs-perpetual misconfiguration).
- `pybit>=1.4.0` dependency in `pyproject.toml` / `requirements.txt`.
- `BybitConfig` schema (`src/quad/config/schema.py`) with Bybit REST/WS URLs.
- `is_margin_mode_already_set()` / `is_order_not_found()` methods on the
  `ExchangeAdapter` ABC, so per-exchange error semantics live in adapters
  instead of orchestration code.

### Changed
- **Exchange target** switched from Binance USD-M Futures to Bybit USDT perpetual.
  `QUAD_MODE`, `exchange.name`, `config.yaml`, `.env.example`,
  `docker-compose.yml` all now default to `bybit` with `testnet: true`
  (testnet is the default safety environment; live is opt-in).
- **Shared error hierarchy** (`ExchangeError` and subclasses) moved from
  `binance.py` to `base.py` so all adapters use one definition.
- **Ghost-order / margin-mode detection** generalized: the orchestrator and
  order gateway now call `adapter.is_order_not_found()` / `adapter.is_margin_mode_already_set()`
  instead of string-matching Binance error codes (`-2013`, `-4046`).

### Removed
- **Binance adapter** (`src/quad/exchange/binance.py`) — deleted; no longer wired
  into the factory.
- **Mock mode** (`src/quad/exchange/mock.py`) — removed entirely. Only testnet and
  live environments remain.
- `BINANCE_*` environment variables; replaced by `BYBIT_API_KEY` /
  `BYBIT_API_SECRET` / `BYBIT_TESTNET`.
- `tests/test_price_tick_normalization.py` — Binance-mock-specific regression;
`tests/test_one_trade_per_cycle.py` — rewritten to use `BybitFuturesAdapter`.
- `FIX_PLAN.md` and `orchestrator_dump.txt` — stale scratch artifacts removed.

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
