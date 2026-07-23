"""Vertical Spread strategy implementation.

Credit and debit spreads in a single direction (bullish or bearish)
using same-expiry options. Vertical spreads define risk and reward
by combining a short and long leg at different strikes.
"""

from __future__ import annotations


from decimal import Decimal
from typing import Any

import structlog

from quad.strategy.base import ParamSpec, StrategyBase
from quad.types.risk import Action
from quad.types.strategy import StrategyContext


logger = structlog.get_logger(__name__)

# Valid spread types
SPREAD_TYPES = ("credit_put", "debit_call", "credit_call", "debit_put")


class VerticalSpreadStrategy(StrategyBase):
    """Credit or debit spread in one direction using same-expiry options.

    Supported spread types:
        - credit_put: Sell put (bullish), buy lower put for protection
        - debit_call: Buy call (bullish), sell higher call to reduce cost
        - credit_call: Sell call (bearish), buy higher call for protection
        - debit_put: Buy put (bearish), sell lower put to reduce cost
    """

    @staticmethod
    def get_name() -> str:
        return "vertical_spread"

    @staticmethod
    def get_description() -> str:
        return (
            "Credit or debit vertical spread using same-expiry options. "
            "Supported types: credit_put (bullish), debit_call (bullish), "
            "credit_call (bearish), debit_put (bearish). Delta-targeted "
            "short leg with configurable wing width."
        )

    @staticmethod
    def get_params_spec() -> list[ParamSpec]:
        return [
            ParamSpec("min_dte", "int", 14, "Minimum days to expiry", 1, 365),
            ParamSpec("max_dte", "int", 60, "Maximum days to expiry", 1, 365),
            ParamSpec("spread_type", "str", "credit_put", f"Spread type: {', '.join(SPREAD_TYPES)}"),
            ParamSpec("delta_short", "float", 0.30, "Target delta for the short leg", 0.01, 0.50),
            ParamSpec("wing_width_pct", "float", 25.0, "Wing width as % of short strike", 5.0, 100.0),
            ParamSpec("min_credit_debit_pct", "float", 20.0, "Min credit or max debit as % of width", 1.0, 100.0),
            ParamSpec("take_profit_pct", "float", 50.0, "Take profit when value changes by this %", 1.0, 100.0),
            ParamSpec("stop_loss_pct", "float", 200.0, "Stop loss when value changes by this %", 50.0, 500.0),
        ]

    async def evaluate(self, context: StrategyContext) -> list[Action]:
        """Evaluate vertical spread entry/exit conditions.

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

        existing_position = self._find_existing_position(context)

        if existing_position is None:
            return self._evaluate_entry(context)
        else:
            return await self._evaluate_exit(context, existing_position)

    def _evaluate_entry(self, context: StrategyContext) -> list[Action]:
        """Evaluate entry for a new vertical spread."""
        spread_type = str(self.get_param("spread_type", "credit_put"))
        if spread_type not in SPREAD_TYPES:
            return self.hold_action(f"Invalid spread_type: {spread_type}")

        min_dte = int(self.get_param("min_dte", 14))
        max_dte = int(self.get_param("max_dte", 60))
        delta_short = abs(float(self.get_param("delta_short", 0.30)))
        wing_width_pct = float(self.get_param("wing_width_pct", 25.0))
        min_credit_debit_pct = float(self.get_param("min_credit_debit_pct", 20.0))
        underlying_price = context.underlying_price.price

        contracts = list(self._iter_contracts(context.option_chain))
        if not contracts:
            return self.hold_action("No contracts available")

        # Filter by DTE range
        in_range = [c for c in contracts if self._dte_in_range(c, min_dte, max_dte)]
        if not in_range:
            return self.hold_action(f"No contracts in DTE range {min_dte}-{max_dte}")

        # Determine option type and direction based on spread_type
        is_credit = spread_type.startswith("credit_")
        is_call = spread_type.endswith("call")

        if spread_type == "credit_put":
            # Bullish: sell put, buy lower put
            short_leg_type = "PUT"
            short_above = False  # OTM put below underlying
            wing_direction = -1  # buy lower strike
        elif spread_type == "debit_call":
            # Bullish: buy call, sell higher call
            short_leg_type = "CALL"
            short_above = True
            wing_direction = 1  # sell higher strike
        elif spread_type == "credit_call":
            # Bearish: sell call, buy higher call
            short_leg_type = "CALL"
            short_above = True
            wing_direction = 1  # buy higher strike
        elif spread_type == "debit_put":
            # Bearish: buy put, sell lower put
            short_leg_type = "PUT"
            short_above = False
            wing_direction = -1  # sell lower strike
        else:
            return self.hold_action(f"Unknown spread type: {spread_type}")

        # Find short leg by delta target
        short_leg = self._find_by_delta(
            in_range, delta_short, short_leg_type, underlying_price,
            above_strike=short_above,
        )
        if short_leg is None:
            return self.hold_action(f"No suitable short leg at delta {delta_short}")

        short_strike = self._to_decimal(short_leg.get("strike", 0))
        short_premium = self._mid_price(short_leg)
        if short_premium is None:
            return self.hold_action("Cannot price short leg")

        # Calculate wing strike
        wing_offset = short_strike * Decimal(str(wing_width_pct)) / Decimal("100")
        wing_strike = short_strike + (wing_offset * Decimal(str(wing_direction)))
        # Round to nearest strike in chain
        long_leg = self._find_nearest_strike(in_range, wing_strike, short_leg_type)
        if long_leg is None:
            return self.hold_action("No suitable long leg for wing")

        long_strike = self._to_decimal(long_leg.get("strike", 0))
        long_premium = self._mid_price(long_leg)
        if long_premium is None:
            return self.hold_action("Cannot price long leg")

        # Ensure short and long legs are different strikes
        if short_strike == long_strike:
            return self.hold_action("Short and long strikes are identical")
        width = abs(short_strike - long_strike)

        if is_credit:
            # Credit spread: net credit > min_credit_debit_pct * width
            net_credit = short_premium - long_premium
            if net_credit <= Decimal("0"):
                self.logger.info("net_credit_not_positive", net_credit=str(net_credit))
                return self.hold_action(f"Net credit not positive: {net_credit:.2f}")

            min_net = self._to_decimal(width) * Decimal(str(min_credit_debit_pct)) / Decimal("100")
            if net_credit < min_net:
                self.logger.info(
                    "credit_below_min",
                    net_credit=str(net_credit),
                    min_net=str(min_net),
                )
                return self.hold_action(
                    f"Credit {net_credit:.2f} below min {min_net:.2f}"
                )

            self.logger.info(
                f"{spread_type}_entry",
                short=str(short_leg.get("symbol")),
                long=str(long_leg.get("symbol")),
                net_credit=str(net_credit),
                width=str(width),
            )

            return [
                Action(
                    type="ENTER",
                    strategy=self.get_name(),
                    contract=str(short_leg.get("symbol", "")),
                    side="SELL",
                    quantity=Decimal("1"),
                    order_type="LIMIT",
                    price=short_premium,
                    reason=f"{spread_type}: sell {short_leg.get('symbol')}",
                    metadata={
                        "leg": "short",
                        "spread_type": spread_type,
                        "net_credit": str(net_credit),
                        "width": str(width),
                    },
                ),
                Action(
                    type="ENTER",
                    strategy=self.get_name(),
                    contract=str(long_leg.get("symbol", "")),
                    side="BUY",
                    quantity=Decimal("1"),
                    order_type="LIMIT",
                    price=long_premium,
                    reason=f"{spread_type}: buy {long_leg.get('symbol')}",
                    metadata={
                        "leg": "long",
                        "spread_type": spread_type,
                        "net_credit": str(net_credit),
                        "width": str(width),
                    },
                ),
            ]
        else:
            # Debit spread: net debit < width * (1 - min_credit_debit_pct/100)
            net_debit = long_premium - short_premium
            if net_debit <= Decimal("0"):
                self.logger.info("net_debit_not_positive", net_debit=str(net_debit))
                return self.hold_action(f"Net debit not positive: {net_debit:.2f}")

            max_debit = self._to_decimal(width) * (
                Decimal("1") - Decimal(str(min_credit_debit_pct)) / Decimal("100")
            )
            if net_debit > max_debit:
                self.logger.info(
                    "debit_above_max",
                    net_debit=str(net_debit),
                    max_debit=str(max_debit),
                )
                return self.hold_action(
                    f"Debit {net_debit:.2f} above max {max_debit:.2f}"
                )

            self.logger.info(
                f"{spread_type}_entry",
                short=str(short_leg.get("symbol")),
                long=str(long_leg.get("symbol")),
                net_debit=str(net_debit),
                width=str(width),
            )

            return [
                Action(
                    type="ENTER",
                    strategy=self.get_name(),
                    contract=str(long_leg.get("symbol", "")),
                    side="BUY",
                    quantity=Decimal("1"),
                    order_type="LIMIT",
                    price=long_premium,
                    reason=f"{spread_type}: buy {long_leg.get('symbol')}",
                    metadata={
                        "leg": "long",
                        "spread_type": spread_type,
                        "net_debit": str(net_debit),
                        "width": str(width),
                    },
                ),
                Action(
                    type="ENTER",
                    strategy=self.get_name(),
                    contract=str(short_leg.get("symbol", "")),
                    side="SELL",
                    quantity=Decimal("1"),
                    order_type="LIMIT",
                    price=short_premium,
                    reason=f"{spread_type}: sell {short_leg.get('symbol')}",
                    metadata={
                        "leg": "short",
                        "spread_type": spread_type,
                        "net_debit": str(net_debit),
                        "width": str(width),
                    },
                ),
            ]

    async def _evaluate_exit(
        self,
        context: StrategyContext,
        existing_position: dict[str, Any],
    ) -> list[Action]:
        """Evaluate exit for an existing vertical spread."""
        spread_type = str(self.get_param("spread_type", "credit_put"))
        take_profit_pct = float(self.get_param("take_profit_pct", 50.0))
        stop_loss_pct = float(self.get_param("stop_loss_pct", 200.0))
        is_credit = spread_type.startswith("credit_")

        legs = self._find_strategy_legs(context)
        if not legs:
            return self.hold_action("Cannot find spread legs in chain")

        current_value = self._combined_value(legs)
        entry_value = self._estimate_entry_value(context, existing_position)
        if entry_value is None or entry_value <= Decimal("0"):
            return self.hold_action("Cannot determine entry value")

        dte_values = [self._calculate_dte(l) for l in legs if self._calculate_dte(l) is not None]
        min_dte = min(dte_values) if dte_values else 999

        if min_dte < 1:
            self.logger.info("exit_near_expiry", dte=min_dte)
            return self._exit_all_actions(legs, "Near expiration")

        if is_credit:
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

        return self.hold_action("Vertical spread within tolerance")

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
        side = leg.get("leg_side", "LONG")
        if isinstance(side, str):
            return "SELL" in side.upper() or side == "SHORT"
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
                c["leg_side"] = side
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

