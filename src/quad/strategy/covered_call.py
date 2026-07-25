"""Covered Call strategy implementation.

Sells out-of-the-money (OTM) call options against a long underlying
position to generate premium income. The strategy benefits from neutral
to slightly bullish price movement.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import structlog

from quad.strategy.base import ParamSpec, StrategyBase
from quad.types.risk import Action
from quad.types.strategy import StrategyContext


logger = structlog.get_logger(__name__)


class CoveredCallStrategy(StrategyBase):
    """Sells OTM call options against a held long underlying position.

    Entry conditions:
        - OTM call with delta closest to delta_target
        - DTE within [min_dte, max_dte]
        - Expected return >= min_return_pct
        - Account holds the underlying asset
        - IV percentile >= min_iv_rank within expiry chain

    Exit conditions:
        - Take profit: premium decays to take_profit_pct of max
        - Stop loss: premium rises to stop_loss_pct of entry
        - Expiry: DTE drops below 1 day
        - Roll: when DTE < roll_when_dte_lt or delta > roll_when_delta_exceeds
    """

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self._underlying_symbol: str | None = None

    # ---- Parameter specification ----

    @staticmethod
    def get_name() -> str:
        return "covered_call"

    @staticmethod
    def get_description() -> str:
        return (
            "Sell OTM call options against a held long underlying position. "
            "Generates premium income from neutral-to-slightly-bullish markets. "
            "Delta-targeted strike selection with configurable DTE range."
        )

    @staticmethod
    def get_params_spec() -> list[ParamSpec]:
        return [
            ParamSpec("min_dte", "int", 7, "Minimum days to expiry", 1, 365),
            ParamSpec("max_dte", "int", 45, "Maximum days to expiry", 1, 365),
            ParamSpec("delta_target", "float", 0.25, "Target delta for call selection", 0.01, 0.99),
            ParamSpec("min_return_pct", "float", 0.5, "Minimum premium return as % of underlying", 0.0, 100.0),
            ParamSpec("take_profit_pct", "float", 50.0, "Take profit when premium decays by this %", 1.0, 100.0),
            ParamSpec("stop_loss_pct", "float", 200.0, "Stop loss when premium increases by this %", 50.0, 500.0),
            ParamSpec("min_iv_rank", "float", 30.0, "Minimum IV percentile rank within expiry chain", 0.0, 100.0),
            ParamSpec("roll_when_dte_lt", "int", 3, "Roll position when DTE drops below this", 1, 45),
            ParamSpec("roll_when_delta_exceeds", "float", 0.40, "Roll when short delta exceeds this threshold", 0.01, 0.99),
            ParamSpec("roll_credit_min_pct", "float", 10.0, "Minimum net credit as % of current premium to roll", 0.0, 100.0),
        ]

    # ---- Core evaluation ----

    async def evaluate(self, context: StrategyContext) -> list[Action]:
        """Evaluate covered call entry/exit conditions.

        Args:
            context: Current market and account context.

        Returns:
            List of Action objects (ENTER, EXIT, or HOLD).
        """
        self.logger.info("evaluate_start", strategy=self.get_name())

        if context.underlying_price is None:
            self.logger.warning("no_underlying_price")
            return self.hold_action("No underlying price available")

        if not context.option_chain:
            self.logger.warning("empty_option_chain")
            return self.hold_action("Empty option chain")

        underlying_price = context.underlying_price.price
        self._underlying_symbol = context.underlying_price.symbol

        # Check we hold the underlying asset
        if not self._has_underlying(context):
            self.logger.warning(
                "no_underlying_position",
                underlying=self._underlying_symbol,
            )
            return self.hold_action(f"No {self._underlying_symbol} position held")

        # Check if we already have a position for this strategy — determine entry vs exit
        existing_position = self._find_existing_position(context)

        if existing_position is None:
            return await self._evaluate_entry(context, underlying_price)
        else:
            return await self._evaluate_exit(context, underlying_price, existing_position)

    async def _evaluate_entry(
        self,
        context: StrategyContext,
        underlying_price: Decimal,
    ) -> list[Action]:
        """Evaluate whether to enter a new covered call position."""
        min_dte = int(self.get_param("min_dte", 7))
        max_dte = int(self.get_param("max_dte", 45))
        delta_target = float(self.get_param("delta_target", 0.25))
        min_return_pct = float(self.get_param("min_return_pct", 0.5))
        min_iv_rank = float(self.get_param("min_iv_rank", 30.0))

        # Filter to OTM calls with DTE in range
        eligible_calls = []
        for contract in context.option_chain:
            contract_dict = self._to_dict(contract)
            dte = self._calculate_dte(contract_dict)
            if dte is None or dte < min_dte or dte > max_dte:
                continue
            if contract_dict.get("option_type") != "CALL":
                continue
            strike = self._to_decimal(contract_dict.get("strike", 0))
            if strike <= underlying_price:
                continue  # OTM only
            eligible_calls.append(contract_dict)

        if not eligible_calls:
            self.logger.info("no_eligible_calls", dte_range=f"{min_dte}-{max_dte}")
            return self.hold_action("No eligible OTM calls in DTE range")

        # Find closest to delta_target
        best = min(
            eligible_calls,
            key=lambda c: abs(abs(self._to_decimal(c.get("delta", 0))) - Decimal(str(delta_target))),
        )
        best_delta = abs(self._to_decimal(best.get("delta", 0)))
        best_strike = self._to_decimal(best.get("strike", 0))
        best_premium = self._mid_price(best)

        if best_premium <= Decimal("0"):
            self.logger.warning("zero_premium_call")
            return self.hold_action("Best call has zero premium")

        # IV rank filter: ensure IV is in the upper percentile of its expiry chain
        iv_percentile = self._compute_iv_percentile(best, context.option_chain)
        if iv_percentile < min_iv_rank:
            self.logger.info(
                "iv_percentile_below_min",
                contract=best.get("symbol"),
                iv_percentile=round(iv_percentile, 1),
                min_iv_rank=min_iv_rank,
            )
            return self.hold_action(
                f"IV percentile {iv_percentile:.1f}% below min {min_iv_rank}%"
            )

        # Calculate expected return
        return_pct = float(best_premium / underlying_price * 100)
        if return_pct < min_return_pct:
            self.logger.info(
                "return_below_min",
                return_pct=round(return_pct, 2),
                min_return_pct=min_return_pct,
            )
            return self.hold_action(f"Return {return_pct:.2f}% below min {min_return_pct}%")

        self.logger.info(
            "covered_call_entry",
            contract=best.get("symbol"),
            strike=str(best_strike),
            delta=round(float(best_delta), 3),
            premium=str(best_premium),
            return_pct=round(return_pct, 2),
            iv_percentile=round(iv_percentile, 1),
        )

        return [
            Action(
                type="ENTER",
                strategy=self.get_name(),
                contract=str(best.get("symbol", "")),
                side="SELL",
                quantity=Decimal("1"),
                order_type="LIMIT",
                price=best_premium,
                reason=(
                    f"Covered call: sell {best.get('symbol')} "
                    f"at {best_premium:.2f} "
                    f"(delta={float(best_delta):.2f}, "
                    f"return={return_pct:.1f}%)"
                ),
                metadata={
                    "strike": str(best_strike),
                    "delta": float(best_delta),
                    "premium": str(best_premium),
                    "return_pct": return_pct,
                    "underlying_price": str(underlying_price),
                    "iv_percentile": iv_percentile,
                },
            )
        ]

    async def _evaluate_exit(
        self,
        context: StrategyContext,
        underlying_price: Decimal,
        existing_position: dict[str, Any],
    ) -> list[Action]:
        """Evaluate whether to exit an existing covered call."""
        take_profit_pct = float(self.get_param("take_profit_pct", 50.0))
        stop_loss_pct = float(self.get_param("stop_loss_pct", 200.0))
        roll_when_dte_lt = int(self.get_param("roll_when_dte_lt", 3))
        roll_when_delta_exceeds = float(self.get_param("roll_when_delta_exceeds", 0.40))

        contract_symbol = str(existing_position.get("contract_symbol", ""))
        entry_price = self._to_decimal(existing_position.get("entry_price", 0))

        # Find current contract in chain
        current_contract = self._find_contract_in_chain(context, contract_symbol)
        if current_contract is None:
            self.logger.warning("contract_not_in_chain", symbol=contract_symbol)
            return self.hold_action("Contract not in current chain")

        current_price = self._mid_price(current_contract)
        if current_price is None or current_price < Decimal("0"):
            return self.hold_action("Cannot determine current price")

        dte = self._calculate_dte(current_contract)
        if dte is not None and dte < 1:
            self.logger.info("exit_expiry", symbol=contract_symbol)
            return self._exit_action(contract_symbol, "Near expiration, rolling")

        # Check if we should roll — near expiry or delta too high (being tested)
        current_delta = abs(float(self._to_decimal(current_contract.get("delta", 0))))
        should_roll = (
            (dte is not None and dte < roll_when_dte_lt)
            or current_delta > roll_when_delta_exceeds
        )
        if should_roll:
            roll_actions = await self._evaluate_roll(context, current_contract, underlying_price)
            if roll_actions is not None:
                return roll_actions

        # Take profit check
        if entry_price > Decimal("0"):
            decay_pct = float((entry_price - current_price) / entry_price * 100)
            if decay_pct >= take_profit_pct:
                self.logger.info(
                    "exit_take_profit",
                    symbol=contract_symbol,
                    decay_pct=round(decay_pct, 1),
                )
                return self._exit_action(
                    contract_symbol,
                    f"Take profit: premium decayed {decay_pct:.1f}%",
                )

            # Stop loss check
            loss_pct = float((current_price - entry_price) / entry_price * 100)
            if loss_pct >= stop_loss_pct:
                self.logger.warning(
                    "exit_stop_loss",
                    symbol=contract_symbol,
                    loss_pct=round(loss_pct, 1),
                )
                return self._exit_action(
                    contract_symbol,
                    f"Stop loss: premium increased {loss_pct:.1f}%",
                )

        return self.hold_action("Covered call within tolerance")

    async def _evaluate_roll(
        self,
        context: StrategyContext,
        current_contract: dict[str, Any],
        underlying_price: Decimal,
    ) -> list[Action] | None:
        """Evaluate rolling the covered call up and/or out for a net credit.

        When the short call is tested (delta exceeded or near expiry), this
        method finds the next available expiry with contracts in the DTE range
        and selects a higher-strike OTM call at the delta target. Returns a
        pair of EXIT (buy back current) + ENTER (sell new) actions when the
        roll generates at least roll_credit_min_pct net credit. Returns None
        when no profitable roll is available.
        """
        delta_target = float(self.get_param("delta_target", 0.25))
        roll_credit_min_pct = float(self.get_param("roll_credit_min_pct", 10.0))
        min_dte = int(self.get_param("min_dte", 7))
        max_dte = int(self.get_param("max_dte", 45))

        current_expiry = current_contract.get("expiry")
        current_strike = self._to_decimal(current_contract.get("strike", 0))
        current_price = self._mid_price(current_contract)
        if current_price is None or current_price <= Decimal("0"):
            return None

        # Collect unique expiry timestamps ordered by time
        unique_expiries: dict[int, dict[str, Any]] = {}
        for c in self._iter_contracts(context.option_chain):
            expiry = c.get("expiry")
            if expiry is not None and expiry not in unique_expiries:
                unique_expiries[expiry] = c

        # Find the next expiry after current that falls within DTE range
        next_expiry: int | None = None
        current_expiry_ts = current_expiry or 0
        for expiry in sorted(unique_expiries.keys()):
            if expiry <= current_expiry_ts:
                continue
            sample = unique_expiries[expiry]
            dte = self._calculate_dte(sample)
            if dte is not None and min_dte <= dte <= max_dte:
                next_expiry = expiry
                break

        if next_expiry is None:
            self.logger.info("roll_no_next_expiry")
            return None

        # Filter to OTM calls at the next expiry, preferring higher strikes
        eligible = []
        for contract in self._iter_contracts(context.option_chain):
            if contract.get("expiry") != next_expiry:
                continue
            if contract.get("option_type") != "CALL":
                continue
            strike = self._to_decimal(contract.get("strike", 0))
            if strike <= underlying_price:
                continue  # OTM only
            eligible.append(contract)

        if not eligible:
            self.logger.info("roll_no_eligible_calls", expiry=next_expiry)
            return None

        # Prefer strikes higher than current (roll up), fall back to any OTM
        roll_candidates = [
            c for c in eligible
            if self._to_decimal(c.get("strike", 0)) > current_strike
        ]
        if not roll_candidates:
            roll_candidates = eligible

        # Find closest to delta target
        target = Decimal(str(delta_target))
        new_contract = min(
            roll_candidates,
            key=lambda c: abs(abs(self._to_decimal(c.get("delta", 0))) - target),
        )

        new_premium = self._mid_price(new_contract)
        if new_premium is None or new_premium <= Decimal("0"):
            return None

        # Net credit: receive new premium, pay current price to buy back
        net_credit = new_premium - current_price
        min_credit = current_price * Decimal(str(roll_credit_min_pct)) / Decimal("100")

        if net_credit < min_credit:
            self.logger.info(
                "roll_credit_below_min",
                net_credit=str(net_credit),
                min_credit=str(min_credit),
            )
            return None

        new_strike = self._to_decimal(new_contract.get("strike", 0))
        new_delta = abs(self._to_decimal(new_contract.get("delta", 0)))

        self.logger.info(
            "cc_roll",
            old_contract=str(current_contract.get("symbol")),
            new_contract=str(new_contract.get("symbol")),
            net_credit=str(net_credit),
        )

        return [
            Action(
                type="EXIT",
                strategy=self.get_name(),
                contract=str(current_contract.get("symbol", "")),
                side="BUY",
                quantity=Decimal("1"),
                order_type="MARKET",
                reason=f"Roll CC: buy back {current_contract.get('symbol')}",
                metadata={"roll": True},
            ),
            Action(
                type="ENTER",
                strategy=self.get_name(),
                contract=str(new_contract.get("symbol", "")),
                side="SELL",
                quantity=Decimal("1"),
                order_type="LIMIT",
                price=new_premium,
                reason=(
                    f"Roll CC: sell {new_contract.get('symbol')} "
                    f"at {new_premium:.2f} "
                    f"(strike {float(new_strike):.0f}, "
                    f"delta={float(new_delta):.2f})"
                ),
                metadata={
                    "strike": str(new_strike),
                    "delta": float(new_delta),
                    "premium": str(new_premium),
                    "underlying_price": str(underlying_price),
                    "roll_from": str(current_contract.get("symbol", "")),
                },
            ),
        ]

    def _compute_iv_percentile(
        self,
        contract: dict[str, Any],
        option_chain: list,
    ) -> float:
        """Compute IV percentile of a contract within its expiry chain.

        Ranks the contract's implied volatility against all other contracts
        sharing the same expiry. Returns a value between 0 and 100.

        Args:
            contract: The target contract dict.
            option_chain: Full option chain list.

        Returns:
            IV percentile (0-100), or 0 if data is insufficient.
        """
        contract_iv = float(self._to_decimal(contract.get("implied_volatility", 0)))
        contract_expiry = contract.get("expiry")

        if contract_iv <= 0 or contract_expiry is None:
            return 0.0

        # Collect IVs of all contracts with the same expiry
        ivs: list[float] = []
        for c in self._iter_contracts(option_chain):
            expiry = c.get("expiry")
            iv = float(self._to_decimal(c.get("implied_volatility", 0)))
            if expiry == contract_expiry and iv > 0:
                ivs.append(iv)

        if not ivs:
            return 0.0

        ivs.sort()
        rank = sum(1 for iv in ivs if iv <= contract_iv) - 1
        if len(ivs) <= 1:
            return 100.0
        return (rank / (len(ivs) - 1)) * 100

    # ---- Helpers ----

    def _exit_action(self, contract_symbol: str, reason: str) -> list[Action]:
        """Create an EXIT action for the given contract."""
        return [
            Action(
                type="EXIT",
                strategy=self.get_name(),
                contract=contract_symbol,
                side="BUY",
                quantity=Decimal("1"),
                order_type="MARKET",
                reason=reason,
                metadata={"exit_reason": reason},
            )
        ]

    def _has_underlying(self, context: StrategyContext) -> bool:
        """Check if the account holds the underlying asset."""
        if context.account is None or not context.account.balances:
            return False
        if self._underlying_symbol is None:
            return False
        base_asset = self._underlying_symbol.replace("USDT", "")
        balance = context.account.balances.get(base_asset)
        if balance is None:
            balance = context.account.balances.get(self._underlying_symbol)
        if balance is None:
            return False
        return balance.free > Decimal("0") or balance.locked > Decimal("0")

    def _find_existing_position(
        self,
        context: StrategyContext,
    ) -> dict[str, Any] | None:
        """Find an existing position opened by this strategy."""
        strategy_positions = [
            p for p in context.positions
            if p.strategy == self.get_name()
        ]
        if not strategy_positions:
            return None
        # Return the most recent open position
        open_positions = [p for p in strategy_positions if p.status == "OPEN"]
        if not open_positions:
            return None
        return {
            "contract_symbol": open_positions[-1].contract_symbol,
            "entry_price": open_positions[-1].entry_price,
            "quantity": open_positions[-1].quantity,
        }

    def _find_contract_in_chain(
        self,
        context: StrategyContext,
        symbol: str,
    ) -> dict[str, Any] | None:
        """Find a specific contract in the option chain by symbol."""
        for contract in context.option_chain:
            contract_dict = self._to_dict(contract)
            if contract_dict.get("symbol") == symbol:
                return contract_dict
        return None
