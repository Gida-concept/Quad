"""Circuit breaker manager for the Quad options trading bot.

Provides four circuit breaker tiers that monitor real-time conditions
and prevent trading when risk thresholds are breached.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from datetime import datetime, timezone
from typing import Any

import structlog

from quad.types.risk import CircuitBreakerStatus
from quad.types.strategy import StrategyContext


# ---------------------------------------------------------------------------
# Internal breaker state
# ---------------------------------------------------------------------------


@dataclass
class _CircuitBreaker:
    """Internal mutable state for a single circuit breaker instance."""

    name: str
    tier: int
    active: bool = False
    triggered_at: float | None = None  # unix timestamp
    reason: str = ""
    auto_reset: bool = True

    # Tier-specific configuration
    threshold: Decimal = Decimal("0")
    hysteresis: Decimal = Decimal("0")
    max_consecutive: int = 0

    # Runtime tracking
    consecutive_losses: int = 0
    last_utc_day: int = 0
    peak_value: Decimal = Decimal("0")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DAILY_LOSS_BREAKER = "DAILY_LOSS_BREAKER"
DRAWDOWN_BREAKER = "DRAWDOWN_BREAKER"
CONSECUTIVE_LOSS_BREAKER = "CONSECUTIVE_LOSS_BREAKER"
KILL_SWITCH = "KILL_SWITCH"

ALL_BREAKERS = [
    DAILY_LOSS_BREAKER,
    DRAWDOWN_BREAKER,
    CONSECUTIVE_LOSS_BREAKER,
    KILL_SWITCH,
]

# Default configurations
_DEFAULTS: dict[str, Any] = {
    "daily_loss": {"max_loss_usd": Decimal("500")},
    "drawdown": {"max_drawdown_pct": Decimal("25")},
    "consecutive_losses": {"max_consecutive": 5},
}


class CircuitBreakerManager:
    """Manages four circuit breaker tiers.

    Each breaker monitors specific conditions. When triggered, it
    activates and prevents new trades. Auto-resettable breakers clear
    when the underlying condition recovers.

    Parameters
    ----------
    config:
        Configuration dictionary. The risk sub-section is extracted via
        ``config.get('risk', config)``.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self._log = structlog.get_logger(__name__)
        self._lock = asyncio.Lock()

        raw = config.get("risk", config)
        self._cfg: dict[str, Any] = raw if isinstance(raw, dict) else config

        # Extract circuit_breakers sub-section
        self._cb_cfg: dict[str, Any] = self._cfg.get("circuit_breakers", {})

        # Initialise breakers
        self._breakers: dict[str, _CircuitBreaker] = self._init_breakers()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def check_all(
        self, context: StrategyContext
    ) -> dict[str, CircuitBreakerStatus]:
        """Evaluate all circuit breakers against the current context.

        Returns
        -------
        dict[str, CircuitBreakerStatus]
            Mapping of breaker name to status.
        """
        async with self._lock:
            daily_pnl = (
                context.risk_status.daily_pnl
                if context.risk_status
                else Decimal("0")
            )
            drawdown_pct = (
                context.risk_status.drawdown_percent
                if context.risk_status
                else Decimal("0")
            )

            # Track peak value for drawdown calculation
            portfolio_value = (
                context.account.total_usdt if context.account else Decimal("0")
            )

            self._check_daily_loss(daily_pnl)
            self._check_drawdown(drawdown_pct, portfolio_value)
            self._check_kill_switch()

            return {
                name: self._breaker_status_dict(b)
                for name, b in self._breakers.items()
            }

    async def update_monitoring_data(self, context: StrategyContext) -> None:
        """Feed real-time data to circuit breakers for evaluation.

        Called each cycle to update peak tracking and consecutive loss
        streaks.
        """
        async with self._lock:
            portfolio_value = (
                context.account.total_usdt if context.account else Decimal("0")
            )
            daily_pnl = (
                context.risk_status.daily_pnl
                if context.risk_status
                else Decimal("0")
            )
            drawdown_pct = (
                context.risk_status.drawdown_percent
                if context.risk_status
                else Decimal("0")
            )

            # Update peak
            dd_breaker = self._breakers[DRAWDOWN_BREAKER]
            if portfolio_value > dd_breaker.peak_value:
                dd_breaker.peak_value = portfolio_value

            # Track consecutive losses from trades in context
            self._update_consecutive_losses(context)

            # Evaluate conditions silently (state changes happen here)
            self._check_daily_loss(daily_pnl)
            self._check_drawdown(drawdown_pct, portfolio_value)
            self._check_consecutive_losses()

    def trigger(self, breaker_name: str, reason: str) -> None:
        """Force-trigger a specific circuit breaker.

        Parameters
        ----------
        breaker_name:
            One of the ``ALL_BREAKERS`` constants.
        reason:
            Human-readable reason for triggering.
        """
        if breaker_name not in self._breakers:
            self._log.warning("unknown_breaker", breaker=breaker_name)
            return

        breaker = self._breakers[breaker_name]
        breaker.active = True
        breaker.triggered_at = time.time()
        breaker.reason = reason
        self._log.warning(
            "breaker_triggered",
            name=breaker_name,
            reason=reason,
            tier=breaker.tier,
        )

    async def reset(self, breaker_name: str) -> bool:
        """Attempt to auto-reset a circuit breaker.

        Returns
        -------
        bool
            True if the breaker was successfully reset.
        """
        async with self._lock:
            if breaker_name not in self._breakers:
                return False

            breaker = self._breakers[breaker_name]

            if not breaker.auto_reset and breaker_name != KILL_SWITCH:
                # Non-auto-reset breakers cannot be automatically reset
                return False

            if breaker_name == KILL_SWITCH:
                return False  # kill switch requires explicit token

            if breaker.active:
                breaker.active = False
                breaker.reason = ""
                self._log.info("breaker_reset", name=breaker_name)
                return True
            return True

    def reset_kill_switch(self, reset_token: str) -> bool:
        """Explicitly reset the kill switch.

        Requires a token in the format ``"KILL_RESET_" + <uuid hex>``.

        Returns
        -------
        bool
            True if the kill switch was successfully reset.
        """
        expected_prefix = "KILL_RESET_"
        if not reset_token.startswith(expected_prefix):
            self._log.warning("kill_switch_reset_invalid_token")
            return False

        hex_part = reset_token[len(expected_prefix) :]
        try:
            uuid.UUID(hex=hex_part)
        except (ValueError, AttributeError):
            self._log.warning("kill_switch_reset_invalid_uuid")
            return False

        breaker = self._breakers[KILL_SWITCH]
        if breaker.active:
            breaker.active = False
            breaker.triggered_at = None
            breaker.reason = ""
            self._log.info("kill_switch_reset")
            return True
        return False

    def status(self) -> dict[str, CircuitBreakerStatus]:
        """Return the status of all circuit breakers."""
        return {
            name: self._breaker_status_dict(b)
            for name, b in self._breakers.items()
        }

    def is_trading_allowed(self) -> bool:
        """Return True if NO active breaker blocks trading."""
        return not any(b.active for b in self._breakers.values())

    # ------------------------------------------------------------------
    # Internal: breaker checks
    # ------------------------------------------------------------------

    def _check_daily_loss(self, daily_pnl: Decimal) -> None:
        """Tier 1: Trigger if daily PnL exceeds max loss. Auto-reset at UTC midnight."""
        breaker = self._breakers[DAILY_LOSS_BREAKER]
        max_loss = self._cb_cfg.get("daily_loss", {}).get(
            "max_loss_usd", _DEFAULTS["daily_loss"]["max_loss_usd"]
        )
        max_loss_dec = Decimal(str(max_loss))

        now_utc_day = datetime.now(timezone.utc).timetuple().tm_yday

        if breaker.active:
            # Auto-reset check: new UTC day
            if now_utc_day != breaker.last_utc_day:
                breaker.active = False
                breaker.triggered_at = None
                breaker.reason = ""
                self._log.info("daily_loss_breaker_auto_reset")
            return

        # Trigger check
        if daily_pnl < -max_loss_dec:
            breaker.active = True
            breaker.triggered_at = time.time()
            breaker.reason = (
                f"Daily PnL {daily_pnl:.2f} exceeds loss limit {-max_loss_dec:.2f}"
            )
            breaker.last_utc_day = now_utc_day
            self._log.warning(
                "daily_loss_triggered",
                daily_pnl=str(daily_pnl),
                max_loss=str(max_loss_dec),
            )

    def _check_drawdown(
        self, drawdown_pct: Decimal, portfolio_value: Decimal
    ) -> None:
        """Tier 2: Trigger if drawdown exceeds max. Auto-reset with hysteresis."""
        breaker = self._breakers[DRAWDOWN_BREAKER]
        max_dd = self._cb_cfg.get("drawdown", {}).get(
            "max_drawdown_pct", _DEFAULTS["drawdown"]["max_drawdown_pct"]
        )
        max_dd_dec = Decimal(str(max_dd))

        # Update peak
        if portfolio_value > breaker.peak_value:
            breaker.peak_value = portfolio_value

        if breaker.active:
            # Auto-reset: drawdown recovered to (max_dd - hysteresis)
            recovery_threshold = max_dd_dec - breaker.hysteresis
            if drawdown_pct <= recovery_threshold:
                breaker.active = False
                breaker.triggered_at = None
                breaker.reason = ""
                self._log.info("drawdown_breaker_auto_reset")
            return

        # Trigger check
        if drawdown_pct > max_dd_dec:
            breaker.active = True
            breaker.triggered_at = time.time()
            breaker.reason = (
                f"Drawdown {drawdown_pct:.2f}% exceeds limit {max_dd_dec:.2f}%"
            )
            self._log.warning(
                "drawdown_triggered",
                drawdown=str(drawdown_pct),
                max_drawdown=str(max_dd_dec),
            )

    def _check_consecutive_losses(self) -> None:
        """Tier 3: Trigger after N consecutive losing trades. Auto-reset on win."""
        breaker = self._breakers[CONSECUTIVE_LOSS_BREAKER]
        max_consecutive = self._cb_cfg.get("consecutive_losses", {}).get(
            "max_consecutive",
            _DEFAULTS["consecutive_losses"]["max_consecutive"],
        )

        if breaker.active:
            # Auto-reset: check if streak has been broken
            if breaker.consecutive_losses < max_consecutive:
                breaker.active = False
                breaker.triggered_at = None
                breaker.reason = ""
                self._log.info("consecutive_loss_breaker_auto_reset")
            return

        # Trigger check
        if breaker.consecutive_losses >= max_consecutive:
            breaker.active = True
            breaker.triggered_at = time.time()
            breaker.reason = (
                f"{breaker.consecutive_losses} consecutive losses "
                f"(limit {max_consecutive})"
            )
            self._log.warning(
                "consecutive_loss_triggered",
                count=breaker.consecutive_losses,
                limit=max_consecutive,
            )

    def _check_kill_switch(self) -> None:
        """Tier 4: Only manual reset. No auto-reset logic needed here."""
        # Kill switch only activates via trigger() or trigger_kill_switch()
        pass

    def _update_consecutive_losses(self, context: StrategyContext) -> None:
        """Track consecutive losses from realized PnL in context."""
        breaker = self._breakers[CONSECUTIVE_LOSS_BREAKER]

        # Check recent trades for loss/win
        daily_pnl = (
            context.risk_status.daily_pnl
            if context.risk_status
            else Decimal("0")
        )

        # If daily PnL is negative for a new trade, increment streak
        # This is a simplified approach — in production, track per-trade PnL
        if daily_pnl < Decimal("0"):
            if breaker.consecutive_losses < 100:  # prevent overflow
                breaker.consecutive_losses += 1
        else:
            # Winning trade resets the streak
            if breaker.consecutive_losses > 0:
                breaker.consecutive_losses = 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _init_breakers(self) -> dict[str, _CircuitBreaker]:
        """Create initial circuit breaker instances from config."""
        cb_cfg = self._cb_cfg

        daily_loss_cfg = cb_cfg.get("daily_loss", {})
        drawdown_cfg = cb_cfg.get("drawdown", {})
        consec_cfg = cb_cfg.get("consecutive_losses", {})

        return {
            DAILY_LOSS_BREAKER: _CircuitBreaker(
                name=DAILY_LOSS_BREAKER,
                tier=1,
                auto_reset=True,
                threshold=Decimal(
                    str(
                        daily_loss_cfg.get(
                            "max_loss_usd",
                            _DEFAULTS["daily_loss"]["max_loss_usd"],
                        )
                    )
                ),
                hysteresis=Decimal("0"),
            ),
            DRAWDOWN_BREAKER: _CircuitBreaker(
                name=DRAWDOWN_BREAKER,
                tier=2,
                auto_reset=True,
                threshold=Decimal(
                    str(
                        drawdown_cfg.get(
                            "max_drawdown_pct",
                            _DEFAULTS["drawdown"]["max_drawdown_pct"],
                        )
                    )
                ),
                hysteresis=Decimal("5"),  # 5% hysteresis for recovery
            ),
            CONSECUTIVE_LOSS_BREAKER: _CircuitBreaker(
                name=CONSECUTIVE_LOSS_BREAKER,
                tier=3,
                auto_reset=True,
                max_consecutive=consec_cfg.get(
                    "max_consecutive",
                    _DEFAULTS["consecutive_losses"]["max_consecutive"],
                ),
            ),
            KILL_SWITCH: _CircuitBreaker(
                name=KILL_SWITCH,
                tier=4,
                auto_reset=False,
            ),
        }

    def _breaker_status_dict(
        self, breaker: _CircuitBreaker
    ) -> CircuitBreakerStatus:
        """Convert internal breaker state to a public status dataclass."""
        return CircuitBreakerStatus(
            name=breaker.name,
            active=breaker.active,
            triggered_at=int(breaker.triggered_at * 1000)
            if breaker.triggered_at is not None
            else None,
            reason=breaker.reason,
            tier=breaker.tier,
        )
