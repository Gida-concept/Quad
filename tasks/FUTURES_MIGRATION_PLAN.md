# Quad Futures Migration Plan

**Goal:** Migrate Quad from Binance Options trading to Binance USD-M Futures trading.
**Target API:** Binance Futures (`fapi.binance.com` + `fstream.binance.com`)
**Status:** Planned
**Date:** 2026-07-26

---

## Overview

This plan is organized into 8 sequential phases. Each phase is self-contained enough to be implemented and tested in isolation. Phases must be completed in order because each depends on the previous phase's type definitions and interfaces.

### Dependency Graph

```
Phase 1 (Domain Types + Config) ---- Phase 2 (Exchange Adapter)
                                         |
                                         v
                                   Phase 3 (Market Data)
                                         |
                              +----------+----------+
                              v          v          v
                         Phase 4    Phase 5    Phase 6
                       (Strategy)   (Risk)     (AI)
                              |          |          |
                              +----------+----------+
                                         |
                                         v
                                   Phase 7 (Persistence)
                                         |
                                         v
                                   Phase 8 (Bot & CLI)
```

### Phase Summary

| Phase | Title | Complexity | Est. Days |
|-------|-------|-----------|-----------|
| 1 | Domain Types + Config Schema | Low | 1 |
| 2 | Binance Futures API Adapter | High | 4 |
| 3 | Market Data Pipeline | Medium | 2 |
| 4 | Strategy System (5 strategies) | High | 5 |
| 5 | Risk System | Medium | 2 |
| 6 | AI System | Medium | 2 |
| 7 | Persistence | Low | 1 |
| 8 | Bot & CLI | Medium | 2 |

**Total estimated effort:** ~19 working days

---

## Phase 1 -- Foundation (Domain Types + Config Schema)

**Complexity:** Low
**Dependencies:** None
**Goal:** Swap all data models from options domain to futures domain. Everything else depends on these types.

### 1.1 `src/quad/types/domain.py` -- Position, Order, Account models

**Changes:**
- Remove all options-specific fields from Position: `greeks` (Delta, Gamma, Theta, Vega, Rho), `iv`, `option_type`, `strike`, `expiry`, `underlying_price`
- Add futures-specific fields to Position: `leverage`, `margin_type` (ISOLATED/CROSS), `position_side` (LONG/SHORT), `liquidation_price`, `initial_margin`, `maintenance_margin`, `unrealized_pnl`, `realized_pnl`, `funding_paid`
- Update Order model: Remove `reduce_only` (futures has this too, keep it), add `working_type` (MARK_PRICE/CONTRACT_PRICE), `position_side`, `price_protect`
- Update Account model: Remove `options_position_limit`, `options_position_limit_display`. Add `max_leverage`, `total_wallet_balance`, `total_margin_balance`, `available_balance`, `positions` as list of futures positions.

**New types to add:**

```python
# src/quad/types/domain.py additions

class FuturesPositionSide(Enum):
    LONG = "long"
    SHORT = "short"
    BOTH = "both"  # for one-way mode

class PositionMode(Enum):
    ONE_WAY = "one_way"
    HEDGE = "hedge"

class MarginType(Enum):
    ISOLATED = "isolated"
    CROSS = "cross"

@dataclass
class FuturesPosition:
    symbol: str
    position_side: FuturesPositionSide
    size: float  # in contract units
    entry_price: float
    mark_price: float
    liquidation_price: float
    leverage: int
    margin_type: MarginType
    margin: float
    unrealized_pnl: float
    realized_pnl: float
    funding_paid: float
    update_time: int
```

### 1.2 `src/quad/types/market.py` -- Replace OptionContract with FuturesContract

**Changes:**
- Remove `OptionContract` dataclass entirely (strike, expiry, option_type, iv, greeks, etc.)
- Add `FuturesContract` with: symbol, mark_price, index_price, funding_rate, next_funding_time, volume_24h, open_interest, open_interest_value, last_price, price_change_24h, high_24h, low_24h
- Keep `Candle` and `UnderlyingPrice` (they work for futures too)
- Add `FundingRate` dataclass: symbol, funding_rate, next_funding_time, mark_price, index_price

