"""Phase-5 AI cost controls: judge gate, daily budget, tier/escalation,
Groq key pool, and tenant schema v6.

- Gate: no fresh candle + no position -> HOLD without calling Groq.
- Gate: fresh candle + strong local setup -> Groq IS called.
- Gate: open position -> Groq IS called (needs a decision).
- Budget: exhausted daily budget -> local-only HOLD, no Groq call.
- Tier: cheap uses primary model, smart uses smart model.
- Escalation: cheap judge opposing a strong local -> smart re-judge wins.
- Key pool: GROQ_API_KEYS rotates past a daily-wall 429.
- Schema v6: tenant_config carries ai_tier / ai_max_calls_per_day.
"""

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from quad.orchestrator.orchestrator import QuadOrchestrator  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _orch(**overrides) -> QuadOrchestrator:
    orch = QuadOrchestrator.__new__(QuadOrchestrator)
    cfg = {
        "trading": {"serial_trade_mode": True},
        "ai": {
            "pairs": ["BTCUSDT"],
            "tier": "cheap",
            "judge_max_calls_per_day": 48,
            "judge_gate": {"enabled": True, "min_strength": 0.5},
            "escalate_on_disagreement": True,
            "escalate_min_strength": 0.7,
            "rotation": {"enabled": True, "close_open_position_each_cycle": True},
        },
    }
    cfg["ai"].update(overrides.pop("ai", {}))
    orch._config_dict = cfg
    orch._log = MagicMock()
    orch._rotation_hold_since = {}
    orch._last_judge_candle_ts = {}
    orch._judge_day = str()
    orch._judge_used = 0
    orch._judge_budget_warned_day = str()
    orch._rotation_index = 0
    return orch


def _candle(ts):
    return SimpleNamespace(timestamp=ts)


def _ctx(ts_by_key):
    return SimpleNamespace(
        candles={k: [_candle(ts)] for k, ts in ts_by_key.items()},
        funding_rates={},
        positions=[],
        account=None,
    )


# ---------------------------------------------------------------------------
# Gate unit tests (no Groq involved)
# ---------------------------------------------------------------------------


def test_gate_skips_stale_flat_market():
    orch = _orch()
    orch._last_judge_candle_ts["BTCUSDT"] = 1000
    ctx = _ctx({"BTCUSDT:60": 1000})  # same candle as last judged
    sig = {"local_direction": "NEUTRAL", "local_strength": 0.1}
    allowed, why = orch._judge_gate_check(
        "BTCUSDT", sig, None, ctx, {"min_strength": 0.5}
    )
    assert allowed is False and "no fresh candle" in why


def test_gate_allows_fresh_setup():
    orch = _orch()
    orch._last_judge_candle_ts["BTCUSDT"] = 1000
    ctx = _ctx({"BTCUSDT:60": 2000})  # new candle closed
    sig = {"local_direction": "LONG", "local_strength": 0.8}
    allowed, why = orch._judge_gate_check(
        "BTCUSDT", sig, None, ctx, {"min_strength": 0.5}
    )
    assert allowed is True


def test_gate_skips_fresh_candle_without_setup():
    orch = _orch()
    ctx = _ctx({"BTCUSDT:60": 2000})
    sig = {"local_direction": "NEUTRAL", "local_strength": 0.1}
    allowed, why = orch._judge_gate_check(
        "BTCUSDT", sig, None, ctx, {"min_strength": 0.5}
    )
    assert allowed is False and "no local setup" in why


def test_gate_allows_open_position_without_fresh_candle():
    orch = _orch()
    orch._last_judge_candle_ts["BTCUSDT"] = 1000
    ctx = _ctx({"BTCUSDT:60": 1000})
    sig = {"local_direction": "NEUTRAL", "local_strength": 0.0}
    allowed, _ = orch._judge_gate_check(
        "BTCUSDT", sig, "LONG", ctx, {"min_strength": 0.5}
    )
    assert allowed is True  # position needs a decision every cycle


