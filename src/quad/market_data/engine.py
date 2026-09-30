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
            compatible with Bybit USDT perpetual (``BybitFuturesAdapter``).
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

        # Per-cache receive timestamps (monotonic seconds) backing max_age /
        # staleness checks on the cached getters below.
        self._cache_ts: dict[str, dict[str, float]] = {
            "order_book": {},
            "funding_rate": {},
            "mark_price": {},
            "ticker": {},
        }

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
        Subscribes to Bybit V5 linear market data topics:

        * ``tickers.{symbol}`` — 24h ticker incl. mark price + funding rate
        * ``orderbook.25.{symbol}`` — top-25 bids/asks
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

        # Subscribe to Bybit V5 linear market data topics
        ok = True
        try:
            # tickers.{symbol} carries last/mark/funding in one feed, so a
            # single subscription per symbol serves both the 24h-ticker and
            # the mark-price/funding caches (one combined handler).
            for symbol in self._symbols:
                await self._ws_manager.subscribe(
                    CHANNEL_TICKERS,
                    symbol,
                    self._handle_ticker_and_mark,
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
            ok = False
            self._log.exception("futures_stream_subscription_failed")

        if not ok:
            self._log.critical("market_data_subscriptions_failed")
            raise RuntimeError("market_data_subscriptions_failed")

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
            Kline interval (default ``"1m"``).  Bybit V5 uses ``"1"``,
            ``"5"``, ``"15"``, ``"60"``, ``"D"``, ``"W"`` (mapped from the
            ``candle{interval}`` channel name).
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
            # Logical candle channel; the WS manager maps it to Bybit kline topics.
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
        symbols: list[str] | None = None,
    ) -> str:
        """Subscribe to liquidation order events via WebSocket.

        Bybit only serves ``liquidation.{symbol}`` per-symbol topics (no
        wildcard ``liquidation.*``), so one subscription is opened per
        symbol.

        Parameters
        ----------
        handler:
            Async callback invoked with each decoded JSON message.
        symbols:
            Symbols to subscribe (defaults to the engine's configured
            symbols, or ``["BTCUSDT"]`` when none are configured).

        Returns
        -------
        str
            A subscription ID (from the last symbol subscribed).
        """
        if self._ws_manager is None:
            raise RuntimeError("MarketDataEngine not started. Call start() first.")
        targets = symbols or self._symbols or ["BTCUSDT"]
        sub_id = ""
        for sym in targets:
            sub_id = await self._ws_manager.subscribe(
                CHANNEL_LIQUIDATION_ORDERS,
                sym,
                handler,
            )
        return sub_id

    # ------------------------------------------------------------------
    # Futures market data accessors
    # ------------------------------------------------------------------

    def _touch(self, cache: str, symbol: str) -> None:
        """Record receipt time for a cache entry (staleness bookkeeping)."""
        self._cache_ts.setdefault(cache, {})[symbol] = time.monotonic()

    def is_stale(self, cache: str, symbol: str, max_age: float) -> bool:
        """Whether a cache entry is missing or older than ``max_age`` seconds.

        Parameters
        ----------
        cache:
            One of ``"order_book"``, ``"funding_rate"``, ``"mark_price"``,
            ``"ticker"``.
        symbol:
            The futures symbol.
        max_age:
            Maximum acceptable age in seconds.

        Returns
        -------
        bool
            ``True`` when no timestamp is recorded or the entry is older
            than ``max_age``.
        """
        ts = self._cache_ts.get(cache, {}).get(symbol)
        if ts is None:
            return True
        return (time.monotonic() - ts) > max_age

    def _fresh(self, cache: str, symbol: str, max_age: float | None) -> bool:
        """Whether the cache entry passes an optional ``max_age`` gate."""
        return max_age is None or not self.is_stale(cache, symbol, max_age)

    async def get_funding_rate(
        self, symbol: str, max_age: float | None = None
    ) -> FundingRate | None:
        """Return the latest funding rate for *symbol* from the cache.

        The funding rate cache is updated in real-time via the
        ``mark-price`` WebSocket channel.

        Parameters
        ----------
        symbol:
            The futures symbol (e.g. ``"BTCUSDT"``).
        max_age:
            Optional maximum entry age in seconds; when given and the
            cached entry is older (or missing), ``None`` is returned so
            callers never act on stale data.  Use :meth:`is_stale` with
            ``cache="funding_rate"`` to distinguish missing vs stale.

        Returns
        -------
        FundingRate | None
            ``None`` if no funding rate data has been received yet (or the
            entry is older than ``max_age``).
        """
        if not self._fresh("funding_rate", symbol, max_age):
            return None
        return self._funding_rate_cache.get(symbol)

    async def get_order_book(
        self, symbol: str, max_age: float | None = None
    ) -> dict | None:
        """Return the latest order book snapshot for *symbol* from the cache.

        The order book cache is updated in real-time via the
        ``books5`` WebSocket channel, which provides the top 5 bid/ask levels.

        Parameters
        ----------
        symbol:
            The futures symbol (e.g. ``"BTCUSDT"``).
        max_age:
            Optional maximum entry age in seconds; when given and the
            cached entry is older (or missing), ``None`` is returned so
            callers never act on stale data.  Use :meth:`is_stale` with
            ``cache="order_book"`` to distinguish missing vs stale.

        Returns
        -------
        dict | None
            A dict with keys ``bids``, ``asks``, and ``timestamp``,
            or ``None`` if no data has been received yet (or the entry is
            older than ``max_age``).
        """
        if not self._fresh("order_book", symbol, max_age):
            return None
        return self._order_book_cache.get(symbol)

    async def get_mark_price(
        self, symbol: str, max_age: float | None = None
    ) -> Decimal | None:
        """Return the latest mark price for *symbol* from the cache.

        The mark price cache is updated in real-time via the
        ``mark-price`` WebSocket channel.

        Parameters
        ----------
        symbol:
            The futures symbol (e.g. ``"BTCUSDT"``).
        max_age:
            Optional maximum entry age in seconds; when given and the
            cached entry is older (or missing), ``None`` is returned so
            callers never act on stale data.  Use :meth:`is_stale` with
            ``cache="mark_price"`` to distinguish missing vs stale.

        Returns
        -------
        Decimal | None
            ``None`` if no mark price data has been received yet (or the
            entry is older than ``max_age``).
        """
        if not self._fresh("mark_price", symbol, max_age):
            return None
        return self._mark_price_cache.get(symbol)

    async def get_ticker(
        self, symbol: str, max_age: float | None = None
    ) -> dict | None:
        """Return the latest 24h ticker for *symbol* from the cache.

        The ticker cache is updated in real-time via the
        ``tickers`` WebSocket channel.

        Parameters
        ----------
        symbol:
            The futures symbol (e.g. ``"BTCUSDT"``).
        max_age:
            Optional maximum entry age in seconds; when given and the
            cached entry is older (or missing), ``None`` is returned so
            callers never act on stale data.  Use :meth:`is_stale` with
            ``cache="ticker"`` to distinguish missing vs stale.

        Returns
        -------
        dict | None
            A dict with keys ``symbol``, ``last``, ``bid``, ``ask``,
            ``open24h``, ``high24h``, ``low24h``, ``vol24h``,
            ``volCcy24h``, and ``timestamp``,
            or ``None`` if no data has been received yet (or the entry is
            older than ``max_age``).
        """
        if not self._fresh("ticker", symbol, max_age):
            return None
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
    # WebSocket message handlers (Bybit V5 field names)
    # ------------------------------------------------------------------

    async def _handle_mark_price_update(self, message: dict) -> None:
        """Process ``mark-price`` WebSocket messages.

        Shares the Bybit ``tickers.{symbol}`` feed; Bybit fields:
        - ``symbol``: Contract symbol (e.g. "BTCUSDT")
        - ``markPrice``: Mark price
        - ``indexPrice``: Index price
        - ``fundingRate``: Estimated funding rate
        - ``nextFundingTime``: Next funding time (ms timestamp)
        """
        from quad.types.market import FundingRate

        data_list: list[dict] = message.get("data", [])

        for item in data_list:
            symbol: str = item.get("symbol", "")
            if not symbol:
                continue

            mark_price = Decimal(str(item.get("markPrice", "0")))
            index_price = Decimal(str(item.get("indexPrice", "0") or "0"))
            funding_rate_val = Decimal(str(item.get("fundingRate", "0")))
            next_funding_time: int = int(item.get("nextFundingTime", 0) or 0)

            self._mark_price_cache[symbol] = mark_price
            self._funding_rate_cache[symbol] = FundingRate(
                symbol=symbol,
                funding_rate=funding_rate_val,
                next_funding_time=next_funding_time,
                mark_price=mark_price,
                index_price=index_price or mark_price,
            )
            self._touch("mark_price", symbol)
            self._touch("funding_rate", symbol)

    async def _handle_ticker_and_mark(self, message: dict) -> None:
        """Combined handler for the single ``tickers.{symbol}`` feed.

        One subscription per symbol serves both the 24h-ticker cache (plus
        price buffer) and the mark-price/funding caches — no duplicate
        topic subscriptions.
        """
        await self._handle_ticker(message)
        await self._handle_mark_price_update(message)

    async def _handle_ticker(self, message: dict) -> None:
        """Process ``tickers`` WebSocket messages.

        Bybit V5 tickers fields:
        - ``symbol``: Contract symbol (e.g. "BTCUSDT")
        - ``lastPrice``: Last traded price
        - ``bid1Price`` / ``ask1Price``: Best bid/ask
        - ``highPrice24h`` / ``lowPrice24h``: 24h range
        - ``volume24h`` / ``turnover24h``: 24h volume / turnover
        """
        data_list: list[dict] = message.get("data", [])
        timestamp = int(message.get("ts", 0) or 0)

        for item in data_list:
            symbol: str = item.get("symbol", "")
            if not symbol:
                continue

            self._ticker_cache[symbol] = {
                "symbol": symbol,
                "last": item.get("lastPrice", "0"),
                "bid": item.get("bid1Price", "0"),
                "ask": item.get("ask1Price", "0"),
                "open24h": item.get("prevPrice24h", "0"),
                "high24h": item.get("highPrice24h", "0"),
                "low24h": item.get("lowPrice24h", "0"),
                "vol24h": item.get("volume24h", "0"),
                "volCcy24h": item.get("turnover24h", "0"),
                "timestamp": timestamp,
            }
            self._touch("ticker", symbol)

            # Feed last price into the price buffer
            if self._buffer is not None:
                last_price = Decimal(str(item.get("lastPrice", "0")))
                if last_price > Decimal(0):
                    await self._buffer.append(symbol, last_price)

    @staticmethod
    def _book_is_stale(item: dict, prev: dict) -> bool:
        """Whether an orderbook update is older than the cached snapshot.

        Bybit ``orderbook.25`` deltas carry ``seq`` and ``u`` (updateId);
        out-of-order delivery must not overwrite newer state.
        """
        try:
            seq, pseq = item.get("seq"), prev.get("seq")
            if seq is not None and pseq is not None and int(seq) < int(pseq):
                return True
            upd, pupd = item.get("u", item.get("updateId")), prev.get("update_id")
            if upd is not None and pupd is not None and int(upd) <= int(pupd):
                # Same-or-older updateId with no newer seq: stale/duplicate.
                if seq is None or pseq is None or int(seq) <= int(pseq):
                    return True
        except (TypeError, ValueError):
            return False
        return False

    async def _handle_book_ticker(self, message: dict) -> None:
        """Process ``books5`` WebSocket messages.

        Bybit V5 ``orderbook.25`` fields:
        - ``s``: Contract symbol (e.g. "BTCUSDT")
        - ``b``: Bids as [price, size] (snapshot or delta rows)
        - ``a``: Asks as [price, size]
        - ``u``/``updateId`` + ``seq``: versioning — stale updates are ignored.
        """
        data_list: list[dict] = message.get("data", [])
        timestamp = int(message.get("ts", 0) or 0)

        for item in data_list:
            symbol: str = item.get("s", item.get("symbol", ""))
            if not symbol:
                continue

            prev = self._order_book_cache.get(symbol, {})
            if prev and self._book_is_stale(item, prev):
                continue

            # Parse bids and asks (each is an array of [price, size])
            raw_bids = item.get("b", item.get("bids", []))
            raw_asks = item.get("a", item.get("asks", []))

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
                "timestamp": timestamp,
                "seq": item.get("seq"),
                "update_id": item.get("u", item.get("updateId")),
            }
            self._touch("order_book", symbol)

    async def _handle_kline_update(self, message: dict) -> None:
        """Process candle (kline) WebSocket messages.

        Bybit V5 kline fields:
        - ``symbol`` is carried in the topic (relayed via ``arg.symbol``)
        - ``start``: Candle open time (ms)
        - ``open`` / ``high`` / ``low`` / ``close``: Prices
        - ``volume``: Base-asset volume
        - ``confirm``: Whether the candle is closed
        """
        data_list: list[dict] = message.get("data", [])
        arg = message.get("arg", {}) if isinstance(message.get("arg"), dict) else {}
        topic_symbol = str(arg.get("symbol", ""))

        for item in data_list:
            symbol: str = item.get("symbol", topic_symbol)
            if not symbol:
                continue

            # Skip unconfirmed (still-forming) candles — only closed
            # candles (confirm=true, or no confirm flag) feed the buffer.
            if item.get("confirm") is False:
                continue

            close_price = item.get("close", "0")
            if self._buffer is not None and close_price:
                await self._buffer.append(symbol, Decimal(str(close_price)))

    async def _handle_liquidation_order(self, message: dict) -> None:
        """Process liquidation-orders WebSocket messages.

        Bybit V5 ``liquidation.{symbol}`` fields:
        - ``s``: Contract symbol (e.g. "BTCUSDT")
        - ``S``: Side ("Buy"/"Sell")
        - ``v``: Liquidated size
        - ``p``: Bankruptcy/execution price
        - ``t``: Timestamp (ms)
        """
        data_list: list[dict] = message.get("data", [])

        for item in data_list:
            symbol: str = item.get("s", item.get("symbol", ""))
            self._log.debug(
                "liquidation_event",
                symbol=symbol,
                side=item.get("S", item.get("side", "")),
                size=item.get("v", item.get("size", "")),
                price=item.get("p", item.get("price", "")),
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
                # Use the buffer's public accessor.  Reading _buffers
                # directly bypassed the class API and duplicated its logic.
                buffer_status = self._buffer.snapshot_counts()
            except Exception as exc:
                self._log.debug("buffer_status_unavailable", error=str(exc))

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
