"""Recurring bot jobs for Quad Futures Telegram bot.

Jobs run on a schedule via the PTB job queue.  They only send messages
if a ``notification_chat_id`` is configured.
"""

from __future__ import annotations

import time as _time
from decimal import Decimal
from typing import Any

import structlog
from telegram.ext import ContextTypes

from quad.risk.gates import effective_min_liquidation_distance

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

logger = structlog.get_logger(__name__)


# ============================================================================
# QuadBotJobs
# ============================================================================


class QuadBotJobs:
    """Container for all recurring job callbacks.

    Parameters
    ----------
    shared_state:
        Dict carrying component references and configuration shared between
        command and job handlers.
    """

    def __init__(self, shared_state: dict[str, Any]) -> None:
        self._log = logger.bind()
        self._state = shared_state
        self._config: dict[str, Any] = shared_state["config"]
        self._telegram_config: dict[str, Any] = shared_state["telegram_config"]
        self._notification_chat_id: int | None = shared_state.get(
            "notification_chat_id"
        )

        # Subsystem references
        self._orchestrator = shared_state.get("orchestrator")
        self._risk_manager = shared_state.get("risk_manager")
        self._market_data_engine = shared_state.get("market_data_engine")
        self._optimizer = shared_state.get("optimizer")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _send_if_configured(
        self, context: ContextTypes.DEFAULT_TYPE, text: str
    ) -> bool:
        """Send a message to the notification chat if configured.

        Returns ``True`` if the message was sent.
        """
        if self._notification_chat_id is None:
            return False
        try:
            await context.bot.send_message(
                chat_id=self._notification_chat_id,
                text=text,
                parse_mode="Markdown",
            )
            return True
        except Exception as exc:
            self._log.warning("job_send_failed", error=str(exc))
        # Markdown can fail on dynamic content (e.g. strategy names, AI text);
        # fall back to a plain-text send so scheduled notifications still go out.
        try:
            await context.bot.send_message(
                chat_id=self._notification_chat_id,
                text=text,
                parse_mode=None,
            )
            return True
        except Exception as exc:
            self._log.warning("job_send_failed_plain", error=str(exc))
            return False

    # ------------------------------------------------------------------
    # Job callbacks
    # ------------------------------------------------------------------

    async def job_status_summary(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Send a periodic status summary to the notification chat.

        Runs every 60 minutes.  Includes position count, daily PnL,
        active strategies, and circuit breaker status.
        """
        # Gather status
        position_count = 0
        daily_pnl = Decimal(0)
        circuit_breakers_active = 0

        if self._risk_manager:
            try:
                rs = await self._risk_manager.get_status()
                daily_pnl = rs.daily_pnl
                circuit_breakers_active = sum(
                    1 for cb in rs.circuit_breakers.values() if cb.active
                )
            except Exception as exc:
                self._log.warning("job_status_risk_error", error=str(exc))

        if self._orchestrator:
            try:
                # The orchestrator exposes no ``positions`` attribute; read
                # live positions from the exchange adapter (same pattern as
                # ``cmd_status``).  Previously the count was always 0.
                exchange_adapter = getattr(
                    self._orchestrator, "_exchange_adapter", None
                )
                if exchange_adapter is not None:
                    positions = await exchange_adapter.get_positions()
                    position_count = (
                        len(positions) if isinstance(positions, list) else 0
                    )
            except Exception as exc:
                self._log.warning("job_status_positions_error", error=str(exc))

        pnl_emoji = "🟢" if daily_pnl >= 0 else "🔴"
        cb_emoji = "⚠️" if circuit_breakers_active > 0 else "✅"

        msg = (
            f"📊 *Hourly Status Summary*\n\n"
            f"*Positions:* {position_count} open\n"
            f"*Daily PnL:* {pnl_emoji} ${float(daily_pnl):,.2f}\n"
            f"*Circuit Breakers:* {cb_emoji} {circuit_breakers_active} active"
        )

        sent = await self._send_if_configured(context, msg)
        self._log.info(
            "job_status_summary",
            sent=sent,
            positions=position_count,
            daily_pnl=str(daily_pnl),
            breakers_active=circuit_breakers_active,
        )

    async def job_risk_alert(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Check circuit breakers and alert if any are triggered.

        Runs every 5 minutes.  Only sends a message if at least one
        circuit breaker is active.
        """
        if self._risk_manager is None:
            return

        try:
            rs = await self._risk_manager.get_status()
            active_breakers = {
                name: cb for name, cb in rs.circuit_breakers.items() if cb.active
            }

            if not active_breakers:
                return  # Silent — no alert needed

            lines = ["🚨 *Risk Alert — Circuit Breakers Active*\n"]
            for name, cb in active_breakers.items():
                tier_info = f"  Tier: {cb.tier}" if cb.tier else ""
                lines.append(f"• `{name}` — {cb.reason}{tier_info}")

            msg = "\n".join(lines)
            sent = await self._send_if_configured(context, msg)
            self._log.warning(
                "job_risk_alert",
                sent=sent,
                active_breakers=list(active_breakers.keys()),
            )

        except Exception as exc:
            self._log.warning("job_risk_alert_error", error=str(exc))

    async def job_daily_report(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Send an end-of-day PnL summary.

        Scheduled at the configured time (default 23:00 UTC).
        Includes daily PnL, trade count, position count, and risk status.
        """
        daily_pnl = Decimal(0)
        position_count = 0
        trade_count = 0
        circuit_breakers_active = 0

        if self._risk_manager:
            try:
                rs = await self._risk_manager.get_status()
                daily_pnl = rs.daily_pnl
                circuit_breakers_active = sum(
                    1 for cb in rs.circuit_breakers.values() if cb.active
                )
            except Exception as exc:
                self._log.warning("job_daily_risk_error", error=str(exc))

        if self._orchestrator:
            try:
                # Read live positions from the exchange adapter (the
                # orchestrator exposes no ``positions`` attribute).
                exchange_adapter = getattr(
                    self._orchestrator, "_exchange_adapter", None
                )
                if exchange_adapter is not None:
                    positions = await exchange_adapter.get_positions()
                    position_count = (
                        len(positions) if isinstance(positions, list) else 0
                    )
            except Exception as exc:
                self._log.warning("job_daily_positions_error", error=str(exc))

        # Get trade count from DB if available
        db = self._state.get("db_manager")
        if db and db.is_connected:
            try:
                async with db.pool.acquire() as conn:
                    trade_count = (
                        await conn.fetchval("SELECT COUNT(*) FROM trades") or 0
                    )
            except Exception as exc:
                self._log.warning("job_daily_trade_count_error", error=str(exc))

        pnl_emoji = "🟢" if daily_pnl >= 0 else "🔴"
        cb_emoji = "⚠️" if circuit_breakers_active > 0 else "✅"
        today_str = _time.strftime("%Y-%m-%d")

        msg = (
            f"📅 *Daily Report — {today_str}*\n\n"
            f"*Daily PnL:* {pnl_emoji} ${float(daily_pnl):,.2f}\n"
            f"*Total Trades:* {trade_count}\n"
            f"*Open Positions:* {position_count}\n"
            f"*Circuit Breakers:* {cb_emoji} {circuit_breakers_active} active\n\n"
            "_Report generated automatically._"
        )

        sent = await self._send_if_configured(context, msg)
        self._log.info(
            "job_daily_report",
            sent=sent,
            date=today_str,
            daily_pnl=str(daily_pnl),
            positions=position_count,
            trades=trade_count,
            breakers_active=circuit_breakers_active,
        )

    async def job_optimization_cycle(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Run the strategy self-optimization cycle and notify on completion.

        Scheduled according to ``retrain.interval_days`` in config.  Skips
        silently if no optimizer is available or if the optimizer circuit
        breaker is active.
        """
        if self._optimizer is None:
            self._log.debug("optimizer_not_available")
            return

        if self._optimizer.is_paused:
            msg = (
                "⚠️ *Optimization Cycle Skipped*\n\n"
                "The optimizer has reached its maximum consecutive failure "
                "threshold. Please investigate and reset manually."
            )
            await self._send_if_configured(context, msg)
            self._log.warning("optimizer_paused_skipping")
            return

        self._log.info("optimization_cycle_starting")

        try:
            run = await self._optimizer.run_cycle(trigger="scheduled")

            status_emoji = {
                "completed": "✅",
                "skipped": "⏭️",
                "failed": "❌",
                "running": "🔄",
            }.get(run.status, "❓")

            msg_lines = [
                f"{status_emoji} *Optimization Cycle*",
                f"*Status:* {run.status}",
                f"*Trigger:* {run.trigger}",
            ]

            if run.decisions_analyzed > 0:
                msg_lines.append(f"*Decisions Analyzed:* {run.decisions_analyzed}")
            if run.trades_analyzed > 0:
                msg_lines.append(f"*Trades Analyzed:* {run.trades_analyzed}")
            if run.recommendations_count > 0:
                msg_lines.append(
                    f"*Recommendations:* {run.recommendations_count} "
                    f"({run.applied_count} applied)"
                )
            if run.error_message:
                msg_lines.append(f"*Error:* {run.error_message[:200]}")

            msg = "\n".join(msg_lines)
            await self._send_if_configured(context, msg)

            self._log.info(
                "optimization_cycle_completed",
                status=run.status,
                recommendations=run.recommendations_count,
                applied=run.applied_count,
            )

        except Exception as exc:
            self._log.exception("optimization_cycle_error", error=str(exc))
            msg = (
                "❌ *Optimization Cycle Failed*\n\n"
                f"An unexpected error occurred:\n`{str(exc)[:300]}`"
            )
            await self._send_if_configured(context, msg)

    async def job_funding_rate_countdown(
        self, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Check funding rates and alert if any exceed the max threshold.

        Runs every 30 minutes.  Alerts if funding rate exceeds the configured
        ``risk.max_funding_rate_cost`` threshold.
        """
        if self._market_data_engine is None:
            return

        try:
            max_rate = float(self._config["risk"]["max_funding_rate_cost"])
            symbols = self._config["trading"]["underlyings"]

            alerts: list[str] = []
            for sym in symbols:
                fr = await self._market_data_engine.get_funding_rate(sym)
                if fr is None:
                    continue

                rate_pct = float(fr.funding_rate) * 100
                if abs(float(fr.funding_rate)) > max_rate:
                    now_ms = int(_time.time() * 1000)
                    secs_remaining = max(0, (fr.next_funding_time - now_ms) // 1000)
                    mins, secs = divmod(secs_remaining, 60)
                    alerts.append(
                        f"• `{sym}`: {rate_pct:+.5f}% (next funding in ~{mins}m {secs}s)"
                    )

            if not alerts:
                return  # Silent — no alert needed

            msg = "⚠️ *High Funding Alert*\n\n" + "\n".join(alerts)
            sent = await self._send_if_configured(context, msg)
            self._log.warning(
                "job_funding_rate_alert",
                sent=sent,
                alerts=len(alerts),
            )

        except Exception as exc:
            self._log.warning("job_funding_rate_alert_error", error=str(exc))

    async def job_liquidation_warning(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Check all positions for proximity to liquidation.

        Runs every 5 minutes.  Alerts if any position's distance to
        liquidation is below the configured threshold.
        """
        if self._orchestrator is None:
            return

        try:
            exchange_adapter = getattr(self._orchestrator, "_exchange_adapter", None)
            if exchange_adapter is None:
                return

            positions = await exchange_adapter.get_positions()
            if not positions:
                return

            warnings: list[str] = []
            for pos in positions:
                mark = float(
                    getattr(pos, "mark_price", getattr(pos, "current_price", 0))
                )
                liq = float(getattr(pos, "liquidation_price", 0))
                if mark <= 0 or liq <= 0:
                    continue

                distance = abs(mark - liq) / mark
                min_distance = float(
                    effective_min_liquidation_distance(
                        self._config["risk"],
                        getattr(pos, "leverage", None),
                    )
                )
                if distance < min_distance:
                    symbol = getattr(
                        pos, "symbol", getattr(pos, "contract_symbol", "?")
                    )
                    raw_side = getattr(pos, "position_side", getattr(pos, "side", "?"))
                    side = str(raw_side) if not isinstance(raw_side, str) else raw_side
                    lev = int(getattr(pos, "leverage", 1))

                    warnings.append(
                        f"• `{symbol}` {side}: {distance:.1%} from liquidation\n"
                        f"  Leverage: {lev}x | Liq: ${liq:,.2f} | Mark: ${mark:,.2f}"
                    )

            if not warnings:
                return

            msg = "🚨 *Liquidation Warning*\n\n" + "\n\n".join(warnings)
            sent = await self._send_if_configured(context, msg)
            self._log.warning(
                "job_liquidation_warning",
                sent=sent,
                warnings=len(warnings),
            )

        except Exception as exc:
            self._log.warning("job_liquidation_warning_error", error=str(exc))

    async def job_funding_cost_report(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Send a daily report of cumulative funding costs for open positions.

        Scheduled at 22:00 UTC (end of trading day).
        """
        if self._orchestrator is None:
            return

        try:
            exchange_adapter = getattr(self._orchestrator, "_exchange_adapter", None)
            if exchange_adapter is None:
                return

            positions = await exchange_adapter.get_positions()
            if not positions:
                return

            total_cost = 0.0
            lines: list[str] = []
            for pos in positions:
                funding_paid = float(getattr(pos, "funding_paid", 0))
                symbol = getattr(pos, "symbol", getattr(pos, "contract_symbol", "?"))

                if funding_paid != 0:
                    cost_str = (
                        f"-${abs(funding_paid):.2f} paid"
                        if funding_paid < 0
                        else f"+${funding_paid:.2f} received"
                    )
                    lines.append(f"• `{symbol}`: {cost_str}")
                    total_cost += funding_paid

            if not lines:
                return

            total_emoji = "🔴" if total_cost < 0 else "🟢"
            lines.append(f"\n*Total:* {total_emoji} ${total_cost:+,.2f}")

            msg = "💰 *Daily Funding Cost Report*\n\n" + "\n".join(lines)
            sent = await self._send_if_configured(context, msg)
            self._log.info(
                "job_funding_cost_report",
                sent=sent,
                total_cost=total_cost,
            )

        except Exception as exc:
            self._log.warning("job_funding_cost_report_error", error=str(exc))
