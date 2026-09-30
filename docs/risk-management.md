# Risk Management Deep Dive

---

## Core Philosophy

Quad's risk management system is built on a single principle: **survival first, profitability second.** Every trade must pass through multiple independent validation gates before execution. The system is designed to prevent catastrophic loss -- especially critical for futures trading, where leverage magnifies both gains and losses.

The risk management layer operates as a pipeline of four distinct subsystems:

1. **Pre-Trade Checks (9 Gates)** -- Validates every trade against hard limits
2. **Margin Monitor** -- Tracks available/used margin and liquidation proximity in real-time
3. **Circuit Breakers (7 Types)** -- Automated emergency responses to adverse conditions
4. **Stop-Loss/Take-Profit** -- Manages position exits

Each subsystem is independent and can reject a trade at any point. A trade must pass ALL gates to be executed.

---

## Futures-Specific Risk Concepts

### Why Futures Risk Is Different

Futures trading introduces risk dimensions not present in spot trading:

| Risk Dimension | Why It Matters | Quad's Approach |
|---|---|---|
| **Liquidation Risk** | Leveraged positions can be liquidated if maintenance margin is breached | Monitor distance to the exchange-reported liquidation price against a **leverage-aware** threshold (see below) |
| **Funding Rate Cost** | Perpetual futures have recurring funding payments (every 8h) | Project cost over `funding_rate_periods`; block when projected cost exceeds `max_funding_rate_cost` × position value |
| **Leverage Risk** | Higher leverage amplifies losses as well as gains | Cap effective portfolio leverage at `max_leverage` (also bounded by the account's own limit) |
| **Gap Risk** | Price can gap through stop-losses in low liquidity | Use `STOP_MARKET` orders (market-on-trigger, not limit-if-triggered) |
| **Correlation Risk** | Multiple positions can move against you simultaneously | Cap each quote-asset group's notional at `correlation_threshold_pct` of portfolio value |
| **Concentration Risk** | Too much capital in one position or symbol | Cap single-position exposure via `max_position_concentration` and `max_position_size_pct` |
| **Volatility Risk** | Sudden volatility spikes can trigger rapid P&L changes | Volatility circuit breaker trips on 24h mark-price moves above the configured threshold |

---

## Pre-Execution Validation Pipeline

Every trading decision flows through this pipeline before an order reaches Bybit. The order below is the evaluation order in `GatePipeline._gate_sequence()` (`src/quad/risk/gates.py`), which short-circuits on the first failure:

```
Strategy Suggestion
        │
        ▼
┌────────────────────────────────────────────────────────────┐
│  1. MAX_POSITIONS_GATE         Open positions ≤ limit?      │
└────────────────────────────────────────────────────────────┘
        │ Pass
        ▼
┌────────────────────────────────────────────────────────────┐
│  2. PORTFOLIO_RISK_GATE       Portfolio risk within bounds?│
└────────────────────────────────────────────────────────────┘
        │ Pass
        ▼
┌────────────────────────────────────────────────────────────┐
│  3. DAILY_LOSS_GATE           Daily loss within threshold? │
└────────────────────────────────────────────────────────────┘
        │ Pass
        ▼
┌────────────────────────────────────────────────────────────┐
│  4. DRAWDOWN_GATE             Drawdown within range?       │
└────────────────────────────────────────────────────────────┘
        │ Pass
        ▼
┌────────────────────────────────────────────────────────────┐
│  5. LIQUIDATION_RISK_GATE     No position near liquidation?│
└────────────────────────────────────────────────────────────┘
        │ Pass
        ▼
┌────────────────────────────────────────────────────────────┐
│  6. FUNDING_RATE_COST_GATE    Projected funding acceptable?│
└────────────────────────────────────────────────────────────┘
        │ Pass
        ▼
┌────────────────────────────────────────────────────────────┐
│  7. LEVERAGE_LIMIT_GATE       Effective leverage ≤ max?   │
└────────────────────────────────────────────────────────────┘
        │ Pass
        ▼
┌────────────────────────────────────────────────────────────┐
│  8. POSITION_CONCENTRATION_GATE  Single position ≤ cap?   │
└────────────────────────────────────────────────────────────┘
        │ Pass
        ▼
┌────────────────────────────────────────────────────────────┐
│  9. CORRELATION_GATE          Quote-asset groups ≤ cap?   │
└────────────────────────────────────────────────────────────┘
        │ Pass
        ▼
   Order Submitted
```

If any gate rejects the trade, a specific reason code is logged and the decision is recorded.

### Gate Details

#### 1. Max Positions Check

Counts futures positions with non-zero size and adds 1 when the action is an entry (`ENTER`, `open_long`, `open_short`). Blocks when the total would exceed `risk.max_positions`.

**Rejection Example:** "Position limit 1 reached (1 open, 1 adding)"

#### 2. Portfolio Risk Check

Ensures the proposed notional stays within `risk.max_portfolio_risk_pct` of portfolio value.

**Rejection Example:** "Portfolio notional risk 35.00% exceeds limit of 20.00%"

#### 3. Daily Loss Check

Monitors realised daily P&L. If daily loss exceeds `risk.max_daily_loss_usd`, new entries are blocked.

**Rejection Example:** "Daily loss -520.00 exceeds limit -500.00"

#### 4. Drawdown Check

Tracks portfolio peak-to-trough. Blocks new entries if drawdown exceeds `risk.max_drawdown_pct`.

**Rejection Example:** "Drawdown 31.40% exceeds limit of 25.00%"

#### 5. Liquidation Risk Check

Uses the exchange-reported `liquidation_price` and the current mark price to compute distance, then compares it against the **leverage-aware** threshold from `effective_min_liquidation_distance()`:

```
distance (LONG)  = (mark_price - liquidation_price) / mark_price
distance (SHORT) = (liquidation_price - mark_price) / mark_price

effective_min = min(min_distance_to_liquidation_pct,
                    liquidation_distance_fraction / leverage)
```

A flat percentage is wrong for leveraged isolated positions: at N× leverage the liquidation price sits roughly `1/N` from entry, so a 20% threshold would trip *permanently* at 50x where the real distance is only ~2%. With the default `liquidation_distance_fraction: 0.5`, a 50x position is judged against `0.5 / 50 = 1%`; a 10x position against `0.5 / 10 = 5%`. When a position's leverage is unknown, the configured cap is used unchanged.

**Rejection Example:** "Position(s) ['BTCUSDT'] too close to liquidation: [{'symbol': 'BTCUSDT', 'side': 'long', 'distance_pct': 0.8, 'min_distance_pct': 1.0}]"

#### 6. Funding Rate Cost Check

Entry actions only. Projects the funding cost over `risk.funding_rate_periods` (default 3 × 8h = 24h) and rejects when it exceeds `risk.max_funding_rate_cost` × position value.

**Rejection Example:** "Projected funding cost 1.20 exceeds limit 0.80 for BTCUSDT (rate=0.000500)"

#### 7. Leverage Limit Check

Computes **effective** leverage as total notional ÷ wallet balance (including the proposed entry), and compares it against `min(risk.max_leverage, account.max_leverage)`. This is a portfolio-level check, not a per-order one.

**Rejection Example:** "Effective leverage 12.40x exceeds max 10x"

#### 8. Position Concentration Check

Ensures no single position exceeds `risk.max_position_concentration` of portfolio value.

**Rejection Example:** "Concentration violation(s): [{'symbol': 'BTCUSDT', 'notional': '5500.00', 'concentration_pct': '55.00', 'limit_pct': '40.00'}]"

#### 9. Correlation Check

Groups open positions by **quote asset** (the last 4 characters of the symbol, e.g. `USDT`), adds the proposed entry to its group, and rejects when any group's total notional exceeds `risk.correlation_threshold_pct` of portfolio value. This is a group-exposure check, not a price-correlation calculation.

**Rejection Example:** "Correlated exposure violation(s): [{'quote_asset': 'USDT', 'total_notional': '8210.00', 'portfolio_pct': '82.10'}]"

### Runtime Gate Toggling

Gates can be enabled and disabled at runtime:

```python
from quad.risk.gates import GatePipeline

pipeline = GatePipeline(config)
pipeline.get_gate_status()                       # {gate_name: bool, ...}
pipeline.set_gate_enabled("CORRELATION_GATE", False)
```

`set_gate_enabled()` validates the name against `ALL_GATES` (raising `ValueError`
on an unknown one), flips the flag, logs `risk_gate_toggled` at `warning` with the
old and new state, and appends a JSONL audit line to
`data/risk_gate_changes.jsonl`. Disabled gates are skipped by `_gate_sequence()`,
so the pipeline silently stops enforcing that check.

This is an operator-level escape hatch, not a user setting: a disabled gate is a
hole in the safety envelope that nothing re-checks. If it is ever exposed over
Telegram, the CLI, or the HTTP API, it must stay **operator-only** and behind
explicit confirmation — never something a bound chat or an unauthenticated
endpoint can toggle.

---

## Margin Monitor

The Margin Monitor tracks account balances, margin usage, and liquidation proximity in real-time.

### Available Margin Calculation

```
available_margin = wallet_balance - initial_margin - order_margin
```

Where:
- `wallet_balance`: Total USDT in the futures account (including unrealized PnL)
- `initial_margin`: Margin locked by open positions = Σ(position_value / leverage)
- `order_margin`: Margin held for open orders
- `maintenance_margin`: Minimum margin required to keep positions open (typically 50% of initial margin for isolated positions)

### Margin Types

| Type | Description |
|---|---|
| **ISOLATED** | Margin isolated to one position -- liquidation won't affect other positions |
| **CROSS** | Entire wallet balance shared as margin -- positions can cross-liquidate each other |

### Liquidation Price Calculation

For ISOLATED LONG positions:
```
liquidation_price = entry_price × (1 - 1/leverage + maintenance_margin_ratio)
```

For ISOLATED SHORT positions:
```
liquidation_price = entry_price × (1 + 1/leverage - maintenance_margin_ratio)
```

The gates do **not** recompute this — they read the `liquidation_price` the
exchange reports for the position, and only derive the distance to it.

### Margin Alerts

`risk/exposure.py` computes `margin_utilization_pct` (total margin ÷ wallet
balance) and a per-position `distance_to_liquidation_pct`, both surfaced in the
exposure report (`quad risk`, Telegram `/risk`). There is no threshold-based
alert ladder in the code today; the table below is the operational guidance,
not an implemented escalation.

| Condition | Action |
|---|---|
| Margin used > 70% | Warning log, recommend reducing position sizes |
| Margin used > 85% | Block new entries, liquidate least profitable positions |
| Margin used > 95% | Emergency: force-close positions with highest liquidation risk |
| Liquidation distance below the effective threshold | Immediate alert, consider adding margin or reducing position |

---

## Circuit Breakers

`CircuitBreakerManager` (`src/quad/risk/circuit_breakers.py`) implements **seven**
breakers, listed in `ALL_BREAKERS`. A single active breaker blocks all new
trading (`is_trading_allowed()`).

| Constant | Tier | Trigger | Auto-reset |
|---|---|---|---|
| `DAILY_LOSS_BREAKER` | 1 | `daily_pnl < -risk.max_daily_loss_usd` | Yes — at the next UTC day |
| `DRAWDOWN_BREAKER` | 2 | `drawdown_pct > risk.max_drawdown_pct` | Yes — with hysteresis below the limit |
| `CONSECUTIVE_LOSS_BREAKER` | 3 | Streak ≥ `risk.circuit_breakers.consecutive_losses.max_consecutive` (default 5) | Yes — when the streak breaks |
| `KILL_SWITCH` | 4 | Manual (`trigger_kill_switch()`) | No — manual reset with a `KILL_RESET_<uuid>` token |
| `LIQUIDATION_CASCADE_BREAKER` | 1 | A symbol previously within `min_cascade_distance_pct` (default 0.05) of liquidation has vanished | Yes — when no position is near liquidation |
| `FUNDING_RATE_SPIKE_BREAKER` | 1 | `max_consecutive_spikes` (default 3) consecutive cycles with `abs(rate) > funding_rate_spike_threshold` (default 0.001) on one symbol | Yes — when no symbol spikes |
| `VOLATILITY_BREAKER` | 2 | 24h mark-price move exceeds `volatility_breaker_atr_pct` (default 0.05) | Yes — when volatility normalises |

Note: there is **no** position-growth breaker. Breaker state (active flags, peak
value, consecutive-loss streak, funding-spike counts, near-liquidation symbols)
is persisted and restored across restarts on a best-effort basis.

`risk.circuit_breakers.drawdown_tiers` (default `[5.0, 10.0, 15.0]`) is validated
to be strictly increasing and describes the escalating drawdown tiers.

---

## Stop-Loss and Take-Profit

The per-position bracket config lives under `risk.per_position_sl` and
`risk.per_position_tp`. There are no `risk.stop_loss.*` or `risk.take_profit.*`
keys.

### Stop-Loss Strategies

| Type | Description | Best For |
|---|---|---|
| **Fixed** | Stop at a % of trade capital | Simple, all strategies (the only supported `type`) |
| **Trailing Stop** | Adjust stop upward as position becomes profitable | Not implemented; would require repeated `set_stop_loss` actions |
| **Volatility-Adjusted** | Widen stops during high volatility | Not implemented |

### Configuration

```yaml
risk:
  per_position_sl:
    enabled: true
    type: "fixed"
    capital_pct: 30.0     # stop-loss as % of trade capital (default 30)
```

### Take-Profit Strategies

| Type | Description |
|---|---|
| **Fixed** | Target at a % of trade capital (the only supported `type`) |
| **Fixed PnL** | Close at a target USDT amount — express it as `capital_pct` of the configured trade capital |
| **Mark Price Target** | The bracket triggers on `MARK_PRICE` with `priceProtect` on |

### Configuration

```yaml
risk:
  per_position_tp:
    enabled: true
    type: "fixed"
    capital_pct: 50.0     # take-profit as % of trade capital (default 50)
```

Both are enforced at the last mile by the execution engine: with either bracket
enabled, an entry that carries no matching bracket price is **refused**
(`naked_entry_refused`, reason `"missing TP/SL brackets"`). Bracket orders are
submitted with `risk_checked=True` and are bounded by `risk.max_position_size_usd`;
see [Execution Safety Semantics](./strategy-development.md#execution-safety-semantics).

---

## Kill Switch

The kill switch provides an emergency mechanism to immediately halt all trading.

### Trigger Conditions

| # | Condition | Description |
|---|---|---|
| 1 | Circuit Breaker Tier 3 (Consecutive Losses) | Losing streak reaches `max_consecutive` |
| 2 | Manual Command | Telegram `/kill` (requires inline confirmation) or an API/operator call |
| 3 | Liquidation cascade | A near-liquidation position disappears (liquidated) |
| 4 | Funding rate spike | Sustained spike escalation on any tracked symbol |

### Reset

The kill switch never auto-resets. `reset_kill_switch(reset_token)` requires a
token of the form `KILL_RESET_<uuid hex>`; anything else is rejected and logged.

---

## Position Sizing

### Leverage-Adjusted Position Sizing

Position sizing in futures considers leverage as a force multiplier for both gains
and losses. `PositionSizer.compute_size()` (`src/quad/risk/sizing.py`) derives
the full Kelly fraction from the trade history (win rate and average win/loss),
then walks a fixed cap chain:

```
size = kelly_fraction × risk.kelly.fraction × portfolio_value
size = size / risk.max_leverage                      # leverage multiplies exposure
size = min(size, risk.max_position_size_pct × portfolio_value)
size = min(size, risk.max_position_size_usd)
size = min(size, portfolio_value)
if size < risk.min_position_size_usd:
    size = risk.kelly.default_fraction × portfolio_value   # no-history fallback
```

With no trade history yet, Kelly returns 0 and the `default_fraction` fallback
applies (default 2% of portfolio). `kelly.default_fraction` is a percentage, not
a fraction.

### Minimum Position Size Check

All orders are validated against the exchange's minimum notional and quantity
requirements for the specific symbol (`LOT_SIZE` / `MIN_NOTIONAL`). A quantity
below the exchange minimum is floored **up** to satisfy both filters — but if
that floor-up would push the notional past `risk.max_position_size_usd`, the
engine rejects the order instead of rounding it over the risk cap.

---

## Risk Parameter Configuration

All risk parameters live under the `risk` section of the config. The names below
match `RiskConfig` in `src/quad/config/schema.py` exactly, so the sample
validates as-is:

```yaml
risk:
  max_positions: 1
  max_leverage: 50
  max_portfolio_risk_pct: 20.0        # max % of portfolio at risk per trade
  max_daily_loss_usd: 500.0           # absolute daily loss limit in USD
  max_drawdown_pct: 25.0              # max drawdown from peak, in percent
  min_distance_to_liquidation_pct: 0.20   # absolute cap on liquidation distance
  liquidation_distance_fraction: 0.5      # fraction of the 1/leverage distance
  max_funding_rate_cost: 0.001
  funding_rate_periods: 3             # 3 × 8h = 24h projection window
  max_position_concentration: 0.4
  correlation_threshold_pct: 60.0     # max % of portfolio per quote-asset group
  max_position_size_pct: 0.10         # max position as fraction of portfolio
  max_position_size_usd: 10000.0      # absolute notional cap
  min_position_size_usd: 10.0
  per_position_sl:
    enabled: true
    type: "fixed"
    capital_pct: 30.0
  per_position_tp:
    enabled: true
    type: "fixed"
    capital_pct: 50.0
```

Edit `config/config.local.yaml` (or set the matching `QUAD_*` env vars) and
restart. There is no `quad config set` command; `quad config` is a read-only
overview of the resolved config.

---

## Performance Monitoring

The risk management system continuously monitors trading performance:

| Metric | Formula | Target | Use |
|---|---|---|---|
| **Win Rate** | `wins / total_trades × 100` | > 50% | Basic strategy effectiveness |
| **Profit Factor** | `gross_profit / gross_loss` | > 1.5 | Ratio of winning to losing volume |
| **Sharpe Ratio** | `(mean_return - risk_free) / std_return` | > 1.0 | Risk-adjusted return |
| **Max Drawdown** | `max(peak - trough) / peak` | < 10% | Largest peak-to-trough decline |
| **Funding Cost Ratio** | `total_funding_paid / total_pnl` | < 20% | Funding cost efficiency |
| **Avg Win/Loss** | `avg(win) / avg(loss)` | > 1.5 | Average risk-reward achieved |
