"""Account-configuration Telegram commands: /leverage, /position_mode, /settings, /set.

Extracted verbatim from ``commands.py`` -- all 4 methods' ASTs are unchanged;
only the enclosing class moved.  ``QuadBotCommands`` keeps each command with its
original signature, and the bot registers handlers via ``getattr`` on the
instance, so PTB wiring is unaffected.

These belong together because they are the only commands that *write* runtime
state (leverage, position mode, config keys), and each carries the same two
guards: an operator check and the ``/set`` key allowlist.
"""

from __future__ import annotations

import json as _json
import re as _re
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

# ---------------------------------------------------------------------------
# /set allowlist: only non-sensitive runtime tunables may change via Telegram.
# Everything else (credentials, wiring, prompt overrides, mode switches) is
# operator-only or fully blocked.
# ---------------------------------------------------------------------------

#: Non-sensitive tunables settable via ``/set`` by any bound chat.
SAFE_SET_KEYS = frozenset(
    {
        "trading.leverage",
        "trading.margin_mode",
        "trading.position_mode",
        "trading.serial_trade_mode",
        "ai.enabled",
        "risk.max_drawdown_pct",
        "risk.max_funding_rate_cost",
        "risk.min_distance_to_liquidation_pct",
    }
)

#: Key prefixes / exact keys that are never settable via ``/set``.
BLOCKED_SET_PREFIXES = ("exchange.", "telegram.")
BLOCKED_SET_KEYS = frozenset(
    {
        "ai.system_prompt_override",
        "exchange.testnet",
    }
)

#: Sensitive keys an operator (notification chat) may still change.
OPERATOR_SET_KEYS = frozenset(
    {
        "trading.trade_capital_usd",
        "trading.leverage",
        "ai.system_prompt_override",
    }
)

#: Hard guardrails for numeric tunables even when the key is allowed.
MAX_SET_LEVERAGE = 10
MAX_SET_TRADE_CAPITAL_USD = 10_000


