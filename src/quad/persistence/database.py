"""Database manager for the Quad futures trading bot.

Provides an async SQLite wrapper built on aiosqlite,
with automatic migrations, backup stubs, and context
manager support.
"""

from __future__ import annotations

import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite
import structlog
from typing_extensions import Self

from .models import (
    ALL_MODELS,
    INDEX_DEFINITIONS,
    SCHEMA_MIGRATIONS,
    SCHEMA_VERSION,
    SCHEMA_VERSION_TABLE_DDL,
)

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# SQLite compatibility layer — translates asyncpg-style $N params to SQLite ?
# ---------------------------------------------------------------------------

_PARAM_RE = re.compile(r"\$(\d+)")

# Regex to detect ALTER TABLE ... ADD COLUMN statements so we can check
# whether the column already exists before running them (idempotent
# migrations).  Group 1 = table name, Group 2 = column name.
_ALTER_ADD_COL_RE = re.compile(
    r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+(?:COLUMN\s+)?(\w+)\s+",
    re.IGNORECASE,
)


def _rewrite_params(query: str, params: tuple) -> tuple[str, tuple | None]:
    """Convert PostgreSQL $N parameter style to SQLite ? style."""
    if not params:
        return query, None
    new_query = _PARAM_RE.sub("?", query)
    return new_query, params


class _SQLiteConnection:
    """Wraps an aiosqlite connection to mimic asyncpg's Connection interface.

    Provides fetch / fetchrow / fetchval / execute / transaction with
    automatic $N to ? parameter translation so existing repository code
    (written for PostgreSQL) works without changes.
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def fetch(self, query: str, *params: Any) -> list[Any]:
        """Run a query and return all result rows (list of sqlite3.Row)."""
        q, p = _rewrite_params(query, params)
        cursor = await self._conn.execute(q, p or ())
        rows = await cursor.fetchall()
        await cursor.close()
        return list(rows)

    async def fetchrow(self, query: str, *params: Any) -> Any | None:
        """Run a query and return the first result row, or None."""
        q, p = _rewrite_params(query, params)
        cursor = await self._conn.execute(q, p or ())
        row = await cursor.fetchone()
        await cursor.close()
        return row

    async def fetchval(self, query: str, *params: Any) -> Any | None:
        """Run a query and return the first column of the first row."""
        q, p = _rewrite_params(query, params)
        cursor = await self._conn.execute(q, p or ())
        row = await cursor.fetchone()
        await cursor.close()
        return row[0] if row else None

    async def execute(self, query: str, *params: Any) -> str:
        """Run a statement and return a status string (rowcount or "OK")."""
        q, p = _rewrite_params(query, params)
        cursor = await self._conn.execute(q, p or ())
        rc = cursor.rowcount
        await cursor.close()
        return str(rc) if rc is not None and rc >= 0 else "OK"

    @asynccontextmanager
    async def transaction(self):
        """Context manager wrapping a BEGIN/COMMIT pair.

        Rolls back on exception.
        """
        await self._conn.execute("BEGIN")
        try:
            yield
            await self._conn.execute("COMMIT")
        except Exception:
            await self._conn.execute("ROLLBACK")
            raise


class _SQLitePool:
    """Lightweight pool wrapper -- single aiosqlite connection.

    SQLite does not benefit from multiple concurrent writers, so one
    connection is sufficient.  acquire() returns a _SQLiteConnection
    that proxies the underlying connection.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    def _ensure(self) -> aiosqlite.Connection:
        """Open the connection if not already open (non-blocking via aiosqlite).

        Uses ``aiosqlite.connect()`` which returns immediately and runs the
        actual SQLite connection on a background thread, avoiding event-loop
        blocking that raw ``sqlite3.connect()`` would cause.
        """
        if self._conn is None:
            if self._db_path != ":memory:":
                db_dir = os.path.dirname(os.path.abspath(self._db_path))
                if db_dir:
                    os.makedirs(db_dir, exist_ok=True)
            self._conn = aiosqlite.connect(
                self._db_path,
                isolation_level=None,  # autocommit mode — explicit BEGIN/COMMIT
            )
        return self._conn

    async def _init_connection(self) -> None:
        """Await the connection (starts its background thread) and run PRAGMAs.

        Must be called exactly once, before any execute() calls.
        """
        conn = self._ensure()
        await conn  # start the background thread
        if self._db_path != ":memory:":
            await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.execute("PRAGMA foreign_keys=ON")

    @asynccontextmanager
    async def acquire(self):
        """Acquire a wrapped connection (context manager).

        Initializes the connection on first call (starts the background
        thread and runs PRAGMAs).  Subsequent calls reuse the same
        underlying aiosqlite connection.
        """
        raw = self._ensure()
        if not hasattr(self, "_inited"):
            await self._init_connection()
            self._inited = True
        yield _SQLiteConnection(raw)

    @property
    def is_open(self) -> bool:
        return self._conn is not None

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None


