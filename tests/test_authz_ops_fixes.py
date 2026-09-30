"""Authz + ops hardening regression tests (one per fix area)."""

import asyncio
import inspect
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# ---------------------------------------------------------------------------
# (1) /set allowlist + guardrails
# ---------------------------------------------------------------------------

from quad.bot.commands import (  # noqa: E402
    BLOCKED_SET_KEYS,
    BLOCKED_SET_PREFIXES,
    SAFE_SET_KEYS,
    QuadBotCommands,
)


def test_set_constants_block_sensitive():
    assert "exchange.testnet" in BLOCKED_SET_KEYS
    assert "ai.system_prompt_override" in BLOCKED_SET_KEYS
    assert any("exchange.api_key".startswith(p) for p in BLOCKED_SET_PREFIXES)
    assert any("telegram.bot_token".startswith(p) for p in BLOCKED_SET_PREFIXES)
    assert "trading.leverage" in SAFE_SET_KEYS
    assert "exchange.testnet" not in SAFE_SET_KEYS
    assert "trading.trade_capital_usd" not in SAFE_SET_KEYS  # operator-only


class _FakeMessage:
    def __init__(self, chat_id=99):
        self.chat_id = chat_id
        self.replies = []

    async def reply_text(self, text, parse_mode=None, **kw):
        self.replies.append(text)
        return text


class _FakeBindings:
    """Deny every chat (non-operator)."""

    async def get_by_chat_id(self, chat_id):
        return None


class _FakeConfigManager:
    def __init__(self):
        self.set_calls = []

    def get(self, key):
        return "old"

    def set(self, key, value):
        self.set_calls.append((key, value))


def _make_cmds(bindings=...):
    orch = SimpleNamespace(_config_manager=_FakeConfigManager())
    cmds = QuadBotCommands(
        {
            "config": {},
            "telegram_config": {},
            "notification_chat_id": None,
            "orchestrator": orch,
        }
    )
    if bindings is ...:
        cmds._bindings = _FakeBindings()  # non-operator by default
    else:
        cmds._bindings = bindings
    return cmds


def _update(args, chat_id=99):
    return SimpleNamespace(
        message=_FakeMessage(chat_id),
        effective_user=SimpleNamespace(id=7),
    ), SimpleNamespace(args=args)


def test_set_denies_blocked_key_for_non_operator():
    cmds = _make_cmds()
    update, ctx = _update(["exchange.api_key", "hacked"])
    asyncio.run(cmds.cmd_set(update, ctx))
    assert any("cannot be changed" in r for r in update.message.replies)
    assert cmds._orchestrator._config_manager.set_calls == []


def test_set_operator_prompt_override_passes_auth():
    cmds = _make_cmds(bindings=None)  # no DB -> operator chat
    update, ctx = _update(["ai.system_prompt_override", "hello"])
    asyncio.run(cmds.cmd_set(update, ctx))
    assert ("ai.system_prompt_override", "hello") in (
        cmds._orchestrator._config_manager.set_calls
    )


def test_set_leverage_guardrail():
    cmds = _make_cmds(bindings=None)
    update, ctx = _update(["trading.leverage", "999"])
    asyncio.run(cmds.cmd_set(update, ctx))
    assert any("Leverage must be" in r for r in update.message.replies)
    assert cmds._orchestrator._config_manager.set_calls == []
    update2, ctx2 = _update(["trading.leverage", "5"])
    asyncio.run(cmds.cmd_set(update2, ctx2))
    assert ("trading.leverage", 5) in cmds._orchestrator._config_manager.set_calls


# ---------------------------------------------------------------------------
# (2) kill-callback binding check
# ---------------------------------------------------------------------------


def test_kill_callback_denies_unbound_chat():
    cmds = _make_cmds()
    edits = []

    async def _answer():
        return None

    async def _edit(text, **kw):
        edits.append(text)

    query = SimpleNamespace(
        message=SimpleNamespace(chat_id=4242),
        from_user=SimpleNamespace(id=7),
        data="kill_confirm",
        answer=_answer,
        edit_message_text=_edit,
    )
    update = SimpleNamespace(callback_query=query)
    asyncio.run(cmds.cmd_kill_callback(update, SimpleNamespace()))
    assert any("isn't linked" in e for e in edits)


# ---------------------------------------------------------------------------
# (4) validator default veto
# ---------------------------------------------------------------------------

from quad.ai.validator import normalize_decision  # noqa: E402


def test_validator_default_is_veto():
    assert (
        inspect.signature(normalize_decision).parameters["gate_mode"].default == "veto"
    )
    ind = {"trend_regime": "downtrend", "momentum_rsi_14": 75.0}
    dec = {"action": "ENTER", "direction": "LONG", "contract": "BTCUSDT", "quantity": 1}
    assert normalize_decision(dict(dec), indicators=ind).ok is False  # rejected now
    warned = normalize_decision(dict(dec), indicators=ind, gate_mode="warn")
    assert warned.ok is True and warned.decision["gate_result"] == "warn"


