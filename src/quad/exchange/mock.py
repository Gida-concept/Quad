"""Mock exchange adapter for unit testing and backtesting.

Returns pre-configured responses for every method.  No network calls are
made.  All calls are logged at DEBUG level.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from decimal import Decimal
from typing import Any

import structlog

from quad.exchange.base import ExchangeAdapter
from quad.types.domain import (
    Account,
    Balance,
    Order,
    OrderRequest,
    OrderResult,
    Position,
)
from quad.types.exchange import AccountUpdate
from quad.types.market import FundingRate

logger = structlog.get_logger(__name__)


class MockAdapter(ExchangeAdapter):
    """Mock exchange adapter for testing.

    All methods return pre-configured responses or sensible defaults.
    No real network connections are made.

    Args:
        account: Pre-configured account.  If ``None``, a default empty
            account is created.
        positions: Pre-configured positions.  If ``None``, an empty list
            is used.
        futures_data: Pre-configured futures data dict containing
            funding_rates, mark_prices, etc.  If ``None``, defaults
            are returned.
        exchange_info: Pre-configured exchange info response.  If
            ``None``, a minimal default is returned.
        server_time: Fixed server time value.  If ``None``, current
            wall-clock time is used.
    """

    def __init__(
        self,
        account: Account | None = None,
        positions: list[Position] | None = None,
        futures_data: dict[str, Any] | None = None,
        exchange_info: dict | None = None,
        server_time: int | None = None,
    ) -> None:
        self._log = logger.bind(adapter="mock")

        self._account: Account = account or Account(
            id="mock-account",
            exchange="mock",
            balances={"USDT": Balance(asset="USDT", free=Decimal(100000))},
            total_usdt=Decimal(100000),
            timestamp=0,
        )
        self._positions: list[Position] = positions or []
        self._futures_data: dict[str, Any] = futures_data or {
            "funding_rates": {},
            "mark_prices": {},
        }
        self._exchange_info: dict = exchange_info or {
            "timezone": "UTC",
            "serverTime": 0,
            "symbols": [],
            "rateLimits": [],
        }
        self._server_time: int = server_time or 0

        self._connected: bool = False
        self._next_order_id: int = 1
        self._placed_orders: dict[int, Order] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Simulate connecting to the exchange."""
        self._connected = True
        self._log.info("mock_connect")

    async def disconnect(self) -> None:
        """Simulate disconnecting from the exchange."""
        self._connected = False
        self._log.info("mock_disconnect")

    @property
    def is_connected(self) -> bool:
        """Whether the adapter is currently connected."""
        return self._connected

    # ------------------------------------------------------------------
    # REST — Account & Positions
    # ------------------------------------------------------------------

    async def get_account(self) -> Account:
        """Return the pre-configured account."""
        self._log.debug("mock_get_account")
        return self._account

    async def get_positions(self) -> list[Position]:
        """Return the pre-configured positions."""
        self._log.debug("mock_get_positions", count=len(self._positions))
        return list(self._positions)

    # ------------------------------------------------------------------
    # REST — Futures Market Data
    # ------------------------------------------------------------------

    async def get_funding_rate(self, symbol: str) -> FundingRate:
        """Return the pre-configured funding rate or a sensible default.

        Args:
            symbol: The trading pair symbol.

        Returns:
            A ``FundingRate`` with pre-configured or default values.
        """
        self._log.debug("mock_get_funding_rate", symbol=symbol)
        fr_data = self._futures_data.get("funding_rates", {}).get(symbol)
        if fr_data:
            return FundingRate(
                symbol=symbol,
                funding_rate=Decimal(str(fr_data.get("funding_rate", "0.0001"))),
                next_funding_time=fr_data.get("next_funding_time", 0),
                mark_price=Decimal(str(fr_data.get("mark_price", "50000"))),
                index_price=Decimal(str(fr_data.get("index_price", "50000"))),
            )
        return FundingRate(
            symbol=symbol,
            funding_rate=Decimal("0.0001"),
            next_funding_time=0,
            mark_price=Decimal(50000),
            index_price=Decimal(50000),
        )

    async def get_mark_price(self, symbol: str) -> Decimal:
        """Return the pre-configured mark price or a sensible default.

        Args:
            symbol: The trading pair symbol.

        Returns:
            Mark price as a ``Decimal``.
        """
        self._log.debug("mock_get_mark_price", symbol=symbol)
        mp = self._futures_data.get("mark_prices", {}).get(symbol)
        if mp is not None:
            return Decimal(str(mp))
        return Decimal(50000)

    # ------------------------------------------------------------------
    # REST — Order Management
    # ------------------------------------------------------------------

    async def place_order(self, request: OrderRequest) -> OrderResult:
        """Simulate placing an order.

        The order is stored locally with a mocked status of ``"FILLED"``
        (or ``"NEW"`` if the request is a LIMIT order without a price).
        An ``OrderResult`` is returned with a synthetic order ID.

        Args:
            request: The order parameters.

        Returns:
            A simulated ``OrderResult``.
        """
        order_id = self._next_order_id
        self._next_order_id += 1

        status = "NEW"
        if request.order_type.upper() == "MARKET":
            status = "FILLED"
        elif request.price is not None:
            status = "NEW"

        fills: list[dict] = []
        if status == "FILLED":
            fills.append(
                {
                    "qty": str(request.quantity),
                    "price": str(request.price or Decimal(0)),
                    "commission": "0",
                    "commissionAsset": "USDT",
                }
            )

        order = Order(
            id=order_id,
            client_order_id=request.client_order_id or "",
            symbol=request.symbol,
            side=request.side,
            order_type=request.order_type,
            quantity=request.quantity,
            filled_qty=request.quantity if status == "FILLED" else Decimal(0),
            price=request.price,
            status=status,
            time_in_force=request.time_in_force,
        )
        self._placed_orders[order_id] = order

        result = OrderResult(
            order_id=order_id,
            client_order_id=request.client_order_id or "",
            symbol=request.symbol,
            side=request.side,
            order_type=request.order_type,
            quantity=request.quantity,
            filled_qty=order.filled_qty,
            price=request.price,
            status=status,
            fills=fills,
        )

        self._log.debug(
            "mock_place_order",
            order_id=order_id,
            symbol=request.symbol,
            status=status,
        )
        return result

    async def cancel_order(self, order_id: int) -> bool:
        """Simulate cancelling an order.

        Args:
            order_id: The order ID to cancel.

        Returns:
            ``True`` if the order existed and was cancelled, ``False``
            if the order was not found.
        """
        order = self._placed_orders.get(order_id)
        if order is None:
            self._log.debug("mock_cancel_order_not_found", order_id=order_id)
            return False

        if order.status in ("FILLED", "CANCELLED", "REJECTED"):
            self._log.debug(
                "mock_cancel_order_invalid_state",
                order_id=order_id,
                status=order.status,
            )
            return False

        order.status = "CANCELLED"
        self._log.debug("mock_cancel_order", order_id=order_id)
        return True

    async def get_order_status(self, order_id: int) -> Order:
        """Return the stored order with the given ID.

        Args:
            order_id: The order ID to look up.

        Returns:
            The stored ``Order``.

        Raises:
            ValueError: If the order ID was never placed.
        """
        order = self._placed_orders.get(order_id)
        if order is None:
            raise ValueError(f"Order {order_id} not found in mock state")
        self._log.debug("mock_get_order_status", order_id=order_id)
        return order

    async def get_open_orders(
        self, symbol: str | None = None
    ) -> list[Order]:
        """Return all orders with status ``NEW`` or ``PARTIALLY_FILLED``.

        Args:
            symbol: Optional symbol filter.

        Returns:
            A list of open ``Order`` objects.
        """
        open_orders = [
            order
            for order in self._placed_orders.values()
            if order.status in ("NEW", "PARTIALLY_FILLED")
        ]
        if symbol:
            open_orders = [o for o in open_orders if o.symbol == symbol]
        self._log.debug(
            "mock_get_open_orders", count=len(open_orders), symbol=symbol
        )
        return open_orders

    # ------------------------------------------------------------------
    # REST — Futures Configuration
    # ------------------------------------------------------------------

    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        """Simulate setting leverage for a symbol."""
        self._log.debug("mock_set_leverage", symbol=symbol, leverage=leverage)
        return {"symbol": symbol, "leverage": leverage, "success": True}

    async def set_margin_mode(self, symbol: str, margin_type: str) -> dict:
        """Simulate setting margin mode for a symbol."""
        self._log.debug("mock_set_margin_mode", symbol=symbol, margin_type=margin_type)
        return {"symbol": symbol, "marginType": margin_type.upper(), "success": True}

    async def set_position_mode(self, mode: str) -> dict:
        """Simulate setting position mode."""
        self._log.debug("mock_set_position_mode", mode=mode)
        return {"dualSidePosition": str(mode.lower() == "hedge").lower(), "success": True}

    async def get_position_mode(self) -> str:
        """Simulate getting position mode."""
        return "one_way"

    # ------------------------------------------------------------------
    # WebSocket — User Data Streams
    # ------------------------------------------------------------------

    async def subscribe_account_updates(
        self,
    ) -> AsyncGenerator[AccountUpdate, None]:
        """Yield pre-configured account updates from an empty generator.

        Yields:
            Pre-configured ``AccountUpdate`` objects, if any.
        """
        self._log.debug("mock_subscribe_account_updates")
        if False:
            yield  # type: ignore[unreachable]

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    async def get_exchange_info(self) -> dict:
        """Return the pre-configured exchange info dict.

        Returns:
            The exchange info response as a dict.
        """
        self._log.debug("mock_get_exchange_info")
        return dict(self._exchange_info)

    async def get_server_time(self) -> int:
        """Return the pre-configured server time.

        Returns:
            Server time in unix milliseconds.
        """
        return self._server_time

    # ------------------------------------------------------------------
    # Test helpers
    # ------------------------------------------------------------------

    def set_account(self, account: Account) -> None:
        """Override the mock account for testing."""
        self._account = account

    def set_positions(self, positions: list[Position]) -> None:
        """Override the mock positions for testing."""
        self._positions = positions

    def set_futures_data(self, data: dict[str, Any]) -> None:
        """Override the mock futures data for testing."""
        self._futures_data = data

    def set_funding_rates(self, rates: dict[str, dict[str, Any]]) -> None:
        """Set funding rate data for testing."""
        if "funding_rates" not in self._futures_data:
            self._futures_data["funding_rates"] = {}
        self._futures_data["funding_rates"].update(rates)

    def set_mark_prices(self, prices: dict[str, str | float]) -> None:
        """Set mark price data for testing."""
        if "mark_prices" not in self._futures_data:
            self._futures_data["mark_prices"] = {}
        self._futures_data["mark_prices"].update(prices)

    def set_exchange_info(self, info: dict) -> None:
        """Override the mock exchange info for testing."""
        self._exchange_info = info
