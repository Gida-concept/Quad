"""Cash-Secured Put strategy implementation.

Sells out-of-the-money (OTM) put options backed by sufficient cash
reserves to cover potential assignment. Generates premium income from
neutral-to-slightly-bullish or neutral-to-slightly-bearish markets.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import structlog

from quad.strategy.base import ParamSpec, StrategyBase
from quad.types.risk import Action
from quad.types.strategy import StrategyContext


logger = structlog.get_logger(__name__)


class CashSecuredPutStrategy(StrategyBase):
    """Sells OTM put options backed by cash reserves for assignment.

    Entry conditions:
        - OTM put with delta closest to delta_target
        - DTE within [min_dte, max_dte]
        - Expected return >= min_return_pct
        - Sufficient cash to cover assignment (strike * cash_reserve_pct)

    Exit conditions:
        - Take profit: premium decays to take_profit_pct of max
        - Stop loss: premium rises to stop_loss_pct of entry
        - Deep ITM: underlying drops below 80% of strike
        - Expiry: DTE drops below 1 day
    """

    @staticmethod
    def get_name() -> str:
        return "cash_secured_put"

    @staticmethod
    def get_description() -> str:
        return (
            "Sell OTM put options backed by cash reserves. "
            "Generates premium income from neutral-to-bullish markets with "
            "defined risk. Strike selection via delta targeting."
        )

    @staticmethod
    def get_params_spec() -> list[ParamSpec]:
        return [
            ParamSpec("min_dte", "int", 7, "Minimum days to expiry", 1, 365),
            ParamSpec("max_dte", "int", 45, "Maximum days to expiry", 1, 365),
            ParamSpec("delta_target", "float", 0.25, "Target absolute delta for put selection", 0.01, 0.99),
            ParamSpec("min_return_pct", "float", 0.5, "Minimum premium return as % of strike", 0.0, 100.0),
            ParamSpec("take_profit_pct", "float", 50.0, "Take profit when premium decays by this %", 1.0, 100.0),
            ParamSpec("stop_loss_pct", "float", 150.0, "Stop loss when premium increases by this %", 50.0, 500.0),
            ParamSpec("cash_reserve_pct", "float", 20.0, "Cash reserve as % of strike for assignment", 1.0, 100.0),
        ]

    async def evaluate(self, context: StrategyContext) -> list[Action]:
        """Evaluate cash-secured put entry/exit conditions.

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

        existing_position = self._find_existing_position(context)
        underlying_price = context.underlying_price.price

        if existing_position is None:
            return await self._evaluate_entry(context, underlying_price)
        else:
            return await self._evaluate_exit(context, underlying_price, existing_position)

    async def _evaluate_entry(
        self,
        context: StrategyContext,
        underlying_price: Decimal,
    ) -> list[Action]:
        """Evaluate entry conditions for a new cash-secured put."""
        min_dte = int(self.get_param("min_dte", 7))
        max_dte = int(self.get_param("max_dte", 45))
        delta_target = abs(float(self.get_param("delta_target", 0.25)))
        min_return_pct = float(self.get_param("min_return_pct", 0.5))
        cash_reserve_pct = float(self.get_param("cash_reserve_pct", 20.0))

        # Filter to OTM puts with DTE in range
        eligible_puts = []
        for contract in self._iter_contracts(context.option_chain):
            dte = self._calculate_dte(contract)
            if dte is None or dte < min_dte or dte > max_dte:
                continue
            if contract.get("option_type") != "PUT":
                continue
            strike = self._to_decimal(contract.get("strike", 0))
            if strike >= underlying_price:
                continue  # OTM only
            eligible_puts.append(contract)

        if not eligible_puts:
            self.logger.info("no_eligible_puts", dte_range=f"{min_dte}-{max_dte}")
            return self.hold_action("No eligible OTM puts in DTE range")

        # Find closest to target delta
        best = min(
            eligible_puts,
            key=lambda c: abs(abs(self._to_decimal(c.get("delta", 0))) - Decimal(str(delta_target))),
        )
        best_strike = self._to_decimal(best.get("strike", 0))
        best_delta = abs(self._to_decimal(best.get("delta", 0)))
        best_premium = self._mid_price(best)

        if best_premium is None or best_premium <= Decimal("0"):
            self.logger.warning("zero_premium_put")
            return self.hold_action("Best put has zero premium")

        # Check sufficient cash for assignment
        reserve_needed = best_strike * Decimal(str(cash_reserve_pct)) / Decimal("100")
        available_cash = self._get_available_cash(context)
        if available_cash is not None and available_cash < reserve_needed:
            self.logger.warning(
                "insufficient_cash",
                needed=str(reserve_needed),
                available=str(available_cash),
            )
            return self.hold_action(
                f"Insufficient cash: need {reserve_needed:.2f}, "
                f"have {available_cash:.2f}"
            )

        # Calculate return as % of strike
        return_pct = float(best_premium / best_strike * 100)
        if return_pct < min_return_pct:
            self.logger.info(
                "return_below_min",
                return_pct=round(return_pct, 2),
                min_return_pct=min_return_pct,
            )
            return self.hold_action(f"Return {return_pct:.2f}% below min {min_return_pct}%")

        self.logger.info(
            "cash_secured_put_entry",
            contract=best.get("symbol"),
            strike=str(best_strike),
            delta=round(float(best_delta), 3),
            premium=str(best_premium),
            return_pct=round(return_pct, 2),
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
                    f"CSP: sell {best.get('symbol')} at {best_premium:.2f} "
                    f"(delta={float(best_delta):.2f})"
                ),
                metadata={
                    "strike": str(best_strike),
                    "delta": float(best_delta),
                    "premium": str(best_premium),
                    "return_pct": return_pct,
                    "underlying_price": str(underlying_price),
                },
            )
        ]

    async def _evaluate_exit(
        self,
        context: StrategyContext,
        underlying_price: Decimal,
        existing_position: dict[str, Any],
    ) -> list[Action]:
        """Evaluate exit conditions for an existing cash-secured put."""
        take_profit_pct = float(self.get_param("take_profit_pct", 50.0))
        stop_loss_pct = float(self.get_param("stop_loss_pct", 150.0))

        contract_symbol = str(existing_position.get("contract_symbol", ""))
        entry_price = self._to_decimal(existing_position.get("entry_price", 0))

        current_contract = self._find_contract_in_chain(context, contract_symbol)
        if current_contract is None:
            self.logger.warning("contract_not_in_chain", symbol=contract_symbol)
            return self.hold_action("Contract not in current chain")

        current_price = self._mid_price(current_contract)
        if current_price is None or current_price < Decimal("0"):
            return self.hold_action("Cannot determine current price")

        strike = self._to_decimal(current_contract.get("strike", 0))

        dte = self._calculate_dte(current_contract)
        if dte is not None and dte < 1:
            self.logger.info("exit_expiry", symbol=contract_symbol)
            return self._exit_action(contract_symbol, "Near expiration")

        # Deep ITM check: underlying below 80% of strike
        if strike > Decimal("0") and underlying_price < strike * Decimal("0.8"):
            self.logger.warning(
                "deep_itm_early_exit",
                symbol=contract_symbol,
                underlying=str(underlying_price),
                strike=str(strike),
            )
            return self._exit_action(
                contract_symbol,
                f"Deep ITM: underlying at {underlying_price:.2f}, "
                f"strike {strike:.2f}",
            )

        if entry_price > Decimal("0"):
            # Take profit
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

            # Stop loss
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

        return self.hold_action("CSP within tolerance")

    # ---- Helpers ----

    def _exit_action(self, contract_symbol: str, reason: str) -> list[Action]:
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

    def _find_existing_position(self, context: StrategyContext) -> dict[str, Any] | None:
        strategy_positions = [p for p in context.positions if p.strategy == self.get_name()]
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
        for contract in self._iter_contracts(context.option_chain):
            if contract.get("symbol") == symbol:
                return contract
        return None

    def _get_available_cash(self, context: StrategyContext) -> Decimal | None:
        """Get available USDT balance for cash securing."""
        if context.account is None:
            return None
        balances = context.account.balances
        usdt_balance = balances.get("USDT")
        if usdt_balance is None:
            return None
        return usdt_balance.free

