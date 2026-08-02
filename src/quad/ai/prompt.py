"""Structured prompt builder for AI trading decisions.

Builds system and user prompts for Groq LLM analysis from market context
and computed technical indicators.  Uses compact representations to stay
within token limits while preserving decision-critical information.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

import structlog

from quad.ai.context import MarketContext
from quad.config.schema import AiConfig
from quad.types.domain import Account, Position
from quad.types.market import FundingRate

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_TEMPLATE = """You are a professional futures trading AI for Binance Futures. Your role is to analyze market data and recommend trades.

## Core Principles
1. **Capital preservation first** — never risk more than is justified by the setup.
2. **Trade with the trend** — trend following and momentum are your primary edge.
3. **Respect funding rates** — high positive funding (longs paying shorts) suggests crowding and potential reversal; high negative suggests the opposite.
4. **Manage leverage carefully** — use lower leverage in volatile or ranging markets; higher leverage only in strong, clear trends.
5. **Risk management first** — always check liquidation distance, funding costs, and stop-loss viability.
6. **Market regime awareness** — trending markets favour trend following; ranging markets favour mean reversion or grid trading; volatile markets demand wider stops and lower size.

## Output Format
You MUST respond with valid JSON only. No markdown, no explanation outside the JSON block.

{{
  "reasoning": "Brief explanation of market conditions and decision logic",
  "action": "ENTER" | "EXIT" | "HOLD" | "adjust_stop" | "reduce_position",
  "side": "buy" or "sell" (use buy for opening long or closing short; use sell for opening short or closing long),
  "contract": "BTCUSDT" or null,
  "quantity": 0.001-10 or null,
  "order_type": "MARKET" or "LIMIT",
  "limit_price": 0.0 or null (limit price; use null for market orders),
  "strategy": "trend_following" | null,
  "confidence": 0.0-1.0,
  "risk_checks": {{
    "position_size_ok": true/false,
    "portfolio_risk_ok": true/false,
    "concentration_ok": true/false,
    "max_drawdown_ok": true/false,
    "circuit_breakers_ok": true/false,
    "daily_loss_ok": true/false
  }}
}}