# ============================================================================
# DatabaseManager
# ============================================================================


class DatabaseManager:
    """Async SQLite database manager with connection lifecycle and migrations.

    Typical usage::

        async with DatabaseManager("quad.db") as db:
            async with db.pool.acquire() as conn:
                val = await conn.fetchval("SELECT 1")

    Parameters
    ----------
    dsn:
        SQLite database file path, e.g. ``"quad.db"`` or an absolute path.
        Relative paths are resolved from the process working directory.
    min_pool_size:
        Ignored for SQLite (single-connection pool); kept for API
        compatibility with the orchestrator.
    max_pool_size:
        Ignored for SQLite; kept for API compatibility.
    """

    def __init__(
        self,
        dsn: str,
        min_pool_size: int | None = None,
        max_pool_size: int | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        self._dsn = dsn
        self._db_config = config or {}
        self._pool: _SQLitePool | None = None
        self._log = logger.bind(dsn=self._dsn)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def dsn(self) -> str:
        """Return the database path string."""
        return self._dsn

    @property
    def pool(self) -> _SQLitePool:
        """Return the pool.

        Raises
        ------
        RuntimeError
            If the pool has not been created yet.
        """
        if self._pool is None:
            raise RuntimeError(
                "Connection pool is not open. Call connect() or use the "
                "async context manager first."
            )
        return self._pool

    @property
    def is_connected(self) -> bool:
        """Return True if the pool is active."""
        return self._pool is not None and self._pool.is_open

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self, ssl: str | bool | None = None) -> None:
        """Open the SQLite database and create the pool.

        Parameters
        ----------
        ssl:
            Ignored for SQLite (local file). Accepted for API compatibility
            with the orchestrator.
        """
        if self._pool is not None:
            self._log.warning("connect_already_open")
            return

        self._log.info("connecting_sqlite", path=self._dsn)

        # Resolve relative paths (:memory: is a special SQLite value)
        db_path = self._dsn
        if db_path != ":memory:" and not Path(db_path).is_absolute():
            db_path = str(Path.cwd() / db_path)

        self._pool = _SQLitePool(db_path)
        # Test the connection immediately
        async with self._pool.acquire() as conn:
            val = await conn.fetchval("SELECT 1")
            if val != 1:
                raise RuntimeError(f"SQLite connection test failed for {db_path}")

        self._log.info("connected_sqlite", path=db_path)

    async def disconnect(self) -> None:
        """Close the database connection."""
        if self._pool is None:
            self._log.warning("disconnect_not_connected")
            return

        self._log.info("disconnecting")
        await self._pool.close()
        self._pool = None
        self._log.info("disconnected")

    async def is_healthy(self) -> bool:
        """Check whether the database is responsive.

        Runs ``SELECT 1`` and returns True on success.
        """
        if self._pool is None:
            self._log.warning("health_check_no_pool")
            return False

        try:
            async with self._pool.acquire() as conn:
                val = await conn.fetchval("SELECT 1")
                return val == 1
        except Exception:
            self._log.warning("health_check_failed", exc_info=True)
            return False

    async def initialize(self) -> None:
        """Create all tables, indexes, and schema version table if they
        do not exist yet.

        Calls :meth:`connect` implicitly if no pool is open.
        """
        if self._pool is None:
            await self.connect()

        pool = self._pool
        assert pool is not None

        async with pool.acquire() as conn, conn.transaction():
            # Create schema version table first (needed by migrate)
            await conn.execute(SCHEMA_VERSION_TABLE_DDL)
            self._log.debug("schema_version_table_ensured")

            # Create tables
            for model_cls in ALL_MODELS:
                model_any: Any = model_cls
                ddl = model_any.create_table_ddl()
                try:
                    await conn.execute(ddl)
                    self._log.debug("table_created", table=model_any.__tablename__)
                except Exception:
                    self._log.exception(
                        "table_create_failed",
                        table=model_any.__tablename__,
                    )
                    raise

            # Create indexes
            for idx_ddl in INDEX_DEFINITIONS:
                try:
                    await conn.execute(idx_ddl)
                except Exception:
                    self._log.exception("index_create_failed", index=idx_ddl)
                    raise

        self._log.info(
            "initialized",
            tables=len(ALL_MODELS),
            indexes=len(INDEX_DEFINITIONS),
        )

    async def migrate(self) -> None:
        """Apply pending schema migrations.

        Uses a ``_schema_version`` table to track the current schema version
        and applies any outstanding migrations from :data:`SCHEMA_MIGRATIONS`.
        """
        if self._pool is None:
            await self.connect()

        pool = self._pool
        assert pool is not None

        async with pool.acquire() as conn:
            # Read current schema version
            row = await conn.fetchval("SELECT MAX(version) FROM _schema_version")
            current_version = row if row is not None else 0

            if current_version >= SCHEMA_VERSION:
                self._log.info(
                    "schema_up_to_date",
                    current=current_version,
                    target=SCHEMA_VERSION,
                )
                return

            # Apply migrations in order within a transaction
            async with conn.transaction():
                for version in range(current_version + 1, SCHEMA_VERSION + 1):
                    statements = SCHEMA_MIGRATIONS.get(version, [])
                    if statements:
                        self._log.info("applying_migration", version=version)
                        for stmt in statements:
                            # Idempotent ALTER TABLE: before adding a
                            # column, check that it doesn't already exist.
                            # This handles the case where initialize() has
                            # already created the table with the column
                            # included in the base DDL.
                            m = _ALTER_ADD_COL_RE.match(stmt)
                            if m:
                                table_name, col_name = m.group(1), m.group(2)
                                rows = await conn.fetch(
                                    f"PRAGMA table_info({table_name})"
                                )
                                col_exists = any(row[1] == col_name for row in rows)
                                if col_exists:
                                    self._log.debug(
                                        "column_already_exists_skipping",
                                        table=table_name,
                                        column=col_name,
                                    )
                                    continue
                            await conn.execute(stmt)

                    # Record this version
                    await conn.execute(
                        "INSERT INTO _schema_version (version) VALUES (?)",
                        version,
                    )

        self._log.info(
            "migration_complete",
            from_version=current_version,
            to_version=SCHEMA_VERSION,
        )

    # ------------------------------------------------------------------
    # Execute convenience method
    # ------------------------------------------------------------------

    async def execute(self, sql: str, *args: Any) -> str:
        """Execute a SQL statement on a connection from the pool.

        This is a convenience wrapper for callers that have a simple
        execute-and-forget pattern (e.g. logging an AI decision).
        """
        async with self.pool.acquire() as conn:
            return await conn.execute(sql, *args)

    # ------------------------------------------------------------------
    # Backup & snapshot (stubs for SQLite)
    # ------------------------------------------------------------------

    async def backup(self, backup_dir: str | Path) -> None:
        """Create a timestamped backup of the SQLite database file.

        Uses aiosqlite's backup API to create a consistent snapshot of the
        database while it may be under active use.  The backup file is named
        ``quad_YYYYMMDD_HHMMSS.db`` and placed in *backup_dir*.

        Parameters
        ----------
        backup_dir:
            Directory path for the backup file.  Created automatically if it
            does not exist.
        """
        if self._pool is None:
            self._log.warning("backup_no_pool")
            return

        backup_path = Path(backup_dir)
        backup_path.mkdir(parents=True, exist_ok=True)

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        dest_file = backup_path / f"quad_{timestamp}.db"

        try:
            # Access the raw aiosqlite connection for the backup
            raw = self._pool._ensure()
            await raw  # ensure the background thread is started
            if raw is None:
                self._log.warning("backup_no_connection")
                return

            # Backup via aiosqlite's .backup() method
            dest = await aiosqlite.connect(str(dest_file))
            try:
                await raw.backup(dest)
                self._log.info(
                    "backup_completed",
                    source=self._dsn,
                    destination=str(dest_file),
                )
            finally:
                await dest.close()
        except Exception as exc:
            self._log.exception("backup_failed", error=str(exc))

    async def snapshot(self, snapshot_name: str = "") -> None:
        """Create a named snapshot of the database.

        Uses ``VACUUM INTO`` to write a consistent point-in-time copy.
        The snapshot is saved alongside the main database file with the
        name ``<db_stem>_<snapshot_name>.db``.

        Parameters
        ----------
        snapshot_name:
            Name for the snapshot (e.g. ``"pre_optimization"``).  If empty,
            a timestamp-based name is used.
        """
        if self._pool is None:
            self._log.warning("snapshot_no_pool")
            return

        db_path = self._dsn
        if db_path == ":memory:":
            self._log.warning("snapshot_memory_db")
            return

        name = snapshot_name or time.strftime("snapshot_%Y%m%d_%H%M%S")
        db_stem = Path(db_path).stem
        dest_file = Path(db_path).parent / f"{db_stem}_{name}.db"

        try:
            async with self._pool.acquire() as conn:
                # VACUUM INTO creates a consistent point-in-time copy
                await conn.execute(f"VACUUM INTO '{dest_file}'")
                self._log.info(
                    "snapshot_completed",
                    destination=str(dest_file),
                    name=name,
                )
        except Exception as exc:
            self._log.exception("snapshot_failed", error=str(exc))

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> Self:
        await self.connect()
        await self.initialize()
        await self.migrate()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.disconnect()