```python
@dataclass
class FuturesContract:
    symbol: str
    mark_price: float
    index_price: float
    funding_rate: float
    next_funding_time: int  # unix ms
    volume_24h: float
    open_interest: float
    open_interest_value: float
    last_price: float
    price_change_24h: float
    high_24h: float
    low_24h: float
    last_update: int

@dataclass
class FundingRate:
    symbol: str
    funding_rate: float  # positive = longs pay shorts
    next_funding_time: int
    mark_price: float
    index_price: float
```

### 1.3 `src/quad/types/risk.py` -- Update Action and risk types

**Changes:**
- Remove `metadata` fields that reference options (strike, expiry, option_type, greeks)
- Add futures-specific risk metadata: entry_price, liquidation_price, leverage, position_side, stop_loss, take_profit
- Add new risk result types: LiquidationRisk, FundingRateCost, LeverageCheck

```python
@dataclass
class FuturesRiskMetadata:
    symbol: str
    position_side: FuturesPositionSide
    entry_price: float
    mark_price: float
    liquidation_price: float
    leverage: int
    margin_type: MarginType
    position_size_usd: float
    distance_to_liquidation_pct: float
    funding_rate: float

# RiskAction stays similar but remove option-specific fields
@dataclass
class Action:
    action: str  # open_long, open_short, close_long, close_short, hold, adjust_stop, reduce_position
    symbol: str
    quantity: float
    reason: str
    confidence: float
    metadata: Optional[Dict] = None  # will contain FuturesRiskMetadata
```

### 1.4 `src/quad/config/schema.py` -- Remove options config, add futures config

**Changes:**
- Remove all 6 strategy param models: `CoveredCallParams`, `CashSecuredPutParams`, `IronCondorParams`, `VerticalSpreadParams`, `WheelParams`, `HedgingParams`
- Remove options-specific fields from `TradingConfig`: `preferred_expiry`, `min_dte`, `max_dte`, `target_delta`, `max_spread_width`
- Add leverage (int, default 1), margin_mode (isolated/cross), position_mode (one-way/hedge)
- Add `futures_strategies` config section with placeholder parameters for each futures strategy

```python
class TradingConfig(BaseModel):
    enabled: bool = True
    max_positions: int = 5
    max_capital_per_position: float = 0.2  # 20% of capital per position

    # Futures-specific config
    leverage: int = 1
    margin_mode: MarginType = MarginType.ISOLATED
    position_mode: PositionMode = PositionMode.ONE_WAY

    # Risk management
    max_leverage: int = 20
    min_distance_to_liquidation_pct: float = 0.2  # 20%
    max_funding_rate_cost: float = 0.001  # 0.1% per funding interval
    max_position_concentration: float = 0.4  # 40% in one symbol

    # Strategy parameters
    futures_strategies: FuturesStrategiesConfig = FuturesStrategiesConfig()

class FuturesStrategiesConfig(BaseModel):
    trend_following: TrendFollowingParams = TrendFollowingParams()
    grid_trading: GridTradingParams = GridTradingParams()
    mean_reversion: MeanReversionParams = MeanReversionParams()
    dca_bot: DCAParams = DCAParams()
    market_making: MarketMakingParams = MarketMakingParams()

class TrendFollowingParams(BaseModel):
    enabled: bool = False
    fast_ema: int = 9
    slow_ema: int = 21
    adx_period: int = 14
    adx_threshold: int = 25
    atr_period: int = 14
    atr_multiplier_stop: float = 3.0
    max_position_size_usd: float = 1000.0

# ... similar for GridTradingParams, MeanReversionParams, DCAParams, MarketMakingParams
```

### 1.5 `config/config.default.yaml` -- Mirror schema changes

**Changes:**
- Replace all options strategy sections with futures strategy sections
- Add top-level `futures` config block
- Remove options-specific config keys

---

## Phase 2 -- Exchange Adapter (Binance Futures API)

**Complexity:** High
**Dependencies:** Phase 1
**Goal:** Complete rewrite of the exchange layer to talk to Binance Futures API instead of Options API.

### 2.1 `src/quad/exchange/base.py` -- Update abstract interface

**Changes:**
- Remove abstract methods: `get_option_chain()`, `subscribe_option_prices()`, `subscribe_greeks()`, `get_option_markets()`
- Add abstract methods:

