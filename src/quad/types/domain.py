"""Domain model types for Quad futures trading bot.

This module defines the core domain entities used throughout the
application: accounts, balances, positions, orders, and trades.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

__all__ = [
    "Account",
    "Balance",
    "FuturesPosition",
    "FuturesPositionSide",
    "MarginType",
    "Order",
    "OrderRequest",
    "OrderResult",
    "Position",
    "PositionMode",
    "PositionSide",
    "PositionStatus",
    "Trade",
]


# ============================================================================
# Enums
# ============================================================================


class FuturesPositionSide(str, Enum):
    LONG = "long"
    SHORT = "short"
    BOTH = "both"


class PositionMode(str, Enum):
    ONE_WAY = "one_way"
    HEDGE = "hedge"


class MarginType(str, Enum):
    ISOLATED = "isolated"
    CROSS = "cross"


class PositionSide(str, Enum):
    """Side of a trading position."""

    LONG = "LONG"
    SHORT = "SHORT"


class PositionStatus(str, Enum):
    """Status of a trading position."""

    OPEN = "OPEN"
    CLOSED = "CLOSED"
    LIQUIDATED = "LIQUIDATED"


@dataclass
class Balance:
    """Represents a balance for a single asset."""

    asset: str
    """Asset symbol, e.g. 'USDT'."""

    free: Decimal = Decimal(0)
    """Available (unlocked) balance."""

    locked: Decimal = Decimal(0)
    """Locked (in orders) balance."""

    @property
    def total(self) -> Decimal:
        """Total balance (free + locked)."""
        return self.free + self.locked


@dataclass
class FuturesPosition:
    symbol: str = ""
    position_side: FuturesPositionSide = FuturesPositionSide.LONG
    size: float = 0.0
    entry_price: float = 0.0
    mark_price: float = 0.0
    liquidation_price: float = 0.0
    leverage: int = 1
    margin_type: MarginType = MarginType.ISOLATED
    margin: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    funding_paid: float = 0.0
    update_time: int = 0


@dataclass
class Account:
    """Represents a trading account on an exchange."""

    id: str
    """Account identifier."""

    exchange: str
    """Exchange name, e.g. 'binance'."""

    balances: dict[str, Balance] = field(default_factory=dict)
    """Mapping of asset symbol to Balance."""

    total_usdt: Decimal = Decimal(0)
    """Total account value in USDT terms."""

    timestamp: int = 0
    """Snapshot timestamp in unix milliseconds."""

    max_leverage: int = 1
    total_wallet_balance: Decimal = Decimal(0)
    total_margin_balance: Decimal = Decimal(0)
    available_balance: Decimal = Decimal(0)
    positions: list[FuturesPosition] = field(default_factory=list)


@dataclass
class Position:
    """Represents an open or closed trading position."""

    id: int | None = None
    """Position identifier, None if not yet persisted."""

    strategy: str = ""
    """Name of the strategy that opened this position."""

    symbol: str = ""
    """Futures contract symbol for this position."""

    side: PositionSide = PositionSide.LONG
    """Position side: LONG or SHORT."""

    quantity: Decimal = Decimal(0)
    """Number of contracts."""

    entry_price: Decimal = Decimal(0)
    """Average entry price per contract."""

    current_price: Decimal = Decimal(0)
    """Current mark price of the contract."""

    unrealized_pnl: Decimal = Decimal(0)
    """Unrealized profit/loss."""

    realized_pnl: Decimal = Decimal(0)
    """Realized profit/loss from closed portions."""

    leverage: int = 1
    margin_type: MarginType = MarginType.ISOLATED
    position_side: FuturesPositionSide = FuturesPositionSide.LONG
    liquidation_price: Decimal = Decimal(0)
    initial_margin: Decimal = Decimal(0)
    maintenance_margin: Decimal = Decimal(0)
    funding_paid: Decimal = Decimal(0)

    status: PositionStatus = PositionStatus.OPEN
    """Position status: OPEN, CLOSED, or LIQUIDATED."""

    opened_at: int = 0
    """Position open timestamp in unix milliseconds."""

    updated_at: int = 0
    """Last update timestamp in unix milliseconds."""

    stop_loss_order_id: str = ""
    """Exchange order ID for the stop-loss order."""

    take_profit_order_id: str = ""
    """Exchange order ID for the take-profit order."""

    stop_loss_price: Decimal = Decimal(0)
    """Stop-loss trigger price."""

    take_profit_price: Decimal = Decimal(0)
    """Take-profit target price."""


@dataclass
class Order:
    """Represents an order placed on an exchange."""

    id: int | None = None
    """Exchange order ID, None if not yet assigned."""

    client_order_id: str = ""
    """Client-assigned order identifier."""

    symbol: str = ""
    """Trading pair symbol."""

    side: str = ""
    """Order side: BUY or SELL."""

    order_type: str = ""
    """Order type: LIMIT, MARKET, STOP_LOSS, etc."""

    quantity: Decimal = Decimal(0)
    """Requested order quantity."""

    filled_qty: Decimal = Decimal(0)
    """Quantity filled so far."""

    price: Decimal | None = None
    """Order price, or None for market orders."""

    stop_price: Decimal | None = None
    """Stop/trigger price, or None if not applicable."""

    status: str = "NEW"
    """Order status: NEW, PARTIALLY_FILLED, FILLED, CANCELLED, REJECTED, EXPIRED."""

    time_in_force: str = "GTC"
    """Time in force: GTC, IOC, FOK."""

    created_at: int = 0
    """Order creation timestamp in unix milliseconds."""

    updated_at: int = 0
    """Last update timestamp in unix milliseconds."""

    working_type: str = ""
    position_side: str = ""
    price_protect: bool = False


@dataclass
class OrderRequest:
    """Parameters for placing a new order on an exchange."""

    symbol: str = ""
    """Trading pair symbol."""

    side: str = ""
    """Order side: BUY or SELL."""

    order_type: str = ""
    """Order type: LIMIT, MARKET, STOP_LOSS, etc."""

    quantity: Decimal = Decimal(0)
    """Order quantity in contracts."""

    price: Decimal | None = None
    """Limit price, required for LIMIT orders."""

    stop_price: Decimal | None = None
    """Stop price, required for STOP_LOSS/STOP_LOSS_LIMIT orders."""

    time_in_force: str = "GTC"
    """Time in force: GTC, IOC, FOK."""

    client_order_id: str = ""
    """Optional client-assigned order ID."""

    reduce_only: bool = False
    """If True, order will only reduce an existing position."""

    post_only: bool = False
    """If True, order will only be posted as a maker order."""

    working_type: str = ""
    position_side: str = ""
    price_protect: bool = False


@dataclass
class OrderResult:
    """Result returned after submitting or querying an order."""

    order_id: int
    """Exchange-assigned order ID."""

    client_order_id: str = ""
    """Client-assigned order identifier."""

    symbol: str = ""
    """Trading pair symbol."""

    side: str = ""
    """Order side: BUY or SELL."""

    order_type: str = ""
    """Order type."""

    quantity: Decimal = Decimal(0)
    """Requested quantity."""

    filled_qty: Decimal = Decimal(0)
    """Filled quantity."""

    price: Decimal | None = None
    """Order price."""

    status: str = ""
    """Order status."""

    fills: list[dict] = field(default_factory=list)
    """List of fill details, each as a dict with keys like qty, price, commission."""


@dataclass
class Trade:
    """Represents a single executed trade/fill."""

    id: int | None = None
    """Trade identifier."""

    position_id: int | None = None
    """Position this trade belongs to."""

    order_id: int | None = None
    """Order this trade originated from."""

    symbol: str = ""
    """Trading pair symbol."""

    side: str = ""
    """Trade side: BUY or SELL."""

    quantity: Decimal = Decimal(0)
    """Traded quantity."""

    price: Decimal = Decimal(0)
    """Execution price."""

    fee: Decimal = Decimal(0)
    """Trading fee."""

    pnl: Decimal = Decimal(0)
    """Realized profit/loss from this trade."""

    timestamp: int = 0
    """Trade timestamp in unix milliseconds."""
