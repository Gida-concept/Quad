"""Database models for the Quad futures trading bot.

This module defines all 12 table schemas as dataclasses with SQLite DDL
generation and row serialization/deserialization. All Decimal values are stored
as TEXT to preserve precision losslessly. Timestamps are Unix epoch milliseconds
stored as BIGINT.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, fields
from typing import Any, ClassVar, Protocol, runtime_checkable

if sys.version_info >= (3, 11):
    from typing import Self
else:
    from typing_extensions import Self

# ---------------------------------------------------------------------------
# Schema versioning
# ---------------------------------------------------------------------------

SCHEMA_VERSION: int = 10
"""Current schema version. Increment when making breaking changes."""

SCHEMA_MIGRATIONS: dict[int, list[str]] = {
    1: [
        # Version 1 --> 2: Add leverage, margin_type, position_side, liquidation_price,
        # initial_margin, maintenance_margin, funding_paid columns to positions table.
        # These were added to support USDT-perpetual futures position tracking.
        "ALTER TABLE positions ADD COLUMN leverage INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE positions ADD COLUMN margin_type TEXT NOT NULL DEFAULT 'isolated'",
        "ALTER TABLE positions ADD COLUMN position_side TEXT NOT NULL DEFAULT 'LONG'",
        "ALTER TABLE positions ADD COLUMN liquidation_price TEXT NOT NULL DEFAULT '0'",
        "ALTER TABLE positions ADD COLUMN initial_margin TEXT NOT NULL DEFAULT '0'",
        "ALTER TABLE positions ADD COLUMN maintenance_margin TEXT NOT NULL DEFAULT '0'",
        "ALTER TABLE positions ADD COLUMN funding_paid TEXT NOT NULL DEFAULT '0'",
        # Add working_type, position_side, price_protect to orders table
        "ALTER TABLE orders ADD COLUMN working_type TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE orders ADD COLUMN position_side TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE orders ADD COLUMN price_protect INTEGER NOT NULL DEFAULT 0",
    ],
    2: [
        # Version 2 --> 3: Add avg_fill_price to orders, add sessions table,
        # add optimization run tables.
        "ALTER TABLE orders ADD COLUMN avg_fill_price TEXT NOT NULL DEFAULT '0'",
        # Sessions table (idempotent via IF NOT EXISTS)
        (
            "CREATE TABLE IF NOT EXISTS sessions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "start_time BIGINT NOT NULL, "
            "end_time BIGINT, "
            "mode TEXT NOT NULL DEFAULT 'bybit', "
            "state TEXT NOT NULL DEFAULT 'running', "
            "pnl TEXT NOT NULL DEFAULT '0', "
            "trades_count INTEGER NOT NULL DEFAULT 0)"
        ),
        # Optimization runs table
        (
            "CREATE TABLE IF NOT EXISTS optimization_runs ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "run_at BIGINT NOT NULL, "
            "trigger TEXT NOT NULL DEFAULT 'scheduled', "
            "decisions_analyzed INTEGER NOT NULL DEFAULT 0, "
            "trades_analyzed INTEGER NOT NULL DEFAULT 0, "
            "recommendations_count INTEGER NOT NULL DEFAULT 0, "
            "applied_count INTEGER NOT NULL DEFAULT 0, "
            "status TEXT NOT NULL DEFAULT 'running', "
            "started_at BIGINT NOT NULL, "
            "completed_at BIGINT, "
            "summary_json TEXT NOT NULL DEFAULT '{}', "
            "error_message TEXT NOT NULL DEFAULT '')"
        ),
        # Optimization recommendations table
        (
            "CREATE TABLE IF NOT EXISTS optimization_recommendations ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "run_id INTEGER NOT NULL, "
            "recommendation_type TEXT NOT NULL, "
            "target_area TEXT NOT NULL, "
            "current_value TEXT NOT NULL DEFAULT '', "
            "recommended_value TEXT NOT NULL DEFAULT '', "
            "rationale TEXT NOT NULL DEFAULT '', "
            "impact_estimate TEXT NOT NULL DEFAULT '', "
            "confidence TEXT NOT NULL DEFAULT 'medium', "
            "status TEXT NOT NULL DEFAULT 'pending', "
            "applied_at BIGINT, "
            "applied_strategy_params_json TEXT NOT NULL DEFAULT '{}', "
            "FOREIGN KEY (run_id) REFERENCES optimization_runs(id))"
        ),
        # Liquidation events table
        (
            "CREATE TABLE IF NOT EXISTS liquidation_events ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "symbol TEXT NOT NULL, "
            "position_id INTEGER REFERENCES positions(id), "
            "amount TEXT NOT NULL DEFAULT '0', "
            "price TEXT NOT NULL DEFAULT '0', "
            "side TEXT NOT NULL DEFAULT '', "
            "timestamp BIGINT NOT NULL)"
        ),
        # Funding rate records table
        (
            "CREATE TABLE IF NOT EXISTS funding_rate_records ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "symbol TEXT NOT NULL, "
            "rate TEXT NOT NULL DEFAULT '0', "
            "time BIGINT NOT NULL, "
            "mark_price TEXT NOT NULL DEFAULT '0', "
            "index_price TEXT NOT NULL DEFAULT '0')"
        ),
        # Indexes for new tables
        "CREATE INDEX IF NOT EXISTS idx_opt_runs_status ON optimization_runs(status)",
        "CREATE INDEX IF NOT EXISTS idx_opt_runs_run_at ON optimization_runs(run_at)",
        "CREATE INDEX IF NOT EXISTS idx_opt_recommendations_run_id ON optimization_recommendations(run_id)",
        "CREATE INDEX IF NOT EXISTS idx_opt_recommendations_status ON optimization_recommendations(status)",
        "CREATE INDEX IF NOT EXISTS idx_liquidation_events_symbol ON liquidation_events(symbol)",
        "CREATE INDEX IF NOT EXISTS idx_liquidation_events_time ON liquidation_events(timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_funding_rate_records_symbol ON funding_rate_records(symbol)",
        "CREATE INDEX IF NOT EXISTS idx_funding_rate_records_time ON funding_rate_records(time)",
    ],
    # NOTE: Version 3 is intentionally skipped. The migration from v2->v4
    # is safe because the migration loop uses .get(version, []) which
    # returns an empty list for missing versions. This skip was introduced
    # when the Phase-1 inversion-proof upgrade (v4) was added directly
    # after v2, bypassing v3.
    4: [
        # Version 3 --> 4: Phase 1 inversion-proof upgrade.  The LLM now
        # forecasts a DIRECTION and the bot derives the order side
        # deterministically.  Track the predicted direction, confidence,
        # plausibility-gate result, and the position outcome so unresolved
        # ENTER decisions can be reconciled when the position closes.
        "ALTER TABLE decisions ADD COLUMN predicted_direction TEXT NOT NULL DEFAULT 'NEUTRAL'",
        "ALTER TABLE decisions ADD COLUMN confidence REAL NOT NULL DEFAULT 0.0",
        "ALTER TABLE decisions ADD COLUMN gate_result TEXT NOT NULL DEFAULT 'not_checked'",
        "ALTER TABLE decisions ADD COLUMN entry_price TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE decisions ADD COLUMN exit_price TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE decisions ADD COLUMN realized_pnl TEXT NOT NULL DEFAULT '0'",
        "ALTER TABLE decisions ADD COLUMN outcome TEXT NOT NULL DEFAULT 'open'",
        "ALTER TABLE decisions ADD COLUMN resolved_at BIGINT",
    ],
    5: [
        # Version 4 --> 5: multi-tenancy. New tenant tables + tenant_id
        # on every per-user table (market-data funding_rate_records stays
        # global). Fresh installs get tenant_id via base DDL; existing DBs
        # via the ALTERs below (idempotent — migrate() skips existing cols).
        (
            "CREATE TABLE IF NOT EXISTS tenants ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "tenant_uuid TEXT NOT NULL UNIQUE, "
            "telegram_user_id BIGINT UNIQUE, "
            "username TEXT NOT NULL DEFAULT '', "
            "display_name TEXT NOT NULL DEFAULT '', "
            "status TEXT NOT NULL DEFAULT 'active', "
            "created_at BIGINT NOT NULL, "
            "updated_at BIGINT NOT NULL)"
        ),
        (
            "CREATE TABLE IF NOT EXISTS exchange_credentials ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "tenant_uuid TEXT NOT NULL, "
            "exchange TEXT NOT NULL DEFAULT 'bybit', "
            "api_key_enc TEXT NOT NULL DEFAULT '', "
            "api_secret_enc TEXT NOT NULL DEFAULT '', "
            "testnet INTEGER NOT NULL DEFAULT 1, "
            "bybit_uid TEXT NOT NULL DEFAULT '', "
            "permissions TEXT NOT NULL DEFAULT '', "
            "last_verified_at BIGINT, "
            "created_at BIGINT NOT NULL, "
            "updated_at BIGINT NOT NULL)"
        ),
        (
            "CREATE TABLE IF NOT EXISTS tenant_config ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "tenant_uuid TEXT NOT NULL UNIQUE, "
            "market TEXT NOT NULL DEFAULT 'linear', "
            "capital_pct_per_trade REAL NOT NULL DEFAULT 2.0, "
            "leverage INTEGER NOT NULL DEFAULT 5, "
            "take_profit_pct REAL NOT NULL DEFAULT 50.0, "
            "stop_loss_pct REAL NOT NULL DEFAULT 30.0, "
            "strategy TEXT NOT NULL DEFAULT 'trend_following', "
            "max_positions INTEGER NOT NULL DEFAULT 1, "
            "updated_at BIGINT NOT NULL)"
        ),
        (
            "CREATE TABLE IF NOT EXISTS telegram_bindings ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "tenant_uuid TEXT NOT NULL UNIQUE, "
            "chat_id BIGINT NOT NULL UNIQUE, "
            "bound_at BIGINT NOT NULL, "
            "last_seen_at BIGINT)"
        ),
        (
            "CREATE TABLE IF NOT EXISTS pairing_codes ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "tenant_uuid TEXT NOT NULL, "
            "code TEXT NOT NULL UNIQUE, "
            "purpose TEXT NOT NULL DEFAULT 'link', "
            "expires_at BIGINT NOT NULL, "
            "used INTEGER NOT NULL DEFAULT 0, "
            "used_by_chat BIGINT, "
            "created_at BIGINT NOT NULL)"
        ),
        "ALTER TABLE accounts ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'",
        "ALTER TABLE positions ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'",
        "ALTER TABLE orders ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'",
        "ALTER TABLE trades ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'",
        "ALTER TABLE decisions ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'",
        "ALTER TABLE strategy_state ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'",
        "ALTER TABLE sessions ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'",
        "ALTER TABLE performance_snapshots ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'",
        "ALTER TABLE circuit_breaker_events ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'",
        "ALTER TABLE config_changes ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'",
        "ALTER TABLE error_logs ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'",
        "ALTER TABLE optimization_runs ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'",
        "ALTER TABLE optimization_recommendations ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'",
        "ALTER TABLE funding_payments ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'",
        "ALTER TABLE liquidation_events ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_exchange_creds ON exchange_credentials(tenant_id, exchange)",
        "CREATE INDEX IF NOT EXISTS idx_positions_tenant ON positions(tenant_id)",
        "CREATE INDEX IF NOT EXISTS idx_orders_tenant ON orders(tenant_id)",
        "CREATE INDEX IF NOT EXISTS idx_trades_tenant ON trades(tenant_id)",
        "CREATE INDEX IF NOT EXISTS idx_decisions_tenant ON decisions(tenant_id)",
        "CREATE INDEX IF NOT EXISTS idx_pairing_codes_code ON pairing_codes(code)",
        "CREATE INDEX IF NOT EXISTS idx_bindings_chat ON telegram_bindings(chat_id)",
    ],
    6: [
        # Version 5 --> 6: AI cost tiers. Per-tenant judge tier + daily
        # judge-call budget (Phase 5 cost controls).
        "ALTER TABLE tenant_config ADD COLUMN ai_tier TEXT NOT NULL DEFAULT 'cheap'",
        "ALTER TABLE tenant_config ADD COLUMN ai_max_calls_per_day INTEGER NOT NULL DEFAULT 24",
    ],
    7: [
        # Version 6 --> 7: strategy mode (trend/scalp/both) + scalp profile
        # (tight TP/SL bands, own judge budget for AI-judged scalping).
        "ALTER TABLE tenant_config ADD COLUMN strategy_mode TEXT NOT NULL DEFAULT 'trend'",
        "ALTER TABLE tenant_config ADD COLUMN scalp_tp_pct REAL NOT NULL DEFAULT 15.0",
        "ALTER TABLE tenant_config ADD COLUMN scalp_sl_pct REAL NOT NULL DEFAULT 8.0",
        "ALTER TABLE tenant_config ADD COLUMN scalp_max_calls_per_day INTEGER NOT NULL DEFAULT 120",
    ],
    8: [
        # Version 7 --> 8: server-owned symbols. Backfill pre-canonical
        # 5x rows to the Balanced default (10x), then drop the
        # user-symbols column. NOTE: a tenant that deliberately chose 5x
        # moves to 10x here — announced in the changelog; re-tune via PUT.
        "UPDATE tenant_config SET leverage = 10 WHERE leverage = 5",
        "ALTER TABLE tenant_config DROP COLUMN symbols_json",
    ],
    9: [
        # Version 8 --> 9: scope strategy_state by tenant. New installs get
        # UNIQUE(strategy_name, tenant_id) from DDL; existing DBs keep the
        # legacy global UNIQUE(strategy_name) (SQLite cannot drop it without
        # a rebuild) but gain the composite index. The repository upsert
        # targets (strategy_name, tenant_id) atomically so tenants can no
        # longer clobber each other.
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_strategy_state_name_tenant "
        "ON strategy_state(strategy_name, tenant_id)",
    ],
    10: [
        # Version 9 --> 10: JWT revocation. token_version on tenants backs
        # POST /v1/auth/revoke; tokens with an older ver claim are rejected.
        "ALTER TABLE tenants ADD COLUMN token_version INTEGER NOT NULL DEFAULT 1",
    ],
}
"""Mapping of version numbers to lists of SQL migration statements.