```python
# Market data (public, no auth)
@abstractmethod
async def get_funding_rate(self, symbol: str) -> FundingRate: ...
@abstractmethod
async def get_funding_rates(self) -> List[FundingRate]: ...
@abstractmethod
async def get_mark_price(self, symbol: str) -> float: ...
@abstractmethod
async def get_order_book(self, symbol: str, limit: int = 20) -> OrderBook: ...
@abstractmethod
async def get_leverage_brackets(self, symbol: str) -> List[LeverageBracket]: ...
@abstractmethod
async def get_income_history(self, symbol: str, income_type: str, limit: int = 100) -> List[IncomeRecord]: ...
@abstractmethod
async def get_open_interest(self, symbol: str) -> OpenInterest: ...

# Account management (auth required)
@abstractmethod
async def set_leverage(self, symbol: str, leverage: int) -> dict: ...
@abstractmethod
async def set_margin_mode(self, symbol: str, margin_type: str) -> dict: ...
@abstractmethod
async def set_position_mode(self, mode: str) -> dict: ...
@abstractmethod
async def get_position_mode(self) -> str: ...
@abstractmethod
async def get_futures_account(self) -> FuturesAccount: ...
@abstractmethod
async def get_futures_positions(self) -> List[FuturesPosition]: ...
```

### 2.2 `src/quad/exchange/binance.py` -- New BinanceFuturesAdapter

**This is a new file (replace the old BinanceOptionsAdapter).**

**Key implementation details:**

```python
BASE_URL = "https://fapi.binance.com"  # Futures API
WS_BASE = "wss://fstream.binance.com"   # Futures WebSocket

class BinanceFuturesAdapter(BaseExchange):
    def __init__(self, config, loop=None):
        self.api_key = config.exchange.api_key
        self.api_secret = config.exchange.api_secret
        self.session = None
        self.ws_connections = {}
        self.listen_key = None
        self._funding_rates_cache = {}

    async def _sign_request(self, method, path, params=None):
        """HMAC SHA-256 signing for authenticated endpoints."""
        timestamp = int(time.time() * 1000)
        query_string = urlencode(sorted(params.items())) if params else ""
        signature = hmac.new(
            self.api_secret.encode(),
            f"{query_string}&timestamp={timestamp}".encode(),
            hashlib.sha256
        ).hexdigest()
        # Signatures: query string + &timestamp=...&signature=...

    async def _manage_listen_key(self):
        """Futures listen key: POST /fapi/v1/listenKey, PUT every 30min, DELETE on shutdown."""
        # POST to create
        # PUT to keep alive (every 30 minutes)
        # DELETE on shutdown

    async def _process_account_update(self, data):
        """ACCOUNT_UPDATE event from user data stream.
        Contains position changes, order updates, balance changes."""

    async def _process_mark_price_update(self, data):
        """MARK_PRICE_STREAM -- continuous mark price + funding rate."""
```

**WebSocket streams to subscribe to:**

| Stream | Type | Purpose |
|--------|------|---------|
| `!miniTicker@arr` | Market data (public) | All symbols price ticker, 24h stats |
| `!forceOrder@arr` | Market data (public) | Liquidation orders |
| `!markPrice@arr@1s` | Market data (public) | Mark price + funding rate every second |
| `<symbol>@depth20@100ms` | Market data (public) | Order book snapshots |
| `<listenKey>` | User data (auth) | Account/position/order updates |

**Rate limit tracking:**
- Binance Futures uses a weight-based system (not request-per-second)
- Each endpoint has a weight (1-100)
- Max weight per minute: 2400
- Implement a sliding window counter to track rate limit usage
- Add retry with backoff when approaching limits

### 2.3 `src/quad/exchange/paper.py` -- Update PaperTradingAdapter

**Changes:**
- Simulate funding rate payments every 8 hours (at 00:00, 08:00, 16:00 UTC)
- Track liquidation price based on position size, leverage, and entry price
- `liquidation_price = entry_price - (margin / (size * leverage))` for longs
- `liquidation_price = entry_price + (margin / (size * leverage))` for shorts
- Handle leverage properly in position calculation
- Save/load positions from `paper_positions.json` (new format)
- Simulate order book with configurable spread

### 2.4 `src/quad/exchange/mock.py` -- Update MockAdapter

**Changes:**
- Return futures-shaped mock data (funding rates, mark prices, futures positions)
- Respond to futures-specific method calls
- Generate realistic mock funding rates (0.01% to 0.1% range)
- Simulate realistic liquidation prices

### 2.5 `src/quad/exchange/factory.py` -- Update factory

