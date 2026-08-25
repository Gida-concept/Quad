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
| **Liquidation Risk** | Leveraged positions can be liquidated if maintenance margin is breached | Monitor liquidation proximity, maintain ≥20% distance, alert at thresholds |
| **Funding Rate Cost** | Perpetual futures have recurring funding payments (every 8h) | Track cumulative funding costs, avoid trades with unfavorable rates, alert on spikes |
| **Leverage Risk** | Higher leverage amplifies losses as well as gains | Cap max leverage (default 10x), enforce per-strategy limits, monitor margin utilization |
| **Gap Risk** | Price can gap through stop-losses in low liquidity | Use STOP_MARKET orders, maintain liquidation distance buffer |
| **Correlation Risk** | Multiple positions can move against you simultaneously | Cap portfolio correlation at 0.7, diversify across uncorrelated symbols |
| **Concentration Risk** | Too much capital in one position or symbol | Cap single-position exposure at 40% of portfolio, limit max position count |
| **Volatility Risk** | Sudden volatility spikes can trigger rapid P&L changes | Monitor volatility changes, adjust position sizes during turbulent periods |

---

## Pre-Execution Validation Pipeline

Every trading decision flows through this pipeline before an order reaches Bybit:

```
Strategy Suggestion
        │
        ▼
┌──────────────────────────────┐
│  1. Max Positions            │  Total open positions ≤ configured limit?
└──────────────────────────────┘
        │ Pass
        ▼
┌──────────────────────────────┐
│  2. Portfolio Risk           │  Total risk within bounds (% of portfolio)?
└──────────────────────────────┘
        │ Pass
        ▼
┌──────────────────────────────┐
│  3. Daily Loss               │  Daily PnL breaching configured threshold?
└──────────────────────────────┘
        │ Pass
        ▼
┌──────────────────────────────┐
│  4. Drawdown                 │  Portfolio drawdown within acceptable range?
└──────────────────────────────┘
        │ Pass
        ▼
┌──────────────────────────────┐
│  5. Liquidation Risk         │  Position close to liquidation price?
└──────────────────────────────┘
        │ Pass
        ▼
┌──────────────────────────────┐
│  6. Funding Rate Cost        │  Funding cost within acceptable range?
└──────────────────────────────┘
        │ Pass
        ▼
┌──────────────────────────────┐
│  7. Leverage Limit           │  Leverage ≤ configured maximum?
└──────────────────────────────┘
        │ Pass
        ▼
┌──────────────────────────────┐
│  8. Position Concentration   │  Single position not too concentrated?
└──────────────────────────────┘
        │ Pass
        ▼
┌──────────────────────────────┐
│  9. Correlation              │  Positions not overly correlated?
└──────────────────────────────┘
        │ Pass
        ▼
   Order Submitted
```

If any gate rejects the trade, a specific reason code is logged and the decision is recorded.

### Gate Details

#### 1. Max Positions Check

Verifies total open positions don't exceed configured limit. Checks both total count and per-symbol limit.

**Rejection Example:** "Would exceed max of 5 open positions"

#### 2. Portfolio Risk Check

Ensures total position value (notional * leverage) doesn't exceed configured % of portfolio value.

**Rejection Example:** "Portfolio risk would be 35%, exceeding max 20%"

#### 3. Daily Loss Check

Monitors daily realized + unrealized PnL. If daily loss exceeds configured threshold (default 5%), blocks new entries.

**Rejection Example:** "Daily loss of -6.2% exceeds max of -5%"

#### 4. Drawdown Check

Tracks portfolio peak-to-trough. Blocks new entries if drawdown exceeds threshold.

**Rejection Example:** "Current drawdown of 18% exceeds max 15%"

#### 5. Liquidation Risk Check

Each open position must maintain minimum distance from liquidation price. Calculates liquidation price from position size, leverage, entry price, and margin type.

**Rejection Example:** "Position would have 12% distance to liquidation, below 20% minimum"

