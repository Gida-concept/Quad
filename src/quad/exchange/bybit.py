"""Bybit USDT perpetual futures exchange adapter.

Implements :class:`quad.exchange.base.ExchangeAdapter` for Bybit's V5 unified
API, targeting **USDT perpetual** contracts exclusively.  Perpetual is selected
by the ``category="linear"`` parameter and is hard-coded as a class constant
(``CATEGORY``) so there is no separate "futures vs perpetual" toggle to
misconfigure.

This adapter uses the official ``pybit`` SDK for both REST and WebSocket
transport, which handles V5 request signing, receive-window, and WebSocket
subscription/auto-reconnect.  Order/position/account JSON is translated into
the shared domain dataclasses defined in ``quad.types.domain``; filter
normalization reuses the ABC's ``normalize_quantity`` / ``normalize_price`` /
``get_tick_size`` helpers (with a Bybit-specific ``_get_lot_filters`` override).
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncGenerator
from decimal import Decimal, InvalidOperation

import structlog

try:  # pragma: no cover - import guard for environments without the SDK
    from pybit.unified_trading import HTTP, WebSocket
except Exception:  # pragma: no cover
    HTTP = None  # type: ignore[assignment]
    WebSocket = None  # type: ignore[assignment]

from quad.exchange.base import (
    ExchangeAdapter,
    ExchangeAuthError,
    ExchangeBannedError,
    ExchangeConnectionError,
    ExchangeError,
    ExchangeOrderError,
    ExchangeRateLimitError,
)
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
    Trade,
)
from quad.types.exchange import AccountUpdate
from quad.types.market import FundingRate

log = structlog.get_logger(__name__)

# Bybit error codes used by the generic error helpers below.
_MARGIN_MODE_ALREADY_SET_CODE = "110043"  # "Margin mode is not modified"
_ORDER_NOT_FOUND_CODES = ("20001", "30003")  # "Order does not exist" / not found
_ORDER_NOT_FOUND_TEXT = "order does not exist"


class BybitFuturesAdapter(ExchangeAdapter):
    """Full-featured Bybit USDT-perpetual exchange adapter (V5 API).

    Args:
        api_key: Bybit API key.  May also be set via the ``BYBIT_API_KEY``
            environment variable.
        api_secret: Bybit API secret.  May also be set via the
            ``BYBIT_API_SECRET`` environment variable.
        testnet: If ``True``, use the Bybit testnet
            (``https://api-testnet.bybit.com``).
        rate_limit: Optional dict with ``max_weight`` and ``max_orders`` keys
            to configure rate-limit tracking (used for parity with the Binance
            adapter; ``pybit`` performs its own internal throttling).
        recv_window: Request validity window in milliseconds (default 5000).
        config: Optional raw config dict (mirrors the Binance adapter) so the
            top-level ``_dry_run`` flag and per-exchange URL overrides can be
            read.
    """

    # USDT perpetual.  Every market call passes this constant, so the bot can
    # only ever trade linear perpetuals (never inverse/spot/options).
    CATEGORY = "linear"

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        testnet: bool = False,
        rate_limit: dict | None = None,
        recv_window: int | None = None,
        config: dict | None = None,
    ) -> None:
        self._log = log.bind(adapter="bybit_futures")

        self._api_key: str = api_key or os.environ.get("BYBIT_API_KEY", "")
        self._api_secret: str = api_secret or os.environ.get("BYBIT_API_SECRET", "")
        self._testnet: bool = testnet
        self._config = config or {}
        self._exchange_config = self._config.get("exchange", {}) or {}
        self._bybit_config = self._exchange_config.get("bybit", {}) or {}
        self._recv_window: int = recv_window or int(
            self._bybit_config.get("recv_window", 5000)
        )

        # Dry-run hard guard (top-level ``_dry_run`` config key).  When set AND
        # the exchange is live (``testnet=False``), place_order() refuses every
        # order to protect real funds.
        self._dry_run: bool = bool(self._config.get("_dry_run", False))

        # TTL for the exchange-info filter cache (used by normalize_quantity).
        self._exchange_info_ttl: float = float(
            self._bybit_config.get("exchange_info_ttl_seconds", 60)
        )

        # Resolve base URLs (pybit accepts testnet=bool directly, but we keep
        # the resolved values for diagnostics / logging).
        self._rest_base: str = (
            self._bybit_config.get("testnet_base_url", "https://api-testnet.bybit.com")
            if testnet
            else self._bybit_config.get("base_url", "https://api.bybit.com")
        )

        rl = rate_limit or {}
        self._max_weight: int = int(rl.get("max_weight") or 0)
        self._max_orders: int = int(rl.get("max_orders") or 0)

        self._client: object | None = None  # pybit.HTTP
        self._ws: object | None = None  # pybit.WebSocket
        self._ws_task: object | None = None

        # Subscription state for account-update generator.
        self._account_queue: asyncio.Queue = asyncio.Queue()
        self._stop_event: asyncio.Event = asyncio.Event()
        self._connected: bool = False

        # Per-symbol exchange-info cache (overrides ABC _get_lot_filters).
        self._exchange_info_cache: dict = {}

    # ======================================================================
    # Lifecycle
    # ======================================================================

    async def connect(self) -> None:
        """Create the pybit HTTP (and lazy WebSocket) clients and verify auth."""
        if self._connected:
            return
        self._stop_event.clear()

        if HTTP is None:
            raise ExchangeConnectionError("pybit SDK is not installed")

        self._client = HTTP(
            testnet=self._testnet,
            api_key=self._api_key,
            api_secret=self._api_secret,
            recv_window=self._recv_window,
        )

        # Verify connectivity / credentials by hitting the public server time.
        try:
            await self.get_server_time()
            self._log.info(
                "bybit_futures_connected",
                testnet=self._testnet,
                rest_base=self._rest_base,
            )
        except Exception as exc:  # noqa: BLE001 broad guard on connect
            self._client = None
            raise ExchangeConnectionError(
                f"Failed to connect to Bybit: {exc}"
            ) from exc

        self._connected = True

    async def disconnect(self) -> None:
        """Close the WebSocket (if any) and drop client references."""
        self._stop_event.set()
        if self._ws_task is not None:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):  # noqa: S110 best-effort
                pass
            self._ws_task = None
        self._ws = None
        self._client = None
        self._connected = False
        self._log.info("bybit_futures_disconnected")

    @property
    def is_connected(self) -> bool:
        """Whether the pybit HTTP client is initialised."""
        return self._connected and self._client is not None

    @property
    def is_testnet(self) -> bool:
        """Whether this adapter targets the Bybit testnet."""
        return self._testnet

    # ======================================================================
    # Internal — REST helpers
    # ======================================================================

    def _require_client(self):
        if self._client is None:
            raise ExchangeConnectionError("Bybit HTTP client is not initialised")
        return self._client

    def _normalize_error(self, exc: Exception) -> Exception:
        """Map a pybit error into a domain ExchangeError where possible."""
        text = str(exc)
        if _ORDER_NOT_FOUND_CODES and any(c in text for c in _ORDER_NOT_FOUND_CODES):
            return ExchangeOrderError(text)
        if _ORDER_NOT_FOUND_TEXT in text.lower():
            return ExchangeOrderError(text)
        return ExchangeError(text)

    async def _get(self, endpoint: str, params: dict | None = None) -> dict:
        client = self._require_client()
        try:
            # pybit v5 uses _submit_request() for raw API calls.
            # The path must be fully qualified with the base endpoint.
            full_path = f"{client.endpoint}{endpoint}"
            resp = await asyncio.get_event_loop().run_in_executor(
                None, lambda: client._submit_request(
                    method="GET", path=full_path, query=params or {}, auth=True,
                )
            )
            return self._unwrap(resp)
        except Exception as exc:  # noqa: BLE001
            raise self._normalize_error(exc) from exc

    async def _post(self, endpoint: str, params: dict | None = None) -> dict:
        client = self._require_client()
        try:
            full_path = f"{client.endpoint}{endpoint}"
            resp = await asyncio.get_event_loop().run_in_executor(
                None, lambda: client._submit_request(
                    method="POST", path=full_path, query=params or {}, auth=True,
                )
            )
            return self._unwrap(resp)
        except Exception as exc:  # noqa: BLE001
            raise self._normalize_error(exc) from exc

    @staticmethod
    def _unwrap(resp: dict) -> dict:
        """Extract the ``result`` payload from a Bybit V5 envelope.

        Bybit wraps every response as ``{"retCode": 0, "retMsg": "OK",
        "result": {...}, "time": ...}``.  A non-zero ``retCode`` is an error.
        """
        if not isinstance(resp, dict):
            return resp
        ret_code = resp.get("retCode")
        if ret_code not in (0, None):
            msg = resp.get("retMsg", "Bybit error")
            raise ExchangeOrderError(f"Bybit {ret_code}: {msg}")
        result = resp.get("result")
        return result if isinstance(result, dict) else resp

    # ======================================================================
    # REST — Account & Positions
    # ======================================================================

    async def get_account(self) -> Account:
        """Fetch the unified trading account and map it to ``Account``."""
        data = await self._get(
            "/v5/account/wallet-balance", {"accountType": "UNIFIED"}
        )
        if data is None:
            data = {}
        lists = data.get("list", [{}])
        acct = lists[0] if lists else {}
        total_equity = Decimal(str(acct.get("totalEquity", "0") or "0"))
        total_wallet = Decimal(str(acct.get("totalWalletBalance", "0") or "0"))
        total_margin = Decimal(str(acct.get("totalMarginBalance", "0") or "0"))
        available = Decimal(str(acct.get("totalAvailableBalance", "0") or "0"))

        balances: dict[str, Balance] = {}
        for coin in acct.get("coin", []) or []:
            asset = coin.get("coin", "")
            if not asset:
                continue
            free = Decimal(str(coin.get("availableToWithdraw", "0") or "0"))
            locked = Decimal(str(coin.get("locked", "0") or "0"))
            balances[asset] = Balance(asset=asset, free=free, locked=locked)

        return Account(
            id=f"bybit-{self._api_key[:8]}" if self._api_key else "bybit",
            exchange="bybit",
            balances=balances,
            total_usdt=total_equity.quantize(Decimal("0.01")),
            timestamp=int(time.time() * 1000),
            max_leverage=1,
            total_wallet_balance=total_wallet,
            total_margin_balance=total_margin,
            available_balance=available,
        )

    async def get_positions(self) -> list[Position]:
        """Fetch all open USDT-perpetual positions."""
        data = await self._get(
            "/v5/position/list",
            {"category": self.CATEGORY, "settleCoin": "USDT"},
        )
        positions: list[Position] = []
        for entry in (data or {}).get("list", []) if isinstance(data, dict) else []:
            pos = self._parse_position(entry)
            if pos is not None:
                positions.append(pos)
        return positions

    def _parse_position(self, entry: dict) -> Position | None:
        symbol = entry.get("symbol", "")
        if not symbol:
            return None
        size = Decimal(str(entry.get("size", "0") or "0"))
        if size == Decimal(0):
            # Bybit returns both sides; skip zero-size legs.
            side_raw = entry.get("side", "")
            if side_raw in ("Buy", "Sell"):
                return None
        side = PositionSide.LONG if size > 0 else PositionSide.SHORT
        pos_side_raw = entry.get("positionIdx", 0)
        fut_side = (
            FuturesPositionSide.LONG
            if pos_side_raw == 1
            else FuturesPositionSide.SHORT
            if pos_side_raw == 2
            else FuturesPositionSide.BOTH
        )
        try:
            leverage = int(float(entry.get("leverage", 1) or 1))
        except (ValueError, TypeError, InvalidOperation):
            leverage = 1
        margin_type = (
            MarginType.ISOLATED
            if str(entry.get("isIsolated", "false")).lower() == "true"
            else MarginType.CROSS
        )
        return Position(
            symbol=symbol,
            side=side,
            quantity=abs(size),
            entry_price=Decimal(str(entry.get("avgPrice", "0") or "0")),
            current_price=Decimal(str(entry.get("markPrice", "0") or "0")),
            unrealized_pnl=Decimal(str(entry.get("unrealisedPnl", "0") or "0")),
            realized_pnl=Decimal(str(entry.get("realisedPnl", "0") or "0")),
            leverage=leverage,
            margin_type=margin_type,
            position_side=fut_side,
            liquidation_price=Decimal(str(entry.get("liqPrice", "0") or "0")),
            updated_at=int(entry.get("updatedTime", 0) or 0),
        )

    async def get_user_trades(
        self, symbol: str | None = None, limit: int = 500
    ) -> list[Trade]:
        """Fetch executed fills from ``/v5/execution/list``."""
        symbols = [symbol] if symbol else [
            getattr(p, "symbol", "") for p in (await self.get_positions() or [])
        ]
        symbols = [s for s in symbols if s]
        if not symbols:
            return []

        trades: list[Trade] = []
        for sym in symbols:
            try:
                data = await self._get(
                    "/v5/execution/list",
                    {
                        "category": self.CATEGORY,
                        "symbol": sym,
                        "limit": int(limit),
                    },
                )
            except ExchangeError:
                continue
            for entry in (data or {}).get("list", []) if isinstance(data, dict) else []:
                try:
                    trades.append(
                        Trade(
                            order_id=entry.get("orderId", 0) or 0,
                            symbol=sym,
                            side=str(entry.get("side", "")).upper(),
                            quantity=Decimal(str(entry.get("execQty", "0") or "0")),
                            price=Decimal(str(entry.get("execPrice", "0") or "0")),
                            fee=Decimal(str(entry.get("execFee", "0") or "0")),
                            # Bybit's /v5/execution/list returns ``realizedPnl``
                            # per fill — the exchange's own realized PnL for
                            # that leg.  We take it directly from the trade so
                            # the journal and Telegram alerts never compute a
                            # stale/mock PnL from mark-price fallbacks.
                            pnl=Decimal(str(entry.get("realizedPnl", "0") or "0")),
                            timestamp=int(entry.get("execTime", 0) or 0),
                        )
                    )
                except (TypeError, ValueError, InvalidOperation):
                    continue
        return trades

    # ======================================================================
    # REST — Futures Market Data
    # ======================================================================

    async def get_funding_rate(self, symbol: str) -> FundingRate:
        """Fetch the latest funding rate / mark / index prices for a symbol."""
        data = await self._get(
            "/v5/market/tickers",
            {"category": self.CATEGORY, "symbol": symbol},
        )
        entry = ((data or {}).get("list", [{}]) or [{}])[0]
        return FundingRate(
            symbol=entry.get("symbol", symbol),
            funding_rate=Decimal(str(entry.get("fundingRate", "0") or "0")),
            next_funding_time=int(entry.get("nextFundingTime", 0) or 0),
            mark_price=Decimal(str(entry.get("markPrice", "0") or "0")),
            index_price=Decimal(str(entry.get("indexPrice", "0") or "0")),
        )

    async def get_mark_price(self, symbol: str) -> Decimal:
        """Fetch the current mark price for a symbol."""
        data = await self._get(
            "/v5/market/tickers",
            {"category": self.CATEGORY, "symbol": symbol},
        )
        entry = ((data or {}).get("list", [{}]) or [{}])[0]
        return Decimal(str(entry.get("markPrice", "0") or "0"))

    async def get_klines(
        self, symbol: str, interval: str, limit: int = 500
    ) -> list[tuple[float, ...]]:
        """Fetch kline/candlestick data (open_time_s, o, h, l, c, v)."""
        data = await self._get(
            "/v5/market/kline",
            {
                "category": self.CATEGORY,
                "symbol": symbol,
                "interval": interval,
                "limit": int(limit),
            },
        )
        results: list[tuple[float, ...]] = []
        for k in (data or {}).get("list", []) if isinstance(data, dict) else []:
            # Bybit kline tuple: [startTime, open, high, low, close, volume, ...]
            try:
                results.append(
                    (
                        float(k[0]) / 1000.0,
                        float(k[1]),
                        float(k[2]),
                        float(k[3]),
                        float(k[4]),
                        float(k[5]),
                    )
                )
            except (IndexError, TypeError, ValueError):
                continue
        return results

    # ======================================================================
    # REST — Order Management
    # ======================================================================

    async def place_order(self, request: OrderRequest) -> OrderResult:
        """Place an order on Bybit USDT perpetual.

        Calls ``POST /v5/order/create`` with ``category=linear``.

        Raises:
            RuntimeError: If the hard dry-run guard blocks a live order, or the
                quantity fails exchange filter validation.
            ExchangeOrderError: If the order is rejected by Bybit.
        """
        # 0. Hard dry-run guard — refuse every order when dry-run mode is
        #    enabled but the exchange is LIVE.
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

        # 0b. Normalize quantity to the exchange's LOT_SIZE / MIN_NOTIONAL.
        quantity = await self.normalize_quantity(request.symbol, request.quantity)

        params: dict[str, object] = {
            "category": self.CATEGORY,
            "symbol": request.symbol,
            "side": request.side.upper(),
            "orderType": request.order_type.upper(),
            "qty": str(quantity),
            "positionIdx": 0,  # one-way mode
        }

        if request.price is not None:
            params["price"] = str(
                await self.normalize_price(request.symbol, request.price)
            )
        if request.stop_price is not None:
            trigger = await self.normalize_price(request.symbol, request.stop_price)
            params["triggerPrice"] = str(trigger)
        if request.time_in_force:
            params["timeInForce"] = request.time_in_force.upper()
        if request.reduce_only:
            params["reduceOnly"] = True
        if request.client_order_id:
            params["orderLinkId"] = request.client_order_id

        data = await self._post("/v5/order/create", params)

        # Bybit V5 returns orderId as a UUID string (e.g. "0f4a5a75-..."),
        # not an integer.  Keep it as-is to avoid int() conversion errors.
        order_id = data.get("orderId", 0) or 0
        status = data.get("orderStatus", "Created")

        return OrderResult(
            order_id=order_id,
            client_order_id=data.get("orderLinkId", request.client_order_id),
            symbol=data.get("symbol", request.symbol),
            side=data.get("side", request.side),
            order_type=data.get("orderType", request.order_type),
            quantity=Decimal(str(data.get("qty", str(quantity)))),
            filled_qty=Decimal(str(data.get("cumExecQty", "0") or "0")),
            price=(
                Decimal(str(data.get("price", "0")))
                if data.get("price") not in (None, "", "0")
                else request.price
            ),
            status=status,
        )

    async def cancel_order(self, order_id: int | str, symbol: str = "") -> bool:
        """Cancel an order by Bybit order ID."""
        params: dict[str, object] = {"category": self.CATEGORY, "orderId": str(order_id)}
        if symbol:
            params["symbol"] = symbol
        try:
            await self._post("/v5/order/cancel", params)
            return True
        except ExchangeOrderError:
            return False
        except ExchangeError:
            return False

    async def get_order_status(self, order_id: int | str, symbol: str = "") -> Order:
        """Query a single order's status."""
        params: dict[str, object] = {"category": self.CATEGORY, "orderId": str(order_id)}
        if symbol:
            params["symbol"] = symbol
        data = await self._get("/v5/order/history", params)
        entries = (data or {}).get("list", []) if isinstance(data, dict) else []
        entry = entries[0] if entries else {}
        return Order(
            id=entry.get("orderId", order_id) or order_id,
            client_order_id=entry.get("orderLinkId", ""),
            symbol=entry.get("symbol", ""),
            side=entry.get("side", ""),
            order_type=entry.get("orderType", ""),
            quantity=Decimal(str(entry.get("qty", "0") or "0")),
            filled_qty=Decimal(str(entry.get("cumExecQty", "0") or "0")),
            price=(
                Decimal(str(entry.get("price", "0")))
                if entry.get("price") not in (None, "", "0")
                else None
            ),
            stop_price=(
                Decimal(str(entry.get("triggerPrice", "0")))
                if entry.get("triggerPrice") not in (None, "", "0")
                else None
            ),
            status=entry.get("orderStatus", ""),
            time_in_force=entry.get("timeInForce", "GTC"),
            created_at=int(entry.get("createdTime", 0) or 0),
            updated_at=int(entry.get("updatedTime", 0) or 0),
        )

    async def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        """Query all open orders for the given symbol (or all symbols)."""
        params: dict[str, object] = {"category": self.CATEGORY, "openOnly": 1}
        # Bybit requires settleCoin or symbol for open-order queries; fall back
        # to settleCoin=USDT when no symbol is given.
        if symbol:
            params["symbol"] = symbol
        else:
            params["settleCoin"] = "USDT"
        data = await self._get("/v5/order/realtime", params)
        orders: list[Order] = []
        for entry in (data or {}).get("list", []) if isinstance(data, dict) else []:
            orders.append(
                Order(
                    id=entry.get("orderId", 0) or 0,
                    client_order_id=entry.get("orderLinkId", ""),
                    symbol=entry.get("symbol", ""),
                    side=entry.get("side", ""),
                    order_type=entry.get("orderType", ""),
                    quantity=Decimal(str(entry.get("qty", "0") or "0")),
                    filled_qty=Decimal(str(entry.get("cumExecQty", "0") or "0")),
                    price=(
                        Decimal(str(entry.get("price", "0")))
                        if entry.get("price") not in (None, "", "0")
                        else None
                    ),
                    stop_price=(
                        Decimal(str(entry.get("triggerPrice", "0")))
                        if entry.get("triggerPrice") not in (None, "", "0")
                        else None
                    ),
                    status=entry.get("orderStatus", ""),
                    time_in_force=entry.get("timeInForce", "GTC"),
                    created_at=int(entry.get("createdTime", 0) or 0),
                    updated_at=int(entry.get("updatedTime", 0) or 0),
                )
            )
        return orders

    async def get_order_realized_pnl(
        self, order_id: int | str, symbol: str = ""
    ) -> Decimal:
        """Fetch the realized PnL for a single Bybit order via
        ``GET /v5/order/history``.

        This is the **primary** PnL source for EXIT notifications: it queries
        a single close order's ``realizedPnl`` directly, eliminating the
        stale-window race of scanning ``/v5/execution/list`` (which returns
        up to 500 fills across all time for a symbol).

        Bybit V5 ``/v5/order/history`` returns order history including
        ``realizedPnl`` for filled/closed orders.

        Args:
            order_id: The Bybit order ID of the closing order.
            symbol: Contract symbol (e.g. ``BTCUSDT``).

        Returns:
            The exchange's realized PnL for this order as a ``Decimal``.
            ``Decimal(0)`` when the exchange doesn't report a value or the
            query fails — callers must treat 0 as "no data" and fall back to
            a computed PnL rather than trusting it as a real figure.
        """
        if not order_id:
            return Decimal(0)
        params: dict[str, object] = {
            "category": self.CATEGORY,
            "orderId": str(order_id),
        }
        if symbol:
            params["symbol"] = symbol
        try:
            data = await self._get("/v5/order/history", params)
        except ExchangeError:
            return Decimal(0)
        entries = (data or {}).get("list", []) if isinstance(data, dict) else []
        entry = entries[0] if entries else {}
        return Decimal(str(entry.get("realizedPnl", "0") or "0"))

    # ======================================================================
    # REST — Futures Configuration
    # ======================================================================

    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        """Set leverage for a symbol."""
        return await self._post(
            "/v5/position/set-leverage",
            {"category": self.CATEGORY, "symbol": symbol, "buyLeverage": str(leverage),
             "sellLeverage": str(leverage)},
        )

    async def set_margin_mode(self, symbol: str, margin_type: str) -> dict:
        """Set margin mode (isolated/cross) for a symbol."""
        trade_mode = 1 if margin_type.lower() == "isolated" else 0
        return await self._post(
            "/v5/position/switch-isolated",
            {"category": self.CATEGORY, "symbol": symbol, "tradeMode": trade_mode,
             "buyLeverage": "1", "sellLeverage": "1"},
        )

    async def set_position_mode(self, mode: str) -> dict:
        """Set position mode (one_way/hedge)."""
        dual = mode.lower() == "hedge"
        return await self._post(
            "/v5/position/switch-mode",
            {"category": self.CATEGORY, "mode": 3 if dual else 0},
        )

    async def get_position_mode(self) -> str:
        """Get the current position mode."""
        data = await self._get(
            "/v5/position/info", {"category": self.CATEGORY, "symbol": "BTCUSDT"}
        )
        # ``positionIdx`` 0 = one-way (both sides share), 1/2 = hedge.
        entries = (data or {}).get("list", []) if isinstance(data, dict) else []
        hedge = any(str(e.get("positionIdx", 0)) in ("1", "2") for e in entries)
        return "hedge" if hedge else "one_way"

    # ======================================================================
    # WebSocket — User Data Streams
    # ======================================================================

    async def subscribe_account_updates(
        self,
    ) -> AsyncGenerator[AccountUpdate, None]:
        """Subscribe to account/position updates via pybit's private WS.

        Yields ``AccountUpdate`` objects as Bybit pushes them.  The WebSocket
        is opened lazily here; ``disconnect()`` tears it down.
        """
        if WebSocket is None:
            raise ExchangeConnectionError("pybit SDK is not installed")

        def _on_message(_ws_msg):  # pybit passes raw WS frames here
            try:
                topic = _ws_msg.get("topic", "") if isinstance(_ws_msg, dict) else ""
                if "wallet" in topic or "position" in topic:
                    account = asyncio.run_coroutine_threadsafe(
                        self.get_account(), asyncio.get_event_loop()
                    ).result()
                    self._account_queue.put_nowait(
                        AccountUpdate(account=account, event_type=topic,
                                      timestamp=int(time.time() * 1000))
                    )
            except Exception:  # noqa: BLE001 best-effort push
                pass

        self._ws = WebSocket(
            testnet=self._testnet,
            api_key=self._api_key,
            api_secret=self._api_secret,
            channel_type="private",
        )
        try:
            # pybit v5 subscribe() takes a single topic string, not a list.
            # Subscribe to each private topic separately.
            self._ws.subscribe(topic="wallet", callback=_on_message)
            self._ws.subscribe(topic="position", callback=_on_message)
        except Exception as exc:  # noqa: BLE001
            raise ExchangeConnectionError(f"Bybit WS subscribe failed: {exc}") from exc

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
        """Fetch instrument info for USDT-perpetual symbols.

        Bybit returns ``{"result": {"list": [ {symbol, lotSizeFilter,
        priceFilter}, ... ]}}``.  The ABC's ``_get_lot_filters`` expects a
        Binance-shaped ``symbols`` list, so this adapter overrides
        ``_get_lot_filters`` to read Bybit's layout directly.
        """
        data = await self._get(
            "/v5/market/instruments-info",
            {"category": self.CATEGORY, "limit": 1000},
        )
        return data if isinstance(data, dict) else {}

    async def get_server_time(self) -> int:
        """Fetch the current Bybit server time (unix ms)."""
        data = await self._get("/v5/market/time")
        return int((data or {}).get("timeSecond", 0) or 0) * 1000

    # ---- Bybit-specific filter parsing (overrides ABC default) ------------

    async def _get_lot_filters(self, symbol: str) -> tuple[Decimal, Decimal, Decimal]:
        """Return (step_size, min_qty, min_notional) from Bybit instruments-info."""
        cache = self._exchange_info_cache
        ttl = float(getattr(self, "_exchange_info_ttl", 60))
        now = time.monotonic()
        cached = cache.get(symbol)
        if cached is not None and (now - cached[0]) < ttl:
            return cached[1]

        info = await self.get_exchange_info()
        step = min_qty = min_notional = Decimal(0)
        found = False
        for s in info.get("list", []) if isinstance(info, dict) else []:
            if s.get("symbol") != symbol:
                continue
            lot = s.get("lotSizeFilter") or {}
            step = Decimal(str(lot.get("qtyStep", "0") or "0"))
            min_qty = Decimal(str(lot.get("minOrderQty", "0") or "0"))
            price = s.get("priceFilter") or {}
            # Bybit has no MIN_NOTIONAL filter; approximate via minOrderQty.
            min_notional = min_qty
            found = True
            break

        if not found:
            raise RuntimeError(
                f"no instrument info found for {symbol} on Bybit"
            )

        result = (step, min_qty, min_notional)
        cache[symbol] = (now, result)
        return result

    # ---- Exchange-specific error semantics (overrides ABC defaults) -------

    def is_margin_mode_already_set(self, exc: Exception) -> bool:
        """Bybit returns 110043 "Margin mode is not modified" — benign no-op."""
        text = str(exc)
        return _MARGIN_MODE_ALREADY_SET_CODE in text

    def is_order_not_found(self, exc: Exception) -> bool:
        """Bybit returns 20001/30003 "Order does not exist" — resolve locally."""
        text = str(exc).lower()
        if _ORDER_NOT_FOUND_TEXT in text:
            return True
        return any(c in str(exc) for c in _ORDER_NOT_FOUND_CODES)
