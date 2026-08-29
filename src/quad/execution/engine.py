"""Execution engine -- top-level orchestrator for order execution.

The ``ExecutionEngine`` coordinates risk checking, order submission, TWAP
execution, and periodic fill reconciliation.  It is the primary interface
between strategy decisions and the exchange adapter.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from decimal import ROUND_CEILING, Decimal, InvalidOperation
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


def _floor_to_compliant(
    qty: Decimal,
    min_qty: Decimal,
    min_notional: Decimal,
    step_size: Decimal,
    mark_price: Decimal | None,
) -> Decimal:
    """Raise ``qty`` to a step-aligned size that clears minQty AND minNotional.

    The engine floors a sized quantity that fell below the exchange filters
    UP to the smallest valid size: at least ``minQty``, and enough quantity so
    that ``qty * mark_price >= min_notional`` (rounded UP to ``step_size``).
    Returns the raised quantity; the caller must verify it does not exceed the
    pre-cap (the original requested quantity) before submitting.
    """
    target = max(qty, min_qty)
    if min_notional > Decimal(0) and mark_price is not None and mark_price > Decimal(0):
        needed = min_notional / mark_price
        if needed > target:
            target = needed
            if step_size > Decimal(0):
                target = (
                    (needed / step_size).to_integral_value(rounding=ROUND_CEILING)
                    * step_size
                )
    return target


def _as_dec(value: Any) -> Decimal:
    """Coerce an arbitrary exchange value to ``Decimal`` safely.

    Tolerates ``None``, strings, ints, and floats; returns ``Decimal(0)`` on
    anything unparseable so the ingest loop never dies on a malformed fill.
    """
    try:
        if value is None:
            return Decimal(0)
        return Decimal(str(value))
    except (TypeError, ValueError, InvalidOperation):
        return Decimal(0)


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
        # The adapter is read directly by the dry-run guard (``execute`` /
        # ``execute_twap``) and the quantity-normalization helpers, so it must
        # be stored on the instance as well as passed to the sub-components.
        self._exchange_adapter: ExchangeAdapter = exchange_adapter
        self._db_manager = db_manager

        self._gateway = OrderGateway(exchange_adapter, config=self._config)
        # Build the twap sub-config: TwapSlicer expects flat keys from
        # execution.twap.* plus a default_window_seconds key that maps
        # to execution.twap_window_seconds.
        exec_cfg = self._config.get("execution", {})
        twap_cfg = dict(exec_cfg.get("twap", {}))
        twap_cfg["default_window_seconds"] = int(
            exec_cfg.get("twap_window_seconds", 300)
        )
        self._twap = TwapSlicer(config=twap_cfg)
        self._reconciler = FillReconciler(
            exchange_adapter, db_manager=db_manager, config=self._config
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
        # 0. Hard dry-run guard: when dry-run mode is on but the exchange is
        #    LIVE, refuse every order before any risk work or submission.
        if self._is_dry_run and not getattr(
            self._exchange_adapter, "is_testnet", False
        ):
            self._log.critical(
                "dry_run_guard_blocked_order",
                action_type=action.type,
                contract=action.contract or action.symbol,
                side=action.side,
                qty=str(action.quantity),
            )
            self._stats["total_rejected"] += 1
            return self._rejected_result(action)

        context = self._ensure_context(context)
        original_qty = action.quantity

        # 1. Risk check (skip if already checked upstream)
        if action.risk_checked:
            risk_result = RiskResult(passed=True)
        else:
            risk_result = await self._risk_manager.evaluate(action, context)

        # 2. Apply the risk-sized action (Fix #4): if RiskManager.evaluate
        #    produced a sized action in details["action"], use its quantity
        #    (and any other risk adjustments) instead of the raw quantity so
        #    max_position_size_usd / Kelly caps actually constrain the order.
        sized = risk_result.details.get("action") if risk_result.passed else None
        if sized is not None:
            action = sized

        if not risk_result.passed:
            self._log.warning(
                "order_rejected_by_risk",
                action_type=action.type,
                contract=action.contract,
                reason=risk_result.reason,
                gate=risk_result.gate,
            )
            self._stats["total_rejected"] += 1
            return self._rejected_result(action, reason=risk_result.reason)

        # 3. Prepare the final quantity: normalize to the exchange filters
        #    (LOT_SIZE / MIN_NOTIONAL) and floor a sized-but-sub-minimum
        #    positive quantity up to minQty (never above the pre-cap).
        try:
            final_qty = await self._prepare_quantity(action, pre_cap=original_qty)
        except Exception as exc:
            self._log.warning(
                "order_rejected_by_quantity_normalization",
                action_type=action.type,
                contract=action.contract or action.symbol,
                error=str(exc),
            )
            self._stats["total_rejected"] += 1
            return self._rejected_result(action, reason=str(exc))
        action = replace(action, quantity=final_qty)

        # 4. Build the order request from the final (sized + normalized) action
        order_request = self._build_request(action)

        # 5. Submit
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
                order_type=order_request.order_type,
                quantity=order_request.quantity,
                price=order_request.price,
                status="REJECTED",
                fills=[],
            )

        # 6. Submit bracket orders (TP/SL) after opening a position
        if action.type in ("open_long", "open_short", "ENTER") and (
            action.stop_loss_price is not None or action.take_profit_price is not None
        ):
            bracket_ids: dict[str, int] = {}
            # Closing side mirrors the entry: LONG -> SELL, SHORT -> BUY.
            close_side = (
                "SELL" if str(action.side or "").upper() in ("BUY", "LONG") else "BUY"
            )
            if action.stop_loss_price is not None:
                try:
                    sl_action = Action(
                        type="set_stop_loss",
                        strategy=action.strategy,
                        symbol=action.contract or action.symbol,
                        side=close_side,
                        quantity=action.quantity,
                        stop_loss_price=action.stop_loss_price,
                        risk_checked=True,
                    )
                    sl_request = self._build_request(sl_action)
                    sl_result = await self._gateway.submit(sl_request)
                    bracket_ids["stop_loss"] = sl_result.order_id
                except (OrderRejectedError, OrderTimeoutError) as exc:
                    self._log.warning(
                        "bracket_stop_loss_failed",
                        error=str(exc),
                        parent_order_id=result.order_id,
                    )
            if action.take_profit_price is not None:
                try:
                    tp_action = Action(
                        type="set_take_profit",
                        strategy=action.strategy,
                        symbol=action.contract or action.symbol,
                        side=close_side,
                        quantity=action.quantity,
                        take_profit_price=action.take_profit_price,
                        risk_checked=True,
                    )
                    tp_request = self._build_request(tp_action)
                    tp_result = await self._gateway.submit(tp_request)
                    bracket_ids["take_profit"] = tp_result.order_id
                except (OrderRejectedError, OrderTimeoutError) as exc:
                    self._log.warning(
                        "bracket_take_profit_failed",
                        error=str(exc),
                        parent_order_id=result.order_id,
                    )
            if bracket_ids:
                self._log.info(
                    "bracket_orders_placed",
                    bracket_ids=bracket_ids,
                    parent_order_id=result.order_id,
                )

        # 7. Update stats
        self._stats["total_submitted"] += 1
        if result.status == "FILLED":
            self._stats["total_filled"] += 1
            await self._persist_trade(action, result)
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

    async def _persist_trade(self, action: Action, result: OrderResult) -> None:
        """Persist an executed fill to the ``trades`` table (best effort).

        ENTER fills are recorded with ``pnl='0'``; EXIT fills carry the
        realized PnL computed from the entry price attached to the action
        metadata at the orchestrator and the exit fill price, so the daily
        PnL report can sum today's closed-trade PnL.
        """
        if self._db_manager is None or not getattr(
            self._db_manager, "is_connected", False
        ):
            return
        try:
            from quad.persistence.models import TradeModel
            from quad.persistence.repositories import TradeRepository

            fill_price = Decimal(0)
            fills = getattr(result, "fills", None) or []
            if fills:
                try:
                    fill_price = Decimal(str(fills[-1].get("price", "0")))
                except (TypeError, ValueError):
                    fill_price = Decimal(0)
            if not fill_price and result.price:
                fill_price = result.price

            realized_pnl = Decimal(0)
            entry_str = (action.metadata or {}).get("entry_price")
            pos_side = (action.metadata or {}).get("position_side")
            if action.type == "EXIT" and entry_str and pos_side:
                try:
                    entry = Decimal(str(entry_str))
                    is_long = str(pos_side).strip().upper() in (
                        "BUY",
                        "LONG",
                    )
                    diff = fill_price - entry
                    if not is_long:
                        diff = -diff
                    realized_pnl = diff * action.quantity
                except Exception:
                    realized_pnl = Decimal(0)

            repo = TradeRepository(self._db_manager)
            await repo.create(
                TradeModel(
                    id=0,
                    position_id=0,
                    order_id=result.order_id,
                    symbol=result.symbol or action.contract or "",
                    side=str(action.side or result.side or ""),
                    quantity=str(result.quantity or action.quantity),
                    price=str(fill_price),
                    fee="0",
                    pnl=str(realized_pnl),
                    timestamp=int(time.time() * 1000),
                )
            )
        except Exception as exc:
            self._log.warning("trade_persist_failed", error=str(exc))

    async def execute_twap(
        self,
        action: Action,
        context: StrategyContext,
        window: int | None = None,
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
        # 0. Hard dry-run guard.
        if self._is_dry_run and not getattr(
            self._exchange_adapter, "is_testnet", False
        ):
            self._log.critical(
                "dry_run_guard_blocked_twap",
                action_type=action.type,
                contract=action.contract or action.symbol,
                side=action.side,
                qty=str(action.quantity),
            )
            self._stats["total_rejected"] += 1
            return [self._rejected_result(action)]

        if window is None:
            window = self._config.get("execution", {}).get("twap_window_seconds", 300)

        context = self._ensure_context(context)
        original_qty = action.quantity

        # 1. Risk check (skip if already checked upstream)
        if action.risk_checked:
            risk_result = RiskResult(passed=True)
        else:
            risk_result = await self._risk_manager.evaluate(action, context)

        # 2. Apply the risk-sized action (Fix #4).
        sized = risk_result.details.get("action") if risk_result.passed else None
        if sized is not None:
            action = sized

        if not risk_result.passed:
            self._log.warning(
                "twap_rejected_by_risk",
                action_type=action.type,
                contract=action.contract,
                reason=risk_result.reason,
            )
            self._stats["total_rejected"] += 1
            return [self._rejected_result(action, reason=risk_result.reason)]

        # 3. Prepare the final quantity (normalize + floor-up to minQty).
        try:
            final_qty = await self._prepare_quantity(action, pre_cap=original_qty)
        except Exception as exc:
            self._log.warning(
                "twap_rejected_by_quantity_normalization",
                action_type=action.type,
                contract=action.contract or action.symbol,
                error=str(exc),
            )
            self._stats["total_rejected"] += 1
            return [self._rejected_result(action, reason=str(exc))]
        action = replace(action, quantity=final_qty)

        order_request = self._build_request(action)

        # 4. Execute TWAP
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
                    order_type=order_request.order_type,
                    quantity=order_request.quantity,
                    price=order_request.price,
                    status="REJECTED",
                    fills=[],
                )
            ]

        # 5. Update stats
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

        # Ingest exchange fills so closed (SELL) legs appear in the journal
        # and daily PnL reflects reality (see _ingest_exchange_trades).
        ingested = await self._ingest_exchange_trades()

        self._log.info(
            "reconciliation_complete",
            active_orders=len(active),
            discrepancies=len(discrepancies),
            trades_ingested=ingested,
        )
        return {
            "active_orders_checked": len(active),
            "discrepancies_found": len(discrepancies),
            "discrepancies": discrepancies,
            "trades_ingested": ingested,
        }

    async def _ingest_exchange_trades(self) -> int:
        """Ingest exchange fills into the ``trades`` journal (both legs).

        The bot only persisted *opening* fills before this method existed,
        so SELL/exit legs (TP/SL brackets, exchange-triggered closes) never
        appeared and the daily-PnL number computed from the journal was
        meaningless (all zeros).  This pulls ``GET /v5/execution/list`` and
        upserts every fill that is not already in the journal, pairing BUY
        and SELL fills per symbol (FIFO) to assign a signed realized PnL to
        each close (SELL) leg:

        * LONG round-trip (BUY then SELL):  ``pnl = (sell - buy) * qty``
        * SHORT round-trip (SELL then BUY): ``pnl = (buy - sell) * qty``

        One-way mode (the bot default) means a SELL is always the close of a
        prior BUY for the same symbol, so this pairing is sound.  In hedge
        mode the same pairing still produces a correct net per symbol; we do
        not attempt to attribute per-position-side PnL here (the exchange
        ``realizedProfit`` field is account/symbol aggregate, not per fill).

        Returns the number of new trade rows inserted.
        """
        if self._db_manager is None or not getattr(
            self._db_manager, "is_connected", False
        ):
            return 0
        try:
            from quad.persistence.models import TradeModel
            from quad.persistence.repositories import TradeRepository

            try:
                fills = await self._exchange_adapter.get_user_trades()
            except Exception as exc:  # noqa: BLE001 best-effort ingest
                self._log.warning("exchange_trades_fetch_failed", error=str(exc))
                return 0
            if not fills:
                return 0

            repo = TradeRepository(self._db_manager)
            # Group by symbol, keep insertion order (oldest first).
            by_symbol: dict[str, list] = {}
            for f in fills:
                by_symbol.setdefault(getattr(f, "symbol", ""), []).append(f)

            inserted = 0
            for symbol, legs in by_symbol.items():
                legs.sort(key=lambda t: getattr(t, "timestamp", 0) or 0)
                # FIFO queues of open entry prices, per direction.  A SELL
                # closes a prior BUY (LONG); a BUY closes a prior SELL (SHORT).
                open_long_entries: list[Decimal] = []  # from BUY opens
                open_short_entries: list[Decimal] = []  # from SELL opens
                for leg in legs:
                    side = str(getattr(leg, "side", "") or "").upper()
                    order_id = int(getattr(leg, "order_id", 0) or 0)
                    if await repo.exists_for_order(order_id, side):
                        # Already persisted (e.g. the opening fill the engine
                        # wrote directly).  Still advance the pairing queue so
                        # a later close leg balances it on re-ingest.
                        if side == "BUY":
                            open_long_entries.append(_as_dec(getattr(leg, "price", 0)))
                        elif side == "SELL":
                            open_short_entries.append(
                                _as_dec(getattr(leg, "price", 0))
                            )
                        continue

                    price = _as_dec(getattr(leg, "price", 0))
                    qty = _as_dec(getattr(leg, "quantity", 0))
                    # Prefer the exchange-reported realized PnL when available
                    # (OKX fills include realizedPnl).  The
                    # FIFO computation below is only a fallback for when the
                    # exchange does not provide per-fill realized PnL.
                    exchange_pnl = _as_dec(getattr(leg, "pnl", 0))
                    pnl = Decimal(0)
                    if side == "BUY":
                        # Either an opening LONG (queue it) or the close of a
                        # prior SHORT (compute PnL).
                        if open_short_entries:
                            entry = open_short_entries.pop(0)
                            # SHORT close: profit when bought back below entry.
                            fifo_pnl = (entry - price) * qty
                            pnl = exchange_pnl if exchange_pnl else fifo_pnl
                        open_long_entries.append(price)
                    elif side == "SELL":
                        # Either an opening SHORT (queue it) or the close of a
                        # prior LONG (compute PnL).
                        if open_long_entries:
                            entry = open_long_entries.pop(0)
                            # LONG close: profit when sold above entry.
                            fifo_pnl = (price - entry) * qty
                            pnl = exchange_pnl if exchange_pnl else fifo_pnl
                        open_short_entries.append(price)

                    await repo.create(
                        TradeModel(
                            id=0,
                            position_id=0,
                            order_id=order_id,
                            symbol=symbol,
                            side=side,
                            quantity=str(qty),
                            price=str(price),
                            fee=str(_as_dec(getattr(leg, "fee", 0))),
                            pnl=str(pnl),
                            timestamp=int(getattr(leg, "timestamp", 0) or 0),
                        )
                    )
                    inserted += 1
            if inserted:
                self._log.info("exchange_trades_ingested", count=inserted)
            return inserted
        except Exception as exc:  # noqa: BLE001 best-effort ingest
            self._log.warning("exchange_trades_ingest_failed", error=str(exc))
            return 0

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

    @property
    def _is_dry_run(self) -> bool:
        """Whether dry-run mode is enabled (top-level ``_dry_run`` config key)."""
        val = self._config.get("_dry_run", False)
        if isinstance(val, str):
            return val.lower() in ("1", "true", "yes")
        return bool(val)

    def _ensure_context(self, context: Any) -> StrategyContext:
        """Normalize a risk-evaluation context to a ``StrategyContext``.

        Several callers (TradingView webhook, close-all-positions) pass a
        bare dict ``{}`` because they have no live strategy context.  The
        risk gates dereference ``context.futures_positions``,
        ``context.account``, etc., so a plain dict would ``AttributeError``.
        """
        if isinstance(context, StrategyContext):
            return context
        if isinstance(context, dict):
            return StrategyContext(config=context)
        return StrategyContext(config=self._config)

    def _rejected_result(
        self, action: Action, reason: str = "", gate: str = ""
    ) -> OrderResult:
        """Build a REJECTED ``OrderResult`` for a refused action."""
        return OrderResult(
            order_id=0,
            client_order_id="",
            symbol=action.contract or action.symbol,
            side=action.side or "",
            order_type=action.order_type or "MARKET",
            quantity=action.quantity,
            price=action.price,
            status="REJECTED",
            fills=[],
        )

    async def _prepare_quantity(
        self, action: Action, pre_cap: Decimal | None = None
    ) -> Decimal:
        """Normalize a final order quantity to the exchange filters.

        * Rejects a zero/negative quantity (risk sizing zeroed a valid trade).
        * Rounds DOWN to the exchange ``stepSize`` (never up).
        * If the (sized) quantity fell below ``minQty`` but a valid pre-cap
          exists (the pre-sizing quantity) that is at or above ``minQty``,
          floors the quantity UP to ``minQty`` so the trade isn't lost —
          while never exceeding the pre-cap.
        * Otherwise raises ``RuntimeError`` (below minQty / minNotional) so
          the caller can return a clean REJECTED result instead of sending an
          order the exchange would reject (-1113 / -1111 / -4164).
        """
        qty = action.quantity
        if qty is None or qty <= Decimal(0):
            raise RuntimeError(
                f"order quantity is zero/negative after sizing ({qty}); refusing"
            )

        # The pre-cap (original requested quantity before risk sizing) may be
        # passed explicitly (engine-evaluated paths) or recorded upstream by
        # the orchestrator on a pre-sized action (AI path).
        if pre_cap is None or pre_cap <= Decimal(0):
            raw_pre_cap = action.metadata.get("pre_size_quantity")
            if raw_pre_cap is not None:
                try:
                    pre_cap = Decimal(str(raw_pre_cap))
                except Exception:
                    pre_cap = None

        symbol = action.contract or action.symbol
        try:
            return await self._exchange_adapter.normalize_quantity(symbol, qty)
        except Exception:
            # Floor a sub-minQty / sub-minNotional sized quantity UP to the
            # smallest size that clears BOTH the minQty and minNotional
            # filters.  (The previous code only floored to minQty, which could
            # make a sub-minNotional order even smaller and still get bounced
            # with -4164.)  Never exceed the pre-cap (the original requested
            # quantity); otherwise re-raise the original, already-clear
            # rejection so the caller returns a clean REJECTED result.
            if pre_cap is not None and pre_cap > Decimal(0):
                try:
                    filters = await self._exchange_adapter.get_symbol_filters(symbol)
                    min_qty = filters.get("min_qty", Decimal(0))
                    min_notional = filters.get("min_notional", Decimal(0))
                    step_size = filters.get("step_size", Decimal(0))
                except Exception:
                    min_qty = Decimal(0)
                    min_notional = Decimal(0)
                    step_size = Decimal(0)
                try:
                    mark_price = await self._exchange_adapter.get_mark_price(symbol)
                except Exception:
                    mark_price = None
                target = _floor_to_compliant(
                    qty,
                    min_qty,
                    min_notional,
                    step_size,
                    mark_price,
                )
                if target <= pre_cap:
                    self._log.info(
                        "order_quantity_floored_to_min_qty",
                        symbol=symbol,
                        original=str(qty),
                        floored=str(target),
                        min_qty=str(min_qty),
                        min_notional=str(min_notional),
                        pre_cap=str(pre_cap),
                    )
                    return target
            raise

    async def _reconciliation_loop(self) -> None:
        """Background loop that periodically reconciles order state."""
        interval = self._config.get("execution", {}).get(
            "reconcile_interval_seconds", 60
        )

        while not self._stop_event.is_set():
            try:
                await self._gateway.refresh_state()
                active = self._gateway.get_active_orders()
                if active:
                    await self._reconciler.reconcile_pending_orders(active)
                self._stats["reconciliations_run"] += 1
                self._stats["active_order_count"] = len(active)
                # Ingest exchange fills so closed (SELL) legs reach the
                # journal and daily PnL becomes meaningful.
                await self._ingest_exchange_trades()
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
        exec_cfg = self._config.get("execution", {})
        order_request = OrderRequest(
            symbol=action.contract or "",
            side=action.side or "",
            order_type=action.order_type
            or exec_cfg.get("default_order_type", "MARKET"),
            quantity=action.quantity,
            price=action.price,
            # Serial-close EXITs (metadata["serial_close"]) must be flagged
            # reduceOnly so they can never open a position; e.g. closing a
            # BTC LONG at 0.3 qty must not open a SELL short when the local
            # position book is stale, and a fresh position must only open on
            # a confirmed-flat account.
            reduce_only=bool(action.metadata.get("serial_close"))
            or exec_cfg.get("reduce_only", False),
            post_only=exec_cfg.get("post_only", False),
        )

        # Market-only enforcement: no limit orders anywhere in the trade path.
        # Only the TP/SL bracket types below may override this.
        if order_request.order_type not in ("STOP_MARKET", "TAKE_PROFIT_MARKET"):
            order_request.order_type = "MARKET"

        # Handle TP/SL action types.  STOP_MARKET / TAKE_PROFIT_MARKET
        # are market-on-trigger conditional orders that require only a
        # stopPrice (no limit price), keeping the whole trade path
        # market-execution only.  The legacy STOP_LOSS / TAKE_PROFIT types are
        # limit-if-triggered and would be rejected because they mandate a
        # `price` that the bot never supplies.
        if action.type == "set_stop_loss":
            order_request.order_type = "STOP_MARKET"
            order_request.stop_price = action.stop_loss_price
            order_request.reduce_only = True
            order_request.working_type = "MARK_PRICE"
            order_request.price_protect = True
        elif action.type == "set_take_profit":
            order_request.order_type = "TAKE_PROFIT_MARKET"
            order_request.stop_price = action.take_profit_price
            order_request.reduce_only = True
            # Bracket prices are computed from the mark price, so trigger
            # consistently on MARK_PRICE (the API default is CONTRACT_PRICE)
            # and protect the trigger against manipulation.
            order_request.working_type = "MARK_PRICE"
            order_request.price_protect = True

        # timeInForce is only valid for LIMIT-family orders.  MARKET,
        # STOP_MARKET and TAKE_PROFIT_MARKET reject a sent TIF with the exchange
        # error -1114 (TIF_NOT_REQUIRED), so clear the GTC default.
        if order_request.order_type in ("MARKET", "STOP_MARKET", "TAKE_PROFIT_MARKET"):
            order_request.time_in_force = ""

        return order_request
