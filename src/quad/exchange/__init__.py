"""Exchange adapter package for Quad options trading bot.

Provides a pluggable ExchangeAdapter ABC with three implementations:

- ``BinanceOptionsAdapter`` — Live Binance Options trading via REST + WebSocket
- ``PaperTradingAdapter`` — Simulated fills for paper trading
- ``MockAdapter`` — Pre-configured responses for testing/backtesting

Use ``create_exchange(config)`` to instantiate the correct adapter based on
the configuration dictionary.
"""

from __future__ import annotations

from quad.exchange.base import ExchangeAdapter
from quad.exchange.binance import BinanceOptionsAdapter
from quad.exchange.paper import PaperTradingAdapter
from quad.exchange.mock import MockAdapter
from quad.exchange.factory import create_exchange

__all__ = [
    "ExchangeAdapter",
    "BinanceOptionsAdapter",
    "PaperTradingAdapter",
    "MockAdapter",
    "create_exchange",
]
