# Strategy Development Guide

---

## Overview

Quad uses a plugin-based strategy architecture. Each strategy is a Python class that implements the `Strategy` ABC. Strategies auto-register via `StrategyBase.__init_subclass__` when they inherit from `Strategy`, making them extensible without modifying core code.

This guide covers:
1. The Strategy ABC and its methods
2. StrategyContext: what data is available
3. Writing a custom strategy
4. Packaging and registering a strategy plugin
5. Backtesting a strategy
6. Best practices for futures strategy development

Quad ships with 1 default futures strategy: `trend_following`. Custom strategies auto-register via `StrategyBase.__init_subclass__` when they inherit from `StrategyBase`.

---

## Strategy ABC

All strategies inherit from `Strategy` in `src/quad/strategy/base.py`:

```python
from abc import ABC, abstractmethod
from typing import Optional
from decimal import Decimal

class StrategyBase(ABC):
    """Abstract base for trading strategies."""

    @staticmethod
    @abstractmethod
    def get_name() -> str:
        """Return the unique machine-readable name, e.g. 'trend_following'."""
        ...

    @staticmethod
    @abstractmethod
    def get_description() -> str:
        """Return a human-readable description of this strategy."""
        ...

    @staticmethod
    @abstractmethod
    def get_params_spec() -> list[ParamSpec]:
        """Return the parameter specification (name, type, default, description)."""
        ...

    @abstractmethod
    async def evaluate(self, context: StrategyContext) -> list[Action]:
        """Evaluate the strategy against the current context.

        Called once per trading cycle. Returns a list of actions
        (ENTER, EXIT, HOLD, adjust_stop, reduce_position).
        """
        ...
```

---

## StrategyContext

The context object provides all data a strategy needs for decision-making:

```python
@dataclass
class StrategyContext:
    """Context provided to strategies during evaluation."""

    # Account information
    account: Account | None
    positions: list[Position]
    futures_positions: list[Position]
    orders: list[Order]

    # Market data
    funding_rates: dict[str, FundingRate]
    mark_prices: dict[str, float]
    candles: dict[str, list]
    order_books: dict[str, dict]

    # Risk state
    risk_status: RiskStatus | None

    # Configuration
    config: dict
    strategy_params: dict

    # Historical data access
    historical: HistoricalDataAccess | None
```

### Key Data Types

**FuturesContract:**
```python
@dataclass
class FuturesContract:
    symbol: str           # e.g., "BTCUSDT"
    mark_price: Decimal
    index_price: Decimal
    funding_rate: float
    next_funding_time: int  # timestamp
    last_price: Decimal
    volume: int
    open_interest: int
    high_24h: Decimal
    low_24h: Decimal
    price_change_percent_24h: float
```

**Position:**
```python
@dataclass
class Position:
    symbol: str           # e.g., "BTCUSDT"
    side: str             # "LONG", "SHORT", or "BOTH" for hedge mode
    quantity: Decimal
    entry_price: Decimal
    mark_price: Decimal
    liquidation_price: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    # Strategy tracking
    strategy: str         # Strategy that owns this position
```

**Action:**
```python
@dataclass
class Action:
    type: str             # "ENTER", "EXIT", "HOLD", "adjust_stop", "reduce_position"
    strategy: str         # Strategy name
    symbol: str           # e.g., "BTCUSDT"
    quantity: Decimal
    price: Optional[Decimal]
    reason: str
    confidence: float     # 0.0 to 1.0
    side: str             # "BUY" or "SELL"
    order_type: str       # "LIMIT", "MARKET", etc.
    risk_checked: bool
    metadata: dict
```

---

## Writing a Custom Strategy

### Step 1: Create the strategy file

