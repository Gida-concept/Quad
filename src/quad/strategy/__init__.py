"""Strategy system for Quad options trading bot.

Provides the strategy plugin system with auto-registration via
__init_subclass__, six built-in strategies (covered call, cash-secured
put, iron condor, straddle, strangle, vertical spread), and a factory
for creating and discovering strategy instances.
"""

from __future__ import annotations

from quad.strategy.base import StrategyBase, StrategyRegistry, ParamSpec
from quad.strategy.covered_call import CoveredCallStrategy
from quad.strategy.cash_secured_put import CashSecuredPutStrategy
from quad.strategy.iron_condor import IronCondorStrategy
from quad.strategy.straddle import StraddleStrategy
from quad.strategy.strangle import StrangleStrategy
from quad.strategy.vertical_spread import VerticalSpreadStrategy
from quad.strategy.factory import get_strategy, list_strategies, create_default_strategies


__all__ = [
    "StrategyBase",
    "StrategyRegistry",
    "ParamSpec",
    "CoveredCallStrategy",
    "CashSecuredPutStrategy",
    "IronCondorStrategy",
    "StraddleStrategy",
    "StrangleStrategy",
    "VerticalSpreadStrategy",
    "get_strategy",
    "list_strategies",
    "create_default_strategies",
]
