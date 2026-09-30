"""Phase-5 scalp mode: AI-judged mean-reversion scalping inside Groq free limits.

- Scalp local signal scores extremes, ignores chop.
- Batch prompt covers N symbols in one call.
- decide_batch maps per-symbol verdicts, HOLD-fills omissions.
- Scalp rotation: gate skips dead cycles (no Groq call); setup cycles fire
  exactly ONE batch call on the scalp model; ENTER fills a slot, EXIT frees.
- Trend roll-gate: fast loops don't roll the hourly trend position.
- Tenant v7: strategy_mode + scalp profile; API leverage cap for scalp.
"""

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from quad.orchestrator.orchestrator import QuadOrchestrator  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _candle(ts):
    return SimpleNamespace(timestamp=ts)


# ---------------------------------------------------------------------------
# Scalp local signal
# ---------------------------------------------------------------------------


def test_scalp_signal_fires_on_oversold():
    from quad.ai.ta import generate_scalp_signal

    ind = {
        "momentum_rsi_14": 24.0,
        "momentum_stoch_k": 12.0,
        "momentum_stoch_d": 9.0,
        "volatility_bb_position": 0.02,
        "volume_spike": True,
        "volatility_atr_pct": 0.4,
        "price_current": 67000.0,
        "price_change_pct": -0.8,
    }
    sig = generate_scalp_signal(ind, symbol="BTCUSDT")
    assert sig["local_direction"] == "LONG" and sig["local_strength"] >= 0.5


def test_scalp_signal_ignores_chop():
    from quad.ai.ta import generate_scalp_signal

    ind = {
        "momentum_rsi_14": 52.0,
        "momentum_stoch_k": 55.0,
        "momentum_stoch_d": 50.0,
        "volatility_bb_position": 0.5,
        "volume_spike": False,
        "volatility_atr_pct": 0.3,
        "price_current": 67000.0,
        "price_change_pct": 0.1,
    }
    sig = generate_scalp_signal(ind, symbol="BTCUSDT")
    assert sig["local_direction"] == "NEUTRAL"


def test_scalp_signal_dampens_dead_volatility():
    from quad.ai.ta import generate_scalp_signal

    base = {
        "momentum_rsi_14": 22.0,
        "momentum_stoch_k": 10.0,
        "momentum_stoch_d": 8.0,
        "volatility_bb_position": 0.01,
        "volume_spike": False,
        "price_current": 67000.0,
        "price_change_pct": -0.5,
    }
    lively = dict(base, volatility_atr_pct=0.5)
    dead = dict(base, volatility_atr_pct=0.02)
    assert (
        generate_scalp_signal(lively, symbol="X")["local_strength"]
        > generate_scalp_signal(dead, symbol="X")["local_strength"]
    )


# ---------------------------------------------------------------------------
# Batch prompt + decide_batch
# ---------------------------------------------------------------------------


def test_batch_prompt_is_numbers_only_and_covers_all():
    from quad.ai.prompt import build_batch_judgement_prompt

    sigs = [
        {
            "symbol": "BTCUSDT",
            "price": 67234,
            "price_change_pct": 2.1,
            "local_direction": "LONG",
            "local_strength": 0.62,
            "rsi": 58,
            "stoch_k": 61,
            "stoch_d": 55,
            "bb_position": 0.7,
            "volume_spike": False,
        },
        {
            "symbol": "ETHUSDT",
            "price": 3410,
            "price_change_pct": -1.2,
            "local_direction": "NEUTRAL",
            "local_strength": 0.1,
            "rsi": 47,
            "stoch_k": 40,
            "stoch_d": 42,
            "bb_position": 0.45,
            "volume_spike": False,
        },
    ]
    p = build_batch_judgement_prompt(sigs, tp_pct=15.0, sl_pct=8.0)
    assert "BTCUSDT" in p["user"] and "ETHUSDT" in p["user"]
    assert "67234" in p["user"] and "decisions" in p["user"]
    assert len(p["user"]) < 1500  # tiny by design (~150 tokens)


