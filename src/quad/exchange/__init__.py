"""Exchange adapter package for Quad futures trading bot.

Provides a pluggable ExchangeAdapter ABC with the active implementation:

- ``OkxFuturesAdapter`` — Live / demo OKX USDT-perpetual (V5 API,
  ``instType=SWAP``) via the official ``python-okx`` SDK.

Use ``create_exchange(config)`` to instantiate the correct adapter based on
the configuration dictionary.
"""

from __future__ import annotations

from quad.exchange.base import ExchangeAdapter
from quad.exchange.factory import create_exchange
from quad.exchange.okx import OkxFuturesAdapter

__all__ = [
    "ExchangeAdapter",
    "OkxFuturesAdapter",
    "create_exchange",
]
