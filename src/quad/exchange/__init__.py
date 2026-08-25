"""Exchange adapter package for Quad futures trading bot.

Provides a pluggable ExchangeAdapter ABC with the active implementation:

- ``BybitFuturesAdapter`` — Live / testnet Bybit USDT-perpetual (V5 API,
  ``category=linear``) via the official ``pybit`` SDK.

Use ``create_exchange(config)`` to instantiate the correct adapter based on
the configuration dictionary.
"""

from __future__ import annotations

from quad.exchange.base import ExchangeAdapter
from quad.exchange.factory import create_exchange
from quad.exchange.bybit import BybitFuturesAdapter

__all__ = [
    "BybitFuturesAdapter",
    "ExchangeAdapter",
    "create_exchange",
]