## Position Management Rules
- Always check liquidation distance before opening. If price can move against you by more than {max_liquidation_pct}% of the distance to liquidation, reduce size or skip.
- Factor funding rate into position cost: {funding_period_hours}h funding at {funding_rate_example}% annualises to ~{funding_annual_example}%. High cumulative funding cost can erode profits.
- Use market orders for entry when speed matters; use limit orders for tight entries when there's no rush.
- Prefer limit orders for take-profit and stop-loss placements."""


# ============================================================================
# Helpers
# ============================================================================


def _format_account_summary(account: Account | None) -> str:
    """Format account summary as a compact string."""
    if account is None:
        return "Account data unavailable"

    total = float(account.total_usdt)
    lines = [
        f"Total Value: ${total:,.2f} USDT",
    ]
    if account.balances:
        for asset, balance in sorted(account.balances.items()):
            # `balance` is a Balance dataclass (has .total), not a raw number.
            # Use its .total (free + locked) value; degrade gracefully if the
            # slot was populated with a raw number or string instead.
            raw = getattr(balance, "total", balance)
            try:
                val = float(raw)
            except (TypeError, ValueError):
                val = 0.0
            if val > 0:
                lines.append(f"  {asset}: {val}")
    return "\n".join(lines)


def _format_positions(positions: list[Position]) -> str:
    """Format open positions as a compact table."""
    if not positions:
        return "No open positions."

    lines = [f"{'Symbol':<12} {'Side':<8} {'Size':<10} {'Entry':<12} {'Mark':<12} {'Liq':<12} {'PnL%':<10} {'Funding':<10}"]
    lines.append("-" * 86)
    for p in positions:
        pnl_pct = (
            (float(p.current_price) - float(p.entry_price)) / float(p.entry_price) * 100 * (1 if p.side == "LONG" else -1)
        ) if p.entry_price and float(p.entry_price) > 0 else 0.0
        lines.append(
            f"{p.symbol:<12} {p.side:<8} {float(p.quantity):<10.4f} "
            f"{float(p.entry_price):<12,.2f} {float(p.current_price):<12,.2f} "
            f"{float(p.liquidation_price or 0):<12,.2f} {pnl_pct:<+10.2f}% {float(p.funding_paid or 0):<+10.4f}"
        )
    return "\n".join(lines)


def _format_funding_rates(rates: dict[str, FundingRate]) -> str:
    """Format current funding rates as a compact table."""
    if not rates:
        return "No funding rate data available."

    lines = [f"{'Symbol':<12} {'Rate':<12} {'Next Funding':<20} {'Mark Price':<14} {'Index Price':<14}"]
    lines.append("-" * 72)
    for symbol, fr in sorted(rates.items()):
        rate_pct = float(fr.funding_rate) * 100
        next_funding = time.strftime(
            "%m-%d %H:%M",
            time.gmtime(fr.next_funding_time / 1000) if fr.next_funding_time else time.gmtime(0),
        ) if fr.next_funding_time else "N/A"
        lines.append(
            f"{symbol:<12} {rate_pct:<+11.6f}% {next_funding:<20} "
            f"{float(fr.mark_price):<14,.2f} {float(fr.index_price):<14,.2f}"
        )
    return "\n".join(lines)


def _format_order_book(book: dict, symbol: str, depth: int = 5) -> str:
    """Format top-of-book bids and asks as a compact table."""
    bids = book.get("bids", [])[:depth]
    asks = book.get("asks", [])[:depth]

    lines = [f"Order Book {symbol}:"]
    lines.append(f"{'Bids':>20} {'':<5} {'Asks':<20}")
    lines.append("-" * 45)

    max_rows = max(len(bids), len(asks))
    for i in range(max_rows):
        bid_str = ""
        ask_str = ""
        if i < len(bids):
            bid_price, bid_qty = bids[i][0], bids[i][1]
            bid_str = f"{float(bid_price):<10,.2f} {float(bid_qty):<8.4f}"
        if i < len(asks):
            ask_price, ask_qty = asks[i][0], asks[i][1]
            ask_str = f"{float(ask_price):<10,.2f} {float(ask_qty):<8.4f}"
        lines.append(f"{bid_str:>25} {ask_str:<20}")

    return "\n".join(lines)


def _format_indicators_summary(indicators: dict[str, Any]) -> str:
    """Format computed technical indicators into a compact summary block."""
    lines: list[str] = []

    # Trend
    lines.append(f"Trend: {indicators.get('trend_regime', 'unknown')}")
    lines.append(
        f"  EMA20={indicators.get('trend_ema_20', 'N/A')} "
        f"EMA50={indicators.get('trend_ema_50', 'N/A')} "
        f"ADX={indicators.get('trend_adx', 'N/A')}"
    )
    lines.append(
        f"  +DI={indicators.get('trend_plus_di', 'N/A')} "
        f"-DI={indicators.get('trend_minus_di', 'N/A')}"
    )

    # Momentum
    rsi = indicators.get("momentum_rsi_14", "N/A")
    rsi_regime = indicators.get("momentum_rsi_regime", "")
    lines.append(
        f"RSI(14)={rsi} ({rsi_regime}) "
        f"MACD={indicators.get('momentum_macd', 'N/A')} "
        f"Signal={indicators.get('momentum_macd_signal', 'N/A')} "
        f"Cross={indicators.get('momentum_macd_cross', 'N/A')}"
    )
    stoch_k = indicators.get("momentum_stoch_k", "N/A")
    stoch_d = indicators.get("momentum_stoch_d", "N/A")
    lines.append(f"Stoch %K={stoch_k} %D={stoch_d}")

    # Volatility
    lines.append(
        f"BB Width={indicators.get('volatility_bb_width_pct', 'N/A')}% "
        f"BB Position={indicators.get('volatility_bb_position', 'N/A')} "
        f"ATR={indicators.get('volatility_atr_14', 'N/A')} "
        f"ATR%={indicators.get('volatility_atr_pct', 'N/A')}%"
    )

    # Volume
    vol_ratio = indicators.get("volume_sma_20_ratio", "N/A")
    obv_trend = indicators.get("volume_obv_trend", "N/A")
    lines.append(
        f"Vol/SMA20={vol_ratio} "
        f"OBV={obv_trend} "
        f"Spike={'YES' if indicators.get('volume_spike') else 'no'}"
    )

    # Patterns
    patterns = [k for k, v in indicators.items() if k.startswith("pattern_") and v]
    if patterns:
        pattern_names = [p.replace("pattern_", "") for p in patterns]
        lines.append(f"Patterns: {', '.join(pattern_names)}")
    else:
        lines.append("Patterns: none detected")

    # Price action
    lines.append(
        f"Price: ${indicators.get('price_current', 'N/A'):,} "
        f"Change: {indicators.get('price_change_pct', 'N/A')}% "
        f"Range: {indicators.get('price_range_pct', 'N/A')}%"
    )

    return "\n".join(lines)


def _format_compact_candles(
    candles: list, max_candles: int = 20
) -> str:
    """Format the most recent N candles as a compact table for the prompt."""
    if not candles:
        return "No candle data available."

    recent = candles[-max_candles:]
    lines: list[str] = [
        f"Last {len(recent)} candles (oldest first):"
    ]
    lines.append(f"{'Time':<20} {'Open':<12} {'High':<12} {'Low':<12} {'Close':<12} {'Vol':<10}")
    lines.append("-" * 78)

    for c in recent:
        ts = time.strftime("%m-%d %H:%M", time.gmtime(c.timestamp / 1000))
        lines.append(
            f"{ts:<20} {float(c.open):<12,.2f} {float(c.high):<12,.2f} "
            f"{float(c.low):<12,.2f} {float(c.close):<12,.2f} {float(c.volume):<10,.2f}"
        )

    return "\n".join(lines)


# ============================================================================
# Public API
# ============================================================================


def build_trading_prompt(
    context: MarketContext,
    indicators: dict[str, dict[str, Any]],
    config: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Build the system and user prompt pair for a trading decision.

    Parameters
    ----------
    context:
        The aggregated market snapshot from ``collect_market_context``.
    indicators:
        Dict of ``{pair_timeframe_key: computed_indicators_dict}``, e.g.
        ``{"BTCUSDT_15m": {...}, "BTCUSDT_1h": {...}}``.
    config:
        Optional config dict.  Recognised keys:

        * ``ai.system_prompt_override`` — if set, replaces the default
          system prompt.

    Returns
    -------
    dict with keys ``"system"`` and ``"user"``.
    """
    ai_cfg = AiConfig.model_validate(config.get("ai", {}))
    prompt_cfg = ai_cfg.model_dump()["prompt"]

    # Build system prompt from template with config values
    system_prompt = ai_cfg.system_prompt_override or _SYSTEM_PROMPT_TEMPLATE.format(
        max_liquidation_pct=prompt_cfg.get("max_liquidation_pct"),
        funding_period_hours=prompt_cfg.get("funding_period_hours"),
        funding_rate_example=prompt_cfg.get("funding_rate_example"),
        funding_annual_example=prompt_cfg.get("funding_annual_example"),
    )

    # Order book depth and candle count from prompt config
    order_book_depth = prompt_cfg.get("order_book_depth")
    max_candles = prompt_cfg.get("max_candles")

    # Build user prompt sections
    sections: list[str] = [
        "# Market Analysis Request",
        f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(context.timestamp))}",
        "",
    ]

    # Account & Positions
    sections.append("## Account")
    sections.append(_format_account_summary(context.account))
    sections.append("")

    sections.append("## Open Positions")
    sections.append(_format_positions(context.positions))
    sections.append("")

    # Technical analysis per pair/timeframe
    sections.append("## Technical Analysis")
    for key in sorted(indicators.keys()):
        pair, tf = key.split("_", 1)
        ind = indicators[key]
        if ind:
            sections.append(f"### {pair} ({tf})")
            sections.append(_format_indicators_summary(ind))
            # Compact candle data
            candle_key = key
            pair_candles = context.candles.get(candle_key, [])
            if pair_candles:
                sections.append(_format_compact_candles(pair_candles, max_candles=max_candles))
            sections.append("")

    # Market data (funding rates, order books)
    sections.append("## Market Data")
    if context.funding_rates:
        sections.append("### Funding Rates")
        sections.append(_format_funding_rates(context.funding_rates))
        sections.append("")
    for pair in sorted(context.order_books.keys()):
        book = context.order_books[pair]
        sections.append(_format_order_book(book, pair, depth=order_book_depth))
        sections.append("")

    # Risk context
    sections.append("## Risk Parameters")
    risk_cfg = config.get("risk", {})
    trading_cfg = config.get("trading", {})
    sections.append(f"Max Position Size: {risk_cfg.get('max_position_size')} units")
    sections.append(f"Max Portfolio Risk: {risk_cfg.get('max_portfolio_risk_pct')}%")
    sections.append(f"Max Daily Loss: ${risk_cfg.get('max_daily_loss_usd'):,.2f}")
    sections.append(f"Max Leverage: {trading_cfg.get('max_leverage')}x")
    min_dist = trading_cfg.get('min_distance_to_liquidation_pct', 0.1)
    sections.append(f"Min Distance to Liquidation: {min_dist * 100:.0f}%")
    max_funding = trading_cfg.get('max_funding_rate_cost', 0.01)
    sections.append(f"Max Funding Rate Cost: {max_funding * 100:.2f}%")
    sections.append(f"Max Drawdown: {risk_cfg.get('max_drawdown_pct')}%")
    sections.append("")

    sections.append("## Decision Required")
    sections.append("Based on the above data, recommend a trading action (ENTER to open, EXIT to close, HOLD to do nothing, adjust_stop, or reduce_position).")
    sections.append("Respond with valid JSON only following the specified format.")

    user_prompt = "\n".join(sections)

    return {
        "system": system_prompt,
        "user": user_prompt,
    }


