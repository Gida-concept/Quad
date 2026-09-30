"""AI-facing Telegram commands: /analyze, /ai_strategy, /ai_status, /ai_decision.

Extracted verbatim from ``commands.py`` -- all 5 methods' ASTs are unchanged;
only the enclosing class moved.  ``QuadBotCommands`` keeps each command with
its original signature, and the bot registers handlers via ``getattr`` on the
instance, so PTB wiring is unaffected.

They belong together because they are the only commands that surface the AI
subsystem: its token/rate-limit budget (``_build_usage_bar``), its decisions,
and its per-request analysis.  ``_escape_md`` moved with them because these are
its only callers.
"""

from __future__ import annotations

import re as _re
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes


def _escape_md(text: str) -> str:
    """Escape Markdown metacharacters in untrusted (AI-generated) text."""
    try:
        from telegram.helpers import escape_markdown as _tg_escape

        return _tg_escape(text, version=1)
    except Exception:
        return _re.sub(r"([*_`\[\]])", r"\\\1", text)


class AiCommandsMixin:
    """Commands that surface the AI subsystem's analysis and decisions.

    The attributes below are the state this mixin *consumes*.  They are
    declared rather than assigned, so the mixin documents the contract it
    expects from its host class without owning it.  ``_narrow`` comes from
    ``QuadBotCommands``.
    """

    _log: Any
    _config: Any
    _orchestrator: Any
    _market_data_engine: Any
    _groq_client: Any
    _narrow: Any

    async def cmd_analyze(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Send AI-generated market analysis for configured underlyings."""
        message, user = self._narrow(update)

        self._log.info("cmd_analyze", user=user.id)

        if self._groq_client is None:
            msg = (
                "⚠️ AI analysis is not available.\n\n"
                "The Groq API key is not configured. Set `GROQ_API_KEY` "
                "in your `.env` file and restart the bot."
            )
            await message.reply_text(msg, parse_mode="Markdown")
            return

        if self._market_data_engine is None:
            msg = "⚠️ Market data engine is not available."
            await message.reply_text(msg, parse_mode="Markdown")
            return

        # Send initial "thinking" message
        status_msg = await message.reply_text(
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
                    mark_price = await self._market_data_engine.get_mark_price(
                        underlying
                    )
                    funding_rate = await self._market_data_engine.get_funding_rate(
                        underlying
                    )
                    order_book = await self._market_data_engine.get_order_book(
                        underlying
                    )
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
        message, user = self._narrow(update)

        self._log.info("cmd_ai_strategy", user=user.id)

        if self._groq_client is None:
            msg = (
                "⚠️ AI strategy recommendation is not available.\n\n"
                "The Groq API key is not configured. Set `GROQ_API_KEY` "
                "in your `.env` file and restart the bot."
            )
            await message.reply_text(msg, parse_mode="Markdown")
            return

        if self._market_data_engine is None:
            msg = "⚠️ Market data engine is not available."
            await message.reply_text(msg, parse_mode="Markdown")
            return

        status_msg = await message.reply_text(
            "🤔 Consulting Groq AI on strategy selection...",
            parse_mode="Markdown",
        )

        try:
            from quad.ai import recommend_strategy

            # Get data for the first configured underlying
            config = self._config
            underlyings = config["trading"]["underlyings"]
            underlying = next(iter(underlyings), "BTCUSDT")

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
        message, user = self._narrow(update)

        self._log.info("cmd_ai_status", user=user.id)

        if self._groq_client is None:
            msg = (
                "⚠️ AI trading system is not available.\n\n"
                "The Groq API key is not configured. Set `GROQ_API_KEY` "
                "in your `.env` file and restart the bot."
            )
            await message.reply_text(msg, parse_mode="Markdown")
            return

        try:
            stats = self._groq_client.stats
            orchestrator = self._orchestrator

            # Gather orchestrator AI info if available
            ai_info = {}
            if orchestrator is not None:
                status_dict = (
                    orchestrator.status() if hasattr(orchestrator, "status") else {}
                )
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

            last_error = ai_info.get("last_error")
            if last_error:
                msg += f"\n*Last Error:* `{last_error[:200]}`"

            await message.reply_text(msg, parse_mode="Markdown")

        except Exception as exc:
            self._log.exception("cmd_ai_status_error", error=str(exc))
            await message.reply_text(f"⚠️ AI status error: {exc}", parse_mode="Markdown")

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
        message, user = self._narrow(update)

        self._log.info("cmd_ai_decision", user=user.id)

        if self._groq_client is None:
            msg = (
                "⚠️ AI trading system is not available.\n\n"
                "The Groq API key is not configured. Set `GROQ_API_KEY` "
                "in your `.env` file and restart the bot."
            )
            await message.reply_text(msg, parse_mode="Markdown")
            return

        if not self._groq_client.is_available():
            msg = (
                "⚠️ AI rate limit reached.\n\n"
                "The daily request limit has been exhausted. "
                "The AI decision will be available after the window resets."
            )
            await message.reply_text(msg, parse_mode="Markdown")
            return

        if self._orchestrator is None:
            msg = "⚠️ Orchestrator is not available."
            await message.reply_text(msg, parse_mode="Markdown")
            return

        status_msg = await message.reply_text(
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
                    f"*Reasoning:*\n{_escape_md(str(reasoning)[:500])}",
                ]

                msg_text = "\n".join(p for p in msg_parts if p)
                await status_msg.edit_text(msg_text, parse_mode="Markdown")

                # Execute if action is ENTER or EXIT
                if action in ("ENTER", "EXIT") and hasattr(
                    self._orchestrator, "_execute_ai_action"
                ):
                    from quad.types.strategy import StrategyContext

                    strategy_context = StrategyContext(
                        account=account,
                        positions=positions,
                        futures_positions=positions,
                        orders=[],
                        funding_rates={},
                        config=self._config,
                    )
                    await self._orchestrator._execute_ai_action(
                        decision, strategy_context
                    )

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
