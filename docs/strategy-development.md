# Strategy Development Guide

---

## Overview

Quad uses a plugin-based strategy architecture. Each strategy is a Python class that subclasses `StrategyBase` from `src/quad/strategy/base.py`. Subclassing is all that is required: `StrategyBase.__init_subclass__` calls `get_name()` and inserts the class into `StrategyBase.registry`, which `StrategyRegistry` reads.

This guide covers:
1. The `StrategyBase` ABC and its methods
2. `StrategyContext`: what data is available
3. The `Action` type and the safety semantics the execution engine enforces
4. Writing a custom strategy
5. Registering a strategy (and what the entry-point group does and does not do)
6. Backtesting a strategy
7. Best practices for futures strategy development

Quad ships with 1 default futures strategy: `trend_following`. Custom strategies register by subclassing `StrategyBase`.

---

## StrategyBase ABC

`src/quad/strategy/base.py` defines four abstract methods. Three are static (`get_name`, `get_description`, `get_params_spec`) and one is the async entry point (`evaluate`):

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal


@dataclass
class ParamSpec:
    """Specification for a single strategy parameter."""

    name: str
    type: Literal["int", "float", "decimal", "str", "bool"]
    default: Any = None
    description: str = ""
    min_value: float | None = None
    max_value: float | None = None
    required: bool = True


class StrategyBase(ABC):
    """Abstract base for all trading strategies."""

    registry: ClassVar[dict[str, type[StrategyBase]]] = {}

    def __init_subclass__(cls, **kwargs) -> None:
        if not cls.__name__.startswith("_"):
            StrategyBase._register(cls)

    @abstractmethod
    async def evaluate(self, context: StrategyContext) -> list[Action]:
        """Evaluate the strategy against the current context.

        Called once per trading cycle. Returns a list of actions.
        """
        ...

    @staticmethod
    @abstractmethod
    def get_name() -> str:
        """Unique machine-readable name, e.g. 'trend_following'."""
        ...

    @staticmethod
    @abstractmethod
    def get_description() -> str:
        """Human-readable description of this strategy."""
        ...

    @staticmethod
    @abstractmethod
    def get_params_spec() -> list[ParamSpec]:
        """The parameter specification for this strategy."""
        ...
```

There are **no** `name` / `description` properties, and no `analyze()` method. The
orchestrator, CLI, and backtest engine all call `evaluate(context)`. `get_name()`
is what keys the registry and the `strategy.<name>` config section.

### Inherited Helpers

`StrategyBase` provides helpers strategies can use instead of reimplementing them:

| Helper | Purpose |
|---|---|
| `get_param(name, default)` | Resolve a param with the chain *instance params → spec default → provided default*. |
| `hold_action(reason)` | Return a single `HOLD` action. |
| `_calculate_position_size_usd(capital, risk_pct, stop_loss_pct, max_size_usd)` | Risk-based sizing in USD. |
| `_build_tp_sl_actions(symbol, side, entry_price, capital, sl_capital_pct, tp_capital_pct, leverage, strategy_name)` | Build the `set_stop_loss` + `set_take_profit` bracket prices. Prices move by `capital_pct / leverage`, because `capital_pct` is a share of *margin*, not of price. |
| `_get_current_price(symbol, context)`, `_get_atr(symbol, context, period)` | Market-data lookups with defaults. |
| `_check_liquidation_risk(liquidation_price, mark_price, position_side, threshold_pct)` | Returns `(is_safe, distance_pct)`. |
| `_calculate_funding_cost(position_size_usd, funding_rate, hours_held)` | Projected funding cost (Bybit funds every 8h). |

### Registry Access

```python
from quad.strategy.base import StrategyRegistry