Schema version history:
- Version 0 (initial): accounts, positions (basic), orders, trades, decisions,
  strategy_state, performance_snapshots, circuit_breaker_events, config_changes,
  error_logs tables.
- Version 1 --> 2: Added futures-specific columns to positions (leverage, margin_type,
  position_side, liquidation_price, initial_margin, maintenance_margin, funding_paid)
  and orders (working_type, position_side, price_protect).
- Version 2 --> 3: Added avg_fill_price to orders.  Added sessions, optimization_runs,
  optimization_recommendations, liquidation_events, and funding_rate_records tables
  with associated indexes.
- Version 3 --> 4: Added Phase-1 decision-outcome columns to decisions
  (predicted_direction, confidence, gate_result, entry_price, exit_price,
  realized_pnl, outcome, resolved_at) for the inversion-proof direction ->
  side validation upgrade.
- Version 4 --> 5: Multi-tenancy (quad-api). New tenants,
  exchange_credentials (Fernet-encrypted Bybit keys), tenant_config
  (market/capital-pct/leverage/TP/SL/strategy/symbols), telegram_bindings
  (chat_id <-> tenant), pairing_codes tables; tenant_id on all per-user
  tables (funding_rate_records stays global market data).
"""


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
    tenant_id: str = "default"

    @classmethod
    def create_table_ddl(cls) -> str:
        return """CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange TEXT NOT NULL,
    balances_json TEXT NOT NULL DEFAULT '{}',
    total_usdt TEXT NOT NULL DEFAULT '0',
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT 'default'
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
    tenant_id: str = "default"

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
    updated_at BIGINT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT 'default'
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
    tenant_id: str = "default"

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
    tenant_id TEXT NOT NULL DEFAULT 'default',
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
    tenant_id: str = "default"

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
    tenant_id TEXT NOT NULL DEFAULT 'default',
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
class DecisionModel:
    """Strategy decision record.

    Phase 1 fields: the LLM forecasts ``predicted_direction`` (LONG / SHORT /
    NEUTRAL) and the bot derives the order side deterministically.  ``outcome``
    tracks whether an executed ENTER resolved to a win / loss / flat (closed
    without PnL), defaulting to ``"open"`` until the position closes.
    """

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
    predicted_direction: str = "NEUTRAL"
    confidence: float = 0.0
    gate_result: str = "not_checked"
    entry_price: str = ""
    exit_price: str = ""
    realized_pnl: str = "0"
    outcome: str = "open"
    resolved_at: int | None = None
    tenant_id: str = "default"

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
    cycle_time_ms INTEGER NOT NULL DEFAULT 0,
    predicted_direction TEXT NOT NULL DEFAULT 'NEUTRAL',
    confidence REAL NOT NULL DEFAULT 0.0,
    gate_result TEXT NOT NULL DEFAULT 'not_checked',
    entry_price TEXT NOT NULL DEFAULT '',
    exit_price TEXT NOT NULL DEFAULT '',
    realized_pnl TEXT NOT NULL DEFAULT '0',
    outcome TEXT NOT NULL DEFAULT 'open',
    resolved_at BIGINT,
    tenant_id TEXT NOT NULL DEFAULT 'default'
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
    tenant_id: str = "default"

    @classmethod
    def create_table_ddl(cls) -> str:
        return """CREATE TABLE IF NOT EXISTS strategy_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    params_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'idle',
    tenant_id TEXT NOT NULL DEFAULT 'default',
    UNIQUE(strategy_name, tenant_id)
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
    tenant_id: str = "default"

    @classmethod
    def create_table_ddl(cls) -> str:
        return """CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_time BIGINT NOT NULL,
    end_time BIGINT,
    mode TEXT NOT NULL DEFAULT 'bybit',
    state TEXT NOT NULL DEFAULT 'running',
    pnl TEXT NOT NULL DEFAULT '0',
    trades_count INTEGER NOT NULL DEFAULT 0,
    tenant_id TEXT NOT NULL DEFAULT 'default'
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
            if (
                f.type in ("int | None", "Optional[int]")
                and field_values[i] is not None
            ):
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
    tenant_id: str = "default"

    @classmethod
    def create_table_ddl(cls) -> str:
        return """CREATE TABLE IF NOT EXISTS performance_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp BIGINT NOT NULL,
    portfolio_value TEXT NOT NULL DEFAULT '0',
    drawdown TEXT NOT NULL DEFAULT '0',
    positions_count INTEGER NOT NULL DEFAULT 0,
    daily_pnl TEXT NOT NULL DEFAULT '0',
    tenant_id TEXT NOT NULL DEFAULT 'default'
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
    tenant_id: str = "default"

    @classmethod
    def create_table_ddl(cls) -> str:
        return """CREATE TABLE IF NOT EXISTS circuit_breaker_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp BIGINT NOT NULL,
    breaker_name TEXT NOT NULL,
    tier INTEGER NOT NULL DEFAULT 1,
    reason TEXT NOT NULL DEFAULT '',
    resolved_at BIGINT,
    tenant_id TEXT NOT NULL DEFAULT 'default'
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
            if (
                f.type in ("int | None", "Optional[int]")
                and field_values[i] is not None
            ):
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
    tenant_id: str = "default"

    @classmethod
    def create_table_ddl(cls) -> str:
        return """CREATE TABLE IF NOT EXISTS config_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp BIGINT NOT NULL,
    key TEXT NOT NULL,
    old_value TEXT NOT NULL DEFAULT '',
    new_value TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    tenant_id TEXT NOT NULL DEFAULT 'default'
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
    tenant_id: str = "default"

    @classmethod
    def create_table_ddl(cls) -> str:
        return """CREATE TABLE IF NOT EXISTS error_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp BIGINT NOT NULL,
    level TEXT NOT NULL DEFAULT 'ERROR',
    event TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}',
    tenant_id TEXT NOT NULL DEFAULT 'default'
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
    tenant_id: str = "default"

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
    error_message TEXT NOT NULL DEFAULT '',
    tenant_id TEXT NOT NULL DEFAULT 'default'
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
            if (
                f.type in ("int | None", "Optional[int]")
                and field_values[i] is not None
            ):
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
    tenant_id: str = "default"

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
    tenant_id TEXT NOT NULL DEFAULT 'default',
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
            if (
                f.type in ("int | None", "Optional[int]")
                and field_values[i] is not None
            ):
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
    tenant_id: str = "default"

    @classmethod
    def create_table_ddl(cls) -> str:
        return """CREATE TABLE IF NOT EXISTS funding_payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    position_id INTEGER REFERENCES positions(id),
    amount TEXT NOT NULL DEFAULT '0',
    rate TEXT NOT NULL DEFAULT '0',
    funding_time BIGINT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT 'default'
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
    tenant_id: str = "default"

    @classmethod
    def create_table_ddl(cls) -> str:
        return """CREATE TABLE IF NOT EXISTS liquidation_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    position_id INTEGER REFERENCES positions(id),
    amount TEXT NOT NULL DEFAULT '0',
    price TEXT NOT NULL DEFAULT '0',
    side TEXT NOT NULL DEFAULT '',
    timestamp BIGINT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT 'default'
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
class TenantModel:
    """A quad-api tenant (one human user, one Bybit account)."""

    __tablename__: ClassVar[str] = "tenants"

    id: int
    tenant_uuid: str
    telegram_user_id: int | None
    username: str = ""
    display_name: str = ""
    status: str = "active"
    token_version: int = 1
    created_at: int = 0
    updated_at: int = 0

    @classmethod
    def create_table_ddl(cls) -> str:
        return """CREATE TABLE IF NOT EXISTS tenants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_uuid TEXT NOT NULL UNIQUE,
    telegram_user_id BIGINT UNIQUE,
    username TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    token_version INTEGER NOT NULL DEFAULT 1,
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
        cls_fields = fields(cls)
        field_values = list(row)
        for i, f in enumerate(cls_fields):
            if (
                f.type in ("int | None", "Optional[int]")
                and field_values[i] is not None
            ):
                try:
                    field_values[i] = int(field_values[i])
                except (TypeError, ValueError):
                    pass
        return cls(*field_values)


@dataclass
class ExchangeCredentialModel:
    """Encrypted exchange API credentials for one tenant.

    ``api_key_enc`` / ``api_secret_enc`` hold Fernet tokens produced by
    :mod:`quad.security.secrets` — never plaintext.
    """

    __tablename__: ClassVar[str] = "exchange_credentials"

    id: int
    tenant_id: str
    exchange: str = "bybit"
    api_key_enc: str = ""
    api_secret_enc: str = ""
    testnet: int = 1
    bybit_uid: str = ""
    permissions: str = ""
    last_verified_at: int | None = None
    created_at: int = 0
    updated_at: int = 0

    @classmethod
    def create_table_ddl(cls) -> str:
        return """CREATE TABLE IF NOT EXISTS exchange_credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    exchange TEXT NOT NULL DEFAULT 'bybit',
    api_key_enc TEXT NOT NULL DEFAULT '',
    api_secret_enc TEXT NOT NULL DEFAULT '',
    testnet INTEGER NOT NULL DEFAULT 1,
    bybit_uid TEXT NOT NULL DEFAULT '',
    permissions TEXT NOT NULL DEFAULT '',
    last_verified_at BIGINT,
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
        cls_fields = fields(cls)
        field_values = list(row)
        for i, f in enumerate(cls_fields):
            if (
                f.type in ("int | None", "Optional[int]")
                and field_values[i] is not None
            ):
                try:
                    field_values[i] = int(field_values[i])
                except (TypeError, ValueError):
                    pass
        return cls(*field_values)


