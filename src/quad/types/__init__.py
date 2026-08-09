"""Quad types package.

Re-exports all type definitions from submodules for convenient
access via ``from quad.types import *``.
"""

from quad.types import domain, exchange, market, risk, strategy
from quad.types.domain import *
from quad.types.exchange import *
from quad.types.market import *
from quad.types.risk import *
from quad.types.strategy import *

# Compose from submodule __all__ lists (order-preserving, dedupe-safe)
__all__ = tuple(
    dict.fromkeys(
        name
        for module in (domain, exchange, market, risk, strategy)
        for name in module.__all__
    )
)