StrategyRegistry.get("ema_crossover")    # -> class or None
StrategyRegistry.list()                  # -> sorted list of registered names
StrategyRegistry.get_specs()             # -> {name: [ParamSpec, ...]}
```

`quad.strategy.factory` wraps this: `get_strategy(name, params, config)` builds an
instance, `list_strategies()` returns name/description/params metadata, and
`create_default_strategies(config)` instantiates every registered strategy whose
`strategy.<name>.enabled` is truthy.

---

## StrategyContext

`src/quad/types/strategy.py`. All fields have defaults, so the orchestrator can
build a partial context (the TradingView webhook passes `account=None`):

```python
@dataclass
class StrategyContext:
    # Account information
    account: Account | None = None
    positions: list[Position] = field(default_factory=list)
    futures_positions: list[FuturesPosition] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)

    # Market data
    futures_contracts: dict[str, FuturesContract] = field(default_factory=dict)
    funding_rates: dict[str, FundingRate] = field(default_factory=dict)
    mark_prices: dict[str, float] = field(default_factory=dict)
    underlying_price: float | None = None

    # Risk state
    risk_status: RiskStatus | None = None
    circuit_breakers: dict[str, Any] = field(default_factory=dict)

    # Configuration
    config: dict[str, Any] = field(default_factory=dict)
    strategy_params: dict[str, Any] = field(default_factory=dict)

    # Historical data access
    historical: HistoricalDataAccess | None = None
```

Note the field names: there is no `candles` or `order_books` field. Price history
comes from `historical` (`HistoricalDataAccess`), which exposes
`get_candles(symbol, start, end) -> list[dict]` and
`get_funding_rate_history(symbol, start, end) -> list[FundingRate]`.

### Key Data Types

**FuturesContract** (`src/quad/types/market.py`) — note `price_change_24h` and
`volume_24h`, not `price_change_percent_24h`:
```python
@dataclass
class FuturesContract:
    symbol: str = ""              # e.g., "BTCUSDT"
    mark_price: Decimal = Decimal(0)
    index_price: Decimal = Decimal(0)
    funding_rate: Decimal = Decimal(0)
    next_funding_time: int = 0
    volume_24h: Decimal = Decimal(0)
    open_interest: Decimal = Decimal(0)
    open_interest_value: Decimal = Decimal(0)
    last_price: Decimal = Decimal(0)
    price_change_24h: Decimal = Decimal(0)
    high_24h: Decimal = Decimal(0)
    low_24h: Decimal = Decimal(0)
    last_update: int = 0
```

**FuturesPosition** (`src/quad/types/domain.py`) — numeric fields are `float`,
not `Decimal`, and there is **no** `strategy` attribute; ownership is tracked on
the `Decision` record instead:
```python
@dataclass
class FuturesPosition:
    symbol: str = ""
    position_side: FuturesPositionSide = FuturesPositionSide.LONG  # LONG / SHORT
    size: float = 0.0
    entry_price: float = 0.0
    mark_price: float = 0.0
    liquidation_price: float = 0.0
    leverage: int = 1
    margin_type: MarginType = MarginType.ISOLATED
    margin: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    funding_paid: float = 0.0
    update_time: int = 0
```

**Action:**

Defined in `src/quad/types/risk.py`:

```python
@dataclass
class Action:
    type: ActionType = "HOLD"     # see accepted values below
    strategy: str = ""            # owning strategy name
    symbol: str = ""              # e.g. "BTCUSDT"
    quantity: Decimal = Decimal(0)
    price: Decimal | None = None
    reason: str = ""
    confidence: float = 1.0
    risk_checked: bool = False
    risk_result: RiskResult | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    contract: str = ""            # defaults to symbol in __post_init__
    side: str = ""                # BUY / SELL; defaulted per type when empty
    order_type: str = ""          # forced to STOP_MARKET / TAKE_PROFIT_MARKET
    stop_loss_price: Decimal | None = None
    take_profit_price: Decimal | None = None
