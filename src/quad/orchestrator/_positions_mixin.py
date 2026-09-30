"""Position lifecycle for the orchestrator: closing, bracketing, AI actions.

Extracted verbatim from ``orchestrator.py`` -- all 7 methods' ASTs are
unchanged; only the enclosing class moved.  ``QuadOrchestrator`` keeps each
method with its original signature.

These belong together because they are one serialised workflow guarded by
``self._trade_lock``: every position-closing or position-opening action the
orchestrator can take lives here, so the lock's scope is visible in one file
rather than spread across the class.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from quad.types.strategy import StrategyContext


class PositionManagementMixin:
    """Close, bracket, and act on positions under ``self._trade_lock``.

    The attributes below are the state this mixin *consumes*.  They are
    declared rather than assigned, so the mixin documents the contract it
    expects from its host class without owning it.  ``_notify_trade``,
    ``_format_pnl`` and ``_compute_position_pnl`` come from ``NotifyMixin``,
    and ``_position_side_for_symbol`` from the AI-rotation mixin.
    """

    _log: Any
    _config_dict: Any
    _db_manager: Any
    _exchange_adapter: Any
    _execution_engine: Any
    _risk_manager: Any
    _trade_lock: Any
    # From sibling mixins.
    _notify_trade: Any
    _format_pnl: Any
    _compute_position_pnl: Any
    _position_side_for_symbol: Any

    async def _price_bracket_violation(
        self,
        symbol: str,
        position_side: Any,
        open_orders: Any,
    ) -> tuple[bool, str]:
        """Check whether the live mark price is clearly beyond a bracket.

        Compares the current mark price against the STOP_MARKET (stop-loss)
        and TAKE_PROFIT_MARKET triggers among *open_orders* for ``symbol``.
        A LONG position is violated when mark <= SL - tolerance or
        mark >= TP + tolerance; a SHORT position is the mirror image.

        Returns
        -------
        tuple[bool, str]
            ``(True, "sl" | "tp")`` when the price is clearly beyond a
            trigger but the bracket has not fired; ``(False, "")`` otherwise.
        """
        rotation_cfg = self._config_dict.get("ai", {}).get("rotation", {})
        if not rotation_cfg.get("price_bracket_check", True):
            return False, ""
        tolerance_pct = float(rotation_cfg.get("price_bracket_tolerance_pct", 0.5))
        tolerance = Decimal(str(tolerance_pct)) / Decimal(100)

        sl_trigger: Decimal | None = None
        tp_trigger: Decimal | None = None
        for order in open_orders or []:
            if getattr(order, "symbol", "") != symbol:
                continue
            otype = str(getattr(order, "order_type", "")).upper()
            stop_price = getattr(order, "stop_price", None)
            if stop_price is None:
                continue
            if otype in ("STOP_MARKET", "STOP_LOSS", "STOP"):
                sl_trigger = Decimal(str(stop_price))
            elif otype in ("TAKE_PROFIT_MARKET", "TAKE_PROFIT"):
                tp_trigger = Decimal(str(stop_price))

        if sl_trigger is None and tp_trigger is None:
            return False, ""

        try:
            mark = await self._exchange_adapter.get_mark_price(symbol)
        except Exception as exc:
            self._log.warning(
                "price_bracket_check_mark_unavailable",
                symbol=symbol,
                error=str(exc),
            )
            return False, ""
        if mark is None or mark <= 0:
            return False, ""

        from quad.types.domain import PositionSide

        is_long = position_side == PositionSide.LONG
        if sl_trigger is not None:
            if is_long and mark <= sl_trigger * (1 - tolerance):
                return True, "sl"
            if not is_long and mark >= sl_trigger * (1 + tolerance):
                return True, "sl"
        if tp_trigger is not None:
            if is_long and mark >= tp_trigger * (1 + tolerance):
                return True, "tp"
            if not is_long and mark <= tp_trigger * (1 - tolerance):
                return True, "tp"
        return False, ""

    async def _close_orphan_positions_on_start(self) -> None:
        """Flatten positions left open by a previous run at startup.

        Gated by ``ai.rotation.enabled`` and
        ``ai.rotation.close_positions_on_start`` (both default on for the
        rotation mode).  When enabled and open positions exist, cancels the
        old TP/SL brackets and market-closes the positions so the first
        rotation cycle scans flat and can open a fresh trade instead of
        holding a stale one for hours.
        """
        ai_cfg = self._config_dict.get("ai", {})
        rotation_cfg = ai_cfg.get("rotation", {})
        if not rotation_cfg.get("enabled", False):
            self._log.debug("startup_rotation_disabled_skip_flatten")
            return
        if not rotation_cfg.get("close_positions_on_start", True):
            self._log.debug("startup_flatten_disabled_by_config")
            return

        open_positions = await self._exchange_adapter.get_positions()
        from quad.types.domain import PositionStatus

        open_positions = [
            p
            for p in open_positions
            if getattr(p, "status", None) == PositionStatus.OPEN
        ]
        if not open_positions:
            self._log.info("startup_no_orphan_positions")
            return

        self._log.info(
            "startup_flattening_previous_positions",
            count=len(open_positions),
            symbols=[getattr(p, "symbol", "") for p in open_positions],
        )
        closed = await self._close_all_positions()
        self._log.info(
            "startup_positions_flattened"
            if closed
            else "startup_positions_flatten_incomplete",
            requested=len(open_positions),
        )

    async def _close_all_positions(self) -> bool:
        """Close all open positions using MARKET EXIT orders.

        Called when ``serial_trade_mode`` is enabled and a new ENTER
        action is about to be executed.  The method:

        1. Fetches all open positions from the exchange adapter.
        2. Cancels any open orders on those positions.
        3. Builds an EXIT ``Action`` for each position with MARKET order type.
        4. Submits each EXIT through the execution engine.
        5. Logs the results with structlog.

        Returns
        -------
        bool
            ``True`` if all positions were closed successfully,
            ``False`` if any position failed to close or no positions
            were found.
        """
        log = self._log.bind()
        if self._execution_engine is None:
            log.warning("close_all_positions_no_execution_engine")
            return False

        # 1. Get all open positions
        try:
            positions = await self._exchange_adapter.get_positions()
        except Exception as exc:
            log.exception("close_all_positions_fetch_error", error=str(exc))
            return False

        # Filter to only OPEN positions
        from quad.types.domain import PositionStatus

        open_positions = [
            p for p in positions if getattr(p, "status", None) == PositionStatus.OPEN
        ]

        if not open_positions:
            log.debug("close_all_positions_no_open_positions")
            return True  # Nothing to close — success by definition

        log.debug(
            "close_all_positions_started",
            count=len(open_positions),
        )

        # 2. Cancel all open orders
        try:
            open_orders = await self._exchange_adapter.get_open_orders()
            for order in open_orders:
                try:
                    await self._exchange_adapter.cancel_order(
                        order.id, getattr(order, "symbol", "")
                    )
                    log.info(
                        "close_all_positions_order_cancelled",
                        order_id=order.id,
                        symbol=getattr(order, "symbol", "unknown"),
                    )
                except Exception as exc:
                    log.warning(
                        "close_all_positions_cancel_order_error",
                        order_id=getattr(order, "id", "unknown"),
                        error=str(exc),
                    )
        except Exception as exc:
            log.warning(
                "close_all_positions_fetch_orders_error",
                error=str(exc),
            )
            # Continue — non-critical

        # 3. Build and execute EXIT actions
        from quad.types.domain import OrderResult
        from quad.types.risk import Action

        close_tasks: list[tuple[Any, Action, asyncio.Task[OrderResult]]] = []
        for position in open_positions:
            # Determine close side: LONG -> SELL, SHORT -> BUY
            pos_side = getattr(position, "side", None)
            if pos_side is None:
                log.warning(
                    "close_all_positions_unknown_side",
                    contract=getattr(
                        position, "symbol", getattr(position, "contract_symbol", "")
                    ),
                )
                continue

            from quad.types.domain import PositionSide as PS

            close_side = "SELL" if pos_side == PS.LONG else "BUY"

            action = Action(
                type="EXIT",
                strategy="serial_close",
                contract=getattr(
                    position, "symbol", getattr(position, "contract_symbol", "")
                ),
                side=close_side,
                # Preserve the exact fractional quantity.  int() would
                # truncate fractional quantities to 0, silently zeroing the
                # order (and the engine now rejects zero/negative quantities).
                quantity=Decimal(str(getattr(position, "quantity", 0))),
                order_type="MARKET",
                price=None,
                reason="Serial trade mode: closing position before new ENTER",
                metadata={
                    "serial_close": True,
                    # Entry price at close time so the engine can persist the
                    # realized PnL for this trade.
                    "entry_price": str(getattr(position, "entry_price", 0) or 0),
                    # Position side (LONG/SHORT), NOT the closing trade side:
                    # the PnL formula must know the held direction.
                    "position_side": str(getattr(position, "side", "") or ""),
                },
            )

            # 4. Execute through execution engine
            # Track the position alongside the task so per-position outcomes
            # (and realized PnL, when derivable) can be reported accurately.
            task = asyncio.create_task(
                self._execution_engine.execute(
                    action, StrategyContext(config=self._config_dict)
                )
            )
            close_tasks.append((position, action, task))

        # 5. Wait for all close orders to complete
        results: list[tuple[Any, Action, Any | Exception | None]] = []
        for position, action, task in close_tasks:
            outcome = await task
            results.append((position, action, outcome))

        success_count = 0
        fail_count = 0
        closed_details: list[dict[str, Any]] = []
        for position, action, result in results:
            symbol = getattr(position, "symbol", "") or getattr(
                position, "contract_symbol", ""
            )
            if isinstance(result, Exception):
                fail_count += 1
                log.exception(
                    "close_all_positions_execution_error",
                    symbol=symbol,
                    error=str(result),
                )
                continue
            if result is None or result.status not in (
                "FILLED",
                "NEW",
                "PARTIALLY_FILLED",
            ):
                fail_count += 1
                log.warning(
                    "close_all_positions_not_confirmed",
                    symbol=symbol,
                    status=getattr(result, "status", "unknown"),
                )
                continue
            success_count += 1
            closed_details.append(
                {
                    "symbol": symbol,
                    "side": str(getattr(position, "side", "")),
                    "quantity": str(getattr(position, "quantity", "")),
                    "status": getattr(result, "status", "unknown"),
                }
            )
            log.info(
                "close_all_positions_closed",
                symbol=symbol,
                status=getattr(result, "status", "unknown"),
            )

        # 6. Verify flat on the exchange: a "successful" submit is not proof
        #    the position is gone (e.g. -1102 queries left it unseen, or the
        #    close was accepted but a bracket re-opened it).  Only report
        #    success once no OPEN position remains.
        flat = False
        try:
            remaining = await self._exchange_adapter.get_positions()
            from quad.types.domain import PositionStatus as _PS

            still_open = [
                p for p in remaining if getattr(p, "status", None) == _PS.OPEN
            ]
            flat = len(still_open) == 0
            if still_open:
                log.warning(
                    "close_all_positions_remaining",
                    symbols=[
                        getattr(p, "symbol", "") or getattr(p, "contract_symbol", "")
                        for p in still_open
                    ],
                )
        except Exception as exc:
            log.warning("close_all_positions_verify_failed", error=str(exc))

        log.info(
            "close_all_positions_complete",
            total=len(open_positions),
            closed=success_count,
            failed=fail_count,
            flat_verified=flat,
        )

        return flat and fail_count == 0

    async def _execute_ai_action(
        self,
        decision: dict[str, Any],
        context: StrategyContext,
    ) -> bool:
        """Serialize trade execution; delegates to :meth:`_execute_ai_action_locked`."""
        lock = getattr(self, "_trade_lock", None)
        if lock is None:  # instances built without __init__ (tests)
            import asyncio as _asyncio

            self._trade_lock = lock = _asyncio.Lock()
        async with lock:
            return await self._execute_ai_action_locked(decision, context)

    async def _execute_ai_action_locked(
        self,
        decision: dict[str, Any],
        context: StrategyContext,
    ) -> bool:
        """Execute an AI-generated trading action through risk and execution.

        Parameters
        ----------
        decision:
            The parsed trading decision dict from the LLM.
        context:
            The current strategy context for risk evaluation.

        Returns
        -------
        bool
            ``True`` after the order is submitted successfully; ``False``
            on HOLD, incomplete decision, risk rejection/error, or
            execution exception.  Pair-rotation uses this to distinguish
            "ENTER opened a position" from "ENTER was rejected".
        """
        if self._risk_manager is None or self._execution_engine is None:
            self._log.warning("ai_execution_subsystems_missing")
            return False

        action_type = decision.get("action", "HOLD")
        if action_type == "HOLD":
            self._log.debug("ai_decision_hold", reason=decision.get("reasoning", ""))
            return False

        strategy_name = decision.get("strategy") or "ai_default"
        contract_symbol = decision.get("contract")
        quantity = decision.get("quantity")
        # All entries/exits are MARKET — never let the AI pick a limit order.
        order_type = "MARKET"
        limit_price = None  # market orders carry no limit price

        if not contract_symbol or not quantity:
            self._log.warning(
                "ai_decision_incomplete",
                contract=contract_symbol,
                quantity=quantity,
            )
            return False

        # ----------------------------------------------------------------
        # Phase 1 mandatory backstop: re-derive the order side deterministically
        # for ENTER/EXIT.  NEVER fall through to Action.__post_init__ defaults,
        # which would silently invert (ENTER->BUY, EXIT->SELL) and re-introduce
        # the exact long/short inversion bug this upgrade eliminates.
        # ----------------------------------------------------------------
        side = decision.get("side")
        if action_type in ("ENTER", "EXIT"):
            from quad.ai.validator import canonical_direction, derive_side

            direction = canonical_direction(decision.get("direction"))
            position_side = self._position_side_for_symbol(context, contract_symbol)
            derived_side = derive_side(action_type, direction, position_side)
            if not derived_side:
                self._log.warning(
                    "ai_side_un_derivable",
                    action=action_type,
                    direction=direction,
                    contract=contract_symbol,
                    position_side=getattr(position_side, "name", position_side),
                )
                return False
            if side not in (None, "") and str(side).strip().upper() != derived_side:
                self._log.warning(
                    "ai_side_derived_overrides",
                    action=action_type,
                    ai_side=side,
                    derived_side=derived_side,
                    contract=contract_symbol,
                )
            side = derived_side

        if not side:
            self._log.warning(
                "ai_decision_incomplete",
                contract=contract_symbol,
                side=side,
                quantity=quantity,
            )
            return False

        # One-trade-per-cycle: before ANY new ENTER, every existing position
        # must be closed and the account confirmed flat.  This is the hard
        # invariant for the user's "one trade in, one trade out" rule -- it
        # must not rely on the cycle-start force-close alone, because a close
        # can fail (or a stale local position list can hide a live position).
        if action_type == "ENTER":
            if self._config_dict.get("trading", {}).get("serial_trade_mode", True):
                closed = await self._close_all_positions()
                if not closed:
                    self._log.warning(
                        "ai_enter_blocked_positions_not_flat",
                        contract=contract_symbol,
                    )
                    return False
            else:
                # Even with serial mode disabled, never stack a second
                # position: if a position is open and we cannot confirm it is
                # closed, refuse the ENTER.
                try:
                    live = await self._exchange_adapter.get_positions()
                    from quad.types.domain import PositionStatus as _PS

                    open_now = [
                        p for p in live if getattr(p, "status", None) == _PS.OPEN
                    ]
                except Exception:
                    open_now = []
                if open_now:
                    self._log.warning(
                        "ai_enter_blocked_positions_open",
                        contract=contract_symbol,
                        open_symbols=[
                            getattr(p, "symbol", "")
                            or getattr(p, "contract_symbol", "")
                            for p in open_now
                        ],
                    )
                    return False

        self._log.debug(
            "ai_executing_action",
            action=action_type,
            contract=contract_symbol,
            side=side,
        )

        # Attach per-position TP/SL bracket prices on ENTER so the execution
        # engine places STOP_LOSS + TAKE_PROFIT orders alongside the market
        # entry.  The position is then closed ONLY by those brackets.
        stop_loss_price: Decimal | None = None
        take_profit_price: Decimal | None = None
        if action_type == "ENTER":
            # Scalp decisions carry tight per-trade bands so both-mode can
            # hold trend + scalp profiles on one worker safely.
            scalp_cfg = self._config_dict.get("ai", {}).get("scalp", {}) or {}
            if strategy_name == "scalp":
                sl_ov = float(scalp_cfg.get("sl_pct", 8.0))
                tp_ov = float(scalp_cfg.get("tp_pct", 15.0))
            else:
                sl_ov, tp_ov = None, None
            stop_loss_price, take_profit_price = await self._compute_bracket_prices(
                contract_symbol,
                side,
                sl_pct_override=sl_ov,
                tp_pct_override=tp_ov,
            )
            # A trade must never open without its SL/TP when the feature is
            # enabled.  Prices are (None, None) only when every bracket is
            # disabled or when the mark price could not be fetched -- in the
            # latter case refuse the ENTER instead of opening a bare position.
            risk_cfg = self._config_dict.get("risk", {})
            sl_enabled = bool(risk_cfg.get("per_position_sl", {}).get("enabled", True))
            tp_enabled = bool(risk_cfg.get("per_position_tp", {}).get("enabled", True))
            if sl_enabled or tp_enabled:
                if stop_loss_price is None or take_profit_price is None:
                    self._log.warning(
                        "ai_enter_blocked_missing_brackets",
                        contract=contract_symbol,
                        stop_loss=str(stop_loss_price),
                        take_profit=str(take_profit_price),
                    )
                    return False

        # Build Action dataclass
        from quad.types.risk import Action

        action = Action(
            type=action_type,
            strategy=strategy_name,
            symbol=contract_symbol,
            contract=contract_symbol,
            side=side,
            # Preserve the exact AI quantity (e.g. 0.005).  int() would
            # truncate fractional quantities to 0, silently zeroing the order.
            quantity=Decimal(str(quantity)),
            order_type=order_type,
            price=(Decimal(str(limit_price)) if limit_price is not None else None),
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
            reason=decision.get("reasoning", "AI trading decision"),
            # fallback matches AiConfig.default_confidence schema default
            metadata={
                "ai_confidence": decision.get(
                    "confidence",
                    self._config_dict.get("ai", {}).get("default_confidence", 0.8),
                ),
            },
        )
        # Attach the position entry price on EXIT so the execution engine can
        # persist the realized PnL for this closing trade.
        if action_type == "EXIT":
            try:
                ctx_positions = getattr(context, "positions", None) or []
                held_ctx = next(
                    (
                        p
                        for p in ctx_positions
                        if (
                            getattr(p, "symbol", "")
                            or getattr(p, "contract_symbol", "")
                        )
                        == contract_symbol
                    ),
                    None,
                )
                action.metadata["entry_price"] = str(
                    getattr(held_ctx, "entry_price", 0) or 0
                )
            except Exception:
                action.metadata["entry_price"] = "0"

        # Risk check
        try:
            result = await self._risk_manager.evaluate(action, context)
            if not result.passed:
                self._log.warning(
                    "ai_action_rejected_by_risk",
                    action=action_type,
                    contract=contract_symbol,
                    reason=result.reason,
                    gate=result.gate,
                )
                return False
            # Use the risk-sized action (Fix #4): RiskManager.evaluate returns a
            # possibly-reduced quantity in details["action"].  Record the
            # pre-sizing quantity so the execution engine can floor a
            # sub-minQty sized quantity up to the exchange minimum without
            # exceeding the AI's original request (the "pre-cap").
            sized_action = result.details.get("action", action)
            sized_action.risk_checked = True
            sized_action.risk_result = result
            sized_action.metadata = {
                **(sized_action.metadata or {}),
                "pre_size_quantity": str(action.quantity),
            }
        except Exception as exc:
            self._log.exception("ai_risk_evaluation_error", error=str(exc))
            return False

        # Execute
        try:
            order_result = await self._execution_engine.execute(sized_action, context)
            self._log.info(
                "ai_order_executed",
                action=action_type,
                strategy=strategy_name,
                contract=contract_symbol,
                side=side,
                status=getattr(order_result, "status", "unknown"),
            )

            # Inspect the exchange-side status before treating the action as
            # successful.  The execution engine returns ``REJECTED`` (via
            # ``_rejected_result``) when the dry-run guard trips, the risk gate
            # rejects, quantity normalization fails, or submission raises.
            # Returning ``True`` for a rejected order would make the rotation
            # loop advance as if a position opened — producing phantom trades,
            # skipped rotation cycles, and stale ``outcome='open'`` rows.
            # Mirror the confirmation logic in ``_close_all_positions``.
            order_status = getattr(order_result, "status", "unknown")
            if order_status not in ("FILLED", "NEW", "PARTIALLY_FILLED"):
                self._log.warning(
                    "ai_action_not_confirmed",
                    action=action_type,
                    contract=contract_symbol,
                    status=order_status,
                )
                return False

            # Mark the logged decision row as executed so the Phase-3 metrics
            # / prompt context (prompt.py counts ``executed``) reflects reality.
            # ``decision["db_id"]`` is stashed by _log_ai_decision; when the
            # DB path was skipped it is absent and we simply no-op.
            decision_id = decision.get("db_id")
            if decision_id is not None and self._db_manager is not None:
                try:
                    from quad.persistence.repositories import (
                        DecisionRepository,
                        make_repo,
                    )

                    # `update()` is a coroutine — the previous call was never
                    # awaited, so the `executed` flag was silently never
                    # persisted (and Python warned about an un-awaited
                    # coroutine on every successful AI action).
                    await make_repo(
                        DecisionRepository, self._db_manager, self._config_dict
                    ).update(decision_id, executed=1)
                except Exception:
                    self._log.warning(
                        "ai_decision_executed_flag_update_failed",
                        decision_id=decision_id,
                    )

            # Notify on successful execution (ENTER, EXIT, ADJUST, ROLL).
            # ENTER alerts always include the computed SL/TP brackets.
            if action_type in ("ENTER", "EXIT"):
                exit_pnl: str | None = None
                if action_type == "EXIT":
                    # Pass the entry price stashed on the EXIT action's
                    # metadata (captured from the live position at decision
                    # time) as a fallback for stale position books.
                    entry_hint = (
                        Decimal(
                            str(sized_action.metadata.get("entry_price", "0") or "0")
                        )
                        or None
                    )
                    exit_pnl = await self._build_exit_pnl_text(
                        contract_symbol,
                        side,
                        Decimal(str(sized_action.quantity or 0)),
                        order_result,
                        entry_price_hint=entry_hint,
                    )
                await self._notify_trade(
                    action_type=action_type,
                    strategy=strategy_name,
                    contract=contract_symbol,
                    side=side,
                    # Use the sized/final quantity.  int() would truncate
                    # fractional quantities (e.g. 0.005) to 0 in the
                    # notification.
                    quantity=str(sized_action.quantity),
                    price=str(action.price) if action.price else None,
                    reason=action.reason,
                    stop_loss=stop_loss_price,
                    take_profit=take_profit_price,
                    pnl=exit_pnl,
                )
            return True
        except Exception as exc:
            self._log.exception(
                "ai_order_execution_error",
                action=action_type,
                contract=contract_symbol,
                error=str(exc),
            )
            return False

    async def _build_exit_pnl_text(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        order_result: Any,
        entry_price_hint: Decimal | None = None,
    ) -> str | None:
        """Build the ``$x.xx (y%)`` PnL line for a closed position.

        Uses the exchange fill price when available, otherwise the live mark
        price, against the position's stored entry price.  Returns ``None``
        (no PnL line) when no entry price or exit price can be derived.

        ``entry_price_hint`` is the entry price captured on the closing
        action's metadata at decision time (see ``_execute_ai_action`` EXIT
        branch).  It is used as a fallback when the local position book is
        stale / the position has already been removed from the exchange's
        open-positions list at the moment of the EXIT — the common case where
        the bot closes a position and the exchange drops it before the PnL
        notification fires.
        """
        try:
            positions = await self._exchange_adapter.get_positions()
            held = next(
                (
                    p
                    for p in positions
                    if (getattr(p, "symbol", "") or getattr(p, "contract_symbol", ""))
                    == symbol
                ),
                None,
            )
            # Primary source: the live position's entry price.  Fallback:
            # the entry price stashed on the EXIT action's metadata (captured
            # before submission), so a stale/no position still yields PnL.
            raw_entry = getattr(held, "entry_price", 0) if held is not None else None
            if not raw_entry and entry_price_hint is not None:
                raw_entry = entry_price_hint
            entry_price = Decimal(str(raw_entry or 0))

            # PRIMARY: get realized PnL directly from the exchange for this
            # specific closing order via GET /v5/order/history.  This is the
            # exchange's own realized PnL — never a mark-price fallback or
            # FIFO recomputation.  Eliminates the stale-window race of
            # scanning /v5/execution/list (which returns up to 500 fills
            # across all time for a symbol).
            order_id = getattr(order_result, "order_id", 0) or 0
            if order_id:
                try:
                    exchange_pnl = await self._exchange_adapter.get_order_realized_pnl(
                        order_id, symbol
                    )
                    if exchange_pnl:
                        return self._format_pnl(exchange_pnl, entry_price)
                except Exception:
                    pass  # Fall through to computed PnL below

            # FALLBACK: compute PnL from fill/mark price and entry price.
            # Prefer the exchange fill price; fall back to the mark price.
            exit_price = Decimal(0)
            order_fills = getattr(order_result, "fills", None) or []
            if order_fills:
                try:
                    exit_price = Decimal(str(order_fills[-1].get("price", "0")))
                except (TypeError, ValueError):
                    exit_price = Decimal(0)
            if not exit_price:
                mark = await self._exchange_adapter.get_mark_price(symbol)
                exit_price = Decimal(str(mark))
            if not entry_price or not exit_price:
                return None
            pnl = self._compute_position_pnl(
                entry_price=entry_price,
                exit_price=exit_price,
                quantity=quantity,
                side=(str(getattr(held, "side", "")) or side),
            )
            return self._format_pnl(pnl, entry_price)
        except Exception as exc:
            self._log.warning(
                "ai_exit_pnl_compute_failed", symbol=symbol, error=str(exc)
            )
            return None

    async def _compute_bracket_prices(
        self,
        symbol: str,
        side: str,
        sl_pct_override: float | None = None,
        tp_pct_override: float | None = None,
    ) -> tuple[Decimal | None, Decimal | None]:
        """Compute per-position stop-loss / take-profit prices for an ENTER.

        Uses the same formula as ``StrategyBase._build_tp_sl_actions``: for a
        fixed SL/TP the price offset is ``capital_pct / 100 / leverage``
        applied to the current mark price (the market entry price).  Prices
        are ``None`` when the feature is disabled or the mark price is
        unavailable, in which case no bracket orders are placed.

        Parameters
        ----------
        symbol:
            Contract symbol being entered, e.g. ``"BTCUSDT"``.
        side:
            Entry side, ``"buy"``/``"sell"`` or ``"BUY"``/``"SELL"``.

        Returns
        -------
        tuple[Decimal | None, Decimal | None]
            ``(stop_loss_price, take_profit_price)``.
        """
        risk_cfg = self._config_dict.get("risk", {})
        sl_cfg = risk_cfg.get("per_position_sl", {})
        tp_cfg = risk_cfg.get("per_position_tp", {})
        if not sl_cfg.get("enabled", True) and not tp_cfg.get("enabled", True):
            return None, None

        try:
            mark = await self._exchange_adapter.get_mark_price(symbol)
            entry = Decimal(str(mark))
        except Exception as exc:
            self._log.warning(
                "ai_bracket_price_unavailable",
                symbol=symbol,
                error=str(exc),
            )
            return None, None
        if entry <= 0:
            self._log.warning(
                "ai_bracket_price_invalid",
                symbol=symbol,
                mark=str(entry),
            )
            return None, None

        leverage = Decimal(
            str(self._config_dict.get("trading", {}).get("leverage", 50))
        )
        sl_pct = Decimal(
            str(
                sl_pct_override
                if sl_pct_override is not None
                else sl_cfg.get("capital_pct", 30.0)
            )
        )
        tp_pct = Decimal(
            str(
                tp_pct_override
                if tp_pct_override is not None
                else tp_cfg.get("capital_pct", 50.0)
            )
        )
        is_long = side.strip().upper() in ("BUY", "LONG")

        sl_price: Decimal | None = None
        tp_price: Decimal | None = None
        if sl_cfg.get("enabled", True):
            offset = sl_pct / Decimal(100) / leverage
            sl_price = (
                entry * (Decimal(1) - offset)
                if is_long
                else entry * (Decimal(1) + offset)
            )
        if tp_cfg.get("enabled", True):
            offset = tp_pct / Decimal(100) / leverage
            tp_price = (
                entry * (Decimal(1) + offset)
                if is_long
                else entry * (Decimal(1) - offset)
            )
        return sl_price, tp_price