def test_decide_batch_maps_and_hold_fills():
    from quad.ai.groq import GroqClient

    async def go():
        c = GroqClient.__new__(GroqClient)
        c._groq_config = {}
        c._log = MagicMock()

        async def fake_chat(**kw):
            assert kw["model"] == "qwen/qwen3-32b"
            import json as _json

            return _json.dumps(
                {
                    "decisions": [
                        {
                            "symbol": "BTCUSDT",
                            "action": "ENTER",
                            "direction": "LONG",
                            "confidence": 0.7,
                            "quantity": 0.01,
                            "reason": "oversold bounce",
                        },
                        # ETHUSDT omitted by the model on purpose
                    ]
                }
            )

        c.chat = fake_chat
        out = await GroqClient.decide_batch(
            c, "s", "u", ["BTCUSDT", "ETHUSDT"], model="qwen/qwen3-32b"
        )
        assert [d["symbol"] for d in out] == ["BTCUSDT", "ETHUSDT"]
        assert out[0]["action"] == "ENTER"
        assert out[1]["action"] == "HOLD" and out[1]["contract"] == "ETHUSDT"

    _run(go())


# ---------------------------------------------------------------------------
# Scalp rotation with fakes
# ---------------------------------------------------------------------------


def _scalp_orch(**overrides):
    orch = QuadOrchestrator.__new__(QuadOrchestrator)
    cfg = {
        "trading": {"serial_trade_mode": True, "leverage": 5},
        "ai": {
            "mode": "scalp",
            "pairs": ["BTCUSDT", "ETHUSDT"],
            "scalp": {
                "model": "qwen/qwen3-32b",
                "max_calls_per_day": 120,
                "min_strength": 0.6,
                "timeframes": ["5"],
                "max_hold_seconds": 900,
                "tp_pct": 15.0,
                "sl_pct": 8.0,
                "min_confidence": 0.0,
            },
        },
        "risk": {"max_positions": 2},
    }
    cfg["ai"].update(overrides.pop("ai", {}))
    orch._config_dict = cfg
    orch._log = MagicMock()
    orch._rotation_hold_since = {}
    orch._last_judge_candle_ts = {}
    orch._judge_day, orch._judge_used, orch._judge_budget_warned_day = str(), 0, str()
    orch._scalp_day, orch._scalp_used, orch._scalp_budget_warned_day = str(), 0, str()
    orch._scalp_held = {}
    orch._last_trend_roll_ts = {}
    orch._exchange_adapter = MagicMock()
    orch._market_data = MagicMock()
    orch._db_manager = MagicMock()
    orch._execution_engine = MagicMock()
    orch._risk_manager = MagicMock()
    return orch


def _patch_scalp(monkeypatch, candles, indicators):
    import quad.ai.context as CTX
    import quad.ai.ta as TA
    import quad.orchestrator.orchestrator as O

    async def fake_context(**kw):
        return SimpleNamespace(
            candles={k: [_candle(ts)] for k, ts in candles.items()},
            funding_rates={},
            positions=[],
            account=None,
        )

    monkeypatch.setattr(CTX, "collect_market_context", fake_context)
    monkeypatch.setattr(TA, "compute_indicators", lambda cs: dict(indicators))
    monkeypatch.setattr(
        O, "generate_scalp_signal", TA.generate_scalp_signal, raising=False
    )


def test_scalp_rotation_skips_dead_cycle_without_judge(monkeypatch):
    orch = _scalp_orch()
    _patch_scalp(
        monkeypatch, {"BTCUSDT_5": 100, "ETHUSDT_5": 100}, {"momentum_rsi_14": 50.0}
    )  # chop everywhere
    orch._last_judge_candle_ts = {"BTCUSDT": 100, "ETHUSDT": 100}
    batch = AsyncMock(return_value=[])
    orch._groq_client = SimpleNamespace(is_available=lambda: True, decide_batch=batch)
    ok = _run(orch._run_scalp_rotation(None, [], [], MagicMock()))
    assert ok is True
    assert batch.await_count == 0  # no Groq call spent


def test_scalp_rotation_single_batch_call_on_setup(monkeypatch):
    orch = _scalp_orch()
    _patch_scalp(
        monkeypatch,
        {"BTCUSDT_5": 200, "ETHUSDT_5": 200},
        {
            "momentum_rsi_14": 24.0,
            "momentum_stoch_k": 12.0,
            "momentum_stoch_d": 9.0,
            "volatility_bb_position": 0.02,
            "volume_spike": True,
            "volatility_atr_pct": 0.4,
            "price_current": 67000.0,
            "price_change_pct": -0.8,
        },
    )
    calls = []

    async def fake_batch(**kw):
        calls.append(kw)
        return [
            {
                "symbol": "BTCUSDT",
                "action": "HOLD",
                "direction": "NEUTRAL",
                "confidence": 0.2,
                "quantity": None,
                "contract": "BTCUSDT",
            },
            {
                "symbol": "ETHUSDT",
                "action": "HOLD",
                "direction": "NEUTRAL",
                "confidence": 0.2,
                "quantity": None,
                "contract": "ETHUSDT",
            },
        ]

    orch._groq_client = SimpleNamespace(
        is_available=lambda: True, decide_batch=fake_batch
    )
    orch._execute_ai_action = AsyncMock(return_value=True)
    ok = _run(orch._run_scalp_rotation(None, [], [], MagicMock()))
    assert ok is True
    assert len(calls) == 1  # ONE call for both symbols
    assert calls[0]["model"] == "qwen/qwen3-32b"
    assert set(calls[0]["symbols"]) == {"BTCUSDT", "ETHUSDT"}


