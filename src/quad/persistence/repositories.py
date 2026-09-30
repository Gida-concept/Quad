"""Repository classes for the Quad futures trading bot.

Provides a generic ``BaseRepository[T]`` with CRUD operations and
domain-specific repositories for each entity type. Uses asyncpg with
SQLite ``?`` parameter style (via automatic $N to ? conversion).
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Generic, Protocol, TypeVar, cast

import structlog

from .models import (
    AccountModel,
    CircuitBreakerEventModel,
    ConfigChangeModel,
    DecisionModel,
    ErrorLogModel,
    ExchangeCredentialModel,
    FundingPaymentModel,
    LiquidationEventModel,
    OptimizationRecommendationModel,
    OptimizationRunModel,
    OrderModel,
    PairingCodeModel,
    PerformanceSnapshotModel,
    PositionModel,
    SessionModel,
    StrategyStateModel,
    TelegramBindingModel,
    TenantConfigModel,
    TenantModel,
    TradeModel,
)

logger = structlog.get_logger(__name__)

T = TypeVar("T")


class _DbManager(Protocol):
    """Structural DB-manager type (SQLite or Postgres).

    Typing-only (no runtime import of either backend) so repositories stay
    importable without a persistence <-> pg circular import.
    """

    @property
    def pool(self) -> Any: ...


# ---------------------------------------------------------------------------
# Base repository (generic CRUD)
# ---------------------------------------------------------------------------


class BaseRepository(Generic[T]):
    """Generic repository providing CRUD operations (SQLite).

    Parameters
    ----------
    db_manager:
        The ``DatabaseManager`` instance to use.
    model_cls:
        The model dataclass class (must have ``__tablename__``, ``columns``,
        ``to_row``, and ``from_row``).
    """

    def __init__(
        self,
        db_manager: _DbManager,
        model_cls: type[T] | None = None,
        slow_query_threshold_ms: int = 500,
        tenant_id: str | None = None,
    ) -> None:
        self._db = db_manager
        assert model_cls is not None, "BaseRepository requires a model class"
        self._model_cls = cast(Any, model_cls)
        self._table = self._model_cls.__tablename__
        self._columns = self._model_cls.columns()
        self._log = logger.bind(table=self._table)
        self._slow_query_threshold_ms = slow_query_threshold_ms
        # Multi-tenant scope (quad-api workers).  When set, writes are
        # stamped, reads auto-filter, and update/delete/get are guarded to
        # this tenant.  Tables without a tenant_id column are unaffected.
        self._tenant_id: str | None = tenant_id
        self._tenant_scoped = tenant_id is not None and "tenant_id" in self._columns

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _placeholder_clause(self, names: list[str], start: int = 1) -> str:
        """Return a SET clause with positional placeholders.

        Example: ``"col1 = $1, col2 = $2"``
        """
        parts = []
        for i, n in enumerate(names):
            parts.append(f"{n} = ${start + i}")
        return ", ".join(parts)

    def _where_clause(self, names: list[str], start: int = 1) -> str:
        """Return a WHERE clause with positional placeholders.

        Example: ``"col1 = $1 AND col2 = $2"``
        """
        parts = []
        for i, n in enumerate(names):
            parts.append(f"{n} = ${start + i}")
        return " AND ".join(parts)

    def _column_list(self) -> str:
        return ", ".join(self._columns)

    def _tenant_pred(self, n_params: int, prefix_and: bool = True) -> tuple[str, list]:
        """Tenant predicate fragment for raw-SQL custom methods.

        Returns ``("", [])`` when this repo is unscoped.  *n_params* is the
        number of ``$N`` placeholders already used; the tenant placeholder
        continues the numbering.  With ``prefix_and=False`` the fragment is
        a standalone ``WHERE`` clause (for queries with no WHERE yet).
        """
        if not self._tenant_scoped or self._tenant_id is None:
            return "", []
        frag = f"tenant_id = ${n_params + 1}"
        return (f" AND {frag}" if prefix_and else f" WHERE {frag}", [self._tenant_id])

    def _from_row(self, row: Any) -> T:
        """Build a model instance from a database row (cast for typing)."""
        return cast(T, self._model_cls.from_row(row))

    def _param_placeholders(self, start: int = 1) -> str:
        """Return a comma-separated list of positional placeholders."""
        return ", ".join(f"${start + i}" for i in range(len(self._columns)))

    @staticmethod
    def _column_set_pairs(names: list[str]) -> str:
        """Return column = EXCLUDED.column pairs for ON CONFLICT DO UPDATE SET."""
        return ", ".join(f"{n} = EXCLUDED.{n}" for n in names)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def get(self, id: int) -> T | None:
        """Retrieve a single row by primary key."""
        t0 = time.monotonic()
        try:
            async with self._db.pool.acquire() as conn:
                row = await conn.fetchrow(
                    f"SELECT {self._column_list()} FROM {self._table} WHERE id = $1",
                    id,
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > self._slow_query_threshold_ms:
                self._log.warning("slow_query", ms=round(dur), method="get", id=id)
            if row is None:
                return None
            model = self._from_row(row)
            if (
                self._tenant_scoped
                and getattr(model, "tenant_id", None) != self._tenant_id
            ):
                return None
            return model
        except Exception:
            self._log.exception("get_failed", id=id)
            raise

    async def list(self, **filters: Any) -> list[T]:
        """Return all rows, optionally filtered by keyword arguments.

        Example: ``repo.list(status="OPEN")``
        """
        t0 = time.monotonic()
        try:
            async with self._db.pool.acquire() as conn:
                scoped = dict(filters)
                if self._tenant_scoped and "tenant_id" not in scoped:
                    scoped["tenant_id"] = self._tenant_id
                if scoped:
                    keys = list(scoped.keys())
                    where = self._where_clause(keys)
                    sql = (
                        f"SELECT {self._column_list()} FROM {self._table} WHERE {where}"
                    )
                    rows = await conn.fetch(sql, *scoped.values())
                else:
                    sql = f"SELECT {self._column_list()} FROM {self._table}"
                    rows = await conn.fetch(sql)

            dur = (time.monotonic() - t0) * 1000
            if dur > self._slow_query_threshold_ms:
                self._log.warning("slow_query", ms=round(dur), method="list")
            return [self._from_row(r) for r in rows]
        except Exception:
            self._log.exception("list_failed")
            raise

    async def create(self, model: T) -> int:
        """Insert a new row and return the generated id (via RETURNING)."""
        if (
            self._tenant_scoped
            and self._tenant_id is not None
            and hasattr(model, "tenant_id")
        ):
            try:
                model.tenant_id = self._tenant_id
            except Exception:
                pass
        t0 = time.monotonic()
        try:
            async with self._db.pool.acquire() as conn:
                last_id = await conn.fetchval(
                    f"INSERT INTO {self._table} ({self._column_list()}) "
                    f"VALUES ({self._param_placeholders()}) "
                    f"RETURNING id",
                    *model.to_row(),  # type: ignore[attr-defined]
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > self._slow_query_threshold_ms:
                self._log.warning("slow_query", ms=round(dur), method="create")
            self._log.info("row_created", id=last_id)
            return last_id
        except Exception:
            self._log.exception("create_failed")
            raise

    async def update(self, id: int, **updates: Any) -> None:
        """Update columns for the row identified by *id*."""
        if not updates:
            self._log.warning("update_no_fields", id=id)
            return

        t0 = time.monotonic()
        try:
            keys = list(updates.keys())
            # $N placeholders: values first, then id, then tenant guard.
            set_clause = self._placeholder_clause(keys, start=1)
            values = list(updates.values())
            id_placeholder = f"${len(values) + 1}"
            params: list = list(values) + [id]
            where = f"id = {id_placeholder}"
            if self._tenant_scoped:
                tenant_placeholder = f"${len(values) + 2}"
                where += f" AND tenant_id = {tenant_placeholder}"
                params.append(self._tenant_id)
            async with self._db.pool.acquire() as conn:
                await conn.execute(
                    f"UPDATE {self._table} SET {set_clause} WHERE {where}",
                    *params,
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > self._slow_query_threshold_ms:
                self._log.warning("slow_query", ms=round(dur), method="update", id=id)
            self._log.info("row_updated", id=id, fields=list(updates.keys()))
        except Exception:
            self._log.exception("update_failed", id=id)
            raise

    async def delete(self, id: int) -> None:
        """Delete the row identified by *id*."""
        t0 = time.monotonic()
        try:
            async with self._db.pool.acquire() as conn:
                if self._tenant_scoped:
                    await conn.execute(
                        f"DELETE FROM {self._table} WHERE id = $1 AND tenant_id = $2",
                        id,
                        self._tenant_id,
                    )
                else:
                    await conn.execute(
                        f"DELETE FROM {self._table} WHERE id = $1",
                        id,
                    )
            dur = (time.monotonic() - t0) * 1000
            if dur > self._slow_query_threshold_ms:
                self._log.warning("slow_query", ms=round(dur), method="delete", id=id)
            self._log.info("row_deleted", id=id)
        except Exception:
            self._log.exception("delete_failed", id=id)
            raise

    async def count(self, **filters: Any) -> int:
        """Return the number of rows, optionally filtered."""
        t0 = time.monotonic()
        try:
            async with self._db.pool.acquire() as conn:
                scoped = dict(filters)
                if self._tenant_scoped and "tenant_id" not in scoped:
                    scoped["tenant_id"] = self._tenant_id
                if scoped:
                    keys = list(scoped.keys())
                    where = self._where_clause(keys)
                    row = await conn.fetchval(
                        f"SELECT COUNT(*) FROM {self._table} WHERE {where}",
                        *scoped.values(),
                    )
                else:
                    row = await conn.fetchval(f"SELECT COUNT(*) FROM {self._table}")
            dur = (time.monotonic() - t0) * 1000
            if dur > self._slow_query_threshold_ms:
                self._log.warning("slow_query", ms=round(dur), method="count")
            return row if row is not None else 0
        except Exception:
            self._log.exception("count_failed")
            raise


def make_repo(repo_cls: type[T], db_manager: _DbManager, config: dict | None) -> T:
    """Construct a repository, applying the config's tenant scope if present.

    Workers set ``config["_tenant_id"]``; single-tenant runs leave it unset
    and repositories behave exactly as before.
    """
    tenant_id = (config or {}).get("_tenant_id")
    return repo_cls(db_manager, tenant_id=tenant_id)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Domain-specific repositories
# ---------------------------------------------------------------------------


class AccountRepository(BaseRepository[AccountModel]):
    """Repository for trading account records."""

    def __init__(
        self,
        db_manager: _DbManager,
        model_cls: type[AccountModel] | None = None,
        tenant_id: str | None = None,
    ) -> None:
        super().__init__(db_manager, model_cls or AccountModel, tenant_id=tenant_id)

    async def get_by_exchange(self, exchange: str) -> AccountModel | None:
        """Return the account for a given exchange name.

        Uses a direct query with ``fetchrow`` since exchange is expected to be
        unique in practice (even if the schema does not enforce a UNIQUE constraint).
        """
        t0 = time.monotonic()
        try:
            async with self._db.pool.acquire() as conn:
                pred_sql, pred_params = self._tenant_pred(1)
                row = await conn.fetchrow(
                    f"SELECT {self._column_list()} FROM {self._table} WHERE exchange = $1{pred_sql}",
                    exchange,
                    *pred_params,
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > self._slow_query_threshold_ms:
                self._log.warning("slow_query", ms=round(dur), method="get_by_exchange")
            if row is None:
                return None
            return AccountModel.from_row(row)
        except Exception:
            self._log.exception("get_by_exchange_failed")
            raise

    async def update_balance(
        self,
        account_id: int,
        balances_json: str,
        total_usdt: str,
    ) -> None:
        """Update balance data for an account."""
        await self.update(
            account_id,
            balances_json=balances_json,
            total_usdt=total_usdt,
        )

    async def upsert_account(self, account: AccountModel) -> int:
        """Insert or update an account record (by primary key id).

        Uses ``INSERT ... ON CONFLICT DO UPDATE`` (SQLite compatible).

        Returns the row id.
        """
        t0 = time.monotonic()
        try:
            if (
                self._tenant_scoped
                and self._tenant_id is not None
                and hasattr(account, "tenant_id")
            ):
                try:
                    account.tenant_id = self._tenant_id
                except Exception:
                    pass
            columns = self._column_list()
            placeholders = self._param_placeholders()
            set_pairs = self._column_set_pairs(self._columns)
            async with self._db.pool.acquire() as conn:
                last_id = await conn.fetchval(
                    f"INSERT INTO {self._table} ({columns}) "
                    f"VALUES ({placeholders}) "
                    f"ON CONFLICT (id) DO UPDATE SET {set_pairs} "
                    f"RETURNING id",
                    *account.to_row(),
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > self._slow_query_threshold_ms:
                self._log.warning("slow_query", ms=round(dur), method="upsert_account")
            return last_id
        except Exception:
            self._log.exception("upsert_account_failed")
            raise


class PositionRepository(BaseRepository[PositionModel]):
    """Repository for trading positions."""

    def __init__(
        self,
        db_manager: _DbManager,
        model_cls: type[PositionModel] | None = None,
        tenant_id: str | None = None,
    ) -> None:
        super().__init__(db_manager, model_cls or PositionModel, tenant_id=tenant_id)

    async def get_open(self) -> list[PositionModel]:
        """Return all positions with status ``'OPEN'``."""
        return await self.list(status="OPEN")

    async def get_by_strategy(self, strategy: str) -> list[PositionModel]:
        """Return positions opened by a specific strategy."""
        return await self.list(strategy=strategy)

    async def get_by_symbol(self, symbol: str) -> list[PositionModel]:
        """Return positions for a given futures symbol."""
        return await self.list(symbol=symbol)

    async def get_open_futures_positions(
        self, symbol: str, position_side: str | None = None
    ) -> list[PositionModel]:
        """Return open futures positions, optionally filtered by side."""
        t0 = time.monotonic()
        try:
            async with self._db.pool.acquire() as conn:
                if position_side:
                    rows = await conn.fetch(
                        f"SELECT {self._column_list()} FROM {self._table} "
                        f"WHERE status = 'OPEN' AND symbol = $1 AND position_side = $2{self._tenant_pred(2)[0]}",
                        symbol,
                        position_side,
                        *self._tenant_pred(2)[1],
                    )
                else:
                    rows = await conn.fetch(
                        f"SELECT {self._column_list()} FROM {self._table} "
                        f"WHERE status = 'OPEN' AND symbol = $1{self._tenant_pred(1)[0]}",
                        symbol,
                        *self._tenant_pred(1)[1],
                    )
            dur = (time.monotonic() - t0) * 1000
            if dur > self._slow_query_threshold_ms:
                self._log.warning(
                    "slow_query", ms=round(dur), method="get_open_futures_positions"
                )
            return [PositionModel.from_row(r) for r in rows]
        except Exception:
            self._log.exception("get_open_futures_positions_failed")
            raise

    async def get_liquidation_risk_positions(
        self, distance_threshold_pct: float
    ) -> list[PositionModel]:
        """Return positions where distance to liquidation is near threshold.

        This is a client-side filter since liquidation_price is stored as text.
        Returns positions where both liquidation_price and current_price are non-zero.
        """
        t0 = time.monotonic()
        try:
            async with self._db.pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT {self._column_list()} FROM {self._table} "
                    f"WHERE status = 'OPEN' AND liquidation_price != '0' AND current_price != '0'{self._tenant_pred(0)[0]}",
                    *self._tenant_pred(0)[1],
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > self._slow_query_threshold_ms:
                self._log.warning(
                    "slow_query", ms=round(dur), method="get_liquidation_risk_positions"
                )
            results: list[PositionModel] = []
            for r in rows:
                pos = PositionModel.from_row(r)
                liq = float(pos.liquidation_price)
                cur = float(pos.current_price)
                if liq > 0 and cur > 0:
                    if pos.position_side.upper() == "LONG":
                        dist = abs(cur - liq) / cur
                    else:
                        dist = abs(liq - cur) / cur
                    if dist < distance_threshold_pct / 100.0:
                        results.append(pos)
            return results
        except Exception:
            self._log.exception("get_liquidation_risk_positions_failed")
            raise

    async def close(self, position_id: int, pnl: str) -> None:
        """Mark a position as CLOSED and record final realised PnL."""
        await self.update(
            position_id,
            status="CLOSED",
            realized_pnl=pnl,
        )

    async def get_open_count(self) -> int:
        """Return the number of currently open positions."""
        return await self.count(status="OPEN")


class OrderRepository(BaseRepository[OrderModel]):
    """Repository for orders."""

    def __init__(
        self,
        db_manager: _DbManager,
        model_cls: type[OrderModel] | None = None,
        tenant_id: str | None = None,
    ) -> None:
        super().__init__(db_manager, model_cls or OrderModel, tenant_id=tenant_id)

    async def get_open(self) -> list[OrderModel]:
        """Return orders that are still active (NEW or PARTIALLY_FILLED)."""
        t0 = time.monotonic()
        try:
            async with self._db.pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT {self._column_list()} FROM {self._table} "
                    f"WHERE status IN ('NEW', 'PARTIALLY_FILLED'){self._tenant_pred(0)[0]}",
                    *self._tenant_pred(0)[1],
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > self._slow_query_threshold_ms:
                self._log.warning("slow_query", ms=round(dur), method="get_open")
            return [OrderModel.from_row(r) for r in rows]
        except Exception:
            self._log.exception("get_open_orders_failed")
            raise

    async def get_by_position(self, position_id: int) -> list[OrderModel]:
        """Return all orders for a given position."""
        return await self.list(position_id=position_id)

    async def update_status(
        self,
        order_id: int,
        status: str,
        filled_qty: str | None = None,
        avg_price: str | None = None,
    ) -> None:
        """Update an order's status and optionally fill details."""
        updates: dict[str, Any] = {"status": status}
        if filled_qty is not None:
            updates["filled_qty"] = filled_qty
        if avg_price is not None:
            updates["price"] = avg_price
        await self.update(order_id, **updates)

    async def get_recent(self, limit: int = 20) -> list[OrderModel]:
        """Return the most recent *limit* orders by creation time."""
        t0 = time.monotonic()
        try:
            async with self._db.pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT {self._column_list()} FROM {self._table} "
                    f"{self._tenant_pred(0, prefix_and=False)[0]} "
                    f"ORDER BY created_at DESC LIMIT ${len(self._tenant_pred(0, prefix_and=False)[1]) + 1}",
                    *self._tenant_pred(0, prefix_and=False)[1],
                    limit,
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > self._slow_query_threshold_ms:
                self._log.warning("slow_query", ms=round(dur), method="get_recent")
            return [OrderModel.from_row(r) for r in rows]
        except Exception:
            self._log.exception("get_recent_orders_failed")
            raise