#### 6. Funding Rate Cost Check

Evaluates funding rate cost for the proposed position. Blocks entry if 8h funding cost exceeds threshold. Prefers positions with favorable (receiving) funding.

**Rejection Example:** "Funding cost of 0.15% per 8h exceeds max of 0.1%"

#### 7. Leverage Limit Check

Ensures requested leverage doesn't exceed configured maximum (default 10x).

**Rejection Example:** "Requested leverage of 20x exceeds max of 10x"

#### 8. Position Concentration Check

Ensures no single position exceeds configured % of portfolio.

**Rejection Example:** "Adding this position would concentrate 55% in BTCUSDT, exceeding max 40%"

#### 9. Correlation Check

Monitors portfolio correlation. Blocks entry if new position would push portfolio correlation above threshold. Uses rolling 24h price correlation.

**Rejection Example:** "New position would increase portfolio correlation to 0.82, exceeding max 0.7"

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

### Margin Alerts

| Condition | Action |
|---|---|
| Margin used > 70% | Warning log, recommend reducing position sizes |
| Margin used > 85% | Block new entries, liquidate least profitable positions |
| Margin used > 95% | Emergency: force-close positions with highest liquidation risk |
| Liquidation distance < 15% | Immediate alert, consider adding margin or reducing position |

---

## Circuit Breakers

Quad implements seven circuit breaker types, each with graduated responses.

### Breaker 1: P&L Drawdown

Monitors portfolio equity peak-to-trough drawdown.

| Tier | Drawdown | Automated Response |
|---|---|---|
| 0 | < 5% | Normal operation |
| 1 | 5% - 10% | Reduce position sizes by 50%, increase min confidence |
| 2 | 10% - 15% | Pause new entries, maintain existing positions |
| 3 | > 15% | Emergency shutdown, close all positions at market |

### Breaker 2: Daily Loss

Triggers when single-day loss exceeds configured threshold.

| Tier | Daily Loss | Response |
|---|---|---|
| 1 | 5% - 10% | Block new entries until next trading day |
| 2 | > 10% | Emergency: force-close all positions, investigate |

### Breaker 3: Consecutive Losses

Triggers after N consecutive losing trades.

| Threshold | Response |
|---|---|
| 3 losses | Block new entries, cooldown required |
| 5 losses | Emergency: close all positions, manual restart |

### Breaker 4: Position Growth

Detects unusual position growth that could concentrate risk.

| Condition | Response |
|---|---|
| 50% position growth in 24h | Alert, cap position increases |
| 100% growth in 24h | Block new entries to this symbol |

### Breaker 5: Liquidation Cascade

Monitors for cascade risk when positions approach liquidation simultaneously.

| Condition | Response |
|---|---|
| Any position within 10% of liquidation | Warn, suggest corrective action |
| Multiple positions within 10% of liquidation | Pause all trading, evaluate forced closes |
| Any position within 5% of liquidation | Emergency: close position at market |

### Breaker 6: Funding Rate Spike

Monitors funding rate changes that could indicate market stress.

| Condition | Response |
|---|---|
| Funding rate change > 0.2% in any 8h period | Warn, avoid new positions with unfavorable rates |
| Sustained > 0.5% for 24h | Pause entries, evaluate closing funded positions |

### Breaker 7: Volatility

Detects sudden volatility changes via mark price movements.

| Condition | Response |
|---|---|
| 50% volatility increase in 24h | Warn, reduce position sizes |
| 100%+ volatility increase | Trip: pause new entries, tighten stops |

### Circuit Breaker Recovery

| Breaker | Auto-Recoverable? | Recovery |
|---|---|---|
| P&L Drawdown (Tier 1-2) | Yes | Cooldown period + condition clear |
| P&L Drawdown (Tier 3) | No | Manual restart required |
| Daily Loss | No | Manual reset or next UTC day |
| Consecutive Losses | No | Manual reset required |
| Position Growth | Yes | Resets after growth stabilizes |
| Liquidation Cascade | No | Manual intervention required |
| Funding Rate Spike | Yes | Normalizes on rate decline |
| Volatility | Yes | Normalizes on volatility decline |

