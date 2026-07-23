"""Telegram command handlers for QuadBot.

Each command handler is a method on ``QuadBotCommands``.  Handlers are
kept short — they delegate data queries to the respective subsystem and
format the response as Telegram markdown messages.

Admin verification
------------------
Every command checks ``update.effective_user.id`` against the configured
``admin_ids`` list.  Non-admin users receive an "Unauthorized" response.
"""

from __future__ import annotations

import time as _time
from decimal import Decimal
from typing import Any

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Conversation states for /execute
# ---------------------------------------------------------------------------

SELECTING_STRATEGY, CONFIRMING_EXECUTION = range(2)

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

logger = structlog.get_logger(__name__)


# ============================================================================
# QuadBotCommands
# ============================================================================


class QuadBotCommands:
    """Container for all Telegram bot command handlers.

    Parameters
    ----------
    shared_state:
        Dict carrying component references (orchestrator, risk_manager, etc.)
        and configuration shared between command and job handlers.
    """

    def __init__(self, shared_state: dict[str, Any]) -> None:
        self._log = logger.bind()
        self._state = shared_state
        self._config: dict[str, Any] = shared_state.get("config", {})
        self._telegram_config: dict[str, Any] = shared_state.get("telegram_config", {})
        self._admin_ids: list[int] = shared_state.get("admin_ids", [])
        self._notification_chat_id: int | None = shared_state.get(
            "notification_chat_id", None
        )

        # Subsystem references
        self._orchestrator = shared_state.get("orchestrator")
        self._risk_manager = shared_state.get("risk_manager")
        self._execution_engine = shared_state.get("execution_engine")
        self._market_data_engine = shared_state.get("market_data_engine")
        self._db_manager = shared_state.get("db_manager")
        self._groq_client = shared_state.get("groq_client")

    # ------------------------------------------------------------------
    # Admin guard
    # ------------------------------------------------------------------

    def _is_admin(self, update: Update) -> bool:
        """Check if the sending user is an approved admin.

        Returns ``True`` if no admin IDs are configured (open access), or
        if the user's ID is in the admin list.
        """
        if not self._admin_ids:
            return True
        user_id = update.effective_user.id if update.effective_user else None
        return user_id in self._admin_ids

    async def _check_admin(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> bool:
        """Send an "Unauthorized" message if the user is not an admin.

        Returns ``True`` if the user is authorised.
        """
        if self._is_admin(update):
            return True
        self._log.warning(
            "unauthorized_access",
            user=update.effective_user.id if update.effective_user else None,
        )
        if update.effective_chat:
            await update.effective_chat.send_message(
                "⛔ Unauthorized. You are not in the admin whitelist."
            )
        return False

    # ------------------------------------------------------------------
    # Simple command handlers
    # ------------------------------------------------------------------

    async def cmd_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Send a welcome message with available commands."""
        if not await self._check_admin(update, context):
            return

        self._log.info("cmd_start", user=update.effective_user.id)

        msg = (
            "🤖 *Quad Options Trading Bot*\n\n"
            "Your personal automated options trading assistant for Binance Options.\n\n"
            "*Available commands:*\n"
            "• `/status` — Bot health, position summary, PnL, risk status\n"
            "• `/balance` — Account balances, total USDT value\n"
            "• `/positions` — List open positions with PnL\n"
            "• `/orders` — List open or pending orders\n"
            "• `/chain BTC|ETH` — Show option chain for an underlying\n"
            "• `/strategies` — List active strategies and their status\n"
            "• `/execute` — Execute a strategy signal (with confirmation)\n"
            "• `/risk` — Risk status, circuit breakers, exposure report\n"
            "• `/kill` — Emergency kill switch activation (requires confirmation)\n"
            "• `/cancel <id>` — Cancel an order by its ID\n"
            "• `/settings` — Current configuration overview\n"
            "• `/analyze` — AI analysis of current market conditions\n"
            "• `/ai_strategy` — Groq AI recommends a strategy\n"
            "• `/ai_status` — AI trading system status and metrics\n"
            "• `/ai_decision` — Request an AI-driven trading decision\n"
            "• `/help` — Full command reference"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def cmd_help(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Send the full command reference."""
        if not await self._check_admin(update, context):
            return

        self._log.info("cmd_help", user=update.effective_user.id)

        msg = (
            "📚 *Quad Bot Command Reference*\n\n"
            "*Monitoring:*\n"
            "• `/status` — Show bot health, position count, daily PnL, circuit breakers, active strategies\n"
            "• `/balance` — Show all account balances with total USDT portfolio value\n"
            "• `/risk` — Risk gates status, circuit breaker status, exposure report\n\n"
            "*Trading:*\n"
            "• `/positions` — Table of open positions with current PnL (green/red emoji)\n"
            "• `/orders` — Table of pending / open orders\n"
            "• `/chain BTC` or `/chain ETH` — Show top option chain rows for the underlying\n"
            "• `/cancel <order_id>` — Cancel an order by its exchange or client order ID\n\n"
            "*Strategy:*\n"
            "• `/strategies` — List all registered strategies, their parameters, and last signal\n"
            "• `/execute` — Interactive flow to select a strategy and execute its signal\n\n"
            "*Safety:*\n"
            "• `/kill` — Emergency kill switch. Requires confirmation. Cancels all open orders.\n"
            "• `/settings` — Current configuration overview key values\n\n"
            "*General:*\n"
            "• `/start` — Welcome screen\n"
            "• `/help` — This reference\n\n"
            "*AI-Powered:*\n"
            "• `/analyze` — Groq AI analyses current market conditions (option chain, Greeks, IV)\n"
            "• `/ai_strategy` — Groq AI recommends a strategy based on market regime\n"
            "• `/ai_status` — AI trading system status, rate limiter, recent decisions\n"
            "• `/ai_decision` — Trigger a full AI trading decision cycle (ENTER/EXIT/HOLD)"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def cmd_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Send bot status, position summary, PnL, and risk status."""
        if not await self._check_admin(update, context):
            return

        self._log.info("cmd_status", user=update.effective_user.id)

        try:
            # Gather status information from subsystems
            position_count = 0
            daily_pnl = Decimal("0")
            circuit_breakers_active = 0
            active_strategies: list[str] = []

            # Get risk status
            risk_status = None
            if self._risk_manager is not None:
                try:
                    risk_status = await self._risk_manager.get_status()
                    circuit_breakers_active = sum(
                        1 for cb in risk_status.circuit_breakers.values() if cb.active
                    )
                    daily_pnl = risk_status.daily_pnl
                except Exception as exc:
                    self._log.warning("status_risk_error", error=str(exc))

            # Get position count
            if self._orchestrator is not None:
                try:
                    positions = getattr(self._orchestrator, "positions", [])
                    if callable(positions):
                        positions = positions()
                    position_count = len(positions) if isinstance(positions, list) else 0
                except Exception as exc:
                    self._log.warning("status_positions_error", error=str(exc))

            # Get active strategies
            if self._orchestrator is not None:
                try:
                    strat_list = getattr(self._orchestrator, "get_active_strategies", None)
                    if strat_list is not None:
                        strategies = strat_list()
                        active_strategies = (
                            [s.get_name() if hasattr(s, "get_name") else str(s) for s in strategies]
                            if isinstance(strategies, list)
                            else []
                        )
                except Exception as exc:
                    self._log.warning("status_strategies_error", error=str(exc))

            # Get execution stats
            exec_stats = {}
            if self._execution_engine is not None:
                try:
                    exec_stats = self._execution_engine.get_stats()
                except Exception as exc:
                    self._log.warning("status_exec_stats_error", error=str(exc))

            # Format the status message
            pnl_emoji = "🟢" if daily_pnl >= 0 else "🔴"
            cb_emoji = "⚠️" if circuit_breakers_active > 0 else "✅"

            msg = (
                f"📊 *Bot Status*\n\n"
                f"*Positions:* {position_count} open\n"
                f"*Daily PnL:* {pnl_emoji} ${float(daily_pnl):,.2f}\n"
                f"*Circuit Breakers:* {cb_emoji} {circuit_breakers_active} active\n"
                f"*Active Strategies:* {', '.join(active_strategies) if active_strategies else 'None'}\n"
                f"*Orders Submitted:* {exec_stats.get('total_submitted', 0)}\n"
                f"*Orders Filled:* {exec_stats.get('total_filled', 0)}\n"
                f"*Orders Rejected:* {exec_stats.get('total_rejected', 0)}"
            )
            await update.message.reply_text(msg, parse_mode="Markdown")

        except Exception as exc:
            self._log.exception("cmd_status_error", error=str(exc))
            await update.message.reply_text(f"⚠️ Error fetching status: {exc}")

    async def cmd_balance(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Send account balances and total USDT value."""
        if not await self._check_admin(update, context):
            return

        self._log.info("cmd_balance", user=update.effective_user.id)

        try:
            account = None
            if self._orchestrator is not None:
                try:
                    account = getattr(self._orchestrator, "account", None)
                    if callable(account):
                        account = account()
                except Exception as exc:
                    self._log.warning("balance_orchestrator_error", error=str(exc))

            if account is None and self._risk_manager is not None:
                pass  # RiskManager does not hold account data

            if account is None:
                # No account data available
                msg = (
                    "💰 *Account Balance*\n\n"
                    "No account data available. The bot may not be connected to the exchange."
                )
                await update.message.reply_text(msg, parse_mode="Markdown")
                return

            # Format balance info
            exchange = getattr(account, "exchange", "unknown")
            total_usdt = getattr(account, "total_usdt", Decimal("0"))
            balances = getattr(account, "balances", {})

            lines = [f"💳 *Account Balance*  |  Exchange: {exchange}\n"]
            lines.append(f"```\n{'Asset':<10} {'Free':>14} {'Locked':>14} {'Total':>14}")
            lines.append("-" * 54)

            for asset, bal in sorted(balances.items()):
                free = float(bal.free) if hasattr(bal, "free") else 0.0
                locked = float(bal.locked) if hasattr(bal, "locked") else 0.0
                total = free + locked
                lines.append(
                    f"{asset:<10} {free:>14.4f} {locked:>14.4f} {total:>14.4f}"
                )

            lines.append("```")
            lines.append(f"\n*Total Portfolio Value:* ${float(total_usdt):,.2f}")

            msg = "\n".join(lines)
            await update.message.reply_text(msg, parse_mode="Markdown")

        except Exception as exc:
            self._log.exception("cmd_balance_error", error=str(exc))
            await update.message.reply_text(f"⚠️ Error fetching balance: {exc}")

    async def cmd_positions(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """List open positions with PnL."""
        if not await self._check_admin(update, context):
            return

        self._log.info("cmd_positions", user=update.effective_user.id)

        try:
            positions: list[Any] = []
            if self._orchestrator is not None:
                try:
                    pos_data = getattr(self._orchestrator, "positions", None)
                    if callable(pos_data):
                        pos_data = pos_data()
                    positions = list(pos_data) if pos_data else []
                except Exception as exc:
                    self._log.warning("positions_orchestrator_error", error=str(exc))

            if not positions:
                msg = "📋 *Open Positions*\n\nNo open positions."
                await update.message.reply_text(msg, parse_mode="Markdown")
                return

            lines = ["📋 *Open Positions*\n"]
            lines.append(
                "```\n"
                f"{'Symbol':<24} {'Side':<6} {'Qty':>6} {'Entry':>10} {'Current':>10} {'PnL':>10} {'DTE':>4}"
            )
            lines.append("-" * 74)

            for pos in positions[:15]:  # Limit to 15 positions for readability
                symbol = getattr(pos, "contract_symbol", getattr(pos, "symbol", "?"))
                side = getattr(pos, "side", "?")
                qty = float(getattr(pos, "quantity", 0))
                entry = float(getattr(pos, "entry_price", 0))
                current = float(getattr(pos, "current_price", 0))
                pnl = float(getattr(pos, "unrealized_pnl", 0))
                dte = getattr(pos, "days_to_expiry", 0)

                pnl_str = f"{pnl:>+,.2f}"
                lines.append(
                    f"{symbol:<24} {side:<6} {qty:>6.2f} {entry:>10.4f} {current:>10.4f} "
                    f"{pnl_str:>10} {dte:>4}"
                )

            lines.append("```")

            # Summary
            total_pnl = sum(float(getattr(p, "unrealized_pnl", 0)) for p in positions)
            pnl_emoji = "🟢" if total_pnl >= 0 else "🔴"
            lines.append(f"\n*Total Unrealized PnL:* {pnl_emoji} ${total_pnl:+,.2f}")

            msg = "\n".join(lines)
            await update.message.reply_text(msg, parse_mode="Markdown")

        except Exception as exc:
            self._log.exception("cmd_positions_error", error=str(exc))
            await update.message.reply_text(f"⚠️ Error fetching positions: {exc}")

    async def cmd_orders(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """List open or pending orders."""
        if not await self._check_admin(update, context):
            return

        self._log.info("cmd_orders", user=update.effective_user.id)

        try:
            orders: list[Any] = []
            if self._execution_engine is not None:
                try:
                    orders = self._execution_engine.get_active_orders()
                except Exception as exc:
                    self._log.warning("orders_exec_error", error=str(exc))

            if not orders:
                msg = "📋 *Open Orders*\n\nNo open orders."
                await update.message.reply_text(msg, parse_mode="Markdown")
                return

            lines = ["📋 *Open Orders*\n"]
            lines.append(
                "```\n"
                f"{'ID':<8} {'Symbol':<20} {'Side':<5} {'Type':<8} {'Qty':>8} {'Price':>10} {'Status':<12}"
            )
            lines.append("-" * 75)

            for order in orders[:20]:
                oid = str(getattr(order, "id", "?"))
                symbol = getattr(order, "symbol", "?")
                side = getattr(order, "side", "?")
                otype = getattr(order, "type", "?")
                qty = float(getattr(order, "quantity", 0))
                price = float(getattr(order, "price", 0) or 0)
                status = getattr(order, "status", "?")

                lines.append(
                    f"{oid:<8} {symbol:<20} {side:<5} {otype:<8} {qty:>8.2f} "
                    f"{price:>10.4f} {status:<12}"
                )

            lines.append("```")
            msg = "\n".join(lines)
            await update.message.reply_text(msg, parse_mode="Markdown")

        except Exception as exc:
            self._log.exception("cmd_orders_error", error=str(exc))
            await update.message.reply_text(f"⚠️ Error fetching orders: {exc}")

    async def cmd_chain(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show option chain for an underlying.

        Usage: ``/chain BTC`` or ``/chain ETH``.

        Args are taken from ``context.args`` (text after the command).
        """
        if not await self._check_admin(update, context):
            return

        self._log.info("cmd_chain", user=update.effective_user.id)

        # Parse underlying from args
        if not context.args:
            msg = (
                "⚠️ Please specify an underlying asset.\n"
                "Usage: `/chain BTC` or `/chain ETH`"
            )
            await update.message.reply_text(msg, parse_mode="Markdown")
            return

        underlying_raw = context.args[0].upper()
        # Normalize: BTC -> BTCUSDT, ETH -> ETHUSDT
        if underlying_raw in ("BTC", "ETH"):
            underlying = f"{underlying_raw}USDT"
        else:
            underlying = underlying_raw

        try:
            if self._market_data_engine is None:
                msg = "⚠️ Market data engine is not available."
                await update.message.reply_text(msg, parse_mode="Markdown")
                return

            chain = await self._market_data_engine.get_option_chain(underlying)

            if not chain:
                msg = f"📋 *Option Chain: {underlying}*\n\nNo option data available."
                await update.message.reply_text(msg, parse_mode="Markdown")
                return

            # Truncate to top 20 for readability
            top = chain[:20]

            lines = [f"📋 *Option Chain: {underlying}*  (showing {len(top)}/{len(chain)})"]
            lines.append("\n")
            lines.append(
                "```\n"
                f"{'Strike':<10} {'Type':<5} {'Bid':>10} {'Ask':>10} {'IV':>8} {'Delta':>8} {'DTE':>4}"
            )
            lines.append("-" * 59)

            for contract in top:
                strike = float(getattr(contract, "strike", 0))
                opt_type = getattr(contract, "option_type", "?")
                bid = float(getattr(contract, "bid", 0) or 0)
                ask = float(getattr(contract, "ask", 0) or 0)
                iv = float(getattr(contract, "implied_volatility", 0))
                delta = float(getattr(contract, "delta", 0))
                # Estimate DTE from expiry
                expiry = getattr(contract, "expiry", 0)
                now_ms = int(_time.time() * 1000)
                dte = max(0, (expiry - now_ms) // 86400000)

                lines.append(
                    f"{strike:<10.2f} {opt_type:<5} {bid:>10.4f} {ask:>10.4f} "
                    f"{iv:>7.2%} {delta:>+7.4f} {dte:>4}"
                )

            lines.append("```")
            msg = "\n".join(lines)
            await update.message.reply_text(msg, parse_mode="Markdown")

        except Exception as exc:
            self._log.exception("cmd_chain_error", error=str(exc))
            await update.message.reply_text(f"⚠️ Error fetching option chain: {exc}")

    async def cmd_strategies(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """List active strategies and their status."""
        if not await self._check_admin(update, context):
            return

        self._log.info("cmd_strategies", user=update.effective_user.id)

        try:
            # Import here to avoid circular import at module level
            from quad.strategy.base import StrategyRegistry

            registered = StrategyRegistry.list()

            if not registered:
                msg = "📋 *Active Strategies*\n\nNo strategies are registered."
                await update.message.reply_text(msg, parse_mode="Markdown")
                return

            lines = ["📋 *Registered Strategies*\n"]

            for name in registered:
                cls = StrategyRegistry.get(name)
                if cls is None:
                    continue
                desc = cls.get_description()
                params = cls.get_params_spec()

                param_lines = []
                for p in params:
                    default_str = f" (default: {p.default})" if p.default is not None else ""
                    param_lines.append(f"  • `{p.name}`: {p.description}{default_str}")

                lines.append(f"*{name}*\n{desc}")
                if param_lines:
                    lines.extend(param_lines)
                lines.append("")

            msg = "\n".join(lines)
            await update.message.reply_text(msg, parse_mode="Markdown")

        except Exception as exc:
            self._log.exception("cmd_strategies_error", error=str(exc))
            await update.message.reply_text(f"⚠️ Error listing strategies: {exc}")

    async def cmd_kill(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Emergency kill switch activation.

        Requires a confirmation via inline keyboard.
        """
        if not await self._check_admin(update, context):
            return

        self._log.info("cmd_kill", user=update.effective_user.id)

        keyboard = [
            [
                InlineKeyboardButton("🚨 Yes, Kill All", callback_data="kill_confirm"),
                InlineKeyboardButton("Cancel", callback_data="kill_cancel"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        msg = (
            "🚨 *Kill Switch*\n\n"
            "Are you sure you want to activate the emergency kill switch?\n\n"
            "This will:\n"
            "• Cancel all open orders\n"
            "• Place no new trades\n"
            "• Not close existing positions (manual action required)\n\n"
            "*This action cannot be undone via Telegram.*"
        )
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=reply_markup)

    async def cmd_kill_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle kill switch confirmation callback."""
        query = update.callback_query
        if query is None:
            return
        await query.answer()

        if query.data == "kill_confirm":
            reason = "Kill switch triggered via Telegram by admin"
            try:
                if self._risk_manager is not None:
                    self._risk_manager.trigger_kill_switch(reason)
                elif self._orchestrator is not None:
                    ks = getattr(self._orchestrator, "trigger_kill_switch", None)
                    if ks is not None:
                        ks(reason)

                await query.edit_message_text(
                    "🚨 *Kill Switch Activated*\n\n"
                    "All trading has been stopped. Open orders have been cancelled.\n"
                    "Existing positions remain open — manage them manually.",
                    parse_mode="Markdown",
                )
                self._log.warning("kill_switch_activated_via_telegram", user=update.effective_user.id)

            except Exception as exc:
                self._log.exception("kill_switch_error", error=str(exc))
                await query.edit_message_text(f"⚠️ Error activating kill switch: {exc}")

        else:
            await query.edit_message_text("✅ Kill switch cancelled.")

    async def cmd_risk(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Send risk status, circuit breakers, and exposure report."""
        if not await self._check_admin(update, context):
            return

        self._log.info("cmd_risk", user=update.effective_user.id)

        try:
            if self._risk_manager is None:
                msg = "⚠️ Risk manager is not available."
                await update.message.reply_text(msg, parse_mode="Markdown")
                return

            risk_status = await self._risk_manager.get_status()

            # Gates
            gate_lines = []
            for gate_name, passed in risk_status.gates.items():
                emoji = "✅" if passed else "❌"
                gate_lines.append(f"  {emoji} `{gate_name}`")

            # Circuit breakers
            cb_lines = []
            for cb_name, cb in risk_status.circuit_breakers.items():
                emoji = "🔴" if cb.active else "🟢"
                reason = f" — {cb.reason}" if cb.reason else ""
                cb_lines.append(f"  {emoji} `{cb_name}`{reason}")

            # Exposure report
            exposure_lines = []
            try:
                exposure = self._risk_manager.get_exposure_report()
                for key, val in exposure.items():
                    exposure_lines.append(f"  • `{key}`: {val}")
            except Exception as exc:
                self._log.warning("exposure_report_error", error=str(exc))
                exposure_lines.append("  (not available)")

            msg = (
                "⚠️ *Risk Status*\n\n"
                f"*Drawdown:* {float(risk_status.drawdown_percent):.2%}\n"
                f"*Daily PnL:* ${float(risk_status.daily_pnl):,.2f} / ${float(risk_status.daily_loss_limit):,.2f}\n\n"
                f"*Gates:*\n" + "\n".join(gate_lines) + "\n\n"
                f"*Circuit Breakers:*\n" + "\n".join(cb_lines) + "\n\n"
                f"*Exposure:*\n" + "\n".join(exposure_lines)
            )
            await update.message.reply_text(msg, parse_mode="Markdown")

        except Exception as exc:
            self._log.exception("cmd_risk_error", error=str(exc))
            await update.message.reply_text(f"⚠️ Error fetching risk status: {exc}")

    async def cmd_cancel(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Cancel an order by its ID.

        Usage: ``/cancel <order_id>``
        """
        if not await self._check_admin(update, context):
            return

        self._log.info("cmd_cancel", user=update.effective_user.id)

        if not context.args:
            msg = "⚠️ Usage: `/cancel <order_id>`"
            await update.message.reply_text(msg, parse_mode="Markdown")
            return

        order_id = context.args[0]

        try:
            if self._execution_engine is None:
                msg = "⚠️ Execution engine is not available."
                await update.message.reply_text(msg, parse_mode="Markdown")
                return

            success = await self._execution_engine.cancel_order(order_id)
            if success:
                msg = f"✅ Order `{order_id}` cancelled successfully."
            else:
                msg = f"⚠️ Could not cancel order `{order_id}`. It may already be filled or cancelled."
            await update.message.reply_text(msg, parse_mode="Markdown")

        except Exception as exc:
            self._log.exception("cmd_cancel_error", error=str(exc))
            await update.message.reply_text(f"⚠️ Error cancelling order: {exc}")

    async def cmd_settings(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show current key configuration overview."""
        if not await self._check_admin(update, context):
            return

        self._log.info("cmd_settings", user=update.effective_user.id)

        try:
            config = self._config

            # Extract key settings safely
            mode = config.get("_mode", "paper")
            dry_run = config.get("_dry_run", True)
            exchange_name = config.get("exchange", {}).get("name", "binance")
            testnet = config.get("exchange", {}).get("testnet", True)
            default_strategy = config.get("trading", {}).get("default_strategy", "cash_secured_put")
            max_positions = config.get("risk", {}).get("max_positions", 5)
            max_position_size = config.get("risk", {}).get("max_position_size_pct", 0.1)
            daily_loss = config.get("risk", {}).get("max_daily_loss_usd", 500)

            msg = (
                "⚙️ *Current Settings*\n\n"
                f"*Mode:* `{mode}`\n"
                f"*Dry Run:* `{dry_run}`\n"
                f"*Exchange:* `{exchange_name}`\n"
                f"*Testnet:* `{testnet}`\n"
                f"*Default Strategy:* `{default_strategy}`\n"
                f"*Max Positions:* `{max_positions}`\n"
                f"*Max Position Size:* `{float(max_position_size):.0%}`\n"
                f"*Daily Loss Limit:* `${daily_loss}`\n\n"
                f"*Admin IDs:* `{self._admin_ids}`" if self._admin_ids else ""
            )
            await update.message.reply_text(msg, parse_mode="Markdown")

        except Exception as exc:
            self._log.exception("cmd_settings_error", error=str(exc))
            await update.message.reply_text(f"⚠️ Error fetching settings: {exc}")

    # ------------------------------------------------------------------
    # AI-powered commands
    # ------------------------------------------------------------------

    async def cmd_analyze(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Send AI-generated market analysis for configured underlyings."""
        if not await self._check_admin(update, context):
            return

        self._log.info("cmd_analyze", user=update.effective_user.id)

        if self._groq_client is None:
            msg = (
                "⚠️ AI analysis is not available.\n\n"
                "The Groq API key is not configured. Set `GROQ_API_KEY` "
                "in your `.env` file and restart the bot."
            )
            await update.message.reply_text(msg, parse_mode="Markdown")
            return

        if self._market_data_engine is None:
            msg = "⚠️ Market data engine is not available."
            await update.message.reply_text(msg, parse_mode="Markdown")
            return

        # Send initial "thinking" message
        status_msg = await update.message.reply_text(
            "🤔 Analysing market data...",
            parse_mode="Markdown",
        )

        try:
            # Gather market data for configured underlyings
            from quad.ai import analyze_market

            underlyings = ["BTCUSDT", "ETHUSDT"]
            config = self._config
            configured = config.get("trading", {}).get("underlyings", None)
            if configured:
                underlyings = list(configured)

            results: list[str] = []
            for underlying in underlyings:
                try:
                    chain = await self._market_data_engine.get_option_chain(underlying)
                    analysis = await analyze_market(
                        client=self._groq_client,
                        underlying=underlying,
                        underlying_price=None,
                        option_chain=chain,
                        positions=None,
                    )
                    results.append(f"*{underlying}*\n{analysis}")
                except Exception as exc:
                    self._log.warning(
                        "cmd_analyze_fetch_error",
                        underlying=underlying,
                        error=str(exc),
                    )
                    results.append(f"*{underlying}*\n_Data unavailable._")

            msg_text = "🧠 *AI Market Analysis*\n\n" + "\n\n".join(results)
            await status_msg.edit_text(msg_text, parse_mode="Markdown")

        except Exception as exc:
            self._log.exception("cmd_analyze_error", error=str(exc))
            await status_msg.edit_text(
                f"⚠️ Analysis error: {exc}",
                parse_mode="Markdown",
            )

    async def cmd_ai_strategy(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Ask Groq AI to recommend a strategy based on market conditions."""
        if not await self._check_admin(update, context):
            return

        self._log.info("cmd_ai_strategy", user=update.effective_user.id)

        if self._groq_client is None:
            msg = (
                "⚠️ AI strategy recommendation is not available.\n\n"
                "The Groq API key is not configured. Set `GROQ_API_KEY` "
                "in your `.env` file and restart the bot."
            )
            await update.message.reply_text(msg, parse_mode="Markdown")
            return

        if self._market_data_engine is None:
            msg = "⚠️ Market data engine is not available."
            await update.message.reply_text(msg, parse_mode="Markdown")
            return

        status_msg = await update.message.reply_text(
            "🤔 Consulting Groq AI on strategy selection...",
            parse_mode="Markdown",
        )

        try:
            from quad.ai import recommend_strategy

            # Get data for the first configured underlying
            config = self._config
            underlyings = config.get("trading", {}).get("underlyings", ["BTCUSDT"])
            underlying = list(underlyings)[0] if underlyings else "BTCUSDT"

            chain = await self._market_data_engine.get_option_chain(underlying)

            from quad.ai.analysis import _summarise_chain

            chain_summary = _summarise_chain(chain, None)

            recommendation = await recommend_strategy(
                client=self._groq_client,
                underlying=underlying,
                underlying_price=None,
                iv_percentile=None,
                trend_description=None,
                option_chain_summary=chain_summary,
            )

            msg_text = (
                f"🎯 *AI Strategy Recommendation*\n\n"
                f"Based on current {underlying} market conditions:\n\n"
                f"{recommendation}"
            )
            await status_msg.edit_text(msg_text, parse_mode="Markdown")

        except Exception as exc:
            self._log.exception("cmd_ai_strategy_error", error=str(exc))
            await status_msg.edit_text(
                f"⚠️ Strategy recommendation error: {exc}",
                parse_mode="Markdown",
            )

    async def cmd_ai_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show AI trading system status and metrics."""
        if not await self._check_admin(update, context):
            return

        self._log.info("cmd_ai_status", user=update.effective_user.id)

        if self._groq_client is None:
            msg = (
                "⚠️ AI trading system is not available.\n\n"
                "The Groq API key is not configured. Set `GROQ_API_KEY` "
                "in your `.env` file and restart the bot."
            )
            await update.message.reply_text(msg, parse_mode="Markdown")
            return

        try:
            stats = self._groq_client.stats
            orchestrator = self._orchestrator

            # Gather orchestrator AI info if available
            ai_info = {}
            if orchestrator is not None:
                status_dict = orchestrator.status() if hasattr(orchestrator, "status") else {}
                ai_info = status_dict.get("ai", {})

            requests_window = stats.get("requests_in_window", 0)
            max_req = stats.get("max_requests_per_day", 950)
            pct_used = round(requests_window / max_req * 100, 1) if max_req > 0 else 0

            usage_bar = self._build_usage_bar(requests_window, max_req)

            msg = (
                "🧠 *AI Trading System Status*\n\n"
                f"*Status:* {'Available' if stats.get('available') else 'Unavailable'}\n"
                f"*Model:* `{stats.get('model', '?')}`\n"
                f"*API Key:* {'Configured' if self._groq_client._api_key else 'Missing'}\n\n"
                f"*Rate Limiter:*\n"
                f"  {usage_bar}\n"
                f"  Requests today: {requests_window} / {max_req} ({pct_used}%)\n"
                f"  Total requests: {stats.get('total_requests', 0)}\n"
                f"  Total retries: {stats.get('total_retries', 0)}\n"
                f"  Last rate limit: {stats.get('last_rate_limit', 0) or 'Never'}\n\n"
                f"*Recent Activity:*\n"
                f"  Cycles run: {ai_info.get('cycle_count', 0)}\n"
                f"  Cycle interval: {ai_info.get('cycle_interval_s', 3600)}s\n"
                f"  Last cycle time: {ai_info.get('last_cycle_time_ms', 0):.0f}ms\n"
                f"  Last action: `{ai_info.get('last_action', 'N/A')}`\n"
                f"  Consecutive failures: {ai_info.get('consecutive_failures', 0)}\n"
            )

            last_error = ai_info.get('last_error')
            if last_error:
                msg += f"\n*Last Error:* `{last_error[:200]}`"

            await update.message.reply_text(msg, parse_mode="Markdown")

        except Exception as exc:
            self._log.exception("cmd_ai_status_error", error=str(exc))
            await update.message.reply_text(
                f"⚠️ AI status error: {exc}", parse_mode="Markdown"
            )

    def _build_usage_bar(self, used: int, total: int, width: int = 10) -> str:
        """Build a simple text progress bar for rate limit usage."""
        if total <= 0:
            return "[" + " " * width + "]"
        filled = min(int(used / total * width), width)
        bar = "█" * filled + "░" * (width - filled)

        # Colorise with emoji
        pct = used / total if total > 0 else 0
        if pct >= 0.95:
            return f"🔴 [{bar}]"
        elif pct >= 0.80:
            return f"🟡 [{bar}]"
        else:
            return f"🟢 [{bar}]"

    async def cmd_ai_decision(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Request an AI-driven trading decision (ENTER/EXIT/HOLD)."""
        if not await self._check_admin(update, context):
            return

        self._log.info("cmd_ai_decision", user=update.effective_user.id)

        if self._groq_client is None:
            msg = (
                "⚠️ AI trading system is not available.\n\n"
                "The Groq API key is not configured. Set `GROQ_API_KEY` "
                "in your `.env` file and restart the bot."
            )
            await update.message.reply_text(msg, parse_mode="Markdown")
            return

        if not self._groq_client.is_available():
            msg = (
                "⚠️ AI rate limit reached.\n\n"
                "The daily request limit has been exhausted. "
                "The AI decision will be available after the window resets."
            )
            await update.message.reply_text(msg, parse_mode="Markdown")
            return

        if self._orchestrator is None:
            msg = "⚠️ Orchestrator is not available."
            await update.message.reply_text(msg, parse_mode="Markdown")
            return

        status_msg = await update.message.reply_text(
            "🤔 Running AI trading analysis cycle... (this may take 30-60 seconds)",
            parse_mode="Markdown",
        )

        try:
            # Use orchestrator's AI cycle infrastructure
            underlyings = self._config.get("trading", {}).get(
                "underlyings", ["BTCUSDT", "ETHUSDT"]
            )

            # Need the exchange adapter from orchestrator
            exchange_adapter = getattr(self._orchestrator, "_exchange_adapter", None)
            market_data = getattr(self._orchestrator, "_market_data", None)

            if exchange_adapter is None or market_data is None:
                await status_msg.edit_text(
                    "⚠️ Exchange adapter or market data engine not available.",
                    parse_mode="Markdown",
                )
                return

            account = await exchange_adapter.get_account()
            positions = await exchange_adapter.get_positions()

            # Run the AI cycle via orchestrator
            if hasattr(self._orchestrator, "_run_ai_trading_cycle"):
                decision = await self._orchestrator._run_ai_trading_cycle(
                    list(underlyings), account, positions
                )

                # Format the response
                action = decision.get("action", "HOLD")
                reasoning = decision.get("reasoning", "No reasoning provided")
                strategy = decision.get("strategy")
                confidence = decision.get("confidence", 0.0)
                contract = decision.get("contract")
                side = decision.get("side")
                quantity = decision.get("quantity")

                action_emoji = {
                    "ENTER": "🟢",
                    "EXIT": "🔴",
                    "HOLD": "⏸️",
                }.get(action, "❓")

                msg_parts = [
                    f"{action_emoji} *AI Trading Decision*\n",
                    f"*Action:* `{action}`",
                    f"*Confidence:* {confidence:.0%}" if confidence else "",
                    f"*Strategy:* `{strategy}`" if strategy else "",
                    f"*Contract:* `{contract}`" if contract else "",
                    f"*Side:* `{side}`" if side else "",
                    f"*Quantity:* {quantity}" if quantity else "",
                    "",
                    f"*Reasoning:*\n{reasoning[:500]}",
                ]

                msg_text = "\n".join(p for p in msg_parts if p)
                await status_msg.edit_text(msg_text, parse_mode="Markdown")

                # Execute if action is ENTER or EXIT
                if action in ("ENTER", "EXIT") and hasattr(self._orchestrator, "_execute_ai_action"):
                    from quad.types.strategy import StrategyContext

                    strategy_context = StrategyContext(
                        account=account,
                        positions=positions,
                        orders=[],
                        option_chain=[],
                        config=self._config,
                    )
                    await self._orchestrator._execute_ai_action(decision, strategy_context)

                    # Append execution notification
                    await status_msg.edit_text(
                        msg_text
                        + f"\n\n✅ {action} order submitted through risk & execution pipeline.",
                        parse_mode="Markdown",
                    )
            else:
                await status_msg.edit_text(
                    "⚠️ Orchestrator does not support `_run_ai_trading_cycle`.",
                    parse_mode="Markdown",
                )

        except Exception as exc:
            self._log.exception("cmd_ai_decision_error", error=str(exc))
            await status_msg.edit_text(
                f"⚠️ AI decision error: {exc}", parse_mode="Markdown"
            )

    # ------------------------------------------------------------------
    # Execute conversation (multi-step)
    # ------------------------------------------------------------------

    def get_execute_conversation_handler(self) -> ConversationHandler:
        """Return the ``ConversationHandler`` for the /execute flow."""

        async def execute_start(
            update: Update, context: ContextTypes.DEFAULT_TYPE
        ) -> int:
            """Start the execute flow — show strategy picker."""
            if not await self._check_admin(update, context):
                return ConversationHandler.END

            self._log.info("execute_start", user=update.effective_user.id)

            from quad.strategy.base import StrategyRegistry

            strategies = StrategyRegistry.list()
            if not strategies:
                await update.message.reply_text(
                    "⚠️ No strategies are registered.", parse_mode="Markdown"
                )
                return ConversationHandler.END

            keyboard = [
                [InlineKeyboardButton(s.replace("_", " ").title(), callback_data=f"exec_strat_{s}")]
                for s in strategies
            ]
            keyboard.append([InlineKeyboardButton("Cancel", callback_data="exec_cancel")])
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                "🎯 *Execute Strategy*\n\nSelect a strategy to execute:",
                parse_mode="Markdown",
                reply_markup=reply_markup,
            )
            return SELECTING_STRATEGY

        async def execute_strategy_selected(
            update: Update, context: ContextTypes.DEFAULT_TYPE
        ) -> int:
            """User selected a strategy — show confirmation."""
            query = update.callback_query
            if query is None:
                return ConversationHandler.END
            await query.answer()

            if query.data == "exec_cancel":
                await query.edit_message_text("✅ Execution cancelled.")
                return ConversationHandler.END

            strategy_name = query.data.replace("exec_strat_", "")
            context.user_data["execute_strategy"] = strategy_name

            from quad.strategy.base import StrategyRegistry

            cls = StrategyRegistry.get(strategy_name)
            params_info = ""
            if cls is not None:
                spec = cls.get_params_spec()
                if spec:
                    param_lines = [f"  • `{p.name}`: {p.description}" for p in spec]
                    params_info = "\n" + "\n".join(param_lines)

            keyboard = [
                [
                    InlineKeyboardButton("✅ Confirm", callback_data="exec_confirm"),
                    InlineKeyboardButton("Cancel", callback_data="exec_cancel"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            msg = (
                f"🎯 *Execute: {strategy_name}*\n"
                f"{params_info}\n\n"
                "Proceed with execution? This will evaluate the strategy "
                "against current market data and submit orders if signals are generated."
            )
            await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=reply_markup)
            return CONFIRMING_EXECUTION

        async def execute_confirm(
            update: Update, context: ContextTypes.DEFAULT_TYPE
        ) -> int:
            """Confirmed — execute via orchestrator."""
            query = update.callback_query
            if query is None:
                return ConversationHandler.END
            await query.answer()

            if query.data == "exec_cancel":
                await query.edit_message_text("✅ Execution cancelled.")
                return ConversationHandler.END

            strategy_name = context.user_data.get("execute_strategy", "unknown")

            try:
                await query.edit_message_text(
                    f"⏳ Executing `{strategy_name}`...", parse_mode="Markdown"
                )

                # Execute via orchestrator (if available)
                if self._orchestrator is not None:
                    exec_method = getattr(self._orchestrator, "execute_strategy", None)
                    if exec_method is not None:
                        result = await exec_method(strategy_name)
                        await query.edit_message_text(
                            f"✅ `{strategy_name}` executed successfully.\n\n"
                            f"Result: {result}",
                            parse_mode="Markdown",
                        )
                        self._log.info(
                            "execute_complete",
                            strategy=strategy_name,
                            user=update.effective_user.id,
                        )
                    else:
                        await query.edit_message_text(
                            f"⚠️ Orchestrator does not support `execute_strategy`.\n"
                            f"Strategy `{strategy_name}` was selected but not executed.",
                            parse_mode="Markdown",
                        )
                else:
                    await query.edit_message_text(
                        f"ℹ️ No orchestrator configured. Strategy `{strategy_name}` "
                        f"would be executed in production.",
                        parse_mode="Markdown",
                    )

            except Exception as exc:
                self._log.exception("execute_error", strategy=strategy_name, error=str(exc))
                await query.edit_message_text(f"⚠️ Execution error: {exc}")

            context.user_data.pop("execute_strategy", None)
            return ConversationHandler.END

        async def execute_cancel(
            update: Update, context: ContextTypes.DEFAULT_TYPE
        ) -> int:
            """User cancelled the execute flow."""
            query = update.callback_query
            if query is not None:
                await query.answer()
                await query.edit_message_text("✅ Execution cancelled.")
            return ConversationHandler.END

        return ConversationHandler(
            entry_points=[CommandHandler("execute", execute_start)],
            states={
                SELECTING_STRATEGY: [
                    CallbackQueryHandler(execute_strategy_selected, pattern=r"^exec_")
                ],
                CONFIRMING_EXECUTION: [
                    CallbackQueryHandler(execute_confirm, pattern=r"^(exec_confirm|exec_cancel)$")
                ],
            },
            fallbacks=[
                CallbackQueryHandler(execute_cancel, pattern=r"^exec_cancel$"),
                CommandHandler("cancel", execute_cancel),
            ],
        )

    # ------------------------------------------------------------------
    # Error handler
    # ------------------------------------------------------------------

    async def error_handler(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Log errors and notify the admin chat."""
        self._log.error(
            "bot_error",
            error=str(context.error),
            update_id=update.update_id if update else None,
        )

        # Notify admin chat if configured
        if self._notification_chat_id:
            try:
                app = context.application
                if app is not None:
                    await app.bot.send_message(
                        chat_id=self._notification_chat_id,
                        text=f"⚠️ Bot Error:\n`{context.error}`",
                        parse_mode="Markdown",
                    )
            except Exception as exc:
                self._log.warning("error_notification_failed", error=str(exc))

        # Re-raise for Application's own handling
        raise
