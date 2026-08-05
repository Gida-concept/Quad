"""Risk management system for Quad futures trading bot.

Provides pre-trade gates, circuit breakers, position sizing (Fractional Kelly),
position tracking, and a top-level RiskManager coordinating all subsystems.
"""

from __future__ import annotations

from .circuit_breakers import CircuitBreakerManager
from .exposure import FuturesPositionTracker
from .gates import GatePipeline
from .manager import RiskManager
from .sizing import PositionSizer

__all__ = [
    "CircuitBreakerManager",
    "FuturesPositionTracker",
    "GatePipeline",
    "PositionSizer",
    "RiskManager",
]