**Changes:**
- Remove options adapter selection logic
- Default to BinanceFuturesAdapter
- Keep PaperTradingAdapter for testing
- Ensure config drives the selection

---

## Phase 3 -- Market Data Pipeline

**Complexity:** Medium
**Dependencies:** Phase 2
**Goal:** Switch market data from option chains and Greeks to order books and funding rates.

### 3.1 `src/quad/market_data/engine.py` -- Data engine rewrite

**Changes:**
- Replace `option_chain_cache` (the central options data cache) with:
  - `order_book_cache`: `Dict[str, OrderBook]` -- top-of-book per symbol
  - `funding_rate_cache`: `Dict[str, FundingRate]` -- latest funding rates
  - `mark_price_cache`: `Dict[str, float]` -- latest mark prices
  - `ticker_cache`: `Dict[str, Ticker24h]` -- 24h ticker data
- Subscribe to continuous streams on startup:
  - All mini-tickers for `!miniTicker@arr`
  - Mark price stream for `!markPrice@arr@1s`
  - Depth streams for actively traded symbols
- Implement `get_funding_rate(symbol)` and `get_order_book(symbol)` methods
- Remove option chain refresh logic (the periodic full chain refresh)

### 3.2 `src/quad/market_data/websocket.py` -- WebSocket manager

**Changes:**
- Update stream URL from `wss://nbstream.binance.com`/`wss://vstream.binance.com` to `wss://fstream.binance.com`
- Remove combined streams for options
- Add streams:
  - `!bookTicker` -- real-time best bid/ask for all symbols
  - `!markPrice@arr@1s` -- mark price + funding rate array
  - `!ticker_1h` -- 1-hour ticker window (good for funding rate context)
  - Individual `{symbol}@kline_1m` for actively watched symbols (candle data)
- Handle `!forceOrder@arr` for liquidation monitoring
- Keep the reconnection logic, message parsing, and buffer feeding structure

### 3.3 `src/quad/market_data/cache.py` -- Cache layer

**Changes:**
- Remove option-specific caches: `option_chain_cache.py` concepts, IV surface cache
- Add:
  - `FundingRateCache`: TTL cache (8h funding intervals), stores rate + time
  - `OrderBookCache`: Shallow cache (100ms TTL for top 10 levels)
  - `MarkPriceCache`: Simple dict with timestamps
  - `OpenInterestCache`: Updated daily/hourly per symbol
- Keep most of the cache base class and eviction logic

### 3.4 `src/quad/market_data/buffers.py` -- Ring buffers

**Changes:**
- Mostly stays the same -- ring buffers for candles are still needed for technical analysis
- Change event types that feed into buffers (no more option trade events)
- Add funding rate ring buffer (track last N funding rate values for trend)

### 3.5 `src/quad/market_data/historical.py` -- Historical data fetcher

**Changes:**
- Update from options historical endpoints to futures klines API
- `GET /fapi/v1/klines` -- same parameters as options klines (almost identical format)
- Add funding rate history: `GET /fapi/v1/fundingRate`
- Add open interest history: `GET /fapi/v1/openInterestHist`
- Add top trader long/short ratio: `GET /fapi/v1/globalAccountRatio` (if available)

---

## Phase 4 -- Strategy System (The Big One)

**Complexity:** High
**Dependencies:** Phase 1, Phase 2, Phase 3
**Goal:** Replace all 6 options strategies with 5 futures strategies.

### 4.1 `src/quad/strategy/base.py` -- Update StrategyBase

**Changes:**
- Remove options helpers: `_find_by_delta()`, `_calculate_dte()`, `_iter_contracts()`, `_calculate_option_price()`, `_get_atm_strike()`
- Add futures helpers:
  - `_calculate_position_size_usd(capital, risk_pct, stop_loss_pct)` -- standard futures sizing
  - `_get_current_price(symbol)` -- latest mark price
  - `_get_atr(symbol, period=14)` -- ATR value for stop placement
  - `_check_liquidation_risk(position, mark_price)` -- returns distance to liquidation
  - `_calculate_funding_cost(symbol, position_size)` -- projected funding cost
- Update `calculate_signal()` signature to use futures data context
- Keep the registry pattern (`_registry` dict, `register()`, `create()`)

### 4.2 `src/quad/strategy/factory.py` -- Update factory

