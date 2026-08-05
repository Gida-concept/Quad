"""Market data package for Quad futures trading bot.

Provides the market data engine that coordinates WebSocket subscriptions,
price buffering, futures market data caches, and historical data access.
"""

from __future__ import annotations

from quad.market_data.buffers import FundingRateRingBuffer, PriceBuffer
from quad.market_data.cache import (
    FundingRateCache,
    MarkPriceCache,
    OpenInterestCache,
    OrderBookCache,
)
from quad.market_data.engine import MarketDataEngine
from quad.market_data.historical import HistoricalDataProvider
from quad.market_data.websocket import WebSocketManager

__all__ = [
    "FundingRateCache",
    "FundingRateRingBuffer",
    "HistoricalDataProvider",
    "MarkPriceCache",
    "MarketDataEngine",
    "OpenInterestCache",
    "OrderBookCache",
    "PriceBuffer",
    "WebSocketManager",
]
