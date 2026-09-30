"""Tests for Phase-1 multi-tenancy (schema v5): tenants, encrypted credentials,
per-tenant config, Telegram bindings, pairing codes, tenant_id isolation,
and the v4 -> v5 migration path.
"""

import asyncio
import os
import sys
import time
import uuid
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from quad.persistence import (  # noqa: E402
    DatabaseManager,
    ExchangeCredentialRepository,
    PairingCodeRepository,
    TelegramBindingRepository,
    TenantConfigRepository,
    TenantRepository,
)
from quad.persistence.models import (  # noqa: E402
    ALL_MODELS,
    SCHEMA_VERSION,
    PairingCodeModel,
    PositionModel,
)
from quad.persistence.repositories import PositionRepository  # noqa: E402
from quad.security.secrets import (  # noqa: E402
    CredentialKeyError,
    decrypt_secret,
    encrypt_secret,
    generate_key,
)


@pytest.fixture()
def cred_key(monkeypatch):
    key = generate_key()
    monkeypatch.setenv("QUAD_CREDENTIAL_KEY", key)
    return key


def _run(coro):
    return asyncio.run(coro)


def _mem_db():
    return DatabaseManager(":memory:")


# ---------------------------------------------------------------------------
# Encryption helper
# ---------------------------------------------------------------------------


def test_encrypt_decrypt_roundtrip(cred_key):
    token = encrypt_secret("my-bybit-secret")
    assert token != "my-bybit-secret"
    assert decrypt_secret(token) == "my-bybit-secret"


def test_encrypt_refuses_empty(cred_key):
    with pytest.raises(ValueError):
        encrypt_secret("")


def test_decrypt_wrong_key_fails(cred_key):
    token = encrypt_secret("s3cr3t")
    os.environ["QUAD_CREDENTIAL_KEY"] = generate_key()
    with pytest.raises(CredentialKeyError):
        decrypt_secret(token)


def test_missing_key_fails(monkeypatch):
    monkeypatch.delenv("QUAD_CREDENTIAL_KEY", raising=False)
    with pytest.raises(CredentialKeyError):
        encrypt_secret("x")


# ---------------------------------------------------------------------------
# Fresh install: 21 tables at v5
# ---------------------------------------------------------------------------


def test_schema_version_is_5():
    assert SCHEMA_VERSION == 10
    assert len(ALL_MODELS) == 20


def test_fresh_init_creates_tenant_tables():
    async def go():
        async with _mem_db() as db:
            async with db.pool.acquire() as conn:
                v = await conn.fetchval("SELECT MAX(version) FROM _schema_version")
                assert v == 10
                for t in (
                    "tenants",
                    "exchange_credentials",
                    "tenant_config",
                    "telegram_bindings",
                    "pairing_codes",
                ):
                    cols = await conn.fetch(f"PRAGMA table_info({t})")
                    assert len(cols) > 3, t
                pcols = {r[1] for r in await conn.fetch("PRAGMA table_info(positions)")}
                assert "tenant_id" in pcols
                fcols = {
                    r[1]
                    for r in await conn.fetch("PRAGMA table_info(funding_rate_records)")
                }
                assert "tenant_id" not in fcols

    _run(go())


# ---------------------------------------------------------------------------
# Tenant lifecycle + isolation
# ---------------------------------------------------------------------------


def test_tenant_lifecycle_and_isolation(cred_key):
    async def go():
        async with _mem_db() as db:
            tenants = TenantRepository(db)
            creds = ExchangeCredentialRepository(db)
            cfgs = TenantConfigRepository(db)
            binds = TelegramBindingRepository(db)
            positions = PositionRepository(db)

            now = int(time.time() * 1000)
            t1 = await tenants.create_tenant(
                "tenant-aaa", telegram_user_id=111, username="alice"
            )
            await tenants.create_tenant("tenant-bbb")
            assert t1.tenant_uuid == "tenant-aaa"
            assert (await tenants.get_by_telegram_user(111)).id == t1.id
            assert await tenants.get_by_telegram_user(999) is None

            # Encrypted credential round-trip through the repo
            enc = await creds.upsert_encrypted(
                "tenant-aaa", encrypt_secret("KEY-A"), encrypt_secret("SEC-A")
            )
            assert enc.api_key_enc != "KEY-A"
            got = await creds.get_active("tenant-aaa")
            assert decrypt_secret(got.api_key_enc) == "KEY-A"
            assert decrypt_secret(got.api_secret_enc) == "SEC-A"
            # Upsert replaces, keeps single row
            await creds.upsert_encrypted(
                "tenant-aaa", encrypt_secret("KEY-A2"), encrypt_secret("SEC-A2")
            )
            assert await creds.count(tenant_id="tenant-aaa") == 1
            assert (
                decrypt_secret((await creds.get_active("tenant-aaa")).api_key_enc)
                == "KEY-A2"
            )
            assert await creds.get_active("tenant-bbb") is None

            # Config defaults then update
            cfg = await cfgs.get_or_default("tenant-aaa")
            assert cfg.market == "linear" and cfg.leverage == 10
            await cfgs.update(cfg.id, leverage=10, capital_pct_per_trade=1.5)
            assert (await cfgs.get_by_tenant("tenant-aaa")).leverage == 10

            # Bindings
            await binds.bind("tenant-aaa", chat_id=777)
            assert (await binds.get_by_chat_id(777)).tenant_uuid == "tenant-aaa"
            # Rebind moves the chat
            await binds.bind("tenant-bbb", chat_id=777)
            assert (await binds.get_by_chat_id(777)).tenant_uuid == "tenant-bbb"

            # Isolation: positions scoped by tenant_id
            for tuuid, sym in (("tenant-aaa", "BTCUSDT"), ("tenant-bbb", "ETHUSDT")):
                await positions.create(
                    PositionModel(
                        id=0,
                        strategy="trend_following",
                        symbol=sym,
                        side="BUY",
                        quantity="0.01",
                        entry_price="60000",
                        current_price="60000",
                        unrealized_pnl="0",
                        realized_pnl="0",
                        status="OPEN",
                        opened_at=now,
                        updated_at=now,
                        tenant_id=tuuid,
                    )
                )
            mine = await positions.list(tenant_id="tenant-aaa")
            assert [p.symbol for p in mine] == ["BTCUSDT"]
            assert await positions.count(tenant_id="tenant-bbb") == 1

    _run(go())