@dataclass
class TenantConfigModel:
    """Per-tenant trading configuration (Phase 1: linear-only)."""

    __tablename__: ClassVar[str] = "tenant_config"

    id: int
    tenant_uuid: str
    market: str = "linear"
    capital_pct_per_trade: float = 2.0
    leverage: int = 10
    take_profit_pct: float = 50.0
    stop_loss_pct: float = 30.0
    strategy: str = "trend_following"
    max_positions: int = 1
    ai_tier: str = "cheap"
    ai_max_calls_per_day: int = 24
    strategy_mode: str = "trend"
    scalp_tp_pct: float = 15.0
    scalp_sl_pct: float = 8.0
    scalp_max_calls_per_day: int = 120
    updated_at: int = 0

    @classmethod
    def create_table_ddl(cls) -> str:
        return """CREATE TABLE IF NOT EXISTS tenant_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_uuid TEXT NOT NULL UNIQUE,
    market TEXT NOT NULL DEFAULT 'linear',
    capital_pct_per_trade REAL NOT NULL DEFAULT 2.0,
    leverage INTEGER NOT NULL DEFAULT 10,
    take_profit_pct REAL NOT NULL DEFAULT 50.0,
    stop_loss_pct REAL NOT NULL DEFAULT 30.0,
    strategy TEXT NOT NULL DEFAULT 'trend_following',
    max_positions INTEGER NOT NULL DEFAULT 1,
    ai_tier TEXT NOT NULL DEFAULT 'cheap',
    ai_max_calls_per_day INTEGER NOT NULL DEFAULT 24,
    strategy_mode TEXT NOT NULL DEFAULT 'trend',
    scalp_tp_pct REAL NOT NULL DEFAULT 15.0,
    scalp_sl_pct REAL NOT NULL DEFAULT 8.0,
    scalp_max_calls_per_day INTEGER NOT NULL DEFAULT 120,
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
class TelegramBindingModel:
    """Binding between a Telegram chat and a tenant (pairing required)."""

    __tablename__: ClassVar[str] = "telegram_bindings"

    id: int
    tenant_uuid: str
    chat_id: int
    bound_at: int = 0
    last_seen_at: int | None = None

    @classmethod
    def create_table_ddl(cls) -> str:
        return """CREATE TABLE IF NOT EXISTS telegram_bindings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_uuid TEXT NOT NULL UNIQUE,
    chat_id BIGINT NOT NULL UNIQUE,
    bound_at BIGINT NOT NULL,
    last_seen_at BIGINT
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
            if (
                f.type in ("int | None", "Optional[int]")
                and field_values[i] is not None
            ):
                try:
                    field_values[i] = int(field_values[i])
                except (TypeError, ValueError):
                    pass
        return cls(*field_values)


