# Bot Parameter Configuration

Configure the Quad Futures bot for: **50x leverage**, **$5/trade minimum**, **2:1 TP/SL ratio** (SL=30% of capital per trade, TP=50% of capital per trade).

---

## Phase 1 — Fix Leverage Caps (50x)

Remove the 3 hardcoded 10x overrides that silently defeat the YAML and schema defaults.

| # | File | Current | Change |
|---|------|---------|--------|
| 1 | `src/quad/risk/manager.py` line 29 | `DEFAULT_RISK_CONFIG["max_leverage"]: 10` | → `50` |
| 2 | `src/quad/risk/sizing.py` line 23 | `_DEFAULTS["max_leverage"]: 10` | → `50` |
| 3 | `src/quad/risk/gates.py` — leverage gate defaults | `max_leverage: 10` | → `50` |
| 4 | `config/config.local.yaml` | (add) `trading.max_leverage: 50` | — |

**Verify:** Schema already allows 1–125x (`ge=1, le=125`), no change needed there.

---

## Phase 2 — Fix Minimum Position Size ($5)

Remove the hardcoded $10 floor.

| # | File | Current | Change |
|---|------|---------|--------|
| 1 | `src/quad/risk/sizing.py` line 24 | `_DEFAULTS["min_position_size_usd"]: 10` | → `5` |
| 2 | `src/quad/risk/manager.py` line 46 | `DEFAULT_RISK_CONFIG["min_position_size_usd"]: 10` | → `5` |
| 3 | `config/config.local.yaml` | (add) `risk.min_position_size_usd: 5` | — |

**Caveat:** $5 × 50x = $250 notional. Check Binance minimum notional per symbol (typically $100 for altcoins, higher for BTC/ETH). May need `minNotional` filter check in the execution engine.

---

## Phase 3 — TP/SL Action Types & Domain

Add native TP/SL support to the Action system so strategies can place exchange-level stop-loss and take-profit orders.

| # | File | Change |
|---|------|--------|
| 1 | `src/quad/types/risk.py` | Add `stop_loss_price: Decimal = Decimal("0")` and `take_profit_price: Decimal = Decimal("0")` optional fields to `Action` dataclass. Add `"set_stop_loss"` and `"set_take_profit"` to the `ActionType` Literal. |
| 2 | `src/quad/execution/engine.py` — `_build_request()` | Handle `set_stop_loss` → `OrderRequest(type="STOP_LOSS", stop_price=sl_price, reduce_only=True, working_type="MARK_PRICE", price_protect=True)`. Handle `set_take_profit` → `OrderRequest(type="TAKE_PROFIT", stop_price=tp_price, reduce_only=True)`. |
| 3 | `src/quad/execution/engine.py` — `execute()` | After opening a position (open_long/open_short), if the Action has TP/SL prices set, also submit the stop-loss and take-profit orders as bracket orders. |
| 4 | `src/quad/execution/lifecycle.py` | Track TP/SL order IDs alongside the main position. Cancel TP/SL on position close. |
| 5 | `src/quad/execution/tracker.py` | Register TP/SL orders in position state so they're discoverable via `/orders`. |

---

## Phase 4 — Strategy TP/SL Generation

Each strategy should emit `set_stop_loss` / `set_take_profit` actions when it opens a position.

| # | File | Change |
|---|------|--------|
| 1 | `src/quad/strategy/base.py` — `StrategyBase` | Add `_build_tp_sl_actions(symbol, side, entry_price, capital, sl_capital_pct, tp_capital_pct)` → generates `set_stop_loss` and `set_take_profit` actions with prices computed from capital percentages. SL price = for LONG: `entry * (1 - sl_capital_pct/100 / leverage)`, TP price = `entry * (1 + tp_capital_pct/100 / leverage)`. |
| 2 | `src/quad/strategy/dca_bot.py` | Replace simulated close actions with `set_stop_loss`/`set_take_profit` actions. Use `_build_tp_sl_actions()` on entry. Remove in-cycle TP/SL polling from `evaluate()`. |
| 3 | `src/quad/strategy/trend_following.py` | On entry, emit `set_stop_loss` (ATR-based, as before) **and** `set_take_profit` (using `_build_tp_sl_actions()`). Keep trailing stop logic but only adjust the stop-loss price. |
| 4 | `src/quad/strategy/mean_reversion.py` | Replace hardcoded 5% stop-loss with `_build_tp_sl_actions()`. Keep BB middle band as additional take-profit signal. |
| 5 | `src/quad/strategy/grid_trading.py` | Wire up the existing `take_profit_pct` param to emit take-profit on filled grid levels. |
| 6 | `src/quad/strategy/market_making.py` | Minimal: add stop-loss to accumulated inventory positions. |

---

## Phase 5 — Config Schema & Defaults

