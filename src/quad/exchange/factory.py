"""Exchange adapter factory.

Creates the correct ``ExchangeAdapter`` implementation based on the
application configuration.
"""

from __future__ import annotations

import os
from decimal import Decimal

import structlog

from quad.exchange.base import ExchangeAdapter
from quad.exchange.binance import BinanceOptionsAdapter
from quad.exchange.paper import PaperTradingAdapter
from quad.exchange.mock import MockAdapter

logger = structlog.get_logger(__name__)


def create_exchange(
    config: dict | None = None,
) -> ExchangeAdapter:
    """Create an exchange adapter based on the provided configuration.

    The ``mode`` is determined by (in priority order):

    1. ``config["exchange.mode"]`` (explicit mode key)
    2. ``config["exchange"]["name"]`` (nested config section)
    3. ``config.get("exchange.name")`` (flat/dot-notation)
    4. Defaults to ``"binance"``

    Args:
        config: Application configuration dictionary.  May be ``None``
            (all defaults are used, which resolves to paper trading).

    Returns:
        An initialized ``ExchangeAdapter`` instance.

    Raises:
        ValueError: If the configured mode is not one of
            ``binance``, ``paper``, or ``mock``.

    Examples::

        # Paper trading (default)
        adapter = create_exchange({"exchange.name": "paper"})

        # Live Binance with API keys from env vars
        adapter = create_exchange({
            "exchange.name": "binance",
            "exchange.testnet": False,
        })

        # Mock for testing
        adapter = create_exchange({"exchange.name": "mock"})
    """
    cfg = config or {}

    # Determine mode
    mode = (
        cfg.get("exchange.mode")
        or _nested_get(cfg, "exchange", "name")
        or cfg.get("exchange.name", "paper")
    )

    mode = str(mode).lower().strip()
    logger.info("create_exchange", mode=mode)

    if mode == "binance":
        api_key = (
            cfg.get("exchange.api_key")
            or os.environ.get("BINANCE_API_KEY", "")
        )
        api_secret = (
            cfg.get("exchange.api_secret")
            or os.environ.get("BINANCE_API_SECRET", "")
        )
        testnet = _coerce_bool(
            cfg.get("exchange.testnet")
            or os.environ.get("BINANCE_TESTNET", False)
        )
        rate_limit = cfg.get("exchange.rate_limit") or {}

        return BinanceOptionsAdapter(
            api_key=api_key,
            api_secret=api_secret,
            testnet=testnet,
            rate_limit=rate_limit,
        )

    if mode in ("paper", "paper_trading", "paper-trading"):
        initial_balance = Decimal(
            str(cfg.get("paper.initial_balance", 10000))
        )
        return PaperTradingAdapter(
            initial_balance_usdt=initial_balance,
        )

    if mode == "mock":
        return MockAdapter()

    msg = (
        f"Unknown exchange mode: '{mode}'. "
        "Expected one of: binance, paper, mock."
    )
    raise ValueError(msg)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _nested_get(d: dict, *keys: str) -> str | None:
    """Safely traverse nested dict keys.

    Args:
        d: The dictionary to traverse.
        keys: Sequence of keys to follow (e.g. ``"exchange"``, ``"name"``).

    Returns:
        The string value at the leaf, or ``None`` if any key is missing
        or the value is not a string.
    """
    current: object = d
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return None
    if isinstance(current, str):
        return current
    return None


def _coerce_bool(value: object) -> bool:
    """Coerce a value to bool, handling string representations.

    Args:
        value: The value to coerce (bool, str, int, etc.).

    Returns:
        The coerced boolean.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return bool(value)
