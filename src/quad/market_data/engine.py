"""Central market data engine for Quad options trading bot.

The ``MarketDataEngine`` is the main orchestrator that coordinates:

* WebSocket subscription management (via :class:`WebSocketManager`)
* Real-time price buffering (via :class:`PriceBuffer`)
* Option chain caching (via :class:`OptionChainCache`)
* Historical data queries (via :class:`HistoricalDataProvider`)
* Health monitoring via ``status()``
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import structlog

from quad.market_data.buffers import PriceBuffer
from quad.market_data.cache import OptionChainCache
from quad.market_data.historical import HistoricalDataProvider
from quad.market_data.websocket import WebSocketManager

if TYPE_CHECKING:
    from quad.exchange.base import ExchangeAdapter
    from quad.persistence.database import DatabaseManager
    from quad.types.market import Candle, OptionContract, OptionPriceTick

logger = structlog.get_logger(__name__)

# Default grace period for component shutdowns (seconds)
_DEFAULT_SHUTDOWN_TIMEOUT_S = 10.0

# Default config values
_DEFAULT_CONFIG: dict[str, Any] = {
    "buffer_max_ticks": 1000,
    "cache_ttl": 60,
}


class MarketDataEngine:
    """Central market data engine.

    Coordinates WebSocket subscriptions, price buffering, option chain
    caching, and historical data queries into a single interface.

    Usage::

        engine = MarketDataEngine(exchange_adapter, config, db_manager)
        await engine.start()

        chain = await engine.get_option_chain("BTCUSDT")
        latest = await engine.get_latest_price("BTC-220930-20000-C")

        sub_id = await engine.subscribe_option(
            ["BTC-220930-20000-C"],
            my_tick_handler,
        )

        status = engine.status()
        await engine.stop()
    """

    def __init__(
        self,
        exchange_adapter: ExchangeAdapter,
        config: dict | None = None,
        db_manager: DatabaseManager | None = None,
    ) -> None:
        """Initialize the market data engine.

        Parameters
        ----------
        exchange_adapter:
            The exchange adapter used for live data fetching.
        config:
            Optional configuration dict.  Recognised keys:

            * ``buffer_max_ticks`` — max ticks per symbol (default 1000).
            * ``cache_ttl`` — option chain cache TTL in seconds (default 60).
            * ``ws_combined_url`` — WebSocket combined-stream URL.
            * ``shutdown_timeout`` — per-component shutdown grace period.
        db_manager:
            Database manager for historical data queries.  May be ``None``
            if historical queries are not needed (history stubs will be
            used instead).
        """
        merged = dict(_DEFAULT_CONFIG)
        if config:
            merged.update(config)

        self._exchange = exchange_adapter
        self._config = merged
        self._db_manager = db_manager
        self._log = logger.bind()

        # Sub-components (created in start())
        self._ws_manager: WebSocketManager | None = None
        self._buffer: PriceBuffer | None = None
        self._cache: OptionChainCache | None = None
        self._historical: HistoricalDataProvider | None = None

        # Lifecycle
        self._start_time: float | None = None
        self._running = False
        self._stop_event = asyncio.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialize all sub-components and begin processing.

        Creates and starts the WebSocket manager, price buffer, option
        chain cache, and historical data provider (if a database manager
        was provided).
        """
        if self._running:
            self._log.warning("already_running")
            return

        self._log.info("market_data_engine_starting")
        self._start_time = time.monotonic()
        self._stop_event.clear()

        # Create sub-components
        self._buffer = PriceBuffer(
            max_ticks_per_symbol=self._config.get("buffer_max_ticks", 1000),
        )
        self._cache = OptionChainCache(
            exchange_adapter=self._exchange,
            default_ttl=self._config.get("cache_ttl", 60),
        )
        if self._db_manager is not None:
            self._historical = HistoricalDataProvider(
                db_manager=self._db_manager,
            )
        else:
            self._historical = None
            self._log.info("historical_provider_disabled_no_db")

        self._ws_manager = WebSocketManager(
            exchange_adapter=self._exchange,
            config=self._config,
        )
        await self._ws_manager.start()

        self._running = True
        self._log.info("market_data_engine_started")

    async def stop(self) -> None:
        """Gracefully shut down all sub-components.

        Each component is given ``shutdown_timeout`` seconds (default 10)
        to complete its shutdown before the engine moves on.
        """
        if not self._running:
            return

        self._log.info("market_data_engine_stopping")
        self._running = False
        self._stop_event.set()

        timeout = self._config.get("shutdown_timeout", _DEFAULT_SHUTDOWN_TIMEOUT_S)

        # Stop WebSocket manager
        if self._ws_manager is not None:
            try:
                await asyncio.wait_for(
                    self._ws_manager.stop(),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                self._log.warning("ws_manager_stop_timeout")
            except Exception:
                self._log.exception("ws_manager_stop_error")

        self._log.info("market_data_engine_stopped")

    # ------------------------------------------------------------------
    # WebSocket subscriptions
    # ------------------------------------------------------------------

    async def subscribe_option(
        self,
        symbols: list[str],
        handler: Callable[[dict], Awaitable[None]],
    ) -> str:
        """Subscribe to real-time option price ticks via WebSocket.

        Parameters
        ----------
        symbols:
            List of option symbols (e.g. ``["BTC-220930-20000-C"]``).
            Each symbol is subscribed as a raw ticker stream.
        handler:
            Async callback invoked with each decoded JSON message.

        Returns
        -------
        str
            A subscription ID (from the last symbol subscribed).
        """
        if self._ws_manager is None:
            raise RuntimeError("MarketDataEngine not started. Call start() first.")

        sub_id = ""
        for sym in symbols:
            stream_name = f"{sym}@ticker"
            sub_id = await self._ws_manager.subscribe(stream_name, handler)

        self._log.debug(
            "subscribed_option",
            symbols=symbols,
            subscription_id=sub_id,
        )
        return sub_id

    async def subscribe_greeks(
        self,
        symbols: list[str],
        handler: Callable[[dict], Awaitable[None]],
    ) -> str:
        """Subscribe to real-time Greek updates via WebSocket.

        Uses the ``@{underlying}@optionMarkPrice`` stream which carries
        delta, gamma, theta, and vega alongside the mark price.

        Parameters
        ----------
        symbols:
            List of **underlying** symbols (e.g. ``["BTCUSDT", "ETHUSDT"]``).
            Greek streams are per-underlying, not per-option-contract.
        handler:
            Async callback invoked with each decoded JSON message.

        Returns
        -------
        str
            A subscription ID (from the last symbol subscribed).
        """
        if self._ws_manager is None:
            raise RuntimeError("MarketDataEngine not started. Call start() first.")

        sub_id = ""
        for sym in symbols:
            stream_name = f"{sym}@optionMarkPrice"
            sub_id = await self._ws_manager.subscribe(stream_name, handler)

        self._log.debug(
            "subscribed_greeks",
            symbols=symbols,
            subscription_id=sub_id,
        )
        return sub_id

    # ------------------------------------------------------------------
    # Option chain (cached)
    # ------------------------------------------------------------------

    async def get_option_chain(
        self,
        underlying: str,
    ) -> list[OptionContract]:
        """Return the full option chain for *underlying*.

        Uses the option chain cache, auto-refreshing if stale.

        Parameters
        ----------
        underlying:
            The underlying asset symbol (e.g. ``"BTCUSDT"``).
        """
        if self._cache is None:
            raise RuntimeError("MarketDataEngine not started. Call start() first.")
        return await self._cache.get(underlying)

    # ------------------------------------------------------------------
    # Price buffer
    # ------------------------------------------------------------------

    async def get_latest_price(
        self,
        symbol: str,
    ) -> OptionPriceTick | None:
        """Return the most recent price tick for *symbol*.

        Parameters
        ----------
        symbol:
            The option symbol (e.g. ``"BTC-220930-20000-C"``).
        """
        if self._buffer is None:
            return None
        return await self._buffer.get_latest(symbol)

    async def get_recent_prices(
        self,
        symbol: str,
        count: int = 10,
    ) -> list[OptionPriceTick]:
        """Return the last *count* price ticks for *symbol*.

        Parameters
        ----------
        symbol:
            The option symbol.
        count:
            How many ticks to return (most recent first).
        """
        if self._buffer is None:
            return []
        return await self._buffer.get_recent(symbol, count)

    # ------------------------------------------------------------------
    # Historical data
    # ------------------------------------------------------------------

    async def get_candles(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> list[Candle]:
        """Return historical OHLCV candles for *symbol*.

        .. note::
            Delegates to :class:`HistoricalDataProvider`.  Candle queries
            are a stub until the backtesting engine (Phase 9) is built.

        Parameters
        ----------
        symbol:
            The trading pair symbol.
        start:
            Inclusive start of the query window.
        end:
            Inclusive end of the query window.
        """
        from datetime import datetime

        if self._historical is None:
            self._log.warning("historical_provider_not_available")
            return []
        return await self._historical.get_candles(symbol, start, end)

    # ------------------------------------------------------------------
    # Health / status
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Return the full status of all sub-systems.

        Returns
        -------
        dict
            A nested dictionary with status for WebSocket, buffers, cache,
            and uptime.
        """
        ws_status: dict[str, Any] = {
            "active_subscriptions": 0,
            "total_reconnects": 0,
            "streams_active": 0,
        }
        if self._ws_manager is not None:
            s = self._ws_manager.status()
            ws_status["active_subscriptions"] = s.get("active_subscriptions", 0)
            ws_status["streams_active"] = s.get("streams_active", 0)
            rc = s.get("reconnect_counts", {})
            ws_status["total_reconnects"] = sum(rc.values()) if rc else 0

        buffer_status: dict[str, int] = {
            "symbols_tracked": 0,
            "total_ticks": 0,
        }
        if self._buffer is not None:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # Can't await in a sync method; use best-effort values
                    pass
            except RuntimeError:
                pass

        cache_stats: dict[str, int] = {
            "symbols_cached": 0,
            "hits": 0,
            "misses": 0,
        }
        if self._cache is not None:
            stats = self._cache.stats()
            cache_stats["symbols_cached"] = stats.get("symbols_cached", 0)
            cache_stats["hits"] = stats.get("hits", 0)
            cache_stats["misses"] = stats.get("misses", 0)

        uptime = (
            time.monotonic() - self._start_time
            if self._start_time is not None
            else 0.0
        )

        return {
            "websocket": ws_status,
            "buffers": buffer_status,
            "cache": cache_stats,
            "uptime_seconds": round(uptime, 2),
        }
