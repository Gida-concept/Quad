"""Database manager for the Quad options trading bot.

Provides a production-grade async PostgreSQL wrapper built on asyncpg,
with connection pooling, automatic migrations, backup stubs, and context
manager support.
"""

from __future__ import annotations

import asyncio
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import asyncpg
import structlog

from .models import ALL_MODELS, INDEX_DEFINITIONS, SCHEMA_MIGRATIONS, SCHEMA_VERSION, SCHEMA_VERSION_TABLE_DDL

logger = structlog.get_logger(__name__)


class DatabaseManager:
    """Async PostgreSQL database manager with connection pooling and migrations.

    Typical usage::

        async with DatabaseManager("postgresql://user:pass@host:5432/dbname") as db:
            async with db.pool.acquire() as conn:
                val = await conn.fetchval("SELECT 1")

    Parameters
    ----------
    dsn:
        PostgreSQL connection string, e.g.
        ``"postgresql://quad:quad@localhost:5432/quad"``.
    min_pool_size:
        Minimum number of connections in the pool (default 1).
    max_pool_size:
        Maximum number of connections in the pool (default 5).
    """

    def __init__(
        self,
        dsn: str,
        min_pool_size: int = 1,
        max_pool_size: int = 5,
    ) -> None:
        self._dsn = dsn
        self._min_pool_size = min_pool_size
        self._max_pool_size = max_pool_size
        self._pool: Optional[asyncpg.Pool] = None
        self._log = logger.bind(dsn=self._mask_dsn(dsn))

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def dsn(self) -> str:
        """Return the PostgreSQL DSN string."""
        return self._dsn

    @property
    def pool(self) -> asyncpg.Pool:
        """Return the connection pool.

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
        return self._pool is not None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self, ssl: str | bool | None = None) -> None:
        """Create the asyncpg connection pool.

        Parameters
        ----------
        ssl:
            SSL/TLS mode passed through to asyncpg.  Accepts the same types
            asyncpg does (``True``, ``"require"``, ``"prefer"``, etc.).
            If *None*, auto-detects ``sslmode=require`` from the DSN query
            string and enables SSL when found.
        """
        if self._pool is not None:
            self._log.warning("connect_already_open")
            return

        # Auto-detect SSL from DSN query parameters
        if ssl is None:
            parsed = urllib.parse.urlparse(self._dsn)
            qs = urllib.parse.parse_qs(parsed.query)
            if qs.get("sslmode", [None])[0] == "require":
                ssl = True

        self._log.info(
            "connecting",
            min_size=self._min_pool_size,
            max_size=self._max_pool_size,
            ssl=bool(ssl),
        )

        for attempt in range(1, 6):
            try:
                self._pool = await asyncpg.create_pool(
                    dsn=self._dsn,
                    min_size=self._min_pool_size,
                    max_size=self._max_pool_size,
                    command_timeout=60,
                    ssl=ssl,
                )
                break
            except Exception as exc:
                if attempt == 5:
                    self._log.error(
                        "connect_retries_exhausted",
                        max_attempts=5,
                        error=str(exc),
                    )
                    raise
                sleep_secs = 2 ** (attempt - 1)  # 1, 2, 4, 8
                self._log.warning(
                    "connect_retry",
                    attempt=attempt,
                    max_attempts=5,
                    backoff_seconds=sleep_secs,
                    error=str(exc),
                )
                await asyncio.sleep(sleep_secs)

        self._log.info("connected", ssl=bool(ssl))

    async def disconnect(self) -> None:
        """Close the connection pool gracefully."""
        if self._pool is None:
            self._log.warning("disconnect_not_connected")
            return

        self._log.info("disconnecting")
        await self._pool.close()
        self._pool = None
        self._log.info("disconnected")

    async def is_healthy(self) -> bool:
        """Check whether the database pool is responsive.

        Acquires a connection, runs ``SELECT 1``, and releases it.
        Returns ``True`` if the query succeeds, ``False`` otherwise (no
        exception is raised).
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

        async with pool.acquire() as conn:
            async with conn.transaction():
                # Create schema version table first (needed by migrate)
                await conn.execute(SCHEMA_VERSION_TABLE_DDL)
                self._log.debug("schema_version_table_ensured")

                # Create tables
                for model_cls in ALL_MODELS:
                    ddl = model_cls.create_table_ddl()
                    try:
                        await conn.execute(ddl)
                        self._log.debug("table_created", table=model_cls.__tablename__)
                    except Exception:
                        self._log.exception(
                            "table_create_failed",
                            table=model_cls.__tablename__,
                        )
                        raise

                # Create indexes
                for idx_ddl in INDEX_DEFINITIONS:
                    try:
                        await conn.execute(idx_ddl)
                    except Exception:
                        self._log.exception("index_create_failed", index=idx_ddl)
                        raise

        self._log.info("initialized", tables=len(ALL_MODELS), indexes=len(INDEX_DEFINITIONS))

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
            row = await conn.fetchval(
                "SELECT MAX(version) FROM _schema_version"
            )
            current_version = row if row is not None else 0

            if current_version >= SCHEMA_VERSION:
                self._log.info(
                    "schema_up_to_date", current=current_version, target=SCHEMA_VERSION
                )
                return

            # Apply migrations in order within a transaction
            async with conn.transaction():
                for version in range(current_version + 1, SCHEMA_VERSION + 1):
                    statements = SCHEMA_MIGRATIONS.get(version, [])
                    if statements:
                        self._log.info("applying_migration", version=version)
                        for stmt in statements:
                            await conn.execute(stmt)

                    # Record this version
                    await conn.execute(
                        "INSERT INTO _schema_version (version) VALUES ($1)",
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

        Returns the asyncpg command status tag (e.g. ``"INSERT 0 1"``).
        """
        async with self.pool.acquire() as conn:
            return await conn.execute(sql, *args)

    # ------------------------------------------------------------------
    # Backup & snapshot (stubs for PostgreSQL)
    # ------------------------------------------------------------------

    async def backup(self, backup_dir: str | Path) -> None:
        """Stub: PostgreSQL backup is a no-op that logs the intent.

        Production backups should use pg_dump or a cloud-native solution
        (e.g. Fly.io Postgres snapshots, ``pg_dump`` cron job, or
        ``pgBackRest``).

        Parameters
        ----------
        backup_dir:
            Ignored; retained for API compatibility.
        """
        self._log.warning(
            "pg_backup_not_implemented",
            message=(
                "PostgreSQL backup is not implemented in-app. "
                "Use pg_dump, Fly.io automatic backups, or a cloud-native "
                "solution for production backups."
            ),
            backup_dir=str(backup_dir),
        )

    async def snapshot(self) -> None:
        """Stub: PostgreSQL snapshot is a no-op that logs the intent.

        For point-in-time snapshots, use PostgreSQL native ``pg_dump``
        or cloud provider snapshot features.
        """
        self._log.warning(
            "pg_snapshot_not_implemented",
            message=(
                "PostgreSQL snapshot is not implemented in-app. "
                "Use pg_dump or cloud provider snapshots."
            ),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _mask_dsn(dsn: str) -> str:
        """Mask the password in a DSN for logging purposes."""
        if "@" in dsn:
            # postgresql://user:password@host:port/db -> postgresql://user:***@host:port/db
            before_at, after_at = dsn.split("@", 1)
            if ":" in before_at:
                scheme_user = before_at.split(":", 1)[0]
                return f"{scheme_user}:****@{after_at}"
        return dsn

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> DatabaseManager:
        await self.connect()
        await self.initialize()
        await self.migrate()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.disconnect()