@dataclass
class PairingCodeModel:
    """Single-use pairing code linking an extra chat to a tenant."""

    __tablename__: ClassVar[str] = "pairing_codes"

    id: int
    tenant_uuid: str
    code: str
    purpose: str = "link"
    expires_at: int = 0
    used: int = 0
    used_by_chat: int | None = None
    created_at: int = 0

    @classmethod
    def create_table_ddl(cls) -> str:
        return """CREATE TABLE IF NOT EXISTS pairing_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_uuid TEXT NOT NULL,
    code TEXT NOT NULL UNIQUE,
    purpose TEXT NOT NULL DEFAULT 'link',
    expires_at BIGINT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    used_by_chat BIGINT,
    created_at BIGINT NOT NULL
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
            if (
                f.type in ("int | None", "Optional[int]")
                and field_values[i] is not None
            ):
                try:
                    field_values[i] = int(field_values[i])
                except (TypeError, ValueError):
                    pass
        return cls(*field_values)


# ---------------------------------------------------------------------------
# Index DDL definitions
# ---------------------------------------------------------------------------

INDEX_DEFINITIONS: list[str] = [
    "CREATE INDEX IF NOT EXISTS idx_tenants_status ON tenants(status)",
    "CREATE INDEX IF NOT EXISTS idx_tenants_tg_user ON tenants(telegram_user_id)",
    "CREATE INDEX IF NOT EXISTS idx_creds_tenant ON exchange_credentials(tenant_id)",
    "CREATE INDEX IF NOT EXISTS idx_bindings_chat ON telegram_bindings(chat_id)",
    "CREATE INDEX IF NOT EXISTS idx_pairing_codes_code ON pairing_codes(code)",
    "CREATE INDEX IF NOT EXISTS idx_positions_tenant ON positions(tenant_id)",
    "CREATE INDEX IF NOT EXISTS idx_orders_tenant ON orders(tenant_id)",
    "CREATE INDEX IF NOT EXISTS idx_trades_tenant ON trades(tenant_id)",
    "CREATE INDEX IF NOT EXISTS idx_decisions_tenant ON decisions(tenant_id)",
    "CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status)",
    "CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol)",
    "CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)",
    "CREATE INDEX IF NOT EXISTS idx_orders_position_id ON orders(position_id)",
    "CREATE INDEX IF NOT EXISTS idx_trades_position_id ON trades(position_id)",
    "CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_decisions_strategy ON decisions(strategy)",
    "CREATE INDEX IF NOT EXISTS idx_decisions_timestamp ON decisions(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_decisions_outcome ON decisions(outcome)",
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


@runtime_checkable
class ModelSurface(Protocol):
    """Structural type shared by every model class in :data:`ALL_MODELS`.

    The models are independent dataclasses with no common base class, so
    ``ALL_MODELS`` used to be typed ``list[type]`` -- which erased every
    attribute and forced each consumer (schema bootstrap in ``pg.py`` /
    ``database.py``, and ``BaseRepository``) to reach for ``Any``.  Declaring
    the surface they actually rely on keeps those call sites type-safe.
    """

    __tablename__: ClassVar[str]

    @classmethod
    def create_table_ddl(cls) -> str:
        """Return the ``CREATE TABLE`` statement for this model."""

    @classmethod
    def columns(cls) -> list[str]:
        """Return the model's column names, in declaration order."""


ALL_MODELS: list[type[ModelSurface]] = [
    TenantModel,
    ExchangeCredentialModel,
    TenantConfigModel,
    TelegramBindingModel,
    PairingCodeModel,
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
]
"""All model classes, in dependency-safe order."""
