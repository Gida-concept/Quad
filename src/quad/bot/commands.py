"""Telegram command handlers for Quad Futures Bot.

Each command handler is a method on ``QuadBotCommands``.  Handlers are
kept short — they delegate data queries to the respective subsystem and
format the response as Telegram markdown messages.
"""

from __future__ import annotations

import html as _html
import re as _re
import time as _time
import warnings
from collections import defaultdict
from decimal import Decimal
from typing import Any

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update, User
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
)
from telegram.warnings import PTBUserWarning

from quad.risk.gates import effective_min_liquidation_distance

from ._ai_cmds_mixin import AiCommandsMixin, _escape_md
from ._config_cmds_mixin import (
    BLOCKED_SET_KEYS,
    BLOCKED_SET_PREFIXES,
    MAX_SET_LEVERAGE,
    MAX_SET_TRADE_CAPITAL_USD,
    OPERATOR_SET_KEYS,
    SAFE_SET_KEYS,
    AccountConfigCommandsMixin,
)
from ._market_cmds_mixin import MarketInfoCommandsMixin
from ._safety_mixin import SafetyMixin

__all__ = [
    "BLOCKED_SET_KEYS",
    "BLOCKED_SET_PREFIXES",
    "MAX_SET_LEVERAGE",
    "MAX_SET_TRADE_CAPITAL_USD",
    "OPERATOR_SET_KEYS",
    "QuadBotCommands",
    "SAFE_SET_KEYS",
    "UpdateNotAddressable",
    "_escape_md",
]

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


class UpdateNotAddressable(Exception):
    """An update carries neither a message nor a sender.

    Raised by :meth:`QuadBotCommands._narrow`.  Not a fault: inline-mode
    callbacks and channel posts legitimately produce these, so
    :meth:`QuadBotCommands.error_handler` drops them silently rather than
    notifying the admin chat on every occurrence.
    """


# ============================================================================
# QuadBotCommands
# ============================================================================


