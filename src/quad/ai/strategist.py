"""Strategy recommendation helper using Groq AI.

Based on current market conditions (IV regime, trend, Greeks), Groq
can suggest which of the built-in strategies may be the best fit.
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
# Built-in strategy reference
# ---------------------------------------------------------------------------

_STRATEGY_CATALOG = """
Available strategies:
- covered_call: Sell OTM call options against held underlying. Best in neutral-to-slightly-bullish, low IV.
- cash_secured_put: Sell OTM put options with cash collateral. Best in neutral-to-slightly-bullish, elevated IV.
- iron_condor: Sell OTM put spread + OTM call spread. Best in low-IV, range-bound markets.
- straddle: Buy ATM call + ATM put (long volatility). Best before big moves, low IV.
- strangle: Buy OTM call + OTM put (long volatility, cheaper). Best before big moves, low IV, wider range.
- vertical_spread: Buy/sell same-expiry call or put spread. Directional bias with defined risk.
"""

_STRATEGIST_SYSTEM = (
    "You are an options strategy consultant. "
    "Given current market conditions and the available strategy catalog, "
    "recommend the most suitable strategy. "
    "Explain your reasoning briefly (under 150 words)."
    + _STRATEGY_CATALOG
)


# ============================================================================
# Public API
# ============================================================================


async def recommend_strategy(
    client: GroqClient,
    underlying: str,
    underlying_price: Decimal | None,
    iv_percentile: float | None,
    trend_description: str | None,
    option_chain_summary: str | None = None,
) -> str:
    """Recommend a trading strategy based on current market conditions.

    Parameters
    ----------
    client:
        Initialised ``GroqClient`` instance.
    underlying:
        Underlying symbol, e.g. ``"BTCUSDT"``.
    underlying_price:
        Current price of the underlying, or ``None``.
    iv_percentile:
        Current IV percentile (0.0-1.0), or ``None``.
    trend_description:
        Short description of the current trend, e.g.
        ``"uptrend, 5% gain over 7 days"``, or ``None``.
    option_chain_summary:
        Optional additional context about the option chain.

    Returns
    -------
    str
        AI-generated strategy recommendation.
    """
    spot = float(underlying_price or 0)
    iv_pct = f"{iv_percentile:.0%}" if iv_percentile is not None else "unknown"

    user_prompt = (
        f"Recommend a strategy for {underlying}:\n"
        f"Price: ${spot:,.2f}\n"
        f"IV percentile: {iv_pct}\n"
    )
    if trend_description:
        user_prompt += f"Trend: {trend_description}\n"
    if option_chain_summary:
        user_prompt += f"Chain: {option_chain_summary}\n"

    logger.info(
        "ai_recommend_strategy",
        underlying=underlying,
        iv_percentile=iv_percentile,
    )

    try:
        result = await client.chat(
            system=_STRATEGIST_SYSTEM,
            user=user_prompt,
            temperature=0.3,
            max_tokens=300,
        )
        return result
    except Exception as exc:
        logger.warning("ai_recommend_strategy_failed", error=str(exc))
        return "⚠️ Strategy recommendation unavailable (AI service error)."
