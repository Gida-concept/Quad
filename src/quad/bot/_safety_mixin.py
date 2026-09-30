"""Emergency-stop Telegram commands: /kill and its confirmation callback.

Extracted verbatim from ``commands.py`` -- all 3 methods' ASTs are unchanged;
only the enclosing class moved.  ``QuadBotCommands`` keeps each command with its
original signature, and the bot registers handlers via ``getattr`` on the
instance, so PTB wiring is unaffected.

These belong together because ``/kill`` is a two-step, operator-only flow: the
command arms it, the callback confirms it, and both funnel through the same
``_cancel_all_open_orders`` that unions the exchange view with the gateway's
in-memory view.
"""

from __future__ import annotations

from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes


class SafetyMixin:
    """Arm, confirm, and execute the kill switch.

    The attributes below are the state this mixin *consumes*.  They are
    declared rather than assigned, so the mixin documents the contract it
    expects from its host class without owning it.  ``_narrow``,
    ``_callback_chat_id`` and ``get_bound_tenant`` come from ``QuadBotCommands``.
    """

    _log: Any
    _orchestrator: Any
    _execution_engine: Any
    _risk_manager: Any
    _narrow: Any
    _callback_chat_id: Any
    get_bound_tenant: Any

    async def _cancel_all_open_orders(self) -> tuple[int, int, list[str]]:
        """Cancel every open order on the exchange.

        Used by ``/kill``: the kill switch halts *new* entries, but leaving
        resting orders (and especially resting stop/limit entries) live on
        the exchange would mean the bot is not actually stopped.

        Returns
        -------
        tuple[int, int, list[str]]
            ``(cancelled, failed, errors)`` — counts plus a short reason per
            failed order, so the confirmation message reports the truth
            instead of an unconditional "all cancelled".
        """
        adapter = getattr(self._orchestrator, "_exchange_adapter", None)
        orders: list = []

        # 1. Exchange is the source of truth (it knows about orders this
        #    process never saw, e.g. after a restart).
        if adapter is not None:
            try:
                orders = await adapter.get_open_orders()
            except Exception as exc:
                self._log.warning("kill_open_orders_fetch_failed", error=str(exc))

        # 2. Union with the gateway's in-memory view so orders submitted but
        #    not yet visible to the exchange REST call are still covered.
        if self._execution_engine is not None:
            try:
                tracked = self._execution_engine._gateway.get_active_orders()
            except Exception as exc:
                self._log.debug("kill_gateway_orders_unavailable", error=str(exc))
                tracked = []
            seen = {
                (str(getattr(o, "symbol", "")), str(getattr(o, "id", "")))
                for o in orders
            }
            for o in tracked:
                key = (str(getattr(o, "symbol", "")), str(getattr(o, "id", "")))
                if key not in seen:
                    orders.append(o)
                    seen.add(key)

        if not orders:
            return 0, 0, []

        cancelled = 0
        failed = 0
        errors: list[str] = []
        for order in orders:
            order_id = str(getattr(order, "id", "") or "")
            symbol = str(getattr(order, "symbol", "") or "")
            if not order_id:
                failed += 1
                errors.append(f"{symbol or '?'}: missing order id")
                continue
            try:
                ok = False
                if self._execution_engine is not None:
                    client_id = str(getattr(order, "client_order_id", "") or "")
                    # Prefer the exchange order id; fall back to the gateway
                    # (client-order-id keyed) path when only that is known.
                    if adapter is not None:
                        ok = bool(await adapter.cancel_order(order_id, symbol))
                    if not ok and client_id:
                        ok = bool(await self._execution_engine.cancel_order(client_id))
                elif adapter is not None:
                    ok = bool(await adapter.cancel_order(order_id, symbol))
                if ok:
                    cancelled += 1
                    self._log.info(
                        "kill_order_cancelled",
                        order_id=order_id,
                        symbol=symbol,
                    )
                else:
                    failed += 1
                    errors.append(f"{symbol or '?'}/{order_id}: exchange refused")
                    self._log.warning(
                        "kill_order_cancel_rejected",
                        order_id=order_id,
                        symbol=symbol,
                    )
            except Exception as exc:
                failed += 1
                errors.append(f"{symbol or '?'}/{order_id}: {exc}")
                self._log.warning(
                    "kill_order_cancel_failed",
                    order_id=order_id,
                    symbol=symbol,
                    error=str(exc),
                )

        return cancelled, failed, errors

    async def cmd_kill(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Emergency kill switch activation.

        Requires a confirmation via inline keyboard.
        """
        message, user = self._narrow(update)

        self._log.info("cmd_kill", user=user.id)

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
            "• Cancel all open orders on the exchange\n"
            "• Place no new trades\n"
            "• Not close existing positions (manual action required)\n\n"
            "*This action cannot be undone via Telegram.*"
        )
        await message.reply_text(msg, parse_mode="Markdown", reply_markup=reply_markup)

    async def cmd_kill_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle kill switch confirmation callback."""
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        user = query.from_user

        # Binding check: callbacks bypass the CommandHandler gate in bot.py.
        try:
            chat_id = self._callback_chat_id(query)
            bound = (
                await self.get_bound_tenant(chat_id) if chat_id is not None else None
            )
        except Exception:
            bound = None
        if bound is None:
            await query.edit_message_text(
                "🔒 This chat isn't linked to a Quad account. "
                "Send `/start YOURCODE` first."
            )
            return
        # Operator-only: the kill switch halts the operator's own account.
        if bound != "__operator__":
            await query.edit_message_text(
                "⛔ Only the account owner can trigger the kill switch."
            )
            self._log.warning(
                "kill_switch_denied_non_operator",
                user=user.id,
                tenant=bound,
            )
            return

        if query.data == "kill_confirm":
            reason = "Kill switch triggered via Telegram by admin"
            await query.edit_message_text(
                "🚨 *Kill Switch*\n\nHalting new trades and cancelling open orders…",
                parse_mode="Markdown",
            )
            try:
                # 1. Halt new entries FIRST so nothing new is submitted while
                #    we are cancelling.
                triggered = False
                if self._risk_manager is not None:
                    self._risk_manager.trigger_kill_switch(reason)
                    triggered = True
                elif self._orchestrator is not None:
                    self._log.warning(
                        "kill_switch: risk_manager is None, falling back to orchestrator"
                    )
                    ks = getattr(self._orchestrator, "trigger_kill_switch", None)
                    if ks is not None:
                        ks(reason)
                        triggered = True
                if not triggered:
                    self._log.error(
                        "kill_switch: both risk_manager and orchestrator are None"
                    )

                # 2. Actually cancel resting orders.  Previously this step did
                #    not exist while the reply claimed orders were cancelled.
                cancelled, failed, errors = await self._cancel_all_open_orders()

                if failed == 0:
                    order_line = (
                        f"Open orders cancelled: *{cancelled}*."
                        if cancelled
                        else "No open orders to cancel."
                    )
                else:
                    order_line = (
                        f"Open orders cancelled: *{cancelled}* — "
                        f"*⚠️ {failed} could not be cancelled*:\n"
                        + "\n".join(f"  • `{e}`" for e in errors[:8])
                    )

                self._log.warning(
                    "kill_switch_activated_via_telegram",
                    user=user.id,
                    orders_cancelled=cancelled,
                    orders_failed=failed,
                    new_entries_halted=triggered,
                )

                await query.edit_message_text(
                    "🚨 *Kill Switch Activated*\n\n"
                    "• New entries: *halted*\n"
                    f"• {order_line}\n"
                    "• Existing positions: *remain open* — manage them manually.",
                    parse_mode="Markdown",
                )

            except Exception as exc:
                self._log.exception("kill_switch_error", error=str(exc))
                await query.edit_message_text(f"⚠️ Error activating kill switch: {exc}")

        else:
            await query.edit_message_text("✅ Kill switch cancelled.")