class TradeRepository(BaseRepository[TradeModel]):
    """Repository for executed trades."""

    def __init__(
        self,
        db_manager: _DbManager,
        model_cls: type[TradeModel] | None = None,
        tenant_id: str | None = None,
    ) -> None:
        super().__init__(db_manager, model_cls or TradeModel, tenant_id=tenant_id)

    async def get_by_position(self, position_id: int) -> list[TradeModel]:
        """Return all trades belonging to a position."""
        return await self.list(position_id=position_id)

    async def get_recent(self, limit: int = 50) -> list[TradeModel]:
        """Return the most recent *limit* trades by timestamp."""
        t0 = time.monotonic()
        try:
            async with self._db.pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT {self._column_list()} FROM {self._table} "
                    f"{self._tenant_pred(0, prefix_and=False)[0]} "
                    f"ORDER BY timestamp DESC LIMIT ${len(self._tenant_pred(0, prefix_and=False)[1]) + 1}",
                    *self._tenant_pred(0, prefix_and=False)[1],
                    limit,
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > self._slow_query_threshold_ms:
                self._log.warning("slow_query", ms=round(dur), method="get_recent")
            return [TradeModel.from_row(r) for r in rows]
        except Exception:
            self._log.exception("get_recent_trades_failed")
            raise

    async def sum_pnl_since(self, tenant_id: str, since_ms: int) -> Decimal:
        """Sum realized PnL for trades since a given timestamp.

        Performs the aggregation in SQL rather than loading all trade rows
        into memory, which is critical for high-frequency tenants with
        large trade histories.
        """
        t0 = time.monotonic()
        try:
            async with self._db.pool.acquire() as conn:
                if self._tenant_scoped:
                    row = await conn.fetchrow(
                        f"SELECT COALESCE(SUM(pnl), 0) as total FROM {self._table} "
                        f"WHERE tenant_id = $1 AND timestamp >= $2",
                        tenant_id,
                        since_ms,
                    )
                else:
                    row = await conn.fetchrow(
                        f"SELECT COALESCE(SUM(pnl), 0) as total FROM {self._table} "
                        f"WHERE timestamp >= $1",
                        since_ms,
                    )
            dur = (time.monotonic() - t0) * 1000
            if dur > self._slow_query_threshold_ms:
                self._log.warning("slow_query", ms=round(dur), method="sum_pnl_since")
            # Positional access: SQLite returns plain tuples while asyncpg
            # returns Records (both support [0]); key access works only on PG.
            return Decimal(str(row[0])) if row else Decimal(0)
        except Exception:
            self._log.exception("sum_pnl_since_failed")
            raise

    async def exists_for_order(self, order_id: int, side: str) -> bool:
        """True if a trade row already exists for this order id + side.

        Used to de-duplicate exchange income-history fills against rows the
        execution engine already persisted from opening fills, so the
        reconcile sweep never double-inserts a leg.
        """
        if not order_id:
            return False
        try:
            async with self._db.pool.acquire() as conn:
                row = await conn.fetchval(
                    f"SELECT 1 FROM {self._table} "
                    f"WHERE order_id = $1 AND side = $2{self._tenant_pred(2)[0]} LIMIT 1",
                    order_id,
                    side,
                    *self._tenant_pred(2)[1],
                )
            return row is not None
        except Exception:
            self._log.exception("trade_exists_check_failed")
            return False

    async def get_by_date_range(
        self,
        start: int,
        end: int,
    ) -> list[TradeModel]:
        """Return trades within a timestamp range (inclusive)."""
        t0 = time.monotonic()
        try:
            async with self._db.pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT {self._column_list()} FROM {self._table} "
                    f"WHERE timestamp >= $1 AND timestamp <= $2{self._tenant_pred(2)[0]} "
                    f"ORDER BY timestamp ASC",
                    start,
                    end,
                    *self._tenant_pred(2)[1],
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > self._slow_query_threshold_ms:
                self._log.warning(
                    "slow_query", ms=round(dur), method="get_by_date_range"
                )
            return [TradeModel.from_row(r) for r in rows]
        except Exception:
            self._log.exception("get_trades_by_date_range_failed")
            raise


