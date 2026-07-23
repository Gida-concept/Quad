"""Quad configuration system.

Provides the ConfigManager class for loading, merging, and accessing
configuration from multiple sources:

1. config/config.default.yaml (shipped defaults)
2. config/config.local.yaml (user overrides, gitignored)
3. Environment variables (QUAD_* and BINANCE_*)
4. Runtime overrides (via set())

All layers merge with the last layer having the highest priority.
"""

from __future__ import annotations

from .manager import ConfigManager
from .schema import QuadConfig, validate_config

__all__ = [
    "ConfigManager",
    "QuadConfig",
    "validate_config",
]
