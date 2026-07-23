"""Execution engine -- top-level orchestrator for order execution.

The ``ExecutionEngine`` coordinates risk checking, order submission, TWAP
execution, and periodic fill reconciliation.  It is the primary interface
between strategy decisions and the exchange adapter.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import structlog

from quad.exchange.base import ExchangeAdapter
from quad.persistence.database import DatabaseManager
from quad.risk.manager import RiskManager
from quad.types.domain import Order, OrderRequest, OrderResult
from quad.types.risk import Action, RiskResult
from quad.types.strategy import StrategyContext

from .gateway import OrderGateway, OrderRejectedError, OrderTimeoutError
from .reconciler import FillReconciler
from .twap import TwapSlicer

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_RECONCILE_INTERVAL = 60  # seconds

# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class ExecutionEngine:
    """Top-level execution orchestrator.

    Manages the complete order lifecycle: risk checks, single-order
    submission, TWAP-sliced execution, and periodic reconciliation of local
    state against the exchange.

    Parameters
    ----------
    exchange_adapter:
        The exchange adapter for order placement and queries.
    risk_manager:
        The risk manager used to evaluate all proposed trades.
    db_manager:
        Optional database manager for optional persistence of reconciliation
        results.
    config:
        Optional configuration dictionary.  Supported keys:

        * ``reconcile_interval`` (int, default 60) -- seconds between
          background reconciliation runs.
    """

    def __init__(
        self,
        exchange_adapter: ExchangeAdapter,
        risk_manager: RiskManager,
        db_manager: DatabaseManager | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        self._log = structlog.get_logger(__name__)
        self._config = config or {}

        self._gateway = OrderGateway(exchange_adapter, config=self._config)
        self._twap = TwapSlicer(config=self._config)
        self._reconciler = FillReconciler(
            exchange_adapter, db_manager=db_manager
        )
        self._risk_manager = risk_manager

        self._recon_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

        # Statistics counters
        self._stats: dict[str, int] = {
            "total_submitted": 0,
            "total_filled": 0,
            "total_rejected": 0,
            "active_order_count": 0,
            "twap_executions": 0,
            "reconciliations_run": 0,
        }

        self._log.info("execution_engine_initialized")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the background reconciliation loop.

        The reconciliation loop runs every ``reconcile_interval`` seconds
        (default 60) and refreshes gateway state and runs fill
        reconciliation for any tracked active orders.

        Safe to call multiple times (idempotent).
        """
        if self._recon_task is not None and not self._recon_task.done():
            self._log.warning("engine_already_running")
            return

        self._stop_event.clear()
        self._recon_task = asyncio.create_task(self._reconciliation_loop())
        self._log.info("execution_engine_started")

    async def stop(self) -> None:
        """Gracefully stop the background reconciliation loop.

        Sets the stop signal and cancels the pending background task.
        Any in-flight reconciliation will be interrupted.
        """
        self._stop_event.set()
        if self._recon_task is not None and not self._recon_task.done():
            self._recon_task.cancel()
            try:
                await self._recon_task
            except asyncio.CancelledError:
                pass
            self._recon_task = None
        self._log.info("execution_engine_stopped")

    # ------------------------------------------------------------------
    # Order execution
    # ------------------------------------------------------------------

    async def execute(
        self,
        action: Action,
        context: StrategyContext,
    ) -> OrderResult:
        """Evaluate risk and execute a single order.

        Flow
        ----
        1. Build an ``OrderRequest`` from the given ``Action``.
        2. Run ``RiskManager.evaluate(action, context)``.
        3. If risk check fails, return a ``REJECTED`` ``OrderResult``.
        4. Submit via ``OrderGateway.submit()``.
        5. Log the outcome and update statistics.

        Parameters
        ----------
        action:
            The trading action to execute.
        context:
            Current strategy context for risk evaluation.

        Returns
        -------
        OrderResult
            The result, or a ``REJECTED`` result if risk or submission
            failed.
        """
        order_request = self._build_request(action)

        # 1. Risk check
        risk_result = await self._risk_manager.evaluate(action, context)
        if not risk_result.passed:
            self._log.warning(
                "order_rejected_by_risk",
                action_type=action.type,
                contract=action.contract,
                reason=risk_result.reason,
                gate=risk_result.gate,
            )
            self._stats["total_rejected"] += 1
            return OrderResult(
                order_id=0,
                client_order_id=order_request.client_order_id,
                symbol=order_request.symbol,
                side=order_request.side,
                type=order_request.type,
                quantity=order_request.quantity,
                price=order_request.price,
                status="REJECTED",
                fills=[],
            )

        # 2. Submit
        try:
            result = await self._gateway.submit(order_request)
        except (OrderRejectedError, OrderTimeoutError) as exc:
            self._log.error(
                "order_submission_failed",
                error=str(exc),
                client_order_id=order_request.client_order_id,
            )
            self._stats["total_rejected"] += 1
            return OrderResult(
                order_id=0,
                client_order_id=order_request.client_order_id,
                symbol=order_request.symbol,
                side=order_request.side,
                type=order_request.type,
                quantity=order_request.quantity,
                price=order_request.price,
                status="REJECTED",
                fills=[],
            )

        # 3. Update stats
        self._stats["total_submitted"] += 1
        if result.status == "FILLED":
            self._stats["total_filled"] += 1
        self._stats["active_order_count"] = self._gateway.get_active_order_count()

        self._log.info(
            "order_executed",
            client_order_id=result.client_order_id,
            exchange_order_id=result.order_id,
            symbol=result.symbol,
            side=result.side,
            qty=str(result.quantity),
            status=result.status,
        )
        return result

    async def execute_twap(
        self,
        action: Action,
        context: StrategyContext,
        window: int = 300,
    ) -> list[OrderResult]:
        """Evaluate risk and execute an order as TWAP slices.

        Parameters
        ----------
        action:
            The trading action to execute as TWAP slices.
        context:
            Current strategy context for risk evaluation.
        window:
            Total time window in seconds for the TWAP execution.

        Returns
        -------
        list[OrderResult]
            Results for each submitted slice.  If risk fails, returns a
            single-element list with a ``REJECTED`` result.
        """
        order_request = self._build_request(action)

        # 1. Risk check
        risk_result = await self._risk_manager.evaluate(action, context)
        if not risk_result.passed:
            self._log.warning(
                "twap_rejected_by_risk",
                action_type=action.type,
                contract=action.contract,
                reason=risk_result.reason,
            )
            self._stats["total_rejected"] += 1
            return [
                OrderResult(
                    order_id=0,
                    client_order_id=order_request.client_order_id,
                    symbol=order_request.symbol,
                    side=order_request.side,
                    type=order_request.type,
                    quantity=order_request.quantity,
                    price=order_request.price,
                    status="REJECTED",
                    fills=[],
                )
            ]

        # 2. Execute TWAP
        try:
            results = await self._twap.execute(order_request, self._gateway)
        except Exception as exc:
            self._log.exception(
                "twap_execution_failed",
                error=str(exc),
            )
            return [
                OrderResult(
                    order_id=0,
                    client_order_id=order_request.client_order_id,
                    symbol=order_request.symbol,
                    side=order_request.side,
                    type=order_request.type,
                    quantity=order_request.quantity,
                    price=order_request.price,
                    status="REJECTED",
                    fills=[],
                )
            ]

        # 3. Update stats
        self._stats["twap_executions"] += 1
        self._stats["total_submitted"] += len(results)
        for r in results:
            if r.status == "FILLED":
                self._stats["total_filled"] += 1
        self._stats["active_order_count"] = self._gateway.get_active_order_count()

        self._log.info(
            "twap_executed",
            parent_id=order_request.client_order_id or "twap",
            slice_count=len(results),
            window_seconds=window,
        )
        return results

    async def cancel_order(self, client_order_id: str) -> bool:
        """Cancel an order by its client-assigned identifier.

        Parameters
        ----------
        client_order_id:
            The client-assigned order identifier.

        Returns
        -------
        bool
            ``True`` if cancellation was accepted by the exchange.
        """
        self._log.info(
            "cancelling_order",
            client_order_id=client_order_id,
        )
        result = await self._gateway.cancel(client_order_id)
        self._stats["active_order_count"] = self._gateway.get_active_order_count()
        return result

    async def reconcile(self) -> dict[str, Any]:
        """Run reconciliation on demand.

        Refreshes gateway state and runs the ``FillReconciler`` against all
        currently tracked active orders.

        Returns
        -------
        dict
            A reconciliation summary with the number of discrepancies found.
        """
        await self._gateway.refresh_state()
        active = self._gateway.get_active_orders()
        discrepancies = await self._reconciler.reconcile_pending_orders(active)
        self._stats["reconciliations_run"] += 1

        self._log.info(
            "reconciliation_complete",
            active_orders=len(active),
            discrepancies=len(discrepancies),
        )
        return {
            "active_orders_checked": len(active),
            "discrepancies_found": len(discrepancies),
            "discrepancies": discrepancies,
        }

    def get_active_orders(self) -> list[Order]:
        """Return all currently tracked active orders."""
        return self._gateway.get_active_orders()

    def get_stats(self) -> dict[str, int]:
        """Return execution statistics.

        Returns
        -------
        dict
            Counters: ``total_submitted``, ``total_filled``,
            ``total_rejected``, ``active_order_count``,
            ``twap_executions``, ``reconciliations_run``.
        """
        return dict(self._stats)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _reconciliation_loop(self) -> None:
        """Background loop that periodically reconciles order state."""
        interval = self._config.get(
            "reconcile_interval", _DEFAULT_RECONCILE_INTERVAL
        )

        while not self._stop_event.is_set():
            try:
                await self._gateway.refresh_state()
                active = self._gateway.get_active_orders()
                if active:
                    await self._reconciler.reconcile_pending_orders(active)
                self._stats["reconciliations_run"] += 1
                self._stats["active_order_count"] = len(active)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._log.exception(
                    "reconciliation_loop_error",
                    error=str(exc),
                )

            # Wait for next cycle (or stop signal)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=interval,
                )
            except asyncio.TimeoutError:
                pass  # Normal -- time to run the next cycle

    def _build_request(self, action: Action) -> OrderRequest:
        """Build an ``OrderRequest`` from an ``Action``."""
        return OrderRequest(
            symbol=action.contract or "",
            side=action.side or "",
            type=action.order_type or "LIMIT",
            quantity=action.quantity,
            price=action.price,
            reduce_only=False,
            post_only=False,
        )
