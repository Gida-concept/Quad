"""Telegram command handlers for Quad Futures Bot.

Each command handler is a method on ``QuadBotCommands``.  Handlers are
kept short — they delegate data queries to the respective subsystem and
format the response as Telegram markdown messages.
"""

from __future__ import annotations

import html as _html
import json as _json
import re as _re
import time as _time
import warnings
from collections import defaultdict
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
from telegram.warnings import PTBUserWarning

# ---------------------------------------------------------------------------
# Suppress benign PTBUserWarning about per_message=False with CallbackQueryHandler
# in ConversationHandler. This warning is informational -- it tells you that
# with per_message=False (the default), CallbackQueryHandler handlers won't be
# tracked per-message. For our callback-only execute flow this is the correct
# behavior, so the warning is harmless.
# ---------------------------------------------------------------------------

warnings.filterwarnings(
    "ignore",
    message="If 'per_message=False'",
    category=PTBUserWarning,
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
        self._config: dict[str, Any] = shared_state["config"]
        self._telegram_config: dict[str, Any] = shared_state["telegram_config"]
        self._notification_chat_id: int | None = shared_state.get(
            "notification_chat_id"
        )

        # Subsystem references
        self._orchestrator = shared_state.get("orchestrator")
        self._risk_manager = shared_state.get("risk_manager")
        self._execution_engine = shared_state.get("execution_engine")
        self._market_data_engine = shared_state.get("market_data_engine")
        self._db_manager = shared_state.get("db_manager")
        self._groq_client = shared_state.get("groq_client")

        # Rate limiting: per-user per-command cooldown tracking
        self._last_cmd: dict[int, dict[str, float]] = defaultdict(dict)
        self._rate_limit_config: dict[str, float] = {
            # AI-powered commands (expensive): 30s cooldown
            "analyze": 30.0,
            "ai_strategy": 30.0,
            "ai_decision": 30.0,
            # Status / data commands: 5s cooldown
            "status": 5.0,
            "balance": 5.0,
            "positions": 5.0,
            "orders": 5.0,
            "funding_rate": 5.0,
            "book": 5.0,
            "market_regime": 5.0,
            "liquidation_warnings": 5.0,
            "strategies": 5.0,
            "risk": 5.0,
            "settings": 5.0,
            # Safety commands: 2s cooldown
            "start": 2.0,
            "help": 2.0,
            "leverage": 2.0,
            "position_mode": 2.0,
            "set": 2.0,
            "ai_status": 2.0,
            # Critical safety commands: no cooldown
            "kill": 0.0,
            "cancel": 0.0,
        }
        # Default cooldown for commands not listed
        self._default_cooldown = 2.0

    def _check_rate_limit(self, user_id: int, cmd: str) -> float | None:
        """Check if *cmd* is rate-limited for *user_id*.

        Returns ``None`` if the command passes, or the remaining cooldown
        in seconds if the user must wait.
        """
        now = _time.time()
        cooldown = self._rate_limit_config.get(cmd, self._default_cooldown)
        if cooldown <= 0:
            return None
        last = self._last_cmd[user_id].get(cmd, 0.0)
        elapsed = now - last
        if elapsed < cooldown:
            return round(cooldown - elapsed, 1)
        self._last_cmd[user_id][cmd] = now
        return None

    # ------------------------------------------------------------------
    # Simple command handlers
    # ------------------------------------------------------------------

    async def _safe_reply(self, update: Update, text: str, parse_mode: str = "Markdown") -> None:
        """Send a message, truncating and wrapping in code block if needed."""
        MAX_LEN = 4096
        if len(text) > MAX_LEN:
            # Truncate safely at a natural boundary
            text = text[:MAX_LEN - 100] + "\n\n... (truncated)"
        try:
            await update.message.reply_text(text, parse_mode=parse_mode)
        except Exception:
            # If markdown fails, send as plain text
            await update.message.reply_text(text, parse_mode=None)

    async def cmd_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Send a welcome message with available commands."""
        self._log.info("cmd_start", user=update.effective_user.id)

        msg = (
            "🤖 *Quad Futures Trading Bot*\n\n"
            "Your personal automated futures trading assistant for Binance Futures.\n\n"
            "*Available commands:*\n"
            "• `/status` — Bot health, position summary, PnL, risk status\n"
            "• `/balance` — Account balances, total USDT value\n"
            "• `/positions` — List open positions with PnL\n"
            "• `/orders` — List open or pending orders\n"
            "• `/funding_rate [symbol]` — Show funding rate for tracked symbols\n"
            "• `/book <symbol>` — Show order book top 5 bid/ask levels\n"
            "• `/liquidation_warnings` — Show positions near liquidation\n"
            "• `/leverage [symbol] [value]` — View or set leverage\n"
            "• `/position_mode [mode]` — View or set position mode\n"
            "• `/market_regime` — Funding rate landscape and volatility\n"
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

        self._log.info("cmd_help", user=update.effective_user.id)

        msg = (
            "📚 *Quad Bot Command Reference*\n\n"
            "*Monitoring:*\n"
            "• `/status` — Show bot health, position count, daily PnL, circuit breakers, active strategies\n"
            "• `/balance` — Show all account balances with total USDT portfolio value\n"
            "• `/risk` — Risk gates status, circuit breaker status, exposure report\n\n"
            "*Trading:*\n"
            "• `/positions` — Table of open positions with current PnL, leverage, and liquidation price\n"
            "• `/orders` — Table of pending / open orders\n"
            "• `/funding_rate [symbol]` — Show funding rate for one or all tracked symbols\n"
            "• `/book <symbol>` — Show top 5 bids/asks with spread info\n"
            "• `/leverage [symbol] [value]` — View or set leverage for a symbol\n"
            "• `/position_mode [mode]` — View or switch between one-way and hedge mode\n"
            "• `/liquidation_warnings` — Check all positions for proximity to liquidation\n"
            "• `/market_regime` — Funding rate landscape and volatility assessment\n"
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
            "• `/analyze` — Groq AI analyses current market conditions (funding rates, order book, price action)\n"
            "• `/ai_strategy` — Groq AI recommends a futures strategy based on market regime\n"
            "• `/ai_status` — AI trading system status, rate limiter, recent decisions\n"
            "• `/ai_decision` — Trigger a full AI trading decision cycle (ENTER/EXIT/HOLD)"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def cmd_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Send bot status, position summary, PnL, and risk status."""

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

            # Get position count from exchange adapter
            exchange_adapter = getattr(self._orchestrator, "_exchange_adapter", None) if self._orchestrator else None
            if exchange_adapter is not None:
                try:
                    positions = await exchange_adapter.get_positions()
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

        self._log.info("cmd_balance", user=update.effective_user.id)

        try:
            # Fetch live account data from the exchange adapter
            exchange_adapter = getattr(self._orchestrator, "_exchange_adapter", None) if self._orchestrator else None
            if exchange_adapter is None:
                await update.message.reply_text("⚠️ Exchange adapter not available.", parse_mode="Markdown")
                return

            try:
                account = await exchange_adapter.get_account()
            except Exception as exc:
                self._log.warning("balance_fetch_failed", error=str(exc))
                await update.message.reply_text(f"⚠️ Error fetching balance: {exc}", parse_mode="Markdown")
                return

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

        self._log.info("cmd_positions", user=update.effective_user.id)

        try:
            positions: list[Any] = []
            exchange_adapter = getattr(self._orchestrator, "_exchange_adapter", None) if self._orchestrator else None
            if exchange_adapter is not None:
                try:
                    positions = await exchange_adapter.get_positions()
                except Exception as exc:
                    self._log.warning("cmd_positions_fetch_failed", error=str(exc))

            if not positions:
                msg = "📋 *Open Positions*\n\nNo open positions."
                await update.message.reply_text(msg, parse_mode="Markdown")
                return

            lines = ["📋 *Open Positions*\n"]
            lines.append(
                "```\n"
                f"{'Symbol':<12} {'Side':<6} {'Size':>6} {'Entry':>10} {'Mark':>10} {'Liq.Px':>10} {'PnL':>10} {'Lev':>4}"
            )
            lines.append("-" * 72)

            for pos in positions[:15]:  # Limit to 15 positions for readability
                symbol = getattr(pos, "symbol", getattr(pos, "contract_symbol", "?"))
                raw_side = getattr(pos, "position_side", getattr(pos, "side", "?"))
                side = str(raw_side) if not isinstance(raw_side, str) else raw_side
                size = float(getattr(pos, "size", getattr(pos, "quantity", 0)))
                entry = float(getattr(pos, "entry_price", 0))
                mark = float(getattr(pos, "mark_price", getattr(pos, "current_price", 0)))
                liq = float(getattr(pos, "liquidation_price", 0))
                pnl = float(getattr(pos, "unrealized_pnl", 0))
                lev = int(getattr(pos, "leverage", 1))

                pnl_str = f"{pnl:>+,.2f}"
                lines.append(
                    f"{symbol:<12} {side:<6} {size:>6.3f} {entry:>10.4f} {mark:>10.4f} "
                    f"{liq:>10.4f} {pnl_str:>10} {lev:>4}"
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

    # ------------------------------------------------------------------
    # Futures command handlers
    # ------------------------------------------------------------------

    async def cmd_funding_rate(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show funding rate for one or all tracked symbols.

        Usage: ``/funding_rate [symbol]``
        """

        self._log.info("cmd_funding_rate", user=update.effective_user.id)

        if self._market_data_engine is None:
            msg = "⚠️ Market data engine is not available."
            await update.message.reply_text(msg, parse_mode="Markdown")
            return

        try:
            if context.args:
                symbol = context.args[0].upper()
                fr = await self._market_data_engine.get_funding_rate(symbol)
                if fr is None:
                    msg = f"⚠️ No funding rate data available for `{symbol}`."
                    await update.message.reply_text(msg, parse_mode="Markdown")
                    return

                rate_pct = float(fr.funding_rate) * 100
                rate_emoji = "🟢" if float(fr.funding_rate) >= 0 else "🔴"
                now_ms = int(_time.time() * 1000)
                secs_remaining = max(0, (fr.next_funding_time - now_ms) // 1000)
                mins, secs = divmod(secs_remaining, 60)

                msg = (
                    f"💰 *Funding Rate — {symbol}*\n\n"
                    f"*Rate:* {rate_emoji} {rate_pct:+.5f}%\n"
                    f"*Next Funding:* ~{mins}m {secs}s\n"
                    f"*Mark Price:* ${float(fr.mark_price):,.2f}\n"
                    f"*Index Price:* ${float(fr.index_price):,.2f}"
                )
            else:
                # Show all tracked symbols
                config = self._config
                symbols = config["trading"]["underlyings"]
                lines = ["💰 *Funding Rates*\n"]
                lines.append("```\n" f"{'Symbol':<12} {'Rate':>10} {'Countdown':>14} {'Mark Price':>12}")
                lines.append("-" * 52)

                for sym in symbols:
                    fr = await self._market_data_engine.get_funding_rate(sym)
                    if fr is None:
                        lines.append(f"{sym:<12} {'N/A':>10} {'N/A':>14} {'N/A':>12}")
                        continue

                    rate_pct = float(fr.funding_rate) * 100
                    now_ms = int(_time.time() * 1000)
                    secs_remaining = max(0, (fr.next_funding_time - now_ms) // 1000)
                    mins, secs = divmod(secs_remaining, 60)

                    rate_str = f"{rate_pct:+.5f}%"
                    countdown = f"{mins}m {secs}s"
                    mark_str = f"${float(fr.mark_price):,.2f}"
                    lines.append(f"{sym:<12} {rate_str:>10} {countdown:>14} {mark_str:>12}")

                lines.append("```")
                msg = "\n".join(lines)

            await update.message.reply_text(msg, parse_mode="Markdown")

        except Exception as exc:
            self._log.exception("cmd_funding_rate_error", error=str(exc))
            await update.message.reply_text(f"⚠️ Error fetching funding rates: {exc}")

    async def cmd_book(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show top 5 bid/ask levels from order book.

        Usage: ``/book <symbol>``
        """

        self._log.info("cmd_book", user=update.effective_user.id)

        if not context.args:
            msg = "⚠️ Usage: `/book <symbol>`\nExample: `/book BTCUSDT`"
            await update.message.reply_text(msg, parse_mode="Markdown")
            return

        symbol = context.args[0].upper()

        if self._market_data_engine is None:
            msg = "⚠️ Market data engine is not available."
            await update.message.reply_text(msg, parse_mode="Markdown")
            return

        try:
            book = await self._market_data_engine.get_order_book(symbol)

            if book is None:
                msg = f"⚠️ No order book data available for `{symbol}`."
                await update.message.reply_text(msg, parse_mode="Markdown")
                return

            bids = book.get("bids", [])[:5]
            asks = book.get("asks", [])[:5]

            best_bid = float(bids[0][0]) if bids else 0.0
            best_ask = float(asks[0][0]) if asks else 0.0
            spread = best_ask - best_bid
            spread_pct = (spread / best_ask * 100) if best_ask > 0 else 0.0

            lines = [f"📊 *Order Book — {symbol}*\n"]

            lines.append(f"*Spread:* ${spread:.2f} ({spread_pct:.3f}%)\n")

            lines.append("```\n" f"{'Bids':>24}     {'Asks':>24}")
            lines.append(f"{'Price':>12} {'Qty':>10}     {'Price':>12} {'Qty':>10}")
            lines.append("-" * 52)

            max_rows = max(len(bids), len(asks))
            for i in range(max_rows):
                bid_str = f"{float(bids[i][0]):>12.4f} {float(bids[i][1]):>10.4f}" if i < len(bids) else " " * 24
                ask_str = f"{float(asks[i][0]):>12.4f} {float(asks[i][1]):>10.4f}" if i < len(asks) else " " * 24
                lines.append(f"{bid_str}     {ask_str}")

            lines.append("```")
            msg = "\n".join(lines)
            await update.message.reply_text(msg, parse_mode="Markdown")

        except Exception as exc:
            self._log.exception("cmd_book_error", error=str(exc))
            await update.message.reply_text(f"⚠️ Error fetching order book: {exc}")

    async def cmd_leverage(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """View or set leverage for a symbol.

        Usage: ``/leverage [symbol] [value]``
        """

        self._log.info("cmd_leverage", user=update.effective_user.id)

        try:
            trading_config = self._config["trading"]
            risk_config = self._config["risk"]
            default_leverage = trading_config.get("leverage")
            max_leverage = risk_config.get("max_leverage")

            if not context.args:
                msg = (
                    f"⚙️ *Leverage*\n\n"
                    f"*Default Leverage:* `{default_leverage}x`\n"
                    f"*Max Leverage:* `{max_leverage}x`\n\n"
                    "Usage:\n"
                    "• `/leverage SYMBOL` — Show current leverage for a symbol\n"
                    "• `/leverage SYMBOL VALUE` — Set leverage (requires exchange)"
                )
                await update.message.reply_text(msg, parse_mode="Markdown")
                return

            symbol = context.args[0].upper()

            if len(context.args) >= 2:
                try:
                    requested = int(context.args[1])
                    clamped = min(requested, max_leverage)
                    msg = (
                        f"⚙️ *Set Leverage — {symbol}*\n\n"
                        f"*Requested:* `{requested}x`\n"
                        f"*Clamped:* `{clamped}x` (max: `{max_leverage}x`)\n\n"
                        "_Setting leverage on the exchange requires a running exchange adapter._\n"
                        f"*Current Config Default:* `{default_leverage}x`"
                    )
                    await update.message.reply_text(msg, parse_mode="Markdown")
                except ValueError:
                    await update.message.reply_text(
                        "⚠️ Leverage value must be an integer.",
                        parse_mode="Markdown",
                    )
                return

            # 1 arg — show leverage info for the symbol
            msg = (
                f"⚙️ *Leverage — {symbol}*\n\n"
                f"*Config Leverage:* `{default_leverage}x`\n"
                f"*Max Allowed:* `{max_leverage}x`\n\n"
                "To set: `/leverage {symbol} <value>`"
            )
            await update.message.reply_text(msg, parse_mode="Markdown")

        except Exception as exc:
            self._log.exception("cmd_leverage_error", error=str(exc))
            await update.message.reply_text(f"⚠️ Error: {exc}")

    async def cmd_position_mode(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """View or set position mode.

        Usage: ``/position_mode [mode]``
        """

        self._log.info("cmd_position_mode", user=update.effective_user.id)

        try:
            trading_config = self._config["trading"]
            current_mode = trading_config.get("position_mode")

            if not context.args:
                msg = (
                    f"⚙️ *Position Mode*\n\n"
                    f"*Current Mode:* `{current_mode}`\n"
                    f"*Valid Modes:* `one_way`, `hedge`\n\n"
                    "Usage: `/position_mode one_way` or `/position_mode hedge`\n\n"
                    "_Switching position mode on the exchange requires all "
                    "positions to be closed first and a running exchange adapter._"
                )
                await update.message.reply_text(msg, parse_mode="Markdown")
                return

            requested = context.args[0].lower()
            if requested not in ("one_way", "hedge"):
                await update.message.reply_text(
                    "⚠️ Invalid mode. Use `one_way` or `hedge`.",
                    parse_mode="Markdown",
                )
                return

            msg = (
                f"⚙️ *Position Mode — {requested}*\n\n"
                f"*Current Config:* `{current_mode}`\n"
                f"*Requested:* `{requested}`\n\n"
                "_To switch modes on the exchange, close all positions first "
                "and use the exchange API._\n"
                f"*Config Value:* `{requested}`"
            )
            await update.message.reply_text(msg, parse_mode="Markdown")

        except Exception as exc:
            self._log.exception("cmd_position_mode_error", error=str(exc))
            await update.message.reply_text(f"⚠️ Error: {exc}")

    async def cmd_liquidation_warnings(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Check all positions for proximity to liquidation."""

        self._log.info("cmd_liquidation_warnings", user=update.effective_user.id)

        try:
            risk_config = self._config["risk"]
            min_distance = float(risk_config.get("min_distance_to_liquidation_pct"))

            exchange_adapter = getattr(self._orchestrator, "_exchange_adapter", None) if self._orchestrator else None
            if exchange_adapter is None:
                await update.message.reply_text(
                    "⚠️ Exchange adapter not available. Cannot check liquidation proximity.",
                    parse_mode="Markdown",
                )
                return

            positions = await exchange_adapter.get_positions()
            if not positions:
                await update.message.reply_text(
                    "✅ No open positions. Nothing to check.",
                    parse_mode="Markdown",
                )
                return

            at_risk = []
            for pos in positions:
                mark = float(getattr(pos, "mark_price", getattr(pos, "current_price", 0)))
                liq = float(getattr(pos, "liquidation_price", 0))
                if mark <= 0 or liq <= 0:
                    continue

                distance = abs(mark - liq) / mark
                if distance < min_distance:
                    symbol = getattr(pos, "symbol", getattr(pos, "contract_symbol", "?"))
                    raw_side = getattr(pos, "position_side", getattr(pos, "side", "?"))
                    side = str(raw_side) if not isinstance(raw_side, str) else raw_side
                    lev = int(getattr(pos, "leverage", 1))
                    at_risk.append((symbol, side, lev, distance, liq, mark))

            if not at_risk:
                msg = (
                    f"✅ *Liquidation Check*\n\n"
                    f"No positions are near liquidation.\n"
                    f"*Threshold:* `{min_distance:.0%}` distance"
                )
                await update.message.reply_text(msg, parse_mode="Markdown")
                return

            lines = ["🚨 *Liquidation Warnings*\n"]
            lines.append(f"Positions closer than {min_distance:.0%} to liquidation:\n")

            for symbol, side, lev, distance, liq, mark in at_risk:
                lines.append(
                    f"• `{symbol}` {side}\n"
                    f"  Leverage: {lev}x | Distance: {distance:.1%}\n"
                    f"  Liq Price: ${liq:,.2f} | Mark: ${mark:,.2f}\n"
                )

            msg = "\n".join(lines)
            await update.message.reply_text(msg, parse_mode="Markdown")

        except Exception as exc:
            self._log.exception("cmd_liquidation_warnings_error", error=str(exc))
            await update.message.reply_text(f"⚠️ Error checking liquidation: {exc}")

    async def cmd_market_regime(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show funding rate landscape and volatility assessment."""

        self._log.info("cmd_market_regime", user=update.effective_user.id)

        if self._market_data_engine is None:
            msg = "⚠️ Market data engine is not available."
            await update.message.reply_text(msg, parse_mode="Markdown")
            return

        try:
            config = self._config
            symbols = config["trading"]["underlyings"]

            positive_count = 0
            negative_count = 0
            total_rate = 0.0
            lines = ["🌡️ *Market Regime*\n"]
            lines.append(f"{'Symbol':<12} {'Rate':>10} {'Sentiment':>10} {'Mark':>12}")
            lines.append("-" * 48)

            for sym in symbols:
                fr = await self._market_data_engine.get_funding_rate(sym)
                if fr is None:
                    continue

                rate_pct = float(fr.funding_rate) * 100
                total_rate += rate_pct
                if rate_pct >= 0:
                    positive_count += 1
                    sentiment = "🟢 Bullish"
                else:
                    negative_count += 1
                    sentiment = "🔴 Bearish"

                lines.append(
                    f"{sym:<12} {rate_pct:>+9.5f}% {sentiment:>10} "
                    f"${float(fr.mark_price):>8,.2f}"
                )

            lines.append("")
            n = positive_count + negative_count
            if n > 0:
                avg_rate = total_rate / n
                bias = "Bullish (positive funding)" if avg_rate > 0 else "Bearish (negative funding)" if avg_rate < 0 else "Neutral"
                lines.append(f"*Average Rate:* {avg_rate:+.5f}%")
                lines.append(f"*Bias:* {bias}")
                lines.append(f"*Positive / Negative:* {positive_count}/{negative_count}")

                # Volatility assessment from ticker if available
                try:
                    ticker = await self._market_data_engine.get_ticker(symbols[0])
                    if ticker:
                        change = float(ticker.get("price_change_percent", 0))
                        high = float(ticker.get("high_price", 0))
                        low = float(ticker.get("low_price", 0))
                        last = float(ticker.get("last_price", 0))
                        if last > 0 and high > 0 and low > 0:
                            range_pct = (high - low) / last * 100
                            vol_label = "High" if range_pct > 5 else "Moderate" if range_pct > 2 else "Low"
                            lines.append(f"*24h Change:* {change:+.2f}%")
                            lines.append(f"*24h Range:* {range_pct:.1f}% ({vol_label} volatility)")
                except Exception:
                    pass

            msg = "\n".join(lines)
            await update.message.reply_text(msg, parse_mode="Markdown")

        except Exception as exc:
            self._log.exception("cmd_market_regime_error", error=str(exc))
            await update.message.reply_text(f"⚠️ Error: {exc}")

    async def cmd_strategies(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """List active strategies and their status."""

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
                    self._log.warning("kill_switch: risk_manager is None, falling back to orchestrator")
                    ks = getattr(self._orchestrator, "trigger_kill_switch", None)
                    if ks is not None:
                        ks(reason)
                else:
                    self._log.warning("kill_switch: both risk_manager and orchestrator are None")

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

            # Add funding rate info if market data is available
            funding_info = ""
            if self._market_data_engine is not None:
                try:
                    symbols = self._config["trading"]["underlyings"]
                    fr_lines = []
                    for sym in symbols:
                        fr = await self._market_data_engine.get_funding_rate(sym)
                        if fr is not None:
                            rate_pct = float(fr.funding_rate) * 100
                            fr_lines.append(f"  • `{sym}`: {rate_pct:+.5f}%")
                    if fr_lines:
                        funding_info = "\n*Funding Rates:*\n" + "\n".join(fr_lines) + "\n"
                except Exception:
                    pass

            # Liquidation proximity summary
            liq_info = ""
            try:
                exchange_adapter = getattr(self._orchestrator, "_exchange_adapter", None) if self._orchestrator else None
                if exchange_adapter is not None:
                    positions = await exchange_adapter.get_positions()
                    at_risk = 0
                    min_distance_config = float(self._config["risk"]["min_distance_to_liquidation_pct"])
                    for pos in (positions or []):
                        mark = float(getattr(pos, "mark_price", getattr(pos, "current_price", 0)))
                        liq = float(getattr(pos, "liquidation_price", 0))
                        if mark > 0 and liq > 0:
                            distance = abs(mark - liq) / mark
                            if distance < min_distance_config:
                                at_risk += 1
                    liq_emoji = "🚨" if at_risk > 0 else "✅"
                    liq_info = f"\n*Liquidation Risk:* {liq_emoji} {at_risk} position(s) near liquidation\n"
            except Exception:
                pass

            msg = (
                "⚠️ *Risk Status*\n\n"
                f"*Drawdown:* {float(risk_status.drawdown_percent):.2%}\n"
                f"*Daily PnL:* ${float(risk_status.daily_pnl):,.2f} / ${float(risk_status.daily_loss_limit):,.2f}\n"
                f"{liq_info}"
                f"{funding_info}\n"
                f"*Gates:*\n" + "\n".join(gate_lines) + "\n\n"
                f"*Circuit Breakers:*\n" + "\n".join(cb_lines) + "\n\n"
                f"*Exposure:*\n" + "\n".join(exposure_lines)
            )
            await self._safe_reply(update, msg)

        except Exception as exc:
            self._log.exception("cmd_risk_error", error=str(exc))
            await update.message.reply_text(f"⚠️ Error fetching risk status: {exc}")

    async def cmd_cancel(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Cancel an order by its ID.

        Usage: ``/cancel <order_id>``
        """

        self._log.info("cmd_cancel", user=update.effective_user.id)

        if not context.args or not context.args[0].strip():
            await update.message.reply_text("Usage: `/cancel <order_id>`")
            return

        order_id = context.args[0].strip()
        if len(order_id) > 100:
            await update.message.reply_text("Order ID too long (max 100 chars)")
            return
        if not _re.match(r'^[a-zA-Z0-9_\-]+$', order_id):
            await update.message.reply_text("Invalid order ID format")
            return

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
        """Show current configuration tree or key settings overview."""

        self._log.info("cmd_settings", user=update.effective_user.id)

        try:
            # If orchestrator is available, show the full config tree
            if self._orchestrator is not None:
                config_mgr = getattr(self._orchestrator, "_config_manager", None)
                if config_mgr is not None and hasattr(config_mgr, "_config"):
                    formatted = _json.dumps(config_mgr._config, indent=2, default=str)
                    if len(formatted) > 4000:
                        formatted = formatted[:4000] + "\n\n... (truncated)"
                    msg = (
                        f"⚙️ *Full Configuration*\n```\n{formatted}\n```\n"
                        "Use `/set <key> <value>` to change a setting."
                    )
                    await self._safe_reply(update, msg)
                    return

            # Fallback: show key settings
            config = self._config
            mode = config.get("_mode")
            dry_run = config.get("_dry_run")
            exchange_name = config["exchange"]["name"]
            testnet = config["exchange"]["testnet"]
            default_strategy = config["trading"]["default_strategy"]
            max_positions = config["risk"]["max_positions"]
            max_position_size = config["risk"]["max_portfolio_risk_pct"]
            daily_loss = config["risk"]["max_daily_loss_usd"]
            leverage = config["trading"]["leverage"]
            margin_mode = config["trading"]["margin_mode"]
            position_mode = config["trading"]["position_mode"]

            msg = (
                "⚙️ *Current Settings*\n\n"
                f"*Mode:* `{mode}`\n"
                f"*Dry Run:* `{dry_run}`\n"
                f"*Exchange:* `{exchange_name}`\n"
                f"*Testnet:* `{testnet}`\n"
                f"*Default Strategy:* `{default_strategy}`\n"
                f"*Leverage:* `{leverage}x`\n"
                f"*Margin Mode:* `{margin_mode}`\n"
                f"*Position Mode:* `{position_mode}`\n"
                f"*Max Positions:* `{max_positions}`\n"
                f"*Max Position Size:* `{float(max_position_size):.0%}`\n"
                f"*Daily Loss Limit:* `${daily_loss}`\n"
                "\nUse `/set <key> <value>` to change a setting."
            )
            await self._safe_reply(update, msg)

        except Exception as exc:
            self._log.exception("cmd_settings_error", error=str(exc))
            await update.message.reply_text(f"⚠️ Error fetching settings: {exc}")

    async def cmd_set(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Set a config value at runtime.

        Usage: /set <key> <value>
        Example: /set trading.leverage 5
        Example: /set risk.max_funding_rate_cost 0.001
        Example: /set risk.max_drawdown_pct 15
        """
        self._log.info("cmd_set", user=update.effective_user.id, args=context.args)

        if not context.args or len(context.args) < 2:
            await update.message.reply_text(
                "Usage: `/set <config.key.path> <value>`\n\n"
                "Examples:\n"
                "`/set trading.leverage 5`\n"
                "`/set risk.max_funding_rate_cost 0.001`\n"
                "`/set risk.max_drawdown_pct 15`\n"
                "`/set trading.margin_mode isolated`\n"
                "`/set trading.position_mode one_way`\n"
                "`/set trading.serial_trade_mode true`\n"
                "`/set ai.enabled false`\n"
                "`/set exchange.testnet true`",
                parse_mode="Markdown",
            )
            return

        key = context.args[0]
        value_raw = " ".join(context.args[1:])

        # Parse value type
        try:
            value = int(value_raw)
        except ValueError:
            try:
                value = float(value_raw)
            except ValueError:
                if value_raw.lower() in ("true", "false", "yes", "no"):
                    value = value_raw.lower() in ("true", "yes")
                else:
                    value = value_raw

        try:
            if self._orchestrator is None:
                await update.message.reply_text(
                    "⚠️ Orchestrator is not available.",
                    parse_mode="Markdown",
                )
                return

            config_mgr = getattr(self._orchestrator, "_config_manager", None)
            if config_mgr is None:
                await update.message.reply_text(
                    "⚠️ Config manager is not available.",
                    parse_mode="Markdown",
                )
                return

            old_value = config_mgr.get(key)
            config_mgr.set(key, value)

            # Check if there's an env var mapping
            from quad.config.manager import ENV_VAR_MAP as env_var_map

            env_var = None
            for ev_name, config_key in env_var_map.items():
                if config_key == key:
                    env_var = ev_name
                    break

            msg = (
                f"✅ *Config Updated*\n"
                f"Key: `{key}`\n"
                f"Old: `{old_value}`\n"
                f"New: `{value}`\n"
            )
            if env_var:
                msg += f"Env: `{env_var}`\n"
            msg += "\n_⚠️ Some changes may need a restart to take full effect_"

            await update.message.reply_text(msg, parse_mode="Markdown")

        except Exception as exc:
            await update.message.reply_text(
                f"❌ Error setting `{key}`: {exc}",
                parse_mode="Markdown",
            )

    # ------------------------------------------------------------------
    # AI-powered commands
    # ------------------------------------------------------------------

    async def cmd_analyze(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Send AI-generated market analysis for configured underlyings."""

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

            config = self._config
            underlyings = list(config["trading"]["underlyings"])

            results: list[str] = []
            for underlying in underlyings:
                try:
                    mark_price = await self._market_data_engine.get_mark_price(underlying)
                    funding_rate = await self._market_data_engine.get_funding_rate(underlying)
                    order_book = await self._market_data_engine.get_order_book(underlying)
                    analysis = await analyze_market(
                        client=self._groq_client,
                        symbol=underlying,
                        mark_price=mark_price,
                        funding_rate=funding_rate,
                        order_book=order_book,
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
            # Truncate if too long for Telegram
            if len(msg_text) > 4096:
                msg_text = msg_text[:4000] + "\n\n... (truncated)"
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
            underlyings = config["trading"]["underlyings"]
            underlying = list(underlyings)[0] if underlyings else "BTCUSDT"

            mark_price = await self._market_data_engine.get_mark_price(underlying)
            funding_rate = await self._market_data_engine.get_funding_rate(underlying)

            recommendation = await recommend_strategy(
                client=self._groq_client,
                symbol=underlying,
                mark_price=mark_price,
                funding_rate=funding_rate,
            )

            msg_text = (
                f"🎯 *AI Strategy Recommendation*\n\n"
                f"Based on current {underlying} market conditions:\n\n"
                f"{recommendation}"
            )
            # Truncate if too long for Telegram
            if len(msg_text) > 4096:
                msg_text = msg_text[:4000] + "\n\n... (truncated)"
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
            underlyings = self._config["trading"]["underlyings"]

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
                        futures_positions=positions,
                        orders=[],
                        funding_rates=None,
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
                        result = await exec_method(strategy_name=strategy_name, dry_run=False)
                        if result.get("error"):
                            await query.edit_message_text(
                                f"⚠️ `{strategy_name}` execution error:\n{result['error']}",
                                parse_mode="Markdown",
                            )
                        else:
                            action_infos = result.get("actions", [])
                            executed = result.get("executed", [])
                            parts = [f"✅ `{strategy_name}` executed successfully."]
                            if action_infos:
                                parts.append(f"\n*Actions generated:* {result.get('actions_count', len(action_infos))}")
                                for a in action_infos[:5]:
                                    parts.append(f"  • `{a.get('type', '?')}` {a.get('contract', '')} {a.get('side', '')}")
                            if executed:
                                parts.append(f"\n*Execution results:*")
                                for e in executed[:5]:
                                    e_status = e.get("result", e.get("error", "submitted"))
                                    parts.append(f"  • `{e.get('action', '?')}` → {e_status}")
                            await query.edit_message_text(
                                "\n".join(parts),
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
                        text=(
                            "⚠️ <b>Bot Error</b>:\n"
                            f"<code>{_html.escape(str(context.error))}</code>"
                        ),
                        parse_mode="HTML",
                    )
            except Exception as exc:
                self._log.warning("error_notification_failed", error=str(exc))

        # Error has been logged and reported — do not re-raise
