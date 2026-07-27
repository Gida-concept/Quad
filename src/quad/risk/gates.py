"""Pre-trade gate pipeline for futures risk checking.

Provides the GatePipeline class with nine sequential gates that every
proposed Action must pass before execution. Short-circuits on first
failure or runs all gates for diagnostic purposes.

Gates cover position limits, portfolio risk, daily loss, drawdown,
liquidation distance, funding rate cost, leverage, concentration,
and correlation checks.
"""

from __future__ import annotations

import structlog
from decimal import Decimal
from typing import Any

from quad.types.risk import Action, RiskResult
from quad.types.strategy import StrategyContext


# ---------------------------------------------------------------------------
# Gate names (constants for consistency)
# ---------------------------------------------------------------------------

MAX_POSITIONS_GATE = "MAX_POSITIONS_GATE"
PORTFOLIO_RISK_GATE = "PORTFOLIO_RISK_GATE"
DAILY_LOSS_GATE = "DAILY_LOSS_GATE"
DRAWDOWN_GATE = "DRAWDOWN_GATE"
LIQUIDATION_RISK_GATE = "LIQUIDATION_RISK_GATE"
FUNDING_RATE_COST_GATE = "FUNDING_RATE_COST_GATE"
LEVERAGE_LIMIT_GATE = "LEVERAGE_LIMIT_GATE"
POSITION_CONCENTRATION_GATE = "POSITION_CONCENTRATION_GATE"
CORRELATION_GATE = "CORRELATION_GATE"

ALL_GATES = [
    MAX_POSITIONS_GATE,
    PORTFOLIO_RISK_GATE,
    DAILY_LOSS_GATE,
    DRAWDOWN_GATE,
    LIQUIDATION_RISK_GATE,
    FUNDING_RATE_COST_GATE,
    LEVERAGE_LIMIT_GATE,
    POSITION_CONCENTRATION_GATE,
    CORRELATION_GATE,
]

# ---------------------------------------------------------------------------
# Helper: identify entry actions
# ---------------------------------------------------------------------------

def _is_entry(action_type: str) -> bool:
    """Return True for action types that open a new position."""
    return action_type in ("open_long", "open_short")


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


