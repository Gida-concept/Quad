# Interface Commands Reference (Telegram + CLI)

---

## Overview

Quad provides **two interfaces**:

1. **Telegram Bot (primary)** -- live trading control from any device. Commands are answered by a `python-telegram-bot` poller running inside the trading process.
2. **Typer CLI (secondary)** -- local inspection, maintenance, and one foreground launcher. It does **not** open an exchange session for most read commands.

Both halves of this document are verified against the code:

| Surface | Source of truth |
|---|---|
| CLI commands / flags | `src/quad/cli/app.py` |
| CLI entry points | `[project.scripts]` in `pyproject.toml` |
| Telegram command list | `QuadBot._setup_handlers` (`src/quad/bot/bot.py`) |
| Telegram behaviour | `src/quad/bot/commands.py` |

---

## Telegram Commands

### Setup

1. Create a bot via [@BotFather](https://t.me/botfather).
2. Set `TELEGRAM_BOT_TOKEN` in `.env`.
3. Set `TELEGRAM_NOTIFICATION_CHAT_ID` to the chat that owns the deployment. This chat is the **operator** chat.
4. Start the bot (`quad start`). The Telegram subsystem initialises automatically when `telegram.enabled: true` **and** a bot token is present; it is skipped otherwise. It also registers a `telegram_bot` component in the health check, so `quad health` reports it.
5. If a database is configured, other chats must link themselves with `/start YOURCODE` using a pairing code from the control plane (`POST /v1/telegram/pairing-code`).

### Access tiers

The wrapper installed on every `CommandHandler` enforces tiers before the handler runs. Callback queries bypass that wrapper, so the callback handlers re-check it themselves.

| Tier | Commands | Rule |
|---|---|---|
| Public | `/start`, `/help` | No binding required |
| Bound chat | everything else | `get_bound_tenant(chat_id)` must return a tenant; an unbound chat gets 🔒 and the `/start YOURCODE` hint |
| Operator-only | `/kill`, `/execute`, `/leverage SYMBOL VALUE`, `/position_mode MODE`, and part of `/set` | Requires the notification chat (or a no-DB single-tenant run, which resolves to operator) |

Notes:

- With no `db_manager` configured, `get_bound_tenant()` returns `__operator__` for every chat, so a personal single-tenant run behaves as operator-only everywhere and no pairing is needed.
- `/kill` is enforced in `cmd_kill_callback` (the button press), not on `/kill` itself — the prompt is still shown.
- `/execute` re-checks the operator tier twice: when the flow starts, and again when **Confirm** is pressed, because the chat clicking confirm can differ from the chat that started it.
- `/leverage SYMBOL` (one argument, read) and `/position_mode` (no argument, read) are *not* operator-only. Only the write paths are.

### Commands

Registered as simple `CommandHandler`s, plus the `/execute` conversation handler.

| Command | Description | Tier |
|---|---|---|
| `/start` | Welcome screen, or `/start YOURCODE` to bind a chat | Public |
| `/help` | Full command reference | Public |
| `/status` | Position count, daily PnL, circuit-breaker count, active strategies, order stats | Bound |
| `/balance` | Live account balances per asset plus total USDT value | Bound |
| `/positions` | Table of open positions (up to 15) with mark, liquidation price, PnL, leverage | Bound |
| `/orders` | Table of active / pending orders from the execution engine (up to 20) | Bound |
| `/funding_rate [symbol]` | Funding rate for one symbol, or for every tracked underlying | Bound |
| `/book <symbol>` | Top-of-book bids/asks with spread | Bound |
| `/leverage [symbol] [value]` | Show leverage config/max; `SYMBOL VALUE` sets it on the exchange | Read: Bound · Write: **Operator** |
| `/position_mode [mode]` | Show config vs exchange mode; `one_way`/`hedge` switches it | Read: Bound · Write: **Operator** |
| `/liquidation_warnings` | Distance-to-liquidation check for every open position | Bound |
| `/market_regime` | Funding-rate landscape and volatility assessment | Bound |
| `/strategies` | Every registered strategy with its description and parameter spec (no last-signal data) | Bound |
| `/execute` | Interactive flow: pick strategy → confirm → run | **Operator** |
| `/risk` | Risk gates, circuit breakers, exposure report | Bound |
| `/kill` | Kill switch prompt with inline confirm button | Confirm: **Operator** |
| `/cancel <order_id>` | Cancel an order by exchange or client order ID | Bound |
| `/settings` | Full resolved configuration (JSON, truncated at 4000 chars) | Bound |
| `/set <key> <value>` | Runtime config change, allowlist-gated | Bound / **Operator** (see below) |
| `/analyze` | AI analysis of current market conditions | Bound |
| `/ai_strategy` | AI strategy recommendation from the current regime | Bound |
| `/ai_status` | AI system status, rate limiter, recent decisions | Bound |
| `/ai_decision` | Trigger a full AI decision cycle (ENTER/EXIT/HOLD/...) | Bound |
| `/exchange` | Bybit adapter state, testnet vs LIVE, connected or not | Bound |

There is no PnL-only command; PnL is part of `/status`, `/positions`, and `/balance`.

### `/kill` — what it actually does

`/kill` posts a confirmation prompt with two buttons (`kill_confirm` / `kill_cancel`). On confirm, in this order:

1. **Halts new entries** via the kill-switch breaker (`risk_manager.trigger_kill_switch`, falling back to the orchestrator's).
2. **Cancels open orders on the exchange.** It unions `adapter.get_open_orders()` with the execution gateway's in-memory tracked orders, then cancels each one.

The reply reports the truth: `Open orders cancelled: *N*.`, or lists up to 8 reasons when some orders failed.

**It does not close positions.** The confirmation card states `Existing positions: *remain open* — manage them manually.`, and cancelling the button leaves the bot untouched. It also cannot be undone from Telegram.

### `/execute` — the conversation flow

`/execute` is a `ConversationHandler` (not rate-limit-wrapped; the 60 s cooldown is enforced inside the flow).

| Step | Trigger | Behaviour |
|---|---|---|
| 1 | `/execute` | Operator check, 60 s cooldown, then an inline keyboard listing registered strategies with the execution environment label (`🧪 TESTNET`, `🔒 DRY-RUN`, `🚨 **LIVE — REAL MONEY**`, ...) |
| 2 | Tap a strategy | Confirmation card listing the strategy params, environment, and Confirm/Cancel |
| 3 | Tap **Confirm** | Operator re-check, then `orchestrator.execute_strategy(name, dry_run=<bot's real dry-run state>)`; reports actions and execution results |

Typing `/cancel` mid-flow, or pressing `exec_cancel`, ends the conversation without running anything.

### `/set` — allowlist

`/set` accepts only these keys from **any bound chat**:

```
trading.leverage        trading.margin_mode      trading.position_mode
trading.serial_trade_mode
ai.enabled
risk.max_drawdown_pct   risk.max_funding_rate_cost
risk.min_distance_to_liquidation_pct
```

**Operator-only** additions: `trading.trade_capital_usd`, `ai.system_prompt_override`.

**Always refused**, even for the operator: anything under `exchange.` or `telegram.`, and `ai.system_prompt_override` for non-operators. `exchange.testnet` can therefore never be flipped from Telegram.

Guardrails applied after authorisation: `trading.leverage` must be 1–10, `trading.trade_capital_usd` must be > 0 and ≤ 10000. The reply notes the matching env var, if any, and warns that some changes need a restart.

### Rate limits

Per-user, per-command cooldowns from `QuadBotCommands._rate_limit_config`. A command inside its cooldown replies `Please wait Ns before using /<cmd> again.` and does nothing.

| Cooldown | Commands |
|---|---|
| 30 s | `analyze`, `ai_strategy`, `ai_decision` (expensive AI calls) |
| 5 s | `status`, `balance`, `positions`, `orders`, `funding_rate`, `book`, `market_regime`, `liquidation_warnings`, `strategies`, `risk`, `settings` |
| 2 s | `start`, `help`, `leverage`, `position_mode`, `set`, `ai_status`, `exchange` |
| 60 s | `execute` (enforced inside the conversation flow) |
| none | `kill`, `cancel` — safety commands are never throttled |
| 2 s (default) | anything not listed above |

### Message shapes

Real templates copied from the handlers (`{}` = runtime value):

**`/status`**
```
📊 *Bot Status*

*Positions:* {n} open
*Daily PnL:* {🟢|🔴} ${amount:,.2f}
*Circuit Breakers:* {✅|⚠️} {n} active
*Active Strategies:* {names or None}
*Exchange:* 🟢 Bybit USDT perpetual (pybit)
*Orders Submitted:* {n}
*Orders Filled:* {n}
*Orders Rejected:* {n}
```

**`/positions`** (max 15 rows; total PnL is summed across *all* positions, not just the 15 shown)
```
📋 *Open Positions*

Symbol        Side   Size    Entry        Mark      Liq.Px      PnL  Lev
------------------------------------------------------------------------
{symbol}     {side} {size}  {entry:10.4f} {mark:10.4f} {liq:10.4f} {pnl:>+10,.2f} {lev:>4}

*Total Unrealized PnL:* {🟢|🔴} ${total:+,.2f}
```
With no positions it replies `📋 *Open Positions*\n\nNo open positions.`

**`/balance`** (assets sorted by name)
```
💳 *Account Balance*  |  Exchange: {exchange}
Asset           Free          Locked          Total
------------------------------------------------------
{asset}  {free:>14.4f} {locked:>14.4f} {total:>14.4f}

*Total Portfolio Value:* ${total_usdt:,.2f}
```

**`/kill` confirmation card**
```
🚨 *Kill Switch*

Are you sure you want to activate the emergency kill switch?

This will:
• Cancel all open orders on the exchange
• Place no new trades
• Not close existing positions (manual action required)

*This action cannot be undone via Telegram.*
```

**`/kill` result**
```
🚨 *Kill Switch Activated*

• New entries: *halted*
• Open orders cancelled: *{cancelled}*.
• Existing positions: *remain open* — manage them manually.
```

Variants of the middle line: `No open orders to cancel.` when there was nothing to do, and `Open orders cancelled: *{n}* — *⚠️ {m} could not be cancelled*:` followed by up to 8 per-order reasons when some failed.

---

## CLI Commands

### Entry points

| Command | Defined as | Purpose |
|---|---|---|
| `quad` | `quad = "quad.cli:app"` | The Typer CLI documented below |
| `quad-run` | `quad-run = "quad.__main__:run"` | Equivalent to `python -m quad`: launch the trading process in the foreground |
| `python -m quad` | `quad.__main__:run` | Same as `quad-run` |

There is no `quad-bot` script. The old `quad-bot = quad.__main__:main` entry pointed at an `async def` coroutine, so the generated console-script wrapper returned an un-awaited coroutine and the process exited without starting the bot. Use `quad-run` or `python -m quad`.

### Global options

There are none. `quad` itself only accepts `--help`, `--install-completion`, and `--show-completion`; it prints help when called with no command. Every command declares its own options:

| Option | Commands | Meaning |
|---|---|---|
| `--config`, `-c` | `status`, `balance`, `positions`, `orders`, `risk`, `evaluate`, `execute`, `backtest`, `config`, `db-info`, `run`, `start`, `health`, `trades`, `decisions` | Path to the config **YAML file** (default `config/config.yaml`) |
| `--dry-run` / `--live`, `-n` | `start`, `execute` | Dry run (default) vs permitting real orders |
| `--yes`, `-y` | `execute` | Required confirmation for `--live` |
| `--limit`, `-n` | `trades`, `decisions` | Number of rows (default 20) |
| `--days`, `-d` | `backtest` | Period length (default 30) |
| `--timeout` | `health` | HTTP timeout in seconds (default 3.0) |
| `--path`, `-p` | `logs` | Log file to tail (default `logs/quad.log`) |
| `--lines`, `-n` | `logs` | How many lines (default 50) |

`stop` and `strategies` take no options at all.

`--config` is resolved as a file: the config manager is constructed from `Path(config_path).parent`, so `--config /etc/quad/config.yaml` reads `/etc/quad/config.yaml` plus any env-var overrides.

### Command index

| Command | Reads | Live exchange call? |
|---|---|---|
| `quad status` | config | No |
| `quad balance` | local DB | No |
| `quad positions` | local DB | No |
| `quad orders` | local DB | No |
| `quad trades` | local DB | No |
| `quad decisions` | local DB | No |
| `quad db-info` | local DB | No |
| `quad risk` | config | No |
| `quad config` | config | No |
| `quad evaluate <strategy>` | config + strategy registry | No |
| `quad execute <strategy>` | config + strategy registry | No |
| `quad backtest <strategy>` | config + strategy registry | No |
| `quad strategies` | strategy registry | No |
| `quad logs` | log file | No |
| `quad health` | running bot's HTTP endpoint | No (localhost HTTP) |
| `quad run` | — | Runs the bot |
| `quad start` | — | Runs the bot |
| `quad stop` | — | No (prints instructions) |

---

### Lifecycle commands

#### `quad run`

Runs the full orchestrator in the foreground: config → database → Bybit → market data → risk → execution → Telegram → health server. Blocks until SIGINT/SIGTERM.

```bash
quad run
quad run --config config/config.yaml
```

#### `quad status`

Resolved trading configuration only — it does not query the exchange or the database.

```bash
quad status
```
```
==================================================
  QUAD FUTURES TRADING BOT — STATUS
==================================================
  Mode:          bybit
  Dry Run:       True
  Exchange:      bybit
  Testnet:       True
  Leverage:      10x
  Margin Mode:   isolated
  Position Mode: one_way
  Config File:   config/config.yaml
  Timestamp:     2026-09-30 02:37:02 UTC
==================================================
```

#### `quad start`

The same foreground launcher plus a **dry-run guard**. `--dry-run` is the default and is the safe first step.

```bash
# Dry run (default)
quad start
quad start --dry-run

# Live — refused unless both guards are cleared
quad start --live
```

`--live` exits 1 and names the guard that tripped:

```
❌ --live refused: exchange.testnet is still true.
   Set `exchange.testnet: false` (or BYBIT_TESTNET=false) first.
```
```
❌ --live refused: `_dry_run` is still true. The engine blocks every order while it is set.
   Set `_dry_run: false` (or QUAD_DRY_RUN=false) first.
```

If both guards are cleared it prints `🚨 LIVE MODE: real orders will be placed on Bybit.` and then starts.

Options: `--config/-c`, `--dry-run/--live/-n`. There is no `--strategy` option.

#### `quad stop`

Purely explanatory — it performs no action, takes no options, and exits 0. The bot runs in the foreground, so it stops on SIGINT (Ctrl+C) or SIGTERM, which triggers the orchestrator's reverse-order graceful shutdown.

```bash
quad stop
```
```
Quad runs in the foreground and stops gracefully on SIGINT/SIGTERM.

  Foreground:  press Ctrl+C
  Docker:      docker compose stop quad

A remote kill switch is available from Telegram: /kill
  (halts new entries and cancels open orders; open positions remain).
```

There is no `--no-close-positions` or `--emergency` flag; nothing on the CLI closes positions.

---

### Data commands (local database)

`balance`, `positions`, `orders`, `trades`, and `decisions` read the **local database**, last written by the trading cycle. They are not live exchange queries and say so on failure as well as on success. Use the Telegram equivalents for live data.

#### `quad balance`

Prints the most recent account snapshot (id, exchange, total USDT, available balance, total wallet balance, timestamp).

```bash
quad balance
```

When no snapshot exists it exits 1:

```
No account snapshot recorded yet.
  The account row is written by the trading cycle; run the bot first.
  Live balances: Telegram /balance, or query Bybit directly.
```

#### `quad positions`

Most recent 20 rows. Columns: `SYMBOL  SIDE  SIZE  ENTRY  PNL`.

```bash
quad positions
```
```
SYMBOL      SIDE   SIZE        ENTRY        PNL

  (from the database, not a live exchange query)
```

With no rows it prints `No positions recorded in the database.` plus the pointer to Telegram `/positions`.

#### `quad orders`

Most recent 20 rows. Columns: `TIMESTAMP  SYMBOL  SIDE  QTY  STATUS`.

```bash
quad orders
```

#### `quad trades`

Most recent 20 rows (default). Columns: `TIMESTAMP  SYMBOL  SIDE  QTY  PRICE  PNL`.

```bash
quad trades
quad trades -n 100
quad trades --limit 50 --config config/config.yaml
```

Prints `No trades recorded yet.` when the table is empty.

#### `quad decisions`

Most recent 20 rows (default). Columns: `TIMESTAMP  SYMBOL  ACTION  OUTCOME  CONF`.

```bash
quad decisions
quad decisions -n 50
```

Prints `No decisions recorded yet.` when the table is empty.

If the database cannot be read at all, all five exit 1 with `❌ Could not read the database: <error>`.

---

### Strategy commands

#### `quad strategies`

Lists every registered strategy with its description and parameter spec. Takes no options. Real output from this checkout:

```
Registered Strategies
============================================================

trend_following
  Trend following using EMA crossover and ADX filter with TP/SL brackets
    - fast_ema (int, default: 9): Fast EMA period
    - slow_ema (int, default: 21): Slow EMA period
    - adx_period (int, default: 14): ADX calculation period
    ...
============================================================
```

#### `quad evaluate <strategy>`

Prints the strategy's description, its configured parameters, and the full parameter spec. It is a **description dump** — it does not evaluate market data.

```bash
quad evaluate trend_following
```
```
Strategy: trend_following
  Description: Trend following using EMA crossover and ADX filter with TP/SL brackets
  Parameters: {'enabled': True, 'fast_ema': 9, 'slow_ema': 21, 'adx_threshold': 25, 'trade_capital_usd': 5}

  • fast_ema: Fast EMA period [int] (default: 9)
  • slow_ema: Slow EMA period [int] (default: 21)
  ...

To run evaluation live, use:
  quad execute trend_following
```

Unknown name exits 1 and lists what is registered.

#### `quad execute <strategy>`

Validates the request and reports; it **does not run the strategy**. Strategy execution happens in the trading process (`quad start` / `quad-run`), or via Telegram `/execute`.

Options: `--dry-run/--live/-n`, `--yes/-y`, `--config/-c`.

```bash
# Validate only (default)
quad execute trend_following
```
```
Executing strategy: trend_following
  Dry run: True

[DRY RUN] No orders will be placed.
```

`--live` requires `--yes`:

```
❌ Live execution requires explicit confirmation.
  Re-run with `--yes` to place real orders, e.g.:
  quad execute trend_following --live --yes
```

and is refused outright while `exchange.testnet` is true:

```
❌ --live refused: exchange.testnet is still true.
   Set `exchange.testnet: false` (or BYBIT_TESTNET=false) first.
```

If every guard passes:

```
[LIVE] Orders will be placed on the exchange.

  This command only validates the request. Strategy execution
  runs in the trading process — use `quad start`, or the
  Telegram /execute flow.
```

#### `quad backtest <strategy>`

**Not implemented.** The command always exits 1.

```bash
quad backtest trend_following
quad backtest trend_following --days 90
```
```
Backtesting strategy: trend_following
  Period: 30 days

❌ Backtesting is not implemented yet.
  Required: a configured DatabaseManager with historical futures data,
  a strategy instance, and an underlying symbol.
  See docs/strategy-development.md for the planned engine.run() API.
```

The only option is `--days/-d` (default 30) and `--config/-c`. There is no `--start`, `--end`, `--symbol`, `--report`, or `--compare`.

---

### Configuration and database

#### `quad config`

Read-only view of the fully resolved configuration (file + env overrides) as an indented tree.

```bash
quad config
```
```
Resolved Configuration
==================================================
execution:
  reconcile_interval_seconds: 60
  ...
exchange:
  name: bybit
  testnet: True
ai:
  api_key: ***REDACTED***
  ...
mode: bybit
dry_run: True
...
```

Secret-like keys are replaced with `***REDACTED***` when they hold a string value: any key containing `secret`, `password`, `passwd`, `token`, `api_key`, or `private_key`. The `persistence.dsn` password is masked (`postgresql://quad:***@postgres:5432/quad`); a SQLite path is printed as-is.

There is no `quad config set`, `quad config reload`, or `quad config <section>` — runtime changes go through Telegram `/set`, and the CLI has `--config/-c` only.

#### `quad db-info`

Shows the masked DSN and per-table row counts.

```bash
quad db-info
```
```
Database Info
==================================================
  DSN: data/quad.db
  File: data\quad.db (176.0 KiB)

TABLE                             ROWS
------------------------------------
tenants                          <n>
exchange_credentials             <n>
...
liquidation_events               <n>
```

The table list is every model table in `quad.persistence.models.ALL_MODELS` (tenants, exchange_credentials, tenant_config, telegram_bindings, pairing_codes, accounts, positions, orders, trades, decisions, strategy_state, sessions, performance_snapshots, circuit_breaker_events, config_changes, error_logs, optimization_runs, optimization_recommendations, funding_payments, liquidation_events).

- A SQLite DSN is also reported with its file size; if the file does not exist you get `File: <path> (not created yet — has the bot ever run?)`.
- `-1` in the `ROWS` column means the `COUNT(*)` for that table failed (missing table, permission, or driver issue).
- If row counts cannot be read at all, the command prints `Could not read row counts: <error>` and exits 0.

---

### Risk and diagnostics

#### `quad risk`

Prints the **configured** risk limits, not live risk state. There is no running risk manager behind the CLI, so live values must come from Telegram `/risk`.

```bash
quad risk
```
```
Risk Status
==================================================
  Max Positions:             1
  Max Position Size:         10%
  Max Portfolio Risk:        20.0%
  Max Daily Loss:            $500.0
  Max Drawdown:              25.0%
  Min Liquidation Distance:  20%
  Liquidation Warn Fraction: 50% of 1/leverage distance
  Max Funding Rate Cost:     0.1000%
  Max Position Concentration: 40%
==================================================

  Use the Telegram bot `/risk` command for live risk status.
  CLI risk queries require a running risk manager.
```

#### `quad health`

Queries the **running** bot's health endpoint over localhost HTTP — it is not a local state dump. It reads `monitoring.health_server.port` (default 9090) and `bind_address`, rewriting `0.0.0.0`/`::` to `127.0.0.1`, and sends the `X-API-Key` header when `health_server.api_key` is set.

```bash
quad health
quad health --timeout 5
```

Output when reachable (the endpoint returns `status: "ok"` when nothing is degraded):

```
Health (http://127.0.0.1:9090/health)
==================================================
  status:   ok
  uptime:   <seconds>
  version:  <monitoring.health_server.version>
  OK    <component>
  FAIL   <component>
  degraded: <component>, ...
```

Exit codes:

| Situation | Exit |
|---|---|
| Endpoint unreachable | 1 — `❌ No bot reachable at <url> (<reason>).` + `The bot may not be running, or the health server is disabled.` |
| Any component in `degraded` | 1 (after printing the report) |
| All components healthy | 0 |

Unreachable case:

```
❌ No bot reachable at http://127.0.0.1:9090/health ([WinError 10061] No connection could be made because the target machine actively refused it).
   The bot may not be running, or the health server is disabled.
```

#### `quad logs`

Tails a **log file**. Options are `--path/-p` (default `logs/quad.log`) and `--lines/-n` (default 50). There is no `--level`, `--follow`, or `--tail`.

```bash
quad logs
quad logs -p logs/quad.log -n 200
```

The bot logs to **stdout**, not to a file: `_configure_logging()` wires structlog to the standard streams only (JSON by default, level from `QUAD_LOG_LEVEL` / `logging.level`, format from `QUAD_LOG_FORMAT` / `logging.format`). There is no file sink, so `logs/quad.log` only exists if you redirect stdout there yourself. In Docker, read the container log:

```bash
docker compose logs quad          # add -f to follow
```

Missing file exits 1:

```
No log file at logs\quad.log.
Logs go to stdout (captured by the container runtime in Docker), not to a file, unless QUAD_LOG_FILE is configured.
```

---

## Commands and flags that do not exist

Verified against `src/quad/cli/app.py`. Each row below should be read as `quad` followed by what is listed; none of them are accepted.

| Not real | Reality |
|---|---|
| `start --strategy <name>` | No such option. Strategy selection comes from config (`trading.default_strategy`) or Telegram `/strategies`. |
| `stop --no-close-positions`, `stop --emergency` | `stop` takes no options and does nothing but print shutdown instructions. |
| `position <id>` | No single-position command exists. `positions` lists the most recent rows. |
| `cancel <order-id>` | No CLI cancellation path. Use Telegram `/cancel <order_id>`. |
| `strategy set <name>` | There is no `strategy` subcommand group. |
| `trades --from`, `trades --to`, `trades --symbol` | Only `--limit`/`-n` and `--config`/`-c` exist. |
| `logs --level`, `logs --follow`, `logs --tail` | Only `--path`/`-p` and `--lines`/`-n` exist. |
| `backtest --start`, `--end`, `--symbol`, `--report`, `--compare` | Only `--days`/`-d` exist; the command always exits 1. |
| `execute --no-dry-run` | The flag pair is `--dry-run`/`--live`, plus `--yes`/`-y`. |
| `config strategy`, `config set`, `config reload` | `config` is a read-only view with `--config`/`-c` only. |
| `positions --open`, `--symbol`, `--all` | No filtering options; always the 20 most recent rows. |
| `orders --position`, `orders --open` | No filtering options; always the 20 most recent rows. |
| `quad-bot` console script | Removed; it pointed at an `async def` and never started the bot. Use `quad-run` / `python -m quad`. |

There are also no global options (`--data-dir`, `--log-level`, `--version`, or a top-level `--dry-run`). Log level and format come from config (`logging.level`, `logging.format`) or the environment (`QUAD_LOG_LEVEL`, `QUAD_LOG_FORMAT`).

Telegram has no PnL-only command either.
