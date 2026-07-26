"""Abstract base class and registry for the strategy plugin system.

Provides StrategyBase ABC with __init_subclass__ auto-registration,
ParamSpec dataclass for parameter definitions, and StrategyRegistry
for discovery and access.
"""

from __future__ import annotations

import structlog
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from quad.types.domain import FuturesPositionSide
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

    def __init__(
        self,
        params: dict[str, Any] | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        """Initialize strategy with optional parameter overrides.

        Args:
            params: Dictionary of parameter values overriding defaults.
                    Missing parameters fall back to ParamSpec.default.
            config: Optional full application configuration dict. Used by
                    strategies that need access to risk/trading sections.

        Raises:
            ValueError: If a required parameter is missing with no default.
            TypeError: If a parameter value has the wrong type.
        """
        self.params: dict[str, Any] = params or {}
        self._config: dict[str, Any] = config or {}
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
        """Return a hold action indicating no trade decision.

        Args:
            reason: Human-readable reason for the hold.

        Returns:
            List containing a single hold Action.
        """
        return [
            Action(
                type="hold",
                strategy=self.get_name(),
                reason=reason,
            )
        ]

    # ======================================================================
    # Shared static helpers — inherited by all strategies
    # ======================================================================

    @staticmethod
    def _calculate_position_size_usd(
        capital: float,
        risk_pct: float,
        stop_loss_pct: float,
        max_size_usd: float = 0,
    ) -> float:
        """Calculate position size in USD based on risk parameters.

        Args:
            capital: Available capital in USD.
            risk_pct: Fraction of capital to risk (e.g. 0.02 for 2%).
            stop_loss_pct: Stop loss as fraction of entry (e.g. 0.05 for 5%).
            max_size_usd: Maximum position size cap. 0 = no cap.

        Returns:
            Position size in USD.
        """
        risk_amount = capital * risk_pct
        if stop_loss_pct <= 0:
            return 0.0
        size = risk_amount / stop_loss_pct
        if max_size_usd > 0:
            size = min(size, max_size_usd)
        return round(size, 2)

    @staticmethod
    def _build_tp_sl_actions(
        symbol: str,
        side: str,
        entry_price: float,
        capital: float,
        sl_capital_pct: float,
        tp_capital_pct: float,
        leverage: float = 1.0,
        strategy_name: str = "",
    ) -> list[Action]:
        """Build set_stop_loss and set_take_profit actions for a position.

        Uses the 2:1 TP/SL ratio relative to trade capital.

        Args:
            symbol: Trading pair symbol.
            side: Position side ("LONG" or "SHORT").
            entry_price: Average entry price.
            capital: Trade capital in USD.
            sl_capital_pct: Stop-loss as percentage of capital (e.g. 30.0 = 30%).
            tp_capital_pct: Take-profit as percentage of capital (e.g. 50.0 = 50%).
            leverage: Position leverage multiplier.
            strategy_name: Strategy name for the actions.

        Returns:
            List of Action objects (set_stop_loss, set_take_profit).
            Empty list if prices can't be computed.
        """
        actions: list[Action] = []

        if side.upper() == "LONG":
            # For LONG: SL is below entry, TP is above entry
            # Price movement needed = capital_pct / leverage
            sl_price = entry_price * (1 - sl_capital_pct / 100.0 / leverage)
            tp_price = entry_price * (1 + tp_capital_pct / 100.0 / leverage)
        else:
            # For SHORT: SL is above entry, TP is below entry
            sl_price = entry_price * (1 + sl_capital_pct / 100.0 / leverage)
            tp_price = entry_price * (1 - tp_capital_pct / 100.0 / leverage)

        if sl_price > 0:
            actions.append(Action(
                type="set_stop_loss",
                strategy=strategy_name,
                symbol=symbol,
                stop_loss_price=Decimal(str(round(sl_price, 8))),
                reason=f"Stop loss at {sl_price:.8f} ({sl_capital_pct}% of capital at {leverage}x)",
            ))
        if tp_price > 0:
            actions.append(Action(
                type="set_take_profit",
                strategy=strategy_name,
                symbol=symbol,
                take_profit_price=Decimal(str(round(tp_price, 8))),
                reason=f"Take profit at {tp_price:.8f} ({tp_capital_pct}% of capital at {leverage}x)",
            ))

        return actions

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
    def _to_dict(obj: Any) -> dict[str, Any]:
        """Convert a potentially dataclass object to dict."""
        if hasattr(obj, "__dataclass_fields__"):
            return {f: getattr(obj, f) for f in obj.__dataclass_fields__}
        if isinstance(obj, dict):
            return obj
        return {}

    def _get_current_price(self, symbol: str, context: StrategyContext) -> float | None:
        """Get current mark price for a symbol from context.

        Args:
            symbol: Trading pair symbol.
            context: The strategy execution context.

        Returns:
            Mark price as float, or None if not available.
        """
        return context.mark_prices.get(symbol)

    def _get_atr(self, symbol: str, context: StrategyContext, period: int | None = None) -> float | None:
        """Estimate ATR from available price data in context.

        Simple implementation: returns the ATR value from strategy_params
        if pre-calculated, otherwise returns a default percentage.

        Args:
            symbol: Trading pair symbol.
            context: The strategy execution context.
            period: ATR period (for logging/documentation).

        Returns:
            Estimated ATR value, or None if unavailable.
        """
        if period is None:
            period = int(float(self.get_param("atr_period", 14)))
        atr_key = f"atr_{symbol}"
        if context.strategy_params and atr_key in context.strategy_params:
            return float(context.strategy_params[atr_key])
        default_atr_pct = self.get_param("atr_default_pct", 0.02)
        price = self._get_current_price(symbol, context)
        if price:
            return price * default_atr_pct
        return None

    def _check_liquidation_risk(
        self,
        liquidation_price: float,
        mark_price: float,
        position_side: FuturesPositionSide,
        threshold_pct: float | None = None,
    ) -> tuple[bool, float]:
        """Check distance to liquidation.

        Args:
            liquidation_price: Position's liquidation price.
            mark_price: Current mark price.
            position_side: LONG or SHORT.
            threshold_pct: Minimum safe distance as fraction (0.2 = 20%).

        Returns:
            Tuple of (is_safe, distance_pct). is_safe is True if the position
            is more than threshold_pct away from liquidation.
        """
        if threshold_pct is None:
            threshold_pct = float(self.get_param("liquidation_threshold_pct", 0.2))
        if liquidation_price <= 0 or mark_price <= 0:
            return True, 1.0
        if position_side == FuturesPositionSide.LONG:
            distance = (mark_price - liquidation_price) / mark_price
        else:
            distance = (liquidation_price - mark_price) / mark_price
        return distance > threshold_pct, float(distance)

    def _calculate_funding_cost(
        self,
        position_size_usd: float,
        funding_rate: float,
        hours_held: float | None = None,
    ) -> float:
        """Calculate projected funding cost for holding a position.

        Binance funds every 8 hours. A positive funding rate means longs pay shorts.

        Args:
            position_size_usd: Position notional value in USD.
            funding_rate: Current funding rate (e.g. 0.0001 for 0.01%).
            hours_held: Expected hours until position close.

        Returns:
            Projected funding cost in USD (positive = cost to you as a long).
        """
        if hours_held is None:
            hours_held = float(self.get_param("funding_hours_held", 8))
        funding_interval_hours = float(self.get_param("funding_interval_hours", 8.0))
        funding_intervals = hours_held / funding_interval_hours
        return position_size_usd * funding_rate * funding_intervals

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
