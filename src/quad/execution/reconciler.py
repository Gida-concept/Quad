"""Fill reconciliation -- detect discrepancies between local and exchange state.

The ``FillReconciler`` compares locally tracked order state against the
exchange's view to identify discrepancies that can occur during WebSocket
disconnections, network partitions, or race conditions.

Discrepancy types
-----------------
* ``MISSED_FILL`` -- The exchange reports the order as filled (or partially
  filled) but the local state has not recorded the fill.
* ``MISSED_REJECTION`` -- The exchange rejected / expired the order but the
  local state still shows it as active.
* ``STALE_ORDER`` -- The order has been open for more than 24 hours and may
  need manual attention.
* ``PRICE_MISMATCH`` -- The price recorded locally differs from the exchange
  price.
"""

from __future__ import annotations

import time
from collections import deque
from datetime import datetime
from decimal import Decimal
from typing import Any

import structlog

from quad.exchange.base import ExchangeAdapter
from quad.persistence.database import DatabaseManager
from quad.types.domain import Order, Trade

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_DISCREPANCY_HISTORY = 500
_STALE_ORDER_HOURS = 24
_STALE_ORDER_MS = _STALE_ORDER_HOURS * 3600 * 1000

# ---------------------------------------------------------------------------
# Reconciler
# ---------------------------------------------------------------------------