class DecisionRepository(BaseRepository[DecisionModel]):
    """Repository for strategy decision records."""

    def __init__(
        self,
        db_manager: _DbManager,
        model_cls: type[DecisionModel] | None = None,
        tenant_id: str | None = None,
    ) -> None:
        super().__init__(db_manager, model_cls or DecisionModel, tenant_id=tenant_id)

    async def get_recent(self, limit: int = 20) -> list[DecisionModel]:
        """Return the most recent *limit* decisions by timestamp."""
        t0 = time.monotonic()
        try:
            async with self._db.pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT {self._column_list()} FROM {self._table} "
                    f"{self._tenant_pred(0, prefix_and=False)[0]} "
                    f"ORDER BY timestamp DESC LIMIT ${len(self._tenant_pred(0, prefix_and=False)[1]) + 1}",
                    *self._tenant_pred(0, prefix_and=False)[1],
                    limit,
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > self._slow_query_threshold_ms:
                self._log.warning("slow_query", ms=round(dur), method="get_recent")
            return [DecisionModel.from_row(r) for r in rows]
        except Exception:
            self._log.exception("get_recent_decisions_failed")
            raise

    async def get_by_strategy(self, strategy: str) -> list[DecisionModel]:
        """Return all decisions from a specific strategy."""
        return await self.list(strategy=strategy)

    async def get_by_date_range(
        self,
        start: int,
        end: int,
    ) -> list[DecisionModel]:
        """Return decisions within a timestamp range (inclusive)."""
        t0 = time.monotonic()
        try:
            async with self._db.pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT {self._column_list()} FROM {self._table} "
                    f"WHERE timestamp >= $1 AND timestamp <= $2{self._tenant_pred(2)[0]} "
                    f"ORDER BY timestamp ASC",
                    start,
                    end,
                    *self._tenant_pred(2)[1],
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > self._slow_query_threshold_ms:
                self._log.warning(
                    "slow_query", ms=round(dur), method="get_by_date_range"
                )
            return [DecisionModel.from_row(r) for r in rows]
        except Exception:
            self._log.exception("get_decisions_by_date_range_failed")
            raise

    async def get_unresolved(self, limit: int = 100) -> list[DecisionModel]:
        """Return decisions whose position outcome is still unresolved.

        An executed ENTER decision stays ``outcome='open'`` until the
        position closes (via TP/SL bracket on the exchange).  The
        orchestrator reconciles these against live positions each cycle.

        Parameters
        ----------
        limit:
            Maximum number of unresolved decisions to return.

        Returns
        -------
        list[DecisionModel]
            Unresolved decisions ordered oldest-first.
        """
        t0 = time.monotonic()
        try:
            async with self._db.pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT {self._column_list()} FROM {self._table} "
                    f"WHERE outcome = 'open'{self._tenant_pred(0)[0]} ORDER BY timestamp ASC LIMIT ${len(self._tenant_pred(0)[1]) + 1}",
                    *self._tenant_pred(0)[1],
                    limit,
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > self._slow_query_threshold_ms:
                self._log.warning("slow_query", ms=round(dur), method="get_unresolved")
            return [DecisionModel.from_row(r) for r in rows]
        except Exception:
            self._log.exception("get_unresolved_decisions_failed")
            raise

    async def get_resolved(
        self,
        limit: int = 500,
        since: int | None = None,
        only_directional: bool = True,
    ) -> list[DecisionModel]:
        """Return decisions whose outcome has been resolved (not ``'open'``).

        Used by the Phase-3 prediction-quality metrics module: hit rate, ECE,
        and Brier score are computed over resolved ENTER decisions.  The
        ``outcome`` column is one of ``'win'``, ``'loss'``, or ``'flat'``; the
        caller (``quad.ai.metrics``) decides how each label counts.

        Parameters
        ----------
        limit:
            Maximum number of resolved decisions to return (oldest-first).
        since:
            Optional Unix-epoch millisecond floor on ``timestamp``, so the
            metrics window can be narrowed without pulling the whole table.
        only_directional:
            When True (default), restrict to rows that carry a directional
            prediction (LONG/SHORT) AND a resolved outcome — exactly the rows
            ``metrics.compute_metrics`` can use.  When False, return every
            resolved row (including NEUTRAL predictions and non-ENTER
            actions) for inspection.

        Returns
        -------
        list[DecisionModel]
            Resolved decisions ordered oldest-first.
        """
        t0 = time.monotonic()
        try:
            async with self._db.pool.acquire() as conn:
                if since is not None:
                    if only_directional:
                        rows = await conn.fetch(
                            f"SELECT {self._column_list()} FROM {self._table} "
                            f"WHERE outcome != 'open' "
                            f"AND predicted_direction IN ('LONG', 'SHORT') "
                            f"AND timestamp >= $1{self._tenant_pred(1)[0]} "
                            f"ORDER BY timestamp ASC LIMIT ${len(self._tenant_pred(1)[1]) + 2}",
                            since,
                            *self._tenant_pred(1)[1],
                            limit,
                        )
                    else:
                        rows = await conn.fetch(
                            f"SELECT {self._column_list()} FROM {self._table} "
                            f"WHERE outcome != 'open' "
                            f"AND timestamp >= $1{self._tenant_pred(1)[0]} "
                            f"ORDER BY timestamp ASC LIMIT ${len(self._tenant_pred(1)[1]) + 2}",
                            since,
                            *self._tenant_pred(1)[1],
                            limit,
                        )
                else:
                    if only_directional:
                        rows = await conn.fetch(
                            f"SELECT {self._column_list()} FROM {self._table} "
                            f"WHERE outcome != 'open' "
                            f"AND predicted_direction IN ('LONG', 'SHORT'){self._tenant_pred(0)[0]} "
                            f"ORDER BY timestamp ASC LIMIT ${len(self._tenant_pred(0)[1]) + 1}",
                            *self._tenant_pred(0)[1],
                            limit,
                        )
                    else:
                        rows = await conn.fetch(
                            f"SELECT {self._column_list()} FROM {self._table} "
                            f"WHERE outcome != 'open'{self._tenant_pred(0)[0]} "
                            f"ORDER BY timestamp ASC LIMIT ${len(self._tenant_pred(0)[1]) + 1}",
                            *self._tenant_pred(0)[1],
                            limit,
                        )
            dur = (time.monotonic() - t0) * 1000
            if dur > self._slow_query_threshold_ms:
                self._log.warning("slow_query", ms=round(dur), method="get_resolved")
            return [DecisionModel.from_row(r) for r in rows]
        except Exception:
            self._log.exception("get_resolved_decisions_failed")
            raise

    async def mark_outcome(
        self,
        decision_id: int,
        outcome: str,
        realized_pnl: str = "0",
        exit_price: str = "",
        resolved_at: int | None = None,
    ) -> None:
        """Mark a decision's position outcome as resolved.

        Called when the position opened by an ENTER decision disappears
        (closed by TP/SL bracket, liquidation, or manual close).  ``outcome``
        is one of ``"win"``, ``"loss"``, or ``"flat"``.

        Parameters
        ----------
        decision_id:
            Primary key of the decision to resolve.
        outcome:
            ``"win"``, ``"loss"``, or ``"flat"``.
        realized_pnl:
            Realized PnL as a decimal string.
        exit_price:
            Price at which the position closed (decimal string).
        resolved_at:
            Unix epoch milliseconds when the position closed.  Defaults to now.
        """
        if resolved_at is None:
            resolved_at = int(time.time() * 1000)
        await self.update(
            decision_id,
            outcome=outcome,
            realized_pnl=str(realized_pnl or "0"),
            exit_price=str(exit_price or ""),
            resolved_at=resolved_at,
        )


