"""Tests for quad-api v1: telegram-login auth, exchange connect, config,
tenant-scoped reads, pairing codes, rate limits, error envelope, pg routing.
"""

import hashlib
import hmac
import os
import sys
import time
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

fastapi = pytest.importorskip("fastapi")
jwt = pytest.importorskip("jwt")

from fastapi.testclient import TestClient  # noqa: E402

from quad.api.app import create_app  # noqa: E402
from quad.api.deps import RateLimiter  # noqa: E402
from quad.persistence import DatabaseManager  # noqa: E402
from quad.persistence.pg import (  # noqa: E402
    PostgresDatabaseManager,
    create_database,
    translate_ddl,
)

BOT_TOKEN = "test-bot-token-12345"


@pytest.fixture()
def env(monkeypatch):
    from quad.security.secrets import generate_key

    monkeypatch.setenv("QUAD_API_JWT_SECRET", "s" * 40)
    monkeypatch.setenv("QUAD_CREDENTIAL_KEY", generate_key())
    return True


def _tg_payload(auth_date=None, key=BOT_TOKEN, tamper=False):
    data = {
        "id": 424242,
        "first_name": "Tester",
        "username": "tester",
        "auth_date": int(auth_date if auth_date is not None else time.time()),
    }
    check = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    digest = hmac.new(
        hashlib.sha256(key.encode()).digest(), check.encode(), hashlib.sha256
    ).hexdigest()
    if tamper:
        first = "0" if digest[0] != "0" else "1"  # guaranteed mismatch
        data["hash"] = first + digest[1:]
    else:
        data["hash"] = digest
    return data


@pytest.fixture()
def client(env):
    db = DatabaseManager(":memory:")
    app = create_app(db, bot_token=BOT_TOKEN)
    with TestClient(app) as c:
        yield c


