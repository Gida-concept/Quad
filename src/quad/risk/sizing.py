"""Position sizing using Fractional Kelly Criterion for futures trading.

Computes optimal position size based on historical win rate, average
win/loss ratio, a fractional Kelly multiplier, leverage adjustment,
and absolute portfolio limits.

All default values are sourced from ``config.yaml`` and the
Pydantic schema. This module only contains inline fallbacks as a
last resort — they match the canonical defaults and are never used
when the config system is properly set up.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import structlog

from quad.persistence.database import DatabaseManager
from quad.types.risk import Action
from quad.types.strategy import StrategyContext


class PositionSizer:
    """Position sizing using Fractional Kelly Criterion.

    Computes the optimal position size based on historical trade
    statistics (win rate, average win/loss), applies a conservative
    fractional multiplier, adjusts for leverage, and caps at
    portfolio-based and absolute limits.

    Parameters
    ----------
    config:
        Configuration dictionary. The risk sub-section is extracted via
        ``config.get('risk', config)``.
    db_manager:
        Optional database manager for loading historical trade data.
    """

    def __init__(
        self,
        config: dict[str, Any],
        db_manager: DatabaseManager | None = None,
    ) -> None:
        self._log = structlog.get_logger(__name__)
        self._db = db_manager

        self._cfg: dict[str, Any] = config.get("risk", {})

        # Sizing parameters — read from config dict with inline fallbacks
        # that match config.yaml / schema.py defaults.
        self._kelly_multiplier = float(
            self._cfg["kelly"]["fraction"]
        )
        self._default_fraction = float(
            self._cfg["kelly"]["default_fraction"]
        )
        self._max_pos_pct = float(
            self._cfg["max_position_size_pct"]
        )
        self._max_pos_usd = Decimal(
            str(self._cfg["max_position_size_usd"])
        )
        self._max_leverage = int(
            self._cfg["max_leverage"]
        )
        self._min_pos_usd = Decimal(
            str(self._cfg["min_position_size_usd"])
        )
        # Read trade_capital_usd from risk config, with strategy-level fallback
        risk_trade_capital = self._cfg.get("trade_capital_usd")
        if risk_trade_capital is not None:
            trade_capital = risk_trade_capital
        else:
            # Fallback: scan strategy configs for trade_capital_usd
            all_strategy_configs = config.get("strategy", {})
            trade_capital = 5
            for sc in all_strategy_configs.values():
                if isinstance(sc, dict):
                    tc = sc.get("trade_capital_usd")
                    if tc is not None:
                        trade_capital = tc
                        break
        self._trade_capital_usd = Decimal(str(trade_capital))
        self._sl_enabled = bool(
            self._cfg["per_position_sl"]["enabled"]
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def compute_size(
        self, action: Action, context: StrategyContext
    ) -> Action:
        """Return an Action with its quantity adjusted by Kelly sizing.

        The method:
        1. Extracts or computes win rate, avg win, and avg loss from
           context strategy parameters.
        2. Calculates the full Kelly fraction.
        3. Applies the fractional multiplier.
        4. Adjusts for leverage.
        5. Applies portfolio-based and absolute caps.
        6. Checks minimum position size.
        7. Returns a copy of the action with the adjusted quantity.

        Parameters
        ----------
        action:
            The proposed trading action.
        context:
            Current strategy execution context.

        Returns
        -------
        Action
            A new Action with a potentially adjusted quantity.
        """
        # Extract historical stats from context
        params = context.strategy_params or {}
        win_rate = float(params.get("win_rate", 0.0))
        avg_win = Decimal(str(params.get("avg_win", "0")))
        avg_loss = Decimal(str(params.get("avg_loss", "0")))

        portfolio_value = (
            context.account.total_usdt if context.account else Decimal(0)
        )

        if win_rate <= 0 or avg_win <= Decimal(0) or avg_loss <= Decimal(0):
            # Fall back to default fraction
            self._log.debug(
                "using_default_kelly_fraction",
                win_rate=win_rate,
                avg_win=str(avg_win),
                avg_loss=str(avg_loss),
            )
            sized_qty = self._default_size(portfolio_value)
        else:
            kelly_f = self._kelly_fraction(win_rate, avg_win, avg_loss)
            sized_qty = self._adjusted_kelly(kelly_f, portfolio_value)

        # Cap at the original requested quantity (don't oversize)
        if action.quantity > Decimal(0) and sized_qty > action.quantity:
            sized_qty = action.quantity

        # Ensure non-negative
        sized_qty = max(sized_qty, Decimal(0))

        # TP/SL size cap: ensure position isn't larger than what the bracket
        # stop-loss can protect given the trade capital
        tp_sl_max = self._max_size_from_tp_sl(action, portfolio_value)
        if sized_qty > tp_sl_max > Decimal(0):
            sized_qty = tp_sl_max

        self._log.debug(
            "position_sized",
            original_qty=str(action.quantity),
            adjusted_qty=str(sized_qty),
            kelly_fraction=self._kelly_multiplier,
            portfolio_value=str(portfolio_value),
        )

        return Action(
            type=action.type,
            strategy=action.strategy,
            symbol=action.symbol,
            # Preserve the contract (the symbol may be empty on paths that
            # only populate ``contract``, e.g. close-all / TradingView / some
            # strategy actions).  Without this the sized action would lose the
            # contract and the order would be built with an empty symbol.
            contract=action.contract,
            quantity=sized_qty.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            price=action.price,
            reason=action.reason,
            confidence=action.confidence,
            risk_checked=action.risk_checked,
            side=action.side,
            order_type=action.order_type,
            stop_loss_price=action.stop_loss_price,
            take_profit_price=action.take_profit_price,
            metadata={
                **action.metadata,
                "sizing_kelly_fraction": self._kelly_multiplier,
                "sizing_adjusted_qty": str(sized_qty),
            },
        )

    # ------------------------------------------------------------------
    # Kelly calculation
    # ------------------------------------------------------------------

    @staticmethod
    def _kelly_fraction(
        win_rate: float, avg_win: Decimal, avg_loss: Decimal
    ) -> float:
        """Compute the full Kelly fraction.

        Formula::

            f = p - (1 - p) * (avg_loss / avg_win)

        Where:
            p = win_rate (probability of winning)
            b = avg_win / avg_loss (payout ratio)

        Parameters
        ----------
        win_rate:
            Historical win rate as a decimal (0.0 to 1.0).
        avg_win:
            Average winning trade amount.
        avg_loss:
            Average losing trade amount (positive value).

        Returns
        -------
        float
            Full Kelly fraction. Returns 0.0 if inputs are invalid.
        """
        if win_rate <= 0.0 or win_rate >= 1.0:
            return 0.0
        if avg_win <= Decimal(0) or avg_loss <= Decimal(0):
            return 0.0

        # Compute payout ratio b = avg_win / avg_loss
        payout_ratio = float(avg_win / avg_loss)

        if payout_ratio <= 0:
            return 0.0

        # f = p - q/b  where q = 1 - p
        loss_prob = 1.0 - win_rate
        kelly_f = win_rate - (loss_prob / payout_ratio)

        return max(0.0, kelly_f)

    def _adjusted_kelly(
        self, kelly_f: float, portfolio_value: Decimal
    ) -> Decimal:
        """Apply fractional multiplier, leverage adjustment, and caps.

        Steps:
        1. Compute full Kelly amount: ``kelly_f * portfolio_value``.
        2. Apply fractional multiplier.
        3. Adjust for leverage: divide by max_leverage.
        4. Cap at ``max_position_size_pct`` of portfolio value.
        5. Cap at ``max_position_size_usd`` absolute limit.
        6. Never exceed portfolio value.
        7. Check minimum position size; use default if undersized.

        Parameters
        ----------
        kelly_f:
            Full Kelly fraction (0.0 to 1.0).
        portfolio_value:
            Total portfolio value in USDT.

        Returns
        -------
        Decimal
            Adjusted position size in USD notional value.
        """
        if portfolio_value <= Decimal(0):
            return Decimal(0)

        # Step 1 & 2: Fractional Kelly amount
        fraction = Decimal(str(self._kelly_multiplier))
        size = Decimal(str(kelly_f)) * fraction * portfolio_value

        # Step 3: Leverage adjustment (leverage multiplies exposure)
        leverage = Decimal(str(self._max_leverage))
        if leverage > Decimal(1):
            size = size / leverage

        # Step 4: Cap at percentage of portfolio
        max_pct = Decimal(str(self._max_pos_pct))
        pct_cap = max_pct * portfolio_value
        size = min(size, pct_cap)

        # Step 5: Cap at absolute USD limit
        size = min(size, self._max_pos_usd)

        # Step 6: Never exceed 100% of portfolio
        size = min(size, portfolio_value)

        # Step 7: Minimum position size check
        if size < self._min_pos_usd:
            self._log.debug(
                "sized_below_minimum",
                sized_value=str(size.quantize(Decimal("0.01"))),
                min_value=str(self._min_pos_usd),
            )
            size = self._default_size(portfolio_value)

        return max(size, Decimal(0))

    def _default_size(self, portfolio_value: Decimal) -> Decimal:
        """Compute default position size when no historical data is available.

        Uses the ``kelly.default_fraction`` config value as a percentage
        of the portfolio, capped at ``max_position_size_usd``.

        Returns the size as a USD notional value.
        """
        if portfolio_value <= Decimal(0):
            return Decimal(0)

        default_pct = Decimal(str(self._default_fraction))
        size = default_pct * portfolio_value

        size = min(size, self._max_pos_usd)

        size = max(size, self._min_pos_usd)

        return max(size, Decimal(0))

    # ------------------------------------------------------------------
    # TP/SL-aware sizing
    # ------------------------------------------------------------------

    def _max_size_from_tp_sl(
        self,
        action: Action,
        portfolio_value: Decimal,
    ) -> Decimal:
        """Compute max position size based on TP/SL ratio and trade capital.

        For a 2:1 TP/SL ratio with the user's $5 trade capital at 50x leverage:
        - SL = 30% of $5 = $1.50 risk
        - At 50x leverage, $1.50 risk means a 0.6% price move against you
        - Max size = trade_capital * leverage

        This prevents the position from being larger than what the
        per-position SL/TP can reasonably protect.

        Parameters
        ----------
        action:
            The proposed trading action (unused, kept for future flexibility).
        portfolio_value:
            Total portfolio value in USDT (unused, kept for future flexibility).

        Returns
        -------
        Decimal
            Maximum position size in USD notional. ``Decimal("Infinity")``
            if per-position SL is disabled.
        """
        risk_cfg = self._cfg["per_position_sl"]
        sl_enabled = risk_cfg["enabled"]
        if not sl_enabled:
            return Decimal("Infinity")

        trade_capital = self._trade_capital_usd
        max_leverage = Decimal(str(self._max_leverage))

        # At 50x leverage on $5 capital, notional = $5 * 50 = $250
        max_notional = trade_capital * max_leverage

        return max_notional

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def get_sizing_stats(self) -> dict[str, Any]:
        """Return current sizing parameters and configuration."""
        return {
            "kelly_fraction": self._kelly_multiplier,
            "default_fraction": self._default_fraction,
            "max_position_size_pct": self._max_pos_pct,
            "max_position_size_usd": str(self._max_pos_usd),
            "max_leverage": self._max_leverage,
            "min_position_size_usd": str(self._min_pos_usd),
        }
