"""Persistence layer for the Quad futures trading bot.

This module provides the database manager, repository classes, and model
definitions for SQLite-based persistence (via aiosqlite).
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
    "AccountRepository",
    "CircuitBreakerEventRepository",
    "ConfigChangeRepository",
    "DatabaseManager",
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
