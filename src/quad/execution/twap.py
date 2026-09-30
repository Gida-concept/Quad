"""TWAP (Time-Weighted Average Price) order slicer.

Splits a large order into smaller child orders executed over a configurable
time window to minimise market impact.  Each slice is submitted sequentially
via the provided ``OrderGateway`` with randomised interval jitter to avoid
detectable patterns.

Algorithm
---------
1. Determine the number of slices by dividing total quantity by the minimum
   slice quantity, clamped within ``[min_slices, max_slices]``.
2. Compute a base slice size and distribute any remainder across the first
   N slices so the sum exactly equals the original quantity.
3. Space slices evenly across the time window, adding uniform random jitter
   of up to ``jitter_seconds`` to each inter-slice interval.
4. Monitor fill progress after each slice.  If less than 50 % of the order
   has filled and more than 80 % of the time window has elapsed, the
   remaining quantity is submitted as a single urgent slice.
"""

from __future__ import annotations

import asyncio
import random
import time
from decimal import Decimal
from typing import Any, Callable, Awaitable

import structlog

from quad.types.domain import OrderRequest

from .gateway import OrderGateway, OrderRejectedError, OrderResult

# ---------------------------------------------------------------------------
# Slicer
# ---------------------------------------------------------------------------


class TwapSlicer:
    """TWAP order slicer for splitting large orders into smaller child orders.

    Parameters
    ----------
    config:
        Configuration dictionary with optional keys
        (``min_slices``, ``max_slices``, ``default_window_seconds``,
        ``jitter_seconds``, ``min_slice_quantity``,
        ``fill_urgency_threshold``).  Sensible defaults are applied when a
        key is missing.
    """

    def __init__(
        self,
        config: dict[str, Any],
        on_fill: Callable[..., Awaitable[None]] | None = None,
    ) -> None:
        self._log = structlog.get_logger(__name__)
        self._config = config
        self._on_fill = on_fill

        self._min_slices = int(config.get("min_slices", 1))
        self._max_slices = int(config.get("max_slices", 10))
        self._default_window = int(config.get("default_window_seconds", 300))
        self._jitter_seconds = float(config.get("jitter_seconds", 5.0))
        self._min_slice_qty: Decimal = config.get(
            "min_slice_quantity", Decimal("0.001")
        )
        self._urgency_threshold = float(config.get("fill_urgency_threshold", 0.8))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def plan(
        self,
        order_request: OrderRequest,
        window_seconds: int = 300,
        step_size: Decimal | None = None,
        min_notional: Decimal | None = None,
    ) -> list[OrderRequest]:
        """Plan the slices without executing anything.

        Parameters
        ----------
        order_request:
            The parent order to split into slices.
        window_seconds:
            Total time window over which to spread the slices.
        step_size:
            Optional quantity step to align slices to; the residual needed
            to keep the total exact lands on the final slice.
        min_notional:
            Optional minimum notional per slice (with ``price``); the slice
            count is reduced until every slice clears it.

        Returns
        -------
        list[OrderRequest]
            A list of smaller ``OrderRequest`` objects, each with a
            ``client_order_id`` derived from the parent.
        """
        qty = order_request.quantity
        parent_id = order_request.client_order_id or "twap"

        if qty <= Decimal(0):
            self._log.warning(
                "twap_plan_zero_quantity",
                client_order_id=parent_id,
            )
            return []

        # Work in atomic units (minimum contract size)
        total_units = int(qty / self._min_slice_qty)
        if total_units < 1:
            self._log.warning(
                "twap_plan_below_min_unit",
                client_order_id=parent_id,
                total_units=total_units,
                min_slice_qty=str(self._min_slice_qty),
            )
            return []

        # Clamp slice count
        slice_count = min(max(total_units, self._min_slices), self._max_slices)

        # Honour min_notional: fewer, larger slices so each clears the floor.
        if (
            min_notional
            and min_notional > 0
            and order_request.price
            and order_request.price > 0
        ):
            while slice_count > 1:
                if (qty / slice_count) * order_request.price >= min_notional:
                    break
                slice_count -= 1

        # Distribute quantity evenly
        base_units = total_units // slice_count
        remainder = total_units % slice_count

        interval = window_seconds / slice_count if slice_count > 0 else 0

        parent_prefix = parent_id

        raw_quantities: list[Decimal] = []
        for i in range(slice_count):
            slice_units = base_units + (1 if i < remainder else 0)
            if slice_units < 1:
                continue
            raw_quantities.append(self._min_slice_qty * Decimal(str(slice_units)))

        # Align slices to step_size; the final slice absorbs the residual so
        # the slices still sum exactly to the parent quantity.
        quantities = raw_quantities
        if step_size is not None and step_size > 0 and len(raw_quantities) > 1:
            aligned: list[Decimal] = []
            assigned = Decimal(0)
            for i, q in enumerate(raw_quantities):
                if i < len(raw_quantities) - 1:
                    aq = (q // step_size) * step_size
                    if aq <= 0:
                        aq = q  # keep dust rather than dropping quantity
                    aligned.append(aq)
                    assigned += aq
                else:
                    aligned.append(qty - assigned)
            quantities = aligned

        slices: list[OrderRequest] = []
        for i, slice_qty in enumerate(quantities):
            if slice_qty <= 0:
                continue
            child_id = f"{parent_prefix}-slice-{i}"
            slices.append(
                OrderRequest(
                    symbol=order_request.symbol,
                    side=order_request.side,
                    order_type=order_request.order_type,
                    quantity=slice_qty,
                    price=order_request.price,
                    stop_price=order_request.stop_price,
                    time_in_force=order_request.time_in_force,
                    client_order_id=child_id,
                    reduce_only=order_request.reduce_only,
                    post_only=order_request.post_only,
                    working_type=order_request.working_type,
                    position_side=order_request.position_side,
                    price_protect=order_request.price_protect,
                )
            )
        # Attached TP/SL ride on the FINAL slice only: with tpslMode=Full
        # the position-level brackets cover the whole filled qty, and
        # attaching to every slice would move the triggers per slice.
        if slices and (
            order_request.take_profit_price is not None
            or order_request.stop_loss_price is not None
        ):
            slices[-1].take_profit_price = order_request.take_profit_price
            slices[-1].stop_loss_price = order_request.stop_loss_price

        self._log.debug(
            "twap_plan_created",
            parent_id=parent_prefix,
            slice_count=len(slices),
            window_seconds=window_seconds,
            interval_seconds=round(interval, 2),
        )
        return slices

    async def execute(
        self,
        order_request: OrderRequest,
        gateway: OrderGateway,
        window: int | None = None,
    ) -> list[OrderResult]:
        """Execute a large order as TWAP slices via the given gateway.

        Parameters
        ----------
        order_request:
            The parent order to execute as TWAP slices.
        gateway:
            The ``OrderGateway`` through which each slice is submitted.
        window:
            Total time window for the slices (defaults to
            ``default_window_seconds``).

        Returns
        -------
        list[OrderResult]
            Results for each submitted slice (including the urgent final
            slice if the urgency threshold was triggered).
        """
        window = self._default_window if window is None else window

        # Fetch exchange filters for slice alignment (LOT_SIZE / MIN_NOTIONAL)
        step_size = None
        min_notional = None
        try:
            filters = await gateway.get_symbol_filters(order_request.symbol)
            step_size = filters.get("step_size") or filters.get("stepSize")
            min_notional = filters.get("min_notional") or filters.get("minNotional")
        except Exception:
            self._log.debug("twap_filters_unavailable", symbol=order_request.symbol)

        slices = self.plan(
            order_request, window, step_size=step_size, min_notional=min_notional
        )

        if not slices:
            return []

        interval = window / len(slices)
        results: list[OrderResult] = []
        total_filled = Decimal(0)
        start_time = time.monotonic()

        for i, slice_req in enumerate(slices):
            # ----------------------------------------------------------
            # Submit current slice
            # ----------------------------------------------------------
            try:
                result = await gateway.submit(slice_req)
                results.append(result)
                total_filled += result.filled_qty

                # Notify fill callback (e.g. engine persistence)
                if result.status == "FILLED" and self._on_fill is not None:
                    try:
                        await self._on_fill(result)
                    except Exception:
                        self._log.debug(
                            "twap_on_fill_failed",
                            slice_index=i,
                            order_id=result.order_id,
                        )
            except OrderRejectedError as exc:
                self._log.warning(
                    "twap_slice_rejected",
                    slice_index=i,
                    client_order_id=slice_req.client_order_id,
                    error=str(exc),
                )
                continue

            # ----------------------------------------------------------
            # Check urgency after this slice
            # ----------------------------------------------------------
            if i < len(slices) - 1:
                elapsed = time.monotonic() - start_time
                time_frac = elapsed / window if window > 0 else 1.0
                fill_frac = (
                    total_filled / order_request.quantity
                    if order_request.quantity > 0
                    else Decimal(1)
                )

                if time_frac > self._urgency_threshold and fill_frac < Decimal("0.5"):
                    remaining = order_request.quantity - total_filled
                    if remaining > Decimal(0):
                        self._log.info(
                            "twap_urgency_triggered",
                            time_frac=round(time_frac, 3),
                            fill_frac=str(fill_frac),
                            remaining=str(remaining),
                        )
                        urgent_req = OrderRequest(
                            symbol=slice_req.symbol,
                            side=slice_req.side,
                            order_type=slice_req.order_type,
                            quantity=remaining,
                            price=order_request.price,
                            stop_price=slice_req.stop_price,
                            time_in_force=slice_req.time_in_force,
                            client_order_id=(
                                f"{order_request.client_order_id or 'twap'}-urgent-{i}"
                            ),
                            reduce_only=slice_req.reduce_only,
                            post_only=slice_req.post_only,
                            working_type=slice_req.working_type,
                            position_side=slice_req.position_side,
                            price_protect=slice_req.price_protect,
                        )
                        try:
                            urgent_result = await gateway.submit(urgent_req)
                            results.append(urgent_result)
                            total_filled += urgent_result.filled_qty

                            # Notify fill callback for urgent slice
                            if (
                                urgent_result.status == "FILLED"
                                and self._on_fill is not None
                            ):
                                try:
                                    await self._on_fill(urgent_result)
                                except Exception:
                                    self._log.debug(
                                        "twap_on_fill_failed",
                                        slice_index=i,
                                        order_id=urgent_result.order_id,
                                    )
                        except OrderRejectedError as exc2:
                            self._log.warning(
                                "twap_urgent_slice_rejected",
                                error=str(exc2),
                            )
                    break  # No more scheduled slices

                # ----------------------------------------------------------
                # Sleep before next slice (with jitter)
                # ----------------------------------------------------------
                jitter = random.uniform(-self._jitter_seconds, self._jitter_seconds)
                await asyncio_sleep(max(0.0, interval + jitter))

        self._log.info(
            "twap_execution_complete",
            parent_id=order_request.client_order_id or "twap",
            slices_submitted=len(results),
            total_filled=str(total_filled),
        )
        return results


# Re-export OrderResult so callers don't need a separate import
__all__ = [
    "OrderResult",
    "TwapSlicer",
]


# Small helper to avoid clashing with built-in ``time.sleep``
async def asyncio_sleep(delay: float) -> None:
    """Async sleep helper (wraps ``asyncio.sleep``)."""
    await asyncio.sleep(delay)
