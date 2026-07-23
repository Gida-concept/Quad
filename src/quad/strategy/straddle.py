"""Straddle strategy implementation.

Buy or sell both a call and a put at the same at-the-money (ATM) strike.
Long straddles profit from high volatility (large moves in either direction).
Short straddles profit from low volatility (underlying stays near strike).
"""

from __future__ import annotations


from decimal import Decimal
from typing import Any

import structlog

from quad.strategy.base import ParamSpec, StrategyBase
from quad.types.risk import Action
from quad.types.strategy import StrategyContext


logger = structlog.get_logger(__name__)


class StraddleStrategy(StrategyBase):
    """Buy or sell ATM call + ATM put at the same strike.

    Long straddle:
        - Profit from large moves in either direction
        - Requires elevated implied volatility
        - IV >= min_iv_percentile

    Short straddle:
        - Profit from time decay and low volatility
        - Requires suppressed implied volatility
        - IV <= max_iv_percentile
    """

    @staticmethod
    def get_name() -> str:
        return "straddle"

    @staticmethod
    def get_description() -> str:
        return (
            "Buy or sell ATM call and put at the same strike. Long straddle "
            "profits from high volatility; short straddle profits from low "
            "volatility and time decay. IV percentile filters determine entry."
        )

    @staticmethod
    def get_params_spec() -> list[ParamSpec]:
        return [
            ParamSpec("min_dte", "int", 7, "Minimum days to expiry", 1, 365),
            ParamSpec("max_dte", "int", 30, "Maximum days to expiry", 1, 365),
            ParamSpec("direction", "str", "long", "Trade direction: long or short"),
            ParamSpec("min_iv_percentile", "float", 30.0, "Min IV percentile for long entry", 0.0, 100.0),
            ParamSpec("max_iv_percentile", "float", 70.0, "Max IV percentile for short entry", 0.0, 100.0),
            ParamSpec("take_profit_pct", "float", 50.0, "Take profit when value changes by this %", 1.0, 100.0),
            ParamSpec("stop_loss_pct", "float", 100.0, "Stop loss when value changes by this %", 50.0, 500.0),
        ]

    async def evaluate(self, context: StrategyContext) -> list[Action]:
        """Evaluate straddle entry/exit conditions.

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

        direction = str(self.get_param("direction", "long"))
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
        """Evaluate entry for a new straddle."""
        min_dte = int(self.get_param("min_dte", 7))
        max_dte = int(self.get_param("max_dte", 30))
        underlying_price = context.underlying_price.price

        contracts = list(self._iter_contracts(context.option_chain))
        if not contracts:
            return self.hold_action("No contracts available")

        # Filter by DTE range
        in_range = [c for c in contracts if self._dte_in_range(c, min_dte, max_dte)]
        if not in_range:
            return self.hold_action(f"No contracts in DTE range {min_dte}-{max_dte}")

        # Find ATM strike closest to underlying price
        all_strikes = {}
        for c in in_range:
            strike = self._to_decimal(c.get("strike", 0))
            all_strikes[strike] = all_strikes.get(strike, 0) + 1

        if not all_strikes:
            return self.hold_action("No valid strikes")

        # Find ATM strike
        atm_strike = min(
            all_strikes.keys(),
            key=lambda s: abs(s - underlying_price),
        )

        # Find ATM call and put
        atm_call = self._find_by_strike(in_range, atm_strike, "CALL")
        atm_put = self._find_by_strike(in_range, atm_strike, "PUT")

        if atm_call is None or atm_put is None:
            return self.hold_action("Cannot find ATM call and put")

        call_premium = self._mid_price(atm_call)
        put_premium = self._mid_price(atm_put)
        if call_premium is None or put_premium is None:
            return self.hold_action("Cannot price ATM legs")

        if direction == "long":
            # Long straddle: IV check
            min_iv = float(self.get_param("min_iv_percentile", 30.0))
            iv_pct = self._get_iv_percentile(context, atm_call, atm_put)

            if iv_pct is not None and iv_pct < min_iv:
                self.logger.info(
                    "iv_below_min",
                    iv_percentile=round(iv_pct, 1),
                    min_iv=min_iv,
                )
                return self.hold_action(
                    f"IV percentile {iv_pct:.1f}% below min {min_iv}%"
                )

            total_debit = call_premium + put_premium
            self.logger.info(
                "long_straddle_entry",
                strike=str(atm_strike),
                call_premium=str(call_premium),
                put_premium=str(put_premium),
                total_debit=str(total_debit),
                iv_percentile=round(iv_pct, 1) if iv_pct is not None else None,
            )

            return [
                Action(
                    type="ENTER",
                    strategy=self.get_name(),
                    contract=str(atm_call.get("symbol", "")),
                    side="BUY",
                    quantity=Decimal("1"),
                    order_type="LIMIT",
                    price=call_premium,
                    reason=f"Long straddle: buy call {atm_call.get('symbol')}",
                    metadata={
                        "leg": "call",
                        "direction": "long",
                        "strike": str(atm_strike),
                        "total_debit": str(total_debit),
                    },
                ),
                Action(
                    type="ENTER",
                    strategy=self.get_name(),
                    contract=str(atm_put.get("symbol", "")),
                    side="BUY",
                    quantity=Decimal("1"),
                    order_type="LIMIT",
                    price=put_premium,
                    reason=f"Long straddle: buy put {atm_put.get('symbol')}",
                    metadata={
                        "leg": "put",
                        "direction": "long",
                        "strike": str(atm_strike),
                        "total_debit": str(total_debit),
                    },
                ),
            ]
        else:
            # Short straddle: IV check
            max_iv = float(self.get_param("max_iv_percentile", 70.0))
            iv_pct = self._get_iv_percentile(context, atm_call, atm_put)

            if iv_pct is not None and iv_pct > max_iv:
                self.logger.info(
                    "iv_above_max",
                    iv_percentile=round(iv_pct, 1),
                    max_iv=max_iv,
                )
                return self.hold_action(
                    f"IV percentile {iv_pct:.1f}% above max {max_iv}%"
                )

            total_credit = call_premium + put_premium
            self.logger.info(
                "short_straddle_entry",
                strike=str(atm_strike),
                call_premium=str(call_premium),
                put_premium=str(put_premium),
                total_credit=str(total_credit),
            )

            return [
                Action(
                    type="ENTER",
                    strategy=self.get_name(),
                    contract=str(atm_call.get("symbol", "")),
                    side="SELL",
                    quantity=Decimal("1"),
                    order_type="LIMIT",
                    price=call_premium,
                    reason=f"Short straddle: sell call {atm_call.get('symbol')}",
                    metadata={
                        "leg": "call",
                        "direction": "short",
                        "strike": str(atm_strike),
                        "total_credit": str(total_credit),
                    },
                ),
                Action(
                    type="ENTER",
                    strategy=self.get_name(),
                    contract=str(atm_put.get("symbol", "")),
                    side="SELL",
                    quantity=Decimal("1"),
                    order_type="LIMIT",
                    price=put_premium,
                    reason=f"Short straddle: sell put {atm_put.get('symbol')}",
                    metadata={
                        "leg": "put",
                        "direction": "short",
                        "strike": str(atm_strike),
                        "total_credit": str(total_credit),
                    },
                ),
            ]

    async def _evaluate_exit(
        self,
        context: StrategyContext,
        direction: str,
        existing_position: dict[str, Any],
    ) -> list[Action]:
        """Evaluate exit for an existing straddle."""
        take_profit_pct = float(self.get_param("take_profit_pct", 50.0))
        stop_loss_pct = float(self.get_param("stop_loss_pct", 100.0))

        legs = self._find_strategy_legs(context)
        if not legs:
            return self.hold_action("Cannot find straddle legs in chain")

        current_value = self._combined_value(legs)
        max_value = self._estimate_max_value(context, existing_position)
        if max_value is None or max_value <= Decimal("0"):
            return self.hold_action("Cannot determine entry value")

        # Check DTE
        dte_values = [self._calculate_dte(l) for l in legs if self._calculate_dte(l) is not None]
        min_dte = min(dte_values) if dte_values else 999

        if min_dte < 1:
            self.logger.info("exit_near_expiry", dte=min_dte)
            return self._exit_all_actions(legs, "Near expiration")

        if direction == "long":
            # Long: profit when value rises
            profit_pct = float((current_value - max_value) / max_value * 100)
            if profit_pct >= take_profit_pct:
                self.logger.info(
                    "exit_take_profit",
                    profit_pct=round(profit_pct, 1),
                )
                return self._exit_all_actions(
                    legs, f"Take profit: value up {profit_pct:.1f}%"
                )

            loss_pct = float((max_value - current_value) / max_value * 100)
            if loss_pct >= stop_loss_pct:
                self.logger.warning(
                    "exit_stop_loss",
                    loss_pct=round(loss_pct, 1),
                )
                return self._exit_all_actions(
                    legs, f"Stop loss: value down {loss_pct:.1f}%"
                )
        else:
            # Short: profit when value decays
            decay_pct = float((max_value - current_value) / max_value * 100)
            if decay_pct >= take_profit_pct:
                self.logger.info(
                    "exit_take_profit",
                    decay_pct=round(decay_pct, 1),
                )
                return self._exit_all_actions(
                    legs, f"Take profit: credit decayed {decay_pct:.1f}%"
                )

            loss_pct = float((current_value - max_value) / max_value * 100)
            if loss_pct >= stop_loss_pct:
                self.logger.warning(
                    "exit_stop_loss",
                    loss_pct=round(loss_pct, 1),
                )
                return self._exit_all_actions(
                    legs, f"Stop loss: value up {loss_pct:.1f}%"
                )

        return self.hold_action("Straddle within tolerance")

    # ---- Helpers ----

    def _exit_all_actions(self, legs: list[dict[str, Any]], reason: str) -> list[Action]:
        actions = []
        for leg in legs:
            side = "SELL" if "BUY" in str(leg.get("entry_side", "")).upper() else "BUY"
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

    def _estimate_max_value(
        self,
        context: StrategyContext,
        position: dict[str, Any],
    ) -> Decimal | None:
        """Estimate entry value (total debit paid or credit received)."""
        symbols = position.get("contract_symbols", [])
        total = Decimal("0")
        for p in context.positions:
            if p.contract_symbol in symbols and p.strategy == self.get_name():
                cost = p.entry_price * p.quantity
                if p.side == "SHORT" or "SELL" in p.side.upper():
                    total += cost
                else:
                    total += cost
        return total if total > Decimal("0") else None

    def _get_iv_percentile(
        self,
        context: StrategyContext,
        call_contract: dict[str, Any],
        put_contract: dict[str, Any],
    ) -> float | None:
        """Estimate IV percentile from contract IV and historical context."""
        call_iv = self._to_decimal(call_contract.get("implied_volatility", 0))
        put_iv = self._to_decimal(put_contract.get("implied_volatility", 0))
        avg_iv = float((call_iv + put_iv) / Decimal("2")) * 100

        # Check if context provides IV percentile info
        greeks = context.greeks or {}
        iv_data = greeks.get("iv_percentile") or greeks.get("iv_percentile_30d")
        if iv_data is not None:
            try:
                return float(iv_data)
            except (ValueError, TypeError):
                pass

        # Fall back to raw IV as a proxy (when no percentile data available)
        return avg_iv

    @staticmethod
    def _find_by_strike(
        contracts: list[dict[str, Any]],
        target_strike: Decimal,
        option_type: str,
    ) -> dict[str, Any] | None:
        eligible = [c for c in contracts if c.get("option_type") == option_type]
        if not eligible:
            return None
        return min(
            eligible,
            key=lambda c: abs(StraddleStrategy._to_decimal(c.get("strike", 0)) - target_strike),
        )