```

`__post_init__` fills derived fields: `contract = contract or symbol`, and when
`side` is not supplied it defaults from the type (`ENTER` → `BUY`, `EXIT` →
`SELL`, `open_long` / `close_short` → `BUY`, `open_short` / `close_long` →
`SELL`, `set_stop_loss` / `set_take_profit` → `SELL`). It also forces
`order_type` to `STOP_MARKET` for `set_stop_loss` and `TAKE_PROFIT_MARKET` for
`set_take_profit`, because the legacy `STOP_LOSS` / `TAKE_PROFIT` types are
limit-if-triggered and the bot never sends a limit price.

### Accepted `type` Values

`ActionType` is a `Literal` of the full vocabulary. What the trading path
actually acts on:

| `type` | Handling |
|---|---|
| `ENTER` | Canonical entry. Side defaults to `BUY`; the engine derives SHORT entries from the risk/AI layer. |
| `EXIT` | Close. The engine derives the closing side from the held position. |
| `HOLD` | No action. Use `StrategyBase.hold_action()` to emit it. |
| `set_stop_loss` | Market-on-trigger protective stop. |
| `set_take_profit` | Market-on-trigger protective target. |
| `adjust_stop` | In `ActionType`; moves the stop on an existing position. |
| `reduce_position` | In `ActionType`; partial reduction. |
| `open_long` / `open_short` | **Legacy entry aliases.** `execution/engine.py` still accepts them alongside `ENTER` — they take the same entry path, including the naked-entry refusal and the bracket logic. |
| `close_long` / `close_short` | **Legacy close aliases.** Present in `ActionType`; the engine's close path keys on `EXIT`. |

`Action.risk_checked` is a legacy boolean: setting it to `True` makes the engine
synthesise `RiskResult(passed=True)` and skip the 9 gates. The safe alternative
is `risk_result`, which carries the actual `RiskResult` (with `details`) so the
engine can inspect it.

---

## Execution Safety Semantics

These are enforced in `src/quad/execution/engine.py` regardless of what a
strategy returns. A strategy that violates them produces a rejected order, not a
protected position.

1. **No naked entries.** When `risk.per_position_sl.enabled` or
   `risk.per_position_tp.enabled` is true, an `ENTER` / `open_long` / `open_short`
   whose built order request carries no stop-loss or no take-profit is refused
   fail-closed with reason `"missing TP/SL brackets"` and logged as
   `naked_entry_refused`. This is the last-mile guarantee for every path — AI
   decisions, manual runs, and TradingView alerts alike.
2. **Brackets are submitted with `risk_checked=True`.** A bracket protects a
   position that already passed the 9 gates, so it deliberately bypasses the
   pipeline. Because that skips the sizing guards too, the engine bounds the
   bracket itself: it never protects more than the entry's approved size, and it
   re-checks the result against `risk.max_position_size_usd`. If the bracket
   would breach the cap the position is left **unprotected** and
   `bracket_notional_cap_exceeded` is logged at `error` — close it manually.
3. **Quantity floor-up respects the notional cap.** A sized quantity below the
   exchange minimum is floored **up** to satisfy both `minQty` and
   `minNotional` (rounded up to `stepSize`) so the trade is not silently lost. If
   that floor-up would push the notional past `risk.max_position_size_usd`, the
   original rejection is re-raised instead — the engine will not round an order
   up and over the risk cap.
4. **Market-only execution.** Any order type that is not `STOP_MARKET` or
   `TAKE_PROFIT_MARKET` is coerced to `MARKET`, and `time_in_force` is cleared for
   the market family (Bybit returns `TIF_NOT_REQUIRED` otherwise).

---

## Writing a Custom Strategy

### Step 1: Create the strategy file

This is a complete, runnable strategy. It subclasses `StrategyBase`, implements
the four abstract methods, emits brackets alongside its entry, and uses the real
parameter names (`fast_ema` / `slow_ema`, matching
`src/quad/config/schema.py` and `trend_following.py`).

```python
# my_strategies/ema_crossover.py
import time
from decimal import Decimal
from typing import Any

from quad.strategy.base import ParamSpec, StrategyBase
from quad.types.risk import Action
from quad.types.strategy import StrategyContext

SYMBOL = "BTCUSDT"


