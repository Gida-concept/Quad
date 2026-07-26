"""Strategy system for Quad futures trading bot.

Provides the strategy plugin system with auto-registration via
__init_subclass__, built-in strategies, and a factory
for creating and discovering strategy instances.
"""

from __future__ import annotations

from quad.strategy.base import StrategyBase, StrategyRegistry, ParamSpec
from quad.strategy.swing_trading import SwingTradingStrategy
from quad.strategy.factory import get_strategy, list_strategies, create_default_strategies


__all__ = [
    "StrategyBase",
    "StrategyRegistry",
    "ParamSpec",
    "SwingTradingStrategy",
    "get_strategy",
    "list_strategies",
    "create_default_strategies",
]
