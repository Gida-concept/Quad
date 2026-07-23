"""Order gateway -- handles order submission, lifecycle tracking, and retries.

Provides the ``OrderGateway`` class which manages the complete lifecycle of
orders submitted to the exchange: idempotent submission with UUID-based
client_order_id, exponential-backoff retry on transient failures, in-memory
active-order tracking bounded via a ring buffer of completed IDs, and
confirmation-event coordination for future WebSocket-based fill handling.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from decimal import Decimal
from typing import Any

import structlog

from quad.exchange.base import ExchangeAdapter
from quad.types.domain import Order, OrderRequest, OrderResult

# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class OrderRejectedError(Exception):
    """Raised when the exchange rejects an order."""

    def __init__(self, reason: str, order_request: OrderRequest) -> None:
        self.reason = reason
        self.order_request = order_request
        super().__init__(f"Order rejected: {reason}")


class OrderTimeoutError(Exception):
    """Raised when an order is not confirmed within the timeout window."""

    def __init__(self, client_order_id: str, timeout: int) -> None:
        self.client_order_id = client_order_id
        self.timeout = timeout
        super().__init__(
            f"Order {client_order_id} not confirmed within {timeout}s"
        )

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CONFIRMATION_TIMEOUT = 30
_MAX_RETRIES = 3
_COMPLETED_IDS_MAXLEN = 1000

# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------


class OrderGateway:
    """Handles order submission, lifecycle tracking, and retry logic.

    Every submitted order receives a UUID-based ``client_order_id`` for
    idempotent retry semantics.  Transient failures (timeout / connection)
    are retried with exponential backoff (1 s, 2 s, 4 s).  Non-transient
    failures raise ``OrderRejectedError`` immediately.

    Active orders are tracked in-memory under ``_active_orders`` (dict keyed
    by ``client_order_id``).  Orders that reach a terminal state are moved to
    ``_completed_ids``, a ring buffer with a maximum of 1000 entries, to
    prevent unbounded memory growth.

    Parameters
    ----------
    exchange_adapter:
        The exchange adapter used to place, cancel, and query orders.
    config:
        Optional configuration dictionary (reserved for future use).
    """

    def __init__(
        self,
        exchange_adapter: ExchangeAdapter,
        config: dict[str, Any] | None = None,
    ) -> None:
        self._log = structlog.get_logger(__name__)
        self._exchange = exchange_adapter
        self._config = config or {}

        self._active_orders: dict[str, Order] = {}
        self._completed_ids: deque[str] = deque(maxlen=_COMPLETED_IDS_MAXLEN)
        self._pending_confirmations: dict[str, asyncio.Event] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def submit(self, order_request: OrderRequest) -> OrderResult:
        """Submit an order with idempotency, retry, and confirmation tracking.

        Parameters
        ----------
        order_request:
            The order parameters.  If ``client_order_id`` is empty, a UUID
            is generated automatically.

        Returns
        -------
        OrderResult
            The result returned by the exchange adapter.

        Raises
        ------
        OrderRejectedError
            The exchange rejected the order, or all retry attempts on a
            transient failure were exhausted.
        OrderTimeoutError
            The order was accepted by the exchange but did not reach a
            confirmed state within the timeout window (default 30 s).
        """
        # 1. Idempotency key
        client_order_id = order_request.client_order_id or str(uuid.uuid4())
        request = OrderRequest(
            symbol=order_request.symbol,
            side=order_request.side,
            type=order_request.type,
            quantity=order_request.quantity,
            price=order_request.price,
            stop_price=order_request.stop_price,
            time_in_force=order_request.time_in_force,
            client_order_id=client_order_id,
            reduce_only=order_request.reduce_only,
            post_only=order_request.post_only,
        )

        # 2. Prepare confirmation event
        event = asyncio.Event()
        self._pending_confirmations[client_order_id] = event
        self._log.debug(
            "submitting_order",
            client_order_id=client_order_id,
            symbol=request.symbol,
            side=request.side,
            qty=str(request.quantity),
        )

        # 3. Submit with retry on transient failures
        last_error: Exception | None = None
        result: OrderResult | None = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                result = await self._exchange.place_order(request)
                last_error = None
                break
            except (TimeoutError, ConnectionError) as exc:
                last_error = exc
                self._log.warning(
                    "order_submit_retry",
                    attempt=attempt,
                    error=str(exc),
                    client_order_id=client_order_id,
                )
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(2 ** (attempt - 1))  # 1 s, 2 s, 4 s
            except Exception as exc:
                # Non-transient error -- reject immediately
                self._pending_confirmations.pop(client_order_id, None)
                raise OrderRejectedError(str(exc), request)

        if last_error is not None:
            self._pending_confirmations.pop(client_order_id, None)
            raise OrderRejectedError(
                f"All retries exhausted: {last_error}", request
            )

        # 4. Wait for confirmation (event is set immediately on REST success;
        #    in a WebSocket-based system a separate handler would set it)
        event.set()
        try:
            await asyncio.wait_for(event.wait(), timeout=_CONFIRMATION_TIMEOUT)
        except asyncio.TimeoutError:
            raise OrderTimeoutError(client_order_id, _CONFIRMATION_TIMEOUT)
        finally:
            self._pending_confirmations.pop(client_order_id, None)

        # 5. Track in active orders
        order = Order(
            id=result.order_id,
            client_order_id=client_order_id,
            symbol=result.symbol,
            side=result.side,
            type=result.type,
            quantity=result.quantity,
            filled_qty=result.filled_qty,
            price=result.price,
            status=result.status,
            created_at=int(time.time() * 1000),
            updated_at=int(time.time() * 1000),
        )
        self._active_orders[client_order_id] = order

        self._log.info(
            "order_submitted",
            client_order_id=client_order_id,
            exchange_order_id=result.order_id,
            status=result.status,
        )
        return result

    async def cancel(self, client_order_id: str) -> bool:
        """Cancel an order by its client-assigned identifier.

        Parameters
        ----------
        client_order_id:
            The client-assigned order identifier.

        Returns
        -------
        bool
            ``True`` if the cancellation was accepted by the exchange.
        """
        order = self._active_orders.get(client_order_id)
        if order is None or order.id is None:
            self._log.warning(
                "cancel_order_not_found",
                client_order_id=client_order_id,
            )
            return False

        self._log.info(
            "cancelling_order",
            client_order_id=client_order_id,
            exchange_order_id=order.id,
            symbol=order.symbol,
        )
        try:
            cancelled = await self._exchange.cancel_order(order.id)
            if cancelled:
                order.status = "CANCELLED"
                self._move_to_completed(client_order_id)
            return cancelled
        except Exception as exc:
            self._log.exception(
                "cancel_failed",
                client_order_id=client_order_id,
                error=str(exc),
            )
            return False

    async def get_status(self, client_order_id: str) -> Order | None:
        """Get the current status of an order.

        Checks the in-memory active-orders map first.  If the order is found
        and has an exchange-assigned ID, queries the exchange for the latest
        status.  Falls back to scanning exchange open orders.

        Parameters
        ----------
        client_order_id:
            The client-assigned order identifier.

        Returns
        -------
        Order | None
            The current order state, or ``None`` if the order is not tracked
            and not found on the exchange.
        """
        # Check memory first
        order = self._active_orders.get(client_order_id)
        if order is not None:
            # Freshen from exchange if we have an exchange order ID
            if order.id is not None:
                try:
                    ex_order = await self._exchange.get_order_status(order.id)
                    order.status = ex_order.status
                    order.filled_qty = ex_order.filled_qty
                    order.updated_at = int(time.time() * 1000)
                except Exception:
                    pass  # Return what we have in memory
            return order

        # Fallback: scan exchange open orders
        try:
            open_orders = await self._exchange.get_open_orders()
            for o in open_orders:
                if o.client_order_id == client_order_id:
                    return o
        except Exception:
            pass

        return None

    async def refresh_state(self) -> None:
        """Refresh active-order state by querying the exchange.

        Retrieves the list of open orders from the exchange and reconciles
        each locally tracked active order against the exchange data.

        Orders that appear to have reached a terminal state
        (FILLED / CANCELLED / REJECTED / EXPIRED) are moved to the completed
        ring buffer.
        """
        try:
            open_orders = await self._exchange.get_open_orders()
        except Exception as exc:
            self._log.warning("refresh_state_failed", error=str(exc))
            return

        # Build lookup by exchange order ID
        exchange_ids: set[int] = set()
        exchange_map: dict[int, Order] = {}
        for o in open_orders:
            if o.id is not None:
                exchange_ids.add(o.id)
                exchange_map[o.id] = o

        # Reconcile our active orders
        to_remove: list[str] = []
        for client_id, local_order in self._active_orders.items():
            if local_order.id is None:
                continue

            if local_order.id in exchange_ids:
                # Still open -- update from exchange
                ex_order = exchange_map[local_order.id]
                local_order.status = ex_order.status
                local_order.filled_qty = ex_order.filled_qty
                local_order.price = ex_order.price
                local_order.updated_at = int(time.time() * 1000)
            elif local_order.status in ("NEW", "PARTIALLY_FILLED"):
                # No longer in open orders -- query individually
                try:
                    ex_order = await self._exchange.get_order_status(
                        local_order.id
                    )
                    local_order.status = ex_order.status
                    local_order.filled_qty = ex_order.filled_qty
                    local_order.updated_at = int(time.time() * 1000)
                except Exception as exc:
                    self._log.warning(
                        "refresh_state_query_failed",
                        client_order_id=client_id,
                        exchange_order_id=local_order.id,
                        error=str(exc),
                    )
                    continue

                # Terminal status -> move to completed
                if local_order.status in (
                    "FILLED",
                    "CANCELLED",
                    "REJECTED",
                    "EXPIRED",
                ):
                    to_remove.append(client_id)

        for client_id in to_remove:
            self._move_to_completed(client_id)

        self._log.debug(
            "refresh_state_complete",
            active=len(self._active_orders),
            completed=len(self._completed_ids),
        )

    def get_active_order_count(self) -> int:
        """Return the number of tracked active (non-terminal) orders."""
        return len(self._active_orders)

    def get_active_orders(self) -> list[Order]:
        """Return all currently tracked active orders."""
        return list(self._active_orders.values())

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _move_to_completed(self, client_order_id: str) -> None:
        """Move an order from active tracking to the completed ring buffer."""
        if client_order_id in self._active_orders:
            del self._active_orders[client_order_id]
            self._completed_ids.append(client_order_id)
            self._log.debug(
                "order_moved_to_completed",
                client_order_id=client_order_id,
            )
