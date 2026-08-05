"""Market data types for Quad futures trading bot.

This module defines the core market data structures used throughout
the application for representing futures contracts, funding rates,
underlying prices, and candle data.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

__all__ = [
    "Candle",
    "FundingRate",
    "FuturesContract",
    "UnderlyingPrice",
]


@dataclass
class FuturesContract:
    symbol: str = ""
    mark_price: Decimal = Decimal(0)
    index_price: Decimal = Decimal(0)
    funding_rate: Decimal = Decimal(0)
    next_funding_time: int = 0
    volume_24h: Decimal = Decimal(0)
    open_interest: Decimal = Decimal(0)
    open_interest_value: Decimal = Decimal(0)
    last_price: Decimal = Decimal(0)
    price_change_24h: Decimal = Decimal(0)
    high_24h: Decimal = Decimal(0)
    low_24h: Decimal = Decimal(0)
    last_update: int = 0


@dataclass
class FundingRate:
    symbol: str = ""
    funding_rate: Decimal = Decimal(0)
    next_funding_time: int = 0
    mark_price: Decimal = Decimal(0)
    index_price: Decimal = Decimal(0)


@dataclass
class UnderlyingPrice:
    symbol: str = ""
    price: Decimal = Decimal(0)
    timestamp: int = 0


@dataclass
class Candle:
    symbol: str = ""
    open: Decimal = Decimal(0)
    high: Decimal = Decimal(0)
    low: Decimal = Decimal(0)
    close: Decimal = Decimal(0)
    volume: Decimal = Decimal(0)
    timestamp: int = 0