class GatePipeline:
    """Nine-gate pre-trade risk check pipeline for futures trading.

    Every proposed Action must pass ALL gates before execution.
    Each gate returns ``RiskResult(passed=True/False, gate=name, ...)``.
    Short-circuits on the first failure for performance (fail-fast).

    Parameters
    ----------
    config:
        Configuration dictionary. The constructor attempts to extract the
        ``risk`` sub-dict via ``config.get('risk', config)``, so callers
        may pass either the full config or the risk section directly.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self._log = structlog.get_logger(__name__)

        self._cfg: dict[str, Any] = config["risk"]

        # Gate enable/disable flags -- all enabled by default
        self._enabled: dict[str, bool] = {g: True for g in ALL_GATES}

    # ------------------------------------------------------------------
    # Public evaluation API
    # ------------------------------------------------------------------

    async def evaluate(
        self, action: Action, context: StrategyContext
    ) -> RiskResult:
        """Run through all 9 gates. Short-circuits on first failure.

        Returns
        -------
        RiskResult
            ``passed=True`` if all enabled gates pass, otherwise the first
            failing gate result.
        """
        for gate_fn in self._gate_sequence():
            result = await gate_fn(action, context)
            if not result.passed:
                self._log.warning(
                    "gate_blocked",
                    gate=result.gate,
                    reason=result.reason,
                    action_type=action.type,
                    symbol=action.symbol,
                )
                return result
        return RiskResult(
            passed=True,
            gate="ALL",
            reason="All gates passed",
            details={
                "gates_checked": [
                    g for g in ALL_GATES if self._enabled.get(g, False)
                ]
            },
        )

    async def evaluate_all(
        self, action: Action, context: StrategyContext
    ) -> list[RiskResult]:
        """Run ALL gates regardless of failure (diagnostic mode).

        Returns
        -------
        list[RiskResult]
            One result per enabled gate in order.
        """
        results: list[RiskResult] = []
        for gate_fn in self._gate_sequence():
            result = await gate_fn(action, context)
            results.append(result)
        return results

    # ------------------------------------------------------------------
    # Gate configuration
    # ------------------------------------------------------------------

    def get_gate_status(self) -> dict[str, bool]:
        """Return whether each gate is currently enabled."""
        return dict(self._enabled)

    def set_gate_enabled(self, gate_name: str, enabled: bool) -> None:
        """Enable or disable a specific gate by name.

        Raises
        ------
        ValueError
            If *gate_name* is not a recognised gate.
        """
        if gate_name not in ALL_GATES:
            msg = f"Unknown gate: {gate_name}. Valid gates: {ALL_GATES}"
            raise ValueError(msg)
        self._enabled[gate_name] = enabled
        self._log.info("gate_toggled", gate=gate_name, enabled=enabled)

    # ------------------------------------------------------------------
    # Internal: individual gate implementations
    # ------------------------------------------------------------------

    async def _check_max_positions(
        self, action: Action, context: StrategyContext
    ) -> RiskResult:
        """Gate: reject if number of open positions would exceed the limit."""
        limit = int(self._cfg["max_positions"])
        # Count futures positions with non-zero size
        open_count = len(
            [
                p
                for p in (context.futures_positions or [])
                if abs(p.size) > 0
            ]
        )

        # Entry actions count toward the limit
        would_add = 1 if _is_entry(action.type) else 0
        total = open_count + would_add

        if total > limit:
            return RiskResult(
                passed=False,
                gate=MAX_POSITIONS_GATE,
                reason=(
                    f"Position limit {limit} reached "
                    f"({open_count} open, {would_add} adding)"
                ),
                details={
                    "open_positions": open_count,
                    "limit": limit,
                    "would_add": bool(would_add),
                },
            )
        return RiskResult(
            passed=True,
            gate=MAX_POSITIONS_GATE,
            reason=f"{total} <= {limit} positions",
        )

    async def _check_portfolio_risk(
        self, action: Action, context: StrategyContext
    ) -> RiskResult:
        """Gate: reject if total notional exposure exceeds portfolio risk %."""
        max_risk_pct = Decimal(str(self._cfg["max_portfolio_risk_pct"]))
        portfolio_value = (
            context.account.total_usdt if context.account else Decimal("0")
        )
        if portfolio_value <= Decimal("0"):
            return RiskResult(
                passed=True,
                gate=PORTFOLIO_RISK_GATE,
                reason="No portfolio value to evaluate",
            )

        # Sum absolute notional values from existing futures positions
        total_notional = Decimal("0")
        for pos in context.futures_positions or []:
            size = Decimal(str(abs(pos.size)))
            mark_price = Decimal(str(pos.mark_price))
            total_notional += size * mark_price

        # Include proposed position notional for entry actions
        if _is_entry(action.type):
            proposed_notional = abs(action.quantity)
            total_notional += proposed_notional

        risk_pct = (total_notional / portfolio_value) * Decimal("100")

        if risk_pct > max_risk_pct:
            return RiskResult(
                passed=False,
                gate=PORTFOLIO_RISK_GATE,
                reason=(
                    f"Portfolio notional risk {risk_pct:.2f}% exceeds "
                    f"limit of {max_risk_pct:.2f}%"
                ),
                details={
                    "risk_pct": str(risk_pct.quantize(Decimal("0.01"))),
                    "limit_pct": str(max_risk_pct),
                    "total_notional": str(total_notional.quantize(Decimal("0.01"))),
                    "portfolio_value": str(portfolio_value),
                },
            )
        return RiskResult(
            passed=True,
            gate=PORTFOLIO_RISK_GATE,
            reason=f"Risk {risk_pct:.2f}% within {max_risk_pct:.2f}% limit",
        )

    async def _check_daily_loss(
        self, action: Action, context: StrategyContext
    ) -> RiskResult:
        """Gate: reject if daily realised loss exceeds the configured limit."""
        max_loss = Decimal(str(self._cfg["max_daily_loss_usd"]))
        daily_pnl = (
            context.risk_status.daily_pnl
            if context.risk_status
            else Decimal("0")
        )

        if daily_pnl < -max_loss:
            return RiskResult(
                passed=False,
                gate=DAILY_LOSS_GATE,
                reason=(
                    f"Daily loss {daily_pnl:.2f} exceeds limit "
                    f"{-max_loss:.2f}"
                ),
                details={
                    "daily_pnl": str(daily_pnl),
                    "max_daily_loss": str(-max_loss),
                },
            )
        return RiskResult(
            passed=True,
            gate=DAILY_LOSS_GATE,
            reason=f"Daily PnL {daily_pnl:.2f} within limit",
        )

    async def _check_drawdown(
        self, action: Action, context: StrategyContext
    ) -> RiskResult:
        """Gate: reject if current drawdown exceeds the configured limit."""
        max_dd_pct = Decimal(str(self._cfg["max_drawdown_pct"]))
        current_dd = (
            context.risk_status.drawdown_percent
            if context.risk_status
            else Decimal("0")
        )

        if current_dd > max_dd_pct:
            return RiskResult(
                passed=False,
                gate=DRAWDOWN_GATE,
                reason=(
                    f"Drawdown {current_dd:.2f}% exceeds limit "
                    f"of {max_dd_pct:.2f}%"
                ),
                details={
                    "drawdown_pct": str(current_dd),
                    "limit_pct": str(max_dd_pct),
                },
            )
        return RiskResult(
            passed=True,
            gate=DRAWDOWN_GATE,
            reason=(
                f"Drawdown {current_dd:.2f}% within "
                f"{max_dd_pct:.2f}% limit"
            ),
        )

    async def _check_liquidation_risk(
        self, action: Action, context: StrategyContext
    ) -> RiskResult:
        """Gate: reject if any existing position is too close to liquidation.

        For LONG: distance = (mark_price - liquidation_price) / mark_price
        For SHORT: distance = (liquidation_price - mark_price) / mark_price

        If distance < min_distance_to_liquidation_pct, the position is
        considered at risk and no new trades are permitted.
        """
        min_distance = Decimal(str(self._cfg["min_distance_to_liquidation_pct"]))

        near_liquidation: list[dict[str, Any]] = []
        for pos in context.futures_positions or []:
            if pos.liquidation_price <= 0 or pos.mark_price <= 0:
                continue

            if pos.position_side.value == "long":
                distance = (
                    pos.mark_price - pos.liquidation_price
                ) / pos.mark_price
            else:
                distance = (
                    pos.liquidation_price - pos.mark_price
                ) / pos.mark_price

            if distance < float(min_distance):
                near_liquidation.append({
                    "symbol": pos.symbol,
                    "side": pos.position_side.value,
                    "distance_pct": round(distance * 100, 2),
                    "min_distance_pct": round(float(min_distance) * 100, 2),
                })

        if near_liquidation:
            symbols = [n["symbol"] for n in near_liquidation]
            return RiskResult(
                passed=False,
                gate=LIQUIDATION_RISK_GATE,
                reason=(
                    f"Position(s) {symbols} too close to liquidation: "
                    f"{near_liquidation}"
                ),
                details={
                    "near_liquidation": near_liquidation,
                    "min_distance_pct": str(min_distance * Decimal("100")),
                },
            )
        return RiskResult(
            passed=True,
            gate=LIQUIDATION_RISK_GATE,
            reason="No positions close to liquidation",
        )

    async def _check_funding_rate_cost(
        self, action: Action, context: StrategyContext
    ) -> RiskResult:
        """Gate: reject if projected funding cost exceeds allowable ratio.

        For entry actions, estimate holding cost over N funding periods
        (default 3 periods = 24 h). Reject if
        cost > max_funding_rate_cost * position_value.
        """
        max_cost_ratio = Decimal(str(self._cfg["max_funding_rate_cost"]))
        funding_periods = int(self._cfg["funding_rate_periods"])

        # Only check for entry actions
        if not _is_entry(action.type):
            return RiskResult(
                passed=True,
                gate=FUNDING_RATE_COST_GATE,
                reason="Not an entry action",
            )

        position_value = abs(action.quantity)
        if position_value <= Decimal("0"):
            return RiskResult(
                passed=True,
                gate=FUNDING_RATE_COST_GATE,
                reason="No position value",
            )

        # Look up current funding rate for the proposed symbol
        fr_entry = context.funding_rates.get(action.symbol)
        if fr_entry is None:
            return RiskResult(
                passed=True,
                gate=FUNDING_RATE_COST_GATE,
                reason=f"No funding rate data for {action.symbol}",
            )

        funding_rate = abs(Decimal(str(fr_entry.funding_rate)))
        if funding_rate <= Decimal("0"):
            return RiskResult(
                passed=True,
                gate=FUNDING_RATE_COST_GATE,
                reason="Funding rate is zero or negative",
            )

        projected_cost = position_value * funding_rate * Decimal(
            str(funding_periods)
        )
        cost_limit = position_value * max_cost_ratio

        if projected_cost > cost_limit:
            return RiskResult(
                passed=False,
                gate=FUNDING_RATE_COST_GATE,
                reason=(
                    f"Projected funding cost {projected_cost:.2f} exceeds "
                    f"limit {cost_limit:.2f} for {action.symbol} "
                    f"(rate={funding_rate:.6f})"
                ),
                details={
                    "symbol": action.symbol,
                    "projected_cost": str(projected_cost.quantize(Decimal("0.01"))),
                    "cost_limit": str(cost_limit.quantize(Decimal("0.01"))),
                    "funding_rate": str(funding_rate),
                    "periods": funding_periods,
                },
            )
        return RiskResult(
            passed=True,
            gate=FUNDING_RATE_COST_GATE,
            reason=(
                f"Funding cost {projected_cost:.2f} within "
                f"{cost_limit:.2f} limit"
            ),
        )

    async def _check_leverage_limit(
        self, action: Action, context: StrategyContext
    ) -> RiskResult:
        """Gate: reject if effective leverage exceeds the max leverage.

        Computes total notional / wallet balance. For entry actions the
        proposed notional is included in the calculation.
        """
        max_leverage = int(self._cfg["max_leverage"])
        # Also respect account-level max leverage
        if context.account and context.account.max_leverage > 0:
            max_leverage = min(
                max_leverage, context.account.max_leverage
            )

        wallet_balance = (
            context.account.total_wallet_balance
            if context.account
            else Decimal("0")
        )
        if wallet_balance <= Decimal("0"):
            return RiskResult(
                passed=True,
                gate=LEVERAGE_LIMIT_GATE,
                reason="No wallet balance to evaluate",
            )

        # Compute current total notional
        total_notional = Decimal("0")
        for pos in context.futures_positions or []:
            size = Decimal(str(abs(pos.size)))
            mark_price = Decimal(str(pos.mark_price))
            total_notional += size * mark_price

        # Include proposed position for entry actions
        if _is_entry(action.type):
            total_notional += abs(action.quantity)

        effective_leverage = total_notional / wallet_balance

        if effective_leverage > Decimal(str(max_leverage)):
            return RiskResult(
                passed=False,
                gate=LEVERAGE_LIMIT_GATE,
                reason=(
                    f"Effective leverage {effective_leverage:.2f}x exceeds "
                    f"max {max_leverage}x"
                ),
                details={
                    "effective_leverage": str(effective_leverage.quantize(Decimal("0.01"))),
                    "max_leverage": max_leverage,
                    "total_notional": str(total_notional.quantize(Decimal("0.01"))),
                    "wallet_balance": str(wallet_balance),
                },
            )
        return RiskResult(
            passed=True,
            gate=LEVERAGE_LIMIT_GATE,
            reason=(
                f"Leverage {effective_leverage:.2f}x within "
                f"{max_leverage}x limit"
            ),
        )

    async def _check_position_concentration(
        self, action: Action, context: StrategyContext
    ) -> RiskResult:
        """Gate: reject if a single symbol exceeds concentration limit.

        Sums notional value per symbol and compares against
        max_position_concentration * portfolio_value.
        """
        max_conc = Decimal(str(self._cfg["max_position_concentration"]))
        portfolio_value = (
            context.account.total_usdt if context.account else Decimal("0")
        )
        if portfolio_value <= Decimal("0"):
            return RiskResult(
                passed=True,
                gate=POSITION_CONCENTRATION_GATE,
                reason="No portfolio value to evaluate",
            )

        # Compute existing notional per symbol
        notional_by_symbol: dict[str, Decimal] = {}
        for pos in context.futures_positions or []:
            sym = pos.symbol
            size = Decimal(str(abs(pos.size)))
            mark_price = Decimal(str(pos.mark_price))
            notional = size * mark_price
            notional_by_symbol[sym] = (
                notional_by_symbol.get(sym, Decimal("0")) + notional
            )

        # Add proposed position notional for entry actions
        target_symbol = action.symbol
        if _is_entry(action.type) and action.quantity > Decimal("0"):
            notional_by_symbol[target_symbol] = (
                notional_by_symbol.get(target_symbol, Decimal("0"))
                + abs(action.quantity)
            )

        # Check each symbol against the concentration limit
        limit_value = max_conc * portfolio_value
        violations: list[dict[str, Any]] = []
        for sym, notional in notional_by_symbol.items():
            if notional > limit_value:
                conc_pct = (notional / portfolio_value) * Decimal("100")
                violations.append({
                    "symbol": sym,
                    "notional": str(notional.quantize(Decimal("0.01"))),
                    "concentration_pct": str(conc_pct.quantize(Decimal("0.01"))),
                    "limit_pct": str((max_conc * Decimal("100")).quantize(Decimal("0.01"))),
                })

        if violations:
            return RiskResult(
                passed=False,
                gate=POSITION_CONCENTRATION_GATE,
                reason=f"Concentration violation(s): {violations}",
                details={"violations": violations},
            )
        return RiskResult(
            passed=True,
            gate=POSITION_CONCENTRATION_GATE,
            reason="No concentration violations",
        )

    async def _check_correlation(
        self, action: Action, context: StrategyContext
    ) -> RiskResult:
        """Gate: reject if correlated-symbol exposure exceeds threshold.

        Simple implementation: groups positions by quote asset (last 4
        characters of the symbol, e.g. ``USDT``) and flags if any group's
        total notional exceeds *correlation_threshold_pct* of the portfolio.
        """
        threshold_pct = Decimal(str(self._cfg["correlation_threshold_pct"]))
        portfolio_value = (
            context.account.total_usdt if context.account else Decimal("0")
        )
        if portfolio_value <= Decimal("0"):
            return RiskResult(
                passed=True,
                gate=CORRELATION_GATE,
                reason="No portfolio value to evaluate",
            )

        # Group existing positions by quote asset
        notional_by_quote: dict[str, Decimal] = {}
        for pos in context.futures_positions or []:
            sym = pos.symbol
            quote = sym[-4:] if len(sym) >= 4 else sym  # e.g. "USDT"
            size = Decimal(str(abs(pos.size)))
            mark_price = Decimal(str(pos.mark_price))
            notional = size * mark_price
            notional_by_quote[quote] = (
                notional_by_quote.get(quote, Decimal("0")) + notional
            )

        # Add proposed position for entry actions
        if _is_entry(action.type) and action.quantity > Decimal("0") and action.symbol:
            quote = action.symbol[-4:] if len(action.symbol) >= 4 else action.symbol
            notional_by_quote[quote] = (
                notional_by_quote.get(quote, Decimal("0"))
                + abs(action.quantity)
            )

        threshold_value = threshold_pct / Decimal("100") * portfolio_value
        violations: list[dict[str, Any]] = []
        for quote, notional in notional_by_quote.items():
            if notional > threshold_value:
                pct = (notional / portfolio_value) * Decimal("100")
                violations.append({
                    "quote_asset": quote,
                    "total_notional": str(notional.quantize(Decimal("0.01"))),
                    "portfolio_pct": str(pct.quantize(Decimal("0.01"))),
                })

        if violations:
            return RiskResult(
                passed=False,
                gate=CORRELATION_GATE,
                reason=f"Correlated exposure violation(s): {violations}",
                details={"violations": violations},
            )
        return RiskResult(
            passed=True,
            gate=CORRELATION_GATE,
            reason="No correlation violations",
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _gate_sequence(self):
        """Yield enabled gate-check coroutine wrappers in order."""
        gates = [
            (MAX_POSITIONS_GATE, self._check_max_positions),
            (PORTFOLIO_RISK_GATE, self._check_portfolio_risk),
            (DAILY_LOSS_GATE, self._check_daily_loss),
            (DRAWDOWN_GATE, self._check_drawdown),
            (LIQUIDATION_RISK_GATE, self._check_liquidation_risk),
            (FUNDING_RATE_COST_GATE, self._check_funding_rate_cost),
            (LEVERAGE_LIMIT_GATE, self._check_leverage_limit),
            (POSITION_CONCENTRATION_GATE, self._check_position_concentration),
            (CORRELATION_GATE, self._check_correlation),
        ]
        for name, coro in gates:
            if self._enabled.get(name, True):
                yield coro