# ---------------------------------------------------------------------------
# (5) optimizer prompt-update blocklist
# ---------------------------------------------------------------------------

from quad.ai.optimizer import Optimizer  # noqa: E402


def test_optimizer_prompt_update_blocked_without_flag():
    import structlog

    opt = Optimizer.__new__(Optimizer)
    opt._log = structlog.get_logger("test")
    opt._allow_prompt_updates = False
    opt._config_dict = {}
    opt._config_lock = asyncio.Lock()
    opt._config_change_repo = None
    rec = SimpleNamespace(
        id=1,
        recommendation_type="prompt_update",
        target_area="system_prompt",
        recommended_value=json.dumps("evil"),
    )
    asyncio.run(opt._apply_recommendation(rec))
    assert opt._config_dict == {}


# ---------------------------------------------------------------------------
# (3) TV dedupe
# ---------------------------------------------------------------------------

from quad.orchestrator.orchestrator import QuadOrchestrator  # noqa: E402


def _tv_handler():
    import structlog

    orch = QuadOrchestrator.__new__(QuadOrchestrator)
    orch._log = structlog.get_logger("test")
    orch._config_dict = {
        "tradingview_webhook": {
            "enabled": True,
            "secret": "s3cret-s3cret-0016",
            "allow_without_secret": False,
            "port": 9090,
        }
    }
    orch._tv_seen = {}
    orch._execution_engine = None
    orch._exchange_adapter = None
    captured = {}

    class _HS:
        """Minimal HealthServer double honouring the add_route/has_route contract."""

        def __init__(self):
            self.routes = set()

        def add_route(self, method, path, handler):
            self.routes.add((method.upper(), path))
            captured["handler"] = handler

        def has_route(self, method, path):
            return (method.upper(), path) in self.routes

    orch._health_server = _HS()
    asyncio.run(orch._init_tradingview_webhook())
    return orch, captured["handler"]


def test_tv_route_actually_mounted():
    """The webhook must be reported as live only when the route exists."""
    orch, _ = _tv_handler()
    assert orch._tv_webhook is not None
    assert orch._tv_webhook["route"] == "POST /webhook/tradingview"
    assert orch._health_server.has_route("POST", "/webhook/tradingview")


def test_tv_webhook_fails_closed_when_route_not_mounted():
    """If the route cannot be mounted, the webhook reports disabled."""
    import structlog

    orch = QuadOrchestrator.__new__(QuadOrchestrator)
    orch._log = structlog.get_logger("test")
    orch._config_dict = {
        "tradingview_webhook": {
            "enabled": True,
            "secret": "s3cret-s3cret-0016",
            "port": 9090,
        }
    }
    orch._tv_seen = {}
    orch._execution_engine = None
    orch._exchange_adapter = None

    class _BrokenHS:
        def add_route(self, method, path, handler):
            return  # silently never mounts

        def has_route(self, method, path):
            return False

    orch._health_server = _BrokenHS()
    asyncio.run(orch._init_tradingview_webhook())
    assert orch._tv_webhook is None


def test_tv_webhook_requires_secret_no_escape_hatch():
    """No opt-in path exists: a short/absent secret is always fatal.

    The pydantic schema is the authoritative gate — ``allow_without_secret``
    in YAML must not buy a bypass, and the orchestrator must not contain an
    env-var opt-in either.
    """
    import inspect

    import pydantic

    from quad.config.schema import TradingViewWebhookConfig

    for payload in (
        {"enabled": True, "allow_without_secret": True},
        {"enabled": True, "secret": "short"},
        {"enabled": True, "secret": "   "},
    ):
        with pytest.raises(pydantic.ValidationError):
            TradingViewWebhookConfig.model_validate(payload)

    # No "run unauthenticated" escape hatch may reappear in the orchestrator.
    src = inspect.getsource(QuadOrchestrator._init_tradingview_webhook)
    assert "ALLOW_NOAUTH" not in src
    assert "allow_without_secret" not in src


def test_tv_duplicate_rejected():
    orch, handler = _tv_handler()
    body = json.dumps(
        {
            "ticker": "BTCUSDT",
            "action": "buy",
            "quantity": 1,
            "secret": "s3cret-s3cret-0016",
        }
    ).encode()

    async def _read():
        return body

    req = SimpleNamespace(content_type="application/json", headers={}, read=_read)
    first = asyncio.run(handler(req))
    second = asyncio.run(handler(req))
    assert '"ok"' in first.text
    assert '"duplicate"' in second.text


# ---------------------------------------------------------------------------
# (6) groq opt-in enforcement
# ---------------------------------------------------------------------------

from unittest import mock  # noqa: E402

from quad.ai.groq import GroqClient  # noqa: E402


def _groq_cfg(**overrides):
    cfg = {
        "ai": {
            "model": "groq/compound-mini",
            "groq": {
                "timeout_seconds": 30.0,
                "max_retries": 1,
                "base_backoff_seconds": 0.01,
                "rate_limiter": {"max_requests_per_day": 1000, "window_seconds": 86400},
                "token_budget": {
                    "enabled": True,
                    "max_tokens_per_day": 100,
                    "window_seconds": 86400,
                },
            },
        }
    }
    cfg["ai"]["groq"]["token_budget"].update(overrides)
    return cfg


