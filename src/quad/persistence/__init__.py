"""Persistence layer for the Quad futures trading bot.

This module provides the database manager, repository classes, and model
definitions for SQLite-based persistence (via aiosqlite).
"""

from .database import DatabaseManager
from .pg import PostgresDatabaseManager, create_database
from .models import (
    ExchangeCredentialModel,
    PairingCodeModel,
    TelegramBindingModel,
    TenantConfigModel,
    TenantModel,
)
from .repositories import (
    make_repo,
    AccountRepository,
    CircuitBreakerEventRepository,
    ConfigChangeRepository,
    DecisionRepository,
    ErrorLogRepository,
    ExchangeCredentialRepository,
    FundingRepository,
    LiquidationRepository,
    OptimizationRecommendationRepository,
    OptimizationRunRepository,
    OrderRepository,
    PairingCodeRepository,
    PerformanceSnapshotRepository,
    PositionRepository,
    SessionRepository,
    StrategyStateRepository,
    TelegramBindingRepository,
    TenantConfigRepository,
    TenantRepository,
    TradeRepository,
)

__all__ = [
    "AccountRepository",
    "CircuitBreakerEventRepository",
    "ConfigChangeRepository",
    "DatabaseManager",
    "PostgresDatabaseManager",
    "create_database",
    "make_repo",
    "DecisionRepository",
    "ErrorLogRepository",
    "ExchangeCredentialModel",
    "ExchangeCredentialRepository",
    "FundingRepository",
    "LiquidationRepository",
    "OptimizationRecommendationRepository",
    "OptimizationRunRepository",
    "OrderRepository",
    "PairingCodeModel",
    "PairingCodeRepository",
    "PerformanceSnapshotRepository",
    "PositionRepository",
    "SessionRepository",
    "StrategyStateRepository",
    "TelegramBindingModel",
    "TelegramBindingRepository",
    "TenantConfigModel",
    "TenantConfigRepository",
    "TenantModel",
    "TenantRepository",
    "TradeRepository",
]
