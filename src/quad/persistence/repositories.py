"""Repository classes for the Quad options trading bot.

Provides a generic ``BaseRepository[T]`` with CRUD operations and
domain-specific repositories for each entity type. Uses asyncpg with
PostgreSQL ``$N`` parameter style.
"""

from __future__ import annotations

import time
from typing import Any, Generic, Optional, TypeVar

import structlog

from .database import DatabaseManager
from .models import (
    AccountModel,
    DecisionModel,
    OptionContractModel,
    OrderModel,
    PerformanceSnapshotModel,
    PositionModel,
    SessionModel,
    TradeModel,
)

logger = structlog.get_logger(__name__)

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Base repository (generic CRUD)
# ---------------------------------------------------------------------------


class BaseRepository(Generic[T]):
    """Generic PostgreSQL repository providing CRUD operations.

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
        db_manager: DatabaseManager,
        model_cls: Optional[type[T]] = None,
    ) -> None:
        self._db = db_manager
        self._model_cls = model_cls  # type: ignore[assignment]
        self._table = model_cls.__tablename__  # type: ignore[attr-defined]
        self._columns = model_cls.columns()  # type: ignore[attr-defined]
        self._log = logger.bind(table=self._table)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _placeholder_clause(self, names: list[str], start: int = 1) -> str:
        """Return a SET clause with PostgreSQL ``$N`` placeholders.

        Example: ``"col1 = $1, col2 = $2"``
        """
        parts = []
        for i, n in enumerate(names):
            parts.append(f"{n} = ${start + i}")
        return ", ".join(parts)

    def _column_list(self) -> str:
        return ", ".join(self._columns)

    def _param_placeholders(self, start: int = 1) -> str:
        """Return a comma-separated list of PostgreSQL ``$N`` placeholders."""
        return ", ".join(f"${start + i}" for i in range(len(self._columns)))

    @staticmethod
    def _column_set_pairs(names: list[str]) -> str:
        """Return column = EXCLUDED.column pairs for ON CONFLICT DO UPDATE SET."""
        return ", ".join(f"{n} = EXCLUDED.{n}" for n in names)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def get(self, id: int) -> Optional[T]:
        """Retrieve a single row by primary key."""
        t0 = time.monotonic()
        try:
            async with self._db.pool.acquire() as conn:
                row = await conn.fetchrow(
                    f"SELECT {self._column_list()} FROM {self._table} WHERE id = $1",
                    id,
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > 500:
                self._log.warning("slow_query", ms=round(dur), method="get", id=id)
            if row is None:
                return None
            return self._model_cls.from_row(row)  # type: ignore[attr-defined]
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
                if filters:
                    keys = list(filters.keys())
                    where = self._placeholder_clause(keys)
                    sql = f"SELECT {self._column_list()} FROM {self._table} WHERE {where}"
                    rows = await conn.fetch(sql, *filters.values())
                else:
                    sql = f"SELECT {self._column_list()} FROM {self._table}"
                    rows = await conn.fetch(sql)

            dur = (time.monotonic() - t0) * 1000
            if dur > 500:
                self._log.warning("slow_query", ms=round(dur), method="list")
            return [self._model_cls.from_row(r) for r in rows]  # type: ignore[attr-defined]
        except Exception:
            self._log.exception("list_failed")
            raise

    async def create(self, model: T) -> int:
        """Insert a new row and return the generated id (via RETURNING)."""
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
            if dur > 500:
                self._log.warning("slow_query", ms=round(dur), method="create")
            self._log.info("row_created", id=last_id)
            return last_id  # type: ignore[return-value]
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
            set_clause = self._placeholder_clause(keys)
            # $N placeholders: last one is id
            set_clause = self._placeholder_clause(keys, start=1)
            values = list(updates.values())
            id_placeholder = f"${len(values) + 1}"
            async with self._db.pool.acquire() as conn:
                await conn.execute(
                    f"UPDATE {self._table} SET {set_clause} WHERE id = {id_placeholder}",
                    *values,
                    id,
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > 500:
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
                await conn.execute(
                    f"DELETE FROM {self._table} WHERE id = $1",
                    id,
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > 500:
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
                if filters:
                    keys = list(filters.keys())
                    where = self._placeholder_clause(keys)
                    row = await conn.fetchval(
                        f"SELECT COUNT(*) FROM {self._table} WHERE {where}",
                        *filters.values(),
                    )
                else:
                    row = await conn.fetchval(
                        f"SELECT COUNT(*) FROM {self._table}"
                    )
            dur = (time.monotonic() - t0) * 1000
            if dur > 500:
                self._log.warning("slow_query", ms=round(dur), method="count")
            return row if row is not None else 0  # type: ignore[return-value]
        except Exception:
            self._log.exception("count_failed")
            raise


# ---------------------------------------------------------------------------
# Domain-specific repositories
# ---------------------------------------------------------------------------


class AccountRepository(BaseRepository[AccountModel]):
    """Repository for trading account records."""

    def __init__(
        self,
        db_manager: DatabaseManager,
        model_cls: Optional[type[AccountModel]] = None,
    ) -> None:
        super().__init__(db_manager, model_cls or AccountModel)

    async def get_by_exchange(self, exchange: str) -> Optional[AccountModel]:
        """Return the account for a given exchange name."""
        results = await self.list(exchange=exchange)
        return results[0] if results else None

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

        Uses ``INSERT ... ON CONFLICT DO UPDATE`` for PostgreSQL.

        Returns the row id.
        """
        t0 = time.monotonic()
        try:
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
            if dur > 500:
                self._log.warning("slow_query", ms=round(dur), method="upsert_account")
            return last_id  # type: ignore[return-value]
        except Exception:
            self._log.exception("upsert_account_failed")
            raise