# ============================================================================
# Optimisation Cycle Prompts
# ============================================================================

OPTIMIZATION_SYSTEM_PROMPT: str = """You are a futures trading strategy optimization analyst. Your job is to review recent trading performance and recommend concrete, actionable improvements.

You will receive:
1. A summary of recent trading decisions (actions taken, reasoning provided)
2. A summary of executed trades (PnL, fees, fill quality)
3. Performance metrics (portfolio value trend, drawdown, win rate)
4. Current strategy configuration (filters, thresholds, risk limits)

Your output must be valid JSON with this exact structure:
{
  "summary": {
    "period_win_rate": <number>,
    "period_pnl": <string>,
    "max_drawdown": <string>,
    "key_observation": <string>
  },
  "recommendations": [
    {
      "type": "parameter_adjustment" | "prompt_update" | "risk_threshold" | "strategy_toggle",
      "target_area": <string>,
      "current_value": <any>,
      "recommended_value": <any>,
      "rationale": <string>,
      "impact_estimate": <string>,
      "confidence": "low" | "medium" | "high"
    }
  ]
}

Focus recommendations on:
- Strategy parameters that correlate with poor outcomes (e.g., ATR multiplier too tight, ADX threshold too high, grid spacing too wide)
- Risk threshold adjustments based on drawdown history
- Prompt improvements based on recurring reasoning errors
- Strategy toggling if a strategy consistently underperforms in current conditions
"""


