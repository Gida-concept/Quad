"""Database models for the Quad futures trading bot.

This module defines all 12 table schemas as dataclasses with SQLite DDL
generation and row serialization/deserialization. All Decimal values are stored
as TEXT to preserve precision losslessly. Timestamps are Unix epoch milliseconds
stored as BIGINT.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, fields
from typing import Any, ClassVar

if sys.version_info >= (3, 11):
    from typing import Self
else:
    from typing_extensions import Self

# ---------------------------------------------------------------------------
# Schema versioning
# ---------------------------------------------------------------------------

SCHEMA_VERSION: int = 3
"""Current schema version. Increment when making breaking changes."""

SCHEMA_MIGRATIONS: dict[int, list[str]] = {
    # version -> list of DDL / ALTER statements
}
"""Mapping of version numbers to lists of SQL migration statements."""


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _col_names(cls: type) -> list[str]:
    """Return column names for a model dataclass (all fields)."""
    return [f.name for f in fields(cls)]


def _to_row(instance: Any) -> tuple:
    """Serialize a dataclass instance to a tuple for INSERT.

    Converts ``id=0`` to ``None`` so SQLite ``INTEGER PRIMARY KEY
    AUTOINCREMENT`` auto-generates the next id (PostgreSQL's SERIAL
    ignores explicit zero; SQLite stores it literally).
    """
    vals = []
    for f in fields(instance.__class__):
        v = getattr(instance, f.name)
        if f.name == "id" and v == 0:
            v = None
        vals.append(v)
    return tuple(vals)


def _from_row(cls: type, row: tuple) -> Any:
    """Construct a model instance from a database row tuple."""
    return cls(*row)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass
class AccountModel:
    """Trading account state from the exchange."""

    __tablename__: ClassVar[str] = "accounts"

    id: int
    exchange: str
    balances_json: str
    total_usdt: str
    created_at: int
    updated_at: int

    @classmethod
    def create_table_ddl(cls) -> str:
        return """CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange TEXT NOT NULL,
    balances_json TEXT NOT NULL DEFAULT '{}',
    total_usdt TEXT NOT NULL DEFAULT '0',
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL
)"""

    @classmethod
    def columns(cls) -> list[str]:
        return _col_names(cls)

    def to_row(self) -> tuple:
        return _to_row(self)

    @classmethod
    def from_row(cls, row: tuple) -> Self:
        return _from_row(cls, row)


@dataclass
class PositionModel:
    """Trading position -- open or closed."""

    __tablename__: ClassVar[str] = "positions"

    id: int
    strategy: str
    symbol: str
    side: str
    quantity: str
    entry_price: str
    current_price: str
    unrealized_pnl: str
    realized_pnl: str
    status: str
    opened_at: int
    updated_at: int
    leverage: int = 1
    margin_type: str = "isolated"
    position_side: str = "LONG"
    liquidation_price: str = "0"
    initial_margin: str = "0"
    maintenance_margin: str = "0"
    funding_paid: str = "0"

    @classmethod
    def create_table_ddl(cls) -> str:
        return """CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity TEXT NOT NULL DEFAULT '0',
    entry_price TEXT NOT NULL DEFAULT '0',
    current_price TEXT NOT NULL DEFAULT '0',
    unrealized_pnl TEXT NOT NULL DEFAULT '0',
    realized_pnl TEXT NOT NULL DEFAULT '0',
    leverage INTEGER NOT NULL DEFAULT 1,
    margin_type TEXT NOT NULL DEFAULT 'isolated',
    position_side TEXT NOT NULL DEFAULT 'LONG',
    liquidation_price TEXT NOT NULL DEFAULT '0',
    initial_margin TEXT NOT NULL DEFAULT '0',
    maintenance_margin TEXT NOT NULL DEFAULT '0',
    funding_paid TEXT NOT NULL DEFAULT '0',
    status TEXT NOT NULL DEFAULT 'OPEN',
    opened_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL
)"""

    @classmethod
    def columns(cls) -> list[str]:
        return _col_names(cls)

    def to_row(self) -> tuple:
        return _to_row(self)

    @classmethod
    def from_row(cls, row: tuple) -> Self:
        return _from_row(cls, row)


@dataclass
class OrderModel:
    """Order placed on the exchange."""

    __tablename__: ClassVar[str] = "orders"

    id: int
    client_order_id: str
    position_id: int
    symbol: str
    side: str
    type: str
    quantity: str
    filled_qty: str
    price: str
    status: str
    time_in_force: str
    created_at: int
    updated_at: int
    working_type: str = ""
    position_side: str = ""
    price_protect: bool = False
    avg_fill_price: str = "0"

    @classmethod
    def create_table_ddl(cls) -> str:
        return """CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT NOT NULL UNIQUE,
    position_id INTEGER,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    type TEXT NOT NULL,
    quantity TEXT NOT NULL DEFAULT '0',
    filled_qty TEXT NOT NULL DEFAULT '0',
    price TEXT NOT NULL DEFAULT '0',
    status TEXT NOT NULL DEFAULT 'NEW',
    time_in_force TEXT NOT NULL DEFAULT 'GTC',
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL,
    working_type TEXT NOT NULL DEFAULT '',
    position_side TEXT NOT NULL DEFAULT '',
    price_protect INTEGER NOT NULL DEFAULT 0,
    avg_fill_price TEXT NOT NULL DEFAULT '0',
    FOREIGN KEY (position_id) REFERENCES positions(id)
)"""

    @classmethod
    def columns(cls) -> list[str]:
        return _col_names(cls)

    def to_row(self) -> tuple:
        return _to_row(self)

    @classmethod
    def from_row(cls, row: tuple) -> Self:
        return _from_row(cls, row)


@dataclass
class TradeModel:
    """Single executed trade / fill."""

    __tablename__: ClassVar[str] = "trades"

    id: int
    position_id: int
    order_id: int
    symbol: str
    side: str
    quantity: str
    price: str
    fee: str
    pnl: str
    timestamp: int

    @classmethod
    def create_table_ddl(cls) -> str:
        return """CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER,
    order_id INTEGER,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity TEXT NOT NULL DEFAULT '0',
    price TEXT NOT NULL DEFAULT '0',
    fee TEXT NOT NULL DEFAULT '0',
    pnl TEXT NOT NULL DEFAULT '0',
    timestamp BIGINT NOT NULL,
    FOREIGN KEY (position_id) REFERENCES positions(id),
    FOREIGN KEY (order_id) REFERENCES orders(id)
)"""

    @classmethod
    def columns(cls) -> list[str]:
        return _col_names(cls)

    def to_row(self) -> tuple:
        return _to_row(self)

    @classmethod
    def from_row(cls, row: tuple) -> Self:
        return _from_row(cls, row)


@dataclass
class OptionContractModel:
    """Option chain contract snapshot (historical reference)."""

    __tablename__: ClassVar[str] = "option_contracts"

    id: int
    symbol: str
    underlying: str
    strike: str
    expiry: int
    option_type: str
    mark_price: str
    bid: str
    ask: str
    volume: str
    open_interest: int
    iv: str
    delta: str
    gamma: str
    theta: str
    vega: str
    updated_at: int

    @classmethod
    def create_table_ddl(cls) -> str:
        return """CREATE TABLE IF NOT EXISTS option_contracts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL UNIQUE,
    underlying TEXT NOT NULL,
    strike TEXT NOT NULL DEFAULT '0',
    expiry BIGINT NOT NULL,
    option_type TEXT NOT NULL,
    mark_price TEXT NOT NULL DEFAULT '0',
    bid TEXT NOT NULL DEFAULT '0',
    ask TEXT NOT NULL DEFAULT '0',
    volume TEXT NOT NULL DEFAULT '0',
    open_interest INTEGER NOT NULL DEFAULT 0,
    iv TEXT NOT NULL DEFAULT '0',
    delta TEXT NOT NULL DEFAULT '0',
    gamma TEXT NOT NULL DEFAULT '0',
    theta TEXT NOT NULL DEFAULT '0',
    vega TEXT NOT NULL DEFAULT '0',
    updated_at BIGINT NOT NULL
)"""

    @classmethod
    def columns(cls) -> list[str]:
        return _col_names(cls)

    def to_row(self) -> tuple:
        return _to_row(self)

    @classmethod
    def from_row(cls, row: tuple) -> Self:
        return _from_row(cls, row)


@dataclass
class DecisionModel:
    """Strategy decision record."""

    __tablename__: ClassVar[str] = "decisions"

    id: int
    timestamp: int
    strategy: str
    action: str
    symbol: str
    reason: str
    risk_passed: int
    executed: int
    cycle_time_ms: int

    @classmethod
    def create_table_ddl(cls) -> str:
        return """CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp BIGINT NOT NULL,
    strategy TEXT NOT NULL,
    action TEXT NOT NULL,
    symbol TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    risk_passed INTEGER NOT NULL DEFAULT 0,
    executed INTEGER NOT NULL DEFAULT 0,
    cycle_time_ms INTEGER NOT NULL DEFAULT 0
)"""

    @classmethod
    def columns(cls) -> list[str]:
        return _col_names(cls)

    def to_row(self) -> tuple:
        return _to_row(self)

    @classmethod
    def from_row(cls, row: tuple) -> Self:
        return _from_row(cls, row)


@dataclass
class StrategyStateModel:
    """Persistent state of a strategy."""

    __tablename__: ClassVar[str] = "strategy_state"

    id: int
    strategy_name: str
    enabled: int
    params_json: str
    status: str

    @classmethod
    def create_table_ddl(cls) -> str:
        return """CREATE TABLE IF NOT EXISTS strategy_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1,
    params_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'idle'
)"""

    @classmethod
    def columns(cls) -> list[str]:
        return _col_names(cls)

    def to_row(self) -> tuple:
        return _to_row(self)

    @classmethod
    def from_row(cls, row: tuple) -> Self:
        return _from_row(cls, row)


@dataclass
class SessionModel:
    """Trading session record."""

    __tablename__: ClassVar[str] = "sessions"

    id: int
    start_time: int
    end_time: int | None
    mode: str
    state: str
    pnl: str
    trades_count: int

    @classmethod
    def create_table_ddl(cls) -> str:
        return """CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_time BIGINT NOT NULL,
    end_time BIGINT,
    mode TEXT NOT NULL DEFAULT 'binance',
    state TEXT NOT NULL DEFAULT 'running',
    pnl TEXT NOT NULL DEFAULT '0',
    trades_count INTEGER NOT NULL DEFAULT 0
)"""

    @classmethod
    def columns(cls) -> list[str]:
        return _col_names(cls)

    def to_row(self) -> tuple:
        return _to_row(self)

    @classmethod
    def from_row(cls, row: tuple) -> Self:
        cls_fields = fields(cls)
        # handle nullable end_time
        field_values = list(row)
        for i, f in enumerate(cls_fields):
            if f.type in ("int | None", "Optional[int]") and field_values[i] is not None:
                try:
                    field_values[i] = int(field_values[i])
                except (TypeError, ValueError):
                    pass
        return cls(*field_values)


@dataclass
class PerformanceSnapshotModel:
    """Periodic portfolio performance snapshot."""

    __tablename__: ClassVar[str] = "performance_snapshots"

    id: int
    timestamp: int
    portfolio_value: str
    drawdown: str
    positions_count: int
    daily_pnl: str

    @classmethod
    def create_table_ddl(cls) -> str:
        return """CREATE TABLE IF NOT EXISTS performance_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp BIGINT NOT NULL,
    portfolio_value TEXT NOT NULL DEFAULT '0',
    drawdown TEXT NOT NULL DEFAULT '0',
    positions_count INTEGER NOT NULL DEFAULT 0,
    daily_pnl TEXT NOT NULL DEFAULT '0'
)"""

    @classmethod
    def columns(cls) -> list[str]:
        return _col_names(cls)

    def to_row(self) -> tuple:
        return _to_row(self)

    @classmethod
    def from_row(cls, row: tuple) -> Self:
        return _from_row(cls, row)


@dataclass
class CircuitBreakerEventModel:
    """Circuit breaker trigger event."""

    __tablename__: ClassVar[str] = "circuit_breaker_events"

    id: int
    timestamp: int
    breaker_name: str
    tier: int
    reason: str
    resolved_at: int

    @classmethod
    def create_table_ddl(cls) -> str:
        return """CREATE TABLE IF NOT EXISTS circuit_breaker_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp BIGINT NOT NULL,
    breaker_name TEXT NOT NULL,
    tier INTEGER NOT NULL DEFAULT 1,
    reason TEXT NOT NULL DEFAULT '',
    resolved_at BIGINT
)"""

    @classmethod
    def columns(cls) -> list[str]:
        return _col_names(cls)

    def to_row(self) -> tuple:
        return _to_row(self)

    @classmethod
    def from_row(cls, row: tuple) -> Self:
        cls_fields = fields(cls)
        field_values = list(row)
        for i, f in enumerate(cls_fields):
            if f.type in ("int | None", "Optional[int]") and field_values[i] is not None:
                try:
                    field_values[i] = int(field_values[i])
                except (TypeError, ValueError):
                    pass
        return cls(*field_values)


@dataclass
class ConfigChangeModel:
    """Audit log for configuration changes."""

    __tablename__: ClassVar[str] = "config_changes"

    id: int
    timestamp: int
    key: str
    old_value: str
    new_value: str
    source: str

    @classmethod
    def create_table_ddl(cls) -> str:
        return """CREATE TABLE IF NOT EXISTS config_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp BIGINT NOT NULL,
    key TEXT NOT NULL,
    old_value TEXT NOT NULL DEFAULT '',
    new_value TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT ''
)"""

    @classmethod
    def columns(cls) -> list[str]:
        return _col_names(cls)

    def to_row(self) -> tuple:
        return _to_row(self)

    @classmethod
    def from_row(cls, row: tuple) -> Self:
        return _from_row(cls, row)


@dataclass
class ErrorLogModel:
    """Application error log entry."""

    __tablename__: ClassVar[str] = "error_logs"

    id: int
    timestamp: int
    level: str
    event: str
    message: str
    details_json: str

    @classmethod
    def create_table_ddl(cls) -> str:
        return """CREATE TABLE IF NOT EXISTS error_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp BIGINT NOT NULL,
    level TEXT NOT NULL DEFAULT 'ERROR',
    event TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}'
)"""

    @classmethod
    def columns(cls) -> list[str]:
        return _col_names(cls)

    def to_row(self) -> tuple:
        return _to_row(self)

    @classmethod
    def from_row(cls, row: tuple) -> Self:
        return _from_row(cls, row)


@dataclass
class OptimizationRunModel:
    """One execution of the self-optimization cycle."""

    __tablename__: ClassVar[str] = "optimization_runs"

    id: int
    run_at: int
    trigger: str
    decisions_analyzed: int
    trades_analyzed: int
    recommendations_count: int
    applied_count: int
    status: str
    started_at: int
    completed_at: int | None
    summary_json: str
    error_message: str

    @classmethod
    def create_table_ddl(cls) -> str:
        return """CREATE TABLE IF NOT EXISTS optimization_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at BIGINT NOT NULL,
    trigger TEXT NOT NULL DEFAULT 'scheduled',
    decisions_analyzed INTEGER NOT NULL DEFAULT 0,
    trades_analyzed INTEGER NOT NULL DEFAULT 0,
    recommendations_count INTEGER NOT NULL DEFAULT 0,
    applied_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',
    started_at BIGINT NOT NULL,
    completed_at BIGINT,
    summary_json TEXT NOT NULL DEFAULT '{}',
    error_message TEXT NOT NULL DEFAULT ''
)"""

    @classmethod
    def columns(cls) -> list[str]:
        return _col_names(cls)

    def to_row(self) -> tuple:
        return _to_row(self)

    @classmethod
    def from_row(cls, row: tuple) -> Self:
        cls_fields = fields(cls)
        field_values = list(row)
        for i, f in enumerate(cls_fields):
            if f.type in ("int | None", "Optional[int]") and field_values[i] is not None:
                try:
                    field_values[i] = int(field_values[i])
                except (TypeError, ValueError):
                    pass
        return cls(*field_values)


@dataclass
class OptimizationRecommendationModel:
    """A single recommendation from an optimization run."""

    __tablename__: ClassVar[str] = "optimization_recommendations"

    id: int
    run_id: int
    recommendation_type: str
    target_area: str
    current_value: str
    recommended_value: str
    rationale: str
    impact_estimate: str
    confidence: str
    status: str
    applied_at: int | None
    applied_strategy_params_json: str

    @classmethod
    def create_table_ddl(cls) -> str:
        return """CREATE TABLE IF NOT EXISTS optimization_recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    recommendation_type TEXT NOT NULL,
    target_area TEXT NOT NULL,
    current_value TEXT NOT NULL DEFAULT '',
    recommended_value TEXT NOT NULL DEFAULT '',
    rationale TEXT NOT NULL DEFAULT '',
    impact_estimate TEXT NOT NULL DEFAULT '',
    confidence TEXT NOT NULL DEFAULT 'medium',
    status TEXT NOT NULL DEFAULT 'pending',
    applied_at BIGINT,
    applied_strategy_params_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (run_id) REFERENCES optimization_runs(id)
)"""

    @classmethod
    def columns(cls) -> list[str]:
        return _col_names(cls)

    def to_row(self) -> tuple:
        return _to_row(self)

    @classmethod
    def from_row(cls, row: tuple) -> Self:
        cls_fields = fields(cls)
        field_values = list(row)
        for i, f in enumerate(cls_fields):
            if f.type in ("int | None", "Optional[int]") and field_values[i] is not None:
                try:
                    field_values[i] = int(field_values[i])
                except (TypeError, ValueError):
                    pass
        return cls(*field_values)


@dataclass
class FundingPaymentModel:
    """Funding payment settlement record."""

    __tablename__: ClassVar[str] = "funding_payments"

    id: int
    symbol: str
    position_id: int
    amount: str  # positive = paid, negative = received
    rate: str  # the funding rate at settlement
    funding_time: int  # unix ms of the funding settlement

    @classmethod
    def create_table_ddl(cls) -> str:
        return """CREATE TABLE IF NOT EXISTS funding_payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    position_id INTEGER REFERENCES positions(id),
    amount TEXT NOT NULL DEFAULT '0',
    rate TEXT NOT NULL DEFAULT '0',
    funding_time BIGINT NOT NULL
)"""

    @classmethod
    def columns(cls) -> list[str]:
        return _col_names(cls)

    def to_row(self) -> tuple:
        return _to_row(self)

    @classmethod
    def from_row(cls, row: tuple) -> Self:
        return _from_row(cls, row)


@dataclass
class LiquidationEventModel:
    """Liquidation event record."""

    __tablename__: ClassVar[str] = "liquidation_events"

    id: int
    symbol: str
    position_id: int
    amount: str  # liquidated quantity
    price: str  # liquidation price
    side: str  # "BUY" or "SELL" (opposite of position side)
    timestamp: int

    @classmethod
    def create_table_ddl(cls) -> str:
        return """CREATE TABLE IF NOT EXISTS liquidation_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    position_id INTEGER REFERENCES positions(id),
    amount TEXT NOT NULL DEFAULT '0',
    price TEXT NOT NULL DEFAULT '0',
    side TEXT NOT NULL DEFAULT '',
    timestamp BIGINT NOT NULL
)"""

    @classmethod
    def columns(cls) -> list[str]:
        return _col_names(cls)

    def to_row(self) -> tuple:
        return _to_row(self)

    @classmethod
    def from_row(cls, row: tuple) -> Self:
        return _from_row(cls, row)


@dataclass
class FundingRateRecordModel:
    """Funding rate historical record."""

    __tablename__: ClassVar[str] = "funding_rate_records"

    id: int
    symbol: str
    rate: str
    time: int
    mark_price: str
    index_price: str

    @classmethod
    def create_table_ddl(cls) -> str:
        return """CREATE TABLE IF NOT EXISTS funding_rate_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    rate TEXT NOT NULL DEFAULT '0',
    time BIGINT NOT NULL,
    mark_price TEXT NOT NULL DEFAULT '0',
    index_price TEXT NOT NULL DEFAULT '0'
)"""

    @classmethod
    def columns(cls) -> list[str]:
        return _col_names(cls)

    def to_row(self) -> tuple:
        return _to_row(self)

    @classmethod
    def from_row(cls, row: tuple) -> Self:
        return _from_row(cls, row)


# ---------------------------------------------------------------------------
# Index DDL definitions
# ---------------------------------------------------------------------------

INDEX_DEFINITIONS: list[str] = [
    "CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status)",
    "CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol)",
    "CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)",
    "CREATE INDEX IF NOT EXISTS idx_orders_position_id ON orders(position_id)",
    "CREATE INDEX IF NOT EXISTS idx_trades_position_id ON trades(position_id)",
    "CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_decisions_strategy ON decisions(strategy)",
    "CREATE INDEX IF NOT EXISTS idx_decisions_timestamp ON decisions(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_perf_snapshots_timestamp ON performance_snapshots(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_opt_runs_status ON optimization_runs(status)",
    "CREATE INDEX IF NOT EXISTS idx_opt_runs_run_at ON optimization_runs(run_at)",
    "CREATE INDEX IF NOT EXISTS idx_opt_recommendations_run_id ON optimization_recommendations(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_opt_recommendations_type ON optimization_recommendations(recommendation_type)",
    "CREATE INDEX IF NOT EXISTS idx_opt_recommendations_status ON optimization_recommendations(status)",
    "CREATE INDEX IF NOT EXISTS idx_funding_payments_symbol ON funding_payments(symbol)",
    "CREATE INDEX IF NOT EXISTS idx_funding_payments_time ON funding_payments(funding_time)",
    "CREATE INDEX IF NOT EXISTS idx_liquidation_events_symbol ON liquidation_events(symbol)",
    "CREATE INDEX IF NOT EXISTS idx_liquidation_events_time ON liquidation_events(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_funding_rate_records_symbol ON funding_rate_records(symbol)",
    "CREATE INDEX IF NOT EXISTS idx_funding_rate_records_time ON funding_rate_records(time)",
]
"""All CREATE INDEX statements for hot-path queries."""

# ---------------------------------------------------------------------------
# Schema version tracking table DDL
# ---------------------------------------------------------------------------

SCHEMA_VERSION_TABLE_DDL: str = """CREATE TABLE IF NOT EXISTS _schema_version (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version INTEGER NOT NULL,
    applied_at TEXT DEFAULT (datetime('now'))
)"""
"""DDL for the schema version tracking table (SQLite syntax)."""


# ---------------------------------------------------------------------------
# Registry of all models for schema creation
# ---------------------------------------------------------------------------

ALL_MODELS: list[type] = [
    AccountModel,
    PositionModel,
    OrderModel,
    TradeModel,
    DecisionModel,
    StrategyStateModel,
    SessionModel,
    PerformanceSnapshotModel,
    CircuitBreakerEventModel,
    ConfigChangeModel,
    ErrorLogModel,
    OptimizationRunModel,
    OptimizationRecommendationModel,
    FundingPaymentModel,
    LiquidationEventModel,
    FundingRateRecordModel,
]
"""All model classes, in dependency-safe order."""
