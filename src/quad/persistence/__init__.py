"""Persistence layer for the Quad options trading bot.

This module provides the database manager, repository classes, and model
definitions for PostgreSQL-based persistence (via asyncpg).
"""

from .database import DatabaseManager
from .repositories import (
    AccountRepository,
    ConfigChangeRepository,
    DecisionRepository,
    OptionsContractRepository,
    OptimizationRecommendationRepository,
    OptimizationRunRepository,
    OrderRepository,
    PerformanceSnapshotRepository,
    PositionRepository,
    SessionRepository,
    TradeRepository,
)

__all__ = [
    "DatabaseManager",
    "AccountRepository",
    "ConfigChangeRepository",
    "DecisionRepository",
    "OptionsContractRepository",
    "OptimizationRecommendationRepository",
    "OptimizationRunRepository",
    "OrderRepository",
    "PerformanceSnapshotRepository",
    "PositionRepository",
    "SessionRepository",
    "TradeRepository",
]
