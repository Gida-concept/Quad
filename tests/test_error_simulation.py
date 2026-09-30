"""Phase-5 adversarial simulation: every user-triggered failure we can think
of, the error it must produce, and the recovery path. All network calls
are faked — no live Bybit/Postgres needed.

NOTE: all direct DB coroutines run on the TestClient portal loop
(``client.portal.call``) because the app lifespan owns the DB connection.
"""

import hashlib
import hmac
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from quad.api.app import create_app  # noqa: E402
from quad.api.security import issue_token  # noqa: E402
from quad.persistence import DatabaseManager  # noqa: E402
from quad.persistence.models import PositionModel  # noqa: E402
from quad.persistence.repositories import PositionRepository  # noqa: E402

BOT = "sim-bot-token"
NOW = int(time.time() * 1000)


def _payload(uid):
    data = {"id": uid, "first_name": "U", "auth_date": int(time.time())}
    check = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    data["hash"] = hmac.new(
        hashlib.sha256(BOT.encode()).digest(), check.encode(), hashlib.sha256
    ).hexdigest()
    return data


@pytest.fixture()
def app_env(monkeypatch):
    monkeypatch.setenv("QUAD_API_JWT_SECRET", "s" * 40)
    from quad.security.secrets import generate_key

    monkeypatch.setenv("QUAD_CREDENTIAL_KEY", generate_key())
    monkeypatch.setenv("QUAD_SUPERVISOR_ENABLED", "false")
    db = DatabaseManager(":memory:")
    app = create_app(db, bot_token=BOT)
    with TestClient(app) as client:
        yield client, app


def _auth(client, uid=111):
    r = client.post("/v1/auth/telegram", json=_payload(uid))
    assert r.status_code == 200, r.text
    body = r.json()
    return {"Authorization": f"Bearer {body['access_token']}"}, body["tenant_uuid"]


def _portal(client, fn, *args):
    return client.portal.call(fn, *args)


# ---------------------------------------------------------------------------
# Auth failures
# ---------------------------------------------------------------------------


