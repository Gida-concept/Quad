"""Quad backtesting package.

Provides a historical simulation engine for testing strategies against
past market data.  The engine steps through time, evaluates strategies
at each interval, and tracks simulated trades and performance metrics.
"""

from __future__ import annotations

from .engine import BacktestEngine
from .models import BacktestResult, EquityPoint

__all__ = [
    "BacktestEngine",
    "BacktestResult",
    "EquityPoint",
]
