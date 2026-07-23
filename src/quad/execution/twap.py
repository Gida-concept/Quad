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

import random
import time
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import structlog

from quad.types.domain import OrderRequest

from .gateway import OrderGateway, OrderRejectedError, OrderResult

# ---------------------------------------------------------------------------
# Default configuration
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG: dict[str, Any] = {
    "min_slices": 3,
    "max_slices": 10,
    "default_window_seconds": 300,
    "slice_spacing_seconds": 30,
    "jitter_seconds": 5,
    "min_slice_quantity": Decimal("0.01"),
    "fill_urgency_threshold": 0.8,
}

# ---------------------------------------------------------------------------
# Slicer
# ---------------------------------------------------------------------------


class TwapSlicer:
    """TWAP order slicer for splitting large orders into smaller child orders.

    Parameters
    ----------
    config:
        Optional configuration dictionary.  Missing keys fall back to the
        defaults documented in the module-level ``_DEFAULT_CONFIG`` dict.
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._log = structlog.get_logger(__name__)
        cfg: dict[str, Any] = dict(_DEFAULT_CONFIG)
        if config:
            cfg.update(config)
        self._config = cfg

        self._min_slices = int(cfg["min_slices"])
        self._max_slices = int(cfg["max_slices"])
        self._default_window = int(cfg["default_window_seconds"])
        self._jitter_seconds = float(cfg["jitter_seconds"])
        self._min_slice_qty: Decimal = cfg["min_slice_quantity"]
        self._urgency_threshold = float(cfg["fill_urgency_threshold"])

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def plan(
        self,
        order_request: OrderRequest,
        window_seconds: int = 300,
    ) -> list[OrderRequest]:
        """Plan the slices without executing anything.

        Parameters
        ----------
        order_request:
            The parent order to split into slices.
        window_seconds:
            Total time window over which to spread the slices.

        Returns
        -------
        list[OrderRequest]
            A list of smaller ``OrderRequest`` objects, each with a
            ``client_order_id`` derived from the parent.
        """
        qty = order_request.quantity
        parent_id = order_request.client_order_id or "twap"

        if qty <= Decimal("0"):
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

        # Distribute quantity evenly
        base_units = total_units // slice_count
        remainder = total_units % slice_count

        interval = window_seconds / slice_count if slice_count > 0 else 0

        parent_prefix = parent_id

        slices: list[OrderRequest] = []
        for i in range(slice_count):
            slice_units = base_units + (1 if i < remainder else 0)
            if slice_units < 1:
                continue

            slice_qty = self._min_slice_qty * Decimal(str(slice_units))

            child_id = f"{parent_prefix}-slice-{i}"
            slices.append(
                OrderRequest(
                    symbol=order_request.symbol,
                    side=order_request.side,
                    type=order_request.type,
                    quantity=slice_qty,
                    price=order_request.price,
                    stop_price=order_request.stop_price,
                    time_in_force=order_request.time_in_force,
                    client_order_id=child_id,
                    reduce_only=order_request.reduce_only,
                    post_only=order_request.post_only,
                )
            )

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
    ) -> list[OrderResult]:
        """Execute a large order as TWAP slices via the given gateway.

        Parameters
        ----------
        order_request:
            The parent order to execute as TWAP slices.
        gateway:
            The ``OrderGateway`` through which each slice is submitted.

        Returns
        -------
        list[OrderResult]
            Results for each submitted slice (including the urgent final
            slice if the urgency threshold was triggered).
        """
        window = self._default_window
        slices = self.plan(order_request, window)

        if not slices:
            return []

        interval = window / len(slices)
        results: list[OrderResult] = []
        total_filled = Decimal("0")
        start_time = time.monotonic()

        for i, slice_req in enumerate(slices):
            # ----------------------------------------------------------
            # Submit current slice
            # ----------------------------------------------------------
            try:
                result = await gateway.submit(slice_req)
                results.append(result)
                total_filled += result.filled_qty
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
                    else Decimal("1")
                )

                if (
                    time_frac > self._urgency_threshold
                    and fill_frac < Decimal("0.5")
                ):
                    remaining = order_request.quantity - total_filled
                    if remaining > Decimal("0"):
                        self._log.info(
                            "twap_urgency_triggered",
                            time_frac=round(time_frac, 3),
                            fill_frac=str(fill_frac),
                            remaining=str(remaining),
                        )
                        urgent_req = OrderRequest(
                            symbol=slice_req.symbol,
                            side=slice_req.side,
                            type=slice_req.type,
                            quantity=remaining,
                            price=order_request.price,
                            client_order_id=(
                                f"{order_request.client_order_id or 'twap'}"
                                f"-urgent-{i}"
                            ),
                        )
                        try:
                            urgent_result = await gateway.submit(urgent_req)
                            results.append(urgent_result)
                            total_filled += urgent_result.filled_qty
                        except OrderRejectedError as exc2:
                            self._log.warning(
                                "twap_urgent_slice_rejected",
                                error=str(exc2),
                            )
                    break  # No more scheduled slices

                # ----------------------------------------------------------
                # Sleep before next slice (with jitter)
                # ----------------------------------------------------------
                jitter = random.uniform(
                    -self._jitter_seconds, self._jitter_seconds
                )
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
    "TwapSlicer",
    "OrderResult",
]


# Small helper to avoid clashing with built-in ``time.sleep``
async def asyncio_sleep(delay: float) -> None:
    """Async sleep helper (wraps ``asyncio.sleep``)."""
    import asyncio

    await asyncio.sleep(delay)
