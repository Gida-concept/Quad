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
from quad.market_data.websocket import (
    CHANNEL_BOOKS5,
    CHANNEL_CANDLE,
    CHANNEL_LIQUIDATION_ORDERS,
    CHANNEL_MARK_PRICE,
    CHANNEL_TICKERS,
    WebSocketManager,
)

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

        funding = await engine.get_funding_rate("BTC-USDT-SWAP")
        book = await engine.get_order_book("BTC-USDT-SWAP")
        mark = await engine.get_mark_price("BTC-USDT-SWAP")

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
            compatible with OKX USDT perpetual (e.g. ``OkxFuturesAdapter``).
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

        # Symbols to subscribe to (from config)
        self._symbols: list[str] = []

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
        Subscribes to OKX V5 futures market data channels:

        * ``tickers`` — 24h ticker for all symbols
        * ``mark-price`` — mark price + funding rate updates
        * ``books5`` — top 5 order book levels (best bid/ask)
        """
        if self._running:
            self._log.warning("already_running")
            return

        self._log.info("market_data_engine_starting")
        self._start_time = time.monotonic()
        self._stop_event.clear()

        # Get configured symbols
        self._symbols = self._market_data_config.get("symbols", [])

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
        # Override WebSocket URL with the adapter's testnet-aware URL
        self._ws_manager._ws_url = self._exchange.public_ws_url
        await self._ws_manager.start()

        # Subscribe to OKX V5 futures market data channels
        try:
            # Subscribe to tickers for all configured symbols
            for symbol in self._symbols:
                await self._ws_manager.subscribe(
                    CHANNEL_TICKERS,
                    symbol,
                    self._handle_ticker,
                )

            # Subscribe to mark-price for all configured symbols
            for symbol in self._symbols:
                await self._ws_manager.subscribe(
                    CHANNEL_MARK_PRICE,
                    symbol,
                    self._handle_mark_price_update,
                )

            # Subscribe to books5 for all configured symbols
            for symbol in self._symbols:
                await self._ws_manager.subscribe(
                    CHANNEL_BOOKS5,
                    symbol,
                    self._handle_book_ticker,
                )

            self._log.info(
                "futures_market_data_streams_subscribed",
                symbols=self._symbols,
            )
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
            List of futures symbols (e.g. ``["BTC-USDT-SWAP", "ETH-USDT-SWAP"]``).
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
            sub_id = await self._ws_manager.subscribe(
                CHANNEL_TICKERS,
                sym,
                handler,
            )

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
            Kline interval (default ``"1m"``).  OKX V5 uses formats like
            ``"1m"``, ``"5m"``, ``"1H"``, ``"1D"``.
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
            # OKX V5 candle channel format: "candle1m", "candle5m", "candle1H", etc.
            channel = f"{CHANNEL_CANDLE}{interval}"
            sub_id = await self._ws_manager.subscribe(
                channel,
                sym,
                handler,
            )

        self._log.debug(
            "subscribed_kline",
            symbols=symbols,
            interval=interval,
            subscription_id=sub_id,
        )
        return sub_id

    async def subscribe_liquidations(
        self,
        handler: Callable[[dict], Awaitable[None]],
    ) -> str:
        """Subscribe to liquidation order events via WebSocket.

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
            CHANNEL_LIQUIDATION_ORDERS,
            "*",  # Subscribe to all instruments
            handler,
        )

    # ------------------------------------------------------------------
    # Futures market data accessors
    # ------------------------------------------------------------------

    async def get_funding_rate(self, symbol: str) -> FundingRate | None:
        """Return the latest funding rate for *symbol* from the cache.

        The funding rate cache is updated in real-time via the
        ``mark-price`` WebSocket channel.

        Parameters
        ----------
        symbol:
            The futures symbol (e.g. ``"BTC-USDT-SWAP"``).

        Returns
        -------
        FundingRate | None
            ``None`` if no funding rate data has been received yet.
        """
        return self._funding_rate_cache.get(symbol)

    async def get_order_book(self, symbol: str) -> dict | None:
        """Return the latest order book snapshot for *symbol* from the cache.

        The order book cache is updated in real-time via the
        ``books5`` WebSocket channel, which provides the top 5 bid/ask levels.

        Parameters
        ----------
        symbol:
            The futures symbol (e.g. ``"BTC-USDT-SWAP"``).

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
        ``mark-price`` WebSocket channel.

        Parameters
        ----------
        symbol:
            The futures symbol (e.g. ``"BTC-USDT-SWAP"``).

        Returns
        -------
        Decimal | None
            ``None`` if no mark price data has been received yet.
        """
        return self._mark_price_cache.get(symbol)

    async def get_ticker(self, symbol: str) -> dict | None:
        """Return the latest 24h ticker for *symbol* from the cache.

        The ticker cache is updated in real-time via the
        ``tickers`` WebSocket channel.

        Parameters
        ----------
        symbol:
            The futures symbol (e.g. ``"BTC-USDT-SWAP"``).

        Returns
        -------
        dict | None
            A dict with keys ``symbol``, ``last``, ``bid``, ``ask``,
            ``open24h``, ``high24h``, ``low24h``, ``vol24h``,
            ``volCcy24h``, and ``timestamp``,
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
            The futures symbol (e.g. ``"BTC-USDT-SWAP"``).
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
    # WebSocket message handlers (OKX V5 field names)
    # ------------------------------------------------------------------

    async def _handle_mark_price_update(self, message: dict) -> None:
        """Process ``mark-price`` WebSocket messages.

        OKX V5 mark-price fields:
        - ``instId``: Instrument ID (e.g. "BTC-USDT-SWAP")
        - ``instType``: Instrument type (e.g. "SWAP")
        - ``markPx``: Mark price
        - ``fundingRate``: Estimated funding rate
        - ``nextFundingTime``: Next funding time (ms timestamp)
        - ``ts``: Timestamp (ms)
        """
        from quad.types.market import FundingRate

        data_list: list[dict] = message.get("data", [])

        for item in data_list:
            symbol: str = item.get("instId", "")
            if not symbol:
                continue

            mark_price = Decimal(str(item.get("markPx", "0")))
            funding_rate_val = Decimal(str(item.get("fundingRate", "0")))
            next_funding_time: int = int(item.get("nextFundingTime", 0))
            timestamp: int = int(item.get("ts", 0))

            self._mark_price_cache[symbol] = mark_price
            self._funding_rate_cache[symbol] = FundingRate(
                symbol=symbol,
                funding_rate=funding_rate_val,
                next_funding_time=next_funding_time,
                mark_price=mark_price,
                index_price=mark_price,  # OKX doesn't provide index price in this channel
            )

    async def _handle_ticker(self, message: dict) -> None:
        """Process ``tickers`` WebSocket messages.

        OKX V5 tickers fields:
        - ``instId``: Instrument ID (e.g. "BTC-USDT-SWAP")
        - ``instType``: Instrument type (e.g. "SWAP")
        - ``last``: Last traded price
        - ``lastSz``: Last traded size
        - ``askPx``: Best ask price
        - ``askSz``: Best ask size
        - ``bidPx``: Best bid price
        - ``bidSz``: Best bid size
        - ``open24h``: Opening price (24h)
        - ``high24h``: Highest price (24h)
        - ``low24h``: Lowest price (24h)
        - ``vol24h``: Trading volume (24h, in contracts)
        - ``volCcy24h``: Trading volume (24h, in currency)
        - ``ts``: Timestamp (ms)
        """
        data_list: list[dict] = message.get("data", [])

        for item in data_list:
            symbol: str = item.get("instId", "")
            if not symbol:
                continue

            self._ticker_cache[symbol] = {
                "symbol": symbol,
                "last": item.get("last", "0"),
                "bid": item.get("bidPx", "0"),
                "ask": item.get("askPx", "0"),
                "open24h": item.get("open24h", "0"),
                "high24h": item.get("high24h", "0"),
                "low24h": item.get("low24h", "0"),
                "vol24h": item.get("vol24h", "0"),
                "volCcy24h": item.get("volCcy24h", "0"),
                "timestamp": int(item.get("ts", 0)),
            }

            # Feed last price into the price buffer
            if self._buffer is not None:
                last_price = Decimal(str(item.get("last", "0")))
                if last_price > Decimal(0):
                    await self._buffer.append(symbol, last_price)

    async def _handle_book_ticker(self, message: dict) -> None:
        """Process ``books5`` WebSocket messages.

        OKX V5 books5 fields:
        - ``instId``: Instrument ID (e.g. "BTC-USDT-SWAP")
        - ``bids``: Array of [price, size, count] for top 5 bid levels
        - ``asks``: Array of [price, size, count] for top 5 ask levels
        - ``ts``: Timestamp (ms)
        """
        data_list: list[dict] = message.get("data", [])

        for item in data_list:
            symbol: str = item.get("instId", "")
            if not symbol:
                continue

            # Parse bids and asks (each is an array of [price, size, count])
            raw_bids = item.get("bids", [])
            raw_asks = item.get("asks", [])

            bids = [
                (Decimal(str(bid[0])), Decimal(str(bid[1])))
                for bid in raw_bids
                if len(bid) >= 2
            ]
            asks = [
                (Decimal(str(ask[0])), Decimal(str(ask[1])))
                for ask in raw_asks
                if len(ask) >= 2
            ]

            self._order_book_cache[symbol] = {
                "bids": bids,
                "asks": asks,
                "timestamp": int(item.get("ts", 0)),
            }

    async def _handle_kline_update(self, message: dict) -> None:
        """Process candle (kline) WebSocket messages.

        OKX V5 candle fields:
        - ``instId``: Instrument ID (e.g. "BTC-USDT-SWAP")
        - ``ts``: Opening time (ms)
        - ``o``: Open price
        - ``h``: High price
        - ``l``: Low price
        - ``c``: Close price
        - ``vol``: Volume (in contracts)
        - ``volCcy``: Volume (in currency)
        - ``confirm``: 0 = incomplete, 1 = complete candle
        """
        data_list: list[dict] = message.get("data", [])

        for item in data_list:
            symbol: str = item.get("instId", "")
            if not symbol:
                continue

            close_price = item.get("c", "0")
            if self._buffer is not None and close_price:
                await self._buffer.append(symbol, Decimal(str(close_price)))

    async def _handle_liquidation_order(self, message: dict) -> None:
        """Process liquidation-orders WebSocket messages.

        OKX V5 liquidation-orders fields:
        - ``instId``: Instrument ID (e.g. "BTC-USDT-SWAP")
        - ``instType``: Instrument type (e.g. "SWAP")
        - ``ts``: Timestamp (ms)
        - ``underlying``: Underlying asset
        - ``bankruptPx``: Bankruptcy price
        - ``bankruptSz``: Bankruptcy size
        - ``side``: Side (buy/sell)
        - ``ok``: 0 = failed, 1 = success
        - ``ccy``: Currency
        - ``marginMode``: Margin mode (isolated/cross)
        """
        data_list: list[dict] = message.get("data", [])

        for item in data_list:
            symbol: str = item.get("instId", "")
            self._log.debug(
                "liquidation_event",
                symbol=symbol,
                side=item.get("side", ""),
                size=item.get("bankruptSz", ""),
                price=item.get("bankruptPx", ""),
                ok=item.get("ok", 0),
            )

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
            "channels_active": 0,
        }
        if self._ws_manager is not None:
            s = self._ws_manager.status()
            ws_status["active_subscriptions"] = s.get("active_subscriptions", 0)
            ws_status["channels_active"] = s.get("channels_active", 0)
            ws_status["total_reconnects"] = s.get("reconnect_count", 0)

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
