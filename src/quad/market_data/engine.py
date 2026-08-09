"""Central market data engine for Quad futures trading bot.

The ``MarketDataEngine`` is the main orchestrator that coordinates:

* WebSocket subscription management (via :class:`WebSocketManager`)
* Real-time price buffering (via :class:`PriceBuffer`)
* Futures market data caches for order books, funding rates, mark prices, and
  24h tickers
* Historical data queries (via :class:`HistoricalDataProvider`)
* Health monitoring via ``status()``
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog

from quad.market_data.buffers import PriceBuffer
from quad.market_data.historical import HistoricalDataProvider
from quad.market_data.websocket import WebSocketManager

if TYPE_CHECKING:
    from quad.exchange.base import ExchangeAdapter
    from quad.persistence.database import DatabaseManager
    from quad.types.market import Candle, FundingRate

logger = structlog.get_logger(__name__)


class MarketDataEngine:
    """Central market data engine.

    Coordinates WebSocket subscriptions, price buffering, futures market data
    caches, and historical data queries into a single interface.

    Usage::

        engine = MarketDataEngine(exchange_adapter, config, db_manager)
        await engine.start()

        funding = await engine.get_funding_rate("BTCUSDT")
        book = await engine.get_order_book("BTCUSDT")
        mark = await engine.get_mark_price("BTCUSDT")

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
            The exchange adapter used for live data fetching.  Must be
            compatible with Binance Futures (e.g. ``BinanceFuturesAdapter``).
        config:
            Optional configuration dict.  Sub-keys under ``market_data``:

            * ``market_data.buffer_max_ticks`` — max price values per symbol (default 1000).
            * ``market_data.cache_ttl`` — cache TTL in seconds (default 60).
            * ``market_data.engine.shutdown_timeout`` — per-component grace period.
            * ``market_data.ws_url`` — WebSocket URL override.
        db_manager:
            Database manager for historical data queries.  May be ``None``
            if historical queries are not needed.
        """
        self._exchange = exchange_adapter
        self._config = config or {}
        self._market_data_config = self._config["market_data"]
        self._engine_config = self._market_data_config["engine"]
        self._db_manager = db_manager
        self._log = logger.bind()

        # Sub-components (created in start())
        self._ws_manager: WebSocketManager | None = None
        self._buffer: PriceBuffer | None = None
        self._historical: HistoricalDataProvider | None = None

        # Real-time caches (populated by WebSocket message handlers)
        self._order_book_cache: dict[str, dict] = {}
        """Maps symbol -> order book dict with keys: bids, asks, timestamp."""

        self._funding_rate_cache: dict[str, FundingRate] = {}
        """Maps symbol -> latest FundingRate dataclass."""

        self._mark_price_cache: dict[str, Decimal] = {}
        """Maps symbol -> latest mark price as Decimal."""

        self._ticker_cache: dict[str, dict] = {}
        """Maps symbol -> 24h mini ticker data dict."""

        # Lifecycle
        self._start_time: float | None = None
        self._running = False
        self._stop_event = asyncio.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialize all sub-components and begin processing.

        Creates and starts the WebSocket manager, price buffer, and
        historical data provider (if a database manager was provided).
        Subscribes to core futures market data streams:

        * ``!miniTicker@arr`` — 24h mini ticker for all symbols
        * ``!markPrice@arr@1s`` — mark price + funding rate array (1s)
        * ``!bookTicker`` — real-time best bid/ask for all symbols
        """
        if self._running:
            self._log.warning("already_running")
            return

        self._log.info("market_data_engine_starting")
        self._start_time = time.monotonic()
        self._stop_event.clear()

        # Create sub-components
        self._buffer = PriceBuffer(
            max_ticks_per_symbol=self._market_data_config["buffer_sizes"]["ticks"],
            config=self._config,
        )
        if self._db_manager is not None:
            self._historical = HistoricalDataProvider(
                db_manager=self._db_manager,
                exchange_adapter=self._exchange,
            )
        else:
            self._historical = None
            self._log.info("historical_provider_disabled_no_db")

        self._ws_manager = WebSocketManager(
            exchange_adapter=self._exchange,
            config=self._config,
        )
        await self._ws_manager.start()

        # Subscribe to futures market data streams
        try:
            await self._ws_manager.subscribe(
                "!miniTicker@arr",
                self._handle_mini_ticker,
            )
            await self._ws_manager.subscribe(
                "!markPrice@arr@1s",
                self._handle_mark_price_update,
            )
            await self._ws_manager.subscribe(
                "!bookTicker",
                self._handle_book_ticker,
            )
            self._log.info("futures_market_data_streams_subscribed")
        except Exception:
            self._log.exception("futures_stream_subscription_failed")

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

        timeout = float(self._engine_config["shutdown_timeout_seconds"])

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
    # WebSocket subscriptions (futures)
    # ------------------------------------------------------------------

    async def subscribe_ticker(
        self,
        symbols: list[str],
        handler: Callable[[dict], Awaitable[None]],
    ) -> str:
        """Subscribe to 1-hour ticker updates for *symbols* via WebSocket.

        Parameters
        ----------
        symbols:
            List of futures symbols (e.g. ``["BTCUSDT", "ETHUSDT"]``).
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
            stream_name = f"{sym}@ticker_1h"
            sub_id = await self._ws_manager.subscribe(stream_name, handler)

        self._log.debug(
            "subscribed_ticker",
            symbols=symbols,
            subscription_id=sub_id,
        )
        return sub_id

    async def subscribe_kline(
        self,
        symbols: list[str],
        interval: str = "1m",
        handler: Callable[[dict], Awaitable[None]] | None = None,
    ) -> str:
        """Subscribe to kline (candle) updates for *symbols* via WebSocket.

        If *handler* is ``None``, a default handler is used that feeds
        close prices into the :class:`PriceBuffer`.

        Parameters
        ----------
        symbols:
            List of futures symbols.
        interval:
            Kline interval (default ``"1m"``).
        handler:
            Async callback invoked with each decoded JSON message.

        Returns
        -------
        str
            A subscription ID.
        """
        if self._ws_manager is None:
            raise RuntimeError("MarketDataEngine not started. Call start() first.")

        if handler is None:
            handler = self._handle_kline_update

        sub_id = ""
        for sym in symbols:
            stream_name = f"{sym}@kline_{interval}"
            sub_id = await self._ws_manager.subscribe(stream_name, handler)

        self._log.debug(
            "subscribed_kline",
            symbols=symbols,
            interval=interval,
            subscription_id=sub_id,
        )
        return sub_id

    async def subscribe_force_order(
        self,
        handler: Callable[[dict], Awaitable[None]],
    ) -> str:
        """Subscribe to liquidation (force order) events via WebSocket.

        Parameters
        ----------
        handler:
            Async callback invoked with each decoded JSON message.

        Returns
        -------
        str
            A subscription ID.
        """
        if self._ws_manager is None:
            raise RuntimeError("MarketDataEngine not started. Call start() first.")
        return await self._ws_manager.subscribe(
            "!forceOrder@arr",
            handler,
        )

    # ------------------------------------------------------------------
    # Futures market data accessors
    # ------------------------------------------------------------------

    async def get_funding_rate(self, symbol: str) -> FundingRate | None:
        """Return the latest funding rate for *symbol* from the cache.

        The funding rate cache is updated in real-time via the
        ``!markPrice@arr@1s`` WebSocket stream.

        Parameters
        ----------
        symbol:
            The futures symbol (e.g. ``"BTCUSDT"``).

        Returns
        -------
        FundingRate | None
            ``None`` if no funding rate data has been received yet.
        """
        return self._funding_rate_cache.get(symbol)

    async def get_order_book(self, symbol: str) -> dict | None:
        """Return the latest order book snapshot for *symbol* from the cache.

        The order book cache is updated in real-time via the
        ``!bookTicker`` WebSocket stream, which provides the best bid/ask.

        Parameters
        ----------
        symbol:
            The futures symbol (e.g. ``"BTCUSDT"``).

        Returns
        -------
        dict | None
            A dict with keys ``bids``, ``asks``, and ``timestamp``,
            or ``None`` if no data has been received yet.
        """
        return self._order_book_cache.get(symbol)

    async def get_mark_price(self, symbol: str) -> Decimal | None:
        """Return the latest mark price for *symbol* from the cache.

        The mark price cache is updated in real-time via the
        ``!markPrice@arr@1s`` WebSocket stream.

        Parameters
        ----------
        symbol:
            The futures symbol (e.g. ``"BTCUSDT"``).

        Returns
        -------
        Decimal | None
            ``None`` if no mark price data has been received yet.
        """
        return self._mark_price_cache.get(symbol)

    async def get_ticker(self, symbol: str) -> dict | None:
        """Return the latest 24h mini ticker for *symbol* from the cache.

        The ticker cache is updated in real-time via the
        ``!miniTicker@arr`` WebSocket stream.

        Parameters
        ----------
        symbol:
            The futures symbol (e.g. ``"BTCUSDT"``).

        Returns
        -------
        dict | None
            A dict with keys ``symbol``, ``close``, ``open``, ``high``,
            ``low``, ``volume``, ``quote_volume``, and ``event_time``,
            or ``None`` if no data has been received yet.
        """
        return self._ticker_cache.get(symbol)

    # ------------------------------------------------------------------
    # Price buffer
    # ------------------------------------------------------------------

    async def get_latest_price(
        self,
        symbol: str,
    ) -> Decimal | None:
        """Return the most recent price for *symbol* from the price buffer.

        Parameters
        ----------
        symbol:
            The futures symbol (e.g. ``"BTCUSDT"``).
        """
        if self._buffer is None:
            return None
        return await self._buffer.get_latest(symbol)

    async def get_recent_prices(
        self,
        symbol: str,
        count: int = 10,
    ) -> list[Decimal]:
        """Return the last *count* prices for *symbol* (newest first).

        Parameters
        ----------
        symbol:
            The futures symbol.
        count:
            How many prices to return (most recent first).
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

        if self._historical is None:
            self._log.warning("historical_provider_not_available")
            return []
        return await self._historical.get_candles(symbol, start, end)

    # ------------------------------------------------------------------
    # WebSocket message handlers
    # ------------------------------------------------------------------

    async def _handle_mark_price_update(self, message: dict) -> None:
        """Process ``!markPrice@arr@1s`` WebSocket messages.

        Updates the mark price cache and funding rate cache with the
        latest data for each symbol in the array.
        """
        from quad.types.market import FundingRate

        data: Any = message.get("data", message)
        if isinstance(data, dict):
            data = [data]

        for item in data:
            symbol: str = item.get("s", "")
            if not symbol:
                continue

            mark_price = Decimal(str(item.get("p", "0")))
            index_price = Decimal(str(item.get("P", "0")))
            funding_rate_val = Decimal(str(item.get("r", "0")))
            next_funding_time: int = item.get("T", 0)

            self._mark_price_cache[symbol] = mark_price
            self._funding_rate_cache[symbol] = FundingRate(
                symbol=symbol,
                funding_rate=funding_rate_val,
                next_funding_time=next_funding_time,
                mark_price=mark_price,
                index_price=index_price,
            )

    async def _handle_mini_ticker(self, message: dict) -> None:
        """Process ``!miniTicker@arr`` WebSocket messages.

        Updates the ticker cache with 24h mini ticker data and feeds the
        close price into the price buffer.
        """
        data: Any = message.get("data", message)
        if isinstance(data, dict):
            data = [data]

        for item in data:
            symbol: str = item.get("s", "")
            if not symbol:
                continue

            self._ticker_cache[symbol] = {
                "symbol": symbol,
                "close": item.get("c", "0"),
                "open": item.get("o", "0"),
                "high": item.get("h", "0"),
                "low": item.get("l", "0"),
                "volume": item.get("v", "0"),
                "quote_volume": item.get("q", "0"),
                "event_time": item.get("E", 0),
            }

            # Feed close price into the price buffer
            if self._buffer is not None:
                close_price = Decimal(str(item.get("c", "0")))
                if close_price > Decimal(0):
                    await self._buffer.append(symbol, close_price)

    async def _handle_book_ticker(self, message: dict) -> None:
        """Process ``!bookTicker`` WebSocket messages.

        Updates the order book cache with the best bid/ask for each symbol.
        """
        data: Any = message.get("data", message)

        symbol: str = data.get("s", "")
        if not symbol:
            return

        self._order_book_cache[symbol] = {
            "bids": [
                (Decimal(str(data.get("b", "0"))), Decimal(str(data.get("B", "0"))))
            ],
            "asks": [
                (Decimal(str(data.get("a", "0"))), Decimal(str(data.get("A", "0"))))
            ],
            "timestamp": data.get("u", 0),
        }

    async def _handle_kline_update(self, message: dict) -> None:
        """Process individual kline (candle) updates.

        Parses the kline data from ``{symbol}@kline_{interval}`` streams
        and feeds the close price into the price buffer.
        """
        kline: dict = message.get("k", message)

        symbol: str = kline.get("s", "") or message.get("s", "")
        if not symbol or not isinstance(kline, dict):
            return

        close_price = kline.get("c", "0")
        if self._buffer is not None and close_price:
            await self._buffer.append(symbol, Decimal(str(close_price)))

    async def _handle_force_order(self, message: dict) -> None:
        """Process ``!forceOrder@arr`` liquidation events.

        Logs liquidation events for monitoring.  Currently a no-op
        placeholder for future risk management integration.
        """
        data: Any = message.get("data", message)
        order = data.get("o", data) if isinstance(data, dict) else data
        symbol: str = ""
        if isinstance(order, dict):
            symbol = order.get("s", "")
        self._log.debug("liquidation_event", symbol=symbol, data=order)

    # ------------------------------------------------------------------
    # Health / status
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Return the full status of all sub-systems.

        Returns
        -------
        dict
            A nested dictionary with status for WebSocket, buffers, caches,
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
                    pass
            except RuntimeError:
                pass

        uptime = (
            time.monotonic() - self._start_time if self._start_time is not None else 0.0
        )

        return {
            "websocket": ws_status,
            "buffers": buffer_status,
            "caches": {
                "symbols_in_order_book": len(self._order_book_cache),
                "funding_rates_cached": len(self._funding_rate_cache),
                "mark_prices_cached": len(self._mark_price_cache),
                "tickers_cached": len(self._ticker_cache),
            },
            "uptime_seconds": round(uptime, 2),
        }