class EmaCrossover(StrategyBase):
    """EMA crossover strategy with attached TP/SL brackets."""

    # ---- Required interface -------------------------------------------

    @staticmethod
    def get_name() -> str:
        return "ema_crossover"

    @staticmethod
    def get_description() -> str:
        return "Trade EMA crossovers with attached TP/SL brackets"

    @staticmethod
    def get_params_spec() -> list[ParamSpec]:
        return [
            ParamSpec(
                name="fast_ema", type="int", default=9,
                description="Fast EMA period",
            ),
            ParamSpec(
                name="slow_ema", type="int", default=21,
                description="Slow EMA period",
            ),
            ParamSpec(
                name="atr_period", type="int", default=14,
                description="ATR calculation period",
            ),
            ParamSpec(
                name="trade_capital_usd", type="int", default=5,
                description="Capital per trade in USD",
            ),
            ParamSpec(
                name="confidence_default", type="float", default=0.7,
                description="Confidence for a plain crossover",
            ),
            ParamSpec(
                name="confidence_high", type="float", default=0.9,
                description="Confidence for a fresh crossover",
            ),
        ]

    async def evaluate(self, context: StrategyContext) -> list[Action]:
        """Enter on an EMA crossover; exits are owned by the brackets."""
        symbol = SYMBOL
        name = self.get_name()

        # 1. A position for this symbol means we are done — the attached
        #    brackets manage the exit, not this strategy.
        if [p for p in (context.futures_positions or [])
                if p.symbol == symbol and p.size > 0]:
            return self.hold_action(f"{symbol}: position open, brackets manage the exit")

        # 2. Price and indicators.
        price = self._get_current_price(symbol, context)
        if price is None:
            return self.hold_action(f"No mark price for {symbol}")

        ema = await self._ema_cross(context, symbol)
        if ema is None:
            return self.hold_action(f"No EMA data for {symbol}")

        crossed_up = ema["fast"] > ema["slow"] and ema["prev_fast"] <= ema["prev_slow"]
        crossed_dn = ema["fast"] < ema["slow"] and ema["prev_fast"] >= ema["prev_slow"]
        if not (crossed_up or crossed_dn):
            return self.hold_action(
                f"No entry for {symbol}: fast={ema['fast']:.2f} slow={ema['slow']:.2f}"
            )

        # 3. Sizing and brackets, both read from the single source of truth
        #    (the risk / trading config sections).
        risk_cfg = self._config.get("risk", {})
        sl_pct = float(risk_cfg.get("per_position_sl", {}).get("capital_pct", 30.0))
        tp_pct = float(risk_cfg.get("per_position_tp", {}).get("capital_pct", 50.0))
        leverage = int(self._config.get("trading", {}).get("leverage", 10))
        max_pos_usd = float(risk_cfg.get("max_position_size_usd", 10000))
        capital = float(self.get_param("trade_capital_usd", 5))

        size = self._calculate_position_size_usd(
            capital=capital,
            risk_pct=0.02,
            stop_loss_pct=sl_pct / 100.0,
            max_size_usd=max_pos_usd,
        )
        if size <= 0:
            return self.hold_action(f"Sized to zero for {symbol}")

        # 4. Build the entry. capital_pct is a share of *margin*, so the
        #    helper divides by leverage to get a price distance.
        side = "LONG" if crossed_up else "SHORT"
        action = Action(
            type="ENTER",
            strategy=name,
            symbol=symbol,
            side="BUY" if crossed_up else "SELL",
            quantity=Decimal(str(size)),
            reason=(
                f"EMA crossover for {symbol}: fast={ema['fast']:.2f} "
                f"slow={ema['slow']:.2f}"
            ),
            confidence=float(self.get_param("confidence_high", 0.9)),
        )

        # 5. Mandatory protection: fold the bracket prices onto the entry
        #    action itself. Never emit a bare entry — the execution engine
        #    refuses one with `naked_entry_refused`.
        for bracket in self._build_tp_sl_actions(
            symbol=symbol,
            side=side,
            entry_price=float(price),
            capital=capital,
            sl_capital_pct=sl_pct,
            tp_capital_pct=tp_pct,
            leverage=float(leverage),
            strategy_name=name,
        ):
            if bracket.type == "set_stop_loss":
                action.stop_loss_price = bracket.stop_loss_price
            elif bracket.type == "set_take_profit":
                action.take_profit_price = bracket.take_profit_price

        return [action]

    # ---- Helpers ------------------------------------------------------

    async def _ema_cross(
        self, context: StrategyContext, symbol: str
    ) -> dict[str, float] | None:
        """Latest and previous fast/slow EMA, or None if unavailable."""
        if context.historical is None:
            return None
        slow = int(self.get_param("slow_ema", 21))
        lookback = slow * 3
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - lookback * 60_000  # 1-minute candles
        try:
            candles = await context.historical.get_candles(symbol, start_ms, now_ms)
        except Exception:
            return None

        closes: list[float] = [
            float(c["close"]) for c in candles if c.get("close")
        ]
        if len(closes) < slow + 2:
            return None

        def series(period: int) -> list[float]:
            k = 2.0 / (period + 1)
            out = [closes[0]]
            for c in closes[1:]:
                out.append((c - out[-1]) * k + out[-1])
            return out

        f, s = series(int(self.get_param("fast_ema", 9))), series(slow)
        return {"fast": f[-1], "slow": s[-1], "prev_fast": f[-2], "prev_slow": s[-2]}
