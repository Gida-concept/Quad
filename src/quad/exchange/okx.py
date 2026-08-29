"""OKX USDT perpetual futures exchange adapter.

Implements :class:`quad.exchange.base.ExchangeAdapter` for OKX's V5 unified
API, targeting **USDT-M perpetual** contracts exclusively.  Perpetual is
selected by the ``instType='SWAP'`` parameter and is hard-coded as a class
constant (``INST_TYPE``) so there is no separate "perpetual vs delivery"
toggle to misconfigure.

This adapter uses the official ``python-okx`` SDK for REST transport,
which handles V5 request signing, receive-window, and server time
synchronisation.  Order/position/account JSON is translated into the
shared domain dataclasses defined in ``quad.types.domain``; filter
normalization reuses the ABC's ``normalize_quantity`` / ``normalize_price``
/ ``get_tick_size`` helpers (with an OKX-specific ``_get_lot_filters``
override).

Symbol format
-------------
OKX uses its own symbol format: ``BTC-USDT-SWAP`` for USDT perpetuals
vs shorthand ``BTCUSDT``.  The adapter exposes a ``translate_symbol()``
static helper to convert between formats, and the ``okx_symbol()``
helper converts the shorthand ``BTCUSDT`` into ``BTC-USDT-SWAP``.

Usage::

    adapter = OkxFuturesAdapter(
        api_key="...",
        api_secret="...",
        passphrase="...",
        testnet=True,
    )
    await adapter.connect()
    account = await adapter.get_account()
"""
from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncGenerator
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog

try:  # pragma: no cover - import guard for environments without the SDK
    from okx import Account as OkxAccount
    from okx import Funding as OkxFunding
    from okx import MarketData as OkxMarketData
    from okx import PublicData as OkxPublicData
    from okx import Trade as OkxTrade
except Exception:  # pragma: no cover
    OkxAccount = None  # type: ignore[assignment]
    OkxTrade = None  # type: ignore[assignment]
    OkxMarketData = None  # type: ignore[assignment]
    OkxPublicData = None  # type: ignore[assignment]
    OkxFunding = None  # type: ignore[assignment]

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
    PositionStatus,
    Trade,
)
from quad.types.exchange import AccountUpdate
from quad.types.market import FundingRate

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# OKX error codes used by the generic error helpers below.
# ---------------------------------------------------------------------------
_MARGIN_MODE_ALREADY_SET_CODE = "59000"
_ORDER_NOT_FOUND_CODE = "51400"
_ORDER_NOT_FOUND_CODES = ("51400", "51401")  # order does not exist / order already filled
_RATE_LIMIT_CODES = ("50011", "50013", "50014", "50026")  # rate limit / IP ban
_BANNED_CODES = ("50026",)  # IP ban

# OKX instrument type for USDT perpetual
INST_TYPE = "SWAP"

# Shorthand -> OKX symbol translation map (subset of common symbols)
_SHORTHAND_TO_OKX: dict[str, str] = {
    "BTCUSDT": "BTC-USDT-SWAP",
    "ETHUSDT": "ETH-USDT-SWAP",
    "SOLUSDT": "SOL-USDT-SWAP",
    "BNBUSDT": "BNB-USDT-SWAP",
    "DOGEUSDT": "DOGE-USDT-SWAP",
    "XRPUSDT": "XRP-USDT-SWAP",
    "ADAUSDT": "ADA-USDT-SWAP",
    "AVAXUSDT": "AVAX-USDT-SWAP",
    "DOTUSDT": "DOT-USDT-SWAP",
    "LINKUSDT": "LINK-USDT-SWAP",
    "MATICUSDT": "MATIC-USDT-SWAP",
    "UNIUSDT": "UNI-USDT-SWAP",
    "PEPEUSDT": "PEPE-USDT-SWAP",
    "ARBUSDT": "ARB-USDT-SWAP",
    "OPUSDT": "OP-USDT-SWAP",
}


# ---------------------------------------------------------------------------
# Symbol translation helpers
# ---------------------------------------------------------------------------


def okx_symbol(shorthand: str) -> str:
    """Convert a shorthand symbol (``BTCUSDT``) to OKX format (``BTC-USDT-SWAP``).

    If the symbol is already in OKX format, it is returned unchanged.
    """
    upper = shorthand.upper()
    if upper in _SHORTHAND_TO_OKX:
        return _SHORTHAND_TO_OKX[upper]
    # Generic conversion: try to split last 4 chars if they are USDT
    if upper.endswith("USDT"):
        base = upper[:-4]
        return f"{base}-USDT-SWAP"
    # Already in OKX format or unknown — return as-is
    return upper


