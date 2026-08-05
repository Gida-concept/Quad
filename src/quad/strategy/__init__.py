"""Strategy system for Quad futures trading bot.

Provides the strategy plugin system with auto-registration via
__init_subclass__, built-in strategies, and a factory
for creating and discovering strategy instances.
"""

from __future__ import annotations

from quad.strategy.base import ParamSpec, StrategyBase, StrategyRegistry
from quad.strategy.factory import (
    create_default_strategies,
    get_strategy,
    list_strategies,
)
from quad.strategy.trend_following import (
    TrendFollowingStrategy,  # noqa: F401  # side-effect import to trigger __init_subclass__ registration
)

__all__ = [
    "ParamSpec",
    "StrategyBase",
    "StrategyRegistry",
    "create_default_strategies",
    "get_strategy",
    "list_strategies",
]
