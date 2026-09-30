"""Read-only market and account Telegram commands.

``/balance``, ``/positions``, ``/orders``, ``/funding_rate``, ``/book``,
``/liquidation_warnings`` and ``/market_regime`` -- extracted verbatim from
``commands.py``; all 7 methods' ASTs are unchanged, only the enclosing class
moved.  ``QuadBotCommands`` keeps each command with its original signature.

They belong together because they share the same contract: they only *read*
state (exchange adapter, market data engine) and render it, never mutate
anything.  That is the property that makes them safe to run without an
operator check, and it is worth keeping visible in one file.
"""

from __future__ import annotations

import time as _time
from decimal import Decimal
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from quad.risk.gates import effective_min_liquidation_distance


class MarketInfoCommandsMixin:
    """Read-only views over account, position, order and market state.

    The attributes below are the state this mixin *consumes*.  They are
    declared rather than assigned, so the mixin documents the contract it
    expects from its host class without owning it.  ``_narrow`` comes from
    ``QuadBotCommands``.
    """

    _log: Any
    _config: Any
    _orchestrator: Any
    _market_data_engine: Any
    _execution_engine: Any
    _narrow: Any

    async def cmd_balance(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Send account balances and total USDT value."""
        message, user = self._narrow(update)

        self._log.info("cmd_balance", user=user.id)

        try:
            # Fetch live account data from the exchange adapter
            exchange_adapter = (
                getattr(self._orchestrator, "_exchange_adapter", None)
                if self._orchestrator
                else None
            )
            if exchange_adapter is None:
                await message.reply_text(
                    "⚠️ Exchange adapter not available.", parse_mode="Markdown"
                )
                return

            try:
                account = await exchange_adapter.get_account()
            except Exception as exc:
                self._log.warning("balance_fetch_failed", error=str(exc))
                await message.reply_text(
                    f"⚠️ Error fetching balance: {exc}", parse_mode="Markdown"
                )
                return

            if account is None:
                # No account data available
                msg = (
                    "💰 *Account Balance*\n\n"
                    "No account data available. The bot may not be connected to the exchange."
                )
                await message.reply_text(msg, parse_mode="Markdown")
                return

            # Format balance info
            exchange = getattr(account, "exchange", "unknown")
            total_usdt = getattr(account, "total_usdt", Decimal(0))
            balances = getattr(account, "balances", {})

            lines = [f"💳 *Account Balance*  |  Exchange: {exchange}\n"]
            lines.append(
                f"```\n{'Asset':<10} {'Free':>14} {'Locked':>14} {'Total':>14}"
            )
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
            await message.reply_text(msg, parse_mode="Markdown")

        except Exception as exc:
            self._log.exception("cmd_balance_error", error=str(exc))
            await message.reply_text(f"⚠️ Error fetching balance: {exc}")

    async def cmd_positions(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """List open positions with PnL."""
        message, user = self._narrow(update)

        self._log.info("cmd_positions", user=user.id)

        try:
            positions: list[Any] = []
            exchange_adapter = (
                getattr(self._orchestrator, "_exchange_adapter", None)
                if self._orchestrator
                else None
            )
            if exchange_adapter is not None:
                try:
                    positions = await exchange_adapter.get_positions()
                except Exception as exc:
                    self._log.warning("cmd_positions_fetch_failed", error=str(exc))

            if not positions:
                msg = "📋 *Open Positions*\n\nNo open positions."
                await message.reply_text(msg, parse_mode="Markdown")
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
                mark = float(
                    getattr(pos, "mark_price", getattr(pos, "current_price", 0))
                )
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
            await message.reply_text(msg, parse_mode="Markdown")

        except Exception as exc:
            self._log.exception("cmd_positions_error", error=str(exc))
            await message.reply_text(f"⚠️ Error fetching positions: {exc}")

    async def cmd_orders(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """List open or pending orders."""
        message, user = self._narrow(update)

        self._log.info("cmd_orders", user=user.id)

        try:
            orders: list[Any] = []
            if self._execution_engine is not None:
                try:
                    orders = self._execution_engine.get_active_orders()
                except Exception as exc:
                    self._log.warning("orders_exec_error", error=str(exc))

            if not orders:
                msg = "📋 *Open Orders*\n\nNo open orders."
                await message.reply_text(msg, parse_mode="Markdown")
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
            await message.reply_text(msg, parse_mode="Markdown")

        except Exception as exc:
            self._log.exception("cmd_orders_error", error=str(exc))
            await message.reply_text(f"⚠️ Error fetching orders: {exc}")

    async def cmd_funding_rate(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show funding rate for one or all tracked symbols.

        Usage: ``/funding_rate [symbol]``
        """
        message, user = self._narrow(update)

        self._log.info("cmd_funding_rate", user=user.id)

        if self._market_data_engine is None:
            msg = "⚠️ Market data engine is not available."
            await message.reply_text(msg, parse_mode="Markdown")
            return

        try:
            if context.args:
                symbol = context.args[0].upper()
                fr = await self._market_data_engine.get_funding_rate(symbol)
                if fr is None:
                    msg = f"⚠️ No funding rate data available for `{symbol}`."
                    await message.reply_text(msg, parse_mode="Markdown")
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
                lines.append(
                    "```\n"
                    f"{'Symbol':<12} {'Rate':>10} {'Countdown':>14} {'Mark Price':>12}"
                )
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
                    lines.append(
                        f"{sym:<12} {rate_str:>10} {countdown:>14} {mark_str:>12}"
                    )

                lines.append("```")
                msg = "\n".join(lines)

            await message.reply_text(msg, parse_mode="Markdown")

        except Exception as exc:
            self._log.exception("cmd_funding_rate_error", error=str(exc))
            await message.reply_text(f"⚠️ Error fetching funding rates: {exc}")

    async def cmd_book(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show top 5 bid/ask levels from order book.

        Usage: ``/book <symbol>``
        """
        message, user = self._narrow(update)

        self._log.info("cmd_book", user=user.id)

        if not context.args:
            msg = "⚠️ Usage: `/book <symbol>`\nExample: `/book BTCUSDT`"
            await message.reply_text(msg, parse_mode="Markdown")
            return

        symbol = context.args[0].upper()

        if self._market_data_engine is None:
            msg = "⚠️ Market data engine is not available."
            await message.reply_text(msg, parse_mode="Markdown")
            return

        try:
            book = await self._market_data_engine.get_order_book(symbol)

            if book is None:
                msg = f"⚠️ No order book data available for `{symbol}`."
                await message.reply_text(msg, parse_mode="Markdown")
                return

            bids = book.get("bids", [])[:5]
            asks = book.get("asks", [])[:5]

            best_bid = float(bids[0][0]) if bids else 0.0
            best_ask = float(asks[0][0]) if asks else 0.0
            spread = best_ask - best_bid
            spread_pct = (spread / best_ask * 100) if best_ask > 0 else 0.0

            lines = [f"📊 *Order Book — {symbol}*\n"]

            lines.append(f"*Spread:* ${spread:.2f} ({spread_pct:.3f}%)\n")

            lines.append(f"```\n{'Bids':>24}     {'Asks':>24}")
            lines.append(f"{'Price':>12} {'Qty':>10}     {'Price':>12} {'Qty':>10}")
            lines.append("-" * 52)

            max_rows = max(len(bids), len(asks))
            for i in range(max_rows):
                bid_str = (
                    f"{float(bids[i][0]):>12.4f} {float(bids[i][1]):>10.4f}"
                    if i < len(bids)
                    else " " * 24
                )
                ask_str = (
                    f"{float(asks[i][0]):>12.4f} {float(asks[i][1]):>10.4f}"
                    if i < len(asks)
                    else " " * 24
                )
                lines.append(f"{bid_str}     {ask_str}")

            lines.append("```")
            msg = "\n".join(lines)
            await message.reply_text(msg, parse_mode="Markdown")

        except Exception as exc:
            self._log.exception("cmd_book_error", error=str(exc))
            await message.reply_text(f"⚠️ Error fetching order book: {exc}")

    async def cmd_liquidation_warnings(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Check all positions for proximity to liquidation."""
        message, user = self._narrow(update)

        self._log.info("cmd_liquidation_warnings", user=user.id)

        try:
            risk_config = self._config["risk"]

            exchange_adapter = (
                getattr(self._orchestrator, "_exchange_adapter", None)
                if self._orchestrator
                else None
            )
            if exchange_adapter is None:
                await message.reply_text(
                    "⚠️ Exchange adapter not available. Cannot check liquidation proximity.",
                    parse_mode="Markdown",
                )
                return

            positions = await exchange_adapter.get_positions()
            if not positions:
                await message.reply_text(
                    "✅ No open positions. Nothing to check.",
                    parse_mode="Markdown",
                )
                return

            at_risk = []
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
                        risk_config,
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
                    at_risk.append((symbol, side, lev, distance, liq, mark))

            if not at_risk:
                msg = (
                    f"✅ *Liquidation Check*\n\n"
                    f"No positions are near liquidation.\n"
                    f"*Threshold:* `{min_distance:.0%}` distance"
                )
                await message.reply_text(msg, parse_mode="Markdown")
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
            await message.reply_text(msg, parse_mode="Markdown")

        except Exception as exc:
            self._log.exception("cmd_liquidation_warnings_error", error=str(exc))
            await message.reply_text(f"⚠️ Error checking liquidation: {exc}")

    async def cmd_market_regime(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show funding rate landscape and volatility assessment."""
        message, user = self._narrow(update)

        self._log.info("cmd_market_regime", user=user.id)

        if self._market_data_engine is None:
            msg = "⚠️ Market data engine is not available."
            await message.reply_text(msg, parse_mode="Markdown")
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
                bias = (
                    "Bullish (positive funding)"
                    if avg_rate > 0
                    else "Bearish (negative funding)"
                    if avg_rate < 0
                    else "Neutral"
                )
                lines.append(f"*Average Rate:* {avg_rate:+.5f}%")
                lines.append(f"*Bias:* {bias}")
                lines.append(
                    f"*Positive / Negative:* {positive_count}/{negative_count}"
                )

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
                            vol_label = (
                                "High"
                                if range_pct > 5
                                else "Moderate"
                                if range_pct > 2
                                else "Low"
                            )
                            lines.append(f"*24h Change:* {change:+.2f}%")
                            lines.append(
                                f"*24h Range:* {range_pct:.1f}% ({vol_label} volatility)"
                            )
                except Exception as exc:
                    self._log.warning("market_regime_ticker_error", error=str(exc))

            msg = "\n".join(lines)
            await message.reply_text(msg, parse_mode="Markdown")

        except Exception as exc:
            self._log.exception("cmd_market_regime_error", error=str(exc))
            await message.reply_text(f"⚠️ Error: {exc}")