```

There is no `on_position_update()`, `on_tick()`, `required_capital()`, or
`validate_params()` in the interface. Parameter validation happens in
`StrategyBase.__init__` / `_validate_params()` against `get_params_spec()`: a
required spec entry with no default and no supplied value raises `ValueError`, and
a wrong type raises `TypeError`.

### Step 2: Auto-Registration

Importing the module is all it takes:

```python
# Anywhere imported before create_default_strategies() runs
import my_strategies.ema_crossover  # noqa: F401
```

The `__init_subclass__` hook calls `get_name()` and writes
`StrategyBase.registry["ema_crossover"] = EmaCrossover`. Verify with:

```bash
quad strategies
```

Because registration is import-driven, a module that is never imported will not
appear in the registry.

### Step 3: Configure the strategy

Add strategy parameters to `config/config.local.yaml` under the strategy's
registry name:

```yaml
strategy:
  ema_crossover:
    enabled: true
    fast_ema: 9
    slow_ema: 21
    atr_period: 14
    trade_capital_usd: 5
    confidence_default: 0.7
    confidence_high: 0.9
```

`enabled: true` is required — `create_default_strategies()` skips any strategy
whose params dict lacks it. The `strategy` section is typed as
`dict[str, Any]` in `QuadConfig`, so an unknown strategy name validates.

The bracket percentages are *not* strategy params in this example: they are read
from `risk.per_position_sl.capital_pct` and `risk.per_position_tp.capital_pct`,
so there is a single source of truth for them.

### Step 4: Run the strategy

```bash
# Inspect the registered strategy and its parameters
quad strategies
quad evaluate ema_crossover

# Execute the signal (dry run by default)
quad execute ema_crossover

# Start the full trading loop
quad start
```

`quad execute --live --yes` refuses to run while `exchange.testnet` is still
`true`.

### Plugin Packaging: What Actually Works

`pyproject.toml` declares an **empty** `[project.entry-points."quad.strategies"]`
group:

```toml
# Strategy plugins register here (pip-installable third-party strategies):
# [project.entry-points."quad.strategies"]
# my_pkg = "my_pkg.strategy:MyStrategy"
[project.entry-points."quad.strategies"]
```

Be honest about what this means today: **nothing in the code reads it.** There is
no `importlib.metadata` usage anywhere in `src/`, so an installed package that
declares an entry point is not auto-discovered. Today the group is a *reserved
hook* — a placeholder for a future discovery mechanism, not a wired one.

Strategies register purely by subclassing `StrategyBase`. To use a third-party
package today, make sure its module is imported before
`create_default_strategies()` runs (for example from your own entry module).

---

## Backtesting a Strategy

### CLI Status

The CLI accepts the command but fails honestly — it is not wired up yet:

```bash
quad backtest ema_crossover --days 30
# ❌ Backtesting is not implemented yet.
#   Required: a configured DatabaseManager with historical futures data,
#   a strategy instance, and an underlying symbol.
```

### Engine API

`BacktestEngine` in `src/quad/backtesting/engine.py` is the intended entry point:

```python
from quad.backtesting.engine import BacktestEngine
from quad.strategy.factory import get_strategy