def quad_symbol(okx_symbol_str: str) -> str:
    """Convert an OKX-format symbol (``BTC-USDT-SWAP``) to Quad shorthand (``BTCUSDT``).

    This is the inverse of :func:`okx_symbol`.
    """
    s = okx_symbol_str.upper().replace("-SWAP", "").replace("-", "")
    return s


# ---------------------------------------------------------------------------
# OKX Futures Adapter
# ---------------------------------------------------------------------------


class OkxFuturesAdapter(ExchangeAdapter):
    """Full-featured OKX USDT-perpetual exchange adapter (V5 API).

    Args:
        api_key: OKX API key.  May also be set via the ``OKX_API_KEY``
            environment variable.
        api_secret: OKX API secret.  May also be set via the
            ``OKX_API_SECRET`` environment variable.
        passphrase: OKX API passphrase.  May also be set via the
            ``OKX_PASSPHRASE`` environment variable.
        testnet: If ``True``, use demo trading mode (``flag='1'``).
            OKX uses the same domain but a different flag for testnet.
        config: Optional raw config dict for the top-level ``_dry_run``
            flag and per-exchange URL overrides.
    """

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        passphrase: str = "",
        testnet: bool = False,
        rate_limit: dict | None = None,
        recv_window: int | None = None,
        config: dict | None = None,
    ) -> None:
        self._log = log.bind(adapter="okx_futures")

        self._api_key: str = api_key or os.environ.get("OKX_API_KEY", "")
        self._api_secret: str = api_secret or os.environ.get("OKX_API_SECRET", "")
        self._passphrase: str = passphrase or os.environ.get("OKX_PASSPHRASE", "")
        self._testnet: bool = testnet
        self._config = config or {}
        self._exchange_config = self._config.get("exchange", {})
        self._okx_config = self._exchange_config.get("okx", {})

        # Dry-run hard guard (top-level ``_dry_run`` config key)
        self._dry_run: bool = bool(self._config.get("_dry_run", False))

        # TTL for the exchange-info filter cache (used by normalize_quantity)
        self._exchange_info_ttl: float = float(
            self._okx_config.get("exchange_info_ttl_seconds", 60)
        )

        # Resolve URLs
        self._domain: str = (
            "https://www.okx.com"  # OKX uses same domain, flag controls mode
        )

        # API clients (created in connect())
        self._trade_client: Any = None  # OkxTrade.TradeAPI
        self._account_client: Any = None  # OkxAccount.AccountAPI
        self._market_client: Any = None  # OkxMarketData.MarketAPI
        self._public_client: Any = None  # OkxPublicData.PublicAPI

        self._connected: bool = False

        # Per-symbol exchange-info cache (overrides ABC _get_lot_filters)
        self._exchange_info_cache: dict = {}

    # ======================================================================
    # Lifecycle
    # ======================================================================

    async def connect(self) -> None:
        """Create the OKX SDK clients and verify auth."""
        if self._connected:
            return

        if OkxTrade is None:
            raise ExchangeConnectionError("python-okx SDK is not installed")

        # OKX flag: '0' = live, '1' = demo (testnet)
        flag = "1" if self._testnet else "0"

        self._trade_client = OkxTrade.TradeAPI(
            api_key=self._api_key,
            api_secret_key=self._api_secret,
            passphrase=self._passphrase,
            flag=flag,
        )
        self._account_client = OkxAccount.AccountAPI(
            api_key=self._api_key,
            api_secret_key=self._api_secret,
            passphrase=self._passphrase,
            flag=flag,
        )
        self._market_client = OkxMarketData.MarketAPI(
            api_key=self._api_key,
            api_secret_key=self._api_secret,
            passphrase=self._passphrase,
            flag=flag,
        )
        self._public_client = OkxPublicData.PublicAPI(
            api_key=self._api_key,
            api_secret_key=self._api_secret,
            passphrase=self._passphrase,
            flag=flag,
        )

        # Verify connectivity / credentials by hitting the server time.
        # Temporarily mark as connected so _require_clients() passes during init.
        self._connected = True
        try:
            await self.get_server_time()
            self._log.info(
                "okx_futures_connected",
                testnet=self._testnet,
            )
        except Exception as exc:  # noqa: BLE001 broad guard on connect
            self._connected = False
            self._disconnect_clients()
            raise ExchangeConnectionError(
                f"Failed to connect to OKX: {exc}"
            ) from exc

    async def disconnect(self) -> None:
        """Close clients and clear state."""
        self._disconnect_clients()
        self._connected = False
        self._log.info("okx_futures_disconnected")

    def _disconnect_clients(self) -> None:
        """Best-effort close of SDK clients."""
        for name in ("_trade_client", "_account_client", "_market_client", "_public_client"):
            client = getattr(self, name, None)
            if client is not None:
                try:
                    # python-okx httpx client has a .close() method
                    if hasattr(client, "close"):
                        client.close()
                except Exception:  # noqa: S110
                    pass
            setattr(self, name, None)

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def is_testnet(self) -> bool:
        return self._testnet

    # ======================================================================
    # Internal — REST helpers (sync SDK via async executor)
    # ======================================================================

    def _require_clients(self) -> None:
        if not self._connected:
            raise ExchangeConnectionError("OKX client is not initialised; call connect() first")

    async def _get_trade(self, method: str, params: dict | None = None) -> Any:
        """Run a Trade API call in an executor."""
        self._require_clients()
        try:
            client_method = getattr(self._trade_client, method)
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: client_method(**(params or {}))
            )
            return self._unwrap(result)
        except Exception as exc:  # noqa: BLE001
            raise self._normalize_error(exc) from exc

    async def _get_account(self, method: str, params: dict | None = None) -> Any:
        """Run an Account API call in an executor."""
        self._require_clients()
        try:
            client_method = getattr(self._account_client, method)
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: client_method(**(params or {}))
            )
            return self._unwrap(result)
        except Exception as exc:  # noqa: BLE001
            raise self._normalize_error(exc) from exc

    async def _get_public(self, method: str, params: dict | None = None) -> Any:
        """Run a Public API call in an executor."""
        self._require_clients()
        try:
            client_method = getattr(self._public_client, method)
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: client_method(**(params or {}))
            )
            return self._unwrap(result)
        except Exception as exc:  # noqa: BLE001
            raise self._normalize_error(exc) from exc

    async def _get_market(self, method: str, params: dict | None = None) -> Any:
        """Run a Market API call in an executor."""
        self._require_clients()
        try:
            client_method = getattr(self._market_client, method)
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: client_method(**(params or {}))
            )
            return self._unwrap(result)
        except Exception as exc:  # noqa: BLE001
            raise self._normalize_error(exc) from exc

    @staticmethod
    def _unwrap(resp: Any) -> Any:
        """Unwrap an OKX SDK response dict, extracting ``data``."""
        if isinstance(resp, dict):
            code = str(resp.get("code", ""))
            msg = resp.get("msg", "")
            if code != "0":
                raise ExchangeError(f"OKX API error {code}: {msg} {resp}")
            return resp.get("data", resp)
        return resp

    def _normalize_error(self, exc: Exception) -> Exception:
        """Map an OKX error into a domain ExchangeError where possible."""
        text = str(exc)
        # Rate limit / IP ban
        for code in _RATE_LIMIT_CODES:
            if code in text:
                if code in _BANNED_CODES:
                    return ExchangeBannedError(text)
                return ExchangeRateLimitError(text)
        # Order not found
        for code in _ORDER_NOT_FOUND_CODES:
            if code in text:
                return ExchangeOrderError(text)
        if "order does not exist" in text.lower():
            return ExchangeOrderError(text)
        # Auth
        if any(c in text for c in ("50111", "50112", "50113")):
            return ExchangeAuthError(text)
        return ExchangeError(text)

    # ======================================================================
    # REST — Account & Positions
    # ======================================================================

    async def get_account(self) -> Account:
        """Fetch futures account information including balances."""
        data = await self._get_account("get_account_balance")

        # data is a list; first item has 'details' and 'totalEq'
        account_info = data[0] if isinstance(data, list) and data else {}
        total_eq = Decimal(str(account_info.get("totalEq", "0") or "0"))

        balances: dict[str, Balance] = {}
        for detail in account_info.get("details", []):
            ccy = detail.get("ccy", "")
            if not ccy:
                continue
            balances[ccy] = Balance(
                asset=ccy,
                free=Decimal(str(detail.get("availBal", "0") or "0")),
                locked=Decimal(str(detail.get("frozenBal", "0") or "0")),
            )

        # Also get account config for margin balance, available balance, max leverage
        try:
            config_data = await self._get_account("get_account_config")
            acct_config = config_data[0] if isinstance(config_data, list) and config_data else {}
        except Exception:  # noqa: BLE001
            acct_config = {}

        total_wallet = Decimal(str(account_info.get("totalEq", "0") or "0"))

        # Get positions for the positions field
        positions = await self.get_positions()

        return Account(
            id="okx",
            exchange="okx",
            balances=balances,
            total_usdt=total_eq,
            timestamp=int(time.time() * 1000),
            max_leverage=1,  # OKX doesn't report this at account level easily
            total_wallet_balance=total_wallet,
            total_margin_balance=Decimal(str(account_info.get("adjEq", "0") or "0")),
            available_balance=Decimal(str(account_info.get("imr", "0") or "0")),
            positions=[],
        )

    async def get_positions(self) -> list[Position]:
        """Fetch all open futures positions from the exchange."""
        data = await self._get_account(
            "get_positions",
            {"instType": INST_TYPE},
        )

        items = data if isinstance(data, list) else []
        positions: list[Position] = []

        for pos in items:
            # Skip positions with zero size
            pos_val = float(pos.get("pos", "0") or "0")
            if pos_val == 0:
                continue

            inst_id = pos.get("instId", "")
            side_str = pos.get("posSide", "net").lower()
            avg_px = pos.get("avgPx", "0")
            mark_px = pos.get("markPx", "0")
            liq_px = pos.get("liqPx", "0")
            lever = pos.get("lever", "1")
            mgn_mode = pos.get("mgnMode", "isolated")

            # Determine position side
            if side_str == "long":
                position_side = FuturesPositionSide.LONG
                pos_side = PositionSide.LONG
            elif side_str == "short":
                position_side = FuturesPositionSide.SHORT
                pos_side = PositionSide.SHORT
            else:
                # Net mode: positive pos = LONG, negative = SHORT
                if pos_val > 0:
                    position_side = FuturesPositionSide.LONG
                    pos_side = PositionSide.LONG
                else:
                    position_side = FuturesPositionSide.SHORT
                    pos_side = PositionSide.SHORT

            positions.append(
                Position(
                    id=0,
                    strategy="",
                    symbol=inst_id,
                    side=pos_side,
                    quantity=Decimal(str(abs(pos_val))),
                    entry_price=Decimal(str(avg_px or "0")),
                    current_price=Decimal(str(mark_px or "0")),
                    unrealized_pnl=Decimal(str(pos.get("upl", "0") or "0")),
                    realized_pnl=Decimal(str(pos.get("realizedPnl", "0") or "0")),
                    leverage=int(float(lever or "1")),
                    margin_type=MarginType.CROSS if mgn_mode == "cross" else MarginType.ISOLATED,
                    position_side=position_side,
                    liquidation_price=Decimal(str(liq_px or "0")),
                    initial_margin=Decimal(str(pos.get("mgn", "0") or "0")),
                    maintenance_margin=Decimal(str(pos.get("mgnRatio", "0") or "0")),
                    funding_paid=Decimal(str(pos.get("fundingFee", "0") or "0")),
                    status=PositionStatus.OPEN,
                    opened_at=int(pos.get("cTime", 0) or 0),
                    updated_at=int(pos.get("uTime", 0) or 0),
                )
            )

        return positions

    # ======================================================================
    # REST — Futures Market Data
    # ======================================================================

    async def get_funding_rate(self, symbol: str) -> FundingRate:
        """Fetch current funding rate for a symbol."""
        inst_id = okx_symbol(symbol)
        data = await self._get_public(
            "get_funding_rate",
            {"instId": inst_id},
        )

        info = data[0] if isinstance(data, list) and data else {}
        funding_rate_val = info.get("fundingRate", "0")
        next_funding_time = info.get("fundingTime", 0)
        mark_price = info.get("nextFundingRate", "0")  # mark price is in a different endpoint

        return FundingRate(
            symbol=inst_id,
            funding_rate=Decimal(str(funding_rate_val or "0")),
            next_funding_time=int(next_funding_time or 0),
            mark_price=Decimal(str(info.get("fundingRate", "0") or "0")),
            index_price=Decimal(0),
        )

    async def get_mark_price(self, symbol: str) -> Decimal:
        """Fetch current mark price for a symbol."""
        inst_id = okx_symbol(symbol)
        data = await self._get_public(
            "get_mark_price",
            {"instType": INST_TYPE, "instId": inst_id},
        )

        info = data[0] if isinstance(data, list) and data else {}
        return Decimal(str(info.get("markPx", "0") or "0"))

    # ======================================================================
    # REST — Order Management
    # ======================================================================

    async def place_order(self, request: OrderRequest) -> OrderResult:
        """Place an order on the exchange."""
        # Build OKX params
        params = {
            "instId": request.symbol,
            "tdMode": "cross",  # default; can be overridden by config
            "side": request.side.lower() if request.side else "buy",
            "ordType": request.order_type.lower() if request.order_type else "market",
            "sz": str(request.quantity) if request.quantity else "0",
        }

        # Set position side for hedge mode
        if request.position_side:
            params["posSide"] = request.position_side.lower()

        # Limit order price
        if request.price is not None:
            params["px"] = str(request.price)

        # Reduce-only
        if request.reduce_only:
            params["reduceOnly"] = True

        # Client order ID
        if request.client_order_id:
            params["clOrdId"] = request.client_order_id

        data = await self._get_trade("place_order", params)

        # OKX returns [{ordId, clOrdId, sCode, sMsg}] on success
        result = data[0] if isinstance(data, list) and data else {}
        ord_id = result.get("ordId", "")
        s_code = result.get("sCode", "0")
        s_msg = result.get("sMsg", "")

        if s_code != "0":
            raise ExchangeOrderError(f"OKX order rejected: {s_code} {s_msg}")

        return OrderResult(
            order_id=ord_id,
            client_order_id=result.get("clOrdId", request.client_order_id),
            symbol=request.symbol,
            side=request.side or "",
            order_type=request.order_type or "market",
            quantity=request.quantity,
            filled_qty=Decimal(0),
            price=request.price,
            status="NEW",
            fills=[],
        )

    async def cancel_order(self, order_id: int | str, symbol: str = "") -> bool:
        """Cancel an order by exchange order ID."""
        if not symbol:
            raise ValueError("symbol is required for OKX cancel_order")
        try:
            inst_id = okx_symbol(symbol) if not symbol.startswith("-") else symbol
            data = await self._get_trade(
                "cancel_order",
                {"instId": inst_id, "ordId": str(order_id)},
            )
            result = data[0] if isinstance(data, list) and data else {}
            s_code = result.get("sCode", "0")
            return s_code == "0"
        except ExchangeOrderError:
            return False

    async def get_order_status(self, order_id: int | str, symbol: str = "") -> Order:
        """Get the current status of an order from the exchange."""
        if not symbol:
            raise ValueError("symbol is required for OKX get_order_status")
        inst_id = okx_symbol(symbol) if not symbol.startswith("-") else symbol
        data = await self._get_trade(
            "get_order",
            {"instId": inst_id, "ordId": str(order_id)},
        )

        info = data[0] if isinstance(data, list) and data else {}
        return Order(
            id=info.get("ordId", order_id),
            client_order_id=info.get("clOrdId", ""),
            symbol=info.get("instId", symbol),
            side=info.get("side", "").upper(),
            order_type=info.get("ordType", ""),
            quantity=Decimal(str(info.get("sz", "0") or "0")),
            filled_qty=Decimal(str(info.get("accFillSz", "0") or "0")),
            price=Decimal(str(info.get("px", "0") or "0")) if info.get("px") else None,
            stop_price=Decimal(str(info.get("triggerPx", "0") or "0")) if info.get("triggerPx") else None,
            status=self._map_okx_status(info.get("state", "")),
            time_in_force=info.get("tdMode", ""),
            created_at=int(info.get("cTime", 0) or 0),
            updated_at=int(info.get("uTime", 0) or 0),
        )

    async def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        """Get all currently open orders."""
        params: dict[str, Any] = {"instType": INST_TYPE}
        if symbol:
            params["instId"] = okx_symbol(symbol)

        data = await self._get_trade("get_order_list", params)

        items = data if isinstance(data, list) else []
        orders: list[Order] = []
        for entry in items:
            orders.append(
                Order(
                    id=entry.get("ordId", ""),
                    client_order_id=entry.get("clOrdId", ""),
                    symbol=entry.get("instId", ""),
                    side=entry.get("side", "").upper(),
                    order_type=entry.get("ordType", ""),
                    quantity=Decimal(str(entry.get("sz", "0") or "0")),
                    filled_qty=Decimal(str(entry.get("accFillSz", "0") or "0")),
                    price=Decimal(str(entry.get("px", "0") or "0")) if entry.get("px") else None,
                    stop_price=Decimal(str(entry.get("triggerPx", "0") or "0")) if entry.get("triggerPx") else None,
                    status=self._map_okx_status(entry.get("state", "")),
                    time_in_force=entry.get("tdMode", ""),
                    created_at=int(entry.get("cTime", 0) or 0),
                    updated_at=int(entry.get("uTime", 0) or 0),
                )
            )
        return orders

    async def get_user_trades(
        self, symbol: str | None = None, limit: int = 500
    ) -> list[Trade]:
        """Fetch executed fills for the account."""
        params: dict[str, Any] = {"instType": INST_TYPE}
        if symbol:
            params["instId"] = okx_symbol(symbol)
        params["limit"] = str(min(limit, 100))

        data = await self._get_trade("get_fills", params)

        items = data if isinstance(data, list) else []
        trades: list[Trade] = []
        for fill in items:
            trades.append(
                Trade(
                    id=0,  # OKX doesn't return a unique trade ID
                    position_id=0,
                    order_id=fill.get("ordId", ""),
                    symbol=fill.get("instId", ""),
                    side=fill.get("side", "").upper(),
                    quantity=Decimal(str(fill.get("fillSz", "0") or "0")),
                    price=Decimal(str(fill.get("fillPx", "0") or "0")),
                    fee=Decimal(str(fill.get("fee", "0") or "0")),
                    pnl=Decimal(str(fill.get("pnl", "0") or "0")),
                    timestamp=int(fill.get("ts", 0) or 0),
                )
            )
        return trades

    async def get_order_realized_pnl(
        self, order_id: int | str, symbol: str = ""
    ) -> Decimal:
        """Fetch the realized PnL for a single order."""
        if not order_id:
            return Decimal(0)
        try:
            params: dict[str, Any] = {"instType": INST_TYPE}
            if symbol:
                params["instId"] = okx_symbol(symbol)
            params["ordId"] = str(order_id)

            data = await self._get_trade("get_fills", params)
            items = data if isinstance(data, list) else []
            total_pnl = Decimal(0)
            for fill in items:
                total_pnl += Decimal(str(fill.get("pnl", "0") or "0"))
            return total_pnl
        except ExchangeError:
            return Decimal(0)

    # ======================================================================
    # REST — Futures Configuration
    # ======================================================================

    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        """Set leverage for a symbol."""
        inst_id = okx_symbol(symbol)
        data = await self._get_account(
            "set_leverage",
            {"lever": str(leverage), "mgnMode": "isolated", "instId": inst_id},
        )
        return data[0] if isinstance(data, list) and data else {}

    async def set_margin_mode(self, symbol: str, margin_type: str) -> dict:
        """Set margin mode (isolated/cross) for a symbol.

        OKX V5 API: POST /api/v5/account/set-margin-mode
        Parameters: instId, mgnMode
        """
        if margin_type.lower() == "isolated":
            inst_id = okx_symbol(symbol)
            # Use raw API call since the SDK's set_isolated_mode has wrong params
            client = self._account_client
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: client._request_with_params(
                    "POST",
                    "/api/v5/account/set-margin-mode",
                    {"instId": inst_id, "mgnMode": "isolated"},
                ),
            )
            data = self._unwrap(result)
            return data[0] if isinstance(data, list) and data else {}
        # Cross is the default, nothing to set
        return {}

    async def set_position_mode(self, mode: str) -> dict:
        """Set position mode (one_way/hedge)."""
        pos_mode = "long_short_mode" if mode.lower() == "hedge" else "net_mode"
        data = await self._get_account(
            "set_position_mode",
            {"posMode": pos_mode},
        )
        return data[0] if isinstance(data, list) and data else {}

    async def get_position_mode(self) -> str:
        """Get current position mode."""
        data = await self._get_account("get_account_config")
        config = data[0] if isinstance(data, list) and data else {}
        pos_mode = config.get("posMode", "net_mode")
        return "hedge" if pos_mode == "long_short_mode" else "one_way"

    # ======================================================================
    # WebSocket — User Data Streams
    # ======================================================================

    async def subscribe_account_updates(
        self,
    ) -> AsyncGenerator[AccountUpdate, None]:
        """Subscribe to account / position updates via user data stream.

        This is a stub that yields no updates — OKX WebSocket requires
        separate async WebSocket implementation using the SDK's WS classes.
        The existing ``WebSocketManager`` handles public streams; private
        account updates are polled via REST in the reconciliation loop.
        """
        while True:
            await asyncio.sleep(60)  # never yield; reconciliation loop handles updates
            break  # exit immediately; caller handles via REST polling

    # ======================================================================
    # Utility
    # ======================================================================

    async def get_exchange_info(self) -> dict:
        """Fetch instrument info for USDT-perpetual symbols.

        OKX returns instrument info in its own format, so this adapter
        overrides ``_get_lot_filters`` to parse the OKX layout directly.
        """
        data = await self._get_public(
            "get_instruments",
            {"instType": INST_TYPE},
        )
        # data is a list of instrument dicts
        return {"list": data if isinstance(data, list) else []}

    async def get_server_time(self) -> int:
        """Fetch the current OKX server time."""
        data = await self._get_public("get_system_time")
        ts = data if isinstance(data, str) else (data[0] if isinstance(data, list) and data else {})
        if isinstance(ts, dict):
            return int(ts.get("ts", 0) or 0)
        return int(ts or 0)

    # ---- OKX-specific filter parsing (overrides ABC default) ------------

    async def _get_lot_filters(self, symbol: str) -> tuple[Decimal, Decimal, Decimal]:
        """Return (step_size, min_qty, min_notional) from OKX instruments info."""
        cache = self._exchange_info_cache
        ttl = float(getattr(self, "_exchange_info_ttl", 60))
        now = time.monotonic()
        cached = cache.get(symbol)
        if cached is not None and (now - cached[0]) < ttl:
            return cached[1]

        inst_id = okx_symbol(symbol) if not symbol.startswith("-") else symbol

        data = await self._get_public(
            "get_instruments",
            {"instType": INST_TYPE, "instId": inst_id},
        )

        instruments = data if isinstance(data, list) else []
        step = min_qty = min_notional = Decimal(0)
        found = False

        for inst in instruments:
            if inst.get("instId") != inst_id:
                continue
            # lotSz = step size, minSz = minimum order quantity
            step = Decimal(str(inst.get("lotSz", "0") or "0"))
            min_qty = Decimal(str(inst.get("minSz", "0") or "0"))
            # OKX doesn't have minNotional; use ctVal (contract value) * minSz
            ct_val = Decimal(str(inst.get("ctVal", "1") or "1"))
            min_notional = ct_val * min_qty
            found = True
            break

        if not found:
            raise RuntimeError(
                f"no instrument info found for {inst_id} on OKX"
            )

        result = (step, min_qty, min_notional)
        cache[inst_id] = (now, result)
        return result

    # ---- Exchange-specific error semantics (overrides ABC defaults) -------

    def is_margin_mode_already_set(self, exc: Exception) -> bool:
        """OKX returns 59000 when margin mode is already set — benign no-op."""
        return _MARGIN_MODE_ALREADY_SET_CODE in str(exc)

    def is_order_not_found(self, exc: Exception) -> bool:
        """OKX returns 51400/51401 when an order no longer exists — resolve locally."""
        text = str(exc)
        return any(c in text for c in _ORDER_NOT_FOUND_CODES) or "order does not exist" in text.lower()

    # ======================================================================
    # Internal helpers
    # ======================================================================

    @staticmethod
    def _map_okx_status(state: str) -> str:
        """Map OKX order state to Quad domain status."""
        state_map = {
            "live": "NEW",
            "canceled": "CANCELLED",
            "partially_filled": "PARTIALLY_FILLED",
            "filled": "FILLED",
            "mutual_canceled": "CANCELLED",
            "order_failed": "REJECTED",
            "accepted": "NEW",
            "triggered": "NEW",
            "pending_cancel": "NEW",
            "sl_trigger_pending": "NEW",
            "sl order": "NEW",
        }
        return state_map.get(state.lower(), state.upper() if state else "UNKNOWN")
