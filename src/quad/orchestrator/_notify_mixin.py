"""Telegram trade / circuit-breaker notifications for the orchestrator.

Extracted verbatim from ``orchestrator.py`` -- every method's AST is unchanged;
only the enclosing class moved.  ``QuadOrchestrator`` keeps all five methods
with their original signatures, so nothing outside this package can tell the
difference.
"""

from __future__ import annotations

import html as _html
from decimal import Decimal
from typing import Any


class NotifyMixin:
    """Side/PnL formatting helpers and the Telegram send paths.

    The attributes below are the state this mixin *consumes*.  They are
    declared rather than assigned, so the mixin documents the contract it
    expects from its host class without owning it -- ``QuadOrchestrator``
    remains the single place the state is created.
    """

    _log: Any
    _telegram_bot: Any
    _telegram_chat_id: Any

    @staticmethod
    def _side_label(side: Any) -> str:
        """Normalize a position/order side to ``LONG``/``SHORT``/``BUY``/``SELL``.

        Positions carry ``PositionSide.LONG`` enums while order sides
        are ``BUY``/``SELL`` strings; the Telegram alert should never show the
        raw ``PositionSide.LONG`` repr.
        """
        if side is None:
            return ""
        from quad.types.domain import PositionSide as PS

        if isinstance(side, PS):
            return side.value
        text = str(side).strip()
        if text.startswith("PositionSide."):
            text = text.split(".", 1)[1]
        return text.upper()

    @staticmethod
    def _compute_position_pnl(
        entry_price: Decimal | None,
        exit_price: Decimal | None,
        quantity: Decimal,
        side: str,
    ) -> Decimal:
        """Realized PnL for a closed position in quote (USDT) terms.

        ``(exit - entry) * qty`` for LONG, ``(entry - exit) * qty`` for
        SHORT.  Returns ``Decimal(0)`` when the prices are unavailable so a
        close never fails on missing data.
        """
        try:
            if entry_price is None or exit_price is None or not quantity:
                return Decimal(0)
            is_long = str(side or "").strip().upper() in ("BUY", "LONG")
            diff = exit_price - entry_price
            if not is_long:
                diff = -diff
            return diff * quantity
        except Exception:
            return Decimal(0)

    @staticmethod
    def _format_pnl(pnl: Decimal, entry_price: Decimal | None) -> str:
        """Format realized PnL as ``$x.xx (y.y%)``.

        The percentage is relative to the entry notional (``entry * qty`` is
        unavailable here, so it is relative to the entry price instead);
        pass ``None`` to omit the percentage.
        """
        try:
            amount = f"${float(pnl):,.2f}"
            if entry_price and entry_price > 0:
                pct = float(pnl) / float(entry_price) * 100.0
                return f"{amount} ({pct:+.2f}%)"
            return amount
        except Exception:
            return f"${float(pnl):,.2f}"

    async def _notify_trade(
        self,
        action_type: str,
        strategy: str,
        contract: str,
        side: str,
        quantity: str,
        price: str | None,
        reason: str,
        pnl: str | None = None,
        stop_loss: Decimal | None = None,
        take_profit: Decimal | None = None,
    ) -> None:
        """Send trade notification via Telegram."""
        if not getattr(self, "_telegram_bot", None) or not getattr(
            self, "_telegram_chat_id", 0
        ):
            return
        try:
            emoji = {
                "ENTER": "\U0001f7e2",
                "EXIT": "\U0001f534",
                "ADJUST": "\U0001f7e1",
                "ROLL": "\U0001f504",
            }.get(action_type, "⚪")
            esc = _html.escape
            msg = (
                f"{emoji} <b>{esc(action_type)}</b> | {esc(strategy)}\n"
                f"Contract: <code>{esc(contract)}</code>\n"
                f"Side: {esc(side)} | Qty: {esc(quantity)}\n"
                f"Price: {esc(price or 'MARKET')}\n"
            )
            if stop_loss is not None and take_profit is not None:
                msg += (
                    f"SL: <code>{esc(str(stop_loss))}</code> | "
                    f"TP: <code>{esc(str(take_profit))}</code>\n"
                )
            if pnl:
                msg += f"PnL: {esc(pnl)}\n"
            msg += f"Reason: {esc(reason)}"
            await self._telegram_bot.send_message(
                chat_id=self._telegram_chat_id,
                text=msg,
                parse_mode="HTML",
            )
        except Exception as exc:
            self._log.warning("telegram_notify_failed", error=str(exc))

    async def _notify_circuit_breaker(self, name: str, reason: str, tier: int) -> None:
        """Send circuit breaker alert via Telegram."""
        if not getattr(self, "_telegram_bot", None) or not getattr(
            self, "_telegram_chat_id", 0
        ):
            return
        try:
            esc = _html.escape
            msg = (
                f"\U0001f6a8 <b>Circuit Breaker Triggered</b>\n"
                f"Name: <code>{esc(name)}</code>\n"
                f"Tier: {esc(str(tier))}\n"
                f"Reason: {esc(reason)}"
            )
            await self._telegram_bot.send_message(
                chat_id=self._telegram_chat_id,
                text=msg,
                parse_mode="HTML",
            )
        except Exception as exc:
            self._log.warning("telegram_cb_notify_failed", error=str(exc))
