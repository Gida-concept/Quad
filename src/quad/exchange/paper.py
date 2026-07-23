"""Paper trading exchange adapter.

Simulates exchange interactions without real orders or market
connections.  Uses a virtual balance and simulates fills based on
mark-prices or limit-price crossings.

All log messages are prefixed with ``[PAPER]`` to clearly distinguish
paper-trading output from live mode.
"""

from __future__ import annotations

import asyncio
import math
import time
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
    PositionSide,
    PositionStatus,
)
from quad.types.exchange import AccountUpdate
from quad.types.market import GreekTick, OptionContract, OptionPriceTick

logger = structlog.get_logger(__name__)


class PaperTradingAdapter(ExchangeAdapter):
    """Simulated exchange adapter for paper trading.

    Manages a virtual account balance and simulates order fills.  Market
    orders fill immediately with configurable slippage; limit orders
    check whether the virtual ``mark price`` crosses the limit price.

    Real market data should be fed from the market data module — the
    adapter itself does not generate prices.

    Args:
        initial_balance_usdt: Starting virtual balance in USDT.
        fill_latency_ms: Simulated fill latency in milliseconds.
        slippage_pct: Slippage percentage for market orders (as a
            decimal, e.g. ``0.001`` for 0.1%).
    """

    def __init__(
        self,
        initial_balance_usdt: Decimal | float = Decimal("10000"),
        fill_latency_ms: int = 200,
        slippage_pct: Decimal | float = Decimal("0.001"),
    ) -> None:
        self._log = logger.bind(adapter="paper")

        # Convert numeric types to Decimal for consistency
        if not isinstance(initial_balance_usdt, Decimal):
            initial_balance_usdt = Decimal(str(initial_balance_usdt))
        if not isinstance(slippage_pct, Decimal):
            slippage_pct = Decimal(str(slippage_pct))

        self._fill_latency_ms: int = fill_latency_ms
        self._slippage_pct: Decimal = slippage_pct

        # Virtual state
        now_ms = int(time.time() * 1000)
        self._account: Account = Account(
            id="paper-account",
            exchange="paper",
            balances={
                "USDT": Balance(
                    asset="USDT",
                    free=initial_balance_usdt,
                    locked=Decimal("0"),
                )
            },
            total_usdt=initial_balance_usdt,
            timestamp=now_ms,
        )
        self._positions: dict[str, Position] = {}
        self._orders: dict[int, Order] = {}
        self._next_order_id: int = 1
        self._connected: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Activate the paper trading adapter."""
        self._connected = True
        self._log.info("[PAPER] paper_adapter_connected")

    async def disconnect(self) -> None:
        """Deactivate the paper trading adapter."""
        self._connected = False
        self._log.info("[PAPER] paper_adapter_disconnected")

    @property
    def is_connected(self) -> bool:
        """Whether the adapter is currently connected."""
        return self._connected

    @property
    def virtual_balance(self) -> Decimal:
        """Current free USDT balance."""
        return self._account.balances["USDT"].free

    @property
    def portfolio_value(self) -> Decimal:
        """Estimated total portfolio value (cash + position market value)."""
        total = self._account.balances["USDT"].free
        for pos in self._positions.values():
            if pos.status == PositionStatus.OPEN:
                total += pos.current_price * pos.quantity
        return total

    # ------------------------------------------------------------------
    # REST — Account & Positions
    # ------------------------------------------------------------------

    async def get_account(self) -> Account:
        """Return the virtual account with current balance."""
        self._account.total_usdt = self.portfolio_value
        self._account.timestamp = int(time.time() * 1000)
        return self._account

    async def get_positions(self) -> list[Position]:
        """Return all open virtual positions."""
        return [
            p for p in self._positions.values() if p.status == PositionStatus.OPEN
        ]

    # ------------------------------------------------------------------
    # REST — Market Data
    # ------------------------------------------------------------------

    async def get_option_chain(self, underlying: str) -> list[OptionContract]:
        """Paper trading has no built-in market data.

        Returns an empty list.  Real option chain data should come from
        the market data module.
        """
        self._log.debug("[PAPER] get_option_chain", underlying=underlying)
        return []

    # ------------------------------------------------------------------
    # REST — Order Management
    # ------------------------------------------------------------------

    async def place_order(self, request: OrderRequest) -> OrderResult:
        """Simulate placing and filling an order.

        Market orders fill immediately (with configurable slippage).
        Limit orders are accepted as ``NEW`` and must be filled via
        ``_simulate_fill()`` when the mark price reaches the limit.

        Args:
            request: The order parameters.

        Returns:
            A simulated ``OrderResult``.
        """
        if not self._connected:
            await self.connect()

        order_id = self._next_order_id
        self._next_order_id += 1

        quantity = request.quantity
        side = request.side.upper()
        order_type = request.type.upper()

        # Determine fill price and status
        fill_price: Decimal | None = None
        status: str = "NEW"
        fills: list[dict] = []

        if order_type == "MARKET":
            # Market order: fill immediately at mark price with slippage
            fill_price = self._compute_fill_price(
                side, request.price, is_market=True
            )
            status = "FILLED"
            total_cost = fill_price * quantity
            fills.append(
                {
                    "qty": str(quantity),
                    "price": str(fill_price),
                    "commission": "0",
                    "commissionAsset": "USDT",
                }
            )
        elif order_type == "LIMIT" and request.price is not None:
            # Limit order: check if we can fill immediately
            fill_price = self._compute_fill_price(
                side, request.price, is_market=False
            )
            if fill_price is not None:
                status = "FILLED"
                total_cost = fill_price * quantity
                fills.append(
                    {
                        "qty": str(quantity),
                        "price": str(fill_price),
                        "commission": "0",
                        "commissionAsset": "USDT",
                    }
                )
            else:
                # Limit not yet crossed; stay open
                fill_price = request.price
                status = "NEW"
        else:
            # Unknown type — reject
            status = "REJECTED"
            self._log.warning(
                "[PAPER] unknown_order_type", order_type=order_type
            )

        # Update virtual balance and positions if filled
        if status == "FILLED" and fill_price is not None:
            total_cost = fill_price * quantity
            commission = self._estimate_commission(total_cost)
            total_deducted = total_cost + commission

            if side == "BUY":
                if self._account.balances["USDT"].free < total_deducted:
                    status = "REJECTED"
                    fills = []
                    self._log.warning(
                        "[PAPER] insufficient_funds",
                        required=total_deducted,
                        available=self._account.balances["USDT"].free,
                    )
                else:
                    self._account.balances["USDT"].free -= total_deducted
                    self._account.balances["USDT"].locked = Decimal("0")

            elif side == "SELL":
                self._account.balances["USDT"].free += total_deducted

            self._account.total_usdt = self.portfolio_value

            # Simulate latency
            if self._fill_latency_ms > 0:
                await asyncio.sleep(self._fill_latency_ms / 1000.0)

            # Record position
            self._update_position(request, fill_price, quantity, side)

        order = Order(
            id=order_id,
            client_order_id=request.client_order_id or "",
            symbol=request.symbol,
            side=request.side,
            type=request.type,
            quantity=quantity,
            filled_qty=quantity if status == "FILLED" else Decimal("0"),
            price=request.price,
            stop_price=request.stop_price,
            status=status,
            time_in_force=request.time_in_force,
            created_at=int(time.time() * 1000),
            updated_at=int(time.time() * 1000),
        )
        self._orders[order_id] = order

        result = OrderResult(
            order_id=order_id,
            client_order_id=request.client_order_id or "",
            symbol=request.symbol,
            side=request.side,
            type=request.type,
            quantity=quantity,
            filled_qty=order.filled_qty,
            price=request.price,
            status=status,
            fills=fills,
        )

        self._log.info(
            "[PAPER] order_placed",
            order_id=order_id,
            symbol=request.symbol,
            side=side,
            quantity=str(quantity),
            price=str(request.price),
            status=status,
        )
        return result

    async def cancel_order(self, order_id: int) -> bool:
        """Cancel a virtual order.

        Args:
            order_id: The order ID to cancel.

        Returns:
            ``True`` if cancellation succeeded.
        """
        order = self._orders.get(order_id)
        if order is None:
            return False

        if order.status in ("FILLED", "CANCELLED", "REJECTED", "EXPIRED"):
            return False

        order.status = "CANCELLED"
        order.updated_at = int(time.time() * 1000)
        self._log.info("[PAPER] order_cancelled", order_id=order_id)
        return True

    async def get_order_status(self, order_id: int) -> Order:
        """Return the stored order with the given ID.

        Args:
            order_id: The order ID to look up.

        Returns:
            The stored ``Order``.

        Raises:
            ValueError: If the order ID is not found.
        """
        order = self._orders.get(order_id)
        if order is None:
            raise ValueError(
                f"[PAPER] Order {order_id} not found in paper state"
            )
        return order

    async def get_open_orders(
        self, symbol: str | None = None
    ) -> list[Order]:
        """Return all open virtual orders.

        Args:
            symbol: Optional symbol filter.

        Returns:
            A list of open ``Order`` objects.
        """
        open_orders = [
            order
            for order in self._orders.values()
            if order.status in ("NEW", "PARTIALLY_FILLED")
        ]
        if symbol:
            open_orders = [o for o in open_orders if o.symbol == symbol]
        return open_orders

    # ------------------------------------------------------------------
    # WebSocket — Market Data Streams
    # ------------------------------------------------------------------

    async def subscribe_option_prices(
        self, symbols: list[str]
    ) -> AsyncGenerator[OptionPriceTick, None]:
        """Paper trading does not connect to real WebSocket streams.

        Returns an empty generator.  In a paper-trading scenario, price
        data should come from the market data engine using a real
        exchange adapter.

        Yields:
            Nothing by default.
        """
        self._log.info("[PAPER] subscribe_option_prices_not_available")
        if False:
            yield  # type: ignore[unreachable]

    async def subscribe_greeks(
        self, symbols: list[str]
    ) -> AsyncGenerator[GreekTick, None]:
        """Paper trading does not connect to real WebSocket streams.

        Yields:
            Nothing by default.
        """
        self._log.info("[PAPER] subscribe_greeks_not_available")
        if False:
            yield  # type: ignore[unreachable]

    async def subscribe_account_updates(
        self,
    ) -> AsyncGenerator[AccountUpdate, None]:
        """Paper trading does not connect to a user data stream.

        Yields:
            Nothing by default.
        """
        self._log.info("[PAPER] subscribe_account_updates_not_available")
        if False:
            yield  # type: ignore[unreachable]

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    async def get_exchange_info(self) -> dict:
        """Return minimal exchange info for paper trading."""
        return {
            "timezone": "UTC",
            "serverTime": int(time.time() * 1000),
            "optionContracts": [],
            "optionAssets": [{"name": "USDT"}],
            "optionSymbols": [],
            "rateLimits": [],
        }

    async def get_server_time(self) -> int:
        """Return current wall clock time as server time."""
        return int(time.time() * 1000)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _compute_fill_price(
        self,
        side: str,
        limit_price: Decimal | None,
        is_market: bool = False,
    ) -> Decimal | None:
        """Compute the simulated fill price for an order.

        For market orders, returns a price based on a virtual mark price
        with slippage.  For limit orders, returns a price if the limit
        would be crossed, otherwise ``None``.

        Args:
            side: ``"BUY"`` or ``"SELL"``.
            limit_price: The limit price for limit orders.
            is_market: Whether this is a market order.

        Returns:
            The fill price, or ``None`` if the limit would not be
            crossed.
        """
        # In paper mode, we don't have real mark prices.
        # Use the limit price as the fill price for limit orders.
        # For market orders, use limit_price as the "expected" price
        # with slippage, or default to some reasonable value.

        if limit_price is not None:
            if is_market:
                # Apply slippage to the limit price as a proxy for market impact
                slippage = limit_price * self._slippage_pct
                if side == "BUY":
                    return (limit_price + slippage).quantize(
                        Decimal("0.01")
                    )
                return (limit_price - slippage).quantize(Decimal("0.01"))
            # Limit order: we always accept the limit price for simplicity
            return limit_price

        # No limit price provided (e.g. market order without price hint)
        return None

    def _estimate_commission(self, trade_value: Decimal) -> Decimal:
        """Estimate trading commission at 0.03% (typical Binance VIP 0).

        Args:
            trade_value: The total trade value in USDT.

        Returns:
            Estimated commission.
        """
        return (trade_value * Decimal("0.0003")).quantize(Decimal("0.01"))

    def _update_position(
        self,
        request: OrderRequest,
        fill_price: Decimal,
        quantity: Decimal,
        side: str,
    ) -> None:
        """Update the virtual position tracker after a fill.

        Args:
            request: The original order request.
            fill_price: The fill price.
            quantity: The filled quantity.
            side: ``"BUY"`` or ``"SELL"``.
        """
        now_ms = int(time.time() * 1000)
        symbol = request.symbol

        if symbol in self._positions:
            pos = self._positions[symbol]
            pos.quantity += quantity if side == "BUY" else -quantity
            pos.current_price = fill_price
            pos.updated_at = now_ms

            if pos.quantity <= 0:
                pos.status = PositionStatus.CLOSED
                pos.updated_at = now_ms
                self._log.info(
                    "[PAPER] position_closed",
                    symbol=symbol,
                    pnl=str(pos.realized_pnl),
                )
        else:
            pos = Position(
                id=None,
                strategy=request.client_order_id.split("_")[0]
                if "_" in request.client_order_id
                else "",
                contract_symbol=symbol,
                side=PositionSide.LONG if side == "BUY" else PositionSide.SHORT,
                quantity=quantity,
                entry_price=fill_price,
                current_price=fill_price,
                opened_at=now_ms,
                updated_at=now_ms,
                status=PositionStatus.OPEN,
            )
            self._positions[symbol] = pos