**Changes:**
- Remove CoveredCallParams, CashSecuredPutParams, IronCondorParams, VerticalSpreadParams, WheelParams, HedgingParams
- Add TrendFollowingParams, GridTradingParams, MeanReversionParams, DCAParams, MarketMakingParams
- Map strategy names to classes: `"trend_following" -> TrendFollowing`, etc.

### 4.3 `src/quad/strategy/trend_following.py` -- New

**Logic:**
- Calculate fast EMA and slow EMA
- Calculate ADX for trend strength filter
- Calculate ATR for dynamic stop loss
- Entry conditions:
  - **Long:** fast_ema > slow_ema (golden cross) AND ADX > threshold
  - **Short:** fast_ema < slow_ema (death cross) AND ADX > threshold
- Exit conditions:
  - Trailing stop based on ATR multiplier
  - Or trend reversal signal (EMA cross opposite direction)
- Position sizing: `risk_amount = capital * risk_per_trade / ATR_stop_distance`
- No overlap with grid/DCA -- pure directional trend following

### 4.4 `src/quad/strategy/grid_trading.py` -- New

**Logic:**
- Define center price (current mark price or last trade price)
- Place limit buy orders at N intervals below center (`grid_levels`)
- Place limit sell orders at N intervals above center (`grid_levels`)
- Each grid level has a take-profit order at the next level up (for buys) or down (for sells)
- Grid spacing is configurable as % of center price
- On fill: place the opposite order at the same level (e.g., buy fills, place sell at +spacing)
- Config: `grid_count` (5-20), `grid_spacing_pct` (0.1%-2%), `take_profit_pct`, `max_active_orders`
- Inventory management: reduce grid on side that increases directional exposure

### 4.5 `src/quad/strategy/mean_reversion.py` -- New

**Logic:**
- Use RSI(14) + Bollinger Bands(20, 2)
- Entry:
  - **Long:** RSI < 30 (oversold) AND price touches/is below lower Bollinger band
  - **Short:** RSI > 70 (overbought) AND price touches/is above upper Bollinger band
- Exit:
  - Price returns to middle Bollinger Band (SMA 20)
  - Or ATR-based stop hit (2-3x ATR)
  - Or opposite signal generated
- Position sizing: fixed risk per trade, scaled by distance to target (wider = smaller size)
- Avoid trading in strong trends (check ADX < 25 as filter)

### 4.6 `src/quad/strategy/dca_bot.py` -- New

**Logic:**
- Define initial entry price and direction
- Enter initial position with base size
- On subsequent drops by `entry_spacing_pct`: enter additional position
- Each entry is same size or scaled (increase position on deeper drops)
- Take profit target: close entire position when price reaches `take_profit_pct`
- Config: `max_entries` (3-10), `entry_spacing_pct` (1%-5%), `take_profit_pct` (2%-10%), `base_position_size_usd`
- Risk management: stop loss at bottom of last entry level + ATR buffer
- Can be long or short direction

### 4.7 `src/quad/strategy/market_making.py` -- New

**Logic:**
- Place both bid and ask limit orders near the top of book
- Spread = base_spread + volatility_adjustment (ATR as % of price)
- Order size = base_size * (max_inventory - current_inventory) / max_inventory
- Dynamically adjust spread width based on:
  - Volatility (wider in high vol)
  - Funding rate (skew toward the side receiving funding)
  - Inventory imbalance (move price to reduce exposure)
- Config: `base_spread_pct`, `max_inventory_usd`, `order_size_usd`, `rebalance_threshold`
- Neutral goal: earn spread + funding rate, not directional

### 4.8 Keep `hedging.py` placeholder

- Keep the file with a stub class that warns "not implemented"
- Will be filled in a future phase when hedging between perp/futures or cross-exchange is desired

---

## Phase 5 -- Risk System

**Complexity:** Medium
**Dependencies:** Phase 1, Phase 2
**Goal:** Replace options-specific risk checks with futures risk dimensions.

### 5.1 `src/quad/risk/manager.py` -- Update evaluate()

**Changes:**
- Remove Greek-based risk evaluation (delta, gamma, theta, vega exposure)
- Add futures-specific risk evaluation:
  - liquidation risk (distance to liquidation)
  - funding rate cost projection
  - leverage limit check
  - position concentration by symbol
  - daily loss check
  - drawdown check
- Return `RiskAssessment` with futures-specific fields instead of Greek fields