def test_missing_and_bad_tokens(app_env):
    client, _ = app_env
    assert client.get("/v1/config").status_code == 401
    assert (
        client.get("/v1/config", headers={"Authorization": "Bearer junk"}).status_code
        == 401
    )
    forged = issue_token("ghost-tenant", 999)
    r = client.get("/v1/config", headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code == 401


def test_halted_tenant_locked_out(app_env):
    client, app = app_env
    h, uuid = _auth(client)

    async def suspend():
        t = await app.state.api.tenants.get_by_uuid(uuid)
        await app.state.api.tenants.update(t.id, status="halted")

    _portal(client, suspend)
    assert client.get("/v1/config", headers=h).status_code == 200  # reads stay open
    assert client.get("/v1/positions", headers=h).status_code == 200
    r = client.post(
        "/v1/exchange/connect",
        headers=h,
        json={"api_key": "K" * 10, "api_secret": "S" * 10},
    )
    assert r.status_code == 409  # fail closed: resume first
    assert r.json()["error"]["code"] == "trading_halted"


# ---------------------------------------------------------------------------
# Exchange-connect failures
# ---------------------------------------------------------------------------


def test_connect_rejects_bad_credentials(app_env, monkeypatch):
    import quad.api.routes_exchange as ex
    from quad.api.bybit_verify import BybitVerifyError

    async def _bad_creds(k, s, **kw):
        raise BybitVerifyError("Bybit rejected the credentials: invalid key")

    monkeypatch.setattr(ex, "verify_bybit_credentials", _bad_creds)
    client, _ = app_env
    h, _ = _auth(client)
    r = client.post(
        "/v1/exchange/connect",
        headers=h,
        json={"api_key": "BADKEY12", "api_secret": "BADSECRET1"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_credentials"


def test_connect_survives_bybit_outage(app_env, monkeypatch):
    import quad.api.routes_exchange as ex

    async def outage(k, s, **kw):
        raise TimeoutError("timed out")

    monkeypatch.setattr(ex, "verify_bybit_credentials", outage)
    client, _ = app_env
    h, _ = _auth(client)
    r = client.post(
        "/v1/exchange/connect",
        headers=h,
        json={"api_key": "K" * 10, "api_secret": "S" * 10},
    )
    # outage surfaces as 400 (retryable), never a 500 or a traceback
    assert r.status_code == 400


def test_connect_validation(app_env):
    client, _ = app_env
    h, _ = _auth(client)
    assert client.post("/v1/exchange/connect", headers=h, json={}).status_code == 422
    r = client.post(
        "/v1/exchange/connect",
        headers=h,
        json={"api_key": "short", "api_secret": "S" * 10},
    )
    assert r.status_code == 422


def test_double_connect_upserts_and_disconnect_idempotent(app_env, monkeypatch):
    import quad.api.routes_exchange as ex

    async def _mock(k, s, **kw):
        return {"bybit_uid": "1", "permissions": ""}

    monkeypatch.setattr(ex, "verify_bybit_credentials", _mock)
    client, app = app_env
    h, uuid = _auth(client)
    body = {"api_key": "K" * 10, "api_secret": "S" * 10}
    assert client.post("/v1/exchange/connect", headers=h, json=body).status_code == 200
    assert client.post("/v1/exchange/connect", headers=h, json=body).status_code == 200

    async def count():
        return await app.state.api.credentials.count(tenant_id=uuid)

    assert _portal(client, count) == 1
    assert client.delete("/v1/exchange/disconnect", headers=h).status_code == 200
    assert client.delete("/v1/exchange/disconnect", headers=h).status_code == 200


# ---------------------------------------------------------------------------
# Config failures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [
        ("leverage", 999),
        ("leverage", 0),
        ("capital_pct_per_trade", 0),
        ("capital_pct_per_trade", 101),
        ("take_profit_pct", -1),
        ("max_positions", 0),
        ("max_positions", 99),
    ],
)
def test_config_rejects_out_of_range(app_env, field, value):
    client, _ = app_env
    h, _ = _auth(client)
    assert client.put("/v1/config", headers=h, json={field: value}).status_code == 422


def test_config_ignores_user_symbols(app_env):
    client, _ = app_env
    h, _ = _auth(client)
    assert client.put("/v1/config", headers=h, json={"symbols": []}).status_code == 200
    big = [f"C{i}USDT" for i in range(11)]
    r = client.put("/v1/config", headers=h, json={"symbols": big})
    assert r.status_code == 200 and len(r.json()["symbols"]) == 4
    assert (
        client.put("/v1/config", headers=h, json={"market": "spot"}).status_code == 422
    )


def test_config_update_audited(app_env):
    client, app = app_env
    h, uuid = _auth(client)
    assert client.put("/v1/config", headers=h, json={"leverage": 7}).status_code == 200

    async def keys():
        rows = await app.state.api.config_audit.list(tenant_id=uuid)
        return [r.key for r in rows]

    assert "config.update" in _portal(client, keys)


# ---------------------------------------------------------------------------
# Worker kill / resume / restart (FakeSupervisor, no processes)
# ---------------------------------------------------------------------------


class _FakeSup:
    def __init__(self):
        self.calls = []

    async def ensure(self, uuid):
        self.calls.append(("ensure", uuid))
        m = MagicMock()
        m.state = "running"
        m.pid = 4242
        m.last_error = ""
        return m

    async def stop(self, uuid):
        self.calls.append(("stop", uuid))

    def status(self, uuid):
        return {"state": "running", "pid": 4242}


@pytest.fixture()
def sup_client(app_env):
    client, app = app_env
    fake = _FakeSup()
    app.state.supervisor = fake
    return client, app, fake


def test_kill_holds_and_resume_recovers(sup_client, monkeypatch):
    import quad.api.flatten as fl

    monkeypatch.setattr(fl, "flatten_account", AsyncMock(return_value=(["c1"], [])))
    client, app, fake = sup_client
    h, uuid = _auth(client)

    r = client.post("/v1/worker/kill", headers=h)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["orders_cancelled"] == ["c1"] and data["tenant"] == "halted"
    assert ("stop", uuid) in fake.calls

    async def status():
        return (await app.state.api.tenants.get_by_uuid(uuid)).status

    assert _portal(client, status) == "halted"

    # resume is an un-halt even with no credentials: 200, worker (re)starts
    # on next connect. Tenant must read back active.
    r = client.post("/v1/worker/resume", headers=h)
    assert r.status_code == 200, r.text
    assert _portal(client, status) == "active"


def test_kill_survives_flatten_outage(sup_client, monkeypatch):
    import quad.api.flatten as fl

    async def boom(state, uuid):
        raise RuntimeError("exchange unreachable")

    monkeypatch.setattr(fl, "flatten_account", boom)
    client, _, _ = sup_client
    h, _ = _auth(client)
    r = client.post("/v1/worker/kill", headers=h)
    assert r.status_code == 200  # halt proceeds regardless
    assert any("flatten unavailable" in w for w in r.json()["data"]["warnings"])


def test_restart_cycles_worker(sup_client, monkeypatch):
    import quad.api.routes_exchange as ex

    async def _mock(k, s, **kw):
        return {"bybit_uid": "1", "permissions": ""}

    monkeypatch.setattr(ex, "verify_bybit_credentials", _mock)
    client, _, fake = sup_client
    h, uuid = _auth(client)
    r = client.post("/v1/worker/restart", headers=h)
    assert r.status_code == 200
    assert ("stop", uuid) in fake.calls and ("ensure", uuid) in fake.calls


def test_worker_endpoints_disabled_without_supervisor(app_env):
    client, _ = app_env
    h, _ = _auth(client)
    # Kill-switch is supervisor-independent: halt always applies, the process
    # stop is skipped when no supervisor is configured.
    r = client.post("/v1/worker/kill", headers=h)
    assert r.status_code == 200 and r.json()["data"]["state"] == "halted"
    # Resume un-halts even with no creds and no supervisor; worker starts on
    # next connect/restart.
    r = client.post("/v1/worker/resume", headers=h)
    assert r.status_code == 200 and r.json()["data"]["worker_state"] == "disabled"
    assert client.get("/v1/status", headers=h).json()["data"]["trading_halted"] is False
    # Restart is meaningless without a supervisor.
    assert client.post("/v1/worker/restart", headers=h).status_code == 503


# ---------------------------------------------------------------------------
# flatten_account unit (fake pybit session)
# ---------------------------------------------------------------------------


def test_flatten_account(app_env):
    from quad.api import flatten as fl
    from quad.security.secrets import encrypt_secret

    client, app = app_env
    _, uuid = _auth(client)

    async def setup():
        await app.state.api.credentials.upsert_encrypted(
            uuid, encrypt_secret("K"), encrypt_secret("S")
        )

    _portal(client, setup)

    class FakeSession:
        placed = []

        def cancel_all_orders(self, **kw):
            assert kw.get("category") == "linear" and kw.get("settleCoin") == "USDT"
            return {
                "retCode": 0,
                "result": {
                    "list": [
                        {"orderId": "o1", "symbol": "BTCUSDT"},
                        {"orderId": "o2", "symbol": "ETHUSDT"},
                    ]
                },
            }

        def get_positions(self, **kw):
            return {
                "retCode": 0,
                "result": {
                    "list": [
                        {
                            "symbol": "BTCUSDT",
                            "side": "Buy",
                            "size": "0.5",
                            "positionIdx": 0,
                        },
                        {
                            "symbol": "ETHUSDT",
                            "side": "Sell",
                            "size": "0",
                            "positionIdx": 0,
                        },
                    ]
                },
            }

        def place_order(self, **kw):
            FakeSession.placed.append(kw)
            return {"retCode": 0}

    async def go():
        return await fl.flatten_account(
            app.state.api, uuid, session_factory=lambda k, s, t: FakeSession()
        )

    cancelled, warnings = _portal(client, go)
    assert "o1" in cancelled and "o2" in cancelled
    assert any("closed:BTCUSDT" in c for c in cancelled)
    assert FakeSession.placed[0]["side"] == "Sell"  # Buy closed with Sell
    assert FakeSession.placed[0]["reduceOnly"] is True


def test_flatten_no_credentials(app_env):
    from quad.api import flatten as fl

    client, app = app_env
    _, uuid = _auth(client)

    async def go():
        return await fl.flatten_account(app.state.api, uuid)

    cancelled, warnings = _portal(client, go)
    assert cancelled == [] and warnings


# ---------------------------------------------------------------------------
# Isolation + multiplicity smoke
# ---------------------------------------------------------------------------


def test_cross_tenant_api_isolation(app_env):
    client, app = app_env
    ha, _ = _auth(client, uid=101)
    hb, _ = _auth(client, uid=102)

    async def seed():
        ta = await app.state.api.tenants.get_by_telegram_user(101)
        tb = await app.state.api.tenants.get_by_telegram_user(102)
        for t, sym in ((ta, "BTCUSDT"), (tb, "ETHUSDT")):
            await PositionRepository(app.state.api.db).create(
                PositionModel(
                    id=0,
                    strategy="s",
                    symbol=sym,
                    side="BUY",
                    quantity="1",
                    entry_price="1",
                    current_price="1",
                    unrealized_pnl="0",
                    realized_pnl="0",
                    status="OPEN",
                    opened_at=NOW,
                    updated_at=NOW,
                    tenant_id=t.tenant_uuid,
                )
            )

    _portal(client, seed)
    assert [p["symbol"] for p in client.get("/v1/positions", headers=ha).json()] == [
        "BTCUSDT"
    ]
    assert [p["symbol"] for p in client.get("/v1/positions", headers=hb).json()] == [
        "ETHUSDT"
    ]


def test_twenty_tenant_smoke(app_env):
    client, app = app_env

    async def seed_one(uuid):
        await PositionRepository(app.state.api.db).create(
            PositionModel(
                id=0,
                strategy="s",
                symbol="BTCUSDT",
                side="BUY",
                quantity="1",
                entry_price="1",
                current_price="1",
                unrealized_pnl="0",
                realized_pnl="0",
                status="OPEN",
                opened_at=NOW,
                updated_at=NOW,
                tenant_id=uuid,
            )
        )

    uuids = []
    for i in range(20):
        r = client.post("/v1/auth/telegram", json=_payload(5000 + i))
        assert r.status_code == 200
        uuids.append(r.json()["tenant_uuid"])
        _portal(client, seed_one, r.json()["tenant_uuid"])
    assert len(uuids) == 20
