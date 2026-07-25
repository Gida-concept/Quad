"""Iron Condor strategy implementation.

A 4-leg credit spread: sell OTM put, buy further OTM put (put wing),
sell OTM call, buy further OTM call (call wing). Defined risk with
high probability of profit in low-volatility environments.

Enhanced with IV rank filtering, forced DTE exits, and rolling logic.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import structlog

from quad.strategy.base import ParamSpec, StrategyBase
from quad.types.risk import Action
from quad.types.strategy import StrategyContext


logger = structlog.get_logger(__name__)


class IronCondorStrategy(StrategyBase):
    """4-leg credit spread: short put spread + short call spread.

    Entry conditions:
        - Sell OTM put at -delta_short delta
        - Buy OTM put at short_put_strike - wing_width (put wing)
        - Sell OTM call at +delta_short delta
        - Buy OTM call at short_call_strike + wing_width (call wing)
        - Net credit > min_credit_pct * wing_width
        - DTE within [min_dte, max_dte]
        - Short put IV percentile >= min_iv_rank

    Exit conditions:
        - Take profit: credit decays to take_profit_pct of max credit
        - Forced exit: DTE < force_exit_dte (avoid gamma risk)
        - Stop loss: debit >= max_credit * (1 + stop_loss_pct/100)
        - Near expiry: DTE < 2

    Rolling conditions:
        - Roll when short leg delta exceeds roll_when_delta_exceeds
        - Roll to next expiry at same delta target
        - Require net credit >= roll_credit_min_pct
    """

    @staticmethod
    def get_name() -> str:
        return "iron_condor"

    @staticmethod
    def get_description() -> str:
        return (
            "4-leg credit spread selling an OTM put and OTM call with "
            "wider protective wings. Defined risk, high probability of "
            "profit in low-volatility sideways markets."
        )

    @staticmethod
    def get_params_spec() -> list[ParamSpec]:
        return [
            ParamSpec("min_dte", "int", 30, "Minimum days to expiry", 1, 365),
            ParamSpec("max_dte", "int", 45, "Maximum days to expiry", 1, 365),
            ParamSpec("delta_short", "float", 0.16, "Target delta for short legs", 0.01, 0.50),
            ParamSpec("wing_width_pct", "float", 25.0, "Wing width as % of short strike distance", 5.0, 100.0),
            ParamSpec("min_credit_pct", "float", 20.0, "Min net credit as % of wing width", 1.0, 100.0),
            ParamSpec("take_profit_pct", "float", 50.0, "Take profit when credit decays by this %", 1.0, 100.0),
            ParamSpec("stop_loss_pct", "float", 200.0, "Stop loss when debit increases by this %", 50.0, 500.0),
            ParamSpec("min_iv_rank", "int", 30, "Min IV percentile rank for short put entry", 1, 100),
            ParamSpec("force_exit_dte", "int", 21, "Force exit when DTE drops below this", 1, 365),
            ParamSpec("roll_when_delta_exceeds", "float", 0.40, "Roll short legs when delta exceeds this", 0.01, 0.99),
            ParamSpec("roll_credit_min_pct", "float", 0.0, "Min net credit % for rolling (0 = any credit)", -10.0, 100.0),
        ]

    async def evaluate(self, context: StrategyContext) -> list[Action]:
        """Evaluate iron condor entry/exit/roll conditions.

        Args:
            context: Current market and account context.

        Returns:
            List of Action objects (multiple ENTER legs, EXIT, or HOLD).
        """
        self.logger.info("evaluate_start", strategy=self.get_name())

        if context.underlying_price is None:
            return self.hold_action("No underlying price available")

        if not context.option_chain:
            return self.hold_action("Empty option chain")

        existing_position = self._find_existing_position(context)
        underlying_price = context.underlying_price.price

        if existing_position is None:
            return self._evaluate_entry(context, underlying_price)
        else:
            # 1) Check exit first
            exit_actions = await self._evaluate_exit(context, existing_position)
            if self._has_non_hold(exit_actions):
                return exit_actions

            # 2) If not exiting, check whether to roll
            roll_actions = self._evaluate_roll(context, existing_position, underlying_price)
            if self._has_non_hold(roll_actions):
                return roll_actions

            return exit_actions

    # ---- Entry ----

    def _evaluate_entry(
        self,
        context: StrategyContext,
        underlying_price: Decimal,
    ) -> list[Action]:
        """Evaluate entry for a new iron condor position."""
        min_dte = int(self.get_param("min_dte", 30))
        max_dte = int(self.get_param("max_dte", 45))
        delta_short = abs(float(self.get_param("delta_short", 0.16)))
        wing_width_pct = float(self.get_param("wing_width_pct", 25.0))
        min_credit_pct = float(self.get_param("min_credit_pct", 20.0))
        min_iv_rank = int(self.get_param("min_iv_rank", 30))

        contracts = list(self._iter_contracts(context.option_chain))
        if not contracts:
            return self.hold_action("No contracts available")

        # Filter by DTE range
        in_range = [c for c in contracts if self._dte_in_range(c, min_dte, max_dte)]
        if not in_range:
            return self.hold_action(f"No contracts in DTE range {min_dte}-{max_dte}")

        # Find short call at delta_short
        short_call = self._find_by_delta(in_range, delta_short, "CALL", underlying_price, above_strike=True)
        if short_call is None:
            return self.hold_action("No suitable short call found")

        # Find short put at -delta_short
        short_put = self._find_by_delta(in_range, delta_short, "PUT", underlying_price, above_strike=False)
        if short_put is None:
            return self.hold_action("No suitable short put found")

        # IV rank filter: check short put IV percentile before proceeding
        if min_iv_rank > 0:
            iv_rank = self._compute_iv_percentile(short_put, contracts)
            if iv_rank is not None and iv_rank < min_iv_rank:
                self.logger.info(
                    "iv_rank_below_min",
                    iv_rank=round(iv_rank, 1),
                    min_iv_rank=min_iv_rank,
                    short_put=str(short_put.get("symbol", "")),
                )
                return self.hold_action(
                    f"Short put IV rank {iv_rank:.0f} below min {min_iv_rank}"
                )

        short_call_strike = self._to_decimal(short_call.get("strike", 0))
        short_put_strike = self._to_decimal(short_put.get("strike", 0))
        short_call_premium = self._mid_price(short_call)
        short_put_premium = self._mid_price(short_put)

        if short_call_premium is None or short_put_premium is None:
            return self.hold_action("Cannot price short legs")

        # Calculate wing width in strike units
        wing_distance = self._wing_distance(short_call_strike, short_put_strike, wing_width_pct)

        # Find long call (protective wing) - higher strike
        long_call = self._find_nearest_strike(
            in_range, short_call_strike + wing_distance, "CALL"
        )
        if long_call is None:
            return self.hold_action("No suitable long call wing found")
        long_call_premium = self._mid_price(long_call)
        if long_call_premium is None:
            return self.hold_action("Cannot price long call wing")

        # Find long put (protective wing) - lower strike
        long_put = self._find_nearest_strike(
            in_range, short_put_strike - wing_distance, "PUT"
        )
        if long_put is None:
            return self.hold_action("No suitable long put wing found")
        long_put_premium = self._mid_price(long_put)
        if long_put_premium is None:
            return self.hold_action("Cannot price long put wing")

        # Net credit
        net_credit = short_call_premium + short_put_premium - long_call_premium - long_put_premium
        if net_credit <= Decimal("0"):
            self.logger.info("net_credit_not_positive", net_credit=str(net_credit))
            return self.hold_action(f"Net credit not positive: {net_credit:.2f}")

        width = short_call_strike - short_put_strike
        min_credit = self._to_decimal(width) * Decimal(str(min_credit_pct)) / Decimal("100")

        if net_credit < min_credit:
            self.logger.info(
                "credit_below_min",
                net_credit=str(net_credit),
                min_credit=str(min_credit),
            )
            return self.hold_action(
                f"Net credit {net_credit:.2f} below min {min_credit:.2f}"
            )

        entered_symbol = str(short_call.get("symbol", ""))
        self.logger.info(
            "iron_condor_entry",
            short_call=str(short_call.get("symbol")),
            long_call=str(long_call.get("symbol")),
            short_put=str(short_put.get("symbol")),
            long_put=str(long_put.get("symbol")),
            net_credit=str(net_credit),
            width=str(width),
        )

        return [
            Action(
                type="ENTER",
                strategy=self.get_name(),
                contract=str(short_call.get("symbol", "")),
                side="SELL",
                quantity=Decimal("1"),
                order_type="LIMIT",
                price=short_call_premium,
                reason=f"Iron condor: sell call wing {short_call.get('symbol')}",
                metadata={
                    "leg": "short_call",
                    "strategy": self.get_name(),
                    "net_credit": str(net_credit),
                },
            ),
            Action(
                type="ENTER",
                strategy=self.get_name(),
                contract=str(long_call.get("symbol", "")),
                side="BUY",
                quantity=Decimal("1"),
                order_type="LIMIT",
                price=long_call_premium,
                reason=f"Iron condor: buy call wing {long_call.get('symbol')}",
                metadata={
                    "leg": "long_call",
                    "strategy": self.get_name(),
                    "net_credit": str(net_credit),
                },
            ),
            Action(
                type="ENTER",
                strategy=self.get_name(),
                contract=str(short_put.get("symbol", "")),
                side="SELL",
                quantity=Decimal("1"),
                order_type="LIMIT",
                price=short_put_premium,
                reason=f"Iron condor: sell put wing {short_put.get('symbol')}",
                metadata={
                    "leg": "short_put",
                    "strategy": self.get_name(),
                    "net_credit": str(net_credit),
                },
            ),
            Action(
                type="ENTER",
                strategy=self.get_name(),
                contract=str(long_put.get("symbol", "")),
                side="BUY",
                quantity=Decimal("1"),
                order_type="LIMIT",
                price=long_put_premium,
                reason=f"Iron condor: buy put wing {long_put.get('symbol')}",
                metadata={
                    "leg": "long_put",
                    "strategy": self.get_name(),
                    "net_credit": str(net_credit),
                },
            ),
        ]

    # ---- Exit ----

    async def _evaluate_exit(
        self,
        context: StrategyContext,
        existing_position: dict[str, Any],
    ) -> list[Action]:
        """Evaluate exit for an existing iron condor."""
        take_profit_pct = float(self.get_param("take_profit_pct", 50.0))
        stop_loss_pct = float(self.get_param("stop_loss_pct", 200.0))
        force_exit_dte = int(self.get_param("force_exit_dte", 21))

        # Find all legs in the current chain
        legs = self._find_strategy_legs(context)
        if not legs:
            return self.hold_action("Cannot find all iron condor legs in chain")

        # Calculate current combined value (cost to close all legs)
        current_value = self._combined_value(legs)

        # Estimate max credit from entry metadata
        max_credit = self._estimate_max_credit(context, existing_position)
        if max_credit is None or max_credit <= Decimal("0"):
            return self.hold_action("Cannot determine max credit")

        # Check DTE
        dte_values = [self._calculate_dte(l) for l in legs if self._calculate_dte(l) is not None]
        min_dte = min(dte_values) if dte_values else 999

        if min_dte < 2:
            self.logger.info("exit_near_expiry", dte=min_dte)
            return self._exit_all_actions(legs, "Near expiry", context.positions)

        # Take profit: credit decayed
        decay_pct = float((max_credit - current_value) / max_credit * 100)
        if decay_pct >= take_profit_pct:
            self.logger.info(
                "exit_take_profit",
                decay_pct=round(decay_pct, 1),
                current_value=str(current_value),
            )
            return self._exit_all_actions(
                legs, f"Take profit: credit decayed {decay_pct:.1f}%", context.positions
            )

        # Forced exit: DTE below threshold to avoid gamma risk
        if min_dte < force_exit_dte:
            self.logger.info(
                "exit_forced_gamma_risk",
                dte=min_dte,
                force_exit_dte=force_exit_dte,
            )
            return self._exit_all_actions(
                legs,
                f"Forced exit: DTE {min_dte} below {force_exit_dte} threshold (gamma risk)",
                context.positions,
            )

        # Stop loss: debit increased
        loss_pct = float((current_value - max_credit) / max_credit * 100)
        if current_value > max_credit and loss_pct >= stop_loss_pct:
            self.logger.warning(
                "exit_stop_loss",
                loss_pct=round(loss_pct, 1),
                current_value=str(current_value),
            )
            return self._exit_all_actions(
                legs, f"Stop loss: debit increased {loss_pct:.1f}%", context.positions
            )

        return self.hold_action(f"Iron condor within tolerance (decay {decay_pct:.1f}%)")

    # ---- Rolling ----

    def _evaluate_roll(
        self,
        context: StrategyContext,
        existing_position: dict[str, Any],
        underlying_price: Decimal,
    ) -> list[Action]:
        """Evaluate whether to roll the iron condor to a later expiry.

        When a short leg's current delta exceeds the configured threshold,
        the entire iron condor is closed and a new one is opened at the
        next available expiry with the same delta target. The roll only
        executes if the new position produces a net credit meeting the
        minimum threshold.

        Args:
            context: Current market and account context.
            existing_position: Position dict from _find_existing_position.
            underlying_price: Current underlying asset price.

        Returns:
            List of EXIT and ENTER actions for the roll, or a HOLD action
            if no roll is warranted.
        """
        delta_short = abs(float(self.get_param("delta_short", 0.16)))
        roll_when_delta_exceeds = float(self.get_param("roll_when_delta_exceeds", 0.40))
        roll_credit_min_pct = float(self.get_param("roll_credit_min_pct", 0.0))
        wing_width_pct = float(self.get_param("wing_width_pct", 25.0))

        legs = self._find_strategy_legs(context)
        if not legs:
            return self.hold_action("Cannot find legs for roll check")

        # Separate short call and short put from the legs
        short_call: dict[str, Any] | None = None
        short_put: dict[str, Any] | None = None
        for leg in legs:
            if leg.get("option_type") == "CALL":
                short_call = leg
            elif leg.get("option_type") == "PUT":
                short_put = leg

        if short_call is None or short_put is None:
            return self.hold_action("Cannot find short legs for roll check")

        # Check current delta of each short leg
        short_call_delta = abs(self._to_decimal(short_call.get("delta", 0)))
        short_put_delta = abs(self._to_decimal(short_put.get("delta", 0)))
        threshold = Decimal(str(roll_when_delta_exceeds))

        call_threatened = short_call_delta > threshold
        put_threatened = short_put_delta > threshold

        if not call_threatened and not put_threatened:
            return self.hold_action(
                f"No short leg exceeds delta threshold {roll_when_delta_exceeds} "
                f"(call delta={float(short_call_delta):.2f}, "
                f"put delta={float(short_put_delta):.2f})"
            )

        self.logger.info(
            "roll_triggered",
            call_threatened=call_threatened,
            put_threatened=put_threatened,
            short_call_delta=float(short_call_delta),
            short_put_delta=float(short_put_delta),
        )

        # Find the next expiry beyond the current position's latest expiry
        contracts = list(self._iter_contracts(context.option_chain))
        current_expiries: set[int] = set()
        for leg in legs:
            exp = leg.get("expiry")
            if exp is not None:
                current_expiries.add(int(exp))

        current_max_expiry = max(current_expiries) if current_expiries else 0

        # Filter contracts to those at a later expiry
        later_contracts = [
            c for c in contracts
            if c.get("expiry") is not None
            and int(c.get("expiry", 0)) > current_max_expiry
        ]

        if not later_contracts:
            return self.hold_action("No later expiry available for roll")

        # Build the new iron condor at the next expiry
        new_short_call = self._find_by_delta(
            later_contracts, delta_short, "CALL", underlying_price, above_strike=True
        )
        if new_short_call is None:
            return self.hold_action("Cannot find short call at later expiry")

        new_short_put = self._find_by_delta(
            later_contracts, delta_short, "PUT", underlying_price, above_strike=False
        )
        if new_short_put is None:
            return self.hold_action("Cannot find short put at later expiry")

        new_short_call_strike = self._to_decimal(new_short_call.get("strike", 0))
        new_short_put_strike = self._to_decimal(new_short_put.get("strike", 0))

        new_short_call_premium = self._mid_price(new_short_call)
        new_short_put_premium = self._mid_price(new_short_put)

        if new_short_call_premium is None or new_short_put_premium is None:
            return self.hold_action("Cannot price new short legs for roll")

        # Calculate wing width for the new position
        wing_distance = self._wing_distance(
            new_short_call_strike, new_short_put_strike, wing_width_pct
        )

        # Find new long wings at the later expiry
        new_long_call = self._find_nearest_strike(
            later_contracts, new_short_call_strike + wing_distance, "CALL"
        )
        if new_long_call is None:
            return self.hold_action("Cannot find long call wing for roll")
        new_long_call_premium = self._mid_price(new_long_call)
        if new_long_call_premium is None:
            return self.hold_action("Cannot price new long call wing for roll")

        new_long_put = self._find_nearest_strike(
            later_contracts, new_short_put_strike - wing_distance, "PUT"
        )
        if new_long_put is None:
            return self.hold_action("Cannot find long put wing for roll")
        new_long_put_premium = self._mid_price(new_long_put)
        if new_long_put_premium is None:
            return self.hold_action("Cannot price new long put wing for roll")

        # Net credit from the new position
        new_net_credit = (
            new_short_call_premium + new_short_put_premium
            - new_long_call_premium - new_long_put_premium
        )

        if new_net_credit <= Decimal("0"):
            self.logger.info(
                "roll_new_credit_not_positive",
                new_net_credit=str(new_net_credit),
            )
            return self.hold_action(f"New position net credit not positive: {new_net_credit:.2f}")

        # Verify roll credit minimum: new credit as % of new spread width
        new_width = new_short_call_strike - new_short_put_strike
        min_roll_credit = self._to_decimal(new_width) * Decimal(str(roll_credit_min_pct)) / Decimal("100")

        if new_net_credit < min_roll_credit:
            self.logger.info(
                "roll_credit_below_min",
                new_net_credit=str(new_net_credit),
                min_roll_credit=str(min_roll_credit),
                roll_credit_min_pct=roll_credit_min_pct,
            )
            return self.hold_action(
                f"Roll credit {new_net_credit:.2f} below min {min_roll_credit:.2f}"
            )

        self.logger.info(
            "iron_condor_roll",
            call_threatened=call_threatened,
            put_threatened=put_threatened,
            new_net_credit=str(new_net_credit),
            new_width=str(new_width),
        )

        # Build actions: EXIT current position + ENTER new position
        roll_reason = (
            f"Rolling iron condor: short call delta={float(short_call_delta):.2f}, "
            f"short put delta={float(short_put_delta):.2f}"
        )

        actions = self._exit_all_actions(legs, roll_reason, context.positions)

        # New ENTER legs at the later expiry
        actions.extend([
            Action(
                type="ENTER",
                strategy=self.get_name(),
                contract=str(new_short_call.get("symbol", "")),
                side="SELL",
                quantity=Decimal("1"),
                order_type="LIMIT",
                price=new_short_call_premium,
                reason=f"Roll to {new_short_call.get('symbol')}",
                metadata={
                    "leg": "short_call",
                    "strategy": self.get_name(),
                    "roll": "true",
                    "roll_net_credit": str(new_net_credit),
                },
            ),
            Action(
                type="ENTER",
                strategy=self.get_name(),
                contract=str(new_long_call.get("symbol", "")),
                side="BUY",
                quantity=Decimal("1"),
                order_type="LIMIT",
                price=new_long_call_premium,
                reason=f"Roll to {new_long_call.get('symbol')}",
                metadata={
                    "leg": "long_call",
                    "strategy": self.get_name(),
                    "roll": "true",
                    "roll_net_credit": str(new_net_credit),
                },
            ),
            Action(
                type="ENTER",
                strategy=self.get_name(),
                contract=str(new_short_put.get("symbol", "")),
                side="SELL",
                quantity=Decimal("1"),
                order_type="LIMIT",
                price=new_short_put_premium,
                reason=f"Roll to {new_short_put.get('symbol')}",
                metadata={
                    "leg": "short_put",
                    "strategy": self.get_name(),
                    "roll": "true",
                    "roll_net_credit": str(new_net_credit),
                },
            ),
            Action(
                type="ENTER",
                strategy=self.get_name(),
                contract=str(new_long_put.get("symbol", "")),
                side="BUY",
                quantity=Decimal("1"),
                order_type="LIMIT",
                price=new_long_put_premium,
                reason=f"Roll to {new_long_put.get('symbol')}",
                metadata={
                    "leg": "long_put",
                    "strategy": self.get_name(),
                    "roll": "true",
                    "roll_net_credit": str(new_net_credit),
                },
            ),
        ])

        return actions

    # ---- Helpers ----

    @staticmethod
    def _has_non_hold(actions: list[Action]) -> bool:
        """Check if any action in the list is not a HOLD type."""
        return any(a.type != "HOLD" for a in actions)

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

    def _exit_all_actions(self, legs: list[dict[str, Any]], reason: str,
                          positions: list[Any] | None = None) -> list[Action]:
        """Create EXIT actions for all legs.

        Determines the correct exit side (BUY or SELL) by looking up each
        leg's entry side in the current positions. Short positions (entered
        as SELL) exit via BUY-to-close; long positions (entered as BUY)
        exit via SELL-to-close.
        """
        # Build lookup: contract_symbol -> side from open positions
        pos_side: dict[str, str] = {}
        if positions is not None:
            for p in positions:
                if p.status == "OPEN":
                    pos_side[p.contract_symbol] = p.side

        actions = []
        for leg in legs:
            symbol = str(leg.get("symbol", ""))
            pos_side_val = pos_side.get(symbol)
            if pos_side_val == "SHORT":
                # Short position -> entered via SELL -> close with BUY
                side = "BUY"
            elif pos_side_val == "LONG":
                # Long position -> entered via BUY -> close with SELL
                side = "SELL"
            else:
                # Fallback: reverse of what the leg dict reports (leg dict
                # has no 'side' field for raw contract data, so this falls
                # through to SELL -- better to issue no action than a wrong one)
                self.logger.warning(
                    "exit_unknown_side",
                    symbol=symbol,
                    pos_side=pos_side_val,
                )
                continue
            actions.append(
                Action(
                    type="EXIT",
                    strategy=self.get_name(),
                    contract=symbol,
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
        """Find all iron condor leg contracts in the current chain."""
        contracts = list(self._iter_contracts(context.option_chain))
        # Look for positions opened by this strategy
        position_symbols = {
            p.contract_symbol
            for p in context.positions
            if p.strategy == self.get_name() and p.status == "OPEN"
        }
        legs = [c for c in contracts if c.get("symbol") in position_symbols]
        return legs

    def _find_existing_position(self, context: StrategyContext) -> dict[str, Any] | None:
        positions = [p for p in context.positions if p.strategy == self.get_name()]
        open_positions = [p for p in positions if p.status == "OPEN"]
        if not open_positions:
            return None
        return {"contract_symbols": [p.contract_symbol for p in open_positions]}

    def _estimate_max_credit(
        self,
        context: StrategyContext,
        position: dict[str, Any],
    ) -> Decimal | None:
        """Estimate max credit from entry cost basis of all legs."""
        symbols = position.get("contract_symbols", [])
        total_credit = Decimal("0")
        for p in context.positions:
            if p.contract_symbol in symbols and p.strategy == self.get_name():
                if p.side == "SHORT" or "SELL" in p.side.upper():
                    total_credit += p.entry_price
                else:
                    total_credit -= p.entry_price
        return total_credit if total_credit > Decimal("0") else None

    def _wing_distance(
        self,
        call_strike: Decimal,
        put_strike: Decimal,
        wing_pct: float,
    ) -> Decimal:
        """Calculate wing offset based on spread width."""
        width = call_strike - put_strike
        return width * Decimal(str(wing_pct)) / Decimal("100")