```python
# my_strategies/ema_crossover.py
from decimal import Decimal
from typing import Optional
from quad.strategy.base import Strategy, StrategyContext
from quad.types import Action, Position, FuturesContract, Tick

class EmaCrossover(Strategy):
    """EMA crossover strategy with ATR-based stops."""

    @property
    def name(self) -> str:
        return "ema_crossover"

    @property
    def description(self) -> str:
        return "Trade EMA crossovers with ATR-based position sizing and stops"

    async def analyze(self, context: StrategyContext) -> list[Action]:
        """Check for EMA crossover signals."""
        # Get historical prices (last 50 bars)
        candles = await context.historical.get_klines(
            "BTCUSDT", interval="1h", limit=50
        )
        closes = [c.close for c in candles]

        # Calculate EMAs
        ema_fast = self._ema(closes, context.strategy_params.get("ema_fast", 9))
        ema_slow = self._ema(closes, context.strategy_params.get("ema_slow", 21))

        current_price = context.mark_prices.get("BTCUSDT", Decimal("0"))
        atr = self._atr(candles, context.strategy_params.get("atr_period", 14))

        # Check existing position
        existing = [p for p in context.futures_positions
                    if p.symbol == "BTCUSDT" and p.strategy == self.name]

        if existing:
            pos = existing[0]
            # Check stop loss
            stop_distance = atr * Decimal(str(context.strategy_params.get("stop_loss_atr", 2.0)))
            if pos.side == "LONG" and current_price <= pos.entry_price - stop_distance:
                return [Action(
                    type="close_long",
                    strategy=self.name,
                    symbol="BTCUSDT",
                    quantity=pos.quantity,
                    price=None,
                    reason=f"Stop loss hit: ATR={atr:.2f}"
                )]
            # Check take profit
            tp_distance = atr * Decimal(str(context.strategy_params.get("take_profit_atr", 4.0)))
            if pos.side == "LONG" and current_price >= pos.entry_price + tp_distance:
                return [Action(
                    type="close_long",
                    strategy=self.name,
                    symbol="BTCUSDT",
                    quantity=pos.quantity,
                    price=None,
                    reason="Take profit target reached"
                )]
            return [Action(type="hold", strategy=self.name, symbol="BTCUSDT",
                          quantity=Decimal("0"), reason="Holding position")]

        # Entry signals
        if ema_fast > ema_slow and ema_fast[-2] <= ema_slow[-2]:
            # Golden cross - go LONG
            position_size = Decimal("0.5")  # configured elsewhere
            return [Action(
                type="open_long",
                strategy=self.name,
                symbol="BTCUSDT",
                quantity=position_size,
                price=None,
                reason=f"EMA crossover: fast={ema_fast:.2f} > slow={ema_slow:.2f}",
                confidence=0.75,
                risk_checked=False,
                metadata={}
            )]
        elif ema_fast < ema_slow and ema_fast[-2] >= ema_slow[-2]:
            # Death cross - go SHORT
            position_size = Decimal("0.3")
            return [Action(
                type="open_short",
                strategy=self.name,
                symbol="BTCUSDT",
                quantity=position_size,
                price=None,
                reason=f"EMA death cross: fast={ema_fast:.2f} < slow={ema_slow:.2f}",
                confidence=0.65,
                risk_checked=False,
                metadata={}
            )]

        return [Action(type="hold", strategy=self.name, symbol="BTCUSDT",
                      quantity=Decimal("0"), reason="No signal")]

    def _ema(self, prices, period):
        """Simple EMA calculation."""
        multiplier = 2 / (period + 1)
        ema = [float(prices[0])]
        for price in prices[1:]:
            ema.append((float(price) - ema[-1]) * multiplier + ema[-1])
        return ema

    # Other required methods omitted for brevity
    async def on_position_update(self, position, context): return None
    async def on_tick(self, tick, context): pass
    def required_capital(self, params): return Decimal("1000")
    def validate_params(self, params): return []
```

### Step 2: Auto-Registration

Quad strategies auto-register via `StrategyBase.__init_subclass__`. Simply import your strategy module and it is automatically registered:

```python
# In your strategy module
from quad.strategy.base import Strategy

class EmaCrossover(Strategy):
    ...
```

The `__init_subclass__` hook in `StrategyBase` automatically adds the class to the global strategy registry on import.

### Step 3: Configure the strategy

Add strategy parameters to `config/config.local.yaml`:

```yaml
strategy:
  ema_crossover:
    enabled: true
    ema_fast: 9
    ema_slow: 21
    atr_period: 14
    atr_multiplier: 2.0
    stop_loss_atr: 2.0
    take_profit_atr: 4.0
```

### Step 4: Run the strategy

```bash
# List available strategies (should include your new one)
quad strategies

# Set as active strategy
quad strategy set ema_crossover

# Start trading
quad start

# Or test it in dry-run mode first
quad start --dry-run
```