engine = BacktestEngine(
    strategy=get_strategy("ema_crossover", config=config),
    db_manager=db,               # None runs in stub mode
    config={
        "starting_capital": Decimal("1000"),
        "commission_pct": Decimal("0.0006"),
        "slippage_pct": Decimal("0.0002"),
        "max_trades_per_day": 5,
    },
)
result = await engine.run(
    underlying="BTCUSDT",
    start=datetime(2024, 1, 1, tzinfo=timezone.utc),
    end=datetime(2024, 6, 30, tzinfo=timezone.utc),
    interval_hours=1,
)
```

All four config keys are required — the engine raises `KeyError` on a missing one
rather than substituting a default. The engine calls the strategy's
`evaluate(context)` at each step, so a strategy that works live works in a
backtest without modification.

### Backtest Data

`BacktestEngine` loads historical futures data from the database. Sources:

1. **Bybit historical downloads** -- kline data from Bybit V5 (`GET /v5/market/kline`, `category=linear`)
2. **Database snapshots** -- previously stored candle data
3. **Live data captures** -- gathered during dry-run sessions

---

## Strategy Design Patterns

These are sketches of *logic*, not of order placement. The trade path is
market-only: anything that is not a `STOP_MARKET` / `TAKE_PROFIT_MARKET` bracket
is coerced to `MARKET`, so resting limit orders (grid, market-making) cannot be
expressed as written and would need a different execution path.

### Pattern 1: Trend Following

```python
# Follow direction using moving average crossovers
if fast_ema crosses above slow_ema → ENTER (side derived from the crossover)
if fast_ema crosses below slow_ema → ENTER (opposite side)
exit is owned by the attached SL/TP brackets, not by an explicit close
```

### Pattern 2: Grid Trading

```python
# Not executable as written: resting limit orders are not supported.
# Would require a new execution path beyond the market-only trade path.
for level in grid_levels:
    if level <= current_price:
        SELL at market
    else:
        BUY at market
```

### Pattern 3: Mean Reversion

```python
# Trade bounces from oversold/overbought levels
if RSI < 30 and price at lower Bollinger Band → ENTER long
if RSI > 70 and price at upper Bollinger Band → ENTER short
Exit via the bracket once RSI returns to 50
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
# Not executable as written: resting limit orders are not supported.
# Provide liquidity with two-sided resting orders at bid - spread/2 /
# ask + spread/2 — this requires a limit-order execution path.
```

### Pattern 6: Funding Rate Arbitrage

```python
# Trade based on funding rate premium/discount
if funding_rate is very positive (perpetual > spot):
    ENTER short (collect positive funding)
if funding_rate is very negative:
    ENTER long (pay negative funding, benefit from contango)
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

Use the positions view to verify position metrics:

```bash
quad positions
```

---

## Strategy Checklist

Before deploying a new strategy to live trading, verify:

| # | Check | How to Verify |
|---|---|---|
| 1 | `evaluate()` returns valid `Action`s | Run in dry-run, check `quad decisions` |
| 2 | Every entry carries TP **and** SL | Otherwise the engine rejects it with `naked_entry_refused` |
| 3 | `get_params_spec()` matches `strategy.<name>` config keys | Instantiate via `get_strategy(name, params)`; a missing required param raises `ValueError`, a wrong type raises `TypeError` |
| 4 | Size respects `risk.max_position_size_usd` | Check the sizing against the actual margin requirement |
| 5 | Backtest shows positive expectancy | `BacktestEngine.run(...)` over 6+ months of data |
| 6 | Strategy handles no-opportunity gracefully | Verify a HOLD action when there is no good setup |
| 7 | Risk gates don't permanently block | Check `quad risk` for PASS on all gates |
| 8 | Strategy works with testnet | Run 1+ week on testnet (`exchange.testnet: true`) |
