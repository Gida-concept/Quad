"""Ring buffers for recent market data ticks.

Provides a memory-bounded ``PriceBuffer`` that stores the most recent N price
values per symbol, and a ``FundingRateRingBuffer`` that tracks funding rate
history for trend analysis.

Both use ``collections.deque(maxlen=...)`` internally.
"""

from __future__ import annotations

import asyncio
from collections import deque
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from quad.types.market import FundingRate

logger = structlog.get_logger(__name__)


class PriceBuffer:
    """In-memory ring buffer for price ticks (Decimal values).

    Stores the most recent *max_ticks_per_symbol* price values per symbol.
    Memory-bounded --- never grows beyond that limit per symbol.

    All public read/write methods serialize access via an internal
    :class:`asyncio.Lock`, making this safe for concurrent coroutines.
    """

    def __init__(
        self,
        max_ticks_per_symbol: int | None = None,
        config: dict | None = None,
    ) -> None:
        """Initialize the price buffer.

        Parameters
        ----------
        max_ticks_per_symbol:
            Maximum number of price values to retain per symbol.  Older values
            are discarded automatically.  Falls back to config or default 1000.
        config:
            Optional configuration dict.  Recognised keys under ``buffers``:
            ``max_ticks_per_symbol``, ``get_recent_count``, ``vwap_window``.
        """
        self._config = config or {}
        self._buffer_config = self._config["market_data"]["buffer_sizes"]
        if max_ticks_per_symbol is None:
            max_ticks_per_symbol = int(
                self._buffer_config.get("max_ticks_per_symbol") or 1000
            )
        if max_ticks_per_symbol < 1:
            raise ValueError("max_ticks_per_symbol must be >= 1")

        self._maxlen = max_ticks_per_symbol
        self._buffers: dict[str, deque[Decimal]] = {}
        self._lock = asyncio.Lock()
        self._log = logger.bind(max_ticks_per_symbol=max_ticks_per_symbol)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def append(self, symbol: str, price: Decimal) -> None:
        """Append a price value for *symbol*.

        Thread-safe via :class:`asyncio.Lock`.
        """
        async with self._lock:
            if symbol not in self._buffers:
                self._buffers[symbol] = deque(maxlen=self._maxlen)
            self._buffers[symbol].append(price)

    async def get_latest(self, symbol: str) -> Decimal | None:
        """Return the most recent price for *symbol*, or ``None``."""
        async with self._lock:
            buf = self._buffers.get(symbol)
            if buf is None or len(buf) == 0:
                return None
            return buf[-1]

    async def get_recent(
        self, symbol: str, count: int | None = None
    ) -> list[Decimal]:
        """Return the last *count* prices for *symbol* (newest first).

        Returns fewer than *count* items if fewer are available.
        """
        if count is None:
            count = int(self._buffer_config.get("get_recent_count") or 10)
        async with self._lock:
            buf = self._buffers.get(symbol)
            if buf is None or len(buf) == 0:
                return []
            prices = list(buf)
            return prices[-count:][::-1]

    async def get_symbols(self) -> set[str]:
        """Return the set of all symbols currently tracked in the buffer."""
        async with self._lock:
            return set(self._buffers.keys())

    async def clear(self, symbol: str | None = None) -> None:
        """Clear buffered data.

        Parameters
        ----------
        symbol:
            If provided, only that symbol's data is cleared.  Otherwise
            **all** symbols are cleared.
        """
        async with self._lock:
            if symbol is not None:
                self._buffers.pop(symbol, None)
            else:
                self._buffers.clear()

    async def vwap(
        self, symbol: str, window: int | None = None
    ) -> Decimal | None:
        """Compute the simple average of the last *window* price values.

        Returns ``None`` if fewer than *window* values are available.
        """
        if window is None:
            window = int(self._buffer_config.get("vwap_window") or 20)
        async with self._lock:
            buf = self._buffers.get(symbol)
            if buf is None or len(buf) < window:
                return None

            prices = list(buf)[-window:]
            total = sum(prices, Decimal(0))
            return total / Decimal(str(window))

    async def has_data(self, symbol: str) -> bool:
        """Return ``True`` if at least one price value exists for *symbol*."""
        async with self._lock:
            buf = self._buffers.get(symbol)
            return buf is not None and len(buf) > 0

    async def total_ticks(self) -> int:
        """Return the total number of price values across all symbols."""
        async with self._lock:
            return sum(len(buf) for buf in self._buffers.values())

    async def symbols_tracked(self) -> int:
        """Return the number of distinct symbols tracked."""
        async with self._lock:
            return len(self._buffers)


class FundingRateRingBuffer:
    """Ring buffer for recent funding rate values.

    Tracks the last N funding rate ``FundingRate`` objects per symbol for
    trend analysis.  Memory-bounded with a fixed max length per symbol.
    """

    def __init__(
        self,
        maxlen: int | None = None,
        config: dict | None = None,
    ) -> None:
        """Initialize the funding rate ring buffer.

        Parameters
        ----------
        maxlen:
            Maximum number of funding rate entries to retain per symbol.
            Older entries are discarded automatically.  Falls back to
            config or default 100.
        config:
            Optional configuration dict.  Recognised keys under ``buffers``:
            ``funding_rate_maxlen``, ``funding_rate_get_recent_count``.
        """
        self._config = config or {}
        self._buffer_config = self._config["market_data"]["buffer_sizes"]
        if maxlen is None:
            maxlen = int(self._buffer_config.get("funding_rate_maxlen") or 100)
        self._maxlen = maxlen
        self._buffers: dict[str, deque[FundingRate]] = {}
        self._lock = asyncio.Lock()
        self._log = logger.bind(maxlen=maxlen)

    async def append(self, symbol: str, rate: FundingRate) -> None:
        """Append a funding rate entry for *symbol*."""
        async with self._lock:
            if symbol not in self._buffers:
                self._buffers[symbol] = deque(maxlen=self._maxlen)
            self._buffers[symbol].append(rate)

    async def get_latest(self, symbol: str) -> FundingRate | None:
        """Return the most recent funding rate for *symbol*, or ``None``."""
        async with self._lock:
            buf = self._buffers.get(symbol)
            if buf is None or len(buf) == 0:
                return None
            return buf[-1]

    async def get_recent(
        self, symbol: str, count: int | None = None
    ) -> list[FundingRate]:
        """Return the last *count* funding rates for *symbol* (newest first)."""
        if count is None:
            count = int(self._buffer_config.get("funding_rate_get_recent_count") or 10)
        async with self._lock:
            buf = self._buffers.get(symbol)
            if buf is None or len(buf) == 0:
                return []
            rates = list(buf)
            return rates[-count:][::-1]

    async def get_symbols(self) -> set[str]:
        """Return the set of all symbols currently tracked."""
        async with self._lock:
            return set(self._buffers.keys())

    async def clear(self, symbol: str | None = None) -> None:
        """Clear buffered data."""
        async with self._lock:
            if symbol is not None:
                self._buffers.pop(symbol, None)
            else:
                self._buffers.clear()

    async def has_data(self, symbol: str) -> bool:
        """Return ``True`` if at least one entry exists for *symbol*."""
        async with self._lock:
            buf = self._buffers.get(symbol)
            return buf is not None and len(buf) > 0

    async def total_entries(self) -> int:
        """Return the total number of entries across all symbols."""
        async with self._lock:
            return sum(len(buf) for buf in self._buffers.values())

    async def symbols_tracked(self) -> int:
        """Return the number of distinct symbols tracked."""
        async with self._lock:
            return len(self._buffers)
