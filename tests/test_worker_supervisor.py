"""Tests for Phase-4 worker config builder + supervisor lifecycle."""

import asyncio
import os
import stat
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from quad.persistence import DatabaseManager  # noqa: E402
from quad.persistence.repositories import (  # noqa: E402
    ExchangeCredentialRepository,
    TenantConfigRepository,
    TenantRepository,
)
from quad.security.secrets import encrypt_secret, generate_key  # noqa: E402
from quad.supervisor import Supervisor  # noqa: E402
from quad.worker import WorkerConfigError, build_worker_config  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def keys(monkeypatch):
    monkeypatch.setenv("QUAD_CREDENTIAL_KEY", generate_key())


async def _seed(db, uuid="tenant-aaa"):
    t = await TenantRepository(db).create_tenant(uuid)
    await ExchangeCredentialRepository(db).upsert_encrypted(
        uuid, encrypt_secret("KEY"), encrypt_secret("SEC"), testnet=True
    )
    cfg_repo = TenantConfigRepository(db)
    cfg = await cfg_repo.get_or_default(uuid)
    await cfg_repo.update(
        cfg.id,
        leverage=10,
        strategy="trend_following",
        capital_pct_per_trade=2.5,
        stop_loss_pct=20.0,
        take_profit_pct=60.0,
        max_positions=2,
    )
    return t


def test_build_worker_config(keys, tmp_path):
    async def go():
        async with DatabaseManager(":memory:") as db:
            await _seed(db)
            cfg = await build_worker_config(
                db,
                "tenant-aaa",
                {"trading": {}, "risk": {}, "ai": {}, "telegram": {"enabled": True}},
            )
            assert cfg["_tenant_id"] == "tenant-aaa"
            assert cfg["_mode"] == "bybit"
            assert cfg["exchange"]["api_key"] == "KEY"
            assert cfg["exchange"]["api_secret"] == "SEC"
            assert cfg["exchange"]["testnet"] is True
            assert "passphrase" not in cfg["exchange"]
            assert cfg["trading"]["leverage"] == 10
            assert cfg["trading"]["underlyings"] == [
                "BTCUSDT",
                "ETHUSDT",
                "SOLUSDT",
                "BNBUSDT",
            ]  # server-owned
            assert cfg["risk"]["max_positions"] == 2
            assert cfg["risk"]["per_position_sl"]["capital_pct"] == 20.0
            assert cfg["risk"]["per_position_tp"]["capital_pct"] == 60.0
            assert cfg["ai"]["pairs"] == ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]
            assert cfg["telegram"]["enabled"] is False

    _run(go())


def test_build_worker_config_failures(keys):
    async def go():
        async with DatabaseManager(":memory:") as db:
            with pytest.raises(WorkerConfigError):
                await build_worker_config(db, "nope", {})
            await TenantRepository(db).create_tenant("t-no-creds")
            with pytest.raises(WorkerConfigError):
                await build_worker_config(db, "t-no-creds", {})

    _run(go())


def test_supervisor_lifecycle(keys, tmp_path):
    async def go():
        async with DatabaseManager(":memory:") as db:
            await _seed(db)
            sup = Supervisor(
                db,
                base_config={"telegram": {"enabled": True}},
                worker_dir=str(tmp_path / "w"),
                worker_argv=["-c", "import time; time.sleep(60)"],
            )
            info = await sup.ensure("tenant-aaa")
            assert info.state == "running" and info.pid
            assert sup.status("tenant-aaa")["state"] == "running"
            # config file is owner-only on POSIX and holds the key material
            if os.name != "nt":
                mode = stat.S_IMODE(os.stat(info.config_path).st_mode)
                assert mode == 0o600
            assert "KEY" in Path(info.config_path).read_text()
            # idempotent ensure
            info2 = await sup.ensure("tenant-aaa")
            assert info2.pid == info.pid
            # sweep restarts a killed worker
            info._process.kill()
            await asyncio.sleep(0.5)
            states = await sup.sweep()
            assert states["tenant-aaa"] == "running"
            assert sup.status("tenant-aaa")["restarts"] == 1
            # stop removes the rendered config
            cfg_path = sup._workers["tenant-aaa"].config_path
            await sup.stop("tenant-aaa")
            assert sup.status("tenant-aaa")["state"] == "stopped"
            assert not os.path.exists(cfg_path)
            await sup.stop_all()

    _run(go())


def test_supervisor_crash_loop(keys, tmp_path):
    async def go():
        async with DatabaseManager(":memory:") as db:
            await _seed(db)
            sup = Supervisor(
                db,
                base_config={},
                worker_dir=str(tmp_path / "w"),
                max_restarts=1,
                restart_window_s=600,
                worker_argv=["-c", "import sys; sys.exit(3)"],
            )
            info = await sup.ensure("tenant-aaa")
            assert info.state == "running"
            for _ in range(4):  # let it crash past budget
                await asyncio.sleep(0.7)
                await sup.sweep()
                if sup.status("tenant-aaa")["state"] == "failed":
                    break
            assert sup.status("tenant-aaa")["state"] == "failed"
            # explicit ensure clears the verdict
            sup._worker_argv = ["-c", "import time; time.sleep(60)"]
            info = await sup.ensure("tenant-aaa")
            assert info.state == "running"
            await sup.stop_all()

    _run(go())


def test_supervisor_api_wiring(keys, tmp_path, monkeypatch):
    """connect/disconnect drive the supervisor when enabled."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import quad.supervisor as sup_mod

    calls = {}

    class FakeSupervisor:
        def __init__(self, db):
            pass

        async def ensure(self, uuid):
            calls["ensure"] = uuid
            m = MagicMock()
            m.state = "running"
            m.last_error = ""
            return m

        async def stop(self, uuid):
            calls["stop"] = uuid

        def status(self, uuid):
            return {"state": "running", "pid": 123}

    monkeypatch.setattr(sup_mod, "Supervisor", FakeSupervisor)
    monkeypatch.setenv("QUAD_SUPERVISOR_ENABLED", "true")
    monkeypatch.setenv("QUAD_API_JWT_SECRET", "s" * 40)

    import quad.api.routes_exchange as ex

    async def _mock(k, s, **kw):
        return {"bybit_uid": "1", "permissions": ""}

    monkeypatch.setattr(ex, "verify_bybit_credentials", _mock)

    # fresh module state for create_app's lazy import
    import quad.api.app as app_mod

    db = DatabaseManager(":memory:")
    app = app_mod.create_app(db, bot_token="tok")
    with TestClient(app) as client:
        # register tenant via telegram login
        import hashlib
        import hmac

        data = {"id": 777, "first_name": "T", "auth_date": int(time.time())}
        check = "\n".join(f"{k}={data[k]}" for k in sorted(data))
        data["hash"] = hmac.new(
            hashlib.sha256(b"tok").digest(), check.encode(), hashlib.sha256
        ).hexdigest()
        r = client.post("/v1/auth/telegram", json=data)
        assert r.status_code == 200, r.text
        tenant_uuid = r.json()["tenant_uuid"]
        h = {"Authorization": f"Bearer {r.json()['access_token']}"}
        r = client.post(
            "/v1/exchange/connect",
            headers=h,
            json={"api_key": "K" * 10, "api_secret": "S" * 10},
        )
        assert r.status_code == 200, r.text
        assert calls.get("ensure") == tenant_uuid
        st = client.get("/v1/exchange/status", headers=h).json()
        assert st["worker_state"] == "running" and st["worker_pid"] == 123
        client.delete("/v1/exchange/disconnect", headers=h)
        assert calls.get("stop")
