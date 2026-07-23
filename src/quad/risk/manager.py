"""Top-level risk manager coordinating all risk subsystems.

Provides the RiskManager class as the single entry point for the
execution engine and orchestrator. Combines GatePipeline,
CircuitBreakerManager, PositionSizer, and ExposureLimiter.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import structlog

from quad.types.risk import Action, RiskResult, RiskStatus
from quad.types.strategy import StrategyContext
from quad.persistence.database import DatabaseManager

from .gates import GatePipeline
from .circuit_breakers import CircuitBreakerManager
from .sizing import PositionSizer
from .exposure import ExposureLimiter

DEFAULT_RISK_CONFIG: dict[str, Any] = {
    "max_positions": 5,
    "max_portfolio_risk_pct": 20,
    "max_daily_loss_usd": 500,
    "max_concentration_pct": 15,
    "max_drawdown_pct": 25,
    "min_dte": 1,
    "max_dte": 365,
    "circuit_breakers": {
        "daily_loss": {"max_loss_usd": 500},
        "drawdown": {"max_drawdown_pct": 25},
        "consecutive_losses": {"max_consecutive": 5},
    },
    "exposure": {
        "max_delta": 100,
        "max_theta": -500,
        "max_vega": 500,
    },
    "kelly": {"fraction": 0.25, "default_fraction": 0.02},
    "max_position_size_pct": 0.10,
    "max_position_size_usd": 10000,
}


class RiskManager:
    """Top-level risk manager coordinating all risk subsystems.

    Single entry point for the execution engine and orchestrator.
    Combines: GatePipeline, CircuitBreakerManager, PositionSizer,
    ExposureLimiter.

    Parameters
    ----------
    config:
        Full configuration dictionary. The risk sub-section is extracted
        automatically.
    db_manager:
        Optional database manager passed through to PositionSizer.
    """

    def __init__(
        self,
        config: dict[str, Any],
        db_manager: DatabaseManager | None = None,
    ) -> None:
        self._log = structlog.get_logger(__name__)

        # Ensure risk section exists with defaults
        self._config = self._ensure_config(config)

        self._gates = GatePipeline(self._config)
        self._breakers = CircuitBreakerManager(self._config)
        self._sizer = PositionSizer(self._config, db_manager=db_manager)
        self._limiter = ExposureLimiter(self._config)

        self._log.info(
            "risk_manager_initialized",
            gates=len(self._gates.get_gate_status()),
            breakers=len(self._breakers.status()),
        )

    # ------------------------------------------------------------------
    # Core evaluation
    # ------------------------------------------------------------------

    async def evaluate(
        self, action: Action, context: StrategyContext
    ) -> RiskResult:
        """Evaluate a proposed action through all risk subsystems.

        Full evaluation flow:

        1. Check circuit breakers — if any active, reject with CB reason.
        2. Run gate pipeline — if any gate fails, reject with gate reason.
        3. Compute optimal position size via PositionSizer.
        4. Return a passing RiskResult with the sized Action.

        Parameters
        ----------
        action:
            The proposed trading action.
        context:
            Current strategy execution context.

        Returns
        -------
        RiskResult
            ``passed=True`` with the sized action in ``details["action"]``,
            or ``passed=False`` with the failing subsystem's reason.
        """
        # Phase 1: Circuit breaker check
        if not self._breakers.is_trading_allowed():
            cb_status = self._breakers.status()
            active_breakers = [
                name
                for name, s in cb_status.items()
                if s.active
            ]
            reasons = {
                name: s.reason
                for name, s in cb_status.items()
                if s.active
            }
            self._log.warning(
                "trade_rejected_circuit_breakers",
                active_breakers=active_breakers,
                reasons=reasons,
            )
            return RiskResult(
                passed=False,
                gate="CIRCUIT_BREAKER",
                reason=f"Active breakers: {active_breakers}. Reasons: {reasons}",
                details={"active_breakers": active_breakers, "reasons": reasons},
            )

        # Phase 2: Gate pipeline check
        gate_result = await self._gates.evaluate(action, context)
        if not gate_result.passed:
            return gate_result

        # Phase 3: Position sizing
        try:
            sized_action = await self._sizer.compute_size(action, context)
        except Exception as exc:
            self._log.exception("position_sizing_failed", error=str(exc))
            return RiskResult(
                passed=False,
                gate="POSITION_SIZING",
                reason=f"Position sizing error: {exc}",
                details={"error": str(exc)},
            )

        # Phase 4: All checks passed
        return RiskResult(
            passed=True,
            gate="ALL",
            reason="All risk checks passed",
            details={
                "action": sized_action,
                "original_action": action,
                "sized_quantity": str(sized_action.quantity),
            },
        )

    # ------------------------------------------------------------------
    # Monitoring / data feed
    # ------------------------------------------------------------------

    async def update_monitoring(self, context: StrategyContext) -> None:
        """Feed current state to circuit breakers and exposure limiter.

        Called each trading cycle before evaluation.
        """
        await self._breakers.update_monitoring_data(context)

        if context.positions and context.option_chain:
            try:
                exposure = await self._limiter.compute_exposure(
                    context.positions,
                    context.option_chain,
                )
                self._log.debug("exposure_updated", exposure={
                    k: str(v) for k, v in exposure.items()
                })
            except Exception as exc:
                self._log.exception("exposure_update_failed", error=str(exc))

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    async def get_status(self) -> RiskStatus:
        """Build a RiskStatus snapshot from all subsystems."""
        cb_status = self._breakers.status()
        gate_status = self._gates.get_gate_status()

        # Determine drawdown and PnL from breakers
        dd_breaker = cb_status.get("DRAWDOWN_BREAKER", type("obj", (object,), {"active": False}))()
        dl_breaker = cb_status.get("DAILY_LOSS_BREAKER", type("obj", (object,), {"active": False}))()

        # Note: actual drawdown and daily PnL values rely on StrategyContext.
        # Here we report from circuit breaker status with defaults.
        drawdown = Decimal("0")
        daily_pnl = Decimal("0")
        daily_loss_limit = Decimal("500")

        return RiskStatus(
            drawdown_percent=drawdown,
            daily_pnl=daily_pnl,
            daily_loss_limit=daily_loss_limit,
            circuit_breakers={
                name: s for name, s in cb_status.items()
            },
            gates=gate_status,
        )

    def is_trading_allowed(self) -> bool:
        """Quick check: returns True if no circuit breaker is active."""
        return self._breakers.is_trading_allowed()

    # ------------------------------------------------------------------
    # Kill switch
    # ------------------------------------------------------------------

    def trigger_kill_switch(self, reason: str) -> None:
        """Emergency stop — triggers the Tier 4 kill switch."""
        self._log.critical("kill_switch_triggered", reason=reason)
        self._breakers.trigger("KILL_SWITCH", reason)

    def reset_kill_switch(self, token: str) -> bool:
        """Reset the kill switch with a valid reset token.

        Returns
        -------
        bool
            True if the kill switch was successfully reset.
        """
        return self._breakers.reset_kill_switch(token)

    # ------------------------------------------------------------------
    # Subsystem access
    # ------------------------------------------------------------------

    def get_gate_status(self) -> dict[str, bool]:
        """Return gate enable/disable status from the gate pipeline."""
        return self._gates.get_gate_status()

    def get_exposure_report(self) -> dict[str, Any]:
        """Return the full exposure report from the exposure limiter."""
        return self._limiter.get_exposure_report()

    def get_sizing_stats(self) -> dict[str, Any]:
        """Return current position sizing parameters."""
        return self._sizer.get_sizing_stats()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _ensure_config(config: dict[str, Any]) -> dict[str, Any]:
        """Ensure the config dict has a populated ``risk`` section."""
        if "risk" not in config or not isinstance(config["risk"], dict):
            config = dict(config)
            config["risk"] = dict(DEFAULT_RISK_CONFIG)
        else:
            # Merge in any missing defaults
            risk = dict(DEFAULT_RISK_CONFIG)
            risk.update(config["risk"])
            config = dict(config)
            config["risk"] = risk
        return config
