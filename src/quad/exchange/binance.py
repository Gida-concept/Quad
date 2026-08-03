"""Binance Futures exchange adapter.

Connects to Binance Futures via REST (``https://fapi.binance.com``) and
WebSocket (``wss://fstream.binance.com``).

Handles:

- REST API calls with HMAC SHA-256 authentication
- Rate-limit tracking via response headers
- WebSocket subscription management with auto-reconnect
- listenKey lifecycle management for user data streams
- Error handling with exponential-backoff retry

Usage::

    adapter = BinanceFuturesAdapter(
        api_key="your_api_key",
        api_secret="your_api_secret",
        testnet=False,
    )
    await adapter.connect()
    account = await adapter.get_account()
    ...

References:
    - Binance Futures REST API:
      https://developers.binance.com/docs/derivatives/usds-margined-futures
    - Futures WebSocket streams:
      https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-streams
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
    FuturesPosition,
    FuturesPositionSide,
    MarginType,
    Order,
    OrderRequest,
    OrderResult,
    Position,
    PositionSide,
    PositionStatus,
)
from quad.types.exchange import AccountUpdate
from quad.types.market import FundingRate, FuturesContract

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Default constants (fallback values when config is not provided)
# Instance properties on BinanceFuturesAdapter read from config and fall
# back to these defaults.
# ---------------------------------------------------------------------------

_BASE_URL = "https://fapi.binance.com"
_TESTNET_BASE_URL = "https://testnet.binancefuture.com"

_WS_BASE_URL = "wss://fstream.binance.com"
_WS_TESTNET_BASE_URL = "wss://stream.binancefuture.com"

_HEADER_USED_WEIGHT = "X-MBX-USED-WEIGHT-"
_HEADER_ORDER_COUNT = "X-MBX-ORDER-COUNT-"

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
# Binance Futures Adapter
# ---------------------------------------------------------------------------


class BinanceFuturesAdapter(ExchangeAdapter):
    """Full-featured Binance Futures exchange adapter.

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
        recv_window: int | None = None,
        config: dict | None = None,
    ) -> None:
        self._log = logger.bind(adapter="binance_futures")

        self._api_key: str = api_key or os.environ.get("BINANCE_API_KEY", "")
        self._api_secret: str = api_secret or os.environ.get(
            "BINANCE_API_SECRET", ""
        )
        self._testnet: bool = testnet
        self._config = config or {}
        self._exchange_config = self._config["exchange"]
        self._binance_config = self._exchange_config["binance"]
        self._recv_window: int = (
            recv_window if recv_window is not None
            else int(self._binance_config["recv_window"])
        )

        # Dry-run hard guard flag (top-level ``_dry_run`` config key).  When
        # set AND the exchange is live (``testnet=False``), place_order()
        # refuses every order to protect real funds.
        self._dry_run: bool = bool(self._config.get("_dry_run", False))
        # TTL for the exchange-info LOT_SIZE / MIN_NOTIONAL filter cache used
        # by normalize_quantity().
        self._exchange_info_ttl: float = float(
            self._binance_config.get("exchange_info_ttl_seconds", 60)
        )

        # Resolve base URLs
        self._rest_base: str = (
            self._binance_config["testnet_base_url"] if testnet
            else self._binance_config["base_url"]
        )
        self._ws_base: str = (
            self._binance_config["ws_testnet_base_url"] if testnet
            else self._binance_config["ws_base_url"]
        )

        # Rate-limit tracking
        rl = rate_limit or {}
        self._max_weight: int = int(rl.get("max_weight"))
        self._max_orders: int = int(rl.get("max_orders"))
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

        # Event queues for async generators
        self._price_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._mark_price_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._account_queue: asyncio.Queue[AccountUpdate] = asyncio.Queue()

        # listenKey state
        self._listen_key: str = ""
        self._listen_key_task: asyncio.Task[None] | None = None

        # Connection state
        self._connected: bool = False
        self._stop_event: asyncio.Event = asyncio.Event()

        # Server time offset (ms) — computed during connect() to compensate
        # for container clock drift on shared hosting platforms.
        self._time_offset: int = 0

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
            total=int(self._binance_config["request_timeout_seconds"]),
            connect=int(self._binance_config["connect_timeout_seconds"]),
        )
        self._session = aiohttp.ClientSession(
            timeout=timeout,
            headers=self._default_headers(),
        )

        # Test connectivity
        try:
            await self._request("GET", "/fapi/v1/ping", signed=False)
            server_time = await self.get_server_time()
            self._time_offset = server_time - int(time.time() * 1000)
            self._log.info(
                "binance_futures_connected",
                testnet=self._testnet,
                server_time=server_time,
                time_offset_ms=self._time_offset,
            )
        except Exception as exc:
            await self._safe_close_session()
            raise ExchangeConnectionError(
                f"Failed to connect to Binance Futures: {exc}"
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
                    "DELETE", "/fapi/v1/listenKey", signed=False
                )
            except Exception:
                pass
            self._listen_key = ""

        await self._safe_close_session()
        self._connected = False
        self._log.info("binance_futures_disconnected")

    @property
    def is_connected(self) -> bool:
        """Whether the HTTP session is active."""
        return self._connected and self._session is not None

    @property
    def is_testnet(self) -> bool:
        """Whether this adapter is pointed at the Binance Futures testnet."""
        return self._testnet

    # ======================================================================
    # REST — Account & Positions
    # ======================================================================

    async def get_account(self) -> Account:
        """Fetch futures account information.

        Calls ``GET /fapi/v2/account`` and maps the response
        into an ``Account`` dataclass.

        Returns:
            An ``Account`` with current balances.
        """
        data = await self._request("GET", "/fapi/v2/account")

        balances: dict[str, Balance] = {}
        for asset_entry in data.get("assets", []):
            asset_name = asset_entry.get("asset", "")
            if not asset_name:
                continue
            wallet_balance = Decimal(str(asset_entry.get("walletBalance", "0")))
            cross_unpnl = Decimal(str(asset_entry.get("crossUnPnl", "0")))
            free = wallet_balance + cross_unpnl
            balances[asset_name] = Balance(
                asset=asset_name,
                free=free,
                locked=Decimal(str(asset_entry.get("locked", "0"))),
            )

        total_wallet_balance = Decimal(str(data.get("totalWalletBalance", "0")))
        total_margin_balance = Decimal(str(data.get("totalMarginBalance", "0")))
        available_balance = Decimal(str(data.get("availableBalance", "0")))

        # Parse positions
        positions: list[FuturesPosition] = []
        for pos_entry in data.get("positions", []):
            pos_amt = float(pos_entry.get("positionAmt", 0))
            if abs(pos_amt) < 1e-8:
                continue
            side_str = pos_entry.get("positionSide", "BOTH")
            pos_side = (
                FuturesPositionSide.LONG if side_str == "LONG"
                else FuturesPositionSide.SHORT if side_str == "SHORT"
                else FuturesPositionSide.BOTH
            )
            positions.append(FuturesPosition(
                symbol=pos_entry.get("symbol", ""),
                position_side=pos_side,
                size=pos_amt,
                entry_price=float(pos_entry.get("entryPrice", 0)),
                mark_price=float(pos_entry.get("markPrice", 0)),
                liquidation_price=float(pos_entry.get("liquidationPrice", 0)),
                leverage=int(pos_entry.get("leverage", 1)),
                margin_type=MarginType.ISOLATED if pos_entry.get("isolated") else MarginType.CROSS,
                margin=float(pos_entry.get("isolatedWallet", 0)),
                unrealized_pnl=float(pos_entry.get("unrealizedProfit", 0)),
                realized_pnl=0.0,
                update_time=int(pos_entry.get("updateTime", 0)),
            ))

        account = Account(
            id=f"binance-{self._api_key[:8]}",
            exchange="binance",
            balances=balances,
            total_usdt=total_wallet_balance.quantize(Decimal("0.01")),
            timestamp=int(time.time() * 1000),
            max_leverage=1,
            total_wallet_balance=total_wallet_balance,
            total_margin_balance=total_margin_balance,
            available_balance=available_balance,
            positions=positions,
        )
        return account

    async def get_positions(self) -> list[Position]:
        """Fetch all open futures positions.

        Calls ``GET /fapi/v2/positionRisk`` and maps the response to
        ``Position`` dataclasses.

        Returns:
            A list of open positions.
        """
        data = await self._request("GET", "/fapi/v2/positionRisk")

        positions: list[Position] = []
        for entry in data if isinstance(data, list) else []:
            pos = self._parse_position(entry)
            if pos is not None:
                positions.append(pos)

        return positions

    # ======================================================================
    # REST — Futures Market Data
    # ======================================================================

    async def get_funding_rate(self, symbol: str) -> FundingRate:
        """Fetch current funding rate for a symbol.

        Calls ``GET /fapi/v1/fundingRate?symbol=...`` and
        ``GET /fapi/v1/premiumIndex?symbol=...``.

        Returns:
            A ``FundingRate`` dataclass.
        """
        # Get latest funding rate from premiumIndex (includes mark/ index price)
        premium = await self._request(
            "GET", f"/fapi/v1/premiumIndex", signed=False,
            data={"symbol": symbol},
        )
        return FundingRate(
            symbol=premium.get("symbol", symbol),
            funding_rate=Decimal(str(premium.get("lastFundingRate", "0"))),
            next_funding_time=int(premium.get("nextFundingTime", 0)),
            mark_price=Decimal(str(premium.get("markPrice", "0"))),
            index_price=Decimal(str(premium.get("indexPrice", "0"))),
        )

    async def get_mark_price(self, symbol: str) -> Decimal:
        """Fetch current mark price for a symbol.

        Calls ``GET /fapi/v1/premiumIndex?symbol=...``.

        Returns:
            Mark price as a ``Decimal``.
        """
        data = await self._request(
            "GET", "/fapi/v1/premiumIndex", signed=False,
            data={"symbol": symbol},
        )
        return Decimal(str(data.get("markPrice", "0")))

    async def get_klines(
        self, symbol: str, interval: str, limit: int = 500
    ) -> list[tuple[float, ...]]:
        """Fetch kline/candlestick data for a symbol.

        Calls ``GET /fapi/v1/klines`` (unsigned).

        Uses the adapter's own ``_request()`` method, which handles URL
        resolution (production vs. testnet), rate-limit tracking, retries,
        server time offset, and connection management.

        Args:
            symbol: Trading pair symbol, e.g. ``"BTCUSDT"``.
            interval: Kline interval, e.g. ``"15m"``, ``"1h"``.
            limit: Number of candles to fetch (max 1000, default 500).

        Returns:
            List of ``(open_time_s, open, high, low, close, volume)`` tuples.
            ``open_time_s`` is the open timestamp in seconds (float).
        """
        data = await self._request(
            "GET", "/fapi/v1/klines", signed=False,
            data={"symbol": symbol, "interval": interval, "limit": limit},
        )

        # Binance kline format: [open_time, open, high, low, close, volume, ...]
        results: list[tuple[float, ...]] = []
        for k in data:
            results.append((
                k[0] / 1000.0,  # open time in seconds
                float(k[1]),     # open
                float(k[2]),     # high
                float(k[3]),     # low
                float(k[4]),     # close
                float(k[5]),     # volume
            ))
        return results

    # ======================================================================
    # REST — Order Management
    # ======================================================================

    async def place_order(self, request: OrderRequest) -> OrderResult:
        """Place an order on Binance Futures.

        Calls ``POST /fapi/v1/order``.

        Args:
            request: The order parameters.

        Returns:
            An ``OrderResult`` with the exchange order ID.

        Raises:
            ExchangeOrderError: If the order is rejected.
            ExchangeRateLimitError: If rate limits are breached.
            RuntimeError: If a hard dry-run guard blocks the order, or the
                quantity fails exchange filter validation.
        """
        # 0. Hard dry-run guard -- refuse every order when dry-run mode is
        #    enabled but the exchange is LIVE.  This is the lowest choke
        #    point in the whole order path: every order (AI rotation, /ai
        #    telegram command, TradingView webhook, strategy actions,
        #    TP/SL brackets, close-all) funnels through place_order(), so
        #    this cannot be bypassed.
        if self._dry_run and not self._testnet:
            self._log.critical(
                "dry_run_guard_blocked_order",
                symbol=request.symbol,
                side=request.side,
                order_type=request.order_type,
                qty=str(request.quantity),
                dry_run=self._dry_run,
                testnet=self._testnet,
            )
            raise RuntimeError(
                "DRY_RUN_GUARD: dry-run mode is enabled but the exchange is "
                "LIVE (testnet=False). Refusing to place the order to protect "
                "real funds."
            )

        # 0b. Normalize quantity to the exchange's LOT_SIZE / MIN_NOTIONAL
        #     filters so no order is rejected for -1113/-1111/-4164.  The
        #     engine already normalizes too, but this is the authoritative
        #     last-line-of-defense (idempotent).
        quantity = await self.normalize_quantity(request.symbol, request.quantity)

        await self._wait_if_rate_limited()

        params: dict[str, Any] = {
            "symbol": request.symbol,
            "side": request.side.upper(),
            "type": request.order_type.upper(),
            "quantity": str(quantity),
        }

        if request.price is not None:
            params["price"] = str(request.price)
        if request.stop_price is not None:
            params["stopPrice"] = str(request.stop_price)
        if request.time_in_force:
            params["timeInForce"] = request.time_in_force.upper()
        if request.reduce_only:
            params["reduceOnly"] = "true"
        if request.post_only:
            params["postOnly"] = "true"
        if request.client_order_id:
            params["newClientOrderId"] = request.client_order_id
        if request.working_type:
            params["workingType"] = request.working_type
        if request.position_side:
            params["positionSide"] = request.position_side.upper()
        if request.price_protect:
            params["priceProtect"] = "true"

        params["newOrderRespType"] = self._binance_config["new_order_resp_type"]

        data = await self._request("POST", "/fapi/v1/order", data=params)

        order_id = int(data.get("orderId", 0))
        status = data.get("status", "NEW")

        result = OrderResult(
            order_id=order_id,
            client_order_id=data.get("clientOrderId", ""),
            symbol=data.get("symbol", request.symbol),
            side=data.get("side", request.side),
            order_type=data.get("type", request.order_type),
            quantity=Decimal(
                str(data.get("origQty", str(quantity)))
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
        """Cancel an order on Binance Futures.

        Calls ``DELETE /fapi/v1/order``.

        Args:
            order_id: The exchange order ID.

        Returns:
            ``True`` if the cancellation was accepted.
        """
        try:
            params: dict[str, Any] = {
                "orderId": order_id,
            }
            data = await self._request("DELETE", "/fapi/v1/order", data=params)
            status = data.get("status", "")
            self._log.info("order_cancelled", order_id=order_id, status=status)
            return True
        except ExchangeOrderError:
            return False
        except ExchangeError:
            return False

    async def get_order_status(self, order_id: int) -> Order:
        """Query a single order status.

        Calls ``GET /fapi/v1/order``.

        Args:
            order_id: The exchange order ID.

        Returns:
            An ``Order`` dataclass.

        Raises:
            ValueError: If the order is not found.
        """
        params: dict[str, Any] = {"orderId": order_id}
        data = await self._request("GET", "/fapi/v1/order", data=params)

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
            status=data.get("status", ""),
            time_in_force=data.get("timeInForce", "GTC"),
            created_at=int(data.get("updateTime", 0)),
            updated_at=int(data.get("updateTime", 0)),
            working_type=data.get("workingType", ""),
            position_side=data.get("positionSide", ""),
            price_protect=data.get("priceProtect", False),
        )
        return order

    async def get_open_orders(
        self, symbol: str | None = None
    ) -> list[Order]:
        """Query all open orders.

        Calls ``GET /fapi/v1/openOrders``.

        Args:
            symbol: Optional symbol filter.

        Returns:
            A list of open ``Order`` objects.
        """
        params: dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol

        data = await self._request("GET", "/fapi/v1/openOrders", data=params)

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
                    working_type=entry.get("workingType", ""),
                    position_side=entry.get("positionSide", ""),
                    price_protect=entry.get("priceProtect", False),
                )
            )
        return orders

    # ======================================================================
    # REST — Futures Configuration
    # ======================================================================

    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        """Set leverage for a symbol.

        Calls ``POST /fapi/v1/leverage``.

        Args:
            symbol: Trading pair symbol.
            leverage: Desired leverage (1-125).

        Returns:
            API response dict.
        """
        return await self._request(
            "POST", "/fapi/v1/leverage",
            data={"symbol": symbol, "leverage": leverage},
        )

    async def set_margin_mode(self, symbol: str, margin_type: str) -> dict:
        """Set margin mode (isolated/cross) for a symbol.

        Calls ``POST /fapi/v1/marginType``.

        Args:
            symbol: Trading pair symbol.
            margin_type: "ISOLATED" or "CROSS".

        Returns:
            API response dict.
        """
        return await self._request(
            "POST", "/fapi/v1/marginType",
            data={"symbol": symbol, "marginType": margin_type.upper()},
        )

    async def set_position_mode(self, mode: str) -> dict:
        """Set position mode (one_way/hedge).

        Calls ``POST /fapi/v1/positionSide/dual``.

        Args:
            mode: "one_way" or "hedge".

        Returns:
            API response dict.
        """
        dual = mode.lower() == "hedge"
        return await self._request(
            "POST", "/fapi/v1/positionSide/dual",
            data={"dualSidePosition": str(dual).lower()},
        )

    async def get_position_mode(self) -> str:
        """Get current position mode.

        Calls ``GET /fapi/v1/positionSide/dual``.

        Returns:
            "hedge" if dual position side is enabled, else "one_way".
        """
        data = await self._request(
            "GET", "/fapi/v1/positionSide/dual",
        )
        if isinstance(data, bool):
            return "hedge" if data else "one_way"
        return "hedge" if data.get("dualSidePosition") else "one_way"

    # ======================================================================
    # WebSocket — Market Data Streams
    # ======================================================================

    async def subscribe_market_data(
        self, symbols: list[str]
    ) -> AsyncGenerator[dict, None]:
        """Subscribe to real-time mini-ticker data.

        Uses the ``!miniTicker@arr`` stream for all symbols.

        Args:
            symbols: Ignored for the combined stream; all symbols
                are included in the array.

        Yields:
            Dict for each mini-ticker update received.
        """
        await self._subscribe_ws_streams(["!miniTicker@arr"])

        while not self._stop_event.is_set():
            try:
                data = await asyncio.wait_for(
                    self._price_queue.get(), timeout=1.0
                )
                yield data
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    async def subscribe_mark_prices(
        self,
    ) -> AsyncGenerator[dict, None]:
        """Subscribe to real-time mark price updates.

        Uses the ``!markPrice@arr@1s`` stream.

        Yields:
            Dict for each mark price update received.
        """
        await self._subscribe_ws_streams(["!markPrice@arr@1s"])

        while not self._stop_event.is_set():
            try:
                data = await asyncio.wait_for(
                    self._mark_price_queue.get(), timeout=1.0
                )
                yield data
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

        Calls ``GET /fapi/v1/exchangeInfo``.

        Returns:
            The full exchange info dict.
        """
        return await self._request("GET", "/fapi/v1/exchangeInfo", signed=False)

    async def get_server_time(self) -> int:
        """Fetch the current Binance server time.

        Calls ``GET /fapi/v1/time``.

        Returns:
            Server time in unix milliseconds.
        """
        data = await self._request("GET", "/fapi/v1/time", signed=False)
        return int(data.get("serverTime", 0))

    # ======================================================================
    # Internal — HTTP
    # ======================================================================

    def _default_headers(self) -> dict[str, str]:
        """Return default HTTP headers for Binance API requests."""
        headers: dict[str, str] = {}
        if self._api_key:
            headers["X-MBX-APIKEY"] = self._api_key
        return headers

    def _sign_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """Add authentication parameters and sign the request.

        Binance HMAC verification uses the query string as-is. Both signing
        and HTTP sending must use the *same* key ordering. aiohttp serialises
        dict keys in insertion order, so we add parameters in that order and
        sign over the same sequence without sorting.

        Uses server time offset (computed during connect()) to compensate
        for container clock drift on shared hosting platforms.
        """
        params["timestamp"] = int(time.time() * 1000) + self._time_offset
        params["recvWindow"] = self._recv_window

        query_string = "&".join(
            f"{k}={v}" for k, v in params.items()  # insertion order, NOT sorted
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
        max_retries: int | None = None,
    ) -> Any:
        """Execute a REST API request with retry and rate-limit handling."""
        if max_retries is None:
            max_retries = int(self._binance_config["max_retries"])
        retry_backoff_base = float(
            self._binance_config["retry_backoff_base"]
        )

        if self._session is None or self._session.closed:
            raise ExchangeConnectionError("HTTP session is not available")

        params: dict[str, Any] = dict(data or {})

        if signed:
            self._sign_params(params)

        url = f"{self._rest_base}{path}"
        kwargs: dict[str, Any] = {}

        if method.upper() == "GET":
            kwargs["params"] = params
        else:
            kwargs["data"] = params

        for attempt in range(max_retries + 1):
            try:
                await self._wait_if_rate_limited()

                async with self._session.request(
                    method.upper(), url, **kwargs
                ) as resp:
                    self._update_rate_limits(resp.headers)

                    if resp.status == 429:
                        retry_after = self._parse_retry_after(resp.headers)
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
                            backoff = retry_backoff_base ** attempt
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

                    content_type = resp.headers.get("Content-Type", "")
                    if "application/json" in content_type:
                        return await resp.json()
                    body = await resp.text()
                    if not body:
                        return {}
                    return body

            except asyncio.TimeoutError as exc:
                if attempt < max_retries:
                    backoff = retry_backoff_base ** attempt
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
                if attempt < max_retries:
                    backoff = retry_backoff_base ** attempt
                    await asyncio.sleep(backoff)
                    continue
                raise ExchangeConnectionError(
                    f"HTTP error: {exc}"
                ) from exc

        raise ExchangeConnectionError(
            f"Request failed after {max_retries} retries"
        )

    # ======================================================================
    # Internal — Rate Limiting
    # ======================================================================

    def _update_rate_limits(self, headers: Any) -> None:
        """Update rate-limit counters from response headers."""
        used_weight_str = headers.get(
            self._binance_config.get("header_used_weight"), ""
        )
        if used_weight_str:
            try:
                self._used_weight = int(used_weight_str)
            except (ValueError, TypeError):
                pass

        order_count_str = headers.get(
            self._binance_config.get("header_order_count"), ""
        )
        if order_count_str:
            try:
                self._used_orders = int(order_count_str)
            except (ValueError, TypeError):
                pass

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

        if weight_pct >= float(self._binance_config.get("rate_limit_warn_threshold")):
            self._log.warning(
                "rate_limit_approaching",
                used_weight=self._used_weight,
                max_weight=self._max_weight,
                pct=f"{weight_pct:.1%}",
            )

        if order_pct >= float(self._binance_config.get("rate_limit_warn_threshold")):
            self._log.warning(
                "order_limit_approaching",
                used_orders=self._used_orders,
                max_orders=self._max_orders,
                pct=f"{order_pct:.1%}",
            )

    def _parse_retry_after(self, headers: Any) -> float:
        """Extract the ``Retry-After`` header value."""
        raw = headers.get("Retry-After", "")
        try:
            return float(raw)
        except (ValueError, TypeError):
            return float(self._binance_config.get("retry_after_fallback_seconds"))

    def _handle_rate_limit(self, retry_after: float) -> None:
        """Handle a 429 rate-limit response."""
        pause_until = time.time() + retry_after
        self._rate_limit_paused = True
        self._rate_limit_pause_until = pause_until
        self._log.warning(
            "rate_limit_hit",
            retry_after_s=retry_after,
            pause_until=pause_until,
        )

    async def _wait_if_rate_limited(self) -> None:
        """Wait if the rate-limit pause is active."""
        if not self._rate_limit_paused:
            if self._used_weight >= self._max_weight * float(self._binance_config.get("rate_limit_hard_threshold")):
                wait = float(self._binance_config.get("rate_limiter_wait_seconds"))
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
        """Subscribe to one or more WebSocket streams."""
        if not stream_names:
            return

        new_streams = [
            s for s in stream_names
            if s not in self._ws_subscriptions
        ]

        if not new_streams:
            return

        for stream_name in new_streams:
            self._ws_subscriptions.setdefault(stream_name, [])

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
        """Listen on a single WebSocket stream with auto-reconnect."""
        retries = 0

        while not self._stop_event.is_set():
            try:
                ws_url = f"{self._ws_base}/ws/{stream_name}"
                async with self._session.ws_connect(
                    ws_url,
                    heartbeat=float(self._binance_config["heartbeat_seconds"]),
                    autoclose=False,
                ) as ws:
                    self._ws_connections[stream_name] = ws
                    retries = 0

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
                if retries > int(self._binance_config.get("ws_max_retries")):
                    self._log.error(
                        "ws_max_retries_reached",
                        stream=stream_name,
                        retries=retries,
                    )
                    break

                ws_base_backoff = float(self._binance_config["ws_backoff_base_seconds"])
                ws_backoff_mult = float(self._binance_config["ws_backoff_multiplier"])
                ws_max_backoff = float(self._binance_config["ws_backoff_max_seconds"])
                ws_jitter = float(self._binance_config["ws_backoff_jitter_factor"])

                backoff = min(
                    ws_base_backoff
                    * (ws_backoff_mult ** (retries - 1)),
                    ws_max_backoff,
                )
                import random

                jitter = random.uniform(0, backoff * ws_jitter)
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
        """Parse and dispatch an incoming WebSocket message."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            self._log.warning("ws_invalid_json", stream=stream_name)
            return

        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    self._dispatch_event(item)
            return

        if isinstance(data, dict):
            self._dispatch_event(data)

    def _dispatch_event(self, data: dict[str, Any]) -> None:
        """Route a parsed WebSocket event to the correct queue."""
        event_type = data.get("e", "")

        if event_type == "24hrTicker":
            self._price_queue.put_nowait(data)
        elif event_type == "markPrice":
            self._mark_price_queue.put_nowait(data)
        elif event_type == "ACCOUNT_UPDATE":
            self._handle_account_update_event(data)
        else:
            # For combined streams without explicit event type
            if "c" in data and "s" in data:
                self._price_queue.put_nowait(data)

    def _handle_account_update_event(
        self, data: dict[str, Any]
    ) -> None:
        """Parse an ACCOUNT_UPDATE event and enqueue an ``AccountUpdate``."""
        try:
            balances_data = data.get("B", {})
            account_balances: dict[str, Balance] = {}

            for entry in balances_data if isinstance(balances_data, list) else []:
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
        """Close the aiohttp session if it is open."""
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
        """Parse a single position entry from the Binance Futures API.

        Args:
            entry: A position dict from ``/fapi/v2/positionRisk``.

        Returns:
            A ``Position`` object, or ``None`` if the entry is empty.
        """
        symbol = entry.get("symbol", "")
        if not symbol:
            return None

        pos_amt = Decimal(str(entry.get("positionAmt", "0")))
        if pos_amt == Decimal("0"):
            return None

        side_str = "LONG" if pos_amt > 0 else "SHORT"
        side = (
            PositionSide.LONG if side_str == "LONG" else PositionSide.SHORT
        )

        pos_side_str = entry.get("positionSide", "BOTH")
        if pos_side_str == "LONG":
            fut_side = FuturesPositionSide.LONG
        elif pos_side_str == "SHORT":
            fut_side = FuturesPositionSide.SHORT
        else:
            fut_side = FuturesPositionSide.BOTH

        isolated = entry.get("isolated", False)
        margin_type = MarginType.ISOLATED if isolated else MarginType.CROSS

        return Position(
            symbol=symbol,
            side=side,
            quantity=abs(pos_amt),
            entry_price=Decimal(str(entry.get("entryPrice", "0"))),
            current_price=Decimal(str(entry.get("markPrice", "0"))),
            unrealized_pnl=Decimal(str(entry.get("unrealizedProfit", "0"))),
            realized_pnl=Decimal(str(entry.get("realizedProfit", "0"))),
            leverage=int(entry.get("leverage", 1)),
            margin_type=margin_type,
            position_side=fut_side,
            liquidation_price=Decimal(str(entry.get("liquidationPrice", "0"))),
            updated_at=int(entry.get("updateTime", 0)),
        )

    def _parse_fills(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        """Parse fill entries from an order response."""
        fills_raw = data.get("fills", [])
        if not fills_raw:
            return []
        return [self._parse_fill(f) for f in fills_raw]

    @staticmethod
    def _parse_fill(fill: dict[str, Any]) -> dict[str, Any]:
        """Parse a single fill entry."""
        return {
            "price": fill.get("price", "0"),
            "qty": fill.get("qty", "0"),
            "commission": fill.get("commission", "0"),
            "commission_asset": fill.get("commissionAsset", ""),
        }

    # ======================================================================
    # Internal — listenKey Management
    # ======================================================================

    async def _ensure_listen_key(self) -> str:
        """Create or retrieve the user data stream listenKey.

        Creates via ``POST /fapi/v1/listenKey``.

        Returns:
            The current listenKey string.
        """
        if self._listen_key:
            return self._listen_key

        data = await self._request(
            "POST", "/fapi/v1/listenKey", signed=False
        )
        self._listen_key = data.get("listenKey", "")
        if not self._listen_key:
            raise ExchangeConnectionError(
                "Failed to create listenKey"
            )

        # Connect to user data WebSocket
        try:
            ws_url = f"{self._ws_base}/ws/{self._listen_key}"
            ws = await self._session.ws_connect(
                ws_url,
                heartbeat=float(self._binance_config["heartbeat_seconds"]),
                autoclose=False,
            )
            self._ws_connections["user_data"] = ws

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
        """Periodically refresh the listenKey to keep the stream alive."""
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(
                    int(self._binance_config["listen_key_refresh_seconds"])
                )
                await self._request(
                    "PUT",
                    "/fapi/v1/listenKey",
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
        """Listen on the user data WebSocket and dispatch events."""
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
                        self._log.warning("user_data_invalid_json")
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
        """Dispatch a user data stream event."""
        event_type = data.get("e", "")
        if event_type == "ACCOUNT_UPDATE":
            self._handle_account_update_event(data)
