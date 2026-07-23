"""Position sizing using Fractional Kelly Criterion for options trading.

Computes optimal position size based on historical win rate, average
win/loss ratio, a fractional Kelly multiplier, and absolute portfolio
limits.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import structlog

from quad.types.risk import Action
from quad.types.strategy import StrategyContext
from quad.persistence.database import DatabaseManager


# Default configuration values
_DEFAULTS: dict[str, Any] = {
    "kelly": {"fraction": 0.25, "default_fraction": 0.02},
    "max_position_size_pct": 0.10,
    "max_position_size_usd": 10000,
}


class PositionSizer:
    """Position sizing using Fractional Kelly Criterion.

    Computes the optimal position size based on historical trade
    statistics (win rate, average win/loss), applies a conservative
    fractional multiplier, and caps at portfolio-based and absolute
    limits.

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

        raw = config.get("risk", config)
        self._cfg: dict[str, Any] = raw if isinstance(raw, dict) else config

        # Sizing parameters
        self._kelly_multiplier = float(
            self._cfg.get("kelly", {}).get(
                "fraction", _DEFAULTS["kelly"]["fraction"]
            )
        )
        self._default_fraction = float(
            self._cfg.get("kelly", {}).get(
                "default_fraction", _DEFAULTS["kelly"]["default_fraction"]
            )
        )
        self._max_pos_pct = float(
            self._cfg.get(
                "max_position_size_pct",
                _DEFAULTS["max_position_size_pct"],
            )
        )
        self._max_pos_usd = Decimal(
            str(
                self._cfg.get(
                    "max_position_size_usd",
                    _DEFAULTS["max_position_size_usd"],
                )
            )
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
        4. Caps at portfolio-based and absolute limits.
        5. Returns a copy of the action with the adjusted quantity.

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
            context.account.total_usdt if context.account else Decimal("0")
        )

        if win_rate <= 0 or avg_win <= Decimal("0") or avg_loss <= Decimal("0"):
            # Fall back to default fraction
            self._log.debug(
                "using_default_kelly_fraction",
                win_rate=win_rate,
                avg_win=str(avg_win),
                avg_loss=str(avg_loss),
            )
            adjusted_qty = self._default_size(portfolio_value, action)
        else:
            kelly_f = self._kelly_fraction(win_rate, avg_win, avg_loss)
            adjusted_qty = self._adjusted_kelly(kelly_f, portfolio_value)

        # Cap at the original requested quantity (don't oversize)
        if action.quantity > Decimal("0") and adjusted_qty > action.quantity:
            adjusted_qty = action.quantity

        # Ensure minimum of 1 contract
        if adjusted_qty < Decimal("1"):
            adjusted_qty = Decimal("1") if action.type == "ENTER" else Decimal("0")

        self._log.debug(
            "position_sized",
            original_qty=str(action.quantity),
            adjusted_qty=str(adjusted_qty),
            kelly_fraction=self._kelly_multiplier,
            portfolio_value=str(portfolio_value),
        )

        return Action(
            type=action.type,
            strategy=action.strategy,
            contract=action.contract,
            side=action.side,
            quantity=adjusted_qty.quantize(Decimal("1"), rounding=ROUND_HALF_UP),
            order_type=action.order_type,
            price=action.price,
            reason=action.reason,
            metadata={
                **action.metadata,
                "sizing_kelly_fraction": self._kelly_multiplier,
                "sizing_adjusted_qty": str(adjusted_qty),
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

        Simplified form when b != 0::

            f = p * (b + 1) - 1 / b

        But the more common form for trading is::

            f = p - (1 - p) * (avg_loss / avg_win)

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
        if avg_win <= Decimal("0") or avg_loss <= Decimal("0"):
            return 0.0

        # Compute payout ratio b = avg_win / avg_loss
        payout_ratio = float(avg_win / avg_loss)

        if payout_ratio <= 0:
            return 0.0

        # f = p - (1-p) / b
        # f = p - q/b  where q = 1-p
        loss_prob = 1.0 - win_rate
        kelly_f = win_rate - (loss_prob / payout_ratio)

        return max(0.0, kelly_f)

    def _adjusted_kelly(
        self, kelly_f: float, portfolio_value: Decimal
    ) -> Decimal:
        """Apply fractional multiplier and portfolio caps to Kelly size.

        Steps:
        1. Multiply full Kelly by ``kelly.fraction`` multiplier.
        2. Cap at ``max_position_size_pct`` of portfolio value.
        3. Cap at ``max_position_size_usd`` absolute limit.
        4. Never exceed full portfolio value.

        Parameters
        ----------
        kelly_f:
            Full Kelly fraction (0.0 to 1.0).
        portfolio_value:
            Total portfolio value in USDT.

        Returns
        -------
        Decimal
            Adjusted position size in contracts (approximate).
        """
        if portfolio_value <= Decimal("0"):
            return Decimal("0")

        # Step 1: Fractional Kelly
        fraction = Decimal(str(self._kelly_multiplier))
        size = Decimal(str(kelly_f)) * fraction * portfolio_value

        # Step 2: Cap at percentage of portfolio
        max_pct = Decimal(str(self._max_pos_pct))
        pct_cap = max_pct * portfolio_value
        if size > pct_cap:
            size = pct_cap

        # Step 3: Cap at absolute USD limit
        if size > self._max_pos_usd:
            size = self._max_pos_usd

        # Step 4: Never exceed 100% of portfolio
        if size > portfolio_value:
            size = portfolio_value

        # Convert to approximate contract count (divide by approximate premium)
        # For options, we approximate 1 contract = ~$100 notional as baseline
        # This is a simplification — real pricing would use option price
        contract_size = (size / Decimal("100")).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )

        return max(contract_size, Decimal("0"))

    def _default_size(
        self, portfolio_value: Decimal, action: Action
    ) -> Decimal:
        """Compute default position size when no historical data is available.

        Uses the ``kelly.default_fraction`` config value as a percentage
        of the portfolio, capped at ``max_position_size_usd``.
        """
        if portfolio_value <= Decimal("0"):
            return Decimal("0")

        default_pct = Decimal(str(self._default_fraction))
        size = default_pct * portfolio_value

        if size > self._max_pos_usd:
            size = self._max_pos_usd

        contract_size = (size / Decimal("100")).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
        return max(contract_size, Decimal("1"))

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
        }
