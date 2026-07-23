"""TradingView signal converter.

Converts parsed TradingView webhook alerts into trading signals that
the Quad execution engine can act on.  Maps TradingView actions
(buy/sell/exit/flat) to Quad order types and validates required fields.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import structlog

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Mapping of TradingView action values to Quad side values
_ACTION_TO_SIDE: dict[str, str] = {
    "buy": "BUY",
    "sell": "SELL",
    "exit": "CLOSE",
    "flat": "CLOSE",
    "close": "CLOSE",
    "short": "SELL",
    "long": "BUY",
}

_SIDE_NORMALISE: dict[str, str] = {
    "buy": "BUY",
    "sell": "SELL",
    "close": "CLOSE",
}


# ============================================================================
# Public types
# ============================================================================


class TradingViewSignal:
    """A structured signal derived from a TradingView alert.

    Parameters
    ----------
    symbol:
        Trading pair/option symbol.
    side:
        Order side: ``"BUY"``, ``"SELL"``, or ``"CLOSE"``.
    quantity:
        Number of contracts.
    price:
        Optional limit price.  ``None`` means market order.
    signal_type:
        Signal classification: ``"entry"``, ``"exit"``, ``"adjust"``.
    strategy_name:
        Optional name of the TradingView strategy that generated the alert.
    raw:
        The original parsed alert dict for reference.
    metadata:
        Arbitrary additional fields preserved from the alert.
    """

    def __init__(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal | None = None,
        signal_type: str = "entry",
        strategy_name: str = "tradingview",
        raw: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.symbol = symbol
        self.side = side
        self.quantity = quantity
        self.price = price
        self.signal_type = signal_type
        self.strategy_name = strategy_name
        self.raw = raw or {}
        self.metadata = metadata or {}

    def to_dict(self) -> dict[str, Any]:
        """Return the signal as a plain dict suitable for logging/metrics."""
        return {
            "symbol": self.symbol,
            "side": self.side,
            "quantity": str(self.quantity),
            "price": str(self.price) if self.price else None,
            "signal_type": self.signal_type,
            "strategy_name": self.strategy_name,
        }

    def __repr__(self) -> str:
        price_str = f" @ ${float(self.price):,.2f}" if self.price else " @ market"
        return (
            f"TradingViewSignal({self.side} {self.quantity}x {self.symbol}"
            f"{price_str}, type={self.signal_type})"
        )


# ============================================================================
# Public API
# ============================================================================


def convert_to_action(
    parsed: dict[str, Any],
    default_quantity: Decimal = Decimal("1"),
) -> TradingViewSignal | None:
    """Convert a parsed TradingView alert into a ``TradingViewSignal``.

    Parameters
    ----------
    parsed:
        Alert dict returned by ``parse_alert()``.
    default_quantity:
        Fallback quantity if the alert does not specify one.

    Returns
    -------
    TradingViewSignal or None
        A structured signal, or ``None`` if the alert could not be
        understood.
    """
    raw = parsed.get("raw", "")

    # ----- Extract symbol -----
    symbol = (
        parsed.get("ticker")
        or parsed.get("symbol")
        or parsed.get("market")
        or ""
    )
    if not symbol:
        logger.warning("tv_signal_missing_symbol", raw=raw[:200])
        return None

    # ----- Extract side / action -----
    action = (
        parsed.get("action")
        or parsed.get("side")
        or parsed.get("order_action")
        or ""
    )
    action_lower = action.strip().lower()
    side = _ACTION_TO_SIDE.get(action_lower, "BUY")
    if action_lower == "exit" or action_lower in ("flat", "close"):
        signal_type = "exit"
    else:
        signal_type = "entry"

    # ----- Extract quantity -----
    quantity = default_quantity
    qty_raw = parsed.get("quantity") or parsed.get("qty") or parsed.get("contracts")
    if qty_raw is not None:
        try:
            quantity = Decimal(str(qty_raw))
        except (ValueError, TypeError):
            logger.warning("tv_signal_invalid_quantity", value=qty_raw)

    # ----- Extract price -----
    price: Decimal | None = None
    price_raw = parsed.get("price") or parsed.get("limit_price")
    if price_raw is not None:
        try:
            price = Decimal(str(price_raw))
        except (ValueError, TypeError):
            pass

    # ----- Extract strategy name -----
    strategy_name = parsed.get("strategy", "tradingview")

    # ----- Preserve all unrecognised fields as metadata -----
    known_keys = {
        "ticker", "symbol", "market", "action", "side", "order_action",
        "quantity", "qty", "contracts", "price", "limit_price", "strategy",
        "secret", "order_type", "time_in_force", "takeprofit", "stoploss",
        "raw", "_format", "_content_type", "_parse_error",
        "alert_message", "message",
    }
    metadata = {k: v for k, v in parsed.items() if k not in known_keys}

    logger.info(
        "tv_signal_converted",
        symbol=symbol,
        side=side,
        quantity=str(quantity),
        price=str(price) if price else "market",
        signal_type=signal_type,
        strategy=strategy_name,
    )

    return TradingViewSignal(
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        signal_type=signal_type,
        strategy_name=strategy_name,
        raw=parsed,
        metadata=metadata,
    )
