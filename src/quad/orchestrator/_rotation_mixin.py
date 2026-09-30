"""The trading cycle and the AI-driven position rotation.

Extracted verbatim from ``orchestrator.py`` -- all 11 methods' ASTs are
unchanged; only the enclosing class moved.  ``QuadOrchestrator`` keeps each
method with its original signature, so ``run_forever()`` and the tests that
drive these paths are untouched.

These belong together because they are one control flow: ``_main_cycle_loop``
schedules ``_main_cycle``, which runs the AI trading cycle, the scalp rotation
and the trend rotation in rotation, each of which scans pairs, judges them
through the risk gates and executes an action.  Separating the loop from the
rotations it drives would hide that ordering.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Any

from quad.ai.context import MarketContext
from quad.monitoring.correlation import (
    new_correlation_id,
    reset_correlation_id,
    set_correlation_id,
)
from quad.types.strategy import StrategyContext


class RotationMixin:
    """The main cycle, the AI trading cycle, and the scalp/trend rotations.

    The attributes below are the state this mixin *consumes*.  They are
    declared rather than assigned, so the mixin documents the contract it
    expects from its host class without owning it.  The ``_*`` entries that
    look like methods come from sibling mixins (``NotifyMixin``,
    ``DecisionJournalMixin``, ``BootstrapMixin``, ``PositionManagementMixin``)
    or are defined here and called from the main class.
    """

    _log: Any
    _config_dict: Any
    _config_manager: Any
    _mode: Any
    _is_dry_run: Any
    _cycle_interval: Any
    _cycle_lock: Any
    _stop_event: Any
    _ai_enabled: Any
    _ai_cycle_count: Any
    _consecutive_ai_failures: Any
    _last_ai_cycle_time_ms: Any
    _last_ai_decision: Any
    _last_ai_error: Any
    _last_judge_candle_ts: Any
    _last_trend_roll_ts: Any
    _current_symbol: Any
    _judge_used: Any
    _rotation_index: Any
    _rotation_hold_since: Any
    _scalp_held: Any
    _active_strategies: Any
    _db_manager: Any
    _exchange_adapter: Any
    _market_data: Any
    _risk_manager: Any
    _groq_client: Any
    _metrics: Any
    _metrics_cycle_count: Any
    # From sibling mixins.
    _close_all_positions: Any
    _execute_ai_action: Any
    _price_bracket_violation: Any
    _compute_ai_metrics: Any
    _reconcile_decision_outcomes: Any
    _log_ai_decision: Any
    _notify_trade: Any
    _notify_circuit_breaker: Any
    _side_label: Any
    _format_pnl: Any
    _compute_position_pnl: Any

    async def _main_cycle(self) -> None:
        """Primary trading loop run as a background task.

        AI-Only Flow (24/7 forced trading mode):
        1. Force-close all open positions for a clean slate each cycle
        2. Collect full market context (candles, positions, account)
        3. Compute technical indicators from 150 fresh candles
        4. Build structured prompts for Groq LLM
        5. Call ``decide_trades()`` on the Groq client
        6. Parse the AI decision into an ``Action``
        7. Pass through risk manager
        8. Execute if risk checks pass (ENTER / EXIT)
        9. HOLD on AI failure — no deterministic fallback

        The cycle runs every ``ai_cycle_interval`` seconds (default 3600).
        Exactly one position at a time is enforced by the force-close step.
        """
        config_manager = self._config_manager
        if config_manager is None:
            self._log.warning("main_cycle_config_manager_missing")
            return
        if self._cycle_lock.locked():
            self._log.warning("main_cycle_already_running")
            return
        await self._cycle_lock.acquire()
        try:
            await self._main_cycle_loop()
        finally:
            self._cycle_lock.release()

    async def _main_cycle_loop(self) -> None:
        """Body of the primary trading loop (serialized by ``_cycle_lock``)."""
        config_manager = self._config_manager
        if config_manager is None:
            self._log.warning("main_cycle_config_manager_missing")
            return
        underlyings = list(config_manager.get("trading", {}).get("underlyings", []))

        while not self._stop_event.is_set():
            cycle_start = time.monotonic()
            self._metrics_cycle_count += 1

            # Bind a correlation id for this iteration so every log line it
            # emits (including nested pair scans and risk evaluation) can be
            # grouped, even while the TradingView webhook and Telegram jobs
            # write into the same stream.
            cycle_cid = new_correlation_id("cycle")
            cid_token = set_correlation_id(cycle_cid)
            self._log = self._log.bind(correlation_id=cycle_cid)

            try:
                # ----------------------------------------------------------
                # 1. Account state
                # ----------------------------------------------------------
                account = await self._exchange_adapter.get_account()
                positions = await self._exchange_adapter.get_positions()
                open_orders = []
                try:
                    open_orders = await self._exchange_adapter.get_open_orders()
                except Exception:  # noqa: S110  Non-critical; continue with empty orders
                    pass

                # ----------------------------------------------------------
                # 1b. Resolve AI decision outcomes against live positions.
                #     Positions close on the exchange (TP/SL brackets); an
                #     ENTER decision stays outcome='open' until its symbol no
                #     longer has an open position.
                # ----------------------------------------------------------
                try:
                    await self._reconcile_decision_outcomes(positions)
                except Exception as exc:
                    self._log.warning(
                        "decision_outcome_reconcile_failed", error=str(exc)
                    )

                # ----------------------------------------------------------
                # 1c. Phase 3 prediction-quality metrics.  Lightweight: one
                #     indexed SELECT + in-memory arithmetic, gated by config.
                # ----------------------------------------------------------
                try:
                    await self._compute_ai_metrics()
                except Exception as exc:
                    self._log.warning("ai_metrics_failed", error=str(exc))

                # ----------------------------------------------------------
                # 2. Strategy context (futures-only; no option chains)
                # ----------------------------------------------------------
                context = StrategyContext(
                    account=account,
                    positions=positions,
                    orders=open_orders,
                    config=self._config_dict,
                )
                # Populate risk status so the daily-loss/drawdown circuit
                # breakers and gates see real PnL — left as None they read
                # daily_pnl=0 and can never trigger.
                if self._risk_manager is not None:
                    try:
                        context.risk_status = await self._risk_manager.get_status()
                    except Exception as exc:
                        self._log.warning("risk_status_populate_failed", error=str(exc))

                # ----------------------------------------------------------
                # 3. AI-First Decision (if enabled and available)
                # ----------------------------------------------------------
                ai_decision: dict[str, Any] = {}
                ai_used = False

                if self._ai_enabled and self._groq_client is not None:
                    try:
                        ai_available = self._groq_client.is_available()
                        if ai_available:
                            rotation_enabled = bool(
                                self._config_dict.get("ai", {})
                                .get("rotation", {})
                                .get("enabled", False)
                            )
                            if rotation_enabled:
                                ai_mode = str(
                                    self._config_dict.get("ai", {}).get("mode", "trend")
                                ).lower()
                                if ai_mode == "scalp":
                                    scalp_ok = await self._run_scalp_rotation(
                                        account, positions, open_orders, context
                                    )
                                    ai_used = ai_used or scalp_ok
                                else:
                                    ai_used = await self._run_ai_rotation(
                                        account, positions, open_orders, context
                                    )
                            else:  # legacy path unchanged
                                if self._config_dict.get("trading", {}).get(
                                    "serial_trade_mode", True
                                ):
                                    closed = await self._close_all_positions()
                                    if closed:
                                        self._log.debug(
                                            "legacy_cycle_flattened",
                                        )
                                    else:
                                        self._log.warning(
                                            "legacy_cycle_flatten_incomplete",
                                        )
                                ai_decision = await self._run_ai_trading_cycle(
                                    underlyings, account, positions
                                )
                                ai_used = True
                                self._consecutive_ai_failures = 0
                                if ai_decision.get("action") in ("ENTER", "EXIT"):
                                    await self._execute_ai_action(ai_decision, context)
                        else:
                            self._log.warning("ai_not_available_skipping")
                    except Exception as exc:
                        self._consecutive_ai_failures += 1
                        self._last_ai_error = str(exc)
                        self._log.warning(
                            "ai_cycle_failed",
                            consecutive=self._consecutive_ai_failures,
                            error=str(exc),
                        )

                # ----------------------------------------------------------
                # 6. Update monitoring / metrics
                # ----------------------------------------------------------
                try:
                    risk_manager = self._risk_manager
                    if risk_manager is not None:
                        await risk_manager.update_monitoring(context)
                        # Check if any circuit breaker was triggered
                        cb_status = await risk_manager.get_status()
                        for cb_name, cb in cb_status.circuit_breakers.items():
                            if cb.active:
                                await self._notify_circuit_breaker(
                                    name=cb_name,
                                    reason=cb.reason or "Circuit breaker triggered",
                                    tier=getattr(cb, "tier", 0),
                                )
                except Exception as exc:
                    self._log.warning(
                        "risk_monitoring_update_error",
                        error=str(exc),
                    )

                # ----------------------------------------------------------
                # 6b. Periodic status / metrics (surface dry-run state)
                # ----------------------------------------------------------
                dry_run = self._is_dry_run
                testnet = bool(getattr(self._exchange_adapter, "is_testnet", False))
                self._log.info(
                    "cycle_status",
                    dry_run=dry_run,
                    testnet=testnet,
                    dry_run_guard_active=dry_run and not testnet,
                    mode=self._mode,
                    positions=len(positions),
                    ai_used=ai_used,
                )

                if self._metrics is not None:
                    self._metrics.set_gauge("active_positions", float(len(positions)))
                    self._metrics.set_gauge(
                        "active_strategies", float(len(self._active_strategies))
                    )
                    self._metrics.set_gauge("dry_run", 1.0 if dry_run else 0.0)
                    self._metrics.set_gauge(
                        "dry_run_guard_active",
                        1.0 if (dry_run and not testnet) else 0.0,
                    )
                    self._metrics.increment_counter("trading_cycles")

                    if ai_used:
                        self._metrics.increment_counter("ai_decisions")
                        self._metrics.set_gauge(
                            "ai_cycle_time_ms", self._last_ai_cycle_time_ms
                        )

                    if account is not None:
                        self._metrics.set_gauge(
                            "portfolio_value",
                            float(getattr(account, "total_usdt", Decimal(0))),
                        )

                # ----------------------------------------------------------
                # 7. Sleep for remaining interval
                # ----------------------------------------------------------
                elapsed = time.monotonic() - cycle_start
                sleep_time = max(0.0, float(self._cycle_interval) - elapsed)

                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)

            except asyncio.CancelledError:
                self._log.info("main_cycle_cancelled")
                break
            except Exception as exc:
                self._log.exception(
                    "main_cycle_error",
                    error=str(exc),
                )
                # On unexpected error, wait the full interval before retrying
                await asyncio.sleep(float(self._cycle_interval))
            finally:
                # Clear the per-cycle correlation id so a crashed cycle does
                # not leak its id into the next iteration's log lines.
                reset_correlation_id(cid_token)

    async def _run_ai_trading_cycle(
        self,
        underlyings: list[str],
        account: Any,
        positions: Any,
    ) -> dict[str, Any]:
        """Run the AI trading decision cycle.

        1. Collect market context (candles, account, positions, chains)
        2. Compute technical indicators
        3. Build structured prompts
        4. Call ``decide_trades()``
        5. Log and return the decision

        Parameters
        ----------
        underlyings:
            List of trading pairs to analyse.
        account:
            Current account state from the exchange adapter.
        positions:
            Current open positions from the exchange adapter.

        Returns
        -------
        dict
            The parsed trading decision from the LLM, or a HOLD dict on failure.
        """
        ai_start = time.monotonic()
        self._ai_cycle_count += 1

        try:
            # 1. Collect market context
            from quad.ai.context import collect_market_context

            context = await collect_market_context(
                exchange_adapter=self._exchange_adapter,
                market_data_engine=self._market_data,
                db_manager=self._db_manager,
                config=self._config_dict,
            )
            self._log.debug(
                "market_context_collected",
                pairs=len(context.candles),
                positions=len(context.positions),
                errors=len(context.errors),
            )

            # 2. Compute technical indicators per pair/timeframe
            from quad.ai.ta import compute_indicators

            indicators: dict[str, dict[str, Any]] = {}
            for key, candles in context.candles.items():
                try:
                    indicators[key] = compute_indicators(candles)
                except Exception as exc:
                    self._log.warning(
                        "indicator_computation_failed",
                        key=key,
                        error=str(exc),
                    )
                    indicators[key] = {}

            # 3. Build structured prompts
            from quad.ai.prompt import build_trading_prompt

            prompts = build_trading_prompt(
                context=context,
                indicators=indicators,
                config=self._config_dict,
            )

            # 4. Call Groq for trading decision
            self._log.debug(
                "ai_decision_request",
                cycle=self._ai_cycle_count,
                system_prompt_len=len(prompts["system"]),
                user_prompt_len=len(prompts["user"]),
            )

            ai_trade_cfg = self._config_dict.get("ai") or {}
            decision = await self._groq_client.decide_trades(
                system_prompt=prompts["system"],
                user_prompt=prompts["user"],
                temperature=ai_trade_cfg.get("temperature"),
                max_tokens=ai_trade_cfg.get("max_tokens"),
            )

            # Track timing
            self._last_ai_cycle_time_ms = round((time.monotonic() - ai_start) * 1000, 2)
            self._last_ai_decision = decision

            # Legacy-path validation: normalize through the same validator
            # the rotation path uses.  Defaults to reject (``veto``) unless
            # ``ai.validator.gate_mode`` is explicitly set to ``"warn"``.
            try:
                from quad.ai.validator import normalize_decision as _normalize

                _validator_cfg = (self._config_dict.get("ai", {}) or {}).get(
                    "validator", {}
                ) or {}
                _gate_mode = str(_validator_cfg.get("gate_mode", "veto"))
                try:
                    _min_conf = float(
                        _validator_cfg.get("min_confidence_to_trade", 0.0) or 0.0
                    )
                except (TypeError, ValueError):
                    _min_conf = 0.0
                _contract = decision.get("contract")
                _position_side = None
                try:
                    for _p in positions or []:
                        if (
                            getattr(_p, "symbol", "")
                            or getattr(_p, "contract_symbol", "")
                        ) == _contract:
                            _position_side = getattr(_p, "side", None)
                            break
                except Exception:
                    _position_side = None
                _snap: dict[str, Any] | None = None
                try:
                    for _k, _v in (indicators or {}).items():
                        if str(_k).startswith(str(_contract)):
                            _snap = _v
                            break
                except Exception:
                    _snap = None
                _result = _normalize(
                    decision,
                    position_side=_position_side,
                    indicators=_snap,
                    gate_mode=_gate_mode,
                    min_confidence_to_trade=_min_conf,
                )
                decision = _result.decision
                self._last_ai_decision = decision
                if not _result.ok:
                    self._log.warning(
                        "ai_decision_rejected",
                        action=decision.get("action"),
                        contract=_contract,
                        reason=_result.rejected_reason,
                    )
                    decision = {
                        "reasoning": (
                            f"Decision rejected by validator: {_result.rejected_reason}"
                        ),
                        "action": "HOLD",
                        "direction": "NEUTRAL",
                        "side": None,
                        "contract": _contract,
                        "quantity": None,
                        "confidence": 0.0,
                        "gate_result": decision.get("gate_result", "not_checked"),
                        "indicators": {},
                    }
                    self._last_ai_decision = decision
            except Exception as exc:
                self._log.warning("legacy_decision_normalize_failed", error=str(exc))

            # 5. Log decision to database
            try:
                await self._log_ai_decision(decision, context)
            except Exception as exc:
                self._log.warning("ai_decision_log_failed", error=str(exc))

            self._log.debug(
                "ai_decision_received",
                action=decision.get("action", "unknown"),
                strategy=decision.get("strategy"),
                confidence=decision.get("confidence"),
                cycle_time_ms=self._last_ai_cycle_time_ms,
            )

            return decision

        except Exception as exc:
            self._log.warning(
                "ai_trading_cycle_crashed",
                error=str(exc),
                cycle=self._ai_cycle_count,
            )
            underlyings = self._config_dict.get("trading", {}).get("underlyings", [])
            return {
                "reasoning": f"Trading cycle exception: {exc}",
                "action": "HOLD",
                "confidence": 0.0,
                "indicators": {},
                "contract": underlyings[0] if underlyings else "",
            }

    async def _run_scalp_rotation(
        self,
        account: Any,
        positions: Any,
        open_orders: Any,
        context: StrategyContext,
    ) -> bool:
        """Fast AI-judged scalp loop (Phase 5).

        Runs every worker cycle on scalp-mode workers (default 300s):
        1m/5m candles -> deterministic scalp signals -> gate -> ONE batch
        judge call for all judge-worthy symbols -> per-symbol validate ->
        execute (ENTER only when a position slot is free, EXIT always).

        Cost design (fits Groq free tier): the gate kills most cycles
        (no fresh 5m candle + no setup = no call); when setups exist, all
        symbols share a single judge call on a 1,000-RPD model
        (``ai.scalp.model``, default qwen/qwen3-32b); a separate daily
        budget (``ai.scalp.max_calls_per_day``, default 120) caps spend.
        Open scalps carry exchange-native TP/SL brackets plus an
        in-memory time-stop (``ai.scalp.max_hold_seconds``, default 900).
        """
        ai_cfg = self._config_dict.get("ai", {}) or {}
        scalp_cfg = ai_cfg.get("scalp", {}) or {}
        pairs = list(scalp_cfg.get("pairs") or ai_cfg.get("pairs") or [])
        if not pairs or self._groq_client is None:
            return False
        if not self._groq_client.is_available():
            self._log.warning("scalp_judge_unavailable")
            return False

        from quad.ai.context import collect_market_context

        scfg = dict(self._config_dict)
        scfg["ai"] = dict(ai_cfg)
        scfg["ai"]["pairs"] = pairs
        scfg["ai"]["timeframes"] = list(scalp_cfg.get("timeframes", ["5"]))
        scfg["ai"]["candle_count"] = int(scalp_cfg.get("candle_lookback", 100))
        try:
            sctx = await collect_market_context(
                exchange_adapter=self._exchange_adapter,
                market_data_engine=self._market_data,
                db_manager=self._db_manager,
                config=scfg,
            )
        except Exception as exc:
            self._log.warning("scalp_context_failed", error=str(exc))
            return False

        from quad.ai.ta import compute_indicators, generate_scalp_signal

        from quad.types.domain import PositionStatus

        live = [
            p
            for p in (positions or [])
            if getattr(p, "status", None) == PositionStatus.OPEN
        ]
        live_symbols = {
            getattr(p, "symbol", "") or getattr(p, "contract_symbol", "") for p in live
        }
        max_positions = int(
            self._config_dict.get("risk", {}).get("max_positions", 1) or 1
        )

        # Time-stop: flat scalp positions held past max_hold_seconds.
        # Only when every open position is scalp-managed; never touch a
        # trend position from here (its brackets still protect it).
        max_hold = float(scalp_cfg.get("max_hold_seconds", 900) or 0)
        now_mono = time.monotonic()
        if max_hold > 0:
            for sym in list(self._scalp_held):
                if sym in live_symbols and now_mono - self._scalp_held[sym] >= max_hold:
                    if live_symbols <= set(self._scalp_held):
                        self._log.info("scalp_time_stop", symbol=sym)
                        await self._close_all_positions()
                        self._scalp_held.pop(sym, None)
                        live_symbols.discard(sym)
                    else:
                        self._log.warning(
                            "scalp_time_stop_skipped_trend_open", symbol=sym
                        )

        gate_cfg = {
            "enabled": True,
            "min_strength": float(scalp_cfg.get("min_strength", 0.6)),
        }
        budget = int(scalp_cfg.get("max_calls_per_day", 120) or 0)
        counters = ("_scalp_day", "_scalp_used", "_scalp_budget_warned_day")

        judged: list[dict[str, Any]] = []
        snapshots: dict[str, dict[str, Any]] = {}
        for symbol in pairs:
            per_tf: dict[str, dict[str, Any]] = {}
            for key in sctx.candles or {}:
                if key.split("_", 1)[0] == symbol:
                    try:
                        per_tf[key] = compute_indicators(sctx.candles[key])
                    except Exception:
                        per_tf[key] = {}
            snap = self._merge_indicators_for_symbol(per_tf, symbol)
            snapshots[symbol] = snap
            sig = generate_scalp_signal(snap, symbol=symbol)
            sig["symbol"] = symbol
            side = self._position_side_for_symbol(sctx, symbol)
            allowed, why = self._judge_gate_check(
                symbol,
                sig,
                side,
                sctx,
                gate_cfg,
                budget=budget,
                counters=counters,
            )
            if allowed:
                judged.append(sig)
            else:
                self._log.debug("scalp_judge_skipped", symbol=symbol, reason=why)

        if not judged:
            return True  # locals say nothing to do; free cycle

        from quad.ai.prompt import build_batch_judgement_prompt

        tps = self._scalp_bands(scalp_cfg)
        prompts = build_batch_judgement_prompt(
            judged,
            has_position={s["symbol"]: s["symbol"] in live_symbols for s in judged},
            tp_pct=tps[0],
            sl_pct=tps[1],
        )
        model = str(scalp_cfg.get("model", "qwen/qwen3-32b"))
        try:
            decisions = await self._groq_client.decide_batch(
                system_prompt=prompts["system"],
                user_prompt=prompts["user"],
                symbols=[s["symbol"] for s in judged],
                max_tokens=int(scalp_cfg.get("max_tokens", 512) or 512),
                model=model,
            )
        except Exception as exc:
            self._log.warning("scalp_judge_failed", error=str(exc))
            return False

        from quad.ai.validator import normalize_decision

        progressed = True
        for dec in decisions:
            sym = dec.get("symbol") or dec.get("contract", "?")
            result = normalize_decision(
                dec,
                position_side=self._position_side_for_symbol(sctx, sym),
                indicators=snapshots.get(sym, {}),
                gate_mode="veto",
                min_confidence_to_trade=float(scalp_cfg.get("min_confidence", 0.55)),
            )
            d = result.decision
            if not result.ok:
                self._log.warning(
                    "scalp_decision_rejected", symbol=sym, reason=result.rejected_reason
                )
                continue
            d["contract"] = sym
            d["strategy"] = "scalp"
            action = d.get("action", "HOLD")
            if action == "ENTER" and len(live_symbols) >= max_positions:
                self._log.info("scalp_enter_blocked_no_slot", symbol=sym)
                continue
            if action in ("ENTER", "EXIT"):
                ok = await self._execute_ai_action(d, context)
                if ok and action == "ENTER":
                    live_symbols.add(sym)
                    self._scalp_held[sym] = now_mono
                elif ok and action == "EXIT":
                    live_symbols.discard(sym)
                    self._scalp_held.pop(sym, None)
        return progressed

    def _scalp_bands(self, scalp_cfg: dict[str, Any]) -> tuple[float, float]:
        """(TP%, SL%) for scalp brackets (tight by design)."""
        try:
            tp = float(scalp_cfg.get("tp_pct", 15.0))
        except (TypeError, ValueError):
            tp = 15.0
        try:
            sl = float(scalp_cfg.get("sl_pct", 8.0))
        except (TypeError, ValueError):
            sl = 8.0
        return tp, sl

    async def _run_ai_rotation(
        self,
        account: Any,
        positions: Any,
        open_orders: Any,
        context: StrategyContext,
    ) -> bool:
        """Run the pair-rotation AI cycle (one pair at a time).

        CASE A — a position is open: scan ONLY that pair and manage it
        (EXIT / adjust_stop / reduce_position); never open a second.

        CASE B — flat: scan each configured pair once, starting at the
        rotation index, sleeping ``retry_sleep`` seconds between attempts.
        Advance to the next pair only when the current position closes.

        Returns
        -------
        bool
            ``True`` if the rotation made progress this cycle, ``False``
            if it could not run (no pairs / Groq unavailable).
        """
        pairs = list(self._config_dict.get("ai", {}).get("pairs", []))
        if (
            not pairs
            or self._groq_client is None
            or not self._groq_client.is_available()
        ):
            self._log.warning("ai_rotation_unavailable")
            return False
        retry_sleep = float(
            self._config_dict["ai"]["rotation"].get("retry_sleep_seconds", 30.0)
        )
        from quad.types.domain import PositionStatus

        open_positions = [
            p for p in positions if getattr(p, "status", None) == PositionStatus.OPEN
        ]

        # CASE A — a position is open: close it and open a fresh trade
        # (1 trade per cycle) unless close_open_position_each_cycle is off,
        # in which case the previous hold-until-TP/SL behavior applies.
        if open_positions:
            held = open_positions[0]
            held_symbol = getattr(held, "symbol", "") or getattr(
                held, "contract_symbol", ""
            )
            self._current_symbol = held_symbol
            self._log.info("rotation_managing_open_position", symbol=held_symbol)

            rotation_cfg = self._config_dict.get("ai", {}).get("rotation", {}) or {}
            close_each_cycle = bool(
                rotation_cfg.get("close_open_position_each_cycle", True)
            )

            if close_each_cycle:
                # Roll gate (both-mode): on fast loops the trend position
                # must roll on its own cadence (default 55 min), not every
                # 5-minute cycle. Until due, manage the held position
                # instead (the judge gate HOLDs without a fresh candle).
                roll_min = float(rotation_cfg.get("roll_min_seconds", 3300) or 0)
                roll_registry = getattr(self, "_last_trend_roll_ts", None)
                if roll_registry is None:
                    roll_registry = self._last_trend_roll_ts = {}
                last_roll = roll_registry.get(held_symbol, 0.0)
                if (
                    roll_min > 0
                    and last_roll
                    and time.monotonic() - last_roll < roll_min
                ):
                    self._log.info(
                        "rotation_roll_not_due",
                        symbol=held_symbol,
                    )
                    try:
                        decision = await self._scan_pair(held_symbol)
                    except Exception as exc:  # CancelledError NOT caught
                        self._log.warning(
                            "ai_scan_error", symbol=held_symbol, error=str(exc)
                        )
                        return False
                    if decision.get("action") == "EXIT":
                        await self._close_all_positions()
                        self._rotation_hold_since.pop(held_symbol, None)
                    elif decision.get("action") in ("adjust_stop", "reduce_position"):
                        await self._execute_ai_action(decision, context)
                    return True
                roll_registry[held_symbol] = time.monotonic()
                # Roll the position every hour: close the current trade (the
                # EXIT is broadcast to Telegram by _notify_trade below) and
                # fall through to CASE B to scan for a fresh entry.  Never
                # open a second position in the same cycle: CASE B opens at
                # most one ENTER.
                self._log.info(
                    "rotation_cycle_rolling_position",
                    symbol=held_symbol,
                )
                closed = await self._close_all_positions()
                self._rotation_hold_since.pop(held_symbol, None)
                if closed:
                    self._log.info(
                        "rotation_cycle_closed",
                        symbol=held_symbol,
                    )
                    # Only announce the EXIT after the close is confirmed
                    # flat -- a trade that is still open on the exchange must not
                    # be broadcast as closed.
                    try:
                        from decimal import Decimal as _D

                        closed_pnl = self._compute_position_pnl(
                            entry_price=_D(str(getattr(held, "entry_price", 0) or 0)),
                            exit_price=_D(str(getattr(held, "current_price", 0) or 0)),
                            quantity=_D(str(getattr(held, "quantity", 0) or 0)),
                            side=str(getattr(held, "side", "")),
                        )
                        pnl_text = self._format_pnl(
                            closed_pnl,
                            _D(str(getattr(held, "entry_price", 0) or 0)),
                        )
                        await self._notify_trade(
                            action_type="EXIT",
                            strategy="rotation_roll",
                            contract=held_symbol,
                            side=self._side_label(getattr(held, "side", "")),
                            quantity=str(getattr(held, "quantity", "")),
                            price=None,
                            reason=(
                                "Rotation cycle: closing previous trade "
                                "before opening a new one"
                            ),
                            pnl=pnl_text,
                        )
                    except Exception as exc:
                        self._log.warning(
                            "rotation_exit_notify_failed",
                            symbol=held_symbol,
                            error=str(exc),
                        )
                    # Advance the rotation index to the pair AFTER the closed
                    # position (ADR-080: "advance to the next pair when the
                    # position closes").  Deterministic regardless of the
                    # prior index state: closing BTC -> next scan is ETH,
                    # not BTC again and not a skipped pair.
                    try:
                        held_idx = pairs.index(held_symbol)
                    except ValueError:
                        held_idx = self._rotation_index
                    self._rotation_index = (held_idx + 1) % len(pairs)
                    # Refresh the in-memory position list (shared with the
                    # caller) so cycle_status / metrics show the post-close
                    # state instead of the stale pre-close position.
                    try:
                        fresh_positions = await self._exchange_adapter.get_positions()
                        if isinstance(fresh_positions, list) and fresh_positions:
                            positions[:] = [
                                p
                                for p in fresh_positions
                                if getattr(p, "status", None) != PositionStatus.OPEN
                            ]
                        elif isinstance(fresh_positions, list):
                            positions[:] = fresh_positions
                    except Exception as exc:
                        self._log.warning(
                            "rotation_positions_refresh_failed",
                            symbol=held_symbol,
                            error=str(exc),
                        )
                else:
                    self._log.warning(
                        "rotation_cycle_close_failed",
                        symbol=held_symbol,
                    )
                    return True  # do not open a second trade when close failed
            else:
                # Legacy behavior: hold until TP/SL bracket triggers.
                # Stale-position guard: force-close a position held longer than
                # ai.rotation.max_hold_seconds so a trade can't hang for hours
                # waiting for a TP/SL bracket that never triggers.
                max_hold_s = float(rotation_cfg.get("max_hold_seconds", 0.0))
                if max_hold_s > 0:
                    held_since = self._rotation_hold_since.get(held_symbol)
                    now = time.monotonic()
                    if held_since is None:
                        self._rotation_hold_since[held_symbol] = now
                    elif now - held_since >= max_hold_s:
                        self._log.info(
                            "rotation_max_hold_reached",
                            symbol=held_symbol,
                            max_hold_seconds=max_hold_s,
                        )
                        closed = await self._close_all_positions()
                        self._rotation_hold_since.pop(held_symbol, None)
                        if closed:
                            self._log.info(
                                "rotation_max_hold_closed",
                                symbol=held_symbol,
                            )
                            return True  # flat now; next cycle opens a fresh trade
                        self._log.warning(
                            "rotation_max_hold_close_failed",
                            symbol=held_symbol,
                        )

                # Price-bracket guard: if the mark price is clearly beyond a
                # TP/SL trigger but the bracket order has not fired, the position
                # would hang indefinitely - force-close it now.
                try:
                    violated, which = await self._price_bracket_violation(
                        held_symbol,
                        getattr(held, "side", None),
                        open_orders,
                    )
                except Exception as exc:
                    self._log.warning(
                        "price_bracket_check_failed",
                        symbol=held_symbol,
                        error=str(exc),
                    )
                    violated, which = False, ""
                if violated:
                    self._log.info(
                        "rotation_price_beyond_bracket",
                        symbol=held_symbol,
                        bracket=which,
                    )
                    closed = await self._close_all_positions()
                    self._rotation_hold_since.pop(held_symbol, None)
                    if closed:
                        self._log.info(
                            "rotation_price_bracket_closed",
                            symbol=held_symbol,
                            bracket=which,
                        )
                        return True  # flat now; next cycle opens a fresh trade
                    self._log.warning(
                        "rotation_price_bracket_close_failed",
                        symbol=held_symbol,
                        bracket=which,
                    )

                try:
                    decision = await self._scan_pair(
                        held_symbol
                    )  # raises on Groq/API error
                except Exception as exc:  # CancelledError NOT caught (BaseException)
                    self._log.warning(
                        "ai_scan_error", symbol=held_symbol, error=str(exc)
                    )
                    return False
                hold_action = decision.get("action", "HOLD")
                if hold_action in ("ENTER", "EXIT"):
                    # Hold-until-TP/SL: while a position is open, never open a new
                    # trade and never close early.  The position is closed ONLY by
                    # the STOP_LOSS / TAKE_PROFIT bracket orders attached at entry.
                    self._log.info(
                        "rotation_hold_until_tp_sl",
                        action=hold_action,
                        symbol=held_symbol,
                        reason="position open; close only via TP/SL bracket",
                    )
                elif hold_action in ("adjust_stop", "reduce_position"):
                    await self._execute_ai_action(decision, context)
                return True  # HOLD / no-op -> keep holding; wait for next hour

        # CASE B — flat: scan each pair once, starting at the rotation index.
        self._rotation_hold_since.clear()  # flat: nothing held to track
        self._rotation_index %= len(pairs)
        scanned = 0
        while scanned < len(pairs):
            if not self._groq_client.is_available():  # daily limit hit mid-scan
                self._log.warning("ai_rate_limit_hit_stopping_scan")
                break
            symbol = pairs[self._rotation_index % len(pairs)]
            self._current_symbol = symbol
            self._log.info(
                "rotation_scanning_pair", symbol=symbol, index=self._rotation_index
            )
            try:
                decision = await self._scan_pair(symbol)
            except Exception as exc:
                self._consecutive_ai_failures += 1
                self._last_ai_error = str(exc)
                self._log.warning("ai_scan_failed", symbol=symbol, error=str(exc))
                if isinstance(exc, RuntimeError) and "rate limit" in str(exc).lower():
                    break  # stop burning requests this hour
                decision = {
                    "action": "HOLD",
                    "reasoning": f"scan exception: {exc}",
                    "confidence": 0.0,
                    "contract": symbol,
                }

            action = decision.get("action", "HOLD")
            if action == "ENTER":
                if await self._execute_ai_action(decision, context):
                    # Position opened -> index now points at the NEXT pair, so the
                    # post-close scan continues after this one.
                    self._rotation_index = (self._rotation_index + 1) % len(pairs)
                    self._log.info("rotation_opened_position", symbol=symbol)
                    return True
                self._log.warning(
                    "rotation_enter_failed_advancing", symbol=symbol
                )  # risk/exec rejected
            elif action == "EXIT":
                self._log.info(
                    "rotation_exit_without_position_advancing", symbol=symbol
                )

            self._rotation_index = (self._rotation_index + 1) % len(pairs)
            scanned += 1
            if scanned < len(pairs):
                await asyncio.sleep(retry_sleep)  # 30s between HOLD scans

        self._log.info("rotation_scan_complete_all_hold", scanned=scanned)
        return True

    def _position_side_for_symbol(
        self,
        context: StrategyContext | MarketContext,
        symbol: str,
    ) -> Any:
        """Return the open position side for ``symbol``, or ``None`` when flat.

        Used by the validator and the execution backstop to derive EXIT sides
        deterministically and to decide whether an EXIT is even possible.

        Parameters
        ----------
        context:
            Current strategy context (contains live positions).
        symbol:
            Contract symbol, e.g. ``"BTCUSDT"``.

        Returns
        -------
        Any
            The ``PositionSide`` of the open position for ``symbol``, or
            ``None`` when no such position is open.
        """
        from quad.types.domain import PositionStatus

        for p in context.positions:
            sym = getattr(p, "symbol", "") or getattr(p, "contract_symbol", "")
            if sym == symbol and getattr(p, "status", None) == PositionStatus.OPEN:
                return getattr(p, "side", None)
        return None

    @staticmethod
    def _merge_indicators_for_symbol(
        indicators: dict[str, dict[str, Any]],
        symbol: str,
    ) -> dict[str, Any]:
        """Merge per-timeframe indicator dicts for ``symbol`` into one dict.

        ``indicators`` keys look like ``"BTCUSDT_15m"``.  The merged dict is
        passed to the validator's plausibility gate; later timeframes (e.g.
        1h) override earlier ones, biasing the gate toward the macro view.

        Parameters
        ----------
        indicators:
            Dict of ``{pair_timeframe_key: indicator_dict}``.
        symbol:
            Contract symbol to filter on, e.g. ``"BTCUSDT"``.

        Returns
        -------
        dict[str, Any]
            Merged indicator dict for ``symbol`` (possibly empty).
        """
        merged: dict[str, Any] = {}
        for key, ind in indicators.items():
            if key.split("_", 1)[0] == symbol:
                merged.update(ind or {})
        return merged

    def _day_budget_allows(
        self,
        budget: int,
        symbol: str,
        scope: str,
        day_attr: str,
        used_attr: str,
        warned_attr: str,
    ) -> bool:
        """UTC-day rolling call budget shared by trend/scalp judges."""
        if budget <= 0:
            return True
        import datetime as _dt

        today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
        if getattr(self, day_attr) != today:
            setattr(self, day_attr, today)
            setattr(self, used_attr, 0)
        if getattr(self, used_attr) >= budget:
            if getattr(self, warned_attr) != today:
                setattr(self, warned_attr, today)
                self._log.warning(
                    "ai_judge_budget_exhausted_local_only",
                    symbol=symbol,
                    budget=budget,
                    scope=scope,
                )
            return False
        setattr(self, used_attr, getattr(self, used_attr) + 1)
        return True

    def _judge_gate_check(
        self,
        symbol: str,
        local_signal: dict[str, Any],
        position_side: Any,
        context: Any,
        gate_cfg: dict[str, Any],
        budget: int | None = None,
        counters: tuple[str, str, str] | None = None,
    ) -> tuple[bool, str]:
        """Decide whether this cycle deserves a paid AI judge call.

        A call is allowed only when there is something worth judging: a
        fresh closed candle AND (a local setup trigger OR an open position
        needing a decision). Otherwise the worker holds on locals for free.
        A per-tenant daily budget caps total calls; beyond it the worker
        runs local-only (exchange-native TP/SL brackets still protect
        open positions). ``budget``/``counters`` let the scalp rotation
        reuse this gate with its own (larger) budget namespace.
        """
        ai_cfg = self._config_dict.get("ai", {}) or {}
        if budget is None:
            budget = int(ai_cfg.get("judge_max_calls_per_day", 48) or 0)
        day_attr, used_attr, warned_attr = counters or (
            "_judge_day",
            "_judge_used",
            "_judge_budget_warned_day",
        )
        scope = "scalp" if (counters or []) else "trend"
        latest_ts = 0
        for key, candles in (getattr(context, "candles", {}) or {}).items():
            if symbol in str(key) and candles:
                try:
                    ts = int(getattr(candles[-1], "timestamp", 0) or 0)
                except (TypeError, ValueError):
                    ts = 0
                latest_ts = max(latest_ts, ts)
        fresh = bool(latest_ts) and latest_ts != self._last_judge_candle_ts.get(symbol)
        try:
            strength = float(local_signal.get("local_strength", 0.0) or 0.0)
        except (TypeError, ValueError):
            strength = 0.0
        min_strength = float(gate_cfg.get("min_strength", 0.5))
        setup = (
            local_signal.get("local_direction") in ("LONG", "SHORT")
            and strength >= min_strength
        )
        has_position = position_side is not None
        if not fresh and not has_position:
            return False, "no fresh candle and no open position"
        if fresh and not setup and not has_position:
            return False, "fresh candle but no local setup"
        if not self._day_budget_allows(
            budget, symbol, scope, day_attr, used_attr, warned_attr
        ):
            return False, "daily budget exhausted (local-only mode)"
        if latest_ts:
            self._last_judge_candle_ts[symbol] = latest_ts
        return True, "judge-worthy"

    async def _scan_pair(self, symbol: str) -> dict[str, Any]:
        """Run the single-pair AI pipeline for ``symbol``.

        Mirrors ``_run_ai_trading_cycle`` but scoped to one pair: collects
        market context with ``ai.pairs=[symbol]``, computes indicators,
        builds prompts, calls Groq, logs the decision, and pins the
        decision contract to ``symbol`` so the LLM cannot hallucinate a
        different pair.

        Parameters
        ----------
        symbol:
            Trading pair to scan, e.g. ``"BTCUSDT"``.

        Returns
        -------
        dict
            The parsed trading decision from the LLM.

        Raises
        ------
        Exception
            Groq / API errors propagate to the caller (``_run_ai_rotation``).
        """
        ai_start = time.monotonic()
        self._ai_cycle_count += 1
        cfg = dict(self._config_dict)
        cfg["ai"] = dict(self._config_dict["ai"])
        cfg["ai"]["pairs"] = [symbol]

        from quad.ai.context import collect_market_context

        context = await collect_market_context(
            exchange_adapter=self._exchange_adapter,
            market_data_engine=self._market_data,
            db_manager=self._db_manager,
            config=cfg,
        )

        from quad.ai.ta import compute_indicators

        indicators: dict[str, dict[str, Any]] = {}
        for key, candles in context.candles.items():
            try:
                indicators[key] = compute_indicators(candles)
            except Exception as exc:
                self._log.warning(
                    "indicator_computation_failed",
                    key=key,
                    error=str(exc),
                )
                indicators[key] = {}

        # Merge per-timeframe indicators for this symbol and resolve the
        # open position side — both needed before the local signal build.
        indicator_snapshot = self._merge_indicators_for_symbol(indicators, symbol)
        position_side = self._position_side_for_symbol(context, symbol)

        from quad.ai.ta import generate_local_signal

        # Extract funding rate for the symbol from the market context.
        funding_rate = None
        funding_annual_pct = None
        if context.funding_rates:
            fr = context.funding_rates.get(symbol)
            if fr is not None:
                funding_rate = float(getattr(fr, "funding_rate", 0) or 0)
                # Annualize: funding_rate * 3 per-day * 365 days * 100 = %
                funding_annual_pct = round(funding_rate * 3 * 365 * 100, 2)

        # Build a compact local signal from the computed indicators.
        # The AI receives this distilled signal (not raw candles) and makes
        # only the final ENTER/HOLD/EXIT decision.
        local_signal = generate_local_signal(
            indicator_snapshot,
            symbol=symbol,
            funding_rate=funding_rate,
            funding_annual_pct=funding_annual_pct,
        )

        gate_cfg = (self._config_dict.get("ai", {}) or {}).get("judge_gate", {}) or {}
        if gate_cfg.get("enabled", True):
            allowed, why = self._judge_gate_check(
                symbol,
                local_signal,
                position_side,
                context,
                gate_cfg,
            )
            if not allowed:
                self._log.info("ai_judge_skipped", symbol=symbol, reason=why)
                return {
                    "reasoning": f"Judge skipped ({why}); holding on local signal",
                    "action": "HOLD",
                    "direction": "NEUTRAL",
                    "side": None,
                    "contract": symbol,
                    "quantity": None,
                    "confidence": 0.0,
                    "gate_result": "judge_skipped",
                    "indicators": indicator_snapshot,
                }

        from quad.ai.prompt import build_final_judgement_prompt

        prompts = build_final_judgement_prompt(
            local_signal=local_signal,
            positions=context.positions,
            account=context.account,
            config=cfg,
        )
        ai_tier = str(cfg["ai"].get("tier", "cheap"))
        judge_model = self._groq_client.model_for_tier(ai_tier)
        decision = await self._groq_client.decide_trades(  # may raise (rate-limit/API)
            system_prompt=prompts["system"],
            user_prompt=prompts["user"],
            temperature=cfg["ai"].get("temperature"),
            max_tokens=cfg["ai"].get("max_tokens"),
            model=judge_model,
        )

        # Disagreement escalation (Phase 5): a strong local signal opposed by
        # the cheap judge gets one re-judge on the smart model; keep the
        # higher-confidence verdict. Smart-tier tenants already judge smart.
        if ai_tier.lower() != "smart" and cfg["ai"].get(
            "escalate_on_disagreement", True
        ):
            try:
                local_strength = float(local_signal.get("local_strength", 0.0) or 0.0)
            except (TypeError, ValueError):
                local_strength = 0.0
            esc_min = float(cfg["ai"].get("escalate_min_strength", 0.7))
            oppose = {"LONG": "SHORT", "SHORT": "LONG"}
            local_dir = local_signal.get("local_direction")
            if (
                local_dir in oppose
                and local_strength >= esc_min
                and decision.get("direction") == oppose[local_dir]
            ):
                self._log.info(
                    "ai_judge_escalated",
                    symbol=symbol,
                    local=local_dir,
                    cheap_direction=decision.get("direction"),
                )
                smart_decision = await self._groq_client.decide_trades(
                    system_prompt=prompts["system"],
                    user_prompt=prompts["user"],
                    temperature=cfg["ai"].get("temperature"),
                    max_tokens=cfg["ai"].get("max_tokens"),
                    model=self._groq_client.model_for_tier("smart"),
                )
                self._judge_used += 1
                try:
                    cheap_conf = float(decision.get("confidence", 0.0) or 0.0)
                    smart_conf = float(smart_decision.get("confidence", 0.0) or 0.0)
                except (TypeError, ValueError):
                    cheap_conf, smart_conf = 0.0, 0.0
                if smart_conf >= cheap_conf:
                    decision = smart_decision

        self._last_ai_cycle_time_ms = round((time.monotonic() - ai_start) * 1000, 2)

        # ----------------------------------------------------------------
        # Phase 1 inversion guard: deterministically validate the decision.
        # The LLM forecasts a DIRECTION; normalize_decision derives the order
        # side and (in veto mode) rejects implausible entries.  A rejected
        # decision is replaced with a safe HOLD so it never reaches execution.
        # ----------------------------------------------------------------
        from quad.ai.validator import normalize_decision

        validator_cfg = cfg.get("ai", {}).get("validator", {})
        gate_mode = validator_cfg.get("gate_mode", "veto")
        min_confidence_to_trade = validator_cfg.get("min_confidence_to_trade", 0.0)

        result = normalize_decision(
            decision,
            position_side=position_side,
            indicators=indicator_snapshot,
            gate_mode=gate_mode,
            min_confidence_to_trade=min_confidence_to_trade,
        )
        decision = result.decision
        decision["indicators"] = indicator_snapshot

        if not result.ok:
            self._log.warning(
                "ai_decision_rejected",
                action=decision.get("action"),
                contract=symbol,
                reason=result.rejected_reason,
                corrected=result.corrected,
            )
            # Replace with a safe HOLD (same style as contract-pinning):
            # a rejected decision must never reach execution.
            decision = {
                "reasoning": f"Decision rejected by validator: {result.rejected_reason}",
                "action": "HOLD",
                "direction": "NEUTRAL",
                "side": None,
                "contract": symbol,
                "quantity": None,
                "confidence": 0.0,
                "gate_result": decision.get("gate_result", "not_checked"),
                "indicators": indicator_snapshot,
            }
        elif result.corrected:
            self._log.info(
                "ai_decision_corrected",
                action=decision.get("action"),
                contract=symbol,
                corrected=result.corrected,
                side=decision.get("side"),
            )

        self._last_ai_decision = decision
        try:
            await self._log_ai_decision(decision, context)
        except Exception as exc:
            self._log.warning("ai_decision_log_failed", error=str(exc))

        # Pin contract: the prompt contains ONLY this pair, so any other contract is a hallucination.
        if decision.get("action") in (
            "ENTER",
            "EXIT",
            "adjust_stop",
            "reduce_position",
        ):
            if decision.get("contract") != symbol:
                self._log.warning(
                    "ai_contract_pinned", expected=symbol, got=decision.get("contract")
                )
            decision["contract"] = symbol
        return decision
