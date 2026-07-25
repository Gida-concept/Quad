"""Strangle strategy implementation with profitability improvements.

Buy or sell OTM call + OTM put at different strikes. Strangles are
cheaper than straddles (wider OTM wings) but require larger moves to
profit. Short strangles are a popular income strategy; long strangles
are a lower-cost volatility play.

Profitability improvements:
    - Tighter delta targets (0.16) for higher probability of profit
    - IV rank filter to avoid low-volatility entries
    - 21 DTE forced exit to avoid gamma risk
    - Rolling logic to manage threatened legs
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
        - IV rank filter prevents entry when IV is too low
        - Forced exit at force_exit_dte to avoid gamma risk
        - Rolling logic extends threatened legs to next expiration

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
            "volatility plays. IV rank filter, forced DTE exit, and "
            "rolling logic improve profitability."
        )

    @staticmethod
    def get_params_spec() -> list[ParamSpec]:
        return [
            ParamSpec("min_dte", "int", 30, "Minimum days to expiry", 1, 365),
            ParamSpec("max_dte", "int", 45, "Maximum days to expiry", 1, 365),
            ParamSpec("direction", "str", "short", "Trade direction: long or short"),
            ParamSpec("call_delta_target", "float", 0.16, "Target delta for call wing", 0.01, 0.50),
            ParamSpec("put_delta_target", "float", -0.16, "Target delta for put wing", -0.50, -0.01),
            ParamSpec("take_profit_pct", "float", 50.0, "Take profit when value changes by this %", 1.0, 100.0),
            ParamSpec("stop_loss_pct", "float", 200.0, "Stop loss when value changes by this %", 50.0, 500.0),
            ParamSpec("min_iv_rank", "float", 30.0, "Minimum IV percentile for short entry (0-100)", 0.0, 100.0),
            ParamSpec("force_exit_dte", "int", 21, "Force exit when DTE drops below this", 1, 365),
            ParamSpec("roll_when_delta_exceeds", "float", 0.40, "Roll short leg when delta exceeds this", 0.01, 0.99),
            ParamSpec("roll_credit_min_pct", "float", 10.0, "Min net credit % for rolling a threatened leg", 0.0, 100.0),
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
        min_dte = int(self.get_param("min_dte", 30))
        max_dte = int(self.get_param("max_dte", 45))
        call_delta_target = abs(float(self.get_param("call_delta_target", 0.16)))
        put_delta_target = abs(float(self.get_param("put_delta_target", 0.16)))
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

        # IV rank filter for short strangle (skip for long entries)
        if direction == "short":
            min_iv_rank = float(self.get_param("min_iv_rank", 30.0))
            call_iv_rank = self._compute_iv_percentile(call_leg, contracts)
            put_iv_rank = self._compute_iv_percentile(put_leg, contracts)

            if call_iv_rank is not None and call_iv_rank < min_iv_rank:
                self.logger.info(
                    "entry_call_iv_rank_too_low",
                    iv_rank=round(call_iv_rank, 1),
                    min_iv_rank=min_iv_rank,
                    call=str(call_leg.get("symbol")),
                )
                return self.hold_action(
                    f"Short call IV rank {call_iv_rank:.1f}% below min {min_iv_rank}%"
                )

            if put_iv_rank is not None and put_iv_rank < min_iv_rank:
                self.logger.info(
                    "entry_put_iv_rank_too_low",
                    iv_rank=round(put_iv_rank, 1),
                    min_iv_rank=min_iv_rank,
                    put=str(put_leg.get("symbol")),
                )
                return self.hold_action(
                    f"Short put IV rank {put_iv_rank:.1f}% below min {min_iv_rank}%"
                )

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
        take_profit_pct = float(self.get_param("take_profit_pct", 50.0))
        stop_loss_pct = float(self.get_param("stop_loss_pct", 200.0))
        force_exit_dte = int(self.get_param("force_exit_dte", 21))

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

            # Forced exit when DTE drops below threshold (gamma risk avoidance)
            if min_dte < force_exit_dte:
                self.logger.info(
                    "exit_forced_dte",
                    dte=min_dte,
                    threshold=force_exit_dte,
                )
                return self._exit_all_actions(
                    legs, f"Forced exit: DTE {min_dte} below threshold {force_exit_dte}"
                )

            # Check roll conditions for threatened short legs
            roll_actions = self._evaluate_roll(legs, context)
            if roll_actions:
                return roll_actions
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

            # Forced exit also applies to long strangles for consistency
            if min_dte < force_exit_dte:
                self.logger.info(
                    "exit_forced_dte",
                    dte=min_dte,
                    threshold=force_exit_dte,
                )
                return self._exit_all_actions(
                    legs, f"Forced exit: DTE {min_dte} below threshold {force_exit_dte}"
                )

        return self.hold_action("Strangle within tolerance")

    # ---- Entry helpers ----

    def _compute_iv_percentile(
        self,
        contract: dict[str, Any],
        contracts: list[dict[str, Any]],
    ) -> float | None:
        """Compute IV percentile of a contract within its expiry chain.

        Groups contracts by expiry and option type, then calculates the
        percentile rank of the given contract's implied volatility within
        that group.

        Args:
            contract: The target contract dict.
            contracts: Full list of available contracts for context.

        Returns:
            IV percentile as a float (0-100), or None if unable to compute.
        """
        expiry = contract.get("expiry")
        option_type = contract.get("option_type")
        if expiry is None or option_type is None:
            return None

        # Find all contracts with same expiry and option type
        same_chain = [
            c for c in contracts
            if c.get("expiry") == expiry
            and c.get("option_type") == option_type
        ]

        ivs: list[float] = []
        for c in same_chain:
            iv = float(self._to_decimal(c.get("implied_volatility", 0)))
            if iv > 0:
                ivs.append(iv)

        if not ivs:
            return None

        contract_iv = float(self._to_decimal(contract.get("implied_volatility", 0)))
        if contract_iv <= 0:
            return None

        # Percentile = proportion of contracts with IV <= contract_iv
        count_le = sum(1 for iv in ivs if iv <= contract_iv)
        return (count_le / len(ivs)) * 100

    # ---- Rolling logic ----

    def _evaluate_roll(
        self,
        legs: list[dict[str, Any]],
        context: StrategyContext,
    ) -> list[Action] | None:
        """Evaluate whether to roll threatened short legs to next expiry.

        Checks each short leg's current delta against the threshold. If a
        short leg's absolute delta exceeds `roll_when_delta_exceeds`, the
        leg is closed and a new short leg at the next expiry is opened at
        the same delta target. A roll is only executed if it generates a
        net credit meeting the `roll_credit_min_pct` threshold.

        Args:
            legs: Current strategy leg contracts from the chain.
            context: Current market and account context.

        Returns:
            List of EXIT/ENTER action pairs for each rolled leg,
            or None if no roll is warranted.
        """
        roll_when_delta_exceeds = float(self.get_param("roll_when_delta_exceeds", 0.40))
        roll_credit_min_pct = float(self.get_param("roll_credit_min_pct", 10.0))

        # Identify short legs in position
        short_call: dict[str, Any] | None = None
        short_put: dict[str, Any] | None = None
        for leg in legs:
            if not self._is_short_leg(leg):
                continue
            if leg.get("option_type") == "CALL":
                short_call = leg
            elif leg.get("option_type") == "PUT":
                short_put = leg

        if short_call is None and short_put is None:
            return None

        # Check which legs are threatened (current delta exceeds threshold)
        call_threatened = False
        put_threatened = False

        if short_call is not None:
            call_delta = abs(float(self._to_decimal(short_call.get("delta", 0))))
            if call_delta > roll_when_delta_exceeds:
                call_threatened = True

        if short_put is not None:
            put_delta = abs(float(self._to_decimal(short_put.get("delta", 0))))
            if put_delta > roll_when_delta_exceeds:
                put_threatened = True

        if not call_threatened and not put_threatened:
            return None

        # Find the next expiry beyond current position expiries
        contracts = list(self._iter_contracts(context.option_chain))
        current_expiries: set[int] = set()
        for leg in legs:
            exp = leg.get("expiry")
            if exp is not None:
                current_expiries.add(int(exp))

        current_max_expiry = max(current_expiries) if current_expiries else 0

        # Filter contracts to those at a later expiry than current
        later_contracts = [
            c for c in contracts
            if c.get("expiry") is not None
            and int(c.get("expiry", 0)) > current_max_expiry
        ]

        if not later_contracts:
            self.logger.info("roll_no_later_expiry")
            return None

        call_delta_target = abs(float(self.get_param("call_delta_target", 0.16)))
        put_delta_target = abs(float(self.get_param("put_delta_target", 0.16)))
        underlying_price = context.underlying_price.price

        actions: list[Action] = []

        # Roll the threatened call leg
        if call_threatened and short_call is not None:
            roll_result = self._build_roll_actions(
                leg=short_call,
                later_contracts=later_contracts,
                delta_target=call_delta_target,
                option_type="CALL",
                underlying_price=underlying_price,
                above_strike=True,
                roll_credit_min_pct=roll_credit_min_pct,
            )
            if roll_result is not None:
                actions.extend(roll_result)

        # Roll the threatened put leg
        if put_threatened and short_put is not None:
            roll_result = self._build_roll_actions(
                leg=short_put,
                later_contracts=later_contracts,
                delta_target=put_delta_target,
                option_type="PUT",
                underlying_price=underlying_price,
                above_strike=False,
                roll_credit_min_pct=roll_credit_min_pct,
            )
            if roll_result is not None:
                actions.extend(roll_result)

        if not actions:
            return None

        return actions

    def _build_roll_actions(
        self,
        leg: dict[str, Any],
        later_contracts: list[dict[str, Any]],
        delta_target: float,
        option_type: str,
        underlying_price: Decimal,
        above_strike: bool,
        roll_credit_min_pct: float,
    ) -> list[Action] | None:
        """Build EXIT/ENTER action pair for rolling a single threatened leg.

        Closes the current short leg and opens a new short leg at the next
        expiry with the target delta. Only proceeds if the net credit
        exceeds the minimum threshold.

        Args:
            leg: Current short leg contract dict.
            later_contracts: Contracts at later expiry dates.
            delta_target: Target delta for the new leg.
            option_type: CALL or PUT.
            underlying_price: Current underlying price.
            above_strike: Whether to look above (call) or below (put) the underlying.
            roll_credit_min_pct: Minimum net credit as % of close cost.

        Returns:
            List with EXIT and ENTER actions, or None if no valid roll.
        """
        close_cost = self._mid_price(leg)
        if close_cost is None or close_cost <= Decimal("0"):
            return None

        # Find a new contract at the target delta among later expiries
        new_leg = self._find_by_delta(
            later_contracts,
            delta_target,
            option_type,
            underlying_price,
            above_strike=above_strike,
        )
        if new_leg is None:
            return None

        new_premium = self._mid_price(new_leg)
        if new_premium is None or new_premium <= Decimal("0"):
            return None

        # Net credit: premium received from new short minus cost to close old
        leg_net_credit = new_premium - close_cost
        if leg_net_credit <= Decimal("0"):
            self.logger.info(
                "roll_no_credit",
                option_type=option_type,
                leg=str(leg.get("symbol")),
                close_cost=str(close_cost),
                new_premium=str(new_premium),
            )
            return None

        # Check minimum credit threshold
        net_credit_pct = float(leg_net_credit / close_cost * 100)
        if net_credit_pct < roll_credit_min_pct:
            self.logger.info(
                "roll_credit_below_min",
                option_type=option_type,
                net_credit_pct=round(net_credit_pct, 1),
                min_pct=roll_credit_min_pct,
            )
            return None

        self.logger.info(
            "roll_execute",
            option_type=option_type,
            old_leg=str(leg.get("symbol")),
            new_leg=str(new_leg.get("symbol")),
            net_credit=str(leg_net_credit),
            net_credit_pct=round(net_credit_pct, 1),
        )

        return [
            Action(
                type="EXIT",
                strategy=self.get_name(),
                contract=str(leg.get("symbol", "")),
                side="BUY",
                quantity=Decimal("1"),
                order_type="MARKET",
                reason=(
                    f"Roll {option_type.lower()}: delta exceeded threshold, "
                    f"close {leg.get('symbol')}"
                ),
                metadata={
                    "roll": "true",
                    "leg": option_type.lower(),
                    "net_credit": str(leg_net_credit),
                    "reason": "delta_exceeded",
                },
            ),
            Action(
                type="ENTER",
                strategy=self.get_name(),
                contract=str(new_leg.get("symbol", "")),
                side="SELL",
                quantity=Decimal("1"),
                order_type="LIMIT",
                price=new_premium,
                reason=f"Roll {option_type.lower()}: sell {new_leg.get('symbol')}",
                metadata={
                    "roll": "true",
                    "leg": option_type.lower(),
                    "net_credit": str(leg_net_credit),
                },
            ),
        ]

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