### 5.2 `src/quad/risk/gates.py` -- Replace gates

**Changes:**
- **REMOVE gates:** `DeltaGate`, `GammaGate`, `ThetaGate`, `VegaGate`, `ExpiryGate`
- **KEEP gates:** `MaxPositionsGate`, `PortfolioRiskGate`, `DailyLossGate`, `DrawdownGate`
- **ADD gates:**
  - `LiquidationRiskGate` -- rejects if position is within N% of liquidation (configurable threshold)
  - `FundingRateCostGate` -- calculates cost of holding position for N funding periods; rejects if > threshold
  - `LeverageLimitGate` -- rejects if effective leverage exceeds max allowed
  - `PositionConcentrationGate` -- rejects if single symbol exceeds max % of portfolio
  - `CorrelationGate` -- optional: rejects if correlated positions increase portfolio risk

```python
class LiquidationRiskGate(Gate):
    """Reject if position is too close to liquidation."""
    def evaluate(self, context: RiskContext) -> GateResult:
        for pos in context.active_positions:
            if not pos.liquidation_price:
                continue
            if pos.position_side == FuturesPositionSide.LONG:
                distance = (pos.mark_price - pos.liquidation_price) / pos.mark_price
            else:
                distance = (pos.liquidation_price - pos.mark_price) / pos.mark_price
            if distance < self.config.min_distance_to_liquidation_pct:
                return GateResult(
                    passed=False,
                    severity="critical",
                    message=f"Position {pos.symbol} only {distance:.1%} from liquidation"
                )
        return GateResult(passed=True)
```

### 5.3 `src/quad/risk/exposure.py` -- Replace Greek tracking

**Changes:**
- Remove `GreeksExposure` tracker
- Add `FuturesPositionTracker`:
  - Tracks total notional exposure per symbol
  - Tracks aggregated leverage usage
  - Monitors liquidation prices across all positions
  - Calculates portfolio-level margin utilization
  - Tracks funding rate payments over time

### 5.4 `src/quad/risk/circuit_breakers.py` -- Update breakers

**Changes:**
- Add `LiquidationCascadeBreaker`: if one position gets liquidated, automatically close correlated positions (same symbol different expiry/perp, or highly correlated altcoins)
- Add `FundingRateSpikeBreaker`: pause trading if funding rates exceed configurable threshold (e.g., > 0.1% for 3 consecutive periods). This indicates market stress.
- Add `VolatilityBreaker`: pause trading if ATR % spikes above threshold
- Keep `DailyLossBreaker` and `DrawdownBreaker` (adapt for futures)
- Remove options-specific breakers (none identified)

### 5.5 `src/quad/risk/sizing.py` -- Update position sizing

**Changes:**
- Keep Kelly formula (it works for any market)
- Adjust `max_position_size_pct` to account for leverage: actual exposure = capital * margin_pct * leverage
- Standard futures position sizing formula:
  ```
  position_size_usd = capital * risk_per_trade_pct / stop_loss_pct
  position_size_units = position_size_usd / entry_price
  ```
- Add `min_position_size_usd` check (avoids orders that would be dust)
- Add leverage-adjusted sizing: position size increases with lower risk trades

---

## Phase 6 -- AI System

**Complexity:** Medium
**Dependencies:** Phase 1, Phase 3, Phase 4
**Goal:** Update AI context collection and prompts from options to futures.

### 6.1 `src/quad/ai/context.py` -- Update context collection

**Changes:**
- Replace option chain and Greeks fetching with:
  - Funding rate data for watched symbols (rate, time to next funding)
  - Open interest data (absolute + trend)
  - Long/short ratio (if available via API)
  - Top-of-book depth (bid/ask sizes)
  - Recent liquidation data (from !forceOrder stream)
  - Technical indicators: EMA crossovers, RSI, Bollinger Bands, ATR, ADX
  - Market regime classification: trending (ADX > 25), mean-reverting (ADX < 20), volatile (high ATR %)

### 6.2 `src/quad/ai/prompt.py` -- Rewrite system prompt

**Changes:**
- Remove all options terminology: Greeks, IV smile, DTE, strike selection, option chain, implied volatility, theta decay
- Add futures-specific terminology: leverage, liquidation risk, funding rate cost, market regime
- Describe available strategies: trend following, grid trading, mean reversion, DCA, market making
- Update the "how to think" section: focus on trend direction, market regime, funding rate sentiment, risk management, not volatility arbitrage
- Describe risk assessment: liquidation distance, funding cost, position sizing

