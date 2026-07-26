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
- swing_trading: EMA crossover + ADX filter. Best in trending markets with strong momentum (ADX > 25).
"""

_STRATEGIST_SYSTEM = (
    "You are a futures strategy consultant. "
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
    symbol: str,
    mark_price: Decimal | None,
    funding_rate: Any | None,
    market_regime: str | None = None,
    adx_value: float | None = None,
    atr_pct: float | None = None,
) -> str:
    """Recommend a futures trading strategy based on current market conditions.

    Parameters
    ----------
    client:
        Initialised ``GroqClient`` instance.
    symbol:
        Trading pair symbol, e.g. ``"BTCUSDT"``.
    mark_price:
        Current mark price, or ``None``.
    funding_rate:
        Current ``FundingRate`` object, or ``None``.
    market_regime:
        Market regime description, e.g. ``"trending"``, ``"ranging"``,
        ``"volatile"``, or ``None``.
    adx_value:
        Current ADX value (0-100), or ``None``.
    atr_pct:
        Current ATR as percentage of price, or ``None``.

    Returns
    -------
    str
        AI-generated strategy recommendation.
    """
    spot = float(mark_price or 0)

    user_prompt = (
        f"Recommend a strategy for {symbol}:\n"
        f"Mark Price: ${spot:,.2f}\n"
    )
    if funding_rate is not None:
        fr = float(getattr(funding_rate, "funding_rate", 0))
        user_prompt += f"Funding Rate: {fr * 100:+.6f}%\n"
    if market_regime:
        user_prompt += f"Market Regime: {market_regime}\n"
    if adx_value is not None:
        user_prompt += f"ADX: {adx_value:.1f}\n"
    if atr_pct is not None:
        user_prompt += f"ATR%: {atr_pct:.2f}%\n"

    logger.info(
        "ai_recommend_strategy",
        symbol=symbol,
        market_regime=market_regime,
        adx_value=adx_value,
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
