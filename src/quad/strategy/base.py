"""Abstract base class and registry for the strategy plugin system.

Provides StrategyBase ABC with __init_subclass__ auto-registration,
ParamSpec dataclass for parameter definitions, and StrategyRegistry
for discovery and access.
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from quad.types.risk import Action
from quad.types.strategy import StrategyContext


logger = structlog.get_logger(__name__)


@dataclass
class ParamSpec:
    """Specification for a single strategy parameter.

    Defines the name, type, default value, description, and optional
    range constraints for a configuration parameter exposed by a strategy.
    """

    name: str
    """Parameter name (used as dict key in strategy params)."""

    type: Literal["int", "float", "decimal", "str", "bool"]
    """Expected parameter type."""

    default: Any = None
    """Default value when not explicitly provided."""

    description: str = ""
    """Human-readable description of this parameter."""

    min_value: float | None = None
    """Minimum allowed value (for int/float/decimal types)."""

    max_value: float | None = None
    """Maximum allowed value (for int/float/decimal types)."""

    required: bool = True
    """Whether this parameter must be provided (no default)."""


class StrategyBase(ABC):
    """Abstract base for all trading strategies.

    Uses __init_subclass__ for automatic registration in the strategy
    registry. Subclasses must implement evaluate(), get_name(),
    get_description(), and get_params_spec().
    """

    registry: dict[str, type["StrategyBase"]] = {}

    # ---- Auto-registration via __init_subclass__ ----

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Automatically register non-abstract subclasses."""
        super().__init_subclass__(**kwargs)
        if not cls.__name__.startswith("_"):
            StrategyBase._register(cls)

    @classmethod
    def _register(cls, strategy_cls: type["StrategyBase"]) -> None:
        """Register a strategy class under its canonical name."""
        try:
            name = strategy_cls.get_name()
        except (TypeError, NotImplementedError):
            return
        cls.registry[name] = strategy_cls
        logger.debug("strategy_registered", name=name, cls=strategy_cls.__name__)

    # ---- Instance lifecycle ----

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        """Initialize strategy with optional parameter overrides.

        Args:
            params: Dictionary of parameter values overriding defaults.
                    Missing parameters fall back to ParamSpec.default.

        Raises:
            ValueError: If a required parameter is missing with no default.
            TypeError: If a parameter value has the wrong type.
        """
        self.params: dict[str, Any] = params or {}
        self.logger = logger.bind(strategy=self.get_name())
        self._validate_params()

    def _validate_params(self) -> None:
        """Validate provided parameters against the param spec.

        Checks that all required parameters are present and that
        values match the declared type.
        """
        spec_map = {s.name: s for s in self.get_params_spec()}

        for spec_entry in self.get_params_spec():
            if spec_entry.required and spec_entry.name not in self.params and spec_entry.default is None:
                raise ValueError(
                    f"Missing required parameter '{spec_entry.name}' "
                    f"for strategy '{self.get_name()}'"
                )

        for key, value in self.params.items():
            spec_entry = spec_map.get(key)
            if spec_entry is None:
                continue
            expected = spec_entry.type
            if expected == "int" and not isinstance(value, int):
                raise TypeError(
                    f"Parameter '{key}' must be int, got {type(value).__name__}"
                )
            if expected == "float" and not isinstance(value, (int, float)):
                raise TypeError(
                    f"Parameter '{key}' must be float, got {type(value).__name__}"
                )
            if expected == "decimal":
                if isinstance(value, Decimal):
                    continue
                if isinstance(value, (int, float)):
                    self.params[key] = Decimal(str(value))
                    continue
                raise TypeError(
                    f"Parameter '{key}' must be Decimal or numeric, "
                    f"got {type(value).__name__}"
                )
            if expected == "bool" and not isinstance(value, bool):
                raise TypeError(
                    f"Parameter '{key}' must be bool, got {type(value).__name__}"
                )
            if expected == "str" and not isinstance(value, str):
                raise TypeError(
                    f"Parameter '{key}' must be str, got {type(value).__name__}"
                )

    def get_param(self, name: str, default: Any = None) -> Any:
        """Get a parameter value with fallback chain.

        Priority: instance param > spec default > provided default.

        Args:
            name: Parameter name.
            default: Fallback value if not found in params or spec.

        Returns:
            The resolved parameter value.
        """
        if name in self.params:
            return self.params[name]
        for spec_entry in self.get_params_spec():
            if spec_entry.name == name and spec_entry.default is not None:
                return spec_entry.default
        return default

    def hold_action(self, reason: str = "No action required") -> list[Action]:
        """Return a HOLD action indicating no trade decision.

        Args:
            reason: Human-readable reason for the hold.

        Returns:
            List containing a single HOLD Action.
        """
        return [
            Action(
                type="HOLD",
                strategy=self.get_name(),
                reason=reason,
            )
        ]

    # ======================================================================
    # Shared static helpers — inherited by all strategies
    # ======================================================================

    @staticmethod
    def _calculate_dte(contract: dict[str, Any]) -> int | None:
        """Calculate days to expiry from a contract dict."""
        expiry = contract.get("expiry")
        if expiry is None:
            return None
        try:
            expiry_ts = int(expiry) / 1000 if int(expiry) > 1e12 else int(expiry)
            now = datetime.now(tz=timezone.utc).timestamp()
            return max(0, int((expiry_ts - now) / 86400))
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _mid_price(contract: dict[str, Any]) -> Decimal | None:
        """Calculate mid price from bid/ask, falling back to mark price."""
        bid = contract.get("bid")
        ask = contract.get("ask")
        if bid is not None and ask is not None:
            try:
                return (Decimal(str(bid)) + Decimal(str(ask))) / Decimal("2")
            except Exception:
                pass
        mark = contract.get("mark_price")
        if mark is not None:
            try:
                return Decimal(str(mark))
            except Exception:
                pass
        return None

    @staticmethod
    def _to_decimal(value: Any) -> Decimal:
        """Safely convert a value to Decimal, returning 0 on failure."""
        if isinstance(value, Decimal):
            return value
        try:
            return Decimal(str(value))
        except (ValueError, TypeError):
            return Decimal("0")

    @staticmethod
    def _iter_contracts(option_chain: list) -> list[dict[str, Any]]:
        """Normalize option chain entries to dicts."""
        result = []
        for item in option_chain:
            if hasattr(item, "__dataclass_fields__"):
                result.append({f: getattr(item, f) for f in item.__dataclass_fields__})
            elif isinstance(item, dict):
                result.append(item)
            else:
                result.append({})
        return result

    @staticmethod
    def _to_dict(obj: Any) -> dict[str, Any]:
        """Convert a potentially dataclass object to dict."""
        if hasattr(obj, "__dataclass_fields__"):
            return {f: getattr(obj, f) for f in obj.__dataclass_fields__}
        if isinstance(obj, dict):
            return obj
        return {}

    def _dte_in_range(self, contract: dict[str, Any], min_dte: int, max_dte: int) -> bool:
        """Check if contract DTE falls within [min_dte, max_dte]."""
        dte = self._calculate_dte(contract)
        return dte is not None and min_dte <= dte <= max_dte

    # ======================================================================
    # Shared instance helpers — for multi-leg strategies
    # ======================================================================

    def _combined_value(self, legs: list[dict[str, Any]]) -> Decimal:
        """Calculate combined mid-price value of a list of legs."""
        total = Decimal("0")
        for leg in legs:
            price = self._mid_price(leg)
            if price is not None:
                total += price
        return total

    def _find_by_delta(
        self,
        contracts: list[dict[str, Any]],
        target_delta: float,
        option_type: str,
        underlying_price: Decimal,
        above_strike: bool,
    ) -> dict[str, Any] | None:
        """Find contract closest to target delta, filtered by type and moneyness."""
        eligible = []
        for c in contracts:
            if c.get("option_type") != option_type:
                continue
            strike = self._to_decimal(c.get("strike", 0))
            if above_strike and strike <= underlying_price:
                continue
            if not above_strike and strike >= underlying_price:
                continue
            delta = abs(self._to_decimal(c.get("delta", 0)))
            if delta <= Decimal("0"):
                continue
            eligible.append(c)

        if not eligible:
            return None

        target = Decimal(str(target_delta))
        return min(eligible, key=lambda c: abs(abs(self._to_decimal(c.get("delta", 0))) - target))

    def _find_nearest_strike(
        self,
        contracts: list[dict[str, Any]],
        target_strike: Decimal,
        option_type: str,
    ) -> dict[str, Any] | None:
        """Find contract of given type with strike closest to target."""
        eligible = [
            c for c in contracts
            if c.get("option_type") == option_type
        ]
        if not eligible:
            return None
        return min(
            eligible,
            key=lambda c: abs(self._to_decimal(c.get("strike", 0)) - target_strike),
        )

    # ---- Abstract interface ----

    @abstractmethod
    async def evaluate(self, context: StrategyContext) -> list[Action]:
        """Evaluate the strategy against the current context.

        Args:
            context: Full market and account context for decision-making.

        Returns:
            List of Action objects representing trading decisions.
            At minimum contains one HOLD action when no trade is warranted.
        """
        ...

    @staticmethod
    @abstractmethod
    def get_name() -> str:
        """Return the unique machine-readable name for this strategy.

        Returns:
            Lowercase snake_case identifier, e.g. 'covered_call'.
        """
        ...

    @staticmethod
    @abstractmethod
    def get_description() -> str:
        """Return a human-readable description of this strategy.

        Returns:
            Short description of the strategy and its mechanics.
        """
        ...

    @staticmethod
    @abstractmethod
    def get_params_spec() -> list[ParamSpec]:
        """Return the parameter specification for this strategy.

        Returns:
            List of ParamSpec dataclass instances defining each
            configurable parameter.
        """
        ...


class StrategyRegistry:
    """Registry for discovering and accessing strategy implementations.

    Provides static methods to query the auto-populated registry
    maintained by StrategyBase.__init_subclass__.
    """

    @staticmethod
    def get(name: str) -> type[StrategyBase] | None:
        """Get a strategy class by its registered name.

        Args:
            name: Strategy name (e.g. 'covered_call').

        Returns:
            The strategy class, or None if not found.
        """
        return StrategyBase.registry.get(name)

    @staticmethod
    def list() -> list[str]:
        """List all registered strategy names in sorted order.

        Returns:
            Sorted list of strategy name strings.
        """
        return sorted(StrategyBase.registry.keys())

    @staticmethod
    def get_specs() -> dict[str, list[ParamSpec]]:
        """Get parameter specifications for all registered strategies.

        Returns:
            Dict mapping strategy name to its list of ParamSpec.
        """
        return {
            name: cls.get_params_spec()
            for name, cls in StrategyBase.registry.items()
        }
