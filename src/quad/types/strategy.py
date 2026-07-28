"""Strategy system types for Quad options trading bot.

This module defines types for the strategy execution context
and data access interfaces used by strategies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from quad.types.domain import Account, FuturesPosition, Order, Position
from quad.types.market import FundingRate, FuturesContract
from quad.types.risk import RiskStatus


__all__ = [
    "StrategyContext",
    "HistoricalDataAccess",
]


@runtime_checkable
class HistoricalDataAccess(Protocol):
    """Protocol for accessing historical market data.

    Strategies use this interface to retrieve historical data for
    analysis and backtesting without depending on the concrete
    data storage implementation.
    """

    async def get_candles(
        self,
        symbol: str,
        start: int,
        end: int,
    ) -> list[dict[str, Any]]:
        """Retrieve OHLCV candles for a symbol over a time range.

        Args:
            symbol: Trading pair symbol.
            start: Start timestamp in unix milliseconds.
            end: End timestamp in unix milliseconds.

        Returns:
            List of candle dicts with keys: timestamp, open, high, low, close, volume.
        """
        ...

    async def get_funding_rate_history(
        self, symbol: str, start: int, end: int
    ) -> list[FundingRate]:
        """Retrieve funding rate history for a symbol.

        Args:
            symbol: Trading pair symbol.
            start: Start timestamp in unix milliseconds.
            end: End timestamp in unix milliseconds.

        Returns:
            List of FundingRate objects.
        """
        ...


@dataclass
class StrategyContext:
    """Full context provided to a strategy for analysis.

    Contains all current market data, account state, risk status,
    and configuration needed for strategy decision-making.
    """

    account: Account | None = None
    """Current account state, or None if not available."""

    positions: list[Position] = field(default_factory=list)
    """Currently open positions."""

    futures_positions: list[FuturesPosition] = field(default_factory=list)
    """Currently open futures positions."""

    orders: list[Order] = field(default_factory=list)
    """Currently open orders."""

    futures_contracts: dict[str, FuturesContract] = field(default_factory=dict)
    """Current futures contract data keyed by symbol."""

    funding_rates: dict[str, FundingRate] = field(default_factory=dict)
    """Current funding rates keyed by symbol."""

    mark_prices: dict[str, float] = field(default_factory=dict)
    """Current mark prices keyed by symbol."""

    underlying_price: float | None = None
    """Current underlying asset price."""

    risk_status: RiskStatus | None = None
    """Current risk management status."""

    circuit_breakers: dict[str, Any] = field(default_factory=dict)
    """Current circuit breaker states."""

    config: dict[str, Any] = field(default_factory=dict)
    """Global configuration dictionary."""

    strategy_params: dict[str, Any] = field(default_factory=dict)
    """Strategy-specific parameters."""

    option_chain: list[Any] = field(default_factory=list)
    """Option chain contracts (empty for futures-only bot; used by backtester)."""

    historical: HistoricalDataAccess | None = None
    """Interface for accessing historical market data."""