### 6.3 `src/quad/ai/analysis.py` -- Replace analysis functions

**Changes:**
- Replace options market analysis (vol surface, skew, IV rank, term structure) with:
  - Trend strength analysis (ADX value, EMA alignment)
  - Volatility regime analysis (ATR % compared to historical, Bollinger Band width)
  - Funding rate sentiment (positive/negative, sustained direction = market sentiment)
  - Market structure (support/resistance from order book and recent highs/lows)
  - Volume analysis (increasing/decreasing, comparing to average)

### 6.4 `src/quad/ai/strategist.py` -- Update strategy recommendation

**Changes:**
- Recommend among futures strategies instead of options strategies
- Logic: trend market -> trend following, ranging market -> grid or mean reversion, directional bias with dip -> DCA
- Confidence scoring based on market regime alignment
- Strategy parameter recommendations (e.g., grid spacing for ranged market)

### 6.5 `src/quad/ai/optimizer.py` -- Update self-optimization

**Changes:**
- Tune futures strategy parameters instead of options strategy parameters
- Optimization dimensions: EMA periods, ATR multipliers, grid spacing, RSI thresholds, DCA entry spacing
- Keep the backtesting evaluation and parameter search structure

### 6.6 `src/quad/ai/ta.py` -- Mostly stays

**Rationale:** Technical indicators (EMA, RSI, ATR, ADX, Bollinger Bands, MACD, etc.) are market-agnostic. They work the same for futures, spot, or options. No significant changes needed.

---

## Phase 7 -- Persistence

**Complexity:** Low
**Dependencies:** Phase 1
**Goal:** Update database models for futures data.

### 7.1 `src/quad/persistence/models.py` -- Update ORM models

**Changes:**
- Remove options-specific models/tables:
  - `OptionPosition` (merged into generic `Position`)
  - `OptionTrade` (merged into generic `Trade`)
  - `GreeksSnapshot`
  - `IVSurface`
  - `OptionChainCache`
- Update `Position` model: add `leverage`, `margin_type`, `position_side`, `liquidation_price`, `initial_margin`, `maintenance_margin`, `realized_pnl`, `unrealized_pnl`, `funding_paid`
- Update `Order` model: add `position_side`, `working_type`, `price_protect`, `avg_fill_price`
- Add new tables:
  - `FundingPayment`: id, symbol, position_id, amount, rate, funding_time
  - `LiquidationEvent`: id, symbol, position_id, amount, price, time, side
  - `FundingRateRecord`: symbol, rate, time, mark_price, index_price (for historical analysis)

### 7.2 `src/quad/persistence/repositories.py` -- Update queries

**Changes:**
- Remove options-specific query methods: `get_option_positions_by_expiry()`, `get_greeks_snapshot()`, `get_iv_surface()`
- Remove options-specific repository: `OptionRepository` or similar
- Update `PositionRepository`: add `get_open_futures_positions(symbol, side)`, `get_liquidation_risk_positions(distance_threshold)`
- Add `FundingRepository`: `save_funding_payment()`, `get_funding_history(symbol, limit)`, `get_total_funding_paid(position_id)`
- Add `LiquidationRepository`: `record_liquidation()`, `get_recent_liquidations(symbol, hours)`

### 7.3 `src/quad/persistence/database.py` -- Database manager

**Changes:**
- Mostly stays (it manages the postgres connection pool)
- Update model imports to reflect new model structure
- Add new tables to schema migration

---

## Phase 8 -- Bot & CLI

**Complexity:** Medium
**Dependencies:** Phase 1-7
**Goal:** Update user-facing interfaces for futures.

### 8.1 `src/quad/bot/commands.py` -- Update Telegram bot commands

**Changes:**
- **Remove commands:** `/chain`, `./iv` (options chain), `/greeks`, `/expiry`, `/opstra`
- **Keep/repurpose commands:**
  - `/status` -- show futures account balance, open positions, margin usage
  - `/positions` -- now shows: symbol, side, size, entry, mark, liquidation price, PnL%, funding paid
  - `/risk` -- now shows: liquidation distance per position, funding cost, leverage usage, concentration
  - `/strategies` -- now shows futures strategies with their status and parameters
  - `/performance` -- shows PnL with funding costs broken out