def test_scalp_enter_fills_slot_and_tracks_hold(monkeypatch):
    orch = _scalp_orch()
    _patch_scalp(
        monkeypatch,
        {"BTCUSDT_5": 200},
        {
            "momentum_rsi_14": 24.0,
            "momentum_stoch_k": 12.0,
            "momentum_stoch_d": 9.0,
            "volatility_bb_position": 0.02,
            "volume_spike": True,
            "volatility_atr_pct": 0.4,
            "price_current": 67000.0,
            "price_change_pct": -0.8,
        },
    )

    async def fake_batch(**kw):
        return [
            {
                "symbol": "BTCUSDT",
                "action": "ENTER",
                "direction": "LONG",
                "confidence": 0.8,
                "quantity": 0.01,
                "contract": "BTCUSDT",
                "side": "BUY",
                "reason": "x",
            }
        ]

    orch._groq_client = SimpleNamespace(
        is_available=lambda: True, decide_batch=fake_batch
    )
    orch._execute_ai_action = AsyncMock(return_value=True)
    _run(orch._run_scalp_rotation(None, [], [], MagicMock()))
    assert "BTCUSDT" in orch._scalp_held
    assert orch._execute_ai_action.await_count == 1
    sent = orch._execute_ai_action.await_args[0][0]
    assert sent["strategy"] == "scalp"  # tight brackets downstream


def test_scalp_budget_exhaustion_blocks_batch(monkeypatch):
    import datetime as _dt

    orch = _scalp_orch()
    _patch_scalp(
        monkeypatch,
        {"BTCUSDT_5": 200},
        {
            "momentum_rsi_14": 24.0,
            "momentum_stoch_k": 12.0,
            "momentum_stoch_d": 9.0,
            "volatility_bb_position": 0.02,
            "volume_spike": True,
            "volatility_atr_pct": 0.4,
            "price_current": 67000.0,
            "price_change_pct": -0.8,
        },
    )
    today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    orch._scalp_day, orch._scalp_used = today, 120
    batch = AsyncMock(return_value=[])
    orch._groq_client = SimpleNamespace(is_available=lambda: True, decide_batch=batch)
    _run(orch._run_scalp_rotation(None, [], [], MagicMock()))
    assert batch.await_count == 0


# ---------------------------------------------------------------------------
# Trend roll-gate + tenant v7 + API guard
# ---------------------------------------------------------------------------


def test_trend_roll_gate_holds_on_fast_loop():
    orch = _scalp_orch()
    orch._config_dict["ai"]["mode"] = "trend"
    orch._config_dict["ai"]["pairs"] = ["BTCUSDT"]
    orch._config_dict["ai"]["rotation"] = {
        "close_open_position_each_cycle": True,
        "roll_min_seconds": 3300,
    }
    orch._groq_client = SimpleNamespace(is_available=lambda: True)
    orch._last_trend_roll_ts["BTCUSDT"] = __import__("time").monotonic()
    # roll not due -> must NOT close; scan path manages instead
    orch._scan_pair = AsyncMock(return_value={"action": "HOLD"})
    orch._close_all_positions = AsyncMock()
    from quad.types.domain import Position, PositionSide, PositionStatus
    from decimal import Decimal

    pos = Position(
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        quantity=Decimal("0.01"),
        status=PositionStatus.OPEN,
        entry_price=Decimal("60000"),
        current_price=Decimal("61000"),
    )

    async def go():
        return await orch._run_ai_rotation(None, [pos], [], MagicMock())

    _run(go())
    assert orch._close_all_positions.await_count == 0
    assert orch._scan_pair.await_count == 1


def test_schema_v7_scalp_fields():
    from quad.persistence import DatabaseManager
    from quad.persistence.repositories import TenantConfigRepository

    async def go():
        async with DatabaseManager(":memory:") as db:
            repo = TenantConfigRepository(db)
            cfg = await repo.get_or_default("t-scalp")
            assert cfg.strategy_mode == "trend"
            assert cfg.scalp_tp_pct == 15.0 and cfg.scalp_sl_pct == 8.0
            assert cfg.scalp_max_calls_per_day == 120
            await repo.update(cfg.id, strategy_mode="scalp", scalp_tp_pct=20.0)
            again = await repo.get_by_tenant("t-scalp")
            assert again is not None and again.strategy_mode == "scalp"
            assert again.scalp_tp_pct == 20.0

    _run(go())


