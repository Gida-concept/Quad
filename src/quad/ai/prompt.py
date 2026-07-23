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
from quad.types.market import OptionContract

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are a professional options trading AI for Binance Options. Your role is to analyze market data and recommend trades.

## Core Principles
1. **Capital preservation first** — never risk more than is justified by the setup.
2. **Trade with the trend** — prefer put selling in uptrends, call selling in downtrends.
3. **Volatility is your friend** — sell when IV is elevated, buy when IV is depressed.
4. **Always be delta-neutral-ish** — manage overall portfolio delta exposure.
5. **No hero trades** — if the setup isn't clear, recommend HOLD.

## Output Format
You MUST respond with valid JSON only. No markdown, no explanation outside the JSON block.

{
  "reasoning": "Brief explanation of market conditions and decision logic",
  "action": "ENTER" | "EXIT" | "HOLD",
  "contract": "SYMBOL_YYMMDD_OPTIONTYPE_STRIKE" or null,
  "side": "BUY" | "SELL" | null,
  "quantity": 1-5 or null,
  "order_type": "LIMIT" | "MARKET" | null,
  "limit_price": 0.0 or null,
  "strategy": "cash_secured_put" | "covered_call" | "iron_condor" | "straddle" | "strangle" | "vertical_spread" | null,
  "confidence": 0.0-1.0,
  "risk_checks": {
    "position_size_ok": true/false,
    "portfolio_risk_ok": true/false,
    "concentration_ok": true/false,
    "max_drawdown_ok": true/false,
    "circuit_breakers_ok": true/false,
    "daily_loss_ok": true/false
  }
}

## Contract Selection Rules
- Prefer expiry 7-45 days out (DTE).
- Prefer options with open_interest > 50.
- For cash-secured puts: choose a strike ~0.25 delta (OTM).
- For covered calls: choose a strike ~0.30 delta (OTM).
- Never recommend a contract that is not in the provided option chain.
- Use the exact contract symbol from the chain data.
- Limit price must be between bid and ask (or near mid) when provided."""


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
        for asset, qty in sorted(account.balances.items()):
            val = float(qty)
            if val > 0:
                lines.append(f"  {asset}: {val}")
    return "\n".join(lines)


def _format_positions(positions: list[Position]) -> str:
    """Format open positions as a compact table."""
    if not positions:
        return "No open positions."

    lines = [f"{'Contract':<30} {'Side':<6} {'Qty':<5} {'Entry':<12} {'PnL':<12} {'DTE':<5}"]
    lines.append("-" * 80)
    for p in positions:
        pnl = float(p.unrealized_pnl) if p.unrealized_pnl else 0.0
        entry = float(p.entry_price) if p.entry_price else 0.0
        sym = p.contract_symbol[:28] if len(p.contract_symbol) > 28 else p.contract_symbol
        dte = p.days_to_expiry if p.days_to_expiry else "?"
        lines.append(
            f"{sym:<30} {p.side:<6} {p.quantity:<5} "
            f"{entry:<12,.2f} {pnl:<+12,.2f} {dte!s:<5}"
        )
    return "\n".join(lines)


def _format_option_chain(
    chain: list[OptionContract], max_entries: int = 20
) -> str:
    """Format option chain as a compact, parseable table.

    Shows only the most liquid strikes (highest open_interest) for each type.
    """
    if not chain:
        return "No options data available."

    # Separate calls and puts
    calls = [c for c in chain if c.option_type == "CALL"]
    puts = [c for c in chain if c.option_type == "PUT"]

    # Sort by open_interest descending, take top N
    calls.sort(key=lambda x: x.open_interest if x.open_interest else 0, reverse=True)
    puts.sort(key=lambda x: x.open_interest if x.open_interest else 0, reverse=True)

    lines = [f"{'Strike':<10} {'Type':<6} {'Bid':<10} {'Ask':<10} {'IV':<8} {'Delta':<8} {'OI':<8}"]
    lines.append("-" * 70)

    for opt in (calls[:max_entries // 2] + puts[:max_entries // 2]):
        bid = float(opt.bid) if opt.bid else 0.0
        ask = float(opt.ask) if opt.ask else 0.0
        iv = round(float(opt.iv) * 100, 1) if opt.iv else 0.0
        delta = round(float(opt.delta), 3) if opt.delta else 0.0
        oi = int(opt.open_interest) if opt.open_interest else 0
        strike = float(opt.strike)

        lines.append(
            f"{strike:<10,.2f} {opt.option_type:<6} {bid:<10,.2f} {ask:<10,.2f} "
            f"{iv:<7.1f}% {delta:<+8.3f} {oi:<8}"
        )

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
    cfg = config or {}
    ai_cfg = AiConfig.model_validate(cfg.get("ai", {}))
    system_prompt = ai_cfg.system_prompt_override or _SYSTEM_PROMPT

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
                sections.append(_format_compact_candles(pair_candles, max_candles=20))
            sections.append("")

    # Option chains
    sections.append("## Option Chains")
    for pair in sorted(context.option_chains.keys()):
        chain = context.option_chains[pair]
        underlying_price = indicators.get(f"{pair}_1h", {}).get("price_current", "N/A")
        sections.append(f"### {pair} (Underlying: ${underlying_price})")
        sections.append(_format_option_chain(chain, max_entries=20))
        sections.append("")

    # Risk context
    sections.append("## Risk Parameters")
    risk_cfg = cfg.get("risk", {})
    sections.append(f"Max Position Size: {risk_cfg.get('max_position_size', 5)} contracts")
    sections.append(f"Max Portfolio Risk: {risk_cfg.get('max_portfolio_risk_pct', 10)}%")
    sections.append(f"Max Daily Loss: ${risk_cfg.get('max_daily_loss_usd', 500):,.2f}")
    sections.append(f"Max Delta Exposure: {risk_cfg.get('max_delta_exposure', 10)}")
    sections.append(f"Max Drawdown: {risk_cfg.get('max_drawdown_pct', 20)}%")
    sections.append("")

    sections.append("## Decision Required")
    sections.append("Based on the above data, recommend a trading action (ENTER, EXIT, or HOLD).")
    sections.append("Respond with valid JSON only following the specified format.")

    user_prompt = "\n".join(sections)

    return {
        "system": system_prompt,
        "user": user_prompt,
    }
