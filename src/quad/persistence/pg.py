"""PostgreSQL backend for quad-api (Hetzner).

Repository code already speaks asyncpg-style ``$N`` parameters; this module
provides an asyncpg pool with the same surface as the SQLite pool plus a
``PostgresDatabaseManager`` covering connect/initialize/migrate. DDL is
translated (``AUTOINCREMENT`` -> ``SERIAL``, SQLite clock defaults -> ``now()``).

Use :func:`create_database` to route by DSN scheme: ``postgresql://`` goes
here, anything else stays on SQLite (dev default).
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from typing import Any

import structlog

from .models import (
    ALL_MODELS,
    INDEX_DEFINITIONS,
    SCHEMA_MIGRATIONS,
    SCHEMA_VERSION,
    SCHEMA_VERSION_TABLE_DDL,
)

logger = structlog.get_logger(__name__)

_ALTER_ADD_COL_RE = re.compile(
    r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+(?:COLUMN\s+)?(\w+)\s+",
    re.IGNORECASE,
)
_DROP_COL_RE = re.compile(
    r"ALTER\s+TABLE\s+(\w+)\s+DROP\s+(?:COLUMN\s+)?(\w+)",
    re.IGNORECASE,
)


def translate_ddl(ddl: str) -> str:
    """Translate SQLite DDL to PostgreSQL."""
    out = ddl.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
    out = out.replace("DEFAULT (datetime('now'))", "DEFAULT now()")
    # SQLite BLOB affinity has no direct PG equivalent; BYTEA is the
    # byte-string type.
    out = re.sub(r"\bBLOB\b", "BYTEA", out, flags=re.IGNORECASE)
    # NOTE: bare `INTEGER PRIMARY KEY` (no AUTOINCREMENT, a rowid alias in
    # SQLite) is intentionally kept as-is: PG treats it as a plain integer
    # PK and the app always supplies explicit ids there.
    if re.search(r"datetime\s*\(|AUTOINCREMENT", out, re.IGNORECASE):
        logger.warning("ddl_untranslated_sqlite", ddl=out)
    return out


class _AsyncpgConnection:
    """Thin wrapper so call sites can share code with the SQLite pool."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def fetch(self, query: str, *params: Any) -> list[Any]:
        rows = await self._conn.fetch(query, *params)
        return list(rows)

    async def fetchrow(self, query: str, *params: Any) -> Any | None:
        return await self._conn.fetchrow(query, *params)

    async def fetchval(self, query: str, *params: Any) -> Any | None:
        return await self._conn.fetchval(query, *params)

    async def execute(self, query: str, *params: Any) -> str:
        return await self._conn.execute(query, *params)

    def transaction(self) -> Any:
        return self._conn.transaction()


class AsyncpgPool:
    """asyncpg connection pool with the _SQLitePool acquire() surface."""

    def __init__(self, dsn: str, **kwargs: Any) -> None:
        self._dsn = dsn
        self._kwargs = kwargs
        self._pool: Any = None

    @property
    def is_open(self) -> bool:
        return self._pool is not None

    async def connect(self) -> None:
        import asyncpg

        self._pool = await asyncpg.create_pool(self._dsn, **self._kwargs)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @asynccontextmanager
    async def acquire(self) -> Any:
        assert self._pool is not None, "pool is not open; call connect() first"
        async with self._pool.acquire() as conn:
            yield _AsyncpgConnection(conn)


class PostgresDatabaseManager:
    """DatabaseManager-compatible facade over :class:`AsyncpgPool`."""

    def __init__(self, dsn: str, **pool_kwargs: Any) -> None:
        self._dsn = dsn
        self._pool: AsyncpgPool | None = None
        self._pool_kwargs = pool_kwargs
        self._log = logger.bind(dsn="postgresql://<redacted>")

    @property
    def dsn(self) -> str:
        return self._dsn

    @property
    def pool(self) -> AsyncpgPool:
        if self._pool is None:
            raise RuntimeError("Connection pool is not open. Call connect() first.")
        return self._pool

    @property
    def is_connected(self) -> bool:
        return self._pool is not None and self._pool.is_open

    async def connect(self, ssl: Any = None) -> None:
        if self._pool is not None:
            return
        self._pool = AsyncpgPool(self._dsn, **self._pool_kwargs)
        await self._pool.connect()

    async def disconnect(self) -> None:
        if self._pool is None:
            return
        await self._pool.close()
        self._pool = None

    async def is_healthy(self) -> bool:
        try:
            if self._pool is None:
                return False
            async with self._pool.acquire() as conn:
                return (await conn.fetchval("SELECT 1")) == 1
        except Exception:
            return False

    async def initialize(self) -> None:
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(translate_ddl(SCHEMA_VERSION_TABLE_DDL))
            for model_cls in ALL_MODELS:
                await conn.execute(translate_ddl(model_cls.create_table_ddl()))
            for idx_ddl in INDEX_DEFINITIONS:
                await conn.execute(idx_ddl)

    async def _column_exists(self, conn: Any, table: str, column: str) -> bool:
        row = await conn.fetchrow(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = $1 AND column_name = $2",
            table,
            column,
        )
        return row is not None

    async def migrate(self) -> None:
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self.pool.acquire() as conn:
            current = await conn.fetchval("SELECT MAX(version) FROM _schema_version")
            current_version = current if current is not None else 0
            if current_version >= SCHEMA_VERSION:
                return
            async with conn.transaction():
                for version in range(current_version + 1, SCHEMA_VERSION + 1):
                    for stmt in SCHEMA_MIGRATIONS.get(version, []):
                        m = _ALTER_ADD_COL_RE.match(stmt)
                        if m and await self._column_exists(
                            conn, m.group(1), m.group(2)
                        ):
                            continue
                        d = None if m else _DROP_COL_RE.match(stmt)
                        if d and not await self._column_exists(
                            conn, d.group(1), d.group(2)
                        ):
                            continue
                        await conn.execute(stmt)
                    await conn.execute(
                        "INSERT INTO _schema_version (version) VALUES ($1)", version
                    )

    async def execute(self, sql: str, *args: Any) -> str:
        async with self.pool.acquire() as conn:
            return await conn.execute(sql, *args)

    async def backup(self, backup_dir: Any) -> None:
        raise NotImplementedError("use pg_dump for Postgres backups")

    async def snapshot(self, snapshot_name: str = "") -> None:
        raise NotImplementedError("use pg_dump for Postgres snapshots")

    async def __aenter__(self) -> PostgresDatabaseManager:
        await self.connect()
        await self.initialize()
        await self.migrate()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.disconnect()


def create_database(dsn: str, **kwargs: Any) -> Any:
    """Route a DSN to the right manager (Postgres vs SQLite)."""
    from .database import DatabaseManager

    if dsn.startswith("postgresql://") or dsn.startswith("postgres://"):
        return PostgresDatabaseManager(dsn, **kwargs)
    return DatabaseManager(dsn, **kwargs)