import hashlib
import hmac
import time

from fastapi.testclient import TestClient

from quad.api.app import create_app
from quad.persistence import DatabaseManager

BOT_TOKEN = "scalp-bot-token"


@pytest.fixture()
def senv(monkeypatch):
    from quad.security.secrets import generate_key

    monkeypatch.setenv("QUAD_API_JWT_SECRET", "s" * 40)
    monkeypatch.setenv("QUAD_CREDENTIAL_KEY", generate_key())
    return True


@pytest.fixture()
def sclient(senv):
    db = DatabaseManager(":memory:")
    app = create_app(db, bot_token=BOT_TOKEN)
    with TestClient(app) as c:
        yield c


def _slogin(client):
    data = {
        "id": 777,
        "first_name": "S",
        "username": "s",
        "auth_date": int(time.time()),
    }
    check = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    data["hash"] = hmac.new(
        hashlib.sha256(BOT_TOKEN.encode()).digest(), check.encode(), hashlib.sha256
    ).hexdigest()
    r = client.post("/v1/auth/telegram", json=data)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _sput(client, headers, **kw):
    body = {
        "market": "linear",
        "capital_pct_per_trade": 2.0,
        "leverage": 5,
        "take_profit_pct": 50.0,
        "stop_loss_pct": 30.0,
        "strategy": "trend_following",
        "symbols": ["BTCUSDT"],
        "max_positions": 1,
        "ai_tier": "cheap",
        "ai_max_calls_per_day": 24,
        "strategy_mode": "trend",
        "scalp_tp_pct": 15.0,
        "scalp_sl_pct": 8.0,
        "scalp_max_calls_per_day": 120,
    }
    body.update(kw)
    return client.put("/v1/config", headers=headers, json=body)


def test_config_scalp_mode_roundtrip(sclient):
    h = _slogin(sclient)
    r = _sput(sclient, h, strategy_mode="scalp", leverage=5)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["strategy_mode"] == "scalp"
    assert data["scalp_tp_pct"] == 15.0 and data["scalp_max_calls_per_day"] == 120


def test_config_scalp_leverage_capped(sclient):
    h = _slogin(sclient)
    r = _sput(sclient, h, strategy_mode="scalp", leverage=50)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_config"
    r = _sput(sclient, h, strategy_mode="both", leverage=25)
    assert r.status_code == 422  # "both" removed: trend XOR scalp only
    r = _sput(sclient, h, strategy_mode="trend", leverage=50)
    assert r.status_code == 200  # trend keeps full range


def test_worker_mode_profiles():
    from quad.persistence import DatabaseManager
    from quad.persistence.repositories import (
        ExchangeCredentialRepository,
        TenantConfigRepository,
        TenantRepository,
    )
    from quad.security.secrets import encrypt_secret, generate_key
    from quad.worker import build_worker_config

    async def go():
        async with DatabaseManager(":memory:") as db:
            old = os.environ.get("QUAD_CREDENTIAL_KEY")
            os.environ["QUAD_CREDENTIAL_KEY"] = generate_key()
            try:
                t = await TenantRepository(db).create_tenant("t-mode")
                await ExchangeCredentialRepository(db).upsert_encrypted(
                    t.tenant_uuid, encrypt_secret("K"), encrypt_secret("S")
                )
                repo = TenantConfigRepository(db)
                cfg = await repo.get_or_default(t.tenant_uuid)
                await repo.update(
                    cfg.id, strategy_mode="scalp", scalp_max_calls_per_day=60
                )
                built = await build_worker_config(db, t.tenant_uuid, {"ai": {}})
                assert built["ai"]["mode"] == "scalp"
                assert built["trading"]["ai_cycle_interval"] == 300
                assert built["ai"]["scalp"]["max_calls_per_day"] == 60
                assert built["ai"]["scalp"]["model"] == "qwen/qwen3-32b"
                await repo.update(cfg.id, strategy_mode="trend")
                built2 = await build_worker_config(db, t.tenant_uuid, {"ai": {}})
                assert built2["trading"]["ai_cycle_interval"] == 3600
            finally:
                if old is None:
                    del os.environ["QUAD_CREDENTIAL_KEY"]
                else:
                    os.environ["QUAD_CREDENTIAL_KEY"] = old

    _run(go())