class QuadBotCommands(
    AccountConfigCommandsMixin, SafetyMixin, MarketInfoCommandsMixin, AiCommandsMixin
):
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

        # Multi-tenant binding repos (None in single-tenant/no-DB mode)
        self._bindings = None
        self._pairing = None
        if shared_state.get("db_manager") is not None:
            from quad.persistence.repositories import (
                PairingCodeRepository,
                TelegramBindingRepository,
            )

            self._bindings = TelegramBindingRepository(shared_state["db_manager"])
            self._pairing = PairingCodeRepository(shared_state["db_manager"])

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
            "exchange": 2.0,
            # Manual order trigger: registered without the CommandHandler
            # rate-limit wrapper, so the cooldown is enforced inside the
            # conversation flow.  Generous enough to stop an accidental
            # double-tap, tight enough to allow a deliberate re-run.
            "execute": 60.0,
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

    @staticmethod
    def _now_ms() -> int:
        import time as _time_mod

        return int(_time_mod.time() * 1000)

    @staticmethod
    def _narrow(update: Update) -> tuple[Message, User]:
        """Return the ``(message, user)`` pair carried by *update*.

        Raises
        ------
        UpdateNotAddressable
            When the update carries no message or no sender — e.g. an
            inline-mode callback routed to a ``CommandHandler``, or a
            stripped update in a test double.

        Why this raises instead of returning ``None``: all 26 call sites
        unpack the result immediately, so the ``None`` branch was never
        handled by anyone — it just surfaced later as
        ``TypeError: cannot unpack non-ITERABLE NoneType``, far from the
        cause.  Raising here keeps the return type honest and lets
        :meth:`error_handler` recognise (and silently drop) the case.
        """
        message = update.message
        user = update.effective_user
        if message is None or user is None:
            raise UpdateNotAddressable(
                "update carries no message/sender "
                f"(message={message is not None}, user={user is not None})"
            )
        return message, user

    async def _safe_reply(
        self, update: Update, text: str, parse_mode: str = "Markdown"
    ) -> None:
        """Send a message, truncating and wrapping in code block if needed."""
        MAX_LEN = 4096
        if len(text) > MAX_LEN:
            # Truncate safely at a natural boundary
            text = text[: MAX_LEN - 100] + "\n\n... (truncated)"
        message, _ = self._narrow(update)
        try:
            await message.reply_text(text, parse_mode=parse_mode)
        except Exception:
            # If markdown fails, send as plain text
            await message.reply_text(text, parse_mode=None)

    @staticmethod
    def _callback_chat_id(query: Any) -> int | None:
        """Return the chat id a callback was raised in, or ``None``.

        ``CallbackQuery.message`` is a ``MaybeInaccessibleMessage``: for an
        old/inaccessible message PTB returns an ``InaccessibleMessage``, which
        has **no** ``chat_id`` attribute.  Reading it raised ``AttributeError``
        inside the binding check, so such callbacks were treated as unbound.
        """
        message = getattr(query, "message", None)
        chat_id = getattr(message, "chat_id", None)
        return chat_id if isinstance(chat_id, int) else None

    async def get_bound_tenant(self, chat_id: int) -> str | None:
        """Return the tenant_uuid bound to a chat, or None.

        Returns ``"__operator__"`` when binding is not enforced (no DB) so
        single-tenant personal runs keep working without pairing.
        """
        if self._bindings is None:
            return "__operator__"
        if (
            self._notification_chat_id is not None
            and chat_id == self._notification_chat_id
        ):
            return "__operator__"
        binding = await self._bindings.get_by_chat_id(chat_id)
        return binding.tenant_uuid if binding else None

    async def require_operator(self, chat_id: int | None) -> str | None:
        """Return the bound tenant only for the operator chat, else ``None``.

        ``"__operator__"`` means "the person who owns this deployment"
        (single-tenant runs with no DB, or the configured notification
        chat).  Any *bound tenant* chat is a customer of the control plane
        and must not be able to run strategies or the kill switch against
        the operator's own exchange account.
        """
        if chat_id is None:
            return None
        try:
            bound = await self.get_bound_tenant(chat_id)
        except Exception:
            self._log.exception("operator_check_failed")
            return None
        if bound != "__operator__":
            return None
        return bound

    def _execution_environment(self) -> tuple[bool, bool, str]:
        """Return ``(is_testnet, is_dry_run, human_label)`` for the live bot.

        Mirrors the orchestrator's ``_is_dry_run`` and the adapter's
        ``is_testnet`` so the confirmation card states exactly where an
        order would land.
        """
        adapter = getattr(self._orchestrator, "_exchange_adapter", None)
        is_testnet = bool(getattr(adapter, "is_testnet", False))
        is_dry_run = bool(
            getattr(self._orchestrator, "_is_dry_run", self._config.get("_dry_run"))
        )
        if is_dry_run and not is_testnet:
            label = "🔒 DRY-RUN (live exchange, orders blocked)"
        elif is_dry_run:
            label = "🧪 DRY-RUN on testnet"
        elif is_testnet:
            label = "🧪 TESTNET (real testnet orders)"
        else:
            label = "🚨 **LIVE — REAL MONEY**"
        return is_testnet, is_dry_run, label

    async def cmd_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Welcome / link flow: ``/start`` or ``/start <pairing-code>``."""
        message, user = self._narrow(update)
        self._log.info("cmd_start", user=user.id)
        chat_id = message.chat_id

        args = getattr(context, "args", None) or []
        # Both repositories are built together from ``shared_state``, but the
        # pairing flow uses each of them, so guard on the one that is about to
        # be dereferenced rather than relying on them never diverging.
        if args and self._pairing is not None and self._bindings is not None:
            code = str(args[0]).strip().upper()
            row = await self._pairing.get_valid(code, self._now_ms())
            if row is None:
                await message.reply_text(
                    "⚠️ That pairing code is invalid or expired. Generate a new one "
                    "from your dashboard (`POST /v1/telegram/pairing-code`) and retry.",
                    parse_mode="Markdown",
                )
                return
            await self._bindings.bind(row.tenant_uuid, chat_id)
            await self._pairing.mark_used(row.id, used_by_chat=chat_id)
            self._log.info("chat_bound", chat_id=chat_id, tenant=row.tenant_uuid)
            await message.reply_text(
                "✅ *Chat linked!* This chat now controls your Quad account.\n\n"
                "Try `/status` to see your account.",
                parse_mode="Markdown",
            )
            return

        bound = await self.get_bound_tenant(chat_id)
        if bound is None:
            await message.reply_text(
                "🤖 *Quad Futures Trading Bot*\n\n"
                "This chat isn't linked to a Quad account yet.\n\n"
                "1. Create your account via the quad-api (`POST /v1/auth/telegram`).\n"
                "2. Generate a pairing code (`POST /v1/telegram/pairing-code`).\n"
                "3. Send `/start YOURCODE` here.\n",
                parse_mode="Markdown",
            )
            return

        msg = (
            "🤖 *Quad Futures Trading Bot*\n\n"
            "Your personal automated USDT perpetual futures trading assistant (Bybit, category=linear).\n\n"
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
            "• `/exchange` — Bybit connection status (testnet/live)\n"
            "• `/help` — Full command reference"
        )
        await message.reply_text(msg, parse_mode="Markdown")

    async def cmd_help(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Send the full command reference."""
        message, user = self._narrow(update)

        self._log.info("cmd_help", user=user.id)

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
        await message.reply_text(msg, parse_mode="Markdown")

    async def cmd_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Send bot status, position summary, PnL, and risk status."""
        message, user = self._narrow(update)

        self._log.info("cmd_status", user=user.id)

        try:
            # Gather status information from subsystems
            position_count = 0
            daily_pnl = Decimal(0)
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
            exchange_adapter = (
                getattr(self._orchestrator, "_exchange_adapter", None)
                if self._orchestrator
                else None
            )
            if exchange_adapter is not None:
                try:
                    positions = await exchange_adapter.get_positions()
                    position_count = (
                        len(positions) if isinstance(positions, list) else 0
                    )
                except Exception as exc:
                    self._log.warning("status_positions_error", error=str(exc))

            # Get active strategies
            if self._orchestrator is not None:
                try:
                    strat_list = getattr(
                        self._orchestrator, "get_active_strategies", None
                    )
                    if strat_list is not None:
                        strategies = strat_list()
                        active_strategies = (
                            [
                                s.get_name() if hasattr(s, "get_name") else str(s)
                                for s in strategies
                            ]
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
                f"*Exchange:* 🟢 Bybit USDT perpetual (pybit)\n"
                f"*Orders Submitted:* {exec_stats.get('total_submitted', 0)}\n"
                f"*Orders Filled:* {exec_stats.get('total_filled', 0)}\n"
                f"*Orders Rejected:* {exec_stats.get('total_rejected', 0)}"
            )
            await message.reply_text(msg, parse_mode="Markdown")

        except Exception as exc:
            self._log.exception("cmd_status_error", error=str(exc))
            await message.reply_text(f"⚠️ Error fetching status: {exc}")

    # ------------------------------------------------------------------
    # Futures command handlers
    # ------------------------------------------------------------------

    async def cmd_strategies(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """List active strategies and their status."""
        message, user = self._narrow(update)

        self._log.info("cmd_strategies", user=user.id)

        try:
            # Import here to avoid circular import at module level
            from quad.strategy.base import StrategyRegistry

            registered = StrategyRegistry.list()

            if not registered:
                msg = "📋 *Active Strategies*\n\nNo strategies are registered."
                await message.reply_text(msg, parse_mode="Markdown")
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
                    default_str = (
                        f" (default: {p.default})" if p.default is not None else ""
                    )
                    param_lines.append(f"  • `{p.name}`: {p.description}{default_str}")

                lines.append(f"*{name}*\n{desc}")
                if param_lines:
                    lines.extend(param_lines)
                lines.append("")

            msg = "\n".join(lines)
            await message.reply_text(msg, parse_mode="Markdown")

        except Exception as exc:
            self._log.exception("cmd_strategies_error", error=str(exc))
            await message.reply_text(f"⚠️ Error listing strategies: {exc}")

    async def cmd_risk(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Send risk status, circuit breakers, and exposure report."""
        message, user = self._narrow(update)

        self._log.info("cmd_risk", user=user.id)

        try:
            if self._risk_manager is None:
                msg = "⚠️ Risk manager is not available."
                await message.reply_text(msg, parse_mode="Markdown")
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
                        funding_info = (
                            "\n*Funding Rates:*\n" + "\n".join(fr_lines) + "\n"
                        )
                except Exception as exc:
                    self._log.warning("risk_funding_rates_error", error=str(exc))

            # Liquidation proximity summary
            liq_info = ""
            try:
                exchange_adapter = (
                    getattr(self._orchestrator, "_exchange_adapter", None)
                    if self._orchestrator
                    else None
                )
                if exchange_adapter is not None:
                    positions = await exchange_adapter.get_positions()
                    at_risk = 0
                    for pos in positions or []:
                        mark = float(
                            getattr(pos, "mark_price", getattr(pos, "current_price", 0))
                        )
                        liq = float(getattr(pos, "liquidation_price", 0))
                        if mark > 0 and liq > 0:
                            distance = abs(mark - liq) / mark
                            min_distance = float(
                                effective_min_liquidation_distance(
                                    self._config["risk"],
                                    getattr(pos, "leverage", None),
                                )
                            )
                            if distance < min_distance:
                                at_risk += 1
                    liq_emoji = "🚨" if at_risk > 0 else "✅"
                    liq_info = f"\n*Liquidation Risk:* {liq_emoji} {at_risk} position(s) near liquidation\n"
            except Exception as exc:
                self._log.warning("risk_liquidation_proximity_error", error=str(exc))

            msg = (
                "⚠️ *Risk Status*\n\n"
                f"*Drawdown:* {float(risk_status.drawdown_percent):.2%}\n"
                f"*Daily PnL:* ${float(risk_status.daily_pnl):,.2f} / ${float(risk_status.daily_loss_limit):,.2f}\n"
                f"{liq_info}"
                f"{funding_info}\n"
                f"*Gates:*\n" + "\n".join(gate_lines) + "\n\n"
                "*Circuit Breakers:*\n" + "\n".join(cb_lines) + "\n\n"
                "*Exposure:*\n" + "\n".join(exposure_lines)
            )
            await self._safe_reply(update, msg)

        except Exception as exc:
            self._log.exception("cmd_risk_error", error=str(exc))
            await message.reply_text(f"⚠️ Error fetching risk status: {exc}")

    async def cmd_cancel(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Cancel an order by its ID.

        Usage: ``/cancel <order_id>``
        """
        message, user = self._narrow(update)

        self._log.info("cmd_cancel", user=user.id)

        if not context.args or not context.args[0].strip():
            await message.reply_text("Usage: `/cancel <order_id>`")
            return

        order_id = context.args[0].strip()
        if len(order_id) > 100:
            await message.reply_text("Order ID too long (max 100 chars)")
            return
        if not _re.match(r"^[a-zA-Z0-9_\-]+$", order_id):
            await message.reply_text("Invalid order ID format")
            return

        try:
            if self._execution_engine is None:
                msg = "⚠️ Execution engine is not available."
                await message.reply_text(msg, parse_mode="Markdown")
                return

            success = await self._execution_engine.cancel_order(order_id)
            if success:
                msg = f"✅ Order `{order_id}` cancelled successfully."
            else:
                msg = f"⚠️ Could not cancel order `{order_id}`. It may already be filled or cancelled."
            await message.reply_text(msg, parse_mode="Markdown")

        except Exception as exc:
            self._log.exception("cmd_cancel_error", error=str(exc))
            await message.reply_text(f"⚠️ Error cancelling order: {exc}")

    # ------------------------------------------------------------------
    # AI-powered commands
    # ------------------------------------------------------------------

    async def cmd_exchange(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show Bybit connection status (adapter, environment, connectivity)."""
        message, user = self._narrow(update)

        self._log.info("cmd_exchange", user=user.id)

        adapter = None
        if self._orchestrator is not None:
            adapter = getattr(self._orchestrator, "_exchange_adapter", None)
        if adapter is None:
            await message.reply_text(
                "ℹ️ *Exchange Status*\n\nExchange adapter not initialized.",
                parse_mode="Markdown",
            )
            return

        name = type(adapter).__name__
        testnet = bool(getattr(adapter, "is_testnet", False))
        connected = bool(
            getattr(adapter, "is_connected", False)
            if not callable(getattr(adapter, "is_connected", False))
            else adapter.is_connected()
        )
        env = "testnet" if testnet else "LIVE"
        state = "🟢 connected" if connected else "🔴 disconnected"
        msg = (
            "ℹ️ *Exchange Status*\n\n"
            f"*Adapter:* `{name}` (Bybit USDT perpetual)\n"
            f"*Environment:* {env}\n"
            f"*State:* {state}\n"
        )
        await message.reply_text(msg, parse_mode="Markdown")

    # ------------------------------------------------------------------
    # Execute conversation (multi-step)
    # ------------------------------------------------------------------

    def get_execute_conversation_handler(self) -> ConversationHandler:
        """Return the ``ConversationHandler`` for the /execute flow.

        Operator-only: running a strategy submits real orders against the
        operator's own exchange account, so a bound *tenant* chat (a
        control-plane customer) is refused even though it passes the normal
        binding gate.  The confirmation card always states the execution
        environment (testnet / live / dry-run) and the actual ``dry_run``
        flag is forwarded to the orchestrator instead of a hardcoded
        ``False``.
        """

        async def execute_start(
            update: Update, context: ContextTypes.DEFAULT_TYPE
        ) -> int:
            """Start the execute flow — show strategy picker."""
            message, user = self._narrow(update)
            self._log.info("execute_start", user=user.id)

            operator = await self.require_operator(message.chat_id)
            if operator is None:
                await message.reply_text(
                    "⛔ Only the account owner can run strategies against this "
                    "deployment. The trading cycle runs automatically — use "
                    "`/status` and `/risk` to monitor it."
                )
                return ConversationHandler.END

            # Cooldown: /execute is a manual order trigger.  The command
            # handler is registered without the rate-limit wrapper, so the
            # cooldown is enforced here.
            remaining = self._check_rate_limit(user.id, "execute")
            if remaining is not None:
                await message.reply_text(
                    f"Please wait {remaining}s before running a strategy again."
                )
                return ConversationHandler.END

            from quad.strategy.base import StrategyRegistry

            strategies = StrategyRegistry.list()
            if not strategies:
                await message.reply_text(
                    "⚠️ No strategies are registered.", parse_mode="Markdown"
                )
                return ConversationHandler.END

            keyboard = [
                [
                    InlineKeyboardButton(
                        s.replace("_", " ").title(), callback_data=f"exec_strat_{s}"
                    )
                ]
                for s in strategies
            ]
            keyboard.append(
                [InlineKeyboardButton("Cancel", callback_data="exec_cancel")]
            )
            reply_markup = InlineKeyboardMarkup(keyboard)

            _, _, env_label = self._execution_environment()
            await message.reply_text(
                f"🎯 *Execute Strategy*\n\nEnvironment: {env_label}\n\n"
                "Select a strategy to execute:",
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

            if query.data is None:
                return ConversationHandler.END
            strategy_name = query.data.replace("exec_strat_", "")
            user_data = context.user_data
            assert user_data is not None
            user_data["execute_strategy"] = strategy_name

            from quad.strategy.base import StrategyRegistry

            cls = StrategyRegistry.get(strategy_name)
            params_info = ""
            if cls is not None:
                spec = cls.get_params_spec()
                if spec:
                    param_lines = [f"  • `{p.name}`: {p.description}" for p in spec]
                    params_info = "\n" + "\n".join(param_lines)

            _, _, env_label = self._execution_environment()

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
                f"Environment: {env_label}\n\n"
                "Proceed? This evaluates the strategy against current market "
                "data and submits orders for any signals it generates."
            )
            await query.edit_message_text(
                msg, parse_mode="Markdown", reply_markup=reply_markup
            )
            return CONFIRMING_EXECUTION

        async def execute_confirm(
            update: Update, context: ContextTypes.DEFAULT_TYPE
        ) -> int:
            """Confirmed — execute via orchestrator."""
            # This is a CallbackQueryHandler, so `update.message` is always
            # None (the message lives on `update.callback_query.message`).
            # Calling `_narrow()` here raised TypeError on every confirm
            # click, i.e. /execute could never complete.  Read the sender
            # straight off the callback query instead.
            query = update.callback_query
            if query is None:
                return ConversationHandler.END
            user = query.from_user
            await query.answer()

            _chat = self._callback_chat_id(query)

            # Re-check operator at confirm time: the chat that started the
            # flow may differ from the one clicking confirm.
            if await self.require_operator(_chat) is None:
                await query.edit_message_text(
                    "⛔ Only the account owner can run strategies against this "
                    "deployment."
                )
                return ConversationHandler.END

            if query.data == "exec_cancel":
                await query.edit_message_text("✅ Execution cancelled.")
                return ConversationHandler.END

            user_data = context.user_data
            assert user_data is not None
            strategy_name = user_data.get("execute_strategy", "unknown")

            try:
                await query.edit_message_text(
                    f"⏳ Executing `{strategy_name}`...", parse_mode="Markdown"
                )

                # Forward the bot's real dry-run state.  Hardcoding False
                # meant a dry-run bot still attempted real submissions.
                _, is_dry_run, _ = self._execution_environment()

                # Execute via orchestrator (if available)
                if self._orchestrator is not None:
                    exec_method = getattr(self._orchestrator, "execute_strategy", None)
                    if exec_method is not None:
                        result = await exec_method(
                            strategy_name=strategy_name, dry_run=is_dry_run
                        )
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
                                parts.append(
                                    f"\n*Actions generated:* {result.get('actions_count', len(action_infos))}"
                                )
                                for a in action_infos[:5]:
                                    parts.append(
                                        f"  • `{a.get('type', '?')}` {a.get('contract', '')} {a.get('side', '')}"
                                    )
                            if executed:
                                parts.append("\n*Execution results:*")
                                for e in executed[:5]:
                                    e_status = e.get(
                                        "result", e.get("error", "submitted")
                                    )
                                    parts.append(
                                        f"  • `{e.get('action', '?')}` → {e_status}"
                                    )
                            await query.edit_message_text(
                                "\n".join(parts),
                                parse_mode="Markdown",
                            )
                        self._log.info(
                            "execute_complete",
                            strategy=strategy_name,
                            user=user.id,
                            dry_run=is_dry_run,
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
                self._log.exception(
                    "execute_error", strategy=strategy_name, error=str(exc)
                )
                await query.edit_message_text(f"⚠️ Execution error: {exc}")

            user_data.pop("execute_strategy", None)
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
                    CallbackQueryHandler(
                        execute_confirm, pattern=r"^(exec_confirm|exec_cancel)$"
                    )
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
        self, update: object, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Log errors and notify the admin chat."""
        # An update with no message/sender is expected traffic (inline-mode
        # callbacks, channel posts), not a fault.  Log at debug and drop it
        # rather than paging the admin on every occurrence.
        if isinstance(context.error, UpdateNotAddressable):
            self._log.debug(
                "bot_update_not_addressable",
                error=str(context.error),
                update_id=getattr(update, "update_id", None),
            )
            return

        self._log.error(
            "bot_error",
            error=str(context.error),
            update_id=getattr(update, "update_id", None),
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
