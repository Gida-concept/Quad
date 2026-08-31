"""Circuit breaker manager for the Quad futures trading bot.

Provides seven circuit breaker tiers that monitor real-time conditions
and prevent trading when risk thresholds are breached. Covers daily loss,
drawdown, consecutive losses, kill switch, liquidation cascade, funding
rate spikes, and volatility.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
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
    threshold: Decimal = Decimal(0)
    hysteresis: Decimal = Decimal(0)
    max_consecutive: int = 0

    # Runtime tracking
    consecutive_losses: int = 0
    last_utc_day: int = 0
    peak_value: Decimal = Decimal(0)

    # Funding rate spike tracking (FUNDING_RATE_SPIKE_BREAKER)
    funding_spike_counts: dict[str, int] = field(default_factory=dict)

    # Near-liquidation position tracking (LIQUIDATION_CASCADE_BREAKER)
    near_liquidation_positions: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DAILY_LOSS_BREAKER = "DAILY_LOSS_BREAKER"
DRAWDOWN_BREAKER = "DRAWDOWN_BREAKER"
CONSECUTIVE_LOSS_BREAKER = "CONSECUTIVE_LOSS_BREAKER"
KILL_SWITCH = "KILL_SWITCH"
LIQUIDATION_CASCADE_BREAKER = "LIQUIDATION_CASCADE_BREAKER"
FUNDING_RATE_SPIKE_BREAKER = "FUNDING_RATE_SPIKE_BREAKER"
VOLATILITY_BREAKER = "VOLATILITY_BREAKER"

ALL_BREAKERS = [
    DAILY_LOSS_BREAKER,
    DRAWDOWN_BREAKER,
    CONSECUTIVE_LOSS_BREAKER,
    KILL_SWITCH,
    LIQUIDATION_CASCADE_BREAKER,
    FUNDING_RATE_SPIKE_BREAKER,
    VOLATILITY_BREAKER,
]


class CircuitBreakerManager:
    """Manages seven circuit breaker tiers.

    Each breaker monitors specific conditions. When triggered, it
    activates and prevents new trades. Auto-resettable breakers clear
    when the underlying condition recovers.

    Tiers:
        1  — Daily Loss, Liquidation Cascade, Funding Rate Spike
        2  — Drawdown, Volatility
        3  — Consecutive Loss
        4  — Kill Switch (manual reset only)

    Parameters
    ----------
    config:
        Configuration dictionary. The risk sub-section is extracted via
        ``config.get('risk', config)``.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self._log = structlog.get_logger(__name__)
        self._lock = asyncio.Lock()

        self._cfg: dict[str, Any] = config["risk"]

        # Extract circuit_breakers sub-section
        self._cb_cfg: dict[str, Any] = self._cfg["circuit_breakers"]

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
                context.risk_status.daily_pnl if context.risk_status else Decimal(0)
            )
            drawdown_pct = (
                context.risk_status.drawdown_percent
                if context.risk_status
                else Decimal(0)
            )

            portfolio_value = (
                context.account.total_usdt if context.account else Decimal(0)
            )

            self._check_daily_loss(daily_pnl)
            self._check_drawdown(drawdown_pct, portfolio_value)
            self._check_kill_switch()
            self._check_liquidation_cascade(context)
            self._check_funding_rate_spike(context)
            self._check_volatility(context)

            return {
                name: self._breaker_status_dict(b) for name, b in self._breakers.items()
            }

    async def update_monitoring_data(self, context: StrategyContext) -> None:
        """Feed real-time data to circuit breakers for evaluation.

        Called each cycle to update peak tracking, consecutive loss
        streaks, funding rate spikes, and liquidation proximity.
        """
        async with self._lock:
            portfolio_value = (
                context.account.total_usdt if context.account else Decimal(0)
            )
            daily_pnl = (
                context.risk_status.daily_pnl if context.risk_status else Decimal(0)
            )
            drawdown_pct = (
                context.risk_status.drawdown_percent
                if context.risk_status
                else Decimal(0)
            )

            # Update peak for drawdown calculation
            dd_breaker = self._breakers[DRAWDOWN_BREAKER]
            dd_breaker.peak_value = max(dd_breaker.peak_value, portfolio_value)

            # Track consecutive losses from trades in context
            self._update_consecutive_losses(context)

            # Evaluate conditions silently (state changes happen here)
            self._check_daily_loss(daily_pnl)
            self._check_drawdown(drawdown_pct, portfolio_value)
            self._check_consecutive_losses()
            self._check_liquidation_cascade(context)
            self._check_funding_rate_spike(context)
            self._check_volatility(context)

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
            name: self._breaker_status_dict(b) for name, b in self._breakers.items()
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
        max_loss = self._cfg["max_daily_loss_usd"]
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

    def _check_drawdown(self, drawdown_pct: Decimal, portfolio_value: Decimal) -> None:
        """Tier 2: Trigger if drawdown exceeds max. Auto-reset with hysteresis."""
        breaker = self._breakers[DRAWDOWN_BREAKER]
        max_dd = self._cfg["max_drawdown_pct"]
        max_dd_dec = Decimal(str(max_dd))

        # Update peak
        breaker.peak_value = max(breaker.peak_value, portfolio_value)

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
        max_consecutive = self._cb_cfg["consecutive_losses"]["max_consecutive"]

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

    def _check_liquidation_cascade(self, context: StrategyContext) -> None:
        """Tier 1: Flag if any position is dangerously close to liquidation.

        Checks futures positions and tracks symbols that cross the
        cascade threshold (default < 5% distance to liquidation).
        If a position that was previously near liquidation is no longer
        present (liquidated), the breaker triggers.
        """
        breaker = self._breakers[LIQUIDATION_CASCADE_BREAKER]
        cascade_cfg = self._cb_cfg["liquidation_cascade"]
        min_distance = float(str(cascade_cfg["min_cascade_distance_pct"]))

        # Collect current symbols that are near liquidation
        current_near: set[str] = set()
        for pos in context.futures_positions or []:
            if pos.liquidation_price <= 0 or pos.mark_price <= 0:
                continue
            if pos.position_side.value == "long":
                distance = (pos.mark_price - pos.liquidation_price) / pos.mark_price
            else:
                distance = (pos.liquidation_price - pos.mark_price) / pos.mark_price
            if distance < min_distance:
                current_near.add(pos.symbol)

        # Check if any previously near-liquidation position is now gone
        # (size == 0 or not in the position list at all)
        previously_near = breaker.near_liquidation_positions
        existing_symbols = {p.symbol for p in (context.futures_positions or [])}
        liquidated = previously_near - current_near - existing_symbols

        if liquidated and not breaker.active:
            breaker.active = True
            breaker.triggered_at = time.time()
            breaker.reason = (
                f"Liquidation cascade detected for symbols: {sorted(liquidated)}"
            )
            self._log.warning(
                "liquidation_cascade_triggered",
                symbols=sorted(liquidated),
            )
        elif breaker.active:
            # Auto-reset: no positions currently near liquidation
            if not current_near:
                breaker.active = False
                breaker.triggered_at = None
                breaker.reason = ""
                self._log.info("liquidation_cascade_breaker_auto_reset")

        # Update tracked set
        breaker.near_liquidation_positions = current_near

    def _check_funding_rate_spike(self, context: StrategyContext) -> None:
        """Tier 1: Trigger if any symbol's funding rate exceeds spike threshold.

        Tracks consecutive spikes per symbol. If 3 or more consecutive
        monitoring cycles show a spike, escalate (trigger the breaker).
        """
        breaker = self._breakers[FUNDING_RATE_SPIKE_BREAKER]
        spike_cfg = self._cb_cfg["funding_rate_spike"]
        spike_threshold = float(str(spike_cfg["funding_rate_spike_threshold"]))

        funding_rates = context.funding_rates or {}
        current_spikes: set[str] = set()

        for sym, fr in funding_rates.items():
            rate = float(str(fr.funding_rate))
            if abs(rate) > spike_threshold:
                current_spikes.add(sym)

        # Update consecutive spike counts
        counts = breaker.funding_spike_counts
        for sym in current_spikes:
            counts[sym] = counts.get(sym, 0) + 1
        # Reset counts for symbols that no longer spike
        for sym in list(counts.keys()):
            if sym not in current_spikes:
                counts[sym] = 0

        # Check for escalation (3+ consecutive spikes)
        max_consecutive_spikes = int(spike_cfg["max_consecutive_spikes"])
        escalating = [
            sym for sym, cnt in counts.items() if cnt >= max_consecutive_spikes
        ]

        if escalating and not breaker.active:
            breaker.active = True
            breaker.triggered_at = time.time()
            breaker.reason = (
                f"Funding rate spike detected for {len(escalating)} "
                f"symbol(s): {sorted(escalating)}"
            )
            self._log.warning(
                "funding_rate_spike_triggered",
                symbols=sorted(escalating),
                counts={s: counts[s] for s in escalating},
            )
        elif breaker.active:
            # Auto-reset: no more escalating spikes
            if not escalating:
                breaker.active = False
                breaker.triggered_at = None
                breaker.reason = ""
                self._log.info("funding_rate_spike_breaker_auto_reset")

    def _check_volatility(self, context: StrategyContext) -> None:
        """Tier 2: Trigger if market volatility exceeds the configured threshold.

        Simple approach using ATR % from futures contract data if available,
        otherwise checks mark price change vs threshold.
        """
        breaker = self._breakers[VOLATILITY_BREAKER]
        vol_cfg = self._cb_cfg["volatility"]
        atr_threshold = float(str(vol_cfg["volatility_breaker_atr_pct"]))

        # Use futures contract data for price change / volatility
        high_volatility_symbols: list[str] = []
        for sym, contract in (context.futures_contracts or {}).items():
            mark_str = str(contract.mark_price)
            change_str = str(contract.price_change_24h)
            mark = float(mark_str) if mark_str != "0" else 0.0
            change = float(change_str) if change_str != "0" else 0.0

            if mark > 0 and abs(change) > 0:
                change_pct = abs(change / mark) * 100
                if change_pct > atr_threshold * 100:
                    high_volatility_symbols.append(sym)

        if high_volatility_symbols and not breaker.active:
            breaker.active = True
            breaker.triggered_at = time.time()
            breaker.reason = (
                f"High volatility detected for symbols: "
                f"{sorted(high_volatility_symbols)} "
                f"(ATR % threshold: {atr_threshold * 100:.1f}%)"
            )
            self._log.warning(
                "volatility_breaker_triggered",
                symbols=sorted(high_volatility_symbols),
                threshold_pct=atr_threshold * 100,
            )
        elif breaker.active:
            # Auto-reset: check if volatility subsided
            if not high_volatility_symbols:
                breaker.active = False
                breaker.triggered_at = None
                breaker.reason = ""
                self._log.info("volatility_breaker_auto_reset")

    def _update_consecutive_losses(self, context: StrategyContext) -> None:
        """Track consecutive losses from realized PnL in context.

        Only increments when daily PnL *decreases* (a new losing trade
        closed), and only resets when daily PnL *increases* (a new winning
        trade closed). This prevents the counter from inflating every cycle
        while the bot sits flat at a negative daily PnL.
        """
        breaker = self._breakers[CONSECUTIVE_LOSS_BREAKER]

        daily_pnl = context.risk_status.daily_pnl if context.risk_status else Decimal(0)

        # Track the previous cycle's daily PnL to detect actual trade events
        prev_pnl = getattr(breaker, "_prev_daily_pnl", None)
        if prev_pnl is None:
            # First cycle: initialise without counting
            breaker._prev_daily_pnl = daily_pnl  # type: ignore[attr-defined]
            return

        if daily_pnl < prev_pnl:
            # Daily PnL decreased → a new losing trade closed
            if breaker.consecutive_losses < 100:  # prevent overflow
                breaker.consecutive_losses += 1
        elif daily_pnl > prev_pnl:
            # Daily PnL increased → a new winning trade closed, reset streak
            breaker.consecutive_losses = 0

        breaker._prev_daily_pnl = daily_pnl  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _init_breakers(self) -> dict[str, _CircuitBreaker]:
        """Create initial circuit breaker instances from config."""
        cb_cfg = self._cb_cfg

        consec_cfg = cb_cfg["consecutive_losses"]
        cascade_cfg = cb_cfg["liquidation_cascade"]
        spike_cfg = cb_cfg["funding_rate_spike"]
        vol_cfg = cb_cfg["volatility"]

        return {
            DAILY_LOSS_BREAKER: _CircuitBreaker(
                name=DAILY_LOSS_BREAKER,
                tier=1,
                auto_reset=True,
                threshold=Decimal(str(self._cfg["max_daily_loss_usd"])),
                hysteresis=Decimal(0),
            ),
            DRAWDOWN_BREAKER: _CircuitBreaker(
                name=DRAWDOWN_BREAKER,
                tier=2,
                auto_reset=True,
                threshold=Decimal(str(self._cfg["max_drawdown_pct"])),
                hysteresis=Decimal(5),  # 5% hysteresis for recovery
            ),
            CONSECUTIVE_LOSS_BREAKER: _CircuitBreaker(
                name=CONSECUTIVE_LOSS_BREAKER,
                tier=3,
                auto_reset=True,
                max_consecutive=consec_cfg["max_consecutive"],
            ),
            KILL_SWITCH: _CircuitBreaker(
                name=KILL_SWITCH,
                tier=4,
                auto_reset=False,
            ),
            LIQUIDATION_CASCADE_BREAKER: _CircuitBreaker(
                name=LIQUIDATION_CASCADE_BREAKER,
                tier=1,
                auto_reset=True,
                threshold=Decimal(str(cascade_cfg["min_cascade_distance_pct"])),
            ),
            FUNDING_RATE_SPIKE_BREAKER: _CircuitBreaker(
                name=FUNDING_RATE_SPIKE_BREAKER,
                tier=1,
                auto_reset=True,
                threshold=Decimal(str(spike_cfg["funding_rate_spike_threshold"])),
            ),
            VOLATILITY_BREAKER: _CircuitBreaker(
                name=VOLATILITY_BREAKER,
                tier=2,
                auto_reset=True,
                threshold=Decimal(str(vol_cfg["volatility_breaker_atr_pct"])),
            ),
        }

    def _breaker_status_dict(self, breaker: _CircuitBreaker) -> CircuitBreakerStatus:
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