class SessionRepository(BaseRepository[SessionModel]):
    """Repository for trading sessions."""

    def __init__(
        self,
        db_manager: _DbManager,
        model_cls: type[SessionModel] | None = None,
        tenant_id: str | None = None,
    ) -> None:
        super().__init__(db_manager, model_cls or SessionModel, tenant_id=tenant_id)

    async def get_latest(self) -> SessionModel | None:
        """Return the most recently started session."""
        t0 = time.monotonic()
        try:
            async with self._db.pool.acquire() as conn:
                row = await conn.fetchrow(
                    f"SELECT {self._column_list()} FROM {self._table} "
                    f"{self._tenant_pred(0, prefix_and=False)[0]} "
                    f"ORDER BY start_time DESC LIMIT 1",
                    *self._tenant_pred(0, prefix_and=False)[1],
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > self._slow_query_threshold_ms:
                self._log.warning("slow_query", ms=round(dur), method="get_latest")
            if row is None:
                return None
            return SessionModel.from_row(row)
        except Exception:
            self._log.exception("get_latest_session_failed")
            raise

    async def close_session(
        self,
        session_id: int,
        end_time: int,
        pnl: str,
        trades_count: int,
    ) -> None:
        """Mark a session as completed."""
        await self.update(
            session_id,
            end_time=end_time,
            state="completed",
            pnl=pnl,
            trades_count=trades_count,
        )

    async def start_session(self, mode: str) -> int:
        """Create a new session and return its id."""
        now = int(time.time() * 1000)
        session = SessionModel(
            id=0,
            start_time=now,
            end_time=None,
            mode=mode,
            state="running",
            pnl="0",
            trades_count=0,
        )
        return await self.create(session)


class PerformanceSnapshotRepository(BaseRepository[PerformanceSnapshotModel]):
    """Repository for portfolio performance snapshots."""

    def __init__(
        self,
        db_manager: _DbManager,
        model_cls: type[PerformanceSnapshotModel] | None = None,
        tenant_id: str | None = None,
    ) -> None:
        super().__init__(
            db_manager, model_cls or PerformanceSnapshotModel, tenant_id=tenant_id
        )

    async def get_recent(self, limit: int = 20) -> list[PerformanceSnapshotModel]:
        """Return the most recent *limit* snapshots by timestamp."""
        t0 = time.monotonic()
        try:
            async with self._db.pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT {self._column_list()} FROM {self._table} "
                    f"{self._tenant_pred(0, prefix_and=False)[0]} "
                    f"ORDER BY timestamp DESC LIMIT ${len(self._tenant_pred(0, prefix_and=False)[1]) + 1}",
                    *self._tenant_pred(0, prefix_and=False)[1],
                    limit,
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > self._slow_query_threshold_ms:
                self._log.warning("slow_query", ms=round(dur), method="get_recent")
            return [PerformanceSnapshotModel.from_row(r) for r in rows]
        except Exception:
            self._log.exception("get_recent_snapshots_failed")
            raise

    async def get_by_date_range(
        self,
        start: int,
        end: int,
    ) -> list[PerformanceSnapshotModel]:
        """Return snapshots within a timestamp range (inclusive)."""
        t0 = time.monotonic()
        try:
            async with self._db.pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT {self._column_list()} FROM {self._table} "
                    f"WHERE timestamp >= $1 AND timestamp <= $2{self._tenant_pred(2)[0]} "
                    f"ORDER BY timestamp ASC",
                    start,
                    end,
                    *self._tenant_pred(2)[1],
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > self._slow_query_threshold_ms:
                self._log.warning(
                    "slow_query", ms=round(dur), method="get_by_date_range"
                )
            return [PerformanceSnapshotModel.from_row(r) for r in rows]
        except Exception:
            self._log.exception("get_snapshots_by_date_range_failed")
            raise


class OptimizationRunRepository(BaseRepository[OptimizationRunModel]):
    """Repository for optimization_run operations."""

    def __init__(
        self,
        db_manager: _DbManager,
        model_cls: type[OptimizationRunModel] | None = None,
        tenant_id: str | None = None,
    ) -> None:
        super().__init__(
            db_manager, model_cls or OptimizationRunModel, tenant_id=tenant_id
        )

    async def get_by_date_range(
        self, start: int, end: int
    ) -> list[OptimizationRunModel]:
        """Return runs within a timestamp range."""
        async with self._db.pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {self._column_list()} FROM {self._table} "
                f"WHERE run_at >= $1 AND run_at <= $2{self._tenant_pred(2)[0]} "
                "ORDER BY run_at DESC",
                start,
                end,
                *self._tenant_pred(2)[1],
            )
            return [self._model_cls.from_row(row) for row in rows]

    async def get_recent(self, limit: int = 10) -> list[OptimizationRunModel]:
        """Return the most recent runs."""
        async with self._db.pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {self._column_list()} FROM {self._table} "
                f"{self._tenant_pred(0, prefix_and=False)[0]} "
                f"ORDER BY run_at DESC LIMIT ${len(self._tenant_pred(0, prefix_and=False)[1]) + 1}",
                *self._tenant_pred(0, prefix_and=False)[1],
                limit,
            )
            return [self._model_cls.from_row(row) for row in rows]

    async def get_by_status(self, status: str) -> list[OptimizationRunModel]:
        """Return runs with a given status."""
        async with self._db.pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {self._column_list()} FROM {self._table} WHERE status = $1{self._tenant_pred(1)[0]} "
                "ORDER BY run_at DESC",
                status,
                *self._tenant_pred(1)[1],
            )
            return [self._model_cls.from_row(row) for row in rows]

    async def get_latest(self) -> OptimizationRunModel | None:
        """Return the most recent run (any status)."""
        async with self._db.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {self._column_list()} FROM {self._table} "
                f"{self._tenant_pred(0, prefix_and=False)[0]} "
                "ORDER BY run_at DESC LIMIT 1",
                *self._tenant_pred(0, prefix_and=False)[1],
            )
            return self._model_cls.from_row(row) if row else None