class AccountConfigCommandsMixin:
    """Commands that mutate leverage, position mode, or runtime config.

    The attributes below are the state this mixin *consumes*.  They are
    declared rather than assigned, so the mixin documents the contract it
    expects from its host class without owning it.  ``_narrow``,
    ``_safe_reply``, ``get_bound_tenant`` and ``require_operator`` come from
    ``QuadBotCommands``.
    """

    _log: Any
    _config: Any
    _orchestrator: Any
    _narrow: Any
    _safe_reply: Any
    get_bound_tenant: Any
    require_operator: Any

    async def cmd_leverage(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """View or set leverage for a symbol.

        Usage: ``/leverage [symbol] [value]``

        The write path is operator-only and really calls
        ``adapter.set_leverage``; it previously only echoed the requested
        value back, so the command silently did nothing.
        """
        message, user = self._narrow(update)

        self._log.info("cmd_leverage", user=user.id)

        try:
            trading_config = self._config.get("trading", {})
            risk_config = self._config.get("risk", {})
            default_leverage = trading_config.get("leverage")
            max_leverage = int(risk_config.get("max_leverage", 50) or 50)
            adapter = (
                getattr(self._orchestrator, "_exchange_adapter", None)
                if self._orchestrator
                else None
            )

            if not context.args:
                live = ""
                if adapter is not None:
                    live = (
                        "\n*Exchange:* connected"
                        if getattr(adapter, "is_connected", False)
                        else "\n*Exchange:* ⚠️ not connected"
                    )
                msg = (
                    f"⚙️ *Leverage*\n\n"
                    f"*Default Leverage:* `{default_leverage}x`\n"
                    f"*Max Leverage:* `{max_leverage}x`"
                    f"{live}\n\n"
                    "Usage:\n"
                    "• `/leverage SYMBOL` — Show current leverage for a symbol\n"
                    "• `/leverage SYMBOL VALUE` — Set leverage on the exchange"
                )
                await message.reply_text(msg, parse_mode="Markdown")
                return

            symbol = context.args[0].upper()
            if not _re.fullmatch(r"[A-Z0-9]{4,30}", symbol):
                await message.reply_text("⚠️ Invalid symbol format.")
                return

            if len(context.args) >= 2:
                try:
                    requested = int(context.args[1])
                except ValueError:
                    await message.reply_text(
                        "⚠️ Leverage value must be an integer.",
                        parse_mode="Markdown",
                    )
                    return
                if requested < 1:
                    await message.reply_text(
                        "⚠️ Leverage must be at least 1.",
                        parse_mode="Markdown",
                    )
                    return

                clamped = min(requested, max_leverage)
                note = ""
                if clamped != requested:
                    note = f"\n*Clamped to `{clamped}x` by `risk.max_leverage`.*"

                if await self.require_operator(message.chat_id) is None:
                    await message.reply_text(
                        "⛔ Only the account owner can change leverage."
                    )
                    return
                if adapter is None:
                    await message.reply_text(
                        "⚠️ Exchange adapter is not available — leverage was "
                        "**not** changed.",
                        parse_mode="Markdown",
                    )
                    return

                self._log.info(
                    "cmd_leverage_set",
                    user=user.id,
                    symbol=symbol,
                    leverage=clamped,
                )
                try:
                    await adapter.set_leverage(symbol, clamped)
                except Exception as exc:
                    self._log.warning(
                        "cmd_leverage_set_failed",
                        symbol=symbol,
                        leverage=clamped,
                        error=str(exc),
                    )
                    await message.reply_text(
                        f"⚠️ Exchange rejected `{clamped}x` for `{symbol}`:\n"
                        f"`{exc}`\n\n"
                        "Common causes: an open position on the symbol, or the "
                        "symbol's risk-limit tier. Close the position or lower "
                        "the value.",
                        parse_mode="Markdown",
                    )
                    return
                await message.reply_text(
                    f"✅ Leverage for `{symbol}` set to `{clamped}x`.{note}",
                    parse_mode="Markdown",
                )
                return

            # 1 arg — show leverage info for the symbol
            live_leverage = None
            if adapter is not None:
                try:
                    positions = await adapter.get_positions()
                    for pos in positions:
                        p_symbol = str(
                            getattr(pos, "symbol", "")
                            or getattr(pos, "contract_symbol", "")
                        )
                        if p_symbol == symbol:
                            live_leverage = getattr(pos, "leverage", None)
                            break
                except Exception as exc:
                    self._log.debug("cmd_leverage_read_failed", error=str(exc))

            live_line = (
                f"\n*Exchange Leverage:* `{live_leverage}x`"
                if live_leverage
                else "\n_Exchange reports leverage per open position — open the "
                "symbol to see it._"
            )
            msg = (
                f"⚙️ *Leverage — {symbol}*\n\n"
                f"*Config Leverage:* `{default_leverage}x`\n"
                f"*Max Allowed:* `{max_leverage}x`{live_line}\n\n"
                f"To set: `/leverage {symbol} <value>`"
            )
            await message.reply_text(msg, parse_mode="Markdown")

        except Exception as exc:
            self._log.exception("cmd_leverage_error", error=str(exc))
            await message.reply_text(f"⚠️ Error: {exc}")

    async def cmd_position_mode(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """View or set position mode (one_way / hedge).

        Usage: ``/position_mode [mode]``

        The write path is operator-only and really calls
        ``adapter.set_position_mode``; it previously only echoed the
        requested value back.
        """
        message, user = self._narrow(update)

        self._log.info("cmd_position_mode", user=user.id)

        try:
            trading_config = self._config["trading"]
            configured = trading_config.get("position_mode")
            adapter = (
                getattr(self._orchestrator, "_exchange_adapter", None)
                if self._orchestrator
                else None
            )
            current = configured
            if adapter is not None:
                try:
                    current = await adapter.get_position_mode()
                except Exception as exc:
                    self._log.debug("cmd_position_mode_read_failed", error=str(exc))

            if not context.args:
                msg = (
                    f"⚙️ *Position Mode*\n\n"
                    f"*Config Mode:* `{configured}`\n"
                    f"*Exchange Mode:* `{current}`\n"
                    f"*Valid Modes:* `one_way`, `hedge`\n\n"
                    "Usage: `/position_mode one_way` or `/position_mode hedge`\n\n"
                    "_Switching requires all positions to be closed first._"
                )
                await message.reply_text(msg, parse_mode="Markdown")
                return

            requested = context.args[0].lower()
            if requested not in ("one_way", "hedge"):
                await message.reply_text(
                    "⚠️ Invalid mode. Use `one_way` or `hedge`.",
                    parse_mode="Markdown",
                )
                return

            if await self.require_operator(message.chat_id) is None:
                await message.reply_text(
                    "⛔ Only the account owner can change the position mode."
                )
                return

            if adapter is None:
                await message.reply_text(
                    "⚠️ Exchange adapter is not available — the position mode "
                    "was **not** changed.",
                    parse_mode="Markdown",
                )
                return

            if requested == current:
                await message.reply_text(
                    f"ℹ️ The exchange is already in `{requested}` mode."
                )
                return

            # A non-empty position set makes Bybit reject the switch; check
            # first so the operator gets a clear reason instead of a code.
            try:
                open_positions = await adapter.get_positions()
            except Exception:
                open_positions = []
            if open_positions:
                await message.reply_text(
                    f"⚠️ Close all positions before switching to `{requested}`. "
                    f"*{len(open_positions)} position(s) still open.*",
                    parse_mode="Markdown",
                )
                return

            self._log.info(
                "cmd_position_mode_set",
                user=user.id,
                mode=requested,
            )
            try:
                await adapter.set_position_mode(requested)
            except Exception as exc:
                self._log.warning(
                    "cmd_position_mode_set_failed",
                    mode=requested,
                    error=str(exc),
                )
                await message.reply_text(
                    f"⚠️ Exchange rejected the switch to `{requested}`:\n`{exc}`",
                    parse_mode="Markdown",
                )
                return

            await message.reply_text(
                f"✅ Exchange position mode set to `{requested}`.\n\n"
                f"⚠️ `trading.position_mode` in `config/config.yaml` is still "
                f"`{configured}` — the bot does not rewrite its own config. "
                "Update it so restarts agree with the exchange.",
                parse_mode="Markdown",
            )

        except Exception as exc:
            self._log.exception("cmd_position_mode_error", error=str(exc))
            await message.reply_text(f"⚠️ Error: {exc}")

    async def cmd_settings(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show current configuration tree or key settings overview."""
        message, user = self._narrow(update)

        self._log.info("cmd_settings", user=user.id)

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
            await message.reply_text(f"⚠️ Error fetching settings: {exc}")

    async def cmd_set(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Set a config value at runtime.

        Usage: /set <key> <value>
        Example: /set trading.leverage 5
        Example: /set risk.max_funding_rate_cost 0.001
        Example: /set risk.max_drawdown_pct 15
        """
        message, user = self._narrow(update)
        self._log.info("cmd_set", user=user.id, args=context.args)

        if not context.args or len(context.args) < 2:
            await message.reply_text(
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

        # --- /set authorization: allowlist + blocklist + guardrails --------
        blocked = key in BLOCKED_SET_KEYS or key.startswith(BLOCKED_SET_PREFIXES)
        if blocked or key not in SAFE_SET_KEYS:
            bound = await self.get_bound_tenant(message.chat_id)
            if bound != "__operator__" or (blocked and key not in OPERATOR_SET_KEYS):
                await message.reply_text(
                    f"🔒 `{key}` cannot be changed via `/set`.",
                    parse_mode="Markdown",
                )
                return
        if key == "trading.leverage":
            try:
                lev = int(value_raw)
            except (TypeError, ValueError):
                lev = None
            if lev is None or lev < 1 or lev > MAX_SET_LEVERAGE:
                await message.reply_text(
                    f"⚠️ Leverage must be 1–{MAX_SET_LEVERAGE}.",
                    parse_mode="Markdown",
                )
                return
        if key == "trading.trade_capital_usd":
            try:
                cap = float(value_raw)
            except (TypeError, ValueError):
                cap = None
            if cap is None or cap <= 0 or cap > MAX_SET_TRADE_CAPITAL_USD:
                await message.reply_text(
                    f"⚠️ Trade capital must be 0–{MAX_SET_TRADE_CAPITAL_USD} USD.",
                    parse_mode="Markdown",
                )
                return

        # Parse value type
        value: int | float | bool | str
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
                await message.reply_text(
                    "⚠️ Orchestrator is not available.",
                    parse_mode="Markdown",
                )
                return

            config_mgr = getattr(self._orchestrator, "_config_manager", None)
            if config_mgr is None:
                await message.reply_text(
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

            await message.reply_text(msg, parse_mode="Markdown")

        except Exception as exc:
            await message.reply_text(
                f"❌ Error setting `{key}`: {exc}",
                parse_mode="Markdown",
            )
