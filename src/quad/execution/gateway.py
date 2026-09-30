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
from typing import Any

import structlog

from quad.common.retry import exponential_backoff, retry_async
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
        super().__init__(f"Order {client_order_id} not confirmed within {timeout}s")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TERMINAL_STATUSES = frozenset(
    {"FILLED", "CANCELLED", "CANCELED", "REJECTED", "EXPIRED"}
)
"""Order statuses that end active tracking (both -LL- and -L- spellings)."""

#: First retry delay for a transient order-submission failure, in seconds.
#: The schedule doubles from here (1s, 2s, 4s, ...) up to a 30s cap.
_ORDER_RETRY_BASE_S = 1.0

# (now configured via gateway config section)

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
        self._gateway_config = self._config.get("exchange", {}).get("gateway", {})

        self._active_orders: dict[str, Order] = {}
        self._completed_ids: deque[str] = deque(maxlen=self._completed_ids_maxlen)
        self._pending_confirmations: dict[str, asyncio.Event] = {}

    # ------------------------------------------------------------------
    # Config-derived properties
    # ------------------------------------------------------------------

    @property
    def _confirmation_timeout(self) -> float:
        return float(self._gateway_config["confirmation_timeout_seconds"])

    @property
    def _max_retries(self) -> int:
        return int(self._gateway_config["max_retries"])

    @property
    def _completed_ids_maxlen(self) -> int:
        return int(self._gateway_config["completed_ids_maxlen"])

    @property
    def _backoff_base(self) -> float:
        """Configured base backoff (``gateway.backoff_base_seconds``).

        NOTE: this is currently *not* used by the submit retry schedule, which
        is fixed at ``_ORDER_RETRY_BASE_S`` (1s/2s/4s..., capped at 30s) to
        preserve the long-standing, test-pinned timing for live order
        submission.  Wiring the config through is a deliberate behaviour
        change, not a refactor, so it has been left for an explicit decision.
        """
        return float(self._gateway_config["backoff_base_seconds"])

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
        # Bybit orderLinkId allows up to 36 characters; the hex UUID
        # (32 chars, no hyphens) is unique and safely compliant.
        client_order_id = order_request.client_order_id or uuid.uuid4().hex
        request = OrderRequest(
            symbol=order_request.symbol,
            side=order_request.side,
            order_type=order_request.order_type,
            quantity=order_request.quantity,
            price=order_request.price,
            stop_price=order_request.stop_price,
            time_in_force=order_request.time_in_force,
            client_order_id=client_order_id,
            reduce_only=order_request.reduce_only,
            post_only=order_request.post_only,
            # Preserve conditional-order fields set by the execution engine
            # (workingType / priceProtect on TP-SL brackets, positionSide in
            # hedge mode).  These were previously dropped here, so the SL's
            # MARK_PRICE trigger and priceProtect never reached the API.
            working_type=order_request.working_type,
            position_side=order_request.position_side,
            price_protect=order_request.price_protect,
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
        async def _submit(attempt: int) -> OrderResult:
            try:
                submitted = await self._exchange.place_order(request)
            except (TimeoutError, ConnectionError):
                # Transient -- let retry_async decide whether to try again.
                raise
            except Exception as exc:
                # Non-transient error -- reject immediately
                self._pending_confirmations.pop(client_order_id, None)
                raise OrderRejectedError(str(exc), request) from exc
            # ACK received. Only a terminal REST result (FILLED/...) is
            # itself the confirmation signal. A non-terminal result
            # (NEW/...) must await WS fill confirmation below, so the
            # timeout can genuinely fire instead of being pre-satisfied.
            if submitted.status in _TERMINAL_STATUSES:
                event.set()
            return submitted

        def _log_retry(attempt: int, exc: Exception, delay: float) -> None:
            self._log.warning(
                "order_submit_retry",
                attempt=attempt,
                error=str(exc),
                client_order_id=client_order_id,
                delay_s=round(delay, 2),
            )

        try:
            result = await retry_async(
                _submit,
                attempts=self._max_retries,
                is_retryable=lambda exc: isinstance(
                    exc, (TimeoutError, ConnectionError)
                ),
                # Documented 1s/2s/4s... backoff, capped at 30s.  See
                # ``_backoff_base`` for why the configured base is not used.
                delay_for=lambda attempt, _exc: exponential_backoff(
                    attempt, _ORDER_RETRY_BASE_S, cap=30.0
                ),
                on_retry=_log_retry,
            )
        except (TimeoutError, ConnectionError) as exc:
            self._pending_confirmations.pop(client_order_id, None)
            raise OrderRejectedError(f"All retries exhausted: {exc}", request) from exc

        # 4. Wait for confirmation only when the result is NOT already
        #    terminal (FILLED/REJECTED/CANCELLED/EXPIRED). A terminal REST
        #    ACK needs no further WS fill confirmation. Non-terminal
        #    results (e.g. NEW/PARTIALLY_FILLED) still await WS confirmation
        #    with a timeout.
        if result.status not in _TERMINAL_STATUSES:
            try:
                await asyncio.wait_for(event.wait(), timeout=self._confirmation_timeout)
            except asyncio.TimeoutError:
                raise OrderTimeoutError(
                    client_order_id, int(self._confirmation_timeout)
                )
            finally:
                self._pending_confirmations.pop(client_order_id, None)
        else:
            self._pending_confirmations.pop(client_order_id, None)

        # 5. Track in active orders
        order = Order(
            id=result.order_id,
            client_order_id=client_order_id,
            symbol=result.symbol,
            side=result.side,
            order_type=result.order_type,
            quantity=result.quantity,
            filled_qty=result.filled_qty,
            price=result.price,
            status=result.status,
            created_at=int(time.time() * 1000),
            updated_at=int(time.time() * 1000),
        )
        self._active_orders[client_order_id] = order
        if order.status in _TERMINAL_STATUSES:
            # Already terminal (e.g. immediately FILLED): don't leak it in
            # active tracking forever; resolve to completed right away.
            self._move_to_completed(client_order_id)

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
            cancelled = await self._exchange.cancel_order(order.id, order.symbol)
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
                    refreshed = await self._exchange.get_order_status(
                        order.id, order.symbol
                    )
                    # get_order_status returns None for an unknown id (the
                    # adapter raises only on transport errors), so reading
                    # .status off it unguarded raised AttributeError.
                    if refreshed is not None:
                        order.status = refreshed.status
                        order.filled_qty = refreshed.filled_qty
                        order.updated_at = int(time.time() * 1000)
                except Exception:  # noqa: S110  Return what we have in memory
                    pass
            return order

        # Fallback: scan exchange open orders
        try:
            open_orders = await self._exchange.get_open_orders()
            for o in open_orders:
                if o.client_order_id == client_order_id:
                    return o
        except Exception:  # noqa: S110  best-effort lookup; caller handles absence
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

        # Build lookup by exchange order ID.  Ids are `int | str` — Bybit
        # returns orderLinkId strings — so the containers must be keyed
        # accordingly (annotating them as int was wrong, not just untidy).
        exchange_ids: set[int | str] = set()
        exchange_map: dict[int | str, Order] = {}
        for o in open_orders:
            if o.id is not None:
                exchange_ids.add(o.id)
                exchange_map[o.id] = o

        # Reconcile our active orders
        to_remove: list[str] = []
        for client_id, local_order in self._active_orders.items():
            if local_order.id is None:
                if local_order.status in _TERMINAL_STATUSES:
                    # Local-terminal order with no exchange ID: nothing left
                    # to track; resolve it instead of holding it forever.
                    to_remove.append(client_id)
                continue

            if local_order.status in _TERMINAL_STATUSES:
                # Already terminal locally: evict regardless of exchange view.
                to_remove.append(client_id)
                continue

            # Declared Optional because the else-branch below rebinds it to a
            # ``get_order_status`` result, which is ``None`` when the order is
            # absent on the exchange.  The declaration is bare so each branch
            # narrows it to what it actually assigned.
            ex_order: Order | None
            if local_order.id in exchange_ids:
                # Still open -- update from exchange
                ex_order = exchange_map[local_order.id]
                local_order.status = ex_order.status
                local_order.filled_qty = ex_order.filled_qty
                local_order.price = ex_order.price
                local_order.updated_at = int(time.time() * 1000)
            else:
                # No longer in open orders -- query individually
                try:
                    ex_order = await self._exchange.get_order_status(
                        local_order.id, local_order.symbol
                    )
                except Exception as exc:
                    if self._exchange.is_order_not_found(exc):
                        # The exchange no longer knows this order (cancelled
                        # / expired / filled-and-closed -- e.g. TP-SL brackets
                        # left behind after a position was flattened).  It
                        # will never return to open orders, so resolve it
                        # locally instead of polling it forever every cycle.
                        self._log.warning(
                            "refresh_state_order_not_found",
                            client_order_id=client_id,
                            exchange_order_id=local_order.id,
                            error=str(exc),
                        )
                        local_order.status = "CANCELLED"
                        to_remove.append(client_id)
                    else:
                        self._log.warning(
                            "refresh_state_query_failed",
                            client_order_id=client_id,
                            exchange_order_id=local_order.id,
                            error=str(exc),
                        )
                    continue

                if ex_order is None:
                    self._log.warning(
                        "refresh_state_order_none",
                        client_order_id=client_id,
                        exchange_order_id=local_order.id,
                    )
                    local_order.status = "CANCELLED"
                    to_remove.append(client_id)
                    continue

                local_order.status = ex_order.status
                local_order.filled_qty = ex_order.filled_qty
                local_order.updated_at = int(time.time() * 1000)

                # Terminal status -> move to completed
                if local_order.status in _TERMINAL_STATUSES:
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

    async def get_symbol_filters(self, symbol: str) -> dict[str, Any]:
        """Retrieve exchange LOT_SIZE / MIN_NOTIONAL filters for a symbol.

        Delegates to the underlying exchange adapter.  Returns an empty
        dict on failure so callers can degrade gracefully.
        """
        try:
            return await self._exchange.get_symbol_filters(symbol)
        except Exception as exc:
            self._log.debug(
                "get_symbol_filters_failed",
                symbol=symbol,
                error=str(exc),
            )
            return {}

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