def build_optimization_prompt(
    decisions: list,
    trades: list,
    performance: list,
    current_config: Any,
) -> dict[str, str]:
    """Build the system and user prompts for the optimization analysis cycle.

    Parameters
    ----------
    decisions:
        List of DecisionModel instances from the lookback period.
    trades:
        List of TradeModel instances from the lookback period.
    performance:
        List of PerformanceSnapshotModel instances from the lookback period.
    current_config:
        The current QuadConfig instance.

    Returns
    -------
    dict
        With keys ``system`` and ``user``.
    """
    system = (
        current_config.ai.system_prompt_override
        or OPTIMIZATION_SYSTEM_PROMPT
    )

    # Build compact summaries
    total_decisions = len(decisions)
    executed_decisions = sum(1 for d in decisions if getattr(d, "executed", 0))
    total_trades = len(trades)
    total_pnl = sum(
        float(getattr(t, "pnl", "0") or "0") for t in trades
    )
    wins = sum(1 for t in trades if float(getattr(t, "pnl", "0") or "0") > 0)
    losses = sum(1 for t in trades if float(getattr(t, "pnl", "0") or "0") < 0)
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0.0

    # Performance trend
    if performance:
        start_val = float(performance[0].portfolio_value)
        end_val = float(performance[-1].portfolio_value)
        perf_change_pct = ((end_val - start_val) / start_val * 100) if start_val else 0.0
    else:
        perf_change_pct = 0.0

    # Strategy breakdown
    strategy_decisions = {}
    for d in decisions:
        s = getattr(d, "strategy", "unknown")
        strategy_decisions.setdefault(s, 0)
        strategy_decisions[s] += 1

    user = (
        f"## Performance Data ({len(decisions)} decisions, {total_trades} trades)\n\n"
        f"**Period Summary:**\n"
        f"- Decisions: {total_decisions} total, {executed_decisions} executed\n"
        f"- Trades: {total_trades} (W: {wins} / L: {losses})\n"
        f"- Win Rate: {win_rate:.1f}%\n"
        f"- Net PnL: ${total_pnl:.2f}\n"
        f"- Portfolio Change: {perf_change_pct:+.2f}%\n\n"
        f"**Strategy Breakdown:**\n"
    )
    for s, count in sorted(strategy_decisions.items()):
        user += f"- {s}: {count} decisions\n"

    user += (
        f"\n**Current Configuration:**\n"
        f"- Max positions: {current_config.risk.max_positions}\n"
        f"- Max daily loss: ${current_config.risk.max_daily_loss_usd:.2f}\n"
        f"- Max drawdown: {current_config.risk.max_drawdown_pct:.1f}%\n"
        f"- Max leverage: {current_config.trading.max_leverage}x\n"
        f"- AI model: {current_config.ai.model}\n"
    )

    return {"system": system, "user": user}
