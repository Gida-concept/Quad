"""Market analysis functions powered by Groq AI.

Provides high-level analysis functions that take live market data
(option chains, greeks, price action) and return AI-generated insights
suitable for display in Telegram or logging.
"""

from __future__ import annotations

import time
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
    "You are a professional futures trading analyst. "
    "Analyse the provided market data and give concise, actionable insights. "
    "Focus on: funding rate sentiment (positive = longs paying shorts, "
    "indicating bullish crowding; negative = shorts paying longs), market "
    "regime (trending/ranging/volatile), order book imbalance (bid/ask "
    "volume ratio), and any notable liquidation clusters. "
    "Keep responses under 300 words. "
    "Use plain language suitable for a Telegram message."
)

# ============================================================================
# Public analysis functions
# ============================================================================


async def analyze_market(
    client: GroqClient,
    symbol: str,
    mark_price: Decimal | None,
    funding_rate: Any | None,
    order_book: dict[str, Any] | None,
    positions: list[Any] | None = None,
) -> str:
    """Analyse current futures market conditions.

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
    order_book:
        Raw order book dict with ``bids`` and ``asks``, or ``None``.
    positions:
        Optional list of open ``Position`` objects for context.

    Returns
    -------
    str
        AI-generated market analysis text.
    """
    # Build a compact market summary from futures data
    market_summary = _summarize_market(symbol, mark_price, funding_rate, order_book)

    position_summary = ""
    if positions:
        pos_count = len(positions)
        pos_pnl = sum(
            float(getattr(p, "unrealized_pnl", Decimal(0))) for p in positions
        )
        position_summary = (
            f"\nOpen positions: {pos_count}, unrealised PnL: ${pos_pnl:+,.2f}"
        )

    user_prompt = (
        f"Analyse {symbol} futures market:\n"
        f"Mark price: ${float(mark_price or 0):,.2f}\n"
        f"{market_summary}"
        f"{position_summary}"
    )

    logger.info(
        "ai_analyze_market",
        symbol=symbol,
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


def _summarize_market(
    symbol: str,
    mark_price: Decimal | None,
    funding_rate: Any | None,
    order_book: dict[str, Any] | None,
) -> str:
    """Build a compact text summary from futures market data.

    Extracts key metrics: funding rate sentiment, order book imbalance,
    volume activity, and technical regime.
    """
    lines: list[str] = []
    spot = float(mark_price or 0)
    lines.append(f"Mark Price: ${spot:,.2f}")

    # Current price and 24h context
    if spot > 0:
        lines.append(f"Price context: current at ${spot:,.2f}")

    # Funding rate analysis
    if funding_rate is not None:
        fr = float(getattr(funding_rate, "funding_rate", 0))
        fr_pct = fr * 100
        if fr > 0.0001:
            sentiment = "positive (longs paying shorts — bullish crowding)"
        elif fr < -0.0001:
            sentiment = "negative (shorts paying longs — bearish crowding)"
        else:
            sentiment = "neutral"
        lines.append(f"Funding Rate: {fr_pct:+.6f}% ({sentiment})")
        next_time = getattr(funding_rate, "next_funding_time", 0)
        if next_time and int(next_time) > 0:
            remaining = int(next_time) - int(time.time() * 1000)
            hours_remaining = max(0, remaining) / 3600000
            lines.append(f"  Next funding in ~{hours_remaining:.1f}h")
    else:
        lines.append("Funding Rate: N/A")

    # Order book imbalance
    if order_book is not None:
        bids = order_book.get("bids", [])
        asks = order_book.get("asks", [])
        if bids and asks:
            bid_vol = sum(float(b[1]) for b in bids[:10])
            ask_vol = sum(float(a[1]) for a in asks[:10])
            ratio = bid_vol / ask_vol if ask_vol > 0 else 0
            bid_price = float(bids[0][0])
            ask_price = float(asks[0][0])
            spread_pct = (
                (ask_price - bid_price) / bid_price * 100 if bid_price > 0 else 0
            )
            lines.append(
                f"Order Book: spread={spread_pct:.4f}%, bid/ask vol ratio={ratio:.2f}"
            )
            lines.append(f"  Top bid: ${bid_price:,.2f} ({float(bids[0][1]):.4f})")
            lines.append(f"  Top ask: ${ask_price:,.2f} ({float(asks[0][1]):.4f})")
    else:
        lines.append("Order Book: N/A")

    return "\n".join(lines)
