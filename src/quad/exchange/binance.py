"""Binance Options exchange adapter.

Connects to Binance Options via REST (``https://eapi.binance.com``) and
WebSocket (``wss://nbstream.binance.com/eoptions/ws/``).

Handles:

- REST API calls with HMAC SHA-256 authentication
- Rate-limit tracking via response headers
- WebSocket subscription management with auto-reconnect
- listenKey lifecycle management for user data streams
- Error handling with exponential-backoff retry

Usage::

    adapter = BinanceOptionsAdapter(
        api_key="your_api_key",
        api_secret="your_api_secret",
        testnet=False,
    )
    await adapter.connect()
    account = await adapter.get_account()
    ...

References:
    - Binance Options REST API:
      https://developers.binance.com/docs/derivatives/options-trading
    - Options WebSocket streams:
      https://developers.binance.com/docs/derivatives/options-trading/websocket-streams
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import aiohttp
import structlog

from quad.exchange.base import ExchangeAdapter
from quad.types.domain import (
    Account,
    Balance,
    Order,
    OrderRequest,
    OrderResult,
    Position,
    PositionSide,
    PositionStatus,
)
from quad.types.exchange import AccountUpdate
from quad.types.market import GreekTick, OptionContract, OptionPriceTick

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://eapi.binance.com"
TESTNET_BASE_URL = "https://testnet.binancefuture.com"

WS_BASE_URL = "wss://nbstream.binance.com/eoptions/ws"
WS_COMBINED_URL = "wss://nbstream.binance.com/eoptions/stream"

# HTTP header names used for rate-limit tracking
HEADER_USED_WEIGHT = "X-MBX-USED-WEIGHT-1M"
HEADER_ORDER_COUNT = "X-MBX-ORDER-COUNT-1M"

# Rate-limit safety margins (as fraction of configured max)
RATE_LIMIT_WARN_THRESHOLD = 0.80
RATE_LIMIT_HARD_THRESHOLD = 0.95

# WebSocket reconnection parameters
WS_BASE_BACKOFF_S = 1.0
WS_MAX_BACKOFF_S = 30.0
WS_BACKOFF_MULTIPLIER = 2.0
WS_JITTER_FACTOR = 0.1
WS_MAX_RETRIES = 10

# listenKey refresh interval (60 minutes per Binance docs; we use 55 for safety)
LISTEN_KEY_REFRESH_S = 55 * 60

# HTTP request timeout defaults
REQUEST_TIMEOUT_S = 30
CONNECT_TIMEOUT_S = 10

# ---------------------------------------------------------------------------
# Custom Exceptions
# ---------------------------------------------------------------------------


class ExchangeError(Exception):
    """Base exception for exchange errors."""


class ExchangeConnectionError(ExchangeError):
    """Raised when the exchange is unreachable."""


class ExchangeAuthError(ExchangeError):
    """Raised on authentication failure (401/403)."""


class ExchangeRateLimitError(ExchangeError):
    """Raised on 429 rate-limit breach."""


class ExchangeBannedError(ExchangeError):
    """Raised on 418 IP ban."""


class ExchangeOrderError(ExchangeError):
    """Raised on order-related errors."""


# ---------------------------------------------------------------------------
# Binance Options Adapter
# ---------------------------------------------------------------------------


class BinanceOptionsAdapter(ExchangeAdapter):
    """Full-featured Binance Options exchange adapter.

    Provides both REST and WebSocket connectivity with automatic
    reconnection, rate-limit tracking, and listenKey lifecycle
    management.

    Args:
        api_key: Binance API key.  May also be set via the
            ``BINANCE_API_KEY`` environment variable.
        api_secret: Binance API secret.  May also be set via the
            ``BINANCE_API_SECRET`` environment variable.
        testnet: If ``True``, use the testnet
            (``https://testnet.binancefuture.com``).
        rate_limit: Optional dict with ``max_weight`` and
            ``max_orders`` keys to configure rate-limit tracking.
        recv_window: Request validity window in milliseconds
            (default 5000).
    """

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        testnet: bool = False,
        rate_limit: dict | None = None,
        recv_window: int = 5000,
    ) -> None:
        self._log = logger.bind(adapter="binance")

        self._api_key: str = api_key or os.environ.get("BINANCE_API_KEY", "")
        self._api_secret: str = api_secret or os.environ.get(
            "BINANCE_API_SECRET", ""
        )
        self._testnet: bool = testnet
        self._recv_window: int = recv_window

        # Resolve base URLs
        self._rest_base: str = TESTNET_BASE_URL if testnet else BASE_URL
        self._ws_base: str = WS_BASE_URL
        self._ws_combined: str = WS_COMBINED_URL

        # Rate-limit tracking
        rl = rate_limit or {}
        self._max_weight: int = int(rl.get("max_weight", 2000))
        self._max_orders: int = int(rl.get("max_orders", 900))
        self._used_weight: int = 0
        self._used_orders: int = 0
        self._rate_limit_paused: bool = False
        self._rate_limit_pause_until: float = 0.0

        # HTTP session
        self._session: aiohttp.ClientSession | None = None

        # WebSocket state
        self._ws_connections: dict[str, aiohttp.ClientWebSocketResponse] = {}
        self._ws_tasks: dict[str, asyncio.Task[None]] = {}
        self._ws_subscriptions: dict[str, list[str]] = {}
        self._ws_close_events: dict[str, asyncio.Event] = {}

        # Price / Greek tick queues for async generators
        self._price_queue: asyncio.Queue[OptionPriceTick] = asyncio.Queue()
        self._greek_queue: asyncio.Queue[GreekTick] = asyncio.Queue()
        self._account_queue: asyncio.Queue[AccountUpdate] = asyncio.Queue()

        # listenKey state
        self._listen_key: str = ""
        self._listen_key_task: asyncio.Task[None] | None = None

        # Connection state
        self._connected: bool = False
        self._stop_event: asyncio.Event = asyncio.Event()

    # ======================================================================
    # Lifecycle
    # ======================================================================

    async def connect(self) -> None:
        """Create the HTTP session and test connectivity.

        This does not open any WebSocket connections — those are opened
        lazily by the ``subscribe_*`` methods.
        """
        if self._connected:
            return

        self._stop_event.clear()

        # Create aiohttp session
        timeout = aiohttp.ClientTimeout(
            total=REQUEST_TIMEOUT_S,
            connect=CONNECT_TIMEOUT_S,
        )
        self._session = aiohttp.ClientSession(
            timeout=timeout,
            headers=self._default_headers(),
        )

        # Test connectivity
        try:
            await self._request("GET", "/eapi/v1/ping", signed=False)
            server_time = await self.get_server_time()
            self._log.info(
                "binance_connected",
                testnet=self._testnet,
                server_time=server_time,
            )
        except Exception as exc:
            await self._safe_close_session()
            raise ExchangeConnectionError(
                f"Failed to connect to Binance: {exc}"
            ) from exc

        self._connected = True

    async def disconnect(self) -> None:
        """Close all connections gracefully."""
        self._stop_event.set()

        # Cancel WebSocket tasks
        for name, task in list(self._ws_tasks.items()):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._ws_tasks.clear()

        # Close WebSocket connections
        for name, ws in list(self._ws_connections.items()):
            try:
                await ws.close()
            except Exception:
                pass
        self._ws_connections.clear()

        # Stop listenKey refresh task
        if self._listen_key_task is not None:
            self._listen_key_task.cancel()
            try:
                await self._listen_key_task
            except (asyncio.CancelledError, Exception):
                pass
            self._listen_key_task = None

        # Close listenKey
        if self._listen_key:
            try:
                await self._request(
                    "DELETE", "/eapi/v1/listenKey", signed=False
                )
            except Exception:
                pass
            self._listen_key = ""

        await self._safe_close_session()
        self._connected = False
        self._log.info("binance_disconnected")

    @property
    def is_connected(self) -> bool:
        """Whether the HTTP session is active."""
        return self._connected and self._session is not None

    # ======================================================================
    # REST — Account & Positions
    # ======================================================================

    async def get_account(self) -> Account:
        """Fetch option margin account information.

        Calls ``GET /eapi/v1/marginAccount`` and maps the response
        into an ``Account`` dataclass.

        Returns:
            An ``Account`` with current balances.

        Raises:
            ExchangeAuthError: If API credentials are invalid.
            ExchangeConnectionError: If the exchange is unreachable.
        """
        data = await self._request("GET", "/eapi/v1/marginAccount")

        balances: dict[str, Balance] = {}
        total_usdt = Decimal("0")

        for asset_entry in data.get("asset", []):
            asset_name = asset_entry.get("asset", "")
            free = Decimal(str(asset_entry.get("available", "0")))
            locked_amount = Decimal(
                str(asset_entry.get("initialMargin", "0"))
            )
            equity = Decimal(str(asset_entry.get("equity", "0")))

            balances[asset_name] = Balance(
                asset=asset_name,
                free=free,
                locked=locked_amount,
            )
            total_usdt += equity

        account = Account(
            id=f"binance-{self._api_key[:8]}",
            exchange="binance",
            balances=balances,
            total_usdt=total_usdt.quantize(Decimal("0.01")),
            timestamp=data.get("time", int(time.time() * 1000)),
        )
        return account

    async def get_positions(self) -> list[Position]:
        """Fetch all open option positions.

        Calls ``GET /eapi/v1/position`` and maps the response to
        ``Position`` dataclasses.

        Returns:
            A list of open positions.
        """
        data = await self._request("GET", "/eapi/v1/position")

        positions: list[Position] = []
        for entry in data if isinstance(data, list) else []:
            pos = self._parse_position(entry)
            if pos is not None:
                positions.append(pos)

        return positions

    # ======================================================================
    # REST — Market Data
    # ======================================================================

    async def get_option_chain(
        self, underlying: str
    ) -> list[OptionContract]:
        """Fetch the option chain for an underlying asset.

        Fetches ``/eapi/v1/exchangeInfo`` to get all symbols, then
        filters by the given underlying.  Also fetches
        ``/eapi/v1/ticker`` and ``/eapi/v1/mark`` for current prices
        and Greeks.

        Args:
            underlying: The underlying asset, e.g. ``"BTCUSDT"``.

        Returns:
            A list of ``OptionContract`` objects.
        """
        # Fetch exchange info to get all symbols
        exchange_info = await self._request(
            "GET", "/eapi/v1/exchangeInfo", signed=False
        )

        # Fetch latest ticker data (prices)
        ticker_data = await self._request(
            "GET", "/eapi/v1/ticker", signed=False
        )
        ticker_map: dict[str, dict] = {}
        if isinstance(ticker_data, list):
            for t in ticker_data:
                ticker_map[t.get("symbol", "")] = t

        # Fetch mark price data (Greeks, IV)
        mark_data = await self._request(
            "GET", "/eapi/v1/mark", signed=False
        )
        mark_map: dict[str, dict] = {}
        if isinstance(mark_data, list):
            for m in mark_data:
                mark_map[m.get("symbol", "")] = m

        symbols_info = exchange_info.get("optionSymbols", [])
        contracts: list[OptionContract] = []

        for sym in symbols_info:
            sym_underlying = sym.get("underlying", "")
            if sym_underlying.upper() != underlying.upper():
                continue

            symbol = sym.get("symbol", "")
            ticker = ticker_map.get(symbol, {})
            mark = mark_map.get(symbol, {})

            option_type = sym.get("side", "").upper()
            if option_type not in ("CALL", "PUT"):
                continue

            contract = OptionContract(
                symbol=symbol,
                underlying=underlying.upper(),
                strike=Decimal(str(sym.get("strikePrice", "0"))),
                expiry=int(sym.get("expiryDate", 0)),
                option_type=option_type,  # type: ignore[arg-type]
                mark_price=Decimal(
                    str(mark.get("markPrice", "0"))
                ),
                bid=(
                    Decimal(str(ticker.get("bidPrice", "0")))
                    if ticker.get("bidPrice") not in (None, "")
                    else None
                ),
                ask=(
                    Decimal(str(ticker.get("askPrice", "0")))
                    if ticker.get("askPrice") not in (None, "")
                    else None
                ),
                volume=Decimal(str(ticker.get("volume", "0"))),
                open_interest=0,  # Requires separate endpoint
                implied_volatility=Decimal(
                    str(mark.get("markIV", "0"))
                ),
                delta=Decimal(str(mark.get("delta", "0"))),
                gamma=Decimal(str(mark.get("gamma", "0"))),
                theta=Decimal(str(mark.get("theta", "0"))),
                vega=Decimal(str(mark.get("vega", "0"))),
            )
            contracts.append(contract)

        return contracts

    # ======================================================================
    # REST — Order Management
    # ======================================================================

    async def place_order(self, request: OrderRequest) -> OrderResult:
        """Place an order on Binance Options.

        Calls ``POST /eapi/v1/order``.

        Args:
            request: The order parameters.

        Returns:
            An ``OrderResult`` with the exchange order ID.

        Raises:
            ExchangeOrderError: If the order is rejected.
            ExchangeRateLimitError: If rate limits are breached.
        """
        # Wait if rate-limited
        await self._wait_if_rate_limited()

        params: dict[str, Any] = {
            "symbol": request.symbol,
            "side": request.side.upper(),
            "type": request.type.upper(),
            "quantity": str(request.quantity),
        }

        if request.price is not None:
            params["price"] = str(request.price)
        if request.time_in_force:
            params["timeInForce"] = request.time_in_force.upper()
        if request.reduce_only:
            params["reduceOnly"] = True
        if request.post_only:
            params["postOnly"] = True
        if request.client_order_id:
            params["clientOrderId"] = request.client_order_id

        params["newOrderRespType"] = "ACK"

        data = await self._request("POST", "/eapi/v1/order", data=params)

        order_id = int(data.get("orderId", 0))
        status = data.get("status", "NEW")

        result = OrderResult(
            order_id=order_id,
            client_order_id=data.get("clientOrderId", ""),
            symbol=data.get("symbol", request.symbol),
            side=data.get("side", request.side),
            type=data.get("type", request.type),
            quantity=Decimal(
                str(data.get("origQty", str(request.quantity)))
            ),
            filled_qty=Decimal(str(data.get("executedQty", "0"))),
            price=(
                Decimal(str(data.get("price", "0")))
                if data.get("price") not in (None, "", "0")
                else request.price
            ),
            status=status,
            fills=self._parse_fills(data),
        )

        self._log.info(
            "order_placed",
            order_id=order_id,
            symbol=request.symbol,
            side=request.side,
            status=status,
        )
        return result

    async def cancel_order(self, order_id: int) -> bool:
        """Cancel an order on Binance Options.

        Calls ``DELETE /eapi/v1/order``.

        Args:
            order_id: The exchange order ID.

        Returns:
            ``True`` if the cancellation was accepted.
        """
        try:
            params: dict[str, Any] = {
                "orderId": order_id,
            }
            data = await self._request("DELETE", "/eapi/v1/order", data=params)
            status = data.get("status", "")
            self._log.info("order_cancelled", order_id=order_id, status=status)
            return True
        except ExchangeOrderError:
            return False
        except ExchangeError:
            return False

    async def get_order_status(self, order_id: int) -> Order:
        """Query a single order status.

        Calls ``GET /eapi/v1/order``.

        Args:
            order_id: The exchange order ID.

        Returns:
            An ``Order`` dataclass.

        Raises:
            ValueError: If the order is not found.
        """
        params: dict[str, Any] = {"orderId": order_id}
        data = await self._request("GET", "/eapi/v1/order", data=params)

        status = data.get("status", "")

        order = Order(
            id=int(data.get("orderId", 0)),
            client_order_id=data.get("clientOrderId", ""),
            symbol=data.get("symbol", ""),
            side=data.get("side", ""),
            type=data.get("type", ""),
            quantity=Decimal(str(data.get("origQty", "0"))),
            filled_qty=Decimal(str(data.get("executedQty", "0"))),
            price=(
                Decimal(str(data.get("price", "0")))
                if data.get("price") not in (None, "", "0")
                else None
            ),
            stop_price=None,
            status=status,
            time_in_force=data.get("timeInForce", "GTC"),
            created_at=int(data.get("updateTime", 0)),
            updated_at=int(data.get("updateTime", 0)),
        )
        return order

    async def get_open_orders(
        self, symbol: str | None = None
    ) -> list[Order]:
        """Query all open orders.

        Calls ``GET /eapi/v1/openOrders``.  Providing a ``symbol``
        reduces request weight from 40 to 1.

        Args:
            symbol: Optional symbol filter.

        Returns:
            A list of open ``Order`` objects.
        """
        params: dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol

        data = await self._request("GET", "/eapi/v1/openOrders", data=params)

        orders: list[Order] = []
        entries = data if isinstance(data, list) else []
        for entry in entries:
            orders.append(
                Order(
                    id=int(entry.get("orderId", 0)),
                    client_order_id=entry.get("clientOrderId", ""),
                    symbol=entry.get("symbol", ""),
                    side=entry.get("side", ""),
                    type=entry.get("type", ""),
                    quantity=Decimal(str(entry.get("origQty", "0"))),
                    filled_qty=Decimal(str(entry.get("executedQty", "0"))),
                    price=(
                        Decimal(str(entry.get("price", "0")))
                        if entry.get("price") not in (None, "", "0")
                        else None
                    ),
                    stop_price=None,
                    status=entry.get("status", ""),
                    time_in_force=entry.get("timeInForce", "GTC"),
                    created_at=int(entry.get("updateTime", 0)),
                    updated_at=int(entry.get("updateTime", 0)),
                )
            )
        return orders

    # ======================================================================
    # WebSocket — Market Data Streams
    # ======================================================================

    async def subscribe_option_prices(
        self, symbols: list[str]
    ) -> AsyncGenerator[OptionPriceTick, None]:
        """Subscribe to real-time price ticks via ``@ticker`` streams.

        Opens WebSocket connections for each symbol's ticker stream.
        Parses incoming JSON into ``OptionPriceTick`` objects and yields
        them.

        Args:
            symbols: List of option symbols to subscribe to.

        Yields:
            ``OptionPriceTick`` for each ticker update.
        """
        stream_names = [f"{s.lower()}@ticker" for s in symbols]
        await self._subscribe_ws_streams(stream_names)

        while not self._stop_event.is_set():
            try:
                tick = await asyncio.wait_for(
                    self._price_queue.get(), timeout=1.0
                )
                yield tick
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    async def subscribe_greeks(
        self, symbols: list[str]
    ) -> AsyncGenerator[GreekTick, None]:
        """Subscribe to real-time Greek updates via mark-price streams.

        Greeks are delivered through the ``@markPrice`` streams (by
        underlying) or via the ``@ticker`` stream where they are
        included.  This implementation uses the underlying-based mark
        price stream.

        Args:
            symbols: List of option symbols to subscribe to.  The
                underlying assets are extracted and subscribed to
                via ``<underlying>@markPrice``.

        Yields:
            ``GreekTick`` for each Greek update received.
        """
        # Extract unique underlyings from symbols
        # Symbol format: BTC-220930-20000-C -> underlying BTC
        underlyings: set[str] = set()
        for sym in symbols:
            parts = sym.split("-")
            if len(parts) >= 1:
                underlyings.add(parts[0].lower())

        stream_names = [f"{u}@markPrice" for u in underlyings]
        await self._subscribe_ws_streams(stream_names)

        while not self._stop_event.is_set():
            try:
                tick = await asyncio.wait_for(
                    self._greek_queue.get(), timeout=1.0
                )
                yield tick
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    async def subscribe_account_updates(
        self,
    ) -> AsyncGenerator[AccountUpdate, None]:
        """Subscribe to account/position updates via user data stream.

        Creates a listenKey, connects to the user data WebSocket, and
        refreshes the listenKey every 55 minutes.  Yields
        ``AccountUpdate`` objects for ``ACCOUNT_UPDATE`` events.

        Yields:
            ``AccountUpdate`` for each event.
        """
        await self._ensure_listen_key()

        # Start listenKey refresh loop
        self._listen_key_task = asyncio.create_task(
            self._listen_key_refresh_loop()
        )

        while not self._stop_event.is_set():
            try:
                update = await asyncio.wait_for(
                    self._account_queue.get(), timeout=1.0
                )
                yield update
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    # ======================================================================
    # Utility
    # ======================================================================

    async def get_exchange_info(self) -> dict:
        """Fetch exchange information (symbols, filters, rate limits).

        Calls ``GET /eapi/v1/exchangeInfo``.

        Returns:
            The full exchange info dict.
        """
        return await self._request("GET", "/eapi/v1/exchangeInfo", signed=False)

    async def get_server_time(self) -> int:
        """Fetch the current Binance server time.

        Calls ``GET /eapi/v1/time``.

        Returns:
            Server time in unix milliseconds.
        """
        data = await self._request("GET", "/eapi/v1/time", signed=False)
        return int(data.get("serverTime", 0))

    # ======================================================================
    # Internal — HTTP
    # ======================================================================

    def _default_headers(self) -> dict[str, str]:
        """Return default HTTP headers for Binance API requests.

        Returns:
            A dict with the API key header.
        """
        headers: dict[str, str] = {}
        if self._api_key:
            headers["X-MBX-APIKEY"] = self._api_key
        return headers

    def _sign_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """Add authentication parameters and sign the request.

        Adds ``timestamp`` and ``recvWindow``, then computes the HMAC
        SHA-256 signature.

        Args:
            params: The request parameters (modified in place).

        Returns:
            The params dict with timestamp, recvWindow, and signature.
        """
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = self._recv_window

        # Build query string sorted by key
        query_string = "&".join(
            f"{k}={v}" for k, v in sorted(params.items())
        )

        signature = hmac.new(
            self._api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        params["signature"] = signature
        return params

    async def _request(
        self,
        method: str,
        path: str,
        signed: bool = True,
        data: dict[str, Any] | None = None,
        max_retries: int = 3,
    ) -> Any:
        """Execute a REST API request with retry and rate-limit handling.

        Args:
            method: HTTP method (GET, POST, DELETE, PUT).
            path: API path, e.g. ``/eapi/v1/account``.
            signed: Whether the request requires HMAC signing.
            data: Optional query/body parameters.
            max_retries: Maximum number of retries on 5xx / timeout.

        Returns:
            The parsed JSON response.

        Raises:
            ExchangeConnectionError: On connection failure.
            ExchangeAuthError: On authentication failure.
            ExchangeRateLimitError: On rate limit breach.
            ExchangeBannedError: On IP ban.
        """
        if self._session is None or self._session.closed:
            raise ExchangeConnectionError("HTTP session is not available")

        params: dict[str, Any] = dict(data or {})

        if signed:
            self._sign_params(params)

        # Decide URL params: GET uses query string, POST uses body
        url = f"{self._rest_base}{path}"
        kwargs: dict[str, Any] = {}

        if method.upper() == "GET":
            kwargs["params"] = params
        else:
            kwargs["data"] = params

        last_error: Exception | None = None

        for attempt in range(max_retries + 1):
            try:
                # Wait if rate-limited
                await self._wait_if_rate_limited()

                async with self._session.request(
                    method.upper(), url, **kwargs
                ) as resp:
                    # Track rate limits from headers
                    self._update_rate_limits(resp.headers)

                    if resp.status == 429:
                        retry_after = self._parse_retry_after(
                            resp.headers
                        )
                        self._handle_rate_limit(retry_after)
                        continue

                    if resp.status == 418:
                        raise ExchangeBannedError(
                            "IP banned by Binance (418). "
                            "Check your rate-limit compliance."
                        )

                    if resp.status in (401, 403):
                        raise ExchangeAuthError(
                            f"Authentication failed ({resp.status}): "
                            f"{await resp.text()}"
                        )

                    if resp.status in (400, 404):
                        body = await resp.text()
                        raise ExchangeOrderError(
                            f"Order error ({resp.status}): {body}"
                        )

                    if resp.status >= 500:
                        if attempt < max_retries:
                            backoff = 2 ** attempt
                            self._log.warning(
                                "server_error_retrying",
                                status=resp.status,
                                attempt=attempt + 1,
                                backoff_s=backoff,
                            )
                            await asyncio.sleep(backoff)
                            continue
                        raise ExchangeConnectionError(
                            f"Server error ({resp.status}) after "
                            f"{max_retries} retries"
                        )

                    # Success
                    content_type = resp.headers.get(
                        "Content-Type", ""
                    )
                    if "application/json" in content_type:
                        return await resp.json()
                    body = await resp.text()
                    if not body:
                        return {}
                    return body

            except asyncio.TimeoutError as exc:
                last_error = exc
                if attempt < max_retries:
                    backoff = 2 ** attempt
                    self._log.warning(
                        "request_timeout_retrying",
                        attempt=attempt + 1,
                        backoff_s=backoff,
                    )
                    await asyncio.sleep(backoff)
                    continue
                raise ExchangeConnectionError(
                    f"Request timed out after {max_retries} retries"
                ) from exc

            except (aiohttp.ClientError, OSError) as exc:
                last_error = exc
                if attempt < max_retries:
                    backoff = 2 ** attempt
                    await asyncio.sleep(backoff)
                    continue
                raise ExchangeConnectionError(
                    f"HTTP error: {exc}"
                ) from exc

        # Should not reach here
        raise ExchangeConnectionError(
            f"Request failed after {max_retries} retries"
        )

    # ======================================================================
    # Internal — Rate Limiting
    # ======================================================================

    def _update_rate_limits(self, headers: Any) -> None:
        """Update rate-limit counters from response headers.

        Args:
            headers: The response headers (case-insensitive dict-like).
        """
        used_weight_str = headers.get(HEADER_USED_WEIGHT, "")
        if used_weight_str:
            try:
                self._used_weight = int(used_weight_str)
            except (ValueError, TypeError):
                pass

        order_count_str = headers.get(HEADER_ORDER_COUNT, "")
        if order_count_str:
            try:
                self._used_orders = int(order_count_str)
            except (ValueError, TypeError):
                pass

        # Log warning if approaching limits
        weight_pct = (
            self._used_weight / self._max_weight
            if self._max_weight > 0
            else 0
        )
        order_pct = (
            self._used_orders / self._max_orders
            if self._max_orders > 0
            else 0
        )

        if weight_pct >= RATE_LIMIT_WARN_THRESHOLD:
            self._log.warning(
                "rate_limit_approaching",
                used_weight=self._used_weight,
                max_weight=self._max_weight,
                pct=f"{weight_pct:.1%}",
            )

        if order_pct >= RATE_LIMIT_WARN_THRESHOLD:
            self._log.warning(
                "order_limit_approaching",
                used_orders=self._used_orders,
                max_orders=self._max_orders,
                pct=f"{order_pct:.1%}",
            )

    def _parse_retry_after(self, headers: Any) -> float:
        """Extract the ``Retry-After`` header value.

        Args:
            headers: Response headers.

        Returns:
            Seconds to wait before retrying (default 5).
        """
        raw = headers.get("Retry-After", "")
        try:
            return float(raw)
        except (ValueError, TypeError):
            return 5.0

    def _handle_rate_limit(self, retry_after: float) -> None:
        """Handle a 429 rate-limit response.

        Pauses all requests for ``retry_after`` seconds.

        Args:
            retry_after: Seconds to pause.
        """
        pause_until = time.time() + retry_after
        self._rate_limit_paused = True
        self._rate_limit_pause_until = pause_until

        self._log.warning(
            "rate_limit_hit",
            retry_after_s=retry_after,
            pause_until=pause_until,
        )

    async def _wait_if_rate_limited(self) -> None:
        """Wait if the rate-limit pause is active.

        Blocks the current coroutine until the pause expires.
        """
        if not self._rate_limit_paused:
            # Check if we're about to hit the limit
            if self._used_weight >= self._max_weight * RATE_LIMIT_HARD_THRESHOLD:
                wait = 1.0
                self._log.warning(
                    "rate_limit_throttling",
                    used_weight=self._used_weight,
                    max_weight=self._max_weight,
                    wait_s=wait,
                )
                await asyncio.sleep(wait)
            return

        remaining = self._rate_limit_pause_until - time.time()
        if remaining > 0:
            self._log.info(
                "rate_limit_waiting",
                remaining_s=round(remaining, 1),
            )
            await asyncio.sleep(remaining)

        self._rate_limit_paused = False

    # ======================================================================
    # Internal — WebSocket
    # ======================================================================

    async def _subscribe_ws_streams(
        self, stream_names: list[str]
    ) -> None:
        """Subscribe to one or more WebSocket streams.

        If only one stream, connects via raw WebSocket.  If multiple,
        uses the combined stream endpoint.

        Args:
            stream_names: List of stream names to subscribe to.
        """
        if not stream_names:
            return

        # Filter to streams not already subscribed
        new_streams = [
            s
            for s in stream_names
            if s not in self._ws_subscriptions
        ]

        if not new_streams:
            return

        for stream_name in new_streams:
            self._ws_subscriptions.setdefault(stream_name, [])

        # Start a task for each new stream
        for stream_name in new_streams:
            task = asyncio.create_task(
                self._ws_listen_loop(stream_name)
            )
            self._ws_tasks[stream_name] = task

        self._log.info(
            "ws_subscribed",
            new_streams=new_streams,
            total_streams=len(self._ws_subscriptions),
        )

    async def _ws_listen_loop(self, stream_name: str) -> None:
        """Listen on a single WebSocket stream with auto-reconnect.

        Runs until ``_stop_event`` is set.  On disconnect, reconnects
        with exponential backoff.

        Args:
            stream_name: The stream name (e.g. ``"btcusdt@ticker"``).
        """
        retries = 0

        while not self._stop_event.is_set():
            try:
                ws_url = f"{self._ws_base}/{stream_name}"
                async with self._session.ws_connect(
                    ws_url,
                    heartbeat=30.0,
                    autoclose=False,
                ) as ws:
                    self._ws_connections[stream_name] = ws
                    retries = 0  # Reset on successful connect

                    self._log.debug(
                        "ws_connected", stream=stream_name
                    )

                    async for msg in ws:
                        if self._stop_event.is_set():
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await self._handle_ws_message(
                                stream_name, msg.data
                            )
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            self._log.error(
                                "ws_error",
                                stream=stream_name,
                                error=ws.exception(),
                            )
                            break
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSING,
                        ):
                            break

            except asyncio.CancelledError:
                break
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
                if self._stop_event.is_set():
                    break

                retries += 1
                if retries > WS_MAX_RETRIES:
                    self._log.error(
                        "ws_max_retries_reached",
                        stream=stream_name,
                        retries=retries,
                    )
                    break

                backoff = min(
                    WS_BASE_BACKOFF_S
                    * (WS_BACKOFF_MULTIPLIER ** (retries - 1)),
                    WS_MAX_BACKOFF_S,
                )
                # Add jitter
                import random
                jitter = random.uniform(
                    0, backoff * WS_JITTER_FACTOR
                )
                total_wait = backoff + jitter

                self._log.warning(
                    "ws_reconnecting",
                    stream=stream_name,
                    retry=retries,
                    backoff_s=round(total_wait, 1),
                    error=str(exc),
                )
                await asyncio.sleep(total_wait)

            finally:
                self._ws_connections.pop(stream_name, None)

    async def _handle_ws_message(
        self, stream_name: str, raw: str
    ) -> None:
        """Parse and dispatch an incoming WebSocket message.

        Args:
            stream_name: The stream that produced this message.
            raw: The raw JSON string.
        """
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            self._log.warning("ws_invalid_json", stream=stream_name)
            return

        if not isinstance(data, dict):
            # Some streams return arrays (e.g. markPrice)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        self._dispatch_event(item)
            return

        self._dispatch_event(data)

    def _dispatch_event(self, data: dict[str, Any]) -> None:
        """Route a parsed WebSocket event to the correct queue.

        Args:
            data: The parsed event dict.
        """
        event_type = data.get("e", "")
        event_time = int(data.get("E", 0))

        if event_type in ("24hrTicker", "ticker"):
            self._handle_ticker_event(data)
        elif event_type == "markPrice":
            self._handle_mark_price_event(data)
        elif event_type == "ACCOUNT_UPDATE":
            self._handle_account_update_event(data)
        elif event_type == "optionSymbol":
            # New symbol notification — ignore in adapters
            pass
        else:
            # For streams that may not have event type (e.g. raw ticker)
            # Try to detect by field presence
            if "s" in data and "c" in data:
                # Looks like a 24hr ticker
                self._handle_ticker_event(data)

    def _handle_ticker_event(self, data: dict[str, Any]) -> None:
        """Parse a ticker event and enqueue an ``OptionPriceTick``.

        The 24hr ticker stream has the following fields:
        ``e`` (event type), ``E`` (event time), ``s`` (symbol),
        ``c`` (last price), ``b`` (bid price), ``a`` (ask price),
        ``v`` (volume).

        Args:
            data: The parsed ticker event dict.
        """
        try:
            tick = OptionPriceTick(
                symbol=data.get("s", ""),
                timestamp=int(data.get("E", 0)),
                bid=(
                    Decimal(str(data.get("b", "0")))
                    if data.get("b")
                    else None
                ),
                ask=(
                    Decimal(str(data.get("a", "0")))
                    if data.get("a")
                    else None
                ),
                last_price=(
                    Decimal(str(data.get("c", "0")))
                    if data.get("c")
                    else None
                ),
                volume=Decimal(str(data.get("v", "0"))),
            )
            self._price_queue.put_nowait(tick)
        except (ValueError, TypeError) as exc:
            self._log.debug(
                "ticker_parse_error",
                error=str(exc),
                data=data,
            )

    def _handle_mark_price_event(self, data: dict[str, Any]) -> None:
        """Parse a mark-price event and enqueue a ``GreekTick``.

        The mark-price stream fields:
        ``e`` (``"markPrice"``), ``E`` (event time), ``s`` (symbol),
        ``mp`` (mark price), ``d`` (delta), ``t`` (theta),
        ``g`` (gamma), ``v`` (vega), ``i`` (IV).

        Args:
            data: The parsed mark-price event dict.
        """
        try:
            tick = GreekTick(
                symbol=data.get("s", ""),
                timestamp=int(data.get("E", 0)),
                delta=Decimal(str(data.get("d", "0"))),
                gamma=Decimal(str(data.get("g", "0"))),
                theta=Decimal(str(data.get("t", "0"))),
                vega=Decimal(str(data.get("v", "0"))),
                rho=Decimal("0"),
            )
            self._greek_queue.put_nowait(tick)
        except (ValueError, TypeError) as exc:
            self._log.debug(
                "mark_price_parse_error",
                error=str(exc),
                data=data,
            )

    def _handle_account_update_event(
        self, data: dict[str, Any]
    ) -> None:
        """Parse an ACCOUNT_UPDATE event and enqueue an ``AccountUpdate``.

        Args:
            data: The parsed event dict.
        """
        try:
            # Parse balances from the event
            balances = data.get("B", {})
            account_balances: dict[str, Balance] = {}

            for entry in balances if isinstance(balances, list) else []:
                asset = entry.get("a", "")
                if asset:
                    account_balances[asset] = Balance(
                        asset=asset,
                        free=Decimal(str(entry.get("wb", "0"))),
                        locked=Decimal(str(entry.get("l", "0"))),
                    )

            account = Account(
                id=f"binance-{self._api_key[:8]}",
                exchange="binance",
                balances=account_balances,
                total_usdt=Decimal("0"),
                timestamp=int(data.get("E", 0)),
            )

            update = AccountUpdate(
                account=account,
                event_type="ACCOUNT_UPDATE",
                timestamp=int(data.get("E", 0)),
            )
            self._account_queue.put_nowait(update)

        except (ValueError, TypeError) as exc:
            self._log.debug(
                "account_update_parse_error",
                error=str(exc),
                data=data,
            )

    # ======================================================================
    # Internal — Session Management
    # ======================================================================

    async def _safe_close_session(self) -> None:
        """Close the aiohttp session if it is open.

        Safe to call multiple times — no-op after the first close.
        """
        if self._session is not None and not self._session.closed:
            try:
                await self._session.close()
            except Exception:
                pass
        self._session = None

    # ======================================================================
    # Internal — Position & Fill Parsing
    # ======================================================================

    def _parse_position(self, entry: dict[str, Any]) -> Position | None:
        """Parse a single position entry from the Binance API.

        Args:
            entry: A position dict from ``/eapi/v1/position``.

        Returns:
            A ``Position`` object, or ``None`` if the entry is empty.
        """
        symbol = entry.get("symbol", "")
        if not symbol:
            return None

        pos_amt = Decimal(str(entry.get("positionAmount", "0")))
        if pos_amt == Decimal("0"):
            return None

        side_str = entry.get("positionSide", "LONG").upper()
        side = (
            PositionSide.LONG
            if side_str == "LONG"
            else PositionSide.SHORT
        )

        return Position(
            contract_symbol=symbol,
            side=side,
            quantity=abs(pos_amt),
            entry_price=Decimal(str(entry.get("entryPrice", "0"))),
            current_price=Decimal(str(entry.get("markPrice", "0"))),
            unrealized_pnl=Decimal(str(entry.get("unrealizedProfit", "0"))),
            realized_pnl=Decimal(str(entry.get("realizedProfit", "0"))),
            updated_at=int(entry.get("updateTime", 0)),
        )

    def _parse_fills(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        """Parse fill entries from an order response.

        Args:
            data: The parsed order response dict.

        Returns:
            A list of fill dicts with ``price``, ``qty``, ``commission``,
            and ``commission_asset`` keys.
        """
        fills_raw = data.get("fills", [])
        if not fills_raw:
            return []
        return [self._parse_fill(f) for f in fills_raw]

    @staticmethod
    def _parse_fill(fill: dict[str, Any]) -> dict[str, Any]:
        """Parse a single fill entry.

        Args:
            fill: A fill dict from the order response.

        Returns:
            A normalized fill dict.
        """
        return {
            "price": fill.get("price", "0"),
            "qty": fill.get("qty", "0"),
            "commission": fill.get("commission", "0"),
            "commission_asset": fill.get("commissionAsset", ""),
        }

    @staticmethod
    def _compute_fill_price(fills: list[dict[str, Any]]) -> Decimal:
        """Compute the volume-weighted average fill price.

        Args:
            fills: A list of fill dicts with ``price`` and ``qty``.

        Returns:
            The average price, or ``Decimal("0")`` if no fills.
        """
        total_qty = Decimal("0")
        total_cost = Decimal("0")
        for f in fills:
            qty = Decimal(str(f.get("qty", "0")))
            price = Decimal(str(f.get("price", "0")))
            total_qty += qty
            total_cost += qty * price
        if total_qty > Decimal("0"):
            return total_cost / total_qty
        return Decimal("0")

    # ======================================================================
    # Internal — Position Updates & Commission Estimation
    # ======================================================================

    def _update_position(
        self,
        position: Position,
        event_data: dict[str, Any],
    ) -> Position:
        """Update a position object with event-stream data.

        Args:
            position: The position to update.
            event_data: A dict with ``markPrice``, ``unrealizedProfit``,
                and ``updateTime`` fields.

        Returns:
            The updated position (same object).
        """
        position.current_price = Decimal(
            str(event_data.get("markPrice", "0"))
        )
        position.unrealized_pnl = Decimal(
            str(event_data.get("unrealizedProfit", "0"))
        )
        position.updated_at = int(event_data.get("updateTime", 0))
        return position

    @staticmethod
    def _estimate_commission(
        quantity: Decimal,
        price: Decimal,
        rate: Decimal = Decimal("0.0003"),
    ) -> Decimal:
        """Estimate the commission for an order.

        Binance Options commission is typically 0.03% of notional value.
        VIP tiers receive lower rates.

        Args:
            quantity: Number of contracts.
            price: Contract price.
            rate: Commission rate (default 0.03%).

        Returns:
            Estimated commission as a ``Decimal``.
        """
        return quantity * price * rate

    # ======================================================================
    # Internal — listenKey Management
    # ======================================================================

    async def _ensure_listen_key(self) -> str:
        """Create or retrieve the user data stream listenKey.

        If no listenKey exists, creates one via
        ``POST /eapi/v1/listenKey``, connects the user data WebSocket,
        and starts the listener task.

        Returns:
            The current listenKey string.

        Raises:
            ExchangeConnectionError: If listenKey creation fails.
        """
        if self._listen_key:
            return self._listen_key

        data = await self._request(
            "POST", "/eapi/v1/listenKey", signed=False
        )
        self._listen_key = data.get("listenKey", "")
        if not self._listen_key:
            raise ExchangeConnectionError(
                "Failed to create listenKey"
            )

        # Connect to user data WebSocket
        try:
            ws_url = f"{self._ws_base}/{self._listen_key}"
            ws = await self._session.ws_connect(
                ws_url, heartbeat=30.0, autoclose=False,
            )
            self._ws_connections["user_data"] = ws

            # Start background listener
            self._ws_tasks["user_data"] = asyncio.create_task(
                self._user_data_listener()
            )
        except Exception as exc:
            raise ExchangeConnectionError(
                f"Failed to connect user data stream: {exc}"
            ) from exc

        self._log.info("listen_key_created")
        return self._listen_key

    async def _listen_key_refresh_loop(self) -> None:
        """Periodically refresh the listenKey to keep the stream alive.

        Refreshes every ``LISTEN_KEY_REFRESH_S`` (55 minutes) until
        ``_stop_event`` is set.
        """
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(LISTEN_KEY_REFRESH_S)
                await self._request(
                    "PUT",
                    "/eapi/v1/listenKey",
                    data={"listenKey": self._listen_key},
                )
                self._log.debug("listen_key_refreshed")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._log.error(
                    "listen_key_refresh_failed",
                    error=str(exc),
                )

    async def _user_data_listener(self) -> None:
        """Listen on the user data WebSocket and dispatch events.

        Runs until the connection drops or ``_stop_event`` is set.
        Reconnects are handled by re-creating the listenKey from
        ``subscribe_account_updates``.
        """
        ws = self._ws_connections.get("user_data")
        if ws is None:
            return

        try:
            async for msg in ws:
                if self._stop_event.is_set():
                    break
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        self._handle_user_data_event(data)
                    except json.JSONDecodeError:
                        self._log.warning(
                            "user_data_invalid_json"
                        )
                elif msg.type in (
                    aiohttp.WSMsgType.ERROR,
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                ):
                    break
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._log.error(
                "user_data_stream_error",
                error=str(exc),
            )

    def _handle_user_data_event(self, data: dict[str, Any]) -> None:
        """Dispatch a user data stream event.

        Currently handles ``ACCOUNT_UPDATE`` events, forwarding them
        to ``_handle_account_update_event``.

        Args:
            data: The parsed event dict.
        """
        event_type = data.get("e", "")
        if event_type == "ACCOUNT_UPDATE":
            self._handle_account_update_event(data)