---

## Stop-Loss and Take-Profit

### Stop-Loss Strategies

| Type | Description | Best For |
|---|---|---|
| **Fixed Loss** | Close position if loss exceeds fixed USDT amount | Simple, all strategies |
| **Trailing Stop** | Adjust stop upward as position becomes profitable | Let winners run |
| **Volatility-Adjusted** | Widen stops during high volatility, tighten during low | Adaptive sizing |

### Configuration

```yaml
risk:
  stop_loss:
    enabled: true
    type: fixed
    fixed_loss_per_contract: 100
```

### Take-Profit Strategies

| Type | Description |
|---|---|
| **Fixed PnL** | Close when profit reaches target USDT amount |
| **Percentage** | Close at N% of position value in profit |
| **Mark Price Target** | Close when mark price reaches target level |

### Configuration

```yaml
risk:
  take_profit:
    enabled: true
    target_pnl_percent: 50
    target_pnl_fixed: 200
```

---

## Kill Switch

The kill switch provides an emergency mechanism to immediately halt all trading.

### Trigger Conditions

| # | Condition | Description |
|---|---|---|
| 1 | Circuit Breaker Tier 3 (P&L Drawdown) | Portfolio drawdown exceeds 15% |
| 2 | Manual Command | User runs `quad stop --emergency` or Telegram /kill |
| 3 | Critical API Errors | Repeated auth failures or invalid responses |
| 4 | System Error | Unhandled exception, database corruption |

### Shutdown Procedure

When the kill switch is triggered:

```
Step 1: STOP new decision cycle immediately
Step 2: Log EMERGENCY state with reason and timestamp
Step 3: For each open position:
  3a. Cancel all open orders
  3b. Submit market order to close position
  3c. Wait for fill confirmation
  3d. Log closure with final PnL
Step 4: Close all WebSocket connections
Step 5: Set state to EMERGENCY
Step 6: Log emergency shutdown complete
```

### Recovery

After emergency shutdown:
1. Investigate and resolve the root cause
2. Manually clear the EMERGENCY state
3. Run `quad start` to resume

---

## Position Sizing

### Leverage-Adjusted Position Sizing

Position sizing in futures considers leverage as a force multiplier for both gains and losses.

### Base Position Size

```
base_size = (portfolio_value × risk_per_trade) / (entry_price × leverage)
```

Where:
- `risk_per_trade`: Configurable % of portfolio to risk (default 2%)
- `entry_price`: Current mark price
- `leverage`: Configured leverage for this position

### Position Sizing Adjustments

| Factor | Adjustment | Rationale |
|---|---|---|
| **Liquidation Distance** | Tight (<20%): -50% size | Higher risk of forced close |
| **Funding Rate** | Unfavorable (>0.01%): -30% size | Additional holding cost |
| **Liquidity** | Low volume: -30% size | Slippage and exit difficulty |
| **Correlation** | High with existing: -20% size | Concentration risk |
| **Volatility** | High (>50% change): -30% size | Gap risk and stop-loss slippage |
| **Win Streak** | After N wins: -10% per win (min 50%) | Mean reversion protection |
| **Loss Streak** | After N losses: -15% per loss (min 25%) | Capital preservation |

### Minimum Position Size Check

All orders are validated against the exchange's minimum notional and quantity requirements for the specific symbol. Orders below min notional are rejected before submission.

---

## Risk Parameter Configuration

All risk parameters are configured in `config.local.yaml` or via `quad config set`:

```yaml
risk:
  max_positions: 5
  max_portfolio_risk: 0.02
  max_daily_loss: 0.05
  max_drawdown: 0.15
  min_distance_to_liquidation_pct: 0.20
  max_funding_rate_cost: 0.001
  max_leverage: 10
  max_position_concentration: 0.4
  max_correlation: 0.7
```

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