class OptimizationRecommendationRepository(
    BaseRepository[OptimizationRecommendationModel]
):
    """Repository for optimization_recommendation operations."""

    def __init__(
        self,
        db_manager: _DbManager,
        model_cls: type[OptimizationRecommendationModel] | None = None,
        tenant_id: str | None = None,
    ) -> None:
        super().__init__(
            db_manager,
            model_cls or OptimizationRecommendationModel,
            tenant_id=tenant_id,
        )

    async def get_by_run(self, run_id: int) -> list[OptimizationRecommendationModel]:
        """Return all recommendations for a given run."""
        async with self._db.pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {self._column_list()} FROM {self._table} "
                f"WHERE run_id = $1{self._tenant_pred(1)[0]} ORDER BY id",
                run_id,
                *self._tenant_pred(1)[1],
            )
            return [self._model_cls.from_row(row) for row in rows]

    async def get_pending(self) -> list[OptimizationRecommendationModel]:
        """Return all recommendations with status = 'pending'."""
        async with self._db.pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {self._column_list()} FROM {self._table} "
                f"WHERE status = 'pending'{self._tenant_pred(0)[0]} ORDER BY run_id DESC, id",
                *self._tenant_pred(0)[1],
            )
            return [self._model_cls.from_row(row) for row in rows]

    async def get_by_type(
        self, recommendation_type: str
    ) -> list[OptimizationRecommendationModel]:
        """Return recommendations of a given type."""
        async with self._db.pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {self._column_list()} FROM {self._table} "
                f"WHERE recommendation_type = $1{self._tenant_pred(1)[0]} ORDER BY id",
                recommendation_type,
                *self._tenant_pred(1)[1],
            )
            return [self._model_cls.from_row(row) for row in rows]

    async def get_by_status(self, status: str) -> list[OptimizationRecommendationModel]:
        """Return recommendations with a given status."""
        async with self._db.pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {self._column_list()} FROM {self._table} "
                f"WHERE status = $1{self._tenant_pred(1)[0]} ORDER BY run_id DESC",
                status,
                *self._tenant_pred(1)[1],
            )
            return [self._model_cls.from_row(row) for row in rows]

    async def mark_applied(
        self, recommendation_id: int, applied_at: int, strategy_params_json: str
    ) -> None:
        """Mark a recommendation as applied."""
        async with self._db.pool.acquire() as conn:
            await conn.execute(
                "UPDATE optimization_recommendations "
                "SET status = 'applied', applied_at = $1, "
                "    applied_strategy_params_json = $2 "
                f"WHERE id = $3{self._tenant_pred(3)[0]}",
                applied_at,
                strategy_params_json,
                recommendation_id,
                *self._tenant_pred(3)[1],
            )


class ConfigChangeRepository(BaseRepository[ConfigChangeModel]):
    """Repository for config_change audit log entries."""

    def __init__(
        self,
        db_manager: _DbManager,
        model_cls: type[ConfigChangeModel] | None = None,
        tenant_id: str | None = None,
    ) -> None:
        super().__init__(
            db_manager, model_cls or ConfigChangeModel, tenant_id=tenant_id
        )

    async def get_recent(self, limit: int = 50) -> list[ConfigChangeModel]:
        """Return the most recent config changes."""
        async with self._db.pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {self._column_list()} FROM {self._table} {self._tenant_pred(0, prefix_and=False)[0]} ORDER BY id DESC LIMIT ${len(self._tenant_pred(0, prefix_and=False)[1]) + 1}",
                *self._tenant_pred(0, prefix_and=False)[1],
                limit,
            )
            return [self._model_cls.from_row(row) for row in rows]

    async def get_by_key(self, key: str, limit: int = 20) -> list[ConfigChangeModel]:
        """Return config changes for a specific key."""
        async with self._db.pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {self._column_list()} FROM {self._table} WHERE key = $1{self._tenant_pred(2)[0]} ORDER BY id DESC LIMIT $2",
                key,
                limit,
                *self._tenant_pred(2)[1],
            )
            return [self._model_cls.from_row(row) for row in rows]