class FillReconciler:
    """Detects discrepancies between local order state and exchange state.

    Parameters
    ----------
    exchange_adapter:
        The exchange adapter used to query order state.
    db_manager:
        Optional database manager for persisting reconciliation results.
    """

    def __init__(
        self,
        exchange_adapter: ExchangeAdapter,
        db_manager: DatabaseManager | None = None,
    ) -> None:
        self._log = structlog.get_logger(__name__)
        self._exchange = exchange_adapter
        self._db = db_manager
        self._discrepancy_history: deque[dict[str, Any]] = deque(
            maxlen=_MAX_DISCREPANCY_HISTORY
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def reconcile_pending_orders(
        self,
        active_orders: list[Order],
    ) -> list[dict[str, Any]]:
        """Check each active order against the exchange for discrepancies.

        Parameters
        ----------
        active_orders:
            List of locally tracked active orders.

        Returns
        -------
        list[dict]
            Each dict describes a single discrepancy with keys:
            ``type``, ``client_order_id``, ``exchange_order_id``,
            ``symbol``, ``local_status``, ``exchange_status``,
            ``timestamp``, and optional ``details``.
        """
        discrepancies: list[dict[str, Any]] = []
        now_ms = int(time.time() * 1000)

        for order in active_orders:
            if order.id is None:
                continue

            try:
                ex_order = await self._exchange.get_order_status(order.id)
            except Exception as exc:
                self._log.warning(
                    "reconcile_query_failed",
                    exchange_order_id=order.id,
                    error=str(exc),
                )
                continue

            # 1. Status comparison
            if ex_order.status != order.status:
                if ex_order.status == "FILLED" and order.status in (
                    "NEW",
                    "PARTIALLY_FILLED",
                ):
                    disc = self._record_discrepancy(
                        "MISSED_FILL",
                        order,
                        ex_order.status,
                        now_ms,
                        details={
                            "exchange_filled_qty": str(ex_order.filled_qty),
                        },
                    )
                    discrepancies.append(disc)
                elif ex_order.status == "PARTIALLY_FILLED" and order.status in (
                    "NEW",
                ):
                    disc = self._record_discrepancy(
                        "MISSED_FILL",
                        order,
                        ex_order.status,
                        now_ms,
                        details={
                            "exchange_filled_qty": str(ex_order.filled_qty),
                        },
                    )
                    discrepancies.append(disc)
                elif ex_order.status in ("REJECTED", "EXPIRED") and order.status in (
                    "NEW",
                    "PARTIALLY_FILLED",
                ):
                    disc = self._record_discrepancy(
                        "MISSED_REJECTION",
                        order,
                        ex_order.status,
                        now_ms,
                    )
                    discrepancies.append(disc)

            # 2. Stale order check
            if order.status in ("NEW", "PARTIALLY_FILLED"):
                if order.created_at > 0:
                    age_ms = now_ms - order.created_at
                    if age_ms > _STALE_ORDER_MS:
                        disc = self._record_discrepancy(
                            "STALE_ORDER",
                            order,
                            ex_order.status,
                            now_ms,
                            details={
                                "age_hours": round(
                                    age_ms / 3600_000, 1
                                ),
                            },
                        )
                        discrepancies.append(disc)

                # Also check exchange-side creation time if available
                if ex_order.created_at > 0:
                    ex_age_ms = now_ms - ex_order.created_at
                    if ex_age_ms > _STALE_ORDER_MS:
                        disc = self._record_discrepancy(
                            "STALE_ORDER",
                            order,
                            ex_order.status,
                            now_ms,
                            details={
                                "age_hours": round(
                                    ex_age_ms / 3600_000, 1
                                ),
                                "source": "exchange",
                            },
                        )
                        # Avoid duplicate if we already flagged from local
                        if not any(
                            d["client_order_id"] == order.client_order_id
                            and d["type"] == "STALE_ORDER"
                            for d in discrepancies
                        ):
                            discrepancies.append(disc)

            # 3. Price mismatch
            if (
                ex_order.price is not None
                and order.price is not None
                and ex_order.price != order.price
            ):
                disc = self._record_discrepancy(
                    "PRICE_MISMATCH",
                    order,
                    ex_order.status,
                    now_ms,
                    details={
                        "local_price": str(order.price),
                        "exchange_price": str(ex_order.price),
                    },
                )
                discrepancies.append(disc)

        if discrepancies:
            self._log.warning(
                "reconcile_discrepancies_found",
                count=len(discrepancies),
                types=[d["type"] for d in discrepancies],
            )
        else:
            self._log.debug("reconcile_no_discrepancies")

        return discrepancies

    async def detect_missed_fills(
        self,
        local_trades: list[Trade],
        exchange_trades: list[Trade],
    ) -> list[Trade]:
        """Detect fills that exist on the exchange but are missing locally.

        Parameters
        ----------
        local_trades:
            Trades recorded in the local database.
        exchange_trades:
            Trades fetched from the exchange.

        Returns
        -------
        list[Trade]
            Trades that appear on the exchange but not in the local dataset.
        """
        local_ids: set[int] = {
            t.id for t in local_trades if t.id is not None
        }
        missed = [
            t
            for t in exchange_trades
            if t.id is not None and t.id not in local_ids
        ]

        if missed:
            self._log.warning(
                "missed_fills_detected",
                count=len(missed),
                trade_ids=[t.id for t in missed],
            )
            for trade in missed:
                self._discrepancy_history.append(
                    {
                        "type": "MISSED_FILL",
                        "trade_id": trade.id,
                        "symbol": trade.symbol,
                        "side": trade.side,
                        "quantity": str(trade.quantity),
                        "price": str(trade.price),
                        "timestamp": time.time(),
                    }
                )

        return missed

    async def generate_report(
        self,
        start: datetime,
        end: datetime,
    ) -> dict[str, Any]:
        """Generate a full reconciliation report for a time period.

        Parameters
        ----------
        start:
            Start of the reporting period.
        end:
            End of the reporting period.

        Returns
        -------
        dict
            Report with period info and filtered discrepancy history.
        """
        start_ts = start.timestamp()
        end_ts = end.timestamp()

        filtered = [
            d
            for d in self._discrepancy_history
            if start_ts <= d.get("timestamp", 0) <= end_ts
        ]

        total_filled = sum(
            1 for d in filtered if d.get("type") == "MISSED_FILL"
        )
        total_rejected = sum(
            1 for d in filtered if d.get("type") == "MISSED_REJECTION"
        )
        total_stale = sum(
            1 for d in filtered if d.get("type") == "STALE_ORDER"
        )
        total_price = sum(
            1 for d in filtered if d.get("type") == "PRICE_MISMATCH"
        )

        report: dict[str, Any] = {
            "period": {
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
            "summary": {
                "total_discrepancies": len(filtered),
                "missed_fills": total_filled,
                "missed_rejections": total_rejected,
                "stale_orders": total_stale,
                "price_mismatches": total_price,
            },
            "discrepancies": filtered,
        }

        self._log.info(
            "reconciliation_report_generated",
            total=len(filtered),
        )
        return report

    def get_recent_discrepancies(
        self,
        count: int = 20,
    ) -> list[dict[str, Any]]:
        """Return the most recent discrepancies.

        Parameters
        ----------
        count:
            Maximum number of entries to return (default 20).

        Returns
        -------
        list[dict]
            The most recent ``count`` discrepancies.
        """
        total = len(self._discrepancy_history)
        return list(self._discrepancy_history)[-min(count, total):]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _record_discrepancy(
        self,
        disc_type: str,
        order: Order,
        exchange_status: str,
        timestamp_ms: int,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a discrepancy record and append to the history ring buffer."""
        record: dict[str, Any] = {
            "type": disc_type,
            "client_order_id": order.client_order_id,
            "exchange_order_id": order.id,
            "symbol": order.symbol,
            "local_status": order.status,
            "exchange_status": exchange_status,
            "timestamp": timestamp_ms / 1000.0,
        }
        if details:
            record["details"] = details

        self._discrepancy_history.append(record)
        return record
