"""Ring buffer for recent market data ticks.

Provides a memory-bounded ``PriceBuffer`` that stores the most recent N ticks
per symbol using ``collections.deque(maxlen=...)`` internally.
"""

from __future__ import annotations

import asyncio
from collections import deque
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from quad.types.market import OptionPriceTick

logger = structlog.get_logger(__name__)


class PriceBuffer:
    """In-memory ring buffer for option price ticks.

    Stores the most recent *max_ticks_per_symbol* ticks per symbol.
    Memory-bounded --- never grows beyond that limit per symbol.

    All public read/write methods serialize access via an internal
    :class:`asyncio.Lock`, making this safe for concurrent coroutines.
    """

    def __init__(self, max_ticks_per_symbol: int = 1000) -> None:
        """Initialize the price buffer.

        Parameters
        ----------
        max_ticks_per_symbol:
            Maximum number of ticks to retain per symbol.  Older ticks are
            discarded automatically.
        """
        if max_ticks_per_symbol < 1:
            raise ValueError("max_ticks_per_symbol must be >= 1")

        self._maxlen = max_ticks_per_symbol
        self._buffers: dict[str, deque[OptionPriceTick]] = {}
        self._lock = asyncio.Lock()
        self._log = logger.bind(max_ticks_per_symbol=max_ticks_per_symbol)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def append(self, symbol: str, tick: OptionPriceTick) -> None:
        """Append a price tick for *symbol*.

        Thread-safe via :class:`asyncio.Lock`.
        """
        async with self._lock:
            if symbol not in self._buffers:
                self._buffers[symbol] = deque(maxlen=self._maxlen)
            self._buffers[symbol].append(tick)

    async def get_latest(self, symbol: str) -> OptionPriceTick | None:
        """Return the most recent tick for *symbol*, or ``None``."""
        async with self._lock:
            buf = self._buffers.get(symbol)
            if buf is None or len(buf) == 0:
                return None
            return buf[-1]

    async def get_recent(
        self, symbol: str, count: int = 10
    ) -> list[OptionPriceTick]:
        """Return the last *count* ticks for *symbol* (newest first).

        Returns fewer than *count* items if fewer are available.
        """
        async with self._lock:
            buf = self._buffers.get(symbol)
            if buf is None or len(buf) == 0:
                return []
            # deques are sequences; grab the last *count* elements
            ticks = list(buf)
            return ticks[-count:][::-1]

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
        self, symbol: str, window: int = 20
    ) -> Decimal | None:
        """Compute the simple average of the last *window* close prices.

        Returns ``None`` if fewer than *window* ticks are available.
        """
        async with self._lock:
            buf = self._buffers.get(symbol)
            if buf is None or len(buf) < window:
                return None

            ticks = list(buf)[-window:]
            total = Decimal("0")
            for t in ticks:
                price = t.last_price if t.last_price is not None else t.bid if t.bid is not None else t.ask
                if price is None:
                    return None  # Cannot compute VWAP without a usable price
                total += Decimal(str(price))
            return total / Decimal(str(window))

    async def has_data(self, symbol: str) -> bool:
        """Return ``True`` if at least one tick exists for *symbol*."""
        async with self._lock:
            buf = self._buffers.get(symbol)
            return buf is not None and len(buf) > 0

    async def total_ticks(self) -> int:
        """Return the total number of ticks across all symbols."""
        async with self._lock:
            return sum(len(buf) for buf in self._buffers.values())

    async def symbols_tracked(self) -> int:
        """Return the number of distinct symbols tracked."""
        async with self._lock:
            return len(self._buffers)
