# Interface Commands Reference (Telegram + CLI)

---

## Overview

Quad provides **two interfaces** for user interaction:

1. **Telegram Bot (Primary)** -- Real-time trading control via Telegram commands. Push notifications, instant status checks, and full bot control from any device.
2. **Typer CLI (Secondary)** -- Command-line interface for debugging, local operations, and detailed configuration.

This document covers both interfaces.

---

## Telegram Commands

The Telegram bot is the primary user interface. It uses **polling mode** (no webhook configuration needed).

### Setup

1. Create a bot via [@BotFather](https://t.me/botfather) on Telegram
2. Set `TELEGRAM_BOT_TOKEN` in your `.env` file
3. Add your Telegram chat ID to `TELEGRAM_NOTIFICATION_CHAT_ID` in `.env`
4. Start the bot with `quad start` -- Telegram interface initializes automatically

### Commands

All commands are available to any chat:

| Command | Description |
|---|---|
| `/start` | Welcome message with available commands |
| `/status` | Show bot health, position summary, PnL, risk status |
| `/positions` | List all open positions with unrealized PnL |
| `/orders` | Show open or pending orders |
| `/balance` | Account balances, total USDT value |
| `/funding_rate` | Current funding rates across tracked symbols |
| `/book <symbol>` | Show order book depth for a symbol |
| `/strategies` | List available strategies and their status |
| `/execute` | Execute a strategy signal (interactive, multi-step flow) |
| `/risk` | Risk status, gate states, circuit breakers, exposure report |
| `/kill` | Emergency kill switch activation (requires confirmation) |
| `/cancel <id>` | Cancel an order by its ID |
| `/settings` | Current configuration overview |
| `/set <key> <value>` | Set a configuration value at runtime |
| `/leverage` | Set or view leverage for a symbol |
| `/position_mode` | Toggle between ONE_WAY and HEDGE position mode |
| `/liquidation_warnings` | Show liquidation risk warnings for open positions |
| `/market_regime` | Show detected market regime (trending, ranging, volatile) |
| `/analyze` | AI analysis of current market conditions |
| `/ai_strategy` | Groq AI recommends a strategy based on market regime |
| `/ai_status` | AI trading system status and metrics |
| `/ai_decision` | Request an AI-driven trading decision (ENTER/EXIT/HOLD) |
| `/help` | Show available commands |

### Command Output Examples

**`/status` output:**
```
Quad Status
State: ACTIVE (since 12:00 UTC)
Uptime: 2d 4h 30m
Mode: testnet
Strategy: trend_following
Position Mode: HEDGE
Margin Mode: ISOLATED
Leverage: 3x
Exchange: Connected (latency: 45ms)
Positions: 2 open / 15 total
Portfolio: 10,045.20 USDT (+0.45%)
Circuit Breakers: INACTIVE
```

**`/positions` output:**
```
Open Positions
1. BTCUSDT  | LONG  | 0.5 cont | Entry: 67,500 | Mark: 68,200 | PnL: +350.00 USDT
2. ETHUSDT  | SHORT | 2.0 cont | Entry: 3,520  | Mark: 3,480  | PnL: +80.00 USDT
```

**`/pnl` output:**
```
Portfolio Summary
Total P&L: +45.20 USDT
Daily P&L: +32.50 USDT
Win Rate: 66.7% (8/12)
Drawdown: 1.2%
```

---

## CLI Commands

Quad's Typer-based CLI provides full bot control for debugging and local operations.

```bash
# Basic usage
quad --help
quad <command> --help
```

### Global Options

| Option | Description |
|---|---|
| `--config PATH` | Path to config directory (default: `./config`) |
| `--data-dir PATH` | Path to data directory (default: `./data`) |
| `--log-level TEXT` | Log level: DEBUG, INFO, WARN, ERROR (default: INFO) |
| `--dry-run` | Run without executing real orders |
| `--version` | Show version and exit |

---

### Lifecycle Commands

#### `quad start`

Start the trading bot.

```bash
# Start with default configuration
quad start

# Start in dry-run mode (safest first step; no real orders)
quad start --dry-run

# Start on OKX demo (testnet is the default when OKX_TESTNET=true)
quad start

# Start with live trading (set OKX_TESTNET=false -- dangerous, real funds)
quad start

# Start with a specific strategy
quad start --strategy trend_following

# Start with custom config
quad start --config /path/to/config
```

**Output:**
```
Quad v2.0.0 starting...
  Mode: testnet
  Strategy: trend_following
  Exchange: okx (USDT perpetual demo)
  Database: postgresql://quad@localhost:5432/quad
  Log level: INFO
  Telegram: enabled (polling mode)

State: WARMING (connecting to exchange, loading data...)
```

#### `quad stop`

Stop the trading bot gracefully.

```bash
# Graceful stop (close positions if configured)
quad stop

# Force stop without closing positions
quad stop --no-close-positions

# Emergency stop (close all positions immediately)
quad stop --emergency
```

**Output:**
```
Quad stopping...
  Closing positions: 2 open
    BTCUSDT LONG: closing...
    ETHUSDT SHORT: closing...
  Positions closed. PnL: +45.20 USDT
  State: IDLE
```

#### `quad status`

Show current bot status.

```bash
quad status
```

**Output:**
```
Quad Status
═══════════════
State:        ACTIVE (since 2026-07-07 10:00:00 UTC)
Uptime:       2d 4h 30m
Mode:         testnet
Strategy:     trend_following
Position Mode: HEDGE
Margin Mode:  ISOLATED
Leverage:     3x

Exchange:     OKX V5 USDT Perpetual (demo)
  Connected:  yes
  WS streams: 4 active

Telegram:     enabled
  Bot:        @quad_trading_bot
  Polling:    active

Positions:    2 open / 15 total
  BTCUSDT LONG: 0.5 contracts, +32.50 USDT (unrealized)
  ETHUSDT SHORT: 2.0 contracts, +80.00 USDT (unrealized)

Portfolio:    10,045.20 USDT (+0.45%)
  Today PnL:  +32.50 USDT
  Win rate:   66.7% (8/12)

Risk Status:
  Drawdown:   1.2% (peak: 10,120.00)
  Liquidation Risk: LOW
  Circuit Breaker: INACTIVE
```

---

### Position Commands

#### `quad positions`

List all positions.

```bash
# All positions
quad positions

# Only open positions
quad positions --open

# Positions for a specific underlying
quad positions --symbol BTC

# Include closed positions
quad positions --all
```

**Output:**
```
Open Positions
═══════════════
ID    Strategy         Symbol   Side   Size  Entry   Mark    PnL         Liq.Price  Status
───   ─────────        ──────   ────   ────  ─────   ─────   ─────       ─────────  ──────
1     trend_following   BTCUSDT  LONG   0.5   67500   68200   +350.00    45200      OPEN
2     mean_reversion    ETHUSDT  SHORT  2.0   3520    3480    +80.00     4980       OPEN
```

#### `quad position <id>`

Show detailed information for a specific position.

```bash
quad position 1
```

**Output:**
```
Position #1
═══════════
Strategy:     trend_following
Symbol:       BTCUSDT
Side:         LONG
Position Side: BOTH (net LONG)
Size:         0.5 contracts
Entry Price:  67,500 USDT
Mark Price:   68,200 USDT
Liquidation:  45,200 USDT
PnL:          +350.00 USDT (+10.4%)
Status:       OPEN

Margin:
  Type:       ISOLATED
  Leverage:   3x
  Initial:    11,250.00 USDT
  Maintenance: 5,625.00 USDT

Funding:
  Last Rate:  0.001% (positive -- receiving)
  Cumulative: +2.50 USDT

Risk:
  Stop Loss:  62,500 USDT (max loss: -2,500.00 USDT)
  Take Profit: 72,000 USDT

Opened:       2026-07-05 14:30:00 UTC
Duration:     1d 19h 30m
```

---

### Order Commands

#### `quad orders`

Show recent orders.

```bash
# Recent orders
quad orders

# Orders for a specific position
quad orders --position 1

# Open orders only
quad orders --open
```

**Output:**
```
Recent Orders
══════════════
ID    Type          Symbol   Side   PosSide  Qty   Price    Status     Filled  Time
───   ─────         ──────   ────   ───────  ───   ─────    ──────     ──────  ────
101   LIMIT         BTCUSDT  BUY    LONG     0.5   67500    FILLED     0.5     14:30:02
102   STOP_MARKET   BTCUSDT  SELL   LONG     0.5   62500    PENDING    0.0     10:15:00
```

#### `quad cancel <order-id>`

Cancel a specific order.

```bash
quad cancel 102
```

---

### Strategy Commands

#### `quad strategies`

List available strategies.

```bash
quad strategies
```

**Output:**
```
Available Strategies
════════════════════
Name              Description                                          Status
────              ───────────                                          ──────
trend_following   Follow trend using EMA crossovers + ATR stops        ACTIVE
grid_trading      Place buy/sell orders in a price grid                INACTIVE
mean_reversion    Trade mean reversion using RSI + Bollinger Bands     ACTIVE
dca_bot           Dollar-cost average into positions                   INACTIVE
market_making     Provide liquidity with two-sided orders              INACTIVE
```

#### `quad strategy set <name>`

Set the active strategy.

```bash
quad strategy set iron_condor
```

---

### Configuration Commands

#### `quad config`

View current configuration.

```bash
# Full config
quad config

# Specific section
quad config trading
quad config risk
quad config strategy
```

#### `quad config set <key> <value>`

Set a configuration value at runtime.

```bash
quad config set risk.max_leverage 5
quad config set strategy.trend_following.ema_fast 12
```

**Output:**
```
Config updated: risk.max_position_size = 5
Applied immediately. No restart required.
```

#### `quad config reload`

Reload configuration from files.

```bash
quad config reload
```

---

### Backtest Commands

#### `quad backtest`

Run a backtest.

```bash
# Backtest with default settings
quad backtest --strategy trend_following

# Specify date range
quad backtest --strategy grid_trading --start 2024-01-01 --end 2024-06-30

# Specify underlying symbol
quad backtest --strategy mean_reversion --symbol BTC

# Generate HTML report
quad backtest --strategy trend_following --report html

# Compare multiple strategies
quad backtest --strategy trend_following --strategy mean_reversion --compare
```

**Output:**
```
Backtest Results: trend_following
═════════════════════════════════
Period:       2024-01-01 to 2024-06-30 (181 days)
Symbol:       BTCUSDT
Leverage:     3x

Performance:
  Total Trades:    18
  Win Rate:        66.7% (12/18)
  Profit Factor:   2.10
  Total PnL:      +1,850.00 USDT
  Max Drawdown:   4.1%
  Sharpe Ratio:   1.65

Summary:
  Best Trade:     +420.00 USDT
  Worst Trade:    -180.00 USDT
  Avg Trade:      +102.78 USDT
  Avg Hold Time:  2.8 days
```

---

### Risk Commands

#### `quad risk`

Show risk status.

```bash
quad risk
```

**Output:**
```
Risk Status
═══════════════
Drawdown:      1.2% (session)
Portfolio:     10,045.20 USDT
Daily PnL:     +32.50 USDT
Daily Loss Limit:  500.00 USDT (not breached)

Circuit Breakers:
  P&L Drawdown:         INACTIVE (threshold: 15%)
  Daily Loss:           INACTIVE (threshold: 10%)
  Consecutive Losses:   INACTIVE (threshold: 3)
  Position Growth:      INACTIVE (threshold: 50%)
  Liquidation Cascade:  INACTIVE (threshold: 10% proximity)
  Funding Rate Spike:   INACTIVE (threshold: 0.2%)
  Volatility:           INACTIVE (threshold: 50% change)

Pre-Trade Check Counters:
  Max Positions:        PASS (last 24 checks)
  Portfolio Risk:       PASS (last 24 checks)
  Daily Loss:           PASS (last 24 checks)
  Drawdown:             PASS (last 24 checks)
  Liquidation Risk:     PASS (last 24 checks)
  Funding Rate Cost:    PASS (last 24 checks)
  Leverage Limit:       PASS (last 24 checks)
  Concentration:        PASS (last 24 checks)
  Correlation:          PASS (last 24 checks)
```

---

### History Commands

#### `quad trades`

Show trade history.

```bash
# Recent trades
quad trades

# All trades in date range
quad trades --from 2026-06-01 --to 2026-07-01

# Trades for specific symbol
quad trades --symbol BTCUSDT
```

#### `quad decisions`

Show recent strategy decisions.

```bash
quad decisions
```

**Output:**
```
Recent Decisions
════════════════
Time                Action        Symbol   Side  Reason            Executed
────                ──────        ──────   ────  ──────            ────────
10:00:00            open_long     BTCUSDT  LONG  trend_following   YES
10:00:00            hold          ETHUSDT  SHORT holding            N/A
09:00:00            close_short   ETHUSDT  SHORT take_profit       YES
08:00:00            hold          BTCUSDT  LONG  holding            N/A
07:00:00            adjust_stop   BTCUSDT  LONG  trailing_stop     YES
```

---

### Health/Diagnostic Commands

#### `quad health`

Show system health.

```bash
quad health
```

**Output:**
```
Quad Health
═══════════════
Process:      running (PID 12345)
Uptime:       2d 4h 30m
Memory:       145 MB RSS / 256 MB limit
CPU:          2.3%

Connections:
  Exchange:   connected (latency: 45ms)
  WebSocket:  4 active streams
  Database:   connected (7.2 MB, 2.3 MB free)
  Telegram:   connected (polling active)

Last Error:   none (0 errors in last 24h)
Cycle Time:   950ms (target: < 5s)
```

#### `quad logs`

View recent logs.

```bash
# Last 50 log lines
quad logs

# Follow logs in real-time
quad logs --follow

# Filter by level
quad logs --level ERROR

# Last N lines
quad logs --tail 200
```
