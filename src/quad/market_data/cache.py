"""TTL cache for option chain data with stampede prevention.

Provides ``OptionChainCache`` that caches option chain snapshots keyed by
underlying symbol, auto-expires entries after a configurable TTL, and uses
per-key :class:`asyncio.Lock` to prevent duplicate API calls when multiple
consumers request the same underlying simultaneously (cache stampede).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from quad.exchange.base import ExchangeAdapter
    from quad.types.market import OptionContract

logger = structlog.get_logger(__name__)


@dataclass
class _CacheEntry:
    """Internal cache entry holding fetched data and expiry metadata."""

    data: list[OptionContract]
    fetched_at: float  # time.monotonic() timestamp
    ttl: int  # seconds


class OptionChainCache:
    """Cache for option chain data keyed by underlying symbol.

    * Auto-expires entries after *default_ttl* seconds.
    * Per-key :class:`asyncio.Lock` prevents cache stampede --- only one
      coroutine fetches a given underlying at a time; others await the
      result.
    * Tracks hit / miss / expiry statistics for observability.
    """

    def __init__(
        self,
        exchange_adapter: ExchangeAdapter,
        default_ttl: int = 60,
    ) -> None:
        """Initialize the cache.

        Parameters
        ----------
        exchange_adapter:
            The exchange adapter used to fetch option chains on cache miss.
        default_ttl:
            Default time-to-live in seconds for cached entries.
        """
        self._exchange = exchange_adapter
        self._default_ttl = default_ttl
        self._cache: dict[str, _CacheEntry] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._hits = 0
        self._misses = 0
        self._expired = 0
        self._log = logger.bind(default_ttl=default_ttl)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get(self, underlying: str) -> list[OptionContract]:
        """Return the option chain for *underlying*.

        Fetches from the exchange adapter if the entry is missing or stale.
        Uses a per-key lock so only one coroutine performs the actual fetch;
        concurrent callers for the same underlying await the same result.

        Parameters
        ----------
        underlying:
            The underlying asset symbol, e.g. ``"BTCUSDT"``.
        """
        # Fast path: check cache without lock
        entry = self._cache.get(underlying)
        if entry is not None and not self._is_expired(entry):
            self._hits += 1
            return entry.data

        # Slow path: acquire per-key lock to prevent stampede
        lock = self._get_or_create_lock(underlying)
        async with lock:
            # Double-check after acquiring the lock (another coroutine may
            # have populated the cache while we were waiting).
            entry = self._cache.get(underlying)
            if entry is not None and not self._is_expired(entry):
                self._hits += 1
                return entry.data

            self._misses += 1
            if entry is not None:
                self._expired += 1

            try:
                data = await self._exchange.get_option_chain(underlying)
            except Exception:
                self._log.exception(
                    "cache_fetch_failed",
                    underlying=underlying,
                )
                # If stale data is available, return it as a fallback
                if entry is not None:
                    self._log.warning(
                        "cache_returning_stale_data",
                        underlying=underlying,
                    )
                    return entry.data
                # No data at all --- re-raise
                raise

            self._cache[underlying] = _CacheEntry(
                data=data,
                fetched_at=time.monotonic(),
                ttl=self._default_ttl,
            )
            return data

    async def get_multi(
        self, underlyings: list[str]
    ) -> dict[str, list[OptionContract]]:
        """Fetch option chains for multiple underlyings concurrently.

        Returns a dict mapping each underlying to its option chain.  Hits
        the cache where possible; fetches stale / missing entries in
        parallel.
        """
        results: dict[str, list[OptionContract]] = {}

        # Launch concurrent gets
        tasks = {u: asyncio.create_task(self.get(u)) for u in underlyings}
        for underlying, task in tasks.items():
            try:
                results[underlying] = await task
            except Exception:
                self._log.exception(
                    "cache_multi_fetch_failed",
                    underlying=underlying,
                )
                results[underlying] = []

        return results

    async def refresh(self, underlying: str) -> list[OptionContract]:
        """Force a re-fetch and cache update for *underlying*.

        Unlike ``get()``, this always calls the exchange adapter regardless
        of cache state.
        """
        try:
            data = await self._exchange.get_option_chain(underlying)
        except Exception:
            self._log.exception(
                "cache_refresh_failed",
                underlying=underlying,
            )
            raise

        self._cache[underlying] = _CacheEntry(
            data=data,
            fetched_at=time.monotonic(),
            ttl=self._default_ttl,
        )
        return data

    def invalidate(self, underlying: str) -> None:
        """Mark the entry for *underlying* as stale.

        The next call to ``get()`` will re-fetch.
        """
        self._cache.pop(underlying, None)
        self._log.debug("cache_invalidated", underlying=underlying)

    def invalidate_all(self) -> None:
        """Clear the entire cache."""
        count = len(self._cache)
        self._cache.clear()
        self._log.debug("cache_invalidated_all", entries_removed=count)

    def is_stale(self, underlying: str) -> bool:
        """Return ``True`` if the entry for *underlying* needs a refresh."""
        entry = self._cache.get(underlying)
        if entry is None:
            return True
        return self._is_expired(entry)

    def get_cached_symbols(self) -> set[str]:
        """Return the set of symbols currently in the cache."""
        return set(self._cache.keys())

    def stats(self) -> dict:
        """Return cache statistics.

        Returns
        -------
        dict
            Keys: ``hits``, ``misses``, ``expired``, ``symbols_cached``.
        """
        return {
            "hits": self._hits,
            "misses": self._misses,
            "expired": self._expired,
            "symbols_cached": len(self._cache),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_expired(self, entry: _CacheEntry) -> bool:
        """Check whether a cache entry has exceeded its TTL."""
        elapsed = time.monotonic() - entry.fetched_at
        return elapsed >= entry.ttl

    def _get_or_create_lock(self, key: str) -> asyncio.Lock:
        """Return the per-key lock, creating one if it does not exist."""
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]
