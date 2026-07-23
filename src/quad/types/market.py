"""Market data types for Quad options trading bot.

This module defines the core market data structures used throughout
the application for representing option contracts, price ticks,
greeks, underlying prices, and candle data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal


__all__ = [
    "OptionContract",
    "GreekTick",
    "OptionPriceTick",
    "UnderlyingPrice",
    "Candle",
]


@dataclass
class OptionContract:
    """Represents a single options contract with current market data.

    All monetary values use Decimal for precision to avoid floating-point
    rounding errors in financial calculations.
    """

    symbol: str
    """Trading pair symbol, e.g. 'BTC-220930-20000-C'."""

    underlying: str
    """Underlying asset, e.g. 'BTCUSDT'."""

    strike: Decimal
    """Strike price of the option contract."""

    expiry: int
    """Expiry timestamp in unix milliseconds."""

    option_type: Literal["CALL", "PUT"]
    """Option type: CALL or PUT."""

    mark_price: Decimal
    """Current mark price of the contract."""

    bid: Decimal | None = None
    """Current best bid price, or None if no bid."""

    ask: Decimal | None = None
    """Current best ask price, or None if no ask."""

    volume: Decimal = Decimal("0")
    """24h trading volume."""

    open_interest: int = 0
    """Open interest in contracts."""

    implied_volatility: Decimal = Decimal("0")
    """Implied volatility as a decimal (e.g. 0.45 for 45%)."""

    delta: Decimal = Decimal("0")
    """Option delta."""

    gamma: Decimal = Decimal("0")
    """Option gamma."""

    theta: Decimal = Decimal("0")
    """Option theta (daily decay)."""

    vega: Decimal = Decimal("0")
    """Option vega."""


@dataclass
class GreekTick:
    """A snapshot of Greek values for an option contract at a point in time."""

    symbol: str
    """Trading pair symbol."""

    timestamp: int
    """Tick timestamp in unix milliseconds."""

    delta: Decimal = Decimal("0")
    """Option delta."""

    gamma: Decimal = Decimal("0")
    """Option gamma."""

    theta: Decimal = Decimal("0")
    """Option theta."""

    vega: Decimal = Decimal("0")
    """Option vega."""

    rho: Decimal = Decimal("0")
    """Option rho."""


@dataclass
class OptionPriceTick:
    """A price tick for an option contract from the trade stream."""

    symbol: str
    """Trading pair symbol."""

    timestamp: int
    """Tick timestamp in unix milliseconds."""

    bid: Decimal | None = None
    """Current best bid at tick time."""

    ask: Decimal | None = None
    """Current best ask at tick time."""

    last_price: Decimal | None = None
    """Last traded price, or None if no trade yet."""

    volume: Decimal = Decimal("0")
    """Traded volume at this tick."""


@dataclass
class UnderlyingPrice:
    """Represents the current price of an underlying asset."""

    symbol: str
    """Underlying asset symbol, e.g. 'BTCUSDT'."""

    price: Decimal
    """Current price of the underlying asset."""

    timestamp: int
    """Price timestamp in unix milliseconds."""


@dataclass
class Candle:
    """OHLCV candle data for a trading pair."""

    symbol: str
    """Trading pair symbol."""

    open: Decimal
    """Opening price."""

    high: Decimal
    """Highest price during the period."""

    low: Decimal
    """Lowest price during the period."""

    close: Decimal
    """Closing price."""

    volume: Decimal
    """Trading volume during the period."""

    timestamp: int
    """Candle open timestamp in unix milliseconds."""
