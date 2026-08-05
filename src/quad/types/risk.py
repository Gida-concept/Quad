"""Risk management types for Quad options trading bot.

This module defines types used by the risk management system including
status tracking, circuit breakers, risk evaluation results, and
trading actions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from quad.types.domain import FuturesPositionSide, MarginType

__all__ = [
    "Action",
    "ActionType",
    "CircuitBreakerStatus",
    "FuturesRiskMetadata",
    "RiskResult",
    "RiskStatus",
]

# Canonical action type constants (single source of truth)
ActionType = Literal[
    "ENTER", "EXIT", "HOLD",
    "adjust_stop", "reduce_position",
    "set_stop_loss", "set_take_profit",
    "open_long", "open_short", "close_long", "close_short",
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

    drawdown_percent: Decimal = Decimal(0)
    """Current drawdown from peak as a decimal (e.g. 0.05 for 5%)."""

    daily_pnl: Decimal = Decimal(0)
    """Realized PnL for the current trading day."""

    daily_loss_limit: Decimal = Decimal(0)
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
class FuturesRiskMetadata:
    symbol: str = ""
    position_side: FuturesPositionSide = FuturesPositionSide.LONG
    entry_price: float = 0.0
    mark_price: float = 0.0
    liquidation_price: float = 0.0
    leverage: int = 1
    margin_type: MarginType = MarginType.ISOLATED
    position_size_usd: float = 0.0
    distance_to_liquidation_pct: float = 0.0
    funding_rate: float = 0.0


@dataclass
class Action:
    """A trading action produced by a strategy after analysis.

    Represents a decision to enter, exit, adjust, or hold a position.
    """

    type: ActionType = "HOLD"
    strategy: str = ""
    symbol: str = ""
    quantity: Decimal = Decimal(0)
    price: Decimal | None = None
    reason: str = ""
    confidence: float = 1.0
    risk_checked: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    contract: str = ""
    side: str = ""
    order_type: str = ""
    stop_loss_price: Decimal | None = None
    take_profit_price: Decimal | None = None

    def __post_init__(self) -> None:
        """Synchronise derived fields after initialisation.

        Maps action types to default sides and order types.  Only sets
        ``side`` when it was not explicitly provided by the caller, so
        AI-generated actions that include a ``side`` field are preserved.
        """
        self.contract = self.contract or self.symbol
        # Set side only if not already provided
        if not self.side:
            if self.type in ("open_long", "close_short", "ENTER"):
                if self.type == "ENTER":
                    self.side = "BUY"  # default: open long
                elif self.type == "open_long" or self.type == "close_short":
                    self.side = "BUY"
            elif self.type in ("open_short", "close_long", "EXIT"):
                if self.type == "EXIT":
                    self.side = "SELL"  # default: close long
                elif self.type == "open_short" or self.type == "close_long":
                    self.side = "SELL"
            elif self.type in ("set_stop_loss", "set_take_profit"):
                self.side = "SELL"
        if self.type == "set_stop_loss":
            # Market-on-trigger stop: Binance requires only a stopPrice (the
            # legacy STOP_LOSS type is limit-if-triggered and would be
            # rejected without a `price`).
            self.order_type = "STOP_MARKET"
        elif self.type == "set_take_profit":
            self.order_type = "TAKE_PROFIT_MARKET"