def test_pairing_code_flow():
    async def go():
        async with _mem_db() as db:
            repo = PairingCodeRepository(db)
            now = int(time.time() * 1000)
            code = uuid.uuid4().hex[:8]
            await repo.create(
                PairingCodeModel(
                    id=0,
                    tenant_uuid="tenant-aaa",
                    code=code,
                    expires_at=now + 600_000,
                    created_at=now,
                )
            )
            assert (await repo.get_valid(code, now)).tenant_uuid == "tenant-aaa"
            assert await repo.get_valid(code, now + 700_000) is None  # expired
            row = await repo.get_valid(code, now)
            await repo.mark_used(row.id, used_by_chat=777)
            assert await repo.get_valid(code, now) is None  # consumed

    _run(go())


# ---------------------------------------------------------------------------
# v4 -> v5 migration on a legacy-shaped table
# ---------------------------------------------------------------------------


def test_migration_adds_tenant_id_to_legacy_table():
    async def go():
        async with _mem_db() as db:
            async with db.pool.acquire() as conn:
                # Simulate a v4 database: no tenant index/column, roll version back
                await conn.execute("DROP INDEX IF EXISTS idx_positions_tenant")
                await conn.execute("ALTER TABLE positions DROP COLUMN tenant_id")
                await conn.execute(
                    "DELETE FROM _schema_version WHERE version IN (5, 6, 7, 8, 9, 10)"
                )
                cols = {r[1] for r in await conn.fetch("PRAGMA table_info(positions)")}
                assert "tenant_id" not in cols
            await db.migrate()
            async with db.pool.acquire() as conn:
                cols = {r[1] for r in await conn.fetch("PRAGMA table_info(positions)")}
                assert "tenant_id" in cols
                v = await conn.fetchval("SELECT MAX(version) FROM _schema_version")
                assert v == 10
            # migrate() is idempotent
            await db.migrate()

    _run(go())


# ---------------------------------------------------------------------------
# v7 -> v8 migration: leverage backfill + symbols_json drop
# ---------------------------------------------------------------------------


def test_migration_v8_backfills_leverage_and_drops_symbols():
    async def go():
        async with _mem_db() as db:
            async with db.pool.acquire() as conn:
                # Simulate a v7 database: symbols_json present, old 5x row.
                await conn.execute(
                    "ALTER TABLE tenant_config ADD COLUMN symbols_json "
                    "TEXT NOT NULL DEFAULT '[]'"
                )
                await conn.execute(
                    "INSERT INTO tenant_config (tenant_uuid, leverage, "
                    "symbols_json, updated_at) VALUES "
                    "('t-old', 5, '[\"BTCUSDT\"]', 1), "
                    "('t-custom', 25, '[\"ETHUSDT\"]', 1)"
                )
                await conn.execute(
                    "DELETE FROM _schema_version WHERE version IN (8, 9, 10)"
                )
            await db.migrate()
            async with db.pool.acquire() as conn:
                cols = {
                    r[1] for r in await conn.fetch("PRAGMA table_info(tenant_config)")
                }
                assert "symbols_json" not in cols
                rows = await conn.fetch(
                    "SELECT tenant_uuid, leverage FROM tenant_config"
                )
                got = {r[0]: r[1] for r in rows}
                assert got["t-old"] == 10  # backfilled to Balanced default
                assert got["t-custom"] == 25  # deliberate choice preserved
                v = await conn.fetchval("SELECT MAX(version) FROM _schema_version")
                assert v == 10
            # migrate() is idempotent (DROP skipped when column absent)
            await db.migrate()

    _run(go())
