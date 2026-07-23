"""Quad orchestrator — top-level application coordinator.

Re-exports the main ``QuadOrchestrator`` class that wires all
subsystems together and manages the trading lifecycle.
"""

from __future__ import annotations

from .orchestrator import QuadOrchestrator

__all__ = ["QuadOrchestrator"]
