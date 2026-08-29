"""Quad configuration system.

Provides the ConfigManager class for loading, merging, and accessing
configuration from multiple sources:

1. config/config.yaml (single source of truth)
2. Environment variables (QUAD_* and OKX_*)
3. Runtime overrides (via set())

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
