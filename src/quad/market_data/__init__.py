"""Market data package for Quad options trading bot.

Provides the market data engine that coordinates WebSocket subscriptions,
price buffering, option chain caching, and historical data access.
"""

from __future__ import annotations

from quad.market_data.websocket import WebSocketManager
from quad.market_data.buffers import PriceBuffer
from quad.market_data.cache import OptionChainCache
from quad.market_data.historical import HistoricalDataProvider
from quad.market_data.engine import MarketDataEngine

__all__ = [
    "MarketDataEngine",
    "WebSocketManager",
    "PriceBuffer",
    "OptionChainCache",
    "HistoricalDataProvider",
]
