"""Exchange adapter factory.

Creates the correct ``ExchangeAdapter`` implementation based on the
application configuration.
"""

from __future__ import annotations

import os

import structlog

from quad.exchange.base import ExchangeAdapter
from quad.exchange.bybit import BybitFuturesAdapter

logger = structlog.get_logger(__name__)


def create_exchange(
    config: dict | None = None,
) -> ExchangeAdapter:
    """Create an exchange adapter based on the provided configuration.

    The ``mode`` is determined by (in priority order):

    1. ``config["exchange.mode"]`` (explicit mode key)
    2. ``config["exchange"]["name"]`` (nested config section)
    3. ``config.get("exchange.name")`` (flat/dot-notation)
    4. Defaults to ``"bybit"``

    Only Bybit USDT-perpetual is supported.  The bot targets Bybit's
    ``category=linear`` (USDT perpetual) market; testnet is the default
    safety environment and live is opt-in via ``exchange.testnet: false``.

    Args:
        config: Application configuration dictionary.  May be ``None``
            (all defaults are used, which resolves to Bybit testnet).

    Returns:
        An initialized ``ExchangeAdapter`` instance.

    Raises:
        ValueError: If the configured mode is not ``bybit``.

    Examples::

        # Bybit (testnet or live)
        adapter = create_exchange({
            "exchange.name": "bybit",
            "exchange.testnet": True,
        })
    """
    cfg = config or {}

    # Determine mode using nested lookup (flat dot-notation keys don't work with dict.get)
    mode = _nested_get(cfg, "exchange", "name") or "bybit"

    mode = str(mode).lower().strip()
    logger.info("create_exchange", mode=mode)

    if mode == "bybit":
        exchange_cfg = cfg["exchange"]
        api_key = exchange_cfg.get("api_key") or os.environ.get("BYBIT_API_KEY", "")
        api_secret = exchange_cfg.get("api_secret") or os.environ.get(
            "BYBIT_API_SECRET", ""
        )
        testnet = _coerce_bool(
            exchange_cfg.get("testnet") or os.environ.get("BYBIT_TESTNET", "")
        )
        rate_limit = exchange_cfg.get("rate_limit") or {}

        return BybitFuturesAdapter(
            api_key=api_key,
            api_secret=api_secret,
            testnet=testnet,
            rate_limit=rate_limit,
            config=cfg,
        )

    msg = f"Unknown exchange mode: '{mode}'. Expected one of: bybit."
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
