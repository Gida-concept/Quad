"""Market analysis functions powered by Groq AI.

Provides high-level analysis functions that take live market data
(option chains, greeks, price action) and return AI-generated insights
suitable for display in Telegram or logging.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import structlog

from .groq import GroqClient

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_MARKET_ANALYSIS_SYSTEM = (
    "You are a professional options trading analyst. "
    "Analyse the provided market data and give concise, actionable insights. "
    "Focus on: implied volatility levels, put/call skew, unusual activity, "
    "Greeks positioning, and notable DTE clusters. "
    "Keep responses under 300 words. "
    "Use plain language suitable for a Telegram message."
)

# ============================================================================
# Public analysis functions
# ============================================================================


async def analyze_market(
    client: GroqClient,
    underlying: str,
    underlying_price: Decimal | None,
    option_chain: list[Any],
    positions: list[Any] | None = None,
) -> str:
    """Analyse current market conditions from option chain data.

    Parameters
    ----------
    client:
        Initialised ``GroqClient`` instance.
    underlying:
        Underlying symbol, e.g. ``"BTCUSDT"``.
    underlying_price:
        Current price of the underlying, or ``None``.
    option_chain:
        List of ``OptionContract`` objects (or compatible dicts).
    positions:
        Optional list of open ``Position`` objects for context.

    Returns
    -------
    str
        AI-generated market analysis text.
    """
    # Build a compact market summary from the option chain data
    chain_summary = _summarise_chain(option_chain, underlying_price)

    position_summary = ""
    if positions:
        pos_count = len(positions)
        pos_pnl = sum(
            float(getattr(p, "unrealized_pnl", Decimal("0")))
            for p in positions
        )
        position_summary = (
            f"\nOpen positions: {pos_count}, "
            f"unrealised PnL: ${pos_pnl:+,.2f}"
        )

    user_prompt = (
        f"Analyse {underlying} options market:\n"
        f"Underlying price: ${float(underlying_price or 0):,.2f}\n"
        f"{chain_summary}"
        f"{position_summary}"
    )

    logger.info(
        "ai_analyze_market",
        underlying=underlying,
        chain_size=len(option_chain),
        positions=len(positions) if positions else 0,
    )

    try:
        result = await client.chat(
            system=_MARKET_ANALYSIS_SYSTEM,
            user=user_prompt,
            temperature=0.3,
        )
        return result
    except Exception as exc:
        logger.warning("ai_analyze_market_failed", error=str(exc))
        return "⚠️ Market analysis unavailable (AI service error)."


# ============================================================================
# Internal helpers
# ============================================================================


def _summarise_chain(
    chain: list[Any],
    underlying_price: Decimal | None,
) -> str:
    """Build a compact text summary from an option chain list.

    Extracts key metrics: ATM IV, put/call skew, volume clusters,
    and extreme strikes.
    """
    if not chain:
        return "Option chain: empty."

    calls = [c for c in chain if getattr(c, "option_type", "") == "CALL"]
    puts = [c for c in chain if getattr(c, "option_type", "") == "PUT"]

    lines: list[str] = []

    # Underlying context
    spot = float(underlying_price or 0)

    # ATM implied volatility (closest to spot)
    atm_ivs = []
    for c in chain:
        strike = float(getattr(c, "strike", 0))
        iv = float(getattr(c, "implied_volatility", 0))
        if spot > 0 and abs(strike - spot) / spot < 0.05 and iv > 0:
            atm_ivs.append(iv)

    if atm_ivs:
        avg_atm_iv = sum(atm_ivs) / len(atm_ivs)
        if spot > 0:
            lines.append(f"ATM IV: {avg_atm_iv:.1%} ({len(atm_ivs)} contracts)")
            lines.append(f"ATM IV range: {min(atm_ivs):.1%} - {max(atm_ivs):.1%}")

    # Put / Call IV skew (OTM put IV vs OTM call IV)
    otm_puts = [
        float(p.implied_volatility)
        for p in puts
        if float(p.strike) < spot * 0.95 and float(p.implied_volatility) > 0
    ]
    otm_calls = [
        float(c.implied_volatility)
        for c in calls
        if float(c.strike) > spot * 1.05 and float(c.implied_volatility) > 0
    ]

    if otm_puts and otm_calls:
        put_iv = sum(otm_puts) / len(otm_puts)
        call_iv = sum(otm_calls) / len(otm_calls)
        skew = (put_iv - call_iv) / call_iv * 100 if call_iv > 0 else 0
        lines.append(f"Put IV: {put_iv:.1%} | Call IV: {call_iv:.1%} | Skew: {skew:+.1f}%")

    # Total contracts in chain
    lines.append(f"Contracts: {len(calls)} calls, {len(puts)} puts")

    # Greeks snapshot (average absolute delta for near-term)
    near_chain = [
        c
        for c in chain
        if float(getattr(c, "delta", 0)) != 0
    ]
    if near_chain:
        avg_delta = sum(abs(float(c.delta)) for c in near_chain) / len(near_chain)
        avg_gamma = sum(abs(float(c.gamma)) for c in near_chain) / len(near_chain)
        lines.append(f"Avg |delta|: {avg_delta:.4f} | Avg |gamma|: {avg_gamma:.6f}")

    return "\n".join(lines)
