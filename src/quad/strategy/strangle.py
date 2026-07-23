"""Strangle strategy implementation.

Buy or sell OTM call + OTM put at different strikes. Strangles are
cheaper than straddles (wider OTM wings) but require larger moves to
profit. Short strangles are a popular income strategy; long strangles
are a lower-cost volatility play.
"""

from __future__ import annotations


from decimal import Decimal
from typing import Any

import structlog

from quad.strategy.base import ParamSpec, StrategyBase
from quad.types.risk import Action
from quad.types.strategy import StrategyContext


logger = structlog.get_logger(__name__)


class StrangleStrategy(StrategyBase):
    """Buy or sell OTM call + OTM put at different strikes.

    Short strangle (default):
        - Sell OTM call at call_delta_target delta
        - Sell OTM put at put_delta_target delta
        - Benefits from time decay and range-bound markets

    Long strangle:
        - Buy OTM call at call_delta_target delta
        - Buy OTM put at put_delta_target delta
        - Lower cost alternative to long straddle
    """

    @staticmethod
    def get_name() -> str:
        return "strangle"

    @staticmethod
    def get_description() -> str:
        return (
            "Buy or sell OTM call and OTM put at different strikes using "
            "delta-targeted wing selection. Short strangles generate income "
            "in range-bound markets; long strangles are lower-cost "
            "volatility plays."
        )

    @staticmethod
    def get_params_spec() -> list[ParamSpec]:
        return [
            ParamSpec("min_dte", "int", 14, "Minimum days to expiry", 1, 365),
            ParamSpec("max_dte", "int", 45, "Maximum days to expiry", 1, 365),
            ParamSpec("direction", "str", "short", "Trade direction: long or short"),
            ParamSpec("call_delta_target", "float", 0.25, "Target delta for call wing", 0.01, 0.50),
            ParamSpec("put_delta_target", "float", -0.25, "Target delta for put wing", -0.50, -0.01),
            ParamSpec("take_profit_pct", "float", 25.0, "Take profit when value changes by this %", 1.0, 100.0),
            ParamSpec("stop_loss_pct", "float", 200.0, "Stop loss when value changes by this %", 50.0, 500.0),
        ]

    async def evaluate(self, context: StrategyContext) -> list[Action]:
        """Evaluate strangle entry/exit conditions.

        Args:
            context: Current market and account context.

        Returns:
            List of Action objects (ENTER legs, EXIT legs, or HOLD).
        """
        self.logger.info("evaluate_start", strategy=self.get_name())

        if context.underlying_price is None:
            return self.hold_action("No underlying price available")

        if not context.option_chain:
            return self.hold_action("Empty option chain")

        direction = str(self.get_param("direction", "short"))
        existing_position = self._find_existing_position(context)

        if existing_position is None:
            return self._evaluate_entry(context, direction)
        else:
            return await self._evaluate_exit(context, direction, existing_position)

    def _evaluate_entry(
        self,
        context: StrategyContext,
        direction: str,
    ) -> list[Action]:
        """Evaluate entry for a new strangle."""
        min_dte = int(self.get_param("min_dte", 14))
        max_dte = int(self.get_param("max_dte", 45))
        call_delta_target = abs(float(self.get_param("call_delta_target", 0.25)))
        put_delta_target = abs(float(self.get_param("put_delta_target", 0.25)))
        underlying_price = context.underlying_price.price

        contracts = list(self._iter_contracts(context.option_chain))
        if not contracts:
            return self.hold_action("No contracts available")

        # Filter by DTE range
        in_range = [c for c in contracts if self._dte_in_range(c, min_dte, max_dte)]
        if not in_range:
            return self.hold_action(f"No contracts in DTE range {min_dte}-{max_dte}")

        # Find OTM call at call_delta_target
        call_leg = self._find_by_delta(
            in_range, call_delta_target, "CALL", underlying_price, above_strike=True,
        )
        if call_leg is None:
            return self.hold_action("No suitable call for strangle wing")

        # Find OTM put at put_delta_target
        put_leg = self._find_by_delta(
            in_range, put_delta_target, "PUT", underlying_price, above_strike=False,
        )
        if put_leg is None:
            return self.hold_action("No suitable put for strangle wing")

        call_premium = self._mid_price(call_leg)
        put_premium = self._mid_price(put_leg)
        if call_premium is None or put_premium is None:
            return self.hold_action("Cannot price strangle wings")

        call_strike = self._to_decimal(call_leg.get("strike", 0))
        put_strike = self._to_decimal(put_leg.get("strike", 0))

        if direction == "short":
            total_credit = call_premium + put_premium
            self.logger.info(
                "short_strangle_entry",
                call=str(call_leg.get("symbol")),
                put=str(put_leg.get("symbol")),
                call_delta=round(float(self._to_decimal(call_leg.get("delta", 0))), 3),
                put_delta=round(float(self._to_decimal(put_leg.get("delta", 0))), 3),
                total_credit=str(total_credit),
            )
            return [
                Action(
                    type="ENTER",
                    strategy=self.get_name(),
                    contract=str(call_leg.get("symbol", "")),
                    side="SELL",
                    quantity=Decimal("1"),
                    order_type="LIMIT",
                    price=call_premium,
                    reason=f"Short strangle: sell call {call_leg.get('symbol')}",
                    metadata={
                        "leg": "call",
                        "direction": "short",
                        "call_strike": str(call_strike),
                        "put_strike": str(put_strike),
                        "total_credit": str(total_credit),
                    },
                ),
                Action(
                    type="ENTER",
                    strategy=self.get_name(),
                    contract=str(put_leg.get("symbol", "")),
                    side="SELL",
                    quantity=Decimal("1"),
                    order_type="LIMIT",
                    price=put_premium,
                    reason=f"Short strangle: sell put {put_leg.get('symbol')}",
                    metadata={
                        "leg": "put",
                        "direction": "short",
                        "call_strike": str(call_strike),
                        "put_strike": str(put_strike),
                        "total_credit": str(total_credit),
                    },
                ),
            ]
        else:
            total_debit = call_premium + put_premium
            self.logger.info(
                "long_strangle_entry",
                call=str(call_leg.get("symbol")),
                put=str(put_leg.get("symbol")),
                total_debit=str(total_debit),
            )
            return [
                Action(
                    type="ENTER",
                    strategy=self.get_name(),
                    contract=str(call_leg.get("symbol", "")),
                    side="BUY",
                    quantity=Decimal("1"),
                    order_type="LIMIT",
                    price=call_premium,
                    reason=f"Long strangle: buy call {call_leg.get('symbol')}",
                    metadata={
                        "leg": "call",
                        "direction": "long",
                        "call_strike": str(call_strike),
                        "put_strike": str(put_strike),
                        "total_debit": str(total_debit),
                    },
                ),
                Action(
                    type="ENTER",
                    strategy=self.get_name(),
                    contract=str(put_leg.get("symbol", "")),
                    side="BUY",
                    quantity=Decimal("1"),
                    order_type="LIMIT",
                    price=put_premium,
                    reason=f"Long strangle: buy put {put_leg.get('symbol')}",
                    metadata={
                        "leg": "put",
                        "direction": "long",
                        "call_strike": str(call_strike),
                        "put_strike": str(put_strike),
                        "total_debit": str(total_debit),
                    },
                ),
            ]

    async def _evaluate_exit(
        self,
        context: StrategyContext,
        direction: str,
        existing_position: dict[str, Any],
    ) -> list[Action]:
        """Evaluate exit for an existing strangle."""
        take_profit_pct = float(self.get_param("take_profit_pct", 25.0))
        stop_loss_pct = float(self.get_param("stop_loss_pct", 200.0))

        legs = self._find_strategy_legs(context)
        if not legs:
            return self.hold_action("Cannot find strangle legs in chain")

        current_value = self._combined_value(legs)
        entry_value = self._estimate_entry_value(context, existing_position)
        if entry_value is None or entry_value <= Decimal("0"):
            return self.hold_action("Cannot determine entry value")

        dte_values = [self._calculate_dte(l) for l in legs if self._calculate_dte(l) is not None]
        min_dte = min(dte_values) if dte_values else 999

        if min_dte < 1:
            self.logger.info("exit_near_expiry", dte=min_dte)
            return self._exit_all_actions(legs, "Near expiration")

        if direction == "short":
            decay_pct = float((entry_value - current_value) / entry_value * 100)
            if decay_pct >= take_profit_pct:
                self.logger.info("exit_take_profit", decay_pct=round(decay_pct, 1))
                return self._exit_all_actions(
                    legs, f"Take profit: credit decayed {decay_pct:.1f}%"
                )

            loss_pct = float((current_value - entry_value) / entry_value * 100)
            if loss_pct >= stop_loss_pct:
                self.logger.warning("exit_stop_loss", loss_pct=round(loss_pct, 1))
                return self._exit_all_actions(
                    legs, f"Stop loss: debit increased {loss_pct:.1f}%"
                )
        else:
            profit_pct = float((current_value - entry_value) / entry_value * 100)
            if profit_pct >= take_profit_pct:
                self.logger.info("exit_take_profit", profit_pct=round(profit_pct, 1))
                return self._exit_all_actions(
                    legs, f"Take profit: value up {profit_pct:.1f}%"
                )

            loss_pct = float((entry_value - current_value) / entry_value * 100)
            if loss_pct >= stop_loss_pct:
                self.logger.warning("exit_stop_loss", loss_pct=round(loss_pct, 1))
                return self._exit_all_actions(
                    legs, f"Stop loss: value down {loss_pct:.1f}%"
                )

        return self.hold_action("Strangle within tolerance")

    # ---- Helpers ----

    def _exit_all_actions(self, legs: list[dict[str, Any]], reason: str) -> list[Action]:
        actions = []
        for leg in legs:
            side = "BUY" if self._is_short_leg(leg) else "SELL"
            actions.append(
                Action(
                    type="EXIT",
                    strategy=self.get_name(),
                    contract=str(leg.get("symbol", "")),
                    side=side,
                    quantity=Decimal("1"),
                    order_type="MARKET",
                    reason=reason,
                    metadata={"exit_reason": reason},
                )
            )
        if not actions:
            return self.hold_action(reason)
        return actions

    def _is_short_leg(self, leg: dict[str, Any]) -> bool:
        entry_side = leg.get("entry_side", "LONG")
        if isinstance(entry_side, str):
            return "SELL" in entry_side.upper() or entry_side == "SHORT"
        return False

    def _find_strategy_legs(self, context: StrategyContext) -> list[dict[str, Any]]:
        contracts = list(self._iter_contracts(context.option_chain))
        position_symbols = {
            p.contract_symbol
            for p in context.positions
            if p.strategy == self.get_name() and p.status == "OPEN"
        }
        legs = []
        for c in contracts:
            if c.get("symbol") in position_symbols:
                side = next(
                    (p.side for p in context.positions
                     if p.contract_symbol == c.get("symbol") and p.strategy == self.get_name()),
                    "LONG",
                )
                c["entry_side"] = side
                legs.append(c)
        return legs

    def _find_existing_position(self, context: StrategyContext) -> dict[str, Any] | None:
        positions = [p for p in context.positions if p.strategy == self.get_name()]
        open_positions = [p for p in positions if p.status == "OPEN"]
        if not open_positions:
            return None
        return {"contract_symbols": [p.contract_symbol for p in open_positions]}

    def _estimate_entry_value(
        self,
        context: StrategyContext,
        position: dict[str, Any],
    ) -> Decimal | None:
        symbols = position.get("contract_symbols", [])
        total = Decimal("0")
        for p in context.positions:
            if p.contract_symbol in symbols and p.strategy == self.get_name():
                total += p.entry_price * abs(p.quantity) if p.quantity else p.entry_price
        return total if total > Decimal("0") else None