---

## Backtesting a Strategy

### Running a Backtest

```bash
# Basic backtest
quad backtest --strategy ema_crossover

# With date range and symbol
quad backtest --strategy ema_crossover \
  --symbol BTCUSDT --start 2024-01-01 --end 2024-06-30

# Generate HTML report
quad backtest --strategy ema_crossover --report html
```

### Backtest Output

```
Backtest Results: ema_crossover
════════════════════════════════
Period:       2024-01-01 to 2024-06-30 (181 days)
Symbol:       BTCUSDT

Performance:
  Total Trades:    24
  Win Rate:        70.8% (17/24)
  Profit Factor:   2.45
  Total PnL:      +1,245.00 USDT
  Max Drawdown:   3.2%
  Sharpe Ratio:   1.85
  Avg Hold Time:  3.2 days
```

### Backtest Data

Backtests use historical futures kline data stored in the market data cache. Data can be loaded from:

1. **Bybit historical downloads** -- CSV kline data downloaded from Bybit V5 API
2. **Database snapshots** -- Previously stored candle data
3. **Live data captures** -- Gathered during paper trading sessions

---

## Strategy Design Patterns

### Pattern 1: Trend Following

```python
# Follow direction using moving average crossovers
if ema_fast crosses above ema_slow → open_long
if ema_fast crosses below ema_slow → open_short
if price hits ATR-based stop → close_position
```

### Pattern 2: Grid Trading

```python
# Place limit buy/sell orders at fixed price intervals
for level in grid_levels:
    if level <= current_price:
        place SELL limit at level
    else:
        place BUY limit at level
Adjust grid when price moves outside range
```

### Pattern 3: Mean Reversion

```python
# Trade bounces from oversold/overbought levels
if RSI < 30 and price at lower Bollinger Band → open_long
if RSI > 70 and price at upper Bollinger Band → open_short
Exit when RSI returns to 50
```

### Pattern 4: DCA (Dollar-Cost Average)

```python
# Enter initial position, add on dips
enter initial position at current price
for each price drop of N%:
    add additional position (smaller size)
Set take profit at N% from average entry
```

### Pattern 5: Market Making

```python
# Provide liquidity with two-sided limit orders
place BUY limit at bid - spread/2
place SELL limit at ask + spread/2
Adjust prices when filled or market moves
```

### Pattern 6: Funding Rate Arbitrage

```python
# Trade based on funding rate premium/discount
if funding_rate is very positive (perpetual > spot):
    open_short (collect positive funding)
if funding_rate is very negative:
    open_long (pay negative funding, benefit from contango)
Close when funding normalizes
```

### Pattern 7: Stop Management

```python
# Adjust stops as positions become profitable
if unrealized_pnl > trail_activation_threshold:
    activate trailing stop at trail_distance
if liquidation_distance < min_safe_distance:
    reduce position size or add margin
```

---

## Testing Tips

### Testnet First

Always test new strategies in dry-run or testnet mode:

```bash
# Dry-run mode (simulates orders)
quad start --dry-run

# Testnet with dry_run off (real order placement on testnet, no live funds)
# Set BYBIT_TESTNET=true and QUAD_DRY_RUN=false in .env
quad start
```

### Verify Actions

Check the decision log to verify your strategy is producing expected actions:

```bash
quad decisions
```

### Check Position Metrics

Use the position detail view to verify position metrics:

```bash
quad position <id>
```

---

## Strategy Checklist

Before deploying a new strategy to live trading, verify:

| # | Check | How to Verify |
|---|---|---|
| 1 | `analyze()` returns valid Actions | Run in dry-run, check `quad decisions` |
| 2 | `validate_params()` catches bad config | Set invalid params, verify error list |
| 3 | `required_capital()` returns reasonable value | Compare with actual margin requirements |
| 4 | `on_position_update()` triggers correctly | Simulate price changes, verify exit |
| 5 | Backtest shows positive expectancy | Run 6+ months of backtest data |
| 6 | Strategy handles no-opportunity gracefully | Verify HOLD action when no good setup |
| 7 | Risk gates don't permanently block | Check `quad risk` for PASS on all gates |
| 8 | Strategy works with testnet | Run 1+ week on testnet |
