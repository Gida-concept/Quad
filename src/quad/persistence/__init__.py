"""Persistence layer for the Quad futures trading bot.

This module provides the database manager, repository classes, and model
definitions for PostgreSQL-based persistence (via asyncpg).
"""

from .database import DatabaseManager
from .repositories import (
    AccountRepository,
    CircuitBreakerEventRepository,
    ConfigChangeRepository,
    DecisionRepository,
    ErrorLogRepository,
    FundingRepository,
    LiquidationRepository,
    OptimizationRecommendationRepository,
    OptimizationRunRepository,
    OrderRepository,
    PerformanceSnapshotRepository,
    PositionRepository,
    SessionRepository,
    StrategyStateRepository,
    TradeRepository,
)

__all__ = [
    "DatabaseManager",
    "AccountRepository",
    "CircuitBreakerEventRepository",
    "ConfigChangeRepository",
    "DecisionRepository",
    "ErrorLogRepository",
    "FundingRepository",
    "LiquidationRepository",
    "OptimizationRecommendationRepository",
    "OptimizationRunRepository",
    "OrderRepository",
    "PerformanceSnapshotRepository",
    "PositionRepository",
    "SessionRepository",
    "StrategyStateRepository",
    "TradeRepository",
]