def test_groq_budget_noop_by_default_enforced_when_opted_in():
    with mock.patch("quad.ai.groq.AsyncGroq"):
        default = GroqClient(api_key="k", config=_groq_cfg())
        # Default: no local veto even over budget.
        asyncio.run(default._check_token_budget(10_000))
        assert default.is_available() is True

        enforced = GroqClient(api_key="k", config=_groq_cfg(enforce=True))
        now = 1_700_000_000.0
        enforced._record_token_usage(90, now=now)
        with pytest.raises(RuntimeError):
            asyncio.run(enforced._check_token_budget(20, now=now))
        enforced._record_token_usage(20, now=now)
        assert enforced.is_available(now) is False


# ---------------------------------------------------------------------------
# (7) supervisor heartbeat + crash config cleanup
# ---------------------------------------------------------------------------

from quad.supervisor import Supervisor, WorkerInfo  # noqa: E402


def test_supervisor_poll_heartbeat_and_crash_cleanup(tmp_path):
    from quad.persistence import DatabaseManager

    sup = Supervisor(DatabaseManager(":memory:"), worker_dir=str(tmp_path))
    info = WorkerInfo(tenant_uuid="t1", state="running")

    class _Alive:
        def poll(self):
            return None

    info._process = _Alive()
    sup._poll(info)
    assert info.last_heartbeat > 0

    cfg = tmp_path / "t1.yaml"
    cfg.write_text("secret: x")
    info.config_path = str(cfg)

    class _Dead:
        returncode = 1

        def poll(self):
            return 1

    info._process = _Dead()
    sup._poll(info)
    assert info.state == "stopped"
    assert not cfg.exists()  # rendered secrets file removed on crash path


# ---------------------------------------------------------------------------
# (8) 422 shape strips input/ctx
# ---------------------------------------------------------------------------


def test_422_shape_has_no_input_or_ctx():
    import jwt  # noqa: F401  (ensures parity with test_api imports)

    from fastapi.testclient import TestClient

    from quad.api.app import create_app
    from quad.persistence import DatabaseManager

    from test_api import _login, BOT_TOKEN  # noqa: E402

    import os

    from quad.security.secrets import generate_key

    os.environ["QUAD_API_JWT_SECRET"] = "s" * 40
    os.environ["QUAD_CREDENTIAL_KEY"] = generate_key()
    db = DatabaseManager(":memory:")
    app = create_app(db, bot_token=BOT_TOKEN)
    with TestClient(app) as client:
        auth = _login(client)
        r = client.put(
            "/v1/config",
            headers={"Authorization": auth["Authorization"]},
            json={"market": "spot"},
        )
        assert r.status_code == 422
        body = r.json()
        assert body["error"]["code"] == "invalid_request"
        assert "input" not in r.text and '"ctx"' not in r.text
        for detail in body["error"]["details"]:
            assert set(detail) == {"loc", "msg", "type"}


# ---------------------------------------------------------------------------
# (9) CLI redaction + DSN mask
# ---------------------------------------------------------------------------

from quad.cli.app import _mask_dsn, _redact_value  # noqa: E402


def test_cli_redact_and_dsn_mask():
    assert _redact_value("api_secret", "hunter2") == "***REDACTED***"
    assert _redact_value("leverage", "10") == "10"
    masked = _mask_dsn("postgresql://bob:hunter2@db:5432/quad")
    assert "hunter2" not in masked and "bob" in masked and "db" in masked
    assert _mask_dsn("data/quad.db") == "data/quad.db"


# ---------------------------------------------------------------------------
# (10) metrics escape + series cap; health degraded + port env
# ---------------------------------------------------------------------------

from quad.monitoring.metrics import MetricsCollector, _format_labels  # noqa: E402


def test_metrics_escape_labels():
    s = _format_labels({"sym": 'a"b\nc\\d'})
    assert s == '{sym="a\\"b\\nc\\\\d"}'


def test_metrics_series_cap():
    mc = MetricsCollector()
    for i in range(5010):
        mc.set_gauge(f"capmetric_{i}", 1.0)
    text = mc.get_metrics_text()
    assert sum(1 for line in text.splitlines() if line.startswith("capmetric_")) <= 5000


def test_health_degraded_and_port_env(monkeypatch):
    import json as _json

    from quad.monitoring.health import HealthServer

    monkeypatch.setenv("QUAD_HEALTH_PORT", "9191")
    hs = HealthServer(config={"monitoring": {"health_server": {"port": 9090}}})
    assert hs._port == 9191
    hs2 = HealthServer(
        config={},
        components={"db": False, "supervisor": lambda: True},
    )

    async def _go():
        return await hs2._handle_health(object())

    resp = asyncio.run(_go())
    body = _json.loads(resp.text)
    assert body["status"] == "degraded" and body["degraded"] == ["db"]