def test_budget_exhaustion_forces_local_only():
    import datetime as _dt

    orch = _orch(ai={"judge_max_calls_per_day": 2})
    today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    orch._judge_day, orch._judge_used = today, 2
    ctx = _ctx({"BTCUSDT:60": 2000})
    sig = {"local_direction": "LONG", "local_strength": 0.9}
    allowed, why = orch._judge_gate_check(
        "BTCUSDT", sig, None, ctx, {"min_strength": 0.5}
    )
    assert allowed is False and "budget" in why


# ---------------------------------------------------------------------------
# _scan_pair integration with a fake Groq client
# ---------------------------------------------------------------------------


def _scan_orch(decisions, **ai_overrides):
    orch = _orch(ai=ai_overrides) if ai_overrides else _orch()
    calls = []

    async def fake_decide(**kw):
        calls.append(kw)
        return dict(decisions[len(calls) - 1])

    groq = SimpleNamespace(
        model_for_tier=lambda t: "smart-model" if t == "smart" else "cheap-model",
        decide_trades=fake_decide,
    )
    orch._groq_client = groq
    orch._ai_cycle_count = 0
    orch._last_ai_cycle_time_ms = 0.0
    orch._last_ai_decision = None
    orch._log_ai_decision = AsyncMock()
    # stub out heavy context collection
    orch._exchange_adapter = MagicMock()
    orch._market_data = MagicMock()
    orch._db_manager = MagicMock()
    return orch, calls


def _patch_scan(monkeypatch, local_signal, candle_ts=2000):
    # NOTE: _scan_pair uses function-level `from x import y`, so patches
    # must target the source modules, not the orchestrator namespace.
    import quad.ai.context as CTX
    import quad.ai.prompt as P
    import quad.ai.ta as TA

    async def fake_context(**kw):
        return _ctx({"BTCUSDT:60": candle_ts})

    monkeypatch.setattr(CTX, "collect_market_context", fake_context)
    monkeypatch.setattr(TA, "compute_indicators", lambda candles: {})
    monkeypatch.setattr(TA, "generate_local_signal", lambda *a, **k: dict(local_signal))
    monkeypatch.setattr(
        P,
        "build_final_judgement_prompt",
        lambda **kw: {"system": "s", "user": "u"},
    )


def test_scan_pair_skips_groq_on_dead_market(monkeypatch):
    orch, calls = _scan_orch([{"action": "HOLD"}])
    _patch_scan(
        monkeypatch,
        {"symbol": "BTCUSDT", "local_direction": "NEUTRAL", "local_strength": 0.1},
    )
    orch._last_judge_candle_ts["BTCUSDT"] = 2000  # already judged
    d = _run(orch._scan_pair("BTCUSDT"))
    assert d["action"] == "HOLD" and d["gate_result"] == "judge_skipped"
    assert calls == []  # Groq never touched


def test_scan_pair_calls_groq_on_setup_and_uses_cheap_model(monkeypatch):
    orch, calls = _scan_orch(
        [
            {
                "action": "ENTER",
                "direction": "LONG",
                "confidence": 0.8,
                "contract": "BTCUSDT",
            }
        ]
    )
    _patch_scan(
        monkeypatch,
        {"symbol": "BTCUSDT", "local_direction": "LONG", "local_strength": 0.8},
        candle_ts=3000,
    )
    d = _run(orch._scan_pair("BTCUSDT"))
    assert len(calls) == 1 and calls[0]["model"] == "cheap-model"
    assert d["action"] in ("ENTER", "HOLD")  # validator may still veto


def test_scan_pair_smart_tier_judges_smart(monkeypatch):
    orch, calls = _scan_orch(
        [
            {
                "action": "HOLD",
                "direction": "NEUTRAL",
                "confidence": 0.5,
                "contract": "BTCUSDT",
            }
        ],
        tier="smart",
    )
    _patch_scan(
        monkeypatch,
        {"symbol": "BTCUSDT", "local_direction": "NEUTRAL", "local_strength": 0.1},
        candle_ts=3000,
    )
    # force a call: open position needs a decision

    orig = orch._judge_gate_check
    orch._judge_gate_check = lambda *a, **k: (True, "test")
    try:
        _run(orch._scan_pair("BTCUSDT"))
    finally:
        orch._judge_gate_check = orig
    assert len(calls) == 1 and calls[0]["model"] == "smart-model"


