"""Recurring bot jobs for Quad Telegram bot.

Jobs run on a schedule via the PTB job queue.  They only send messages
if a ``notification_chat_id`` is configured.
"""

from __future__ import annotations

import time as _time
from decimal import Decimal
from typing import Any

import structlog
from telegram.ext import ContextTypes

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
        self._config: dict[str, Any] = shared_state.get("config", {})
        self._telegram_config: dict[str, Any] = shared_state.get("telegram_config", {})
        self._notification_chat_id: int | None = shared_state.get(
            "notification_chat_id", None
        )

        # Subsystem references
        self._orchestrator = shared_state.get("orchestrator")
        self._risk_manager = shared_state.get("risk_manager")

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
        daily_pnl = Decimal("0")
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
                pos_data = getattr(self._orchestrator, "positions", None)
                if callable(pos_data):
                    pos_data = pos_data()
                if isinstance(pos_data, list):
                    position_count = len(pos_data)
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
                name: cb
                for name, cb in rs.circuit_breakers.items()
                if cb.active
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
        daily_pnl = Decimal("0")
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
                pos_data = getattr(self._orchestrator, "positions", None)
                if callable(pos_data):
                    pos_data = pos_data()
                if isinstance(pos_data, list):
                    position_count = len(pos_data)
            except Exception as exc:
                self._log.warning("job_daily_positions_error", error=str(exc))

        # Get trade count from DB if available
        db = self._state.get("db_manager")
        if db and db.is_connected:
            try:
                async with db.pool.acquire() as conn:
                    trade_count = await conn.fetchval("SELECT COUNT(*) FROM trades") or 0
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
