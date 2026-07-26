"""Pluggable exchange adapter ABC for Binance Futures trading.

Every exchange adapter — live Binance, testnet, or mock — implements
this interface so the rest of the application remains exchange-agnostic.

All monetary values use ``Decimal`` for precision.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from decimal import Decimal

from quad.types.domain import (
    Account,
    FuturesPosition,
    FuturesPositionSide,
    Order,
    OrderRequest,
    OrderResult,
    Position,
)
from quad.types.exchange import AccountUpdate
from quad.types.market import FundingRate, FuturesContract


class ExchangeAdapter(ABC):
    """Pluggable exchange adapter for Binance Futures trading.

    Subclasses must implement every abstract method.  The adapter is
    responsible for its own connection lifecycle (REST session and
    WebSocket connections) via ``connect()`` and ``disconnect()``.
    """

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @abstractmethod
    async def connect(self) -> None:
        """Connect to the exchange (REST + WebSocket).

        Must be called before any other method.  Idempotent — safe to
        call multiple times.
        """
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Disconnect gracefully from the exchange.

        Closes all open WebSocket connections, the REST session, and
        any user-data-stream listenKeys.  Idempotent.
        """
        ...

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Whether the adapter is currently connected to the exchange."""
        ...

    # ------------------------------------------------------------------
    # REST — Account & Positions
    # ------------------------------------------------------------------

    @abstractmethod
    async def get_account(self) -> Account:
        """Fetch futures account information including balances.

        Returns:
            An ``Account`` dataclass with the current balance snapshot.

        Raises:
            ExchangeConnectionError: If the exchange is unreachable.
            ExchangeAuthError: If the API credentials are invalid.
        """
        ...

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """Fetch all open futures positions from the exchange.

        Returns:
            A list of ``Position`` dataclasses for every open position.
        """
        ...

    # ------------------------------------------------------------------
    # REST — Futures Market Data
    # ------------------------------------------------------------------

    @abstractmethod
    async def get_funding_rate(self, symbol: str) -> FundingRate:
        """Fetch current funding rate for a symbol."""
        ...

    @abstractmethod
    async def get_mark_price(self, symbol: str) -> Decimal:
        """Fetch current mark price for a symbol."""
        ...

    # ------------------------------------------------------------------
    # REST — Order Management
    # ------------------------------------------------------------------

    @abstractmethod
    async def place_order(self, request: OrderRequest) -> OrderResult:
        """Place an order on the exchange.

        Args:
            request: The order parameters.

        Returns:
            An ``OrderResult`` with the exchange-assigned order ID and
            initial status.
        """
        ...

    @abstractmethod
    async def cancel_order(self, order_id: int) -> bool:
        """Cancel an order by exchange order ID.

        Args:
            order_id: The exchange-assigned order identifier.

        Returns:
            ``True`` if the cancellation was accepted, ``False`` if the
            order was not found or already filled/cancelled.
        """
        ...

    @abstractmethod
    async def get_order_status(self, order_id: int) -> Order:
        """Get the current status of an order from the exchange.

        Args:
            order_id: The exchange-assigned order identifier.

        Returns:
            An ``Order`` dataclass with the latest status.
        """
        ...

    @abstractmethod
    async def get_open_orders(
        self, symbol: str | None = None
    ) -> list[Order]:
        """Get all currently open orders.

        Args:
            symbol: Optional symbol filter.  If ``None``, returns open
                orders for all symbols.

        Returns:
            A list of ``Order`` dataclasses for every open order.
        """
        ...

    # ------------------------------------------------------------------
    # REST — Futures Configuration
    # ------------------------------------------------------------------

    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        """Set leverage for a symbol."""
        ...

    @abstractmethod
    async def set_margin_mode(self, symbol: str, margin_type: str) -> dict:
        """Set margin mode (isolated/cross) for a symbol."""
        ...

    @abstractmethod
    async def set_position_mode(self, mode: str) -> dict:
        """Set position mode (one_way/hedge)."""
        ...

    @abstractmethod
    async def get_position_mode(self) -> str:
        """Get current position mode."""
        ...

    # ------------------------------------------------------------------
    # WebSocket — User Data Streams
    # ------------------------------------------------------------------

    @abstractmethod
    def subscribe_account_updates(
        self,
    ) -> AsyncGenerator[AccountUpdate, None]:
        """Subscribe to account / position updates via user data stream.

        The returned async generator yields ``AccountUpdate`` objects
        as the exchange pushes them.  The adapter manages the listenKey
        lifecycle (creation, keepalive, re-creation on disconnect)
        transparently.

        Yields:
            ``AccountUpdate`` for each account or position change.
        """
        ...

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    @abstractmethod
    async def get_exchange_info(self) -> dict:
        """Fetch raw exchange information (symbols, filters, rate limits).

        Returns:
            The full exchange info response as a dict.
        """
        ...

    @abstractmethod
    async def get_server_time(self) -> int:
        """Fetch the current exchange server time.

        Returns:
            Server time in unix milliseconds.
        """
        ...