def test_escalation_overrules_cheap_disagreement(monkeypatch):
    cheap = {
        "action": "HOLD",
        "direction": "SHORT",
        "confidence": 0.4,
        "contract": "BTCUSDT",
    }
    smart = {
        "action": "ENTER",
        "direction": "LONG",
        "confidence": 0.9,
        "contract": "BTCUSDT",
    }
    orch, calls = _scan_orch([cheap, smart])
    _patch_scan(
        monkeypatch,
        {"symbol": "BTCUSDT", "local_direction": "LONG", "local_strength": 0.85},
        candle_ts=3000,
    )
    d = _run(orch._scan_pair("BTCUSDT"))
    assert [c["model"] for c in calls] == ["cheap-model", "smart-model"]
    assert d["direction"] == "LONG" and d["confidence"] == 0.9


def test_no_escalation_when_cheap_agrees(monkeypatch):
    cheap = {
        "action": "ENTER",
        "direction": "LONG",
        "confidence": 0.8,
        "contract": "BTCUSDT",
    }
    orch, calls = _scan_orch([cheap])
    _patch_scan(
        monkeypatch,
        {"symbol": "BTCUSDT", "local_direction": "LONG", "local_strength": 0.85},
        candle_ts=3000,
    )
    _run(orch._scan_pair("BTCUSDT"))
    assert [c["model"] for c in calls] == ["cheap-model"]


# ---------------------------------------------------------------------------
# Groq client: tiers, key pool, schema v6
# ---------------------------------------------------------------------------


def test_model_for_tier(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    from quad.ai.groq import GroqClient

    cfg = {
        "ai": {
            "model": "cheap-model",
            "groq": {
                "fallback_model": "fb",
                "smart_model": "smart-model",
                "rate_limiter": {},
                "token_budget": {},
            },
        }
    }
    c = GroqClient.__new__(GroqClient)
    c._model = "cheap-model"
    c._groq_config = cfg["ai"]["groq"]
    assert GroqClient.model_for_tier(c, "cheap") == "cheap-model"
    assert GroqClient.model_for_tier(c, "smart") == "smart-model"
    assert GroqClient.model_for_tier(c, "typo") == "cheap-model"


def test_key_pool_rotation(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEYS", "k1,k2")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    from quad.ai.groq import GroqClient

    c = GroqClient.__new__(GroqClient)
    c._api_keys = ["k1", "k2"]
    c._api_key = "k1"
    c._timeout = 10
    c._log = MagicMock()
    assert GroqClient._rotate_key(c) is True
    assert c._api_key == "k2"
    assert GroqClient._rotate_key(c) is False  # pool spent


def test_schema_v6_tenant_ai_fields():
    from quad.persistence import DatabaseManager
    from quad.persistence.repositories import TenantConfigRepository

    async def go():
        async with DatabaseManager(":memory:") as db:
            repo = TenantConfigRepository(db)
            cfg = await repo.get_or_default("t-ai")
            assert cfg.ai_tier == "cheap" and cfg.ai_max_calls_per_day == 24
            await repo.update(cfg.id, ai_tier="smart", ai_max_calls_per_day=96)
            again = await repo.get_by_tenant("t-ai")
            assert again is not None
            assert again.ai_tier == "smart" and again.ai_max_calls_per_day == 96

    _run(go())


def test_worker_config_carries_ai_tier():
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
                t = await TenantRepository(db).create_tenant("t-w")
                await ExchangeCredentialRepository(db).upsert_encrypted(
                    t.tenant_uuid, encrypt_secret("K"), encrypt_secret("S")
                )
                cfg_repo = TenantConfigRepository(db)
                cfg = await cfg_repo.get_or_default(t.tenant_uuid)
                await cfg_repo.update(cfg.id, ai_tier="smart", ai_max_calls_per_day=96)
                built = await build_worker_config(db, t.tenant_uuid, {"ai": {}})
                assert built["ai"]["tier"] == "smart"
                assert built["ai"]["judge_max_calls_per_day"] == 96
                assert built["ai"]["rotation"]["max_hold_seconds"] == 3600
            finally:
                if old is None:
                    del os.environ["QUAD_CREDENTIAL_KEY"]
                else:
                    os.environ["QUAD_CREDENTIAL_KEY"] = old

    _run(go())
