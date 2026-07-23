"""Strategy system types for Quad options trading bot.

This module defines types for the strategy execution context
and data access interfaces used by strategies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from quad.types.domain import Account, Order, Position
from quad.types.market import OptionContract, UnderlyingPrice
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

    async def get_option_chain_snapshot(
        self,
        symbol: str,
        timestamp: int,
    ) -> list[dict[str, Any]]:
        """Retrieve a snapshot of the option chain at a given timestamp.

        Args:
            symbol: Underlying asset symbol.
            timestamp: Snapshot timestamp in unix milliseconds.

        Returns:
            List of option contract dicts with full market data.
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

    orders: list[Order] = field(default_factory=list)
    """Currently open orders."""

    option_chain: list[OptionContract] = field(default_factory=list)
    """Current option chain data as list of OptionContract dicts."""

    underlying_price: UnderlyingPrice | None = None
    """Current underlying asset price."""

    greeks: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Mapping of symbol to Greek values dict."""

    risk_status: RiskStatus | None = None
    """Current risk management status."""

    circuit_breakers: dict[str, Any] = field(default_factory=dict)
    """Current circuit breaker states."""

    config: dict[str, Any] = field(default_factory=dict)
    """Global configuration dictionary."""

    strategy_params: dict[str, Any] = field(default_factory=dict)
    """Strategy-specific parameters."""

    historical: HistoricalDataAccess | None = None
    """Interface for accessing historical market data."""
