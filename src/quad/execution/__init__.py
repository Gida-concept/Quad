"""Execution engine -- order gateway, TWAP slicing, fill reconciliation.

This module provides the top-level execution orchestrator along with
lower-level components for order management, large-order splitting
(TWAP), and fill reconciliation.
"""

from __future__ import annotations

from .engine import ExecutionEngine
from .gateway import OrderGateway, OrderRejectedError, OrderTimeoutError
from .reconciler import FillReconciler
from .twap import TwapSlicer

__all__ = [
    "ExecutionEngine",
    "OrderGateway",
    "OrderRejectedError",
    "OrderTimeoutError",
    "TwapSlicer",
    "FillReconciler",
]
