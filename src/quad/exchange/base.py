"""Pluggable exchange adapter ABC for USD-margin futures trading.

Every exchange adapter — live, testnet, or backtest — implements this
interface so the rest of the application remains exchange-agnostic.

All monetary values use ``Decimal`` for precision.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation

import structlog

from quad.types.domain import (
    Account,
    Order,
    OrderRequest,
    OrderResult,
    Position,
    Trade,
)
from quad.types.exchange import AccountUpdate
from quad.types.market import FundingRate

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Shared exchange error hierarchy
# ---------------------------------------------------------------------------
# Both adapters raise these so higher layers (gateway, orchestrator) can catch
# exchange failures by type regardless of which exchange produced them.

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

    @property
    def is_testnet(self) -> bool:
        """Whether this adapter targets a testnet environment.

        Defaults to ``False``.  Live Binance adapters override this to
        report their ``testnet`` flag so higher layers (execution engine,
        orchestrator) can enforce a hard dry-run guard without trusting
        the raw config (which may be inconsistent with the adapter's
        actual resolved environment).
        """
        return False

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
    async def cancel_order(self, order_id: int, symbol: str = "") -> bool:
        """Cancel an order by exchange order ID.

        Args:
            order_id: The exchange-assigned order identifier.
            symbol: Optional contract symbol.  Binance requires ``symbol``
                (or ``origClientOrderId``) for ``DELETE /fapi/v1/order``.

        Returns:
            ``True`` if the cancellation was accepted, ``False`` if the
            order was not found or already filled/cancelled.
        """
        ...

    @abstractmethod
    async def get_order_status(self, order_id: int, symbol: str = "") -> Order:
        """Get the current status of an order from the exchange.

        Args:
            order_id: The exchange-assigned order identifier.
            symbol: Optional contract symbol.  Binance requires ``symbol``
                for ``GET /fapi/v1/order``.

        Returns:
            An ``Order`` dataclass with the latest status.
        """
        ...

    @abstractmethod
    async def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        """Get all currently open orders.

        Args:
            symbol: Optional symbol filter.  If ``None``, returns open
                orders for all symbols.

        Returns:
            A list of ``Order`` dataclasses for every open order.
        """
        ...

    @abstractmethod
    async def get_user_trades(
        self, symbol: str | None = None, limit: int = 500
    ) -> list[Trade]:
        """Fetch executed fills (income history) for the account.

        This is the source of truth for CLOSED legs.  The bot's
        ``trades`` journal only captured opening fills before this method
        existed, so SELL/exit legs (TP/SL brackets, exchange-triggered
        closes) never appeared and any daily-PnL computed from the journal
        was meaningless.  Implementing it lets the execution engine ingest
        both legs so the journal and daily PnL match reality.

        Args:
            symbol: Optional symbol filter.  If ``None``, returns trades
                for all symbols the account has traded.
            limit: Maximum number of fills to return (most-recent first).

        Returns:
            A list of ``Trade`` dataclasses.  Each fill carries ``side``
            (BUY/SELL), ``price``, ``quantity``, ``fee``, and (where the
            exchange provides it) an aggregate ``pnl``.
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

    async def get_symbol_filters(self, symbol: str) -> dict[str, Decimal]:
        """Return the cached LOT_SIZE / MIN_NOTIONAL filters for a symbol.

        Convenience wrapper around ``_get_lot_filters`` for callers that
        need the raw filter values (e.g. the execution engine flooring a
        sized quantity up to the exchange minimum).

        Returns:
            Dict with keys ``step_size``, ``min_qty``, ``min_notional``
            (all ``Decimal``).
        """
        step, min_qty, min_notional = await self._get_lot_filters(symbol)
        return {
            "step_size": step,
            "min_qty": min_qty,
            "min_notional": min_notional,
            "tick_size": await self.get_tick_size(symbol),
        }

    async def get_tick_size(self, symbol: str) -> Decimal:
        """Return the symbol's PRICE_FILTER ``tickSize`` (cached).

        The full exchange info is fetched once per symbol and cached for
        ``_exchange_info_ttl`` seconds (default 60), mirroring
        ``_get_lot_filters``.
        """
        cache: dict[str, tuple[float, Decimal]]
        if not hasattr(self, "_price_filter_cache"):
            self._price_filter_cache: dict[str, tuple[float, Decimal]] = {}
        cache = self._price_filter_cache
        ttl = float(getattr(self, "_exchange_info_ttl", 60))

        now = time.monotonic()
        cached = cache.get(symbol)
        if cached is not None and (now - cached[0]) < ttl:
            return cached[1]

        info = await self.get_exchange_info()
        tick = Decimal(0)
        for s in info.get("symbols", []):
            if s.get("symbol") != symbol:
                continue
            for f in s.get("filters", []):
                if f.get("filterType") == "PRICE_FILTER":
                    tick = Decimal(str(f.get("tickSize", "0")))
            break

        cache[symbol] = (now, tick)
        return tick

    async def normalize_price(
        self,
        symbol: str,
        price: Decimal | str | None,
    ) -> Decimal | None:
        """Round ``price`` UP/DOWN to the symbol's PRICE_FILTER ``tickSize``.

        Binance rejects a STOP_MARKET / TAKE_PROFIT_MARKET ``triggerPrice``
        (and any limit ``price``) whose decimal precision exceeds the
        symbol's tick with error -1111 ("Precision is over the maximum
        defined for this asset").  Rounds to the nearest tick; returns the
        price unchanged when no tick size is available.
        """
        if price is None:
            return None
        q = Decimal(str(price))
        tick = await self.get_tick_size(symbol)
        if tick <= Decimal(0):
            return q
        return (q / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick

    async def normalize_quantity(
        self,
        symbol: str,
        quantity: Decimal | str,
        price: Decimal | None = None,
    ) -> Decimal:
        """Normalize a quantity to the exchange's LOT_SIZE / MIN_NOTIONAL filters.

        Rounds **down** to ``stepSize`` (never up), then validates against
        ``minQty`` and ``minNotional`` (using the supplied ``price`` or the
        current mark price).

        Raises ``RuntimeError`` with a clear, exchange-error-mapped message
        when the quantity is below ``minQty`` (Binance ``-1113``/``-1111``)
        or when the implied notional is below ``minNotional`` (Binance
        ``-4164``) — the exchange would reject such an order anyway, so we
        fail loudly before it is ever sent.

        Filter data is cached per symbol for a short TTL
        (``_exchange_info_ttl``, default 60s) to avoid re-fetching the full
        ``/fapi/v1/exchangeInfo`` dump on every order.

        Parameters
        ----------
        symbol:
            Contract symbol, e.g. ``"BTCUSDT"``.
        quantity:
            Raw quantity to normalize (may be a ``Decimal`` or str).
        price:
            Optional reference price for the minNotional check.  Falls back
            to the current mark price when omitted.

        Returns:
            The normalized ``Decimal`` quantity, safe to submit to the
            exchange.

        Raises:
            RuntimeError: If the quantity is zero below minQty or its
                notional is below minNotional, or no LOT_SIZE filter is
                available for the symbol.
        """
        qty = Decimal(str(quantity))
        if qty <= Decimal(0):
            return qty

        step_size, min_qty, min_notional = await self._get_lot_filters(symbol)

        # Round DOWN to stepSize (never up).
        if step_size > Decimal(0):
            qty = (qty / step_size).to_integral_value(rounding=ROUND_DOWN) * step_size
            try:
                qty = qty.quantize(step_size, rounding=ROUND_DOWN)
            except (InvalidOperation, ValueError):
                # Integer step or a huge quantity; the division-based result
                # above is already correct.
                pass

        # Below minQty -> the exchange would reject with -1113/-1111.
        if qty < min_qty:
            raise RuntimeError(
                f"quantity {qty} below minQty {min_qty} for {symbol} "
                f"(exchange would reject; Binance -1113/-1111)"
            )

        # Below minNotional -> the exchange would reject with -4164.
        if min_notional > Decimal(0):
            px = price
            if px is None:
                try:
                    px = await self.get_mark_price(symbol)
                except Exception:
                    px = None  # cannot verify notional; minQty check still applied
            if px is not None and px > Decimal(0):
                notional = qty * px
                if notional < min_notional:
                    raise RuntimeError(
                        f"notional {notional} (qty {qty} x mark {px}) below "
                        f"minNotional {min_notional} for {symbol} "
                        f"(exchange would reject; Binance -4164)"
                    )

        log.debug(
            "quantity_normalized",
            symbol=symbol,
            original=str(Decimal(str(quantity))),
            normalized=str(qty),
            step_size=str(step_size),
            min_qty=str(min_qty),
            min_notional=str(min_notional),
        )
        return qty

    async def _get_lot_filters(self, symbol: str) -> tuple[Decimal, Decimal, Decimal]:
        """Return cached ``(step_size, min_qty, min_notional)`` for a symbol.

        The full exchange info is fetched once per symbol and cached for
        ``_exchange_info_ttl`` seconds (default 60).
        """
        cache: dict[str, tuple[float, tuple[Decimal, Decimal, Decimal]]]
        if not hasattr(self, "_exchange_info_cache"):
            self._exchange_info_cache: dict[
                str, tuple[float, tuple[Decimal, Decimal, Decimal]]
            ] = {}
        cache = self._exchange_info_cache
        ttl = float(getattr(self, "_exchange_info_ttl", 60))

        now = time.monotonic()
        cached = cache.get(symbol)
        if cached is not None and (now - cached[0]) < ttl:
            return cached[1]

        info = await self.get_exchange_info()
        step = min_qty = min_notional = Decimal(0)
        found = False
        for s in info.get("symbols", []):
            if s.get("symbol") != symbol:
                continue
            for f in s.get("filters", []):
                ftype = f.get("filterType")
                if ftype == "LOT_SIZE":
                    step = Decimal(str(f.get("stepSize", "0")))
                    min_qty = Decimal(str(f.get("minQty", "0")))
                    found = True
                elif ftype == "MIN_NOTIONAL":
                    min_notional = Decimal(str(f.get("notional", "0")))
            break

        if not found:
            raise RuntimeError(
                f"no LOT_SIZE filter found for {symbol} in exchange info"
            )

        result = (step, min_qty, min_notional)
        cache[symbol] = (now, result)
        return result

    @abstractmethod
    async def get_server_time(self) -> int:
        """Fetch the current exchange server time.

        Returns:
            Server time in unix milliseconds.
        """
        ...

    # ------------------------------------------------------------------
    # Exchange-specific error semantics (override per adapter)
    # ------------------------------------------------------------------

    def is_margin_mode_already_set(self, exc: Exception) -> bool:
        """Whether an exception means the requested margin mode is already active.

        Some exchanges raise an error when you try to set a margin/position
        mode that is already in effect. The orchestrator traps this as a benign
        no-op rather than a setup failure. Default ``False``; adapters override
        with their own error-code check.
        """
        return False

    def is_order_not_found(self, exc: Exception) -> bool:
        """Whether an exception means the order no longer exists on the exchange.

        Used to resolve ghost orders locally (cancelled / expired / filled-and-
        removed). Default ``False``; adapters override with their own error-code
        check.
        """
        return False
