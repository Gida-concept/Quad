"""TTL caches for futures market data with stampede prevention.

Provides TTL-backed caches for funding rates, order books, mark prices, and
open interest data.  Each cache uses per-key :class:`asyncio.Lock` to prevent
duplicate API calls when multiple consumers request the same key simultaneously
(cache stampede).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Generic, TypeVar

import structlog

from quad.types.market import FundingRate

logger = structlog.get_logger(__name__)


# ============================================================================
# Generic cache entry
# ============================================================================


T = TypeVar("T")


@dataclass
class _CacheEntry(Generic[T]):
    """Internal cache entry holding fetched data and expiry metadata."""

    data: T
    fetched_at: float  # time.monotonic() timestamp
    ttl: int  # seconds


# ============================================================================
# Base class (shared TTL + stampede prevention pattern)
# ============================================================================


class _BaseTTLCache(Generic[T]):
    """Base TTL cache with per-key locks for stampede prevention.

    Subclasses implement ``_fetch(key)`` to populate cache entries on miss.
    """

    def __init__(
        self,
        default_ttl: int | None = None,
        config: dict | None = None,
    ) -> None:
        self._config = config or {}
        self._cache_config = self._config.get("market_data", {}).get("cache_ttl", {})
        if default_ttl is None:
            default_ttl = int(self._cache_config.get("default_ttl", 60))
        self._default_ttl = default_ttl
        self._cache: dict[str, _CacheEntry[T]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._hits = 0
        self._misses = 0
        self._expired = 0
        self._log = logger.bind(default_ttl=default_ttl)

    async def get(self, key: str) -> T | None:
        """Return cached data for *key*, fetching if stale or missing.

        Returns ``None`` if the fetch fails and no stale data is available.
        """
        entry = self._cache.get(key)
        if entry is not None and not self._is_expired(entry):
            self._hits += 1
            return entry.data

        lock = self._get_or_create_lock(key)
        async with lock:
            entry = self._cache.get(key)
            if entry is not None and not self._is_expired(entry):
                self._hits += 1
                return entry.data

            self._misses += 1
            if entry is not None:
                self._expired += 1

            try:
                data = await self._fetch(key)
            except Exception:
                self._log.exception("cache_fetch_failed", key=key)
                if entry is not None:
                    self._log.warning("cache_returning_stale_data", key=key)
                    return entry.data
                return None

            if data is not None:
                self._cache[key] = _CacheEntry(
                    data=data,
                    fetched_at=time.monotonic(),
                    ttl=self._default_ttl,
                )
            return data

    async def refresh(self, key: str) -> T | None:
        """Force a re-fetch and cache update for *key*.

        Unlike ``get()``, this always calls ``_fetch()`` regardless of
        cache state.
        """
        try:
            data = await self._fetch(key)
        except Exception:
            self._log.exception("cache_refresh_failed", key=key)
            raise

        if data is not None:
            self._cache[key] = _CacheEntry(
                data=data,
                fetched_at=time.monotonic(),
                ttl=self._default_ttl,
            )
        return data

    async def get_multi(self, keys: list[str]) -> dict[str, T | None]:
        """Fetch multiple keys concurrently.

        Returns a dict mapping each key to its cached data (or ``None`` on
        fetch failure).
        """
        results: dict[str, T | None] = {}
        tasks = {k: asyncio.create_task(self.get(k)) for k in keys}
        for key, task in tasks.items():
            try:
                results[key] = await task
            except Exception:
                self._log.exception("cache_multi_fetch_failed", key=key)
                results[key] = None
        return results

    def invalidate(self, key: str) -> None:
        """Mark the entry for *key* as stale."""
        self._cache.pop(key, None)
        self._log.debug("cache_invalidated", key=key)

    def invalidate_all(self) -> None:
        """Clear the entire cache."""
        count = len(self._cache)
        self._cache.clear()
        self._log.debug("cache_invalidated_all", entries_removed=count)

    def is_stale(self, key: str) -> bool:
        """Return ``True`` if the entry for *key* needs a refresh."""
        entry = self._cache.get(key)
        if entry is None:
            return True
        return self._is_expired(entry)

    def get_cached_keys(self) -> set[str]:
        """Return the set of keys currently in the cache."""
        return set(self._cache.keys())

    def stats(self) -> dict:
        """Return cache statistics.

        Returns
        -------
        dict
            Keys: ``hits``, ``misses``, ``expired``, ``keys_cached``.
        """
        return {
            "hits": self._hits,
            "misses": self._misses,
            "expired": self._expired,
            "keys_cached": len(self._cache),
        }

    async def _fetch(self, key: str) -> T | None:
        """Subclasses override this to fetch data on cache miss."""
        raise NotImplementedError

    def _is_expired(self, entry: _CacheEntry) -> bool:
        elapsed = time.monotonic() - entry.fetched_at
        return elapsed >= entry.ttl

    def _get_or_create_lock(self, key: str) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]


# ============================================================================
# FundingRateCache
# ============================================================================


class FundingRateCache(_BaseTTLCache[FundingRate]):
    """TTL-backed cache for funding rate data.

    Default TTL is 8 hours (28_800 seconds), matching Binance's 8-hour
    funding interval.  Stores :class:`FundingRate` objects keyed by symbol.
    """

    def __init__(
        self,
        exchange_adapter: Any = None,
        default_ttl: int | None = None,
        config: dict | None = None,
    ) -> None:
        if default_ttl is None:
            cfg = (config or {}).get("market_data", {}).get("cache_ttl", {})
            default_ttl = int(cfg.get("funding_rate", 28800))
        super().__init__(default_ttl=default_ttl, config=config)
        self._exchange = exchange_adapter
        self._log = logger.bind(cache="FundingRateCache", ttl=default_ttl)

    async def _fetch(self, key: str) -> FundingRate | None:
        """Fetch a funding rate from the exchange adapter."""
        if self._exchange is None:
            return None
        return await self._exchange.get_funding_rate(key)


# ============================================================================
# OrderBookCache
# ============================================================================


class OrderBookCache(_BaseTTLCache[dict]):
    """Shallow TTL cache for order book snapshots.

    Default TTL is 100ms (0.1 seconds) for near-real-time access.
    Stores order book data as dicts with ``bids`` and ``asks`` lists.
    """

    def __init__(
        self,
        exchange_adapter: Any = None,
        default_ttl: int | None = None,
        limit: int | None = None,
        config: dict | None = None,
    ) -> None:
        if default_ttl is None:
            cfg = (config or {}).get("market_data", {}).get("cache_ttl", {})
            default_ttl = int(cfg.get("order_book", 5))
        if limit is None:
            cfg = (config or {}).get("market_data", {}).get("cache_ttl", {})
            limit = int(cfg.get("order_book_limit", 20))
        super().__init__(default_ttl=default_ttl, config=config)
        self._exchange = exchange_adapter
        self._limit = limit
        self._log = logger.bind(cache="OrderBookCache", ttl=default_ttl, limit=limit)

    async def _fetch(self, key: str) -> dict | None:
        """Fetch an order book snapshot from the exchange adapter."""
        if self._exchange is None:
            return None
        return await self._exchange.get_order_book(key, limit=self._limit)


# ============================================================================
# MarkPriceCache
# ============================================================================


class MarkPriceCache(_BaseTTLCache[Decimal]):
    """Dict-backed cache for mark prices.

    No TTL-based eviction by default (mark prices update every second via
    WebSocket anyway).  Useful for REST-based fallback lookups.
    """

    def __init__(
        self,
        exchange_adapter: Any = None,
        default_ttl: int | None = None,
        config: dict | None = None,
    ) -> None:
        if default_ttl is None:
            cfg = (config or {}).get("market_data", {}).get("cache_ttl", {})
            default_ttl = int(cfg.get("mark_price", 2))
        super().__init__(default_ttl=default_ttl, config=config)
        self._exchange = exchange_adapter
        self._log = logger.bind(cache="MarkPriceCache", ttl=default_ttl)

    async def _fetch(self, key: str) -> Decimal | None:
        """Fetch a mark price from the exchange adapter."""
        if self._exchange is None:
            return None
        return await self._exchange.get_mark_price(key)


# ============================================================================
# OpenInterestCache
# ============================================================================


class OpenInterestCache(_BaseTTLCache[dict]):
    """Cache for open interest data (daily/hourly per symbol).

    Default TTL is 1 hour.  Stores open interest data as dicts with
    ``symbol``, ``open_interest``, ``timestamp``, and ``value`` keys.
    """

    def __init__(
        self,
        exchange_adapter: Any = None,
        default_ttl: int | None = None,
        config: dict | None = None,
    ) -> None:
        if default_ttl is None:
            cfg = (config or {}).get("market_data", {}).get("cache_ttl", {})
            default_ttl = int(cfg.get("open_interest", 3600))
        super().__init__(default_ttl=default_ttl, config=config)
        self._exchange = exchange_adapter
        self._log = logger.bind(cache="OpenInterestCache", ttl=default_ttl)

    async def _fetch(self, key: str) -> dict | None:
        """Fetch open interest from the exchange adapter."""
        if self._exchange is None:
            return None
        return await self._exchange.get_open_interest(key)
