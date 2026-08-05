"""Exchange adapter package for Quad futures trading bot.

Provides a pluggable ExchangeAdapter ABC with two implementations:

- ``BinanceFuturesAdapter`` — Live Binance Futures trading via REST + WebSocket
- ``MockAdapter`` — Pre-configured responses for testing/backtesting

Use ``create_exchange(config)`` to instantiate the correct adapter based on
the configuration dictionary.
"""

from __future__ import annotations

from quad.exchange.base import ExchangeAdapter
from quad.exchange.binance import BinanceFuturesAdapter
from quad.exchange.factory import create_exchange
from quad.exchange.mock import MockAdapter

__all__ = [
    "BinanceFuturesAdapter",
    "ExchangeAdapter",
    "MockAdapter",
    "create_exchange",
]
