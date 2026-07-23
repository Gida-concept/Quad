"""Risk management types for Quad options trading bot.

This module defines types used by the risk management system including
status tracking, circuit breakers, risk evaluation results, and
trading actions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal


__all__ = [
    "RiskStatus",
    "CircuitBreakerStatus",
    "RiskResult",
    "Action",
]


@dataclass
class CircuitBreakerStatus:
    """Represents the state of a single circuit breaker."""

    name: str
    """Circuit breaker name, e.g. 'pnl_drawdown', 'volatility_spike'."""

    active: bool = False
    """Whether the circuit breaker is currently triggered."""

    triggered_at: int | None = None
    """Timestamp when the breaker was triggered, in unix milliseconds."""

    reason: str = ""
    """Human-readable reason for the trigger."""

    tier: int = 0
    """Severity tier: 1 (warning), 2 (escalated), 3 (critical)."""


@dataclass
class RiskStatus:
    """Aggregated risk status snapshot for the trading system."""

    drawdown_percent: Decimal = Decimal("0")
    """Current drawdown from peak as a decimal (e.g. 0.05 for 5%)."""

    daily_pnl: Decimal = Decimal("0")
    """Realized PnL for the current trading day."""

    daily_loss_limit: Decimal = Decimal("0")
    """Maximum allowable daily loss."""

    circuit_breakers: dict[str, CircuitBreakerStatus] = field(default_factory=dict)
    """Mapping of breaker name to status."""

    gates: dict[str, bool] = field(default_factory=dict)
    """Mapping of gate name to pass/fail status."""


@dataclass
class RiskResult:
    """Result of a risk check evaluation."""

    passed: bool = True
    """Whether the check passed."""

    gate: str = ""
    """Name of the gate that was evaluated."""

    reason: str = ""
    """Human-readable reason for the result."""

    details: dict[str, Any] = field(default_factory=dict)
    """Additional details about the evaluation."""


@dataclass
class Action:
    """A trading action produced by a strategy after analysis.

    Represents a decision to enter, exit, adjust, or hold a position.
    """

    type: Literal["ENTER", "EXIT", "ADJUST", "HOLD"] = "HOLD"
    """Type of action to take."""

    strategy: str = ""
    """Name of the strategy producing this action."""

    contract: str | None = None
    """Target contract symbol, or None for HOLD actions."""

    side: str | None = None
    """Order side: BUY or SELL, or None for HOLD."""

    quantity: Decimal = Decimal("0")
    """Number of contracts."""

    order_type: str | None = None
    """Order type: LIMIT, MARKET, etc., or None for HOLD."""

    price: Decimal | None = None
    """Limit price, or None for market orders / HOLD."""

    reason: str = ""
    """Human-readable reason for this action."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Additional metadata about the action (IV, delta, DTE, etc.)."""