- **Add commands:**
  - `/funding_rate [symbol]` -- current funding rate, time to next funding, predicted rate
  - `/book [symbol]` -- top 10 bids and asks
  - `/leverage <symbol> <leverage>` -- set leverage for a symbol
  - `/position_mode <one_way|hedge>` -- switch position mode
  - `/liquidation_warnings` -- show positions with highest liquidation risk
  - `/market_regime [symbol]` -- show ADX, trend direction, volatility assessment

```python
# Example new command handlers
@bot.command("/funding_rate")
async def cmd_funding_rate(ctx):
    symbol = ctx.args[0] if ctx.args else None
    rates = await ctx.exchange.get_funding_rates()
    # Format and display
    for r in rates:
        if not symbol or r.symbol == symbol:
            next_funding = r.next_funding_time - int(time.time() * 1000)
            msg += f"{r.symbol}: {r.funding_rate:.4%} | Next: {next_funding/3600000:.1f}h\n"

@bot.command("/liquidation")
async def cmd_liquidation(ctx):
    positions = await ctx.exchange.get_futures_positions()
    for pos in sorted(positions, key=lambda p: p.liquidation_price or 0):
        if pos.size != 0:
            distance = abs(pos.mark_price - pos.liquidation_price) / pos.mark_price
            msg += f"{pos.symbol} {pos.position_side.value}: liq at {pos.liquidation_price:.2f} ({distance:.1%} away)\n"
```

### 8.2 `src/quad/bot/jobs.py` -- Update scheduled jobs

**Changes:**
- Add `funding_rate_countdown_job`: Notify before funding settlement (5 min before 00:00, 08:00, 16:00 UTC)
- Add `liquidation_warning_job`: Check all positions every 60s; alert if any within configurable distance to liquidation
- Add `funding_cost_report_job`: Daily summary of total funding paid/received
- Update existing status/health jobs for futures context

### 8.3 `src/quad/cli/app.py` -- Update CLI

**Changes:**
- Mirror the bot command changes
- Add `futures` subgroup: `quad futures position_mode`, `quad futures leverage`, `quad futures funding`
- Update `quad status` to show futures-specific info
- Update `quad positions` output format

### 8.4 Documentation updates

**Files to rewrite:**
- `docs/interface-commands.md` -- all command examples change from options to futures
- `docs/configuration.md` -- reflect new config schema and strategy parameters
- `docs/architecture.md` -- remove options-specific architecture diagrams, add futures data flow
- `README.md` -- change description from "options trading platform" to "futures trading platform"
- `docs/changelog.md` -- add major entry for futures migration

---

## Migration Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Binance Futures API compatibility changes | High | Pin API version; use stable endpoints; monitor Binance changelog |
| Funding rate surprises (negative funding costs) | Medium | Implement max_funding_rate_cost gate; add funding rate monitoring |
| Liquidation during integration testing | High | Start paper trading with minimal leverage; add liquidation alerts |
| Data loss during persistence migration | Medium | Run old and new DB in parallel; migrate with downtime window |
| Strategy bugs (wrong position sizing, flip direction) | High | All strategies start disabled; enable one at a time; paper trade first |
| RSI/Bollinger signals unreliable in crypto | Low | Use ADX filter to avoid trading sideways; validate signals on historical data |

---

## Rollback Plan

If the futures migration causes critical issues:

1. **Phase-level rollback:** Each phase is designed to be reversible via git revert
2. **Full rollback:** `git revert HEAD~N` where N is commits since migration start
3. **Configuration-based fallback:** The old options adapter code remains in git history; can be reinstated if futures adapter fails catastrophically
4. **Data rollback:** Old database tables preserved (just marked inactive); can be repopulated from backups

---

## Testing Strategy

| Phase | Testing Approach |
|-------|-----------------|
| 1 | Unit tests for new domain types; config validation tests |
| 2 | Mock adapter tests for all API methods; paper trading verification |
| 3 | Stream replay tests with recorded futures data; cache invalidation tests |
| 4 | Backtest each strategy on 6 months of futures data; paper trade 1 week |
| 5 | Unit tests for each risk gate with edge cases; liquidation distance calculation tests |
| 6 | Prompt evaluation with futures scenarios; regression on existing AI tests |
| 7 | Migration script dry-run; read/write verification for new tables |
| 8 | Manual E2E test through bot; CLI smoke tests |
