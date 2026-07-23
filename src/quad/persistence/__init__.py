"""Persistence layer for the Quad options trading bot.

This module provides the database manager, repository classes, and model
definitions for PostgreSQL-based persistence (via asyncpg).
"""

from .database import DatabaseManager
from .repositories import (
    AccountRepository,
    DecisionRepository,
    OptionsContractRepository,
    OrderRepository,
    PerformanceSnapshotRepository,
    PositionRepository,
    SessionRepository,
    TradeRepository,
)

__all__ = [
    "DatabaseManager",
    "AccountRepository",
    "DecisionRepository",
    "OptionsContractRepository",
    "OrderRepository",
    "PerformanceSnapshotRepository",
    "PositionRepository",
    "SessionRepository",
    "TradeRepository",
]