class CircuitBreakerEventRepository(BaseRepository[CircuitBreakerEventModel]):
    """Repository for circuit breaker trigger events."""

    def __init__(
        self,
        db_manager: _DbManager,
        model_cls: type[CircuitBreakerEventModel] | None = None,
        tenant_id: str | None = None,
    ) -> None:
        super().__init__(
            db_manager, model_cls or CircuitBreakerEventModel, tenant_id=tenant_id
        )

    async def get_by_type(
        self, breaker_name: str, limit: int = 50
    ) -> list[CircuitBreakerEventModel]:
        """Return the most recent events for a specific circuit breaker by name."""
        t0 = time.monotonic()
        try:
            async with self._db.pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT {self._column_list()} FROM {self._table} "
                    f"WHERE breaker_name = $1{self._tenant_pred(2)[0]} ORDER BY timestamp DESC LIMIT $2",
                    breaker_name,
                    limit,
                    *self._tenant_pred(2)[1],
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > self._slow_query_threshold_ms:
                self._log.warning("slow_query", ms=round(dur), method="get_by_type")
            return [CircuitBreakerEventModel.from_row(r) for r in rows]
        except Exception:
            self._log.exception("get_circuit_events_by_type_failed")
            raise

    async def get_recent(self, limit: int = 100) -> list[CircuitBreakerEventModel]:
        """Return the most recent circuit breaker events."""
        t0 = time.monotonic()
        try:
            async with self._db.pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT {self._column_list()} FROM {self._table} "
                    f"{self._tenant_pred(0, prefix_and=False)[0]} "
                    f"ORDER BY timestamp DESC LIMIT ${len(self._tenant_pred(0, prefix_and=False)[1]) + 1}",
                    *self._tenant_pred(0, prefix_and=False)[1],
                    limit,
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > self._slow_query_threshold_ms:
                self._log.warning("slow_query", ms=round(dur), method="get_recent")
            return [CircuitBreakerEventModel.from_row(r) for r in rows]
        except Exception:
            self._log.exception("get_recent_circuit_events_failed")
            raise

    async def get_by_date_range(
        self, start: int, end: int
    ) -> list[CircuitBreakerEventModel]:
        """Return circuit breaker events within a timestamp range (inclusive)."""
        t0 = time.monotonic()
        try:
            async with self._db.pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT {self._column_list()} FROM {self._table} "
                    f"WHERE timestamp >= $1 AND timestamp <= $2{self._tenant_pred(2)[0]} "
                    "ORDER BY timestamp ASC",
                    start,
                    end,
                    *self._tenant_pred(2)[1],
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > self._slow_query_threshold_ms:
                self._log.warning(
                    "slow_query", ms=round(dur), method="get_by_date_range"
                )
            return [CircuitBreakerEventModel.from_row(r) for r in rows]
        except Exception:
            self._log.exception("get_circuit_events_by_date_range_failed")
            raise


class ErrorLogRepository(BaseRepository[ErrorLogModel]):
    """Repository for application error log entries."""

    def __init__(
        self,
        db_manager: _DbManager,
        model_cls: type[ErrorLogModel] | None = None,
        tenant_id: str | None = None,
    ) -> None:
        super().__init__(db_manager, model_cls or ErrorLogModel, tenant_id=tenant_id)

    async def get_by_level(self, level: str, limit: int = 50) -> list[ErrorLogModel]:
        """Return the most recent error logs for a given severity level."""
        t0 = time.monotonic()
        try:
            async with self._db.pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT {self._column_list()} FROM {self._table} "
                    f"WHERE level = $1{self._tenant_pred(2)[0]} ORDER BY timestamp DESC LIMIT $2",
                    level,
                    limit,
                    *self._tenant_pred(2)[1],
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > self._slow_query_threshold_ms:
                self._log.warning("slow_query", ms=round(dur), method="get_by_level")
            return [ErrorLogModel.from_row(r) for r in rows]
        except Exception:
            self._log.exception("get_error_logs_by_level_failed")
            raise

    async def get_recent(self, limit: int = 100) -> list[ErrorLogModel]:
        """Return the most recent error logs."""
        t0 = time.monotonic()
        try:
            async with self._db.pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT {self._column_list()} FROM {self._table} "
                    f"{self._tenant_pred(0, prefix_and=False)[0]} "
                    f"ORDER BY timestamp DESC LIMIT ${len(self._tenant_pred(0, prefix_and=False)[1]) + 1}",
                    *self._tenant_pred(0, prefix_and=False)[1],
                    limit,
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > self._slow_query_threshold_ms:
                self._log.warning("slow_query", ms=round(dur), method="get_recent")
            return [ErrorLogModel.from_row(r) for r in rows]
        except Exception:
            self._log.exception("get_recent_error_logs_failed")
            raise

    async def get_by_date_range(self, start: int, end: int) -> list[ErrorLogModel]:
        """Return error logs within a timestamp range (inclusive)."""
        t0 = time.monotonic()
        try:
            async with self._db.pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT {self._column_list()} FROM {self._table} "
                    f"WHERE timestamp >= $1 AND timestamp <= $2{self._tenant_pred(2)[0]} "
                    "ORDER BY timestamp ASC",
                    start,
                    end,
                    *self._tenant_pred(2)[1],
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > self._slow_query_threshold_ms:
                self._log.warning(
                    "slow_query", ms=round(dur), method="get_by_date_range"
                )
            return [ErrorLogModel.from_row(r) for r in rows]
        except Exception:
            self._log.exception("get_error_logs_by_date_range_failed")
            raise

    async def get_by_source(self, source: str, limit: int = 50) -> list[ErrorLogModel]:
        """Return the most recent error logs from a specific component (event name)."""
        t0 = time.monotonic()
        try:
            async with self._db.pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT {self._column_list()} FROM {self._table} "
                    f"WHERE event = $1{self._tenant_pred(2)[0]} ORDER BY timestamp DESC LIMIT $2",
                    source,
                    limit,
                    *self._tenant_pred(2)[1],
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > self._slow_query_threshold_ms:
                self._log.warning("slow_query", ms=round(dur), method="get_by_source")
            return [ErrorLogModel.from_row(r) for r in rows]
        except Exception:
            self._log.exception("get_error_logs_by_source_failed")
            raise


