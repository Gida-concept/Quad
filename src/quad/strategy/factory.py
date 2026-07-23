"""Strategy factory for creating and discovering strategy instances.

Provides factory functions to create strategy instances by name,
list all registered strategies with metadata, and create default
strategy instances from configuration.
"""

from __future__ import annotations

from typing import Any

import structlog

from quad.strategy.base import StrategyBase, StrategyRegistry


logger = structlog.get_logger(__name__)


def get_strategy(
    name: str,
    params: dict[str, Any] | None = None,
) -> StrategyBase | None:
    """Create a strategy instance by name with optional parameters.

    Looks up the strategy class in the auto-populated registry and
    instantiates it with the provided parameter overrides.

    Args:
        name: Strategy name (key in the registry).
        params: Optional dictionary of parameter overrides.

    Returns:
        A StrategyBase instance, or None if the strategy name is not
        found in the registry.
    """
    cls = StrategyRegistry.get(name)
    if cls is None:
        logger.warning("strategy_not_found", name=name)
        return None
    try:
        instance = cls(params or {})
        logger.debug("strategy_created", name=name, cls=cls.__name__)
        return instance
    except (ValueError, TypeError) as exc:
        logger.error("strategy_creation_failed", name=name, error=str(exc))
        return None


def list_strategies() -> list[dict[str, Any]]:
    """List all registered strategies with metadata.

    Returns a human-readable list of available strategies including
    their names, descriptions, and parameter specifications.

    Returns:
        List of dicts, each with keys: name, description, params.
    """
    return [
        {
            "name": name,
            "description": cls.get_description(),
            "params": cls.get_params_spec(),
        }
        for name, cls in StrategyBase.registry.items()
    ]


def create_default_strategies(
    config: dict[str, Any],
) -> dict[str, StrategyBase]:
    """Create instances of all configured default strategies.

    Reads strategy parameters from the config dict (keyed by strategy
    name under the 'strategy' section) and instantiates all registered
    strategy classes.

    Args:
        config: Global configuration dictionary. The 'strategy' key
            may contain per-strategy parameter dicts.

    Returns:
        Dict mapping strategy name to instantiated StrategyBase instance.
        Strategies that fail to instantiate are excluded.
    """
    strategies: dict[str, StrategyBase] = {}
    strategy_configs = config.get("strategy", {})

    for name in StrategyRegistry.list():
        params = strategy_configs.get(name, {})
        instance = get_strategy(name, params)
        if instance is not None:
            strategies[name] = instance

    logger.info(
        "default_strategies_created",
        count=len(strategies),
        names=list(strategies.keys()),
    )
    return strategies