class PositionRepository(BaseRepository[PositionModel]):
    """Repository for trading positions."""

    def __init__(
        self,
        db_manager: DatabaseManager,
        model_cls: Optional[type[PositionModel]] = None,
    ) -> None:
        super().__init__(db_manager, model_cls or PositionModel)

    async def get_open(self) -> list[PositionModel]:
        """Return all positions with status ``'OPEN'``."""
        return await self.list(status="OPEN")

    async def get_by_strategy(self, strategy: str) -> list[PositionModel]:
        """Return positions opened by a specific strategy."""
        return await self.list(strategy=strategy)

    async def get_by_contract(self, symbol: str) -> Optional[PositionModel]:
        """Return the position for a given option contract symbol."""
        results = await self.list(contract_symbol=symbol)
        return results[0] if results else None

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
        db_manager: DatabaseManager,
        model_cls: Optional[type[OrderModel]] = None,
    ) -> None:
        super().__init__(db_manager, model_cls or OrderModel)

    async def get_open(self) -> list[OrderModel]:
        """Return orders that are still active (NEW or PARTIALLY_FILLED)."""
        t0 = time.monotonic()
        try:
            async with self._db.pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT {self._column_list()} FROM {self._table} "
                    f"WHERE status IN ('NEW', 'PARTIALLY_FILLED')",
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > 500:
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
        filled_qty: Optional[str] = None,
        avg_price: Optional[str] = None,
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
                    f"ORDER BY created_at DESC LIMIT $1",
                    limit,
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > 500:
                self._log.warning("slow_query", ms=round(dur), method="get_recent")
            return [OrderModel.from_row(r) for r in rows]
        except Exception:
            self._log.exception("get_recent_orders_failed")
            raise


class TradeRepository(BaseRepository[TradeModel]):
    """Repository for executed trades."""

    def __init__(
        self,
        db_manager: DatabaseManager,
        model_cls: Optional[type[TradeModel]] = None,
    ) -> None:
        super().__init__(db_manager, model_cls or TradeModel)

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
                    f"ORDER BY timestamp DESC LIMIT $1",
                    limit,
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > 500:
                self._log.warning("slow_query", ms=round(dur), method="get_recent")
            return [TradeModel.from_row(r) for r in rows]
        except Exception:
            self._log.exception("get_recent_trades_failed")
            raise

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
                    f"WHERE timestamp >= $1 AND timestamp <= $2 "
                    f"ORDER BY timestamp ASC",
                    start, end,
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > 500:
                self._log.warning("slow_query", ms=round(dur), method="get_by_date_range")
            return [TradeModel.from_row(r) for r in rows]
        except Exception:
            self._log.exception("get_trades_by_date_range_failed")
            raise


