"""Pre-trade gate pipeline for risk checking.

Provides the GatePipeline class with six sequential gates that every
proposed Action must pass before execution. Short-circuits on first
failure or runs all gates for diagnostic purposes.
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
CONCENTRATION_GATE = "CONCENTRATION_GATE"
DRAWDOWN_GATE = "DRAWDOWN_GATE"
EXPIRY_GATE = "EXPIRY_GATE"

ALL_GATES = [
    MAX_POSITIONS_GATE,
    PORTFOLIO_RISK_GATE,
    DAILY_LOSS_GATE,
    CONCENTRATION_GATE,
    DRAWDOWN_GATE,
    EXPIRY_GATE,
]

# Default gate configuration fallbacks
_DEFAULTS: dict[str, Any] = {
    "max_positions": 5,
    "max_portfolio_risk_pct": Decimal("20"),
    "max_daily_loss_usd": Decimal("500"),
    "max_concentration_pct": Decimal("15"),
    "max_drawdown_pct": Decimal("25"),
    "min_dte": 1,
    "max_dte": 365,
}


class GatePipeline:
    """Six-gate pre-trade risk check pipeline.

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

        # Allow callers to pass either the full config or just the risk section
        raw = config.get("risk", config)
        self._cfg: dict[str, Any] = raw if isinstance(raw, dict) else config

        # Gate enable/disable flags — all enabled by default
        self._enabled: dict[str, bool] = {g: True for g in ALL_GATES}

    # ------------------------------------------------------------------
    # Public evaluation API
    # ------------------------------------------------------------------

    async def evaluate(
        self, action: Action, context: StrategyContext
    ) -> RiskResult:
        """Run through all 6 gates. Short-circuits on first failure.

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
                    contract=action.contract,
                )
                return result
        return RiskResult(
            passed=True,
            gate="ALL",
            reason="All gates passed",
            details={"gates_checked": [g for g in ALL_GATES if self._enabled.get(g, False)]},
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
        limit = int(self._cfg.get("max_positions", _DEFAULTS["max_positions"]))
        open_count = len(
            [p for p in (context.positions or []) if p.status == "OPEN"]
        )

        # Count the proposed ENTER as an additional position
        would_add = 1 if action.type == "ENTER" else 0
        total = open_count + would_add

        if total >= limit:
            return RiskResult(
                passed=False,
                gate=MAX_POSITIONS_GATE,
                reason=f"Position limit {limit} reached ({open_count} open, {would_add} adding)",
                details={"open_positions": open_count, "limit": limit, "would_add": bool(would_add)},
            )
        return RiskResult(
            passed=True,
            gate=MAX_POSITIONS_GATE,
            reason=f"{total} < {limit} positions",
        )

    async def _check_portfolio_risk(
        self, action: Action, context: StrategyContext
    ) -> RiskResult:
        max_risk_pct = Decimal(
            str(self._cfg.get("max_portfolio_risk_pct", _DEFAULTS["max_portfolio_risk_pct"]))
        )
        portfolio_value = (
            context.account.total_usdt if context.account else Decimal("0")
        )
        if portfolio_value <= Decimal("0"):
            return RiskResult(
                passed=True,
                gate=PORTFOLIO_RISK_GATE,
                reason="No portfolio value to evaluate",
            )

        # Sum absolute deltas from existing positions
        total_abs_delta = Decimal("0")
        for pos in context.positions or []:
            delta = Decimal(str(action.metadata.get("delta", "0")))
            total_abs_delta += abs(delta) * pos.quantity

        # Include proposed action delta if this is an ENTER
        if action.type == "ENTER":
            action_delta = Decimal(str(action.metadata.get("delta", "0")))
            total_abs_delta += abs(action_delta) * action.quantity

        risk_pct = (total_abs_delta / portfolio_value) * Decimal("100")

        if risk_pct > max_risk_pct:
            return RiskResult(
                passed=False,
                gate=PORTFOLIO_RISK_GATE,
                reason=(
                    f"Portfolio delta risk {risk_pct:.2f}% exceeds "
                    f"limit of {max_risk_pct:.2f}%"
                ),
                details={
                    "risk_pct": str(risk_pct),
                    "limit_pct": str(max_risk_pct),
                    "total_abs_delta": str(total_abs_delta),
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
        max_loss = Decimal(
            str(self._cfg.get("max_daily_loss_usd", _DEFAULTS["max_daily_loss_usd"]))
        )
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

    async def _check_concentration(
        self, action: Action, context: StrategyContext
    ) -> RiskResult:
        max_conc_pct = Decimal(
            str(self._cfg.get("max_concentration_pct", _DEFAULTS["max_concentration_pct"]))
        )
        portfolio_value = (
            context.account.total_usdt if context.account else Decimal("0")
        )
        if portfolio_value <= Decimal("0"):
            return RiskResult(
                passed=True,
                gate=CONCENTRATION_GATE,
                reason="No portfolio value to evaluate",
            )

        # Determine the underlying for the proposed action
        target_underlying = action.metadata.get("underlying", action.contract or "")

        # Sum position values for the same underlying
        total_underlying_value = Decimal("0")
        for pos in context.positions or []:
            # Match by underlying if available in metadata, else by contract
            pos_underlying = target_underlying  # simplified check
            if pos.contract_symbol == target_underlying:
                total_underlying_value += abs(pos.current_price) * abs(pos.quantity)

        # Include proposed action value
        if action.type == "ENTER":
            action_price = action.price or Decimal("0")
            total_underlying_value += action_price * action.quantity

        conc_pct = (total_underlying_value / portfolio_value) * Decimal("100")

        if conc_pct > max_conc_pct:
            return RiskResult(
                passed=False,
                gate=CONCENTRATION_GATE,
                reason=(
                    f"Concentration {conc_pct:.2f}% for {target_underlying} "
                    f"exceeds limit of {max_conc_pct:.2f}%"
                ),
                details={
                    "underlying": target_underlying,
                    "concentration_pct": str(conc_pct),
                    "limit_pct": str(max_conc_pct),
                    "underlying_value": str(total_underlying_value),
                    "portfolio_value": str(portfolio_value),
                },
            )
        return RiskResult(
            passed=True,
            gate=CONCENTRATION_GATE,
            reason=f"Concentration {conc_pct:.2f}% within limit",
        )

    async def _check_drawdown(
        self, action: Action, context: StrategyContext
    ) -> RiskResult:
        max_dd_pct = Decimal(
            str(self._cfg.get("max_drawdown_pct", _DEFAULTS["max_drawdown_pct"]))
        )
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
            reason=f"Drawdown {current_dd:.2f}% within {max_dd_pct:.2f}% limit",
        )

    async def _check_expiry(
        self, action: Action, context: StrategyContext
    ) -> RiskResult:
        min_dte = int(self._cfg.get("min_dte", _DEFAULTS["min_dte"]))
        max_dte = int(self._cfg.get("max_dte", _DEFAULTS["max_dte"]))

        # Determine DTE from metadata or context
        dte = int(action.metadata.get("dte", action.metadata.get("days_to_expiry", 0)))
        if dte <= 0 and context.positions:
            # Attempt to derive from a matching position
            for pos in context.positions:
                if pos.contract_symbol == action.contract:
                    dte = pos.days_to_expiry
                    break

        if dte < min_dte:
            return RiskResult(
                passed=False,
                gate=EXPIRY_GATE,
                reason=f"DTE {dte} is below minimum of {min_dte}",
                details={"dte": dte, "min_dte": min_dte, "max_dte": max_dte},
            )
        if dte > max_dte:
            return RiskResult(
                passed=False,
                gate=EXPIRY_GATE,
                reason=f"DTE {dte} exceeds maximum of {max_dte}",
                details={"dte": dte, "min_dte": min_dte, "max_dte": max_dte},
            )
        return RiskResult(
            passed=True,
            gate=EXPIRY_GATE,
            reason=f"DTE {dte} within [{min_dte}, {max_dte}] range",
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
            (CONCENTRATION_GATE, self._check_concentration),
            (DRAWDOWN_GATE, self._check_drawdown),
            (EXPIRY_GATE, self._check_expiry),
        ]
        for name, coro in gates:
            if self._enabled.get(name, True):
                yield coro