class StrategyStateRepository(BaseRepository[StrategyStateModel]):
    """Repository for strategy persistent state records."""

    def __init__(
        self,
        db_manager: _DbManager,
        model_cls: type[StrategyStateModel] | None = None,
        tenant_id: str | None = None,
    ) -> None:
        super().__init__(
            db_manager, model_cls or StrategyStateModel, tenant_id=tenant_id
        )

    async def get_by_strategy(self, name: str) -> StrategyStateModel | None:
        """Return the state for a single strategy by name."""
        t0 = time.monotonic()
        try:
            async with self._db.pool.acquire() as conn:
                row = await conn.fetchrow(
                    f"SELECT {self._column_list()} FROM {self._table} "
                    f"WHERE strategy_name = $1{self._tenant_pred(1)[0]}",
                    name,
                    *self._tenant_pred(1)[1],
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > self._slow_query_threshold_ms:
                self._log.warning("slow_query", ms=round(dur), method="get_by_strategy")
            if row is None:
                return None
            return StrategyStateModel.from_row(row)
        except Exception:
            self._log.exception("get_strategy_state_failed")
            raise

    async def get_enabled(self) -> list[StrategyStateModel]:
        """Return all enabled strategy states."""
        return await self.list(enabled=1)

    async def get_all(self) -> list[StrategyStateModel]:
        """Return all strategy states."""
        t0 = time.monotonic()
        try:
            async with self._db.pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT {self._column_list()} FROM {self._table} "
                    f"{self._tenant_pred(0, prefix_and=False)[0]} "
                    "ORDER BY strategy_name ASC",
                    *self._tenant_pred(0, prefix_and=False)[1],
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > self._slow_query_threshold_ms:
                self._log.warning("slow_query", ms=round(dur), method="get_all")
            return [StrategyStateModel.from_row(r) for r in rows]
        except Exception:
            self._log.exception("get_all_strategy_states_failed")
            raise

    async def upsert(self, state: StrategyStateModel) -> int:
        """Insert or update a strategy state record.

        Scoped by ``(strategy_name, tenant_id)`` via a single atomic
        ``INSERT ... ON CONFLICT DO UPDATE`` so concurrent writers cannot
        race and one tenant can never clobber another's row.
        Returns the row id.
        """
        t0 = time.monotonic()
        try:
            if (
                self._tenant_scoped
                and self._tenant_id is not None
                and hasattr(state, "tenant_id")
            ):
                try:
                    state.tenant_id = self._tenant_id
                except Exception:
                    pass
            if getattr(state, "tenant_id", None) is None:
                try:
                    state.tenant_id = "default"
                except Exception:
                    pass
            columns = self._column_list()
            placeholders = self._param_placeholders()
            update_cols = [
                c
                for c in self._columns
                if c not in ("id", "strategy_name", "tenant_id")
            ]
            set_pairs = self._column_set_pairs(update_cols)
            async with self._db.pool.acquire() as conn:
                try:
                    last_id = await conn.fetchval(
                        f"INSERT INTO {self._table} ({columns}) "
                        f"VALUES ({placeholders}) "
                        f"ON CONFLICT (strategy_name, tenant_id) DO UPDATE "
                        f"SET {set_pairs} "
                        f"RETURNING id",
                        *state.to_row(),
                    )
                except Exception:
                    # Pre-migration DBs without the composite unique index:
                    # fall back to a scoped update on conflict.
                    set_clause = self._placeholder_clause(update_cols, start=3)
                    row = dict(zip(self._columns, state.to_row()))
                    set_vals = [row[c] for c in update_cols]
                    await conn.execute(
                        f"UPDATE {self._table} SET {set_clause} "
                        f"WHERE strategy_name = $1 AND tenant_id = $2",
                        state.strategy_name,
                        state.tenant_id,
                        *set_vals,
                    )
                    row2 = await conn.fetchrow(
                        f"SELECT id FROM {self._table} "
                        f"WHERE strategy_name = $1 AND tenant_id = $2",
                        state.strategy_name,
                        state.tenant_id,
                    )
                    last_id = (
                        (row2["id"] if isinstance(row2, dict) else row2[0])
                        if row2
                        else 0
                    )
            dur = (time.monotonic() - t0) * 1000
            if dur > self._slow_query_threshold_ms:
                self._log.warning("slow_query", ms=round(dur), method="upsert")
            return last_id
        except Exception:
            self._log.exception("upsert_strategy_state_failed")
            raise


# ---------------------------------------------------------------------------
# Futures-specific repositories
# ---------------------------------------------------------------------------


class FundingRepository(BaseRepository[FundingPaymentModel]):
    """Repository for funding payment records."""

    def __init__(
        self,
        db_manager: _DbManager,
        model_cls: type[FundingPaymentModel] | None = None,
        tenant_id: str | None = None,
    ) -> None:
        super().__init__(
            db_manager, model_cls or FundingPaymentModel, tenant_id=tenant_id
        )

    async def save_funding_payment(
        self, symbol: str, position_id: int, amount: str, rate: str
    ) -> int:
        """Record a funding payment."""
        now = int(time.time() * 1000)
        payment = FundingPaymentModel(
            id=0,
            symbol=symbol,
            position_id=position_id,
            amount=amount,
            rate=rate,
            funding_time=now,
        )
        return await self.create(payment)

    async def get_funding_history(
        self, symbol: str, limit: int = 50
    ) -> list[FundingPaymentModel]:
        """Return recent funding payments for a symbol."""
        t0 = time.monotonic()
        try:
            async with self._db.pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT {self._column_list()} FROM {self._table} "
                    f"WHERE symbol = $1{self._tenant_pred(2)[0]} ORDER BY funding_time DESC LIMIT $2",
                    symbol,
                    limit,
                    *self._tenant_pred(2)[1],
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > self._slow_query_threshold_ms:
                self._log.warning(
                    "slow_query", ms=round(dur), method="get_funding_history"
                )
            return [FundingPaymentModel.from_row(r) for r in rows]
        except Exception:
            self._log.exception("get_funding_history_failed")
            raise

    async def get_total_funding_paid(self, position_id: int) -> str:
        """Return total funding paid for a position."""
        t0 = time.monotonic()
        try:
            async with self._db.pool.acquire() as conn:
                row = await conn.fetchval(
                    f"SELECT COALESCE(SUM(CAST(amount AS NUMERIC)), 0) FROM {self._table} "
                    f"WHERE position_id = $1{self._tenant_pred(1)[0]}",
                    position_id,
                    *self._tenant_pred(1)[1],
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > self._slow_query_threshold_ms:
                self._log.warning(
                    "slow_query", ms=round(dur), method="get_total_funding_paid"
                )
            return str(row) if row is not None else "0"
        except Exception:
            self._log.exception("get_total_funding_paid_failed")
            raise


class LiquidationRepository(BaseRepository[LiquidationEventModel]):
    """Repository for liquidation events."""

    def __init__(
        self,
        db_manager: _DbManager,
        model_cls: type[LiquidationEventModel] | None = None,
        tenant_id: str | None = None,
    ) -> None:
        super().__init__(
            db_manager, model_cls or LiquidationEventModel, tenant_id=tenant_id
        )

    async def record_liquidation(
        self, symbol: str, position_id: int, amount: str, price: str, side: str
    ) -> int:
        """Record a liquidation event."""
        now = int(time.time() * 1000)
        event = LiquidationEventModel(
            id=0,
            symbol=symbol,
            position_id=position_id,
            amount=amount,
            price=price,
            side=side,
            timestamp=now,
        )
        return await self.create(event)

    async def get_recent_liquidations(
        self, symbol: str | None = None, hours: int = 24
    ) -> list[LiquidationEventModel]:
        """Return liquidation events from the past N hours."""
        cutoff = int(time.time() * 1000) - hours * 3600 * 1000
        t0 = time.monotonic()
        try:
            async with self._db.pool.acquire() as conn:
                if symbol:
                    rows = await conn.fetch(
                        f"SELECT {self._column_list()} FROM {self._table} "
                        f"WHERE timestamp >= $1 AND symbol = $2{self._tenant_pred(2)[0]} "
                        "ORDER BY timestamp DESC",
                        cutoff,
                        symbol,
                        *self._tenant_pred(2)[1],
                    )
                else:
                    rows = await conn.fetch(
                        f"SELECT {self._column_list()} FROM {self._table} "
                        f"WHERE timestamp >= $1{self._tenant_pred(1)[0]} ORDER BY timestamp DESC",
                        cutoff,
                        *self._tenant_pred(1)[1],
                    )
            dur = (time.monotonic() - t0) * 1000
            if dur > self._slow_query_threshold_ms:
                self._log.warning(
                    "slow_query", ms=round(dur), method="get_recent_liquidations"
                )
            return [LiquidationEventModel.from_row(r) for r in rows]
        except Exception:
            self._log.exception("get_recent_liquidations_failed")
            raise


# ---------------------------------------------------------------------------
# Multi-tenant repositories (quad-api, schema v5)
# ---------------------------------------------------------------------------


class TenantRepository(BaseRepository[TenantModel]):
    """Repository for tenants (one human user per row)."""

    def __init__(
        self,
        db_manager: _DbManager,
        model_cls: type[TenantModel] | None = None,
        tenant_id: str | None = None,
    ) -> None:
        super().__init__(db_manager, model_cls or TenantModel, tenant_id=tenant_id)

    async def get_by_uuid(self, tenant_uuid: str) -> TenantModel | None:
        """Return the tenant with this uuid, or None."""
        rows = await self.list(tenant_uuid=tenant_uuid)
        return rows[0] if rows else None

    async def get_by_telegram_user(self, telegram_user_id: int) -> TenantModel | None:
        """Return the tenant linked to a Telegram user id, or None."""
        rows = await self.list(telegram_user_id=telegram_user_id)
        return rows[0] if rows else None

    async def create_tenant(
        self,
        tenant_uuid: str,
        telegram_user_id: int | None = None,
        username: str = "",
        display_name: str = "",
    ) -> TenantModel:
        """Insert a tenant row and return it."""
        now = int(time.time() * 1000)
        model = TenantModel(
            id=0,
            tenant_uuid=tenant_uuid,
            telegram_user_id=telegram_user_id,
            username=username,
            display_name=display_name,
            status="active",
            created_at=now,
            updated_at=now,
        )
        row_id = await self.create(model)
        created = await self.get(row_id)
        assert created is not None
        return created

    async def update_token_version(self, tenant_uuid: str, new_version: int) -> None:
        """Bump a tenant's ``token_version`` column, invalidating all older JWTs.

        Backed by schema v10 (``ALTER TABLE tenants ADD COLUMN
        token_version``); invalidates every JWT whose ``ver`` claim is older.
        """
        try:
            async with self._db.pool.acquire() as conn:
                await conn.execute(
                    "UPDATE tenants SET token_version = $1 WHERE tenant_uuid = $2",
                    new_version,
                    tenant_uuid,
                )
        except Exception:
            # Column may not exist yet — ignore until migration lands.
            self._log.debug(
                "update_token_version_skipped",
                tenant_uuid=tenant_uuid,
                new_version=new_version,
            )


class ExchangeCredentialRepository(BaseRepository[ExchangeCredentialModel]):
    """Repository for per-tenant encrypted exchange credentials."""

    def __init__(
        self,
        db_manager: _DbManager,
        model_cls: type[ExchangeCredentialModel] | None = None,
        tenant_id: str | None = None,
    ) -> None:
        super().__init__(
            db_manager, model_cls or ExchangeCredentialModel, tenant_id=tenant_id
        )

    async def get_active(
        self, tenant_uuid: str, exchange: str = "bybit"
    ) -> ExchangeCredentialModel | None:
        """Return the tenant's credential row for an exchange, or None."""
        rows = await self.list(tenant_id=tenant_uuid, exchange=exchange)
        return rows[0] if rows else None

    async def upsert_encrypted(
        self,
        tenant_uuid: str,
        api_key_enc: str,
        api_secret_enc: str,
        exchange: str = "bybit",
        testnet: bool = True,
        bybit_uid: str = "",
        permissions: str = "",
    ) -> ExchangeCredentialModel:
        """Insert or replace the encrypted credential row for a tenant."""
        now = int(time.time() * 1000)
        existing = await self.get_active(tenant_uuid, exchange)
        if existing is None:
            model = ExchangeCredentialModel(
                id=0,
                tenant_id=tenant_uuid,
                exchange=exchange,
                api_key_enc=api_key_enc,
                api_secret_enc=api_secret_enc,
                testnet=1 if testnet else 0,
                bybit_uid=bybit_uid,
                permissions=permissions,
                last_verified_at=now,
                created_at=now,
                updated_at=now,
            )
            row_id = await self.create(model)
            created = await self.get(row_id)
            assert created is not None
            return created
        await self.update(
            existing.id,
            api_key_enc=api_key_enc,
            api_secret_enc=api_secret_enc,
            testnet=1 if testnet else 0,
            bybit_uid=bybit_uid,
            permissions=permissions,
            last_verified_at=now,
            updated_at=now,
        )
        updated = await self.get(existing.id)
        assert updated is not None
        return updated


class TenantConfigRepository(BaseRepository[TenantConfigModel]):
    """Repository for per-tenant trading configuration."""

    def __init__(
        self,
        db_manager: _DbManager,
        model_cls: type[TenantConfigModel] | None = None,
        tenant_id: str | None = None,
    ) -> None:
        super().__init__(
            db_manager, model_cls or TenantConfigModel, tenant_id=tenant_id
        )

    async def get_by_tenant(self, tenant_uuid: str) -> TenantConfigModel | None:
        """Return the tenant's config row, or None if never configured."""
        rows = await self.list(tenant_uuid=tenant_uuid)
        return rows[0] if rows else None

    async def get_or_default(self, tenant_uuid: str) -> TenantConfigModel:
        """Return config, creating a linear-default row on first use."""
        existing = await self.get_by_tenant(tenant_uuid)
        if existing is not None:
            return existing
        model = TenantConfigModel(
            id=0,
            tenant_uuid=tenant_uuid,
            market="linear",
            capital_pct_per_trade=2.0,
            leverage=10,
            take_profit_pct=50.0,
            stop_loss_pct=30.0,
            strategy="trend_following",
            max_positions=1,
            updated_at=int(time.time() * 1000),
        )
        row_id = await self.create(model)
        created = await self.get(row_id)
        assert created is not None
        return created


class TelegramBindingRepository(BaseRepository[TelegramBindingModel]):
    """Repository for Telegram chat <-> tenant bindings."""

    def __init__(
        self,
        db_manager: _DbManager,
        model_cls: type[TelegramBindingModel] | None = None,
        tenant_id: str | None = None,
    ) -> None:
        super().__init__(
            db_manager, model_cls or TelegramBindingModel, tenant_id=tenant_id
        )

    async def get_by_chat_id(self, chat_id: int) -> TelegramBindingModel | None:
        """Return the binding for a Telegram chat, or None (unbound)."""
        rows = await self.list(chat_id=chat_id)
        return rows[0] if rows else None

    async def get_by_tenant(self, tenant_uuid: str) -> TelegramBindingModel | None:
        """Return the tenant's binding, or None."""
        rows = await self.list(tenant_uuid=tenant_uuid)
        return rows[0] if rows else None

    async def bind(self, tenant_uuid: str, chat_id: int) -> TelegramBindingModel:
        """Bind a chat to a tenant (rebind moves the chat)."""
        now = int(time.time() * 1000)
        existing_chat = await self.get_by_chat_id(chat_id)
        if existing_chat is not None:
            await self.update(
                existing_chat.id,
                tenant_uuid=tenant_uuid,
                last_seen_at=now,
            )
            bound = await self.get(existing_chat.id)
            assert bound is not None
            return bound
        model = TelegramBindingModel(
            id=0,
            tenant_uuid=tenant_uuid,
            chat_id=chat_id,
            bound_at=now,
            last_seen_at=now,
        )
        row_id = await self.create(model)
        created = await self.get(row_id)
        assert created is not None
        return created


class PairingCodeRepository(BaseRepository[PairingCodeModel]):
    """Repository for single-use chat-linking pairing codes."""

    def __init__(
        self,
        db_manager: _DbManager,
        model_cls: type[PairingCodeModel] | None = None,
        tenant_id: str | None = None,
    ) -> None:
        super().__init__(db_manager, model_cls or PairingCodeModel, tenant_id=tenant_id)

    async def get_valid(self, code: str, now_ms: int) -> PairingCodeModel | None:
        """Return an unexpired, unused code row, or None."""
        t0 = time.monotonic()
        try:
            async with self._db.pool.acquire() as conn:
                pred_sql, pred_params = self._tenant_pred(2)
                row = await conn.fetchrow(
                    f"SELECT {self._column_list()} FROM {self._table} "
                    f"WHERE code = $1 AND used = 0 AND expires_at > $2{pred_sql} "
                    f"ORDER BY expires_at DESC LIMIT 1",
                    code,
                    now_ms,
                    *pred_params,
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > self._slow_query_threshold_ms:
                self._log.warning("slow_query", ms=round(dur), method="get_valid")
            if row is None:
                return None
            return PairingCodeModel.from_row(row)
        except Exception:
            self._log.exception("get_valid_pairing_code_failed")
            raise

    async def mark_used(self, code_id: int, used_by_chat: int) -> None:
        """Consume a pairing code after a successful bind."""
        await self.update(code_id, used=1, used_by_chat=used_by_chat)