Add the per-position TP/SL config section and strategy capital config.

| # | File | Change |
|---|------|--------|
| 1 | `src/quad/config/schema.py` | Add `PerPositionSLConfig(enabled=True, type=Literal["fixed","trailing"], capital_pct=30.0)`, `PerPositionTPConfig(enabled=True, type=Literal["fixed"], capital_pct=50.0)`. Add `trading.trade_capital_usd: int = 5` to TradingConfig. Add `per_position_tp_sl: PerPositionTPConfig` and `per_position_sl: PerPositionSLConfig` to RiskConfig. |
| 2 | `config/config.default.yaml` | Add under `risk:`: `per_position: { stop_loss: { enabled: true, type: "fixed", capital_pct: 30.0 }, take_profit: { enabled: true, type: "fixed", capital_pct: 50.0 } }`. Add `trading.trade_capital_usd: 5`. |
| 3 | `config/config.local.yaml` | Add overrides: `trading.max_leverage: 50`, `risk.min_position_size_usd: 5`, `risk.per_position.*` sections. |

---

## Phase 6 — Position Sizing Update

Make the sizer TP/SL-aware and fix strategy capital.

| # | File | Change |
|---|------|--------|
| 1 | `src/quad/risk/sizing.py` | Add `_max_size_from_tp_sl(capital, leverage, sl_pct, tp_pct)` → computes maximum allowable size given 2:1 ratio: `max_size = capital * tp_pct / (sl_pct * leverage)`. Incorporate as an additional cap in `compute_size()`. |
| 2 | `src/quad/strategy/base.py` — `_calculate_position_size_usd()` | Remove hardcoded `capital` parameter. Read `trade_capital_usd` from `self._config`. Default fallback: `self._config.get("trading",{}).get("trade_capital_usd", 10000)`. |
| 3 | `src/quad/strategy/trend_following.py` | Remove `capital=10000` hardcode, use config `trade_capital_usd`. |
| 4 | `src/quad/strategy/mean_reversion.py` | Same. |

---

## Phase 7 — Trend Following Short Support

Enable short entries in the trend-following strategy.

| # | File | Change |
|---|------|--------|
| 1 | `src/quad/strategy/trend_following.py` — `_check_entry()` | Add short branch: when EMA death cross + ADX above threshold → `open_short`. Currently only handles `bias == "long"` and falls through to hold for short. |

---

## Phase 8 — Verification & docs

| # | File | Change |
|---|------|--------|
| 1 | (verify) | `python -c "from quad.types.risk import Action; print([f.name for f in Action.__dataclass_fields__.values()])"` — should show `stop_loss_price` and `take_profit_price`. |
| 2 | (verify) | `python -c "from quad.strategy.base import StrategyBase; print('OK')"` — import passes. |
| 3 | (verify) | `python -c "from quad.execution.engine import ExecutionEngine; print('OK')"` — import passes. |
| 4 | (verify) | `python -c "from quad.config.schema import QuadConfig; print('OK')"` — schema validates new fields. |
| 5 | `config/config.local.yaml` | Write final version with all overrides (50x, $5 min, 2:1 TP/SL). |
| 6 | `.env.example` | Optionally add `QUAD_RISK_MAX_LEVERAGE`, `QUAD_RISK_MIN_POSITION_SIZE_USD` env vars. |
| 7 | `src/quad/config/manager.py` | Optionally add ENV_VAR_MAP entries for the new risk env vars. |

---

## File Change Summary

```
config/
  config.default.yaml        — add per_position tp/sl, trade_capital_usd
  config.local.yaml          — add overrides (50x, $5, tp/sl)
  .env.example               — optional env vars

src/quad/
  types/risk.py              — Action: new action types + tp/sl price fields
  config/schema.py           — PerPositionSLConfig, PerPositionTPConfig, trade_capital_usd
  risk/manager.py            — DEFAULT_RISK_CONFIG: max_leverage 50, min_pos 5
  risk/sizing.py             — _DEFAULTS: max_leverage 50, min_pos 5; add tp/sl-aware sizing
  risk/gates.py              — _DEFAULTS: max_leverage 50
  strategy/base.py           — _build_tp_sl_actions(), fix _calculate_position_size_usd
  strategy/trend_following.py — add short branch, add tp, use config capital
  strategy/mean_reversion.py  — replace hardcoded sl, use config capital
  strategy/dca_bot.py         — emit native tp/sl orders
  strategy/grid_trading.py    — wire take_profit_pct
  strategy/market_making.py   — add tp/sl for inventory
  execution/engine.py         — _build_request handles set_stop_loss/set_take_profit
  execution/lifecycle.py      — track tp/sl order ids
  execution/tracker.py        — register tp/sl in position state
  config/manager.py           — optional env var map entries
```