def _login(client):
    r = client.post("/v1/auth/telegram", json=_tg_payload())
    assert r.status_code == 200, r.text
    body = r.json()
    return {
        "Authorization": f"Bearer {body['access_token']}",
        "uuid": body["tenant_uuid"],
    }


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_health_no_auth(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_health_reports_healthy_database(client):
    """A reachable database is reported as a healthy component."""
    data = client.get("/health").json()["data"]
    assert data["status"] == "ok"
    assert data["components"]["database"] is True
    assert data["degraded"] == []


def test_health_returns_503_when_the_database_is_unreachable(monkeypatch, env):
    """An API that cannot reach its store must not report itself healthy.

    Returning an unconditional ``ok`` meant a load balancer kept routing
    traffic here while every real endpoint failed.
    """
    db = DatabaseManager(":memory:")

    async def _broken(self) -> bool:
        return False

    monkeypatch.setattr(DatabaseManager, "is_healthy", _broken)
    app = create_app(db=db, bot_token=BOT_TOKEN)
    with TestClient(app) as c:
        r = c.get("/health")
    assert r.status_code == 503
    body = r.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "unhealthy"
    assert body["data"]["status"] == "degraded"
    assert body["data"]["degraded"] == ["database"]


def test_health_check_failure_is_fail_closed(monkeypatch, env):
    """An exception inside the check counts as unhealthy, never as healthy."""

    async def _explode(self) -> bool:
        raise RuntimeError("pool exhausted")

    monkeypatch.setattr(DatabaseManager, "is_healthy", _explode)
    app = create_app(db=DatabaseManager(":memory:"), bot_token=BOT_TOKEN)
    with TestClient(app) as c:
        r = c.get("/health")
    assert r.status_code == 503
    assert r.json()["data"]["components"]["database"] is False


def test_live_does_not_check_the_database(monkeypatch, env):
    """Liveness must stay up during a database blip.

    Restarting the process because the database is briefly unreachable turns a
    small problem into an outage, so ``/live`` asserts nothing but that the
    process is serving.
    """
    calls: list[int] = []

    async def _counting(self) -> bool:
        calls.append(1)
        return False

    monkeypatch.setattr(DatabaseManager, "is_healthy", _counting)
    app = create_app(db=DatabaseManager(":memory:"), bot_token=BOT_TOKEN)
    with TestClient(app) as c:
        r = c.get("/live")
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "ok"
    assert calls == [], "/live must not touch the database"


def test_telegram_login_roundtrip(client):
    auth = _login(client)
    assert auth["uuid"]


def test_telegram_login_bad_hash(client):
    r = client.post("/v1/auth/telegram", json=_tg_payload(tamper=True))
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_login"


def test_telegram_login_expired(client):
    r = client.post(
        "/v1/auth/telegram", json=_tg_payload(auth_date=time.time() - 100_000)
    )
    assert r.status_code == 401


def test_protected_no_token(client):
    r = client.get("/v1/config")
    assert r.status_code == 401
    assert r.json()["ok"] is False


def test_protected_bad_token(client):
    r = client.get("/v1/config", headers={"Authorization": "Bearer junk"})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_config_defaults_and_update(client):
    auth = _login(client)
    h = {"Authorization": auth["Authorization"]}
    r = client.get("/v1/config", headers=h)
    assert r.status_code == 200
    assert r.json()["market"] == "linear" and r.json()["leverage"] == 10
    r = client.put(
        "/v1/config",
        headers=h,
        json={
            "leverage": 10,
            "capital_pct_per_trade": 1.5,
            "symbols": ["BTCUSDT", "ETHUSDT"],
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["leverage"] == 10
    # Symbols are server-owned: user list ignored, bot universe returned.
    assert r.json()["symbols"] == ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]


def test_config_rejects_spot_v1(client):
    auth = _login(client)
    r = client.put(
        "/v1/config",
        headers={"Authorization": auth["Authorization"]},
        json={"market": "spot"},
    )
    assert r.status_code == 422  # Literal["linear"]
    assert r.json()["ok"] is False


def test_config_ignores_user_symbols(client):
    auth = _login(client)
    r = client.put(
        "/v1/config",
        headers={"Authorization": auth["Authorization"]},
        json={"symbols": []},
    )
    assert r.status_code == 200  # extra field ignored, server universe kept
    assert r.json()["symbols"] == ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]


# ---------------------------------------------------------------------------
# Exchange connect (Bybit verification mocked)
# ---------------------------------------------------------------------------


def test_exchange_connect_status_disconnect(client, monkeypatch):
    import quad.api.routes_exchange as ex

    async def _mock_verify(k, s, **kw):
        return {"bybit_uid": "12345", "permissions": "ContractTrade,SpotTrade"}

    monkeypatch.setattr(ex, "verify_bybit_credentials", _mock_verify)
    auth = _login(client)
    h = {"Authorization": auth["Authorization"]}

    r = client.get("/v1/exchange/status", headers=h)
    assert r.json()["connected"] is False

    r = client.post(
        "/v1/exchange/connect",
        headers=h,
        json={"api_key": "KEY12345678", "api_secret": "SECRET12345678"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["verified"] is True and r.json()["bybit_uid"] == "12345"

    r = client.get("/v1/exchange/status", headers=h)
    assert r.json()["connected"] is True

    r = client.delete("/v1/exchange/disconnect", headers=h)
    assert r.json()["ok"] is True
    assert client.get("/v1/exchange/status", headers=h).json()["connected"] is False


def test_exchange_connect_bad_credentials(client, monkeypatch):
    import quad.api.routes_exchange as ex
    from quad.api.bybit_verify import BybitVerifyError

    async def boom(k, s, **kw):
        raise BybitVerifyError("Bybit rejected the credentials: invalid key")

    monkeypatch.setattr(ex, "verify_bybit_credentials", boom)
    auth = _login(client)
    r = client.post(
        "/v1/exchange/connect",
        headers={"Authorization": auth["Authorization"]},
        json={"api_key": "BADKEY1234", "api_secret": "BADSECRET12"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_credentials"


def test_exchange_connect_validation_envelope(client):
    auth = _login(client)
    r = client.post(
        "/v1/exchange/connect",
        headers={"Authorization": auth["Authorization"]},
        json={},
    )
    assert r.status_code == 422
    assert r.json()["ok"] is False and r.json()["error"]["code"] == "invalid_request"


# ---------------------------------------------------------------------------
# Trading reads + pairing + status
# ---------------------------------------------------------------------------


def test_trading_reads_empty(client):
    auth = _login(client)
    h = {"Authorization": auth["Authorization"]}
    assert client.get("/v1/positions", headers=h).json() == []
    assert client.get("/v1/orders", headers=h).json() == []
    pnl = client.get("/v1/pnl", headers=h).json()
    assert pnl["realized_today"] == "0" and pnl["open_unrealized"] == "0"
    st = client.get("/v1/status", headers=h).json()["data"]
    assert st["market"] == "linear" and st["exchange_connected"] is False


def test_pairing_code(client):
    auth = _login(client)
    r = client.post(
        "/v1/telegram/pairing-code", headers={"Authorization": auth["Authorization"]}
    )
    assert r.status_code == 200
    assert len(r.json()["code"]) == 12 and r.json()["expires_at"] > time.time() * 1000


def test_rate_limit_429(client):
    client.app.state.limiter = RateLimiter(max_requests=2, window_seconds=60)
    auth = _login(client)
    h = {"Authorization": auth["Authorization"]}
    client.get("/v1/config", headers=h)
    client.get("/v1/config", headers=h)
    r = client.get("/v1/config", headers=h)
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "rate_limited"


# ---------------------------------------------------------------------------
# Persistence routing (no live Postgres required)
# ---------------------------------------------------------------------------


def test_translate_ddl_no_sqliteisms():
    from quad.persistence.models import PositionModel

    out = translate_ddl(PositionModel.create_table_ddl())
    assert "AUTOINCREMENT" not in out and "SERIAL PRIMARY KEY" in out


def test_create_database_routing(tmp_path):
    assert isinstance(create_database(str(tmp_path / "x.db")), DatabaseManager)
    assert isinstance(create_database(":memory:"), DatabaseManager)
    pg = create_database("postgresql://quad:quad@localhost:5432/quad")
    assert isinstance(pg, PostgresDatabaseManager)


@pytest.mark.skipif(
    not os.environ.get("QUAD_TEST_PG_DSN"),
    reason="needs QUAD_TEST_PG_DSN for live Postgres",
)
def test_live_postgres_init_migrate():
    import asyncio

    async def go():
        db = create_database(os.environ["QUAD_TEST_PG_DSN"])
        async with db:
            assert await db.is_healthy()
            tenant = await db.pool  # noqa - pool exists
            assert tenant is not None

    asyncio.run(go())
