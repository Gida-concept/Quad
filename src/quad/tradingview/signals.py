"""TradingView signal converter.

Converts parsed TradingView webhook alerts into trading signals that
the Quad execution engine can act on.  Maps TradingView actions
(buy/sell/exit/flat) to Quad order types and validates required fields.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

logger = structlog.get_logger(__name__)

#: Environment variable holding the configured TradingView webhook secret.
#: Kept as an alias of the single authoritative name mapped by
#: ``ConfigManager.ENV_VAR_MAP`` (``QUAD_TRADINGVIEW_WEBHOOK_SECRET``) so
#: there is exactly one env var for "the" webhook secret.
WEBHOOK_SECRET_ENV = "QUAD_TRADINGVIEW_WEBHOOK_SECRET"

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
        order_type: str = "MARKET",
        signal_type: str = "entry",
        strategy_name: str = "tradingview",
        raw: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        reduce_only: bool = False,
    ) -> None:
        self.symbol = symbol
        self.side = side
        self.quantity = quantity
        self.price = price
        self.order_type = order_type
        self.signal_type = signal_type
        self.strategy_name = strategy_name
        self.raw = raw or {}
        self.metadata = metadata or {}
        self.reduce_only = reduce_only

    def to_dict(self) -> dict[str, Any]:
        """Return the signal as a plain dict suitable for logging/metrics."""
        return {
            "symbol": self.symbol,
            "side": self.side,
            "quantity": str(self.quantity),
            "price": str(self.price) if self.price else None,
            "order_type": self.order_type,
            "signal_type": self.signal_type,
            "strategy_name": self.strategy_name,
            "reduce_only": self.reduce_only,
        }

    def __repr__(self) -> str:
        price_str = f" @ ${float(self.price):,.2f}" if self.price else " @ market"
        return (
            f"TradingViewSignal({self.side} {self.quantity}x {self.symbol}"
            f"{price_str}, type={self.signal_type})"
        )


# ============================================================================
# Webhook authentication + symbol normalisation
# ============================================================================


def get_webhook_secret(explicit: str | None = None) -> str:
    """Return the configured TradingView webhook secret.

    Explicit argument wins; otherwise ``QUAD_TRADINGVIEW_WEBHOOK_SECRET`` is
    read.  Empty string means no secret is configured.

    Callers that already resolved the secret from config (e.g. the
    orchestrator webhook handler) must pass it explicitly — reading the
    environment here is a fallback for standalone use only.
    """
    if explicit is not None:
        return explicit
    return os.environ.get(WEBHOOK_SECRET_ENV, "")


def verify_signature(
    raw_body: str | bytes,
    signature: str,
    secret: str | None = None,
) -> bool:
    """Verify an HMAC-SHA256 webhook signature (hex digest).

    Returns ``False`` when no secret is configured, or the signature is
    missing/mismatched.  Comparison uses :func:`hmac.compare_digest`.
    """
    sec = get_webhook_secret(secret)
    if not sec or not signature:
        return False
    body = raw_body.encode("utf-8") if isinstance(raw_body, str) else raw_body
    expected = hmac.new(sec.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(str(signature).strip().lower(), expected)


def normalize_symbol(raw: str) -> str:
    """Normalise a TradingView ticker to a Quad contract symbol.

    Strips any ``EXCHANGE:`` prefix, removes ``/``, ``-``, ``_``, space
    and ``.`` separators, drops a trailing ``PERP``/``PERPETUAL`` marker,
    and uppercases (``"binance:BTC-USDT"`` -> ``"BTCUSDT"``).
    """
    s = str(raw or "").strip().upper()
    if ":" in s:
        s = s.split(":")[-1]
    for sep in ("/", "-", "_", " ", "."):
        s = s.replace(sep, "")
    for suffix in ("PERPETUAL", "PERP"):
        if s.endswith(suffix) and len(s) > len(suffix):
            s = s[: -len(suffix)]
            break
    return s


# ============================================================================
# Public API
# ============================================================================


def convert_to_action(
    parsed: dict[str, Any],
    default_quantity: Decimal = Decimal(1),
    expected_secret: str | None = None,
) -> TradingViewSignal | None:
    """Convert a parsed TradingView alert into a ``TradingViewSignal``.

    Parameters
    ----------
    parsed:
        Alert dict returned by ``parse_alert()``.
    default_quantity:
        Fallback quantity if the alert does not specify one.
    expected_secret:
        Optional webhook secret to enforce.  When ``None``, the
        ``QUAD_TRADINGVIEW_WEBHOOK_SECRET`` env var is consulted.
        Enforcement applies to credentials carried *in the alert itself* (a
        ``secret`` field or an HMAC ``signature`` over ``raw``); alerts
        carrying no credential rely on transport-layer auth (the HTTP
        handler verifies the ``X-Webhook-Signature`` header or the payload
        ``secret`` field before parsing).

    Returns
    -------
    TradingViewSignal or None
        A structured signal, or ``None`` when the alert could not be
        understood: missing symbol, unknown/missing action (never a ``BUY``
        default), bad/unsigned secret, or non-positive quantity.  ``CLOSE``
        actions map to ``side="CLOSE"`` with ``reduce_only=True``.
    """
    raw = parsed.get("raw", "")

    # ----- Optional in-alert authentication -----
    secret = get_webhook_secret(expected_secret)
    if secret:
        provided = parsed.get("secret") or parsed.get("webhook_secret") or ""
        sig = parsed.get("signature") or parsed.get("hmac") or ""
        authed = False
        if provided:
            authed = hmac.compare_digest(str(provided), secret)
        elif sig:
            authed = verify_signature(parsed.get("raw", ""), str(sig), secret=secret)
        else:
            logger.warning("tv_signal_rejected_no_credential")
            return None  # require proof of authenticity when secret configured
        if not authed:
            logger.warning("tv_signal_rejected_bad_secret")
            return None

    # ----- Extract + normalise symbol -----
    symbol_raw = (
        parsed.get("ticker") or parsed.get("symbol") or parsed.get("market") or ""
    )
    symbol = normalize_symbol(symbol_raw)
    if not symbol:
        logger.warning("tv_signal_missing_symbol", raw=str(raw)[:200])
        return None

    # ----- Extract side / action (strict: no BUY default) -----
    action = (
        parsed.get("action") or parsed.get("side") or parsed.get("order_action") or ""
    )
    action_lower = str(action).strip().lower()
    side = _ACTION_TO_SIDE.get(action_lower)
    if side is None:
        logger.warning("tv_signal_unknown_action", action=str(action)[:50])
        return None
    if side == "CLOSE":
        signal_type = "exit"
        reduce_only = True
    else:
        signal_type = "exit" if action_lower in ("exit",) else "entry"
        reduce_only = False

    # ----- Extract + validate quantity -----
    quantity = default_quantity
    qty_raw = parsed.get("quantity") or parsed.get("qty") or parsed.get("contracts")
    if qty_raw is not None:
        try:
            quantity = Decimal(str(qty_raw))
        except (ValueError, TypeError, InvalidOperation):
            logger.warning("tv_signal_invalid_quantity", value=str(qty_raw)[:50])
            return None
    try:
        if quantity is None or Decimal(str(quantity)) <= 0:
            logger.warning("tv_signal_non_positive_quantity")
            return None
        quantity = Decimal(str(quantity))
    except (ValueError, TypeError, InvalidOperation):
        logger.warning("tv_signal_invalid_quantity")
        return None

    # ----- Extract price -----
    # All orders are MARKET — ignore any limit price in the alert.
    price: Decimal | None = None

    # ----- Extract strategy name -----
    strategy_name = parsed.get("strategy", "tradingview")

    # ----- Preserve all unrecognised fields as metadata -----
    known_keys = {
        "ticker",
        "symbol",
        "market",
        "action",
        "side",
        "order_action",
        "quantity",
        "qty",
        "contracts",
        "price",
        "limit_price",
        "strategy",
        "secret",
        "webhook_secret",
        "signature",
        "hmac",
        "order_type",
        "time_in_force",
        "takeprofit",
        "stoploss",
        "raw",
        "_format",
        "_content_type",
        "_parse_error",
        "alert_message",
        "message",
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
        order_type="MARKET",  # all orders are MARKET; no limit orders
        signal_type=signal_type,
        strategy_name=strategy_name,
        raw=parsed,
        metadata=metadata,
        reduce_only=reduce_only,
    )