class DecisionRepository(BaseRepository[DecisionModel]):
    """Repository for strategy decision records."""

    def __init__(
        self,
        db_manager: DatabaseManager,
        model_cls: Optional[type[DecisionModel]] = None,
    ) -> None:
        super().__init__(db_manager, model_cls or DecisionModel)

    async def get_recent(self, limit: int = 20) -> list[DecisionModel]:
        """Return the most recent *limit* decisions by timestamp."""
        t0 = time.monotonic()
        try:
            async with self._db.pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT {self._column_list()} FROM {self._table} "
                    f"ORDER BY timestamp DESC LIMIT $1",
                    limit,
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > 500:
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
                    f"WHERE timestamp >= $1 AND timestamp <= $2 "
                    f"ORDER BY timestamp ASC",
                    start, end,
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > 500:
                self._log.warning("slow_query", ms=round(dur), method="get_by_date_range")
            return [DecisionModel.from_row(r) for r in rows]
        except Exception:
            self._log.exception("get_decisions_by_date_range_failed")
            raise


class OptionsContractRepository(BaseRepository[OptionContractModel]):
    """Repository for option contract snapshots."""

    def __init__(
        self,
        db_manager: DatabaseManager,
        model_cls: Optional[type[OptionContractModel]] = None,
    ) -> None:
        super().__init__(db_manager, model_cls or OptionContractModel)

    async def get_by_symbol(self, symbol: str) -> Optional[OptionContractModel]:
        """Return a contract by its unique symbol."""
        results = await self.list(symbol=symbol)
        return results[0] if results else None

    async def get_by_expiry(self, expiry: int) -> list[OptionContractModel]:
        """Return all contracts expiring at a given timestamp."""
        return await self.list(expiry=expiry)

    async def get_active(self) -> list[OptionContractModel]:
        """Return contracts with non-zero volume."""
        t0 = time.monotonic()
        try:
            async with self._db.pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT {self._column_list()} FROM {self._table} "
                    f"WHERE volume > '0' ORDER BY expiry ASC",
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > 500:
                self._log.warning("slow_query", ms=round(dur), method="get_active")
            return [OptionContractModel.from_row(r) for r in rows]
        except Exception:
            self._log.exception("get_active_contracts_failed")
            raise

    async def upsert_contract(self, contract: OptionContractModel) -> int:
        """Insert or update a contract record (by symbol).

        Uses ``INSERT ... ON CONFLICT DO UPDATE`` since ``symbol`` is UNIQUE.
        """
        t0 = time.monotonic()
        try:
            columns = self._column_list()
            placeholders = self._param_placeholders()
            set_pairs = self._column_set_pairs(self._columns)
            async with self._db.pool.acquire() as conn:
                last_id = await conn.fetchval(
                    f"INSERT INTO {self._table} ({columns}) "
                    f"VALUES ({placeholders}) "
                    f"ON CONFLICT (symbol) DO UPDATE SET {set_pairs} "
                    f"RETURNING id",
                    *contract.to_row(),
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > 500:
                self._log.warning("slow_query", ms=round(dur), method="upsert_contract")
            return last_id  # type: ignore[return-value]
        except Exception:
            self._log.exception("upsert_contract_failed")
            raise


class SessionRepository(BaseRepository[SessionModel]):
    """Repository for trading sessions."""

    def __init__(
        self,
        db_manager: DatabaseManager,
        model_cls: Optional[type[SessionModel]] = None,
    ) -> None:
        super().__init__(db_manager, model_cls or SessionModel)

    async def get_latest(self) -> Optional[SessionModel]:
        """Return the most recently started session."""
        t0 = time.monotonic()
        try:
            async with self._db.pool.acquire() as conn:
                row = await conn.fetchrow(
                    f"SELECT {self._column_list()} FROM {self._table} "
                    f"ORDER BY start_time DESC LIMIT 1",
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > 500:
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
        import time as _time

        now = int(_time.time() * 1000)
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
        db_manager: DatabaseManager,
        model_cls: Optional[type[PerformanceSnapshotModel]] = None,
    ) -> None:
        super().__init__(db_manager, model_cls or PerformanceSnapshotModel)

    async def get_recent(self, limit: int = 20) -> list[PerformanceSnapshotModel]:
        """Return the most recent *limit* snapshots by timestamp."""
        t0 = time.monotonic()
        try:
            async with self._db.pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT {self._column_list()} FROM {self._table} "
                    f"ORDER BY timestamp DESC LIMIT $1",
                    limit,
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > 500:
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
                    f"WHERE timestamp >= $1 AND timestamp <= $2 "
                    f"ORDER BY timestamp ASC",
                    start, end,
                )
            dur = (time.monotonic() - t0) * 1000
            if dur > 500:
                self._log.warning("slow_query", ms=round(dur), method="get_by_date_range")
            return [PerformanceSnapshotModel.from_row(r) for r in rows]
        except Exception:
            self._log.exception("get_snapshots_by_date_range_failed")
            raise
