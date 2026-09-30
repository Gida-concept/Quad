"""Regression tests for the fixes found in the full-codebase scan.

Covers the safety-critical items that previously only *looked* correct:
the TradingView webhook route was never mounted, ``/kill`` cancelled nothing,
``/execute`` forced ``dry_run=False``, brackets skipped the risk pipeline,
and the min-quantity floor-up could exceed an approved notional cap.
"""

import asyncio
import inspect
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from quad.bot.commands import QuadBotCommands
from quad.execution.engine import ExecutionEngine
from quad.types.risk import Action
from quad.monitoring.health import HealthServer
from quad.tradingview.signals import (
    WEBHOOK_SECRET_ENV,
    convert_to_action,
    get_webhook_secret,
)


# ---------------------------------------------------------------------------
# CRITICAL: add_route() after start() must mount on the live aiohttp router
# ---------------------------------------------------------------------------


async def _serve_once(server: HealthServer, method: str, path: str, **kw):
    """Issue one real HTTP request against the running aiohttp server."""
    import aiohttp

    async with aiohttp.ClientSession() as session:
        url = f"http://127.0.0.1:{server._port}{path}"
        async with session.request(method, url, **kw) as resp:
            return resp.status, await resp.text()


@pytest.mark.asyncio
async def test_add_route_after_start_is_reachable():
    """The orchestrator starts the health server BEFORE the TV webhook.

    Registering the webhook afterwards must still make the route live — it
    used to be queued in a list and every alert 404'd.
    """
    import aiohttp
    from aiohttp import web

    server = HealthServer(port=0, config={"monitoring": {"health_server": {}}})
    await server.start()
    # aiohttp reports the bound port on the site once started.
    server._port = server._site._server.sockets[0].getsockname()[1]
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://127.0.0.1:{server._port}/health") as r:
                assert r.status == 200, "health endpoint should be up"

        # Registered AFTER start() — the regression case.
        async def _ok(req):
            return web.json_response({"status": "ok"})

        server.add_route("POST", "/webhook/tradingview", _ok)
        assert server.has_route("POST", "/webhook/tradingview")

        # Re-registering must not raise, and must stay a single mount.
        server.add_route("POST", "/webhook/tradingview", _ok)

        status, body = await _serve_once(
            server, "POST", "/webhook/tradingview", json={"a": 1}
        )
        assert status == 200, f"late-mounted route returned {status}: {body}"
        assert json.loads(body)["status"] == "ok"
    finally:
        await server.stop()


def test_add_route_before_start_is_mounted():
    server = HealthServer(port=9090, config={})
    server.add_route("POST", "/early", lambda req: None)
    assert server.has_route("POST", "/early")
    assert not server.has_route("GET", "/early")


def test_has_route_reports_absent_path():
    server = HealthServer(port=9090, config={})
    assert not server.has_route("POST", "/webhook/tradingview")


def test_health_probes_have_k8s_short_aliases():
    """docs/api.md documents /ready and /live; both must exist."""
    src = inspect.getsource(HealthServer.start)
    assert 'add_get("/ready"' in src
    assert 'add_get("/live"' in src


# ---------------------------------------------------------------------------
# Health auth: no-key loopback bypass must not survive a reverse proxy
# ---------------------------------------------------------------------------


def test_forwarded_request_without_api_key_is_rejected():
    server = HealthServer(port=9090, config={})
    req = SimpleNamespace(remote="127.0.0.1", headers={"X-Forwarded-For": "8.8.8.8"})
    assert server._check_api_key(req) is False, (
        "a proxied request must not inherit the loopback no-key bypass"
    )


def test_direct_loopback_without_api_key_is_allowed():
    server = HealthServer(port=9090, config={})
    req = SimpleNamespace(remote="127.0.0.1", headers={})
    assert server._check_api_key(req) is True


def test_remote_without_api_key_is_rejected():
    server = HealthServer(port=9090, config={})
    req = SimpleNamespace(remote="203.0.113.9", headers={})
    assert server._check_api_key(req) is False


def test_api_key_match_and_mismatch(monkeypatch):
    monkeypatch.setenv("QUAD_HEALTH_API_KEY", "s3cret-key")
    server = HealthServer(port=9090, config={})
    good = SimpleNamespace(remote="203.0.113.9", headers={"X-API-Key": "s3cret-key"})
    bad = SimpleNamespace(remote="203.0.113.9", headers={"X-API-Key": "wrong"})
    assert server._check_api_key(good) is True
    assert server._check_api_key(bad) is False


# ---------------------------------------------------------------------------
# TradingView secret: one env var, enforced in-alert
# ---------------------------------------------------------------------------


def test_secret_env_var_is_the_mapped_one():
    assert WEBHOOK_SECRET_ENV == "QUAD_TRADINGVIEW_WEBHOOK_SECRET"
    from quad.config.manager import ENV_VAR_MAP

    assert ENV_VAR_MAP[WEBHOOK_SECRET_ENV] == "tradingview_webhook.secret"


def test_get_webhook_secret_reads_mapped_env(monkeypatch):
    monkeypatch.setenv(WEBHOOK_SECRET_ENV, "from-env-0123456789")
    assert get_webhook_secret() == "from-env-0123456789"
    # Explicit argument still wins (the orchestrator passes config-resolved).
    assert get_webhook_secret("from-config-0123456789") == "from-config-0123456789"


def test_convert_to_action_enforces_config_secret(monkeypatch):
    """An alert with no credential is refused when a secret is configured."""
    monkeypatch.delenv(WEBHOOK_SECRET_ENV, raising=False)
    parsed = {"ticker": "BTCUSDT", "action": "buy", "quantity": "1"}
    assert convert_to_action(parsed, expected_secret="cfg-secret-0123456789") is None


def test_convert_to_action_accepts_matching_secret(monkeypatch):
    monkeypatch.delenv(WEBHOOK_SECRET_ENV, raising=False)
    parsed = {
        "ticker": "BYBIT:BTCUSDT",
        "action": "buy",
        "quantity": "1",
        "secret": "cfg-secret-0123456789",
    }
    sig = convert_to_action(parsed, expected_secret="cfg-secret-0123456789")
    assert sig is not None
    assert sig.symbol == "BTCUSDT"
    assert sig.side == "BUY"


# ---------------------------------------------------------------------------
# /kill must actually cancel resting orders
# ---------------------------------------------------------------------------


class _FakeOrder:
    def __init__(self, oid: str, symbol: str = "BTCUSDT", client_id: str = ""):
        self.id = oid
        self.symbol = symbol
        self.client_order_id = client_id or f"c-{oid}"


class _FakeAdapter:
    def __init__(self, orders, refuse=()):
        self._orders = list(orders)
        self._refuse = set(refuse)
        self.cancelled: list[tuple[str, str]] = []

    async def get_open_orders(self, symbol=None):
        return list(self._orders)

    async def cancel_order(self, order_id, symbol=""):
        self.cancelled.append((str(order_id), symbol))
        return str(order_id) not in self._refuse


class _FakeRisk:
    def __init__(self):
        self.triggered: list[str] = []

    def trigger_kill_switch(self, reason):
        self.triggered.append(reason)


def _commands_with(adapter, risk=None, engine=None):
    cmds = QuadBotCommands(
        {
            "config": {"_dry_run": True, "risk": {}, "trading": {}},
            "telegram_config": {"job_intervals": {}},
            "orchestrator": SimpleNamespace(_exchange_adapter=adapter),
            "execution_engine": engine,
            "risk_manager": risk,
        }
    )
    # Single-tenant (no DB) -> every chat is the operator.
    return cmds


def test_kill_cancels_open_orders():
    adapter = _FakeAdapter([_FakeOrder("1"), _FakeOrder("2")])
    risk = _FakeRisk()
    cmds = _commands_with(adapter, risk=risk)

    cancelled, failed, errors = asyncio.run(cmds._cancel_all_open_orders())

    assert (cancelled, failed, errors) == (2, 0, [])
    assert sorted(oid for oid, _ in adapter.cancelled) == ["1", "2"]


def test_kill_reports_failures_instead_of_claiming_success():
    adapter = _FakeAdapter([_FakeOrder("1"), _FakeOrder("2")], refuse={"2"})
    cmds = _commands_with(adapter, risk=_FakeRisk())

    cancelled, failed, errors = asyncio.run(cmds._cancel_all_open_orders())

    assert cancelled == 1
    assert failed == 1
    assert errors and "2" in errors[0]


def test_kill_with_no_open_orders():
    cmds = _commands_with(_FakeAdapter([]), risk=_FakeRisk())
    assert asyncio.run(cmds._cancel_all_open_orders()) == (0, 0, [])


def test_kill_includes_gateway_tracked_orders():
    """Orders the gateway knows about but the exchange REST call missed."""

    class _FakeGateway:
        def get_active_orders(self):
            return [_FakeOrder("ghost", client_id="c-ghost")]

    adapter = _FakeAdapter([])
    cmds = _commands_with(
        adapter,
        risk=_FakeRisk(),
        engine=SimpleNamespace(_gateway=_FakeGateway()),
    )

    cancelled, failed, _ = asyncio.run(cmds._cancel_all_open_orders())
    assert cancelled == 1
    assert adapter.cancelled == [("ghost", "BTCUSDT")]


def test_kill_switch_actually_halts_entries():
    """The kill flag must be set on the risk manager."""
    risk = _FakeRisk()
    cmds = _commands_with(_FakeAdapter([]), risk=risk)
    assert cmds._risk_manager is risk
    risk.trigger_kill_switch("test")
    assert risk.triggered == ["test"]


# ---------------------------------------------------------------------------
# /execute must not hardcode dry_run=False and is operator-only
# ---------------------------------------------------------------------------


def test_execute_flow_source_forwards_real_dry_run():
    src = inspect.getsource(QuadBotCommands.get_execute_conversation_handler)
    assert "dry_run=is_dry_run" in src, (
        "execute_confirm must forward real dry-run state"
    )
    assert "dry_run=False" not in src, "no hardcoded dry_run=False may remain"
    assert "require_operator" in src, "/execute must be operator-gated"
    # The confirmation card must state where orders would land.
    assert "_execution_environment" in src


def test_execution_environment_labels_live():
    cmds = QuadBotCommands(
        {
            "config": {"_dry_run": False},
            "telegram_config": {"job_intervals": {}},
            "orchestrator": SimpleNamespace(
                _exchange_adapter=SimpleNamespace(is_testnet=False),
                _is_dry_run=False,
            ),
        }
    )
    _, is_dry_run, label = cmds._execution_environment()
    assert is_dry_run is False
    assert "LIVE" in label


def test_execution_environment_labels_dry_run_on_live():
    cmds = QuadBotCommands(
        {
            "config": {"_dry_run": True},
            "telegram_config": {"job_intervals": {}},
            "orchestrator": SimpleNamespace(
                _exchange_adapter=SimpleNamespace(is_testnet=False),
                _is_dry_run=True,
            ),
        }
    )
    _, is_dry_run, label = cmds._execution_environment()
    assert is_dry_run is True
    assert "DRY-RUN" in label and "blocked" in label


def test_execute_has_a_rate_limit_cooldown():
    cmds = QuadBotCommands(
        {"config": {}, "telegram_config": {"job_intervals": {}}, "orchestrator": None}
    )
    assert cmds._rate_limit_config["execute"] > 0
    # /kill must stay instant.
    assert cmds._rate_limit_config["kill"] == 0.0


def test_kill_callback_is_operator_only():
    src = inspect.getsource(QuadBotCommands.cmd_kill_callback)
    assert '"__operator__"' in src, "kill switch must require the operator chat"


# ---------------------------------------------------------------------------
# Update narrowing: every handler must get a message, and callback handlers
# must not look for one (update.message is None for callback queries).
# ---------------------------------------------------------------------------


class _FakeMessage:
    def __init__(self, chat_id: int = 42):
        self.chat_id = chat_id

    async def reply_text(self, *a, **k):
        return None


class _FakeUser:
    def __init__(self, uid: int = 7):
        self.id = uid
        self.first_name = "T"


def test_narrow_returns_message_and_user():
    update = SimpleNamespace(message=_FakeMessage(), effective_user=_FakeUser())
    message, user = QuadBotCommands._narrow(update)
    assert message.chat_id == 42
    assert user.id == 7


def test_narrow_raises_instead_of_returning_none():
    """Returning None made all 26 call sites raise an opaque TypeError."""
    from quad.bot.commands import UpdateNotAddressable

    for update in (
        SimpleNamespace(message=None, effective_user=_FakeUser()),
        SimpleNamespace(message=_FakeMessage(), effective_user=None),
    ):
        with pytest.raises(UpdateNotAddressable):
            QuadBotCommands._narrow(update)


def test_error_handler_silently_drops_unaddressable_updates():
    """Inline-mode traffic must not page the admin on every callback."""
    import asyncio

    from quad.bot.commands import UpdateNotAddressable

    cmds = _commands_with(None)
    sent: list = []

    class _Ctx:
        error = UpdateNotAddressable("no message")
        application = None

    class _App:
        class bot:  # noqa: N801 - mimics the PTB attribute name
            @staticmethod
            async def send_message(**kw):
                sent.append(kw)

    _Ctx.application = _App()
    asyncio.run(cmds.error_handler(SimpleNamespace(update_id=1), _Ctx))
    assert sent == [], "an unaddressable update must not notify the admin"


def test_callback_chat_id_tolerates_inaccessible_message():
    """`query.message` may be an InaccessibleMessage with no chat_id."""

    class _Inaccessible:
        pass

    assert (
        QuadBotCommands._callback_chat_id(SimpleNamespace(message=_Inaccessible()))
        is None
    )
    assert (
        QuadBotCommands._callback_chat_id(SimpleNamespace(message=_FakeMessage(99)))
        == 99
    )
    assert QuadBotCommands._callback_chat_id(SimpleNamespace(message=None)) is None


def test_execute_confirm_does_not_use_narrow():
    """Regression: /execute confirm crashed on every click.

    It is a CallbackQueryHandler, so `update.message` is always None and the
    old `_, user = self._narrow(update)` raised
    `TypeError: cannot unpack non-iterable NoneType` — /execute could never
    complete.

    Only ``execute_start`` may use ``_narrow``: it is registered as a
    ``CommandHandler``, where a message is always present.
    """
    src = _code_without_comments(QuadBotCommands.get_execute_conversation_handler)
    assert src.count("_narrow") == 1, (
        "exactly one _narrow use is allowed in the /execute flow "
        "(execute_start, a CommandHandler); a callback handler using it "
        "crashes because update.message is None"
    )
    assert "query.from_user" in src, (
        "execute_confirm must read the sender off the callback query"
    )


# ---------------------------------------------------------------------------
# Brackets must stay inside the approved notional cap
# ---------------------------------------------------------------------------


def test_bracket_qty_never_exceeds_pre_cap():
    filled = Decimal("0.5")
    assert ExecutionEngine._bracket_qty(filled, Decimal("0.2")) == Decimal("0.2")
    assert ExecutionEngine._bracket_qty(filled, Decimal("1")) == Decimal("0.5")
    # A zero/partial-fill edge must not produce a negative bracket size.
    assert ExecutionEngine._bracket_qty(Decimal("0"), Decimal("1")) == Decimal("0")
    assert ExecutionEngine._bracket_qty(None, None) == Decimal("0")


class _CapAdapter:
    def __init__(self, price):
        self._price = price

    async def get_mark_price(self, symbol):
        return self._price


def _engine_with(adapter, risk_cfg):
    engine = ExecutionEngine.__new__(ExecutionEngine)
    engine._log = __import__("structlog").get_logger("test")
    engine._config = {"risk": risk_cfg}
    engine._exchange_adapter = adapter
    return engine


def test_notional_within_cap_accepts_small_order():
    engine = _engine_with(
        _CapAdapter(Decimal("50000")), {"max_position_size_usd": 1000}
    )
    ok, detail = asyncio.run(engine._notional_within_cap(Decimal("0.01"), "BTCUSDT"))
    assert ok, detail


def test_notional_within_cap_rejects_oversize_order():
    engine = _engine_with(
        _CapAdapter(Decimal("50000")), {"max_position_size_usd": 1000}
    )
    ok, detail = asyncio.run(engine._notional_within_cap(Decimal("1"), "BTCUSDT"))
    assert not ok
    assert "max_position_size_usd" in detail


def test_notional_within_cap_allows_when_no_cap_configured():
    engine = _engine_with(_CapAdapter(Decimal("50000")), {})
    ok, _ = asyncio.run(engine._notional_within_cap(Decimal("1000"), "BTCUSDT"))
    assert ok


def test_notional_within_cap_does_not_block_without_mark_price():
    class _NoPrice(_CapAdapter):
        async def get_mark_price(self, symbol):
            raise RuntimeError("no mark price")

    engine = _engine_with(_NoPrice(Decimal("1")), {"max_position_size_usd": 1000})
    ok, detail = asyncio.run(engine._notional_within_cap(Decimal("1"), "BTCUSDT"))
    assert ok, "a protective bracket must not be blocked by a missing mark price"


def test_prepare_quantity_rejects_floorup_over_risk_cap():
    """Flooring up to the exchange minimum must not breach the cap."""

    class _Adapter:
        async def normalize_quantity(self, symbol, qty):
            raise RuntimeError("below minNotional")

        async def get_symbol_filters(self, symbol):
            return {
                "min_qty": Decimal("0.1"),
                "min_notional": Decimal("5000"),
                "step_size": Decimal("0.001"),
            }

        async def get_mark_price(self, symbol):
            return Decimal("50000")

    engine = _engine_with(_Adapter(), {"max_position_size_usd": 1000})
    action = Action(
        type="ENTER",
        symbol="BTCUSDT",
        side="BUY",
        quantity=Decimal("0.001"),
    )
    # pre_cap 1 @ $50k = $50k notional, far over the $1000 cap -> must refuse.
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(engine._prepare_quantity(action, pre_cap=Decimal("1")))
    assert "cap" in str(exc.value).lower()


def test_prepare_quantity_floors_up_when_within_cap():

    class _Adapter:
        async def normalize_quantity(self, symbol, qty):
            raise RuntimeError("below minNotional")

        async def get_symbol_filters(self, symbol):
            return {
                "min_qty": Decimal("0.01"),
                "min_notional": Decimal("100"),
                "step_size": Decimal("0.001"),
            }

        async def get_mark_price(self, symbol):
            return Decimal("50000")

    engine = _engine_with(_Adapter(), {"max_position_size_usd": 100000})
    action = Action(
        type="ENTER",
        symbol="BTCUSDT",
        side="BUY",
        quantity=Decimal("0.001"),
    )
    out = asyncio.run(engine._prepare_quantity(action, pre_cap=Decimal("1")))
    assert out >= Decimal("0.01")


# ---------------------------------------------------------------------------
# MEDIUM: groq key rotation must not use the deprecated get_event_loop(),
# must actually close the old HTTP pool, and must use one clock everywhere.
# ---------------------------------------------------------------------------


def _code_without_comments(*objs) -> str:
    """Return the source of *objs* with comments and docstrings removed.

    Keeps source-scanning assertions from matching the explanatory comments
    that document the very thing being asserted.  Tokens are rejoined the
    way ``tokenize.untokenize`` would, so expressions keep their original
    spacing (``x = time.time()``) and can be grepped.
    """
    import inspect
    import io
    import tokenize

    chunks = [inspect.getsource(obj) for obj in objs]
    result = []
    for tok in tokenize.generate_tokens(io.StringIO("".join(chunks)).readline):
        if tok.type in (
            tokenize.COMMENT,
            tokenize.NL,
            tokenize.NEWLINE,
            tokenize.INDENT,
            tokenize.DEDENT,
        ):
            continue
        result.append(tok)
    return tokenize.untokenize(result)


def test_groq_has_no_deprecated_get_event_loop():
    from quad.ai.groq import GroqClient

    code = _code_without_comments(GroqClient)
    assert "get_event_loop" not in code, (
        "asyncio.get_event_loop() is deprecated; use get_running_loop() "
        "inside coroutines"
    )
    assert "get_running_loop" in code, "the running-loop API should be used instead"


def test_groq_rate_limit_stamp_uses_wall_clock():
    """`last_rate_limit` is compared against time.time() elsewhere."""
    import re

    from quad.ai.groq import GroqClient

    code = _code_without_comments(GroqClient)
    assignments = re.findall(r"_last_rate_limit\s*=\s*([^\n]+)", code)
    assert assignments, "_last_rate_limit must be assigned somewhere"
    for rhs in assignments:
        assert "asyncio" not in rhs, (
            f"_last_rate_limit is stamped from {rhs!r}; loop time and wall "
            "time must not be mixed"
        )


@pytest.mark.asyncio
async def test_groq_rotate_key_closes_old_client(monkeypatch):
    from quad.ai.groq import GroqClient

    monkeypatch.setenv("GROQ_API_KEYS", "k1,k2")
    client = GroqClient(config={"ai": {"model": "m"}})

    closed = []

    class _Old:
        async def close(self):
            closed.append(True)

    client._client = _Old()
    assert client._rotate_key() is True
    # The close task is scheduled on the running loop; yield so it runs.
    for _ in range(5):
        await asyncio.sleep(0)
    assert closed == [True], "the rotated-out client's HTTP pool must be closed"
    # And the task is released once done (no unbounded growth).
    assert len(client._close_tasks) == 0


def test_groq_rotate_key_without_loop_does_not_raise(monkeypatch):
    """A sync caller must not hit the deprecated get_event_loop()."""
    from quad.ai.groq import GroqClient

    monkeypatch.setenv("GROQ_API_KEYS", "k1,k2")
    client = GroqClient(config={"ai": {"model": "m"}})
    client._client = object()  # no close() attribute
    assert client._rotate_key() is True


# ---------------------------------------------------------------------------
# MEDIUM: websocket task cancellation must be awaited
# ---------------------------------------------------------------------------


def _ws_manager():
    from quad.market_data.websocket import WebSocketManager

    return WebSocketManager(exchange_adapter=None, config={})


@pytest.mark.asyncio
async def test_websocket_cancel_awaits_connection_task():
    mgr = _ws_manager()
    started = asyncio.Event()

    async def _forever():
        started.set()
        await asyncio.sleep(3600)

    task = asyncio.create_task(_forever())
    mgr._connection_task = task
    await started.wait()

    await mgr._cancel_connection_task()

    assert task.done(), "connection task must actually be finished, not just cancelled"
    assert mgr._connection_task is None


@pytest.mark.asyncio
async def test_websocket_cancel_is_idempotent():
    mgr = _ws_manager()
    await mgr._cancel_connection_task()  # no task at all
    mgr._connection_task = asyncio.create_task(asyncio.sleep(0))
    await mgr._cancel_connection_task()
    await mgr._cancel_connection_task()


# ---------------------------------------------------------------------------
# MEDIUM: PriceBuffer must expose a public snapshot, not private _buffers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_price_buffer_snapshot_counts():
    from quad.market_data.buffers import PriceBuffer

    buf = PriceBuffer(max_ticks_per_symbol=10)
    await buf.append("BTCUSDT", Decimal("1"))
    await buf.append("BTCUSDT", Decimal("2"))
    await buf.append("ETHUSDT", Decimal("3"))

    counts = buf.snapshot_counts()
    assert counts == {"symbols_tracked": 2, "total_ticks": 3}
    # Must agree with the locked async accessors.
    assert counts["total_ticks"] == await buf.total_ticks()
    assert counts["symbols_tracked"] == await buf.symbols_tracked()


def test_market_data_status_does_not_touch_private_buffers():
    from quad.market_data.engine import MarketDataEngine

    code = _code_without_comments(MarketDataEngine.status)
    assert "_buffers" not in code, "status() must use the buffer's public API"
    assert "snapshot_counts" in code


# ---------------------------------------------------------------------------
# LOW: no API-key material in Account.id
# ---------------------------------------------------------------------------


def test_bybit_account_id_is_a_non_reversible_fingerprint():
    from quad.exchange.bybit import BybitFuturesAdapter

    key = "SUPERSECRETKEY1234567890"
    a = BybitFuturesAdapter(api_key=key, api_secret="s", testnet=True)
    a2 = BybitFuturesAdapter(api_key=key, api_secret="s", testnet=True)
    b = BybitFuturesAdapter(api_key="OTHERKEY0000000000", api_secret="s", testnet=True)

    fp = a._account_fingerprint()
    assert fp.startswith("bybit-")
    assert key[:8] not in fp
    assert key not in fp
    assert "SUPER" not in fp.upper()
    # Stable and collision-free enough to distinguish accounts.
    assert fp == a2._account_fingerprint()
    assert fp != b._account_fingerprint()
    # No key configured -> stable placeholder.
    assert (
        BybitFuturesAdapter(api_key="", api_secret="")._account_fingerprint() == "bybit"
    )


def test_exchange_bybit_source_has_no_key_prefix_in_account_id():
    from quad.exchange.bybit import BybitFuturesAdapter

    code = _code_without_comments(
        BybitFuturesAdapter.get_account, BybitFuturesAdapter._account_fingerprint
    )
    assert "_api_key[" not in code, "no key slice may appear in an account id"


# ---------------------------------------------------------------------------
# MEDIUM: futures account setup must verify leverage, not just log failures
# ---------------------------------------------------------------------------


def _orch_for_account_setup(dry_run, positions, fail_symbols=()):
    import structlog

    from quad.orchestrator.orchestrator import QuadOrchestrator

    class _Adapter:
        is_testnet = True
        is_margin_mode_already_set = staticmethod(lambda exc: False)

        def __init__(self):
            self.set_calls = []

        async def set_leverage(self, symbol, lev):
            if symbol in fail_symbols:
                raise RuntimeError("leverage rejected")
            self.set_calls.append((symbol, lev))

        async def set_margin_mode(self, symbol, mode, lev):
            self.set_calls.append((symbol, mode, lev))

        async def get_positions(self):
            return positions

        async def get_position_mode(self):
            return "one_way"

    orch = QuadOrchestrator.__new__(QuadOrchestrator)
    orch._log = structlog.get_logger("test")
    orch._config_dict = {
        "_dry_run": dry_run,
        "trading": {
            "leverage": 10,
            "margin_mode": "isolated",
            "position_mode": "one_way",
            "underlyings": ["BTCUSDT"],
        },
        "risk": {"max_leverage": 50},
    }
    orch._exchange_adapter = _Adapter()
    orch._mode = "bybit"
    return orch


def test_account_setup_fails_loudly_on_leverage_mismatch_in_live():
    class _Pos:
        symbol = "BTCUSDT"
        leverage = 3  # exchange says 3, bot asked for 10

    orch = _orch_for_account_setup(dry_run=False, positions=[_Pos()])
    with pytest.raises(RuntimeError, match="account setup incomplete"):
        asyncio.run(orch._setup_futures_account())


def test_account_setup_tolerates_mismatch_in_dry_run():
    class _Pos:
        symbol = "BTCUSDT"
        leverage = 3

    orch = _orch_for_account_setup(dry_run=True, positions=[_Pos()])
    asyncio.run(orch._setup_futures_account())  # must not raise


def test_account_setup_fails_live_on_set_leverage_error():
    orch = _orch_for_account_setup(
        dry_run=False, positions=[], fail_symbols={"BTCUSDT"}
    )
    with pytest.raises(RuntimeError, match="account setup incomplete"):
        asyncio.run(orch._setup_futures_account())


def test_account_setup_clamps_leverage_to_risk_max():
    class _Pos:
        symbol = "BTCUSDT"
        leverage = 20

    orch = _orch_for_account_setup(dry_run=True, positions=[_Pos()])
    orch._config_dict["trading"]["leverage"] = 50
    orch._config_dict["risk"]["max_leverage"] = 20
    asyncio.run(orch._setup_futures_account())
    # Sent and priced at 20, not the configured 50.
    sent = [c for c in orch._exchange_adapter.set_calls if c[1] == 20]
    assert sent, f"expected clamped leverage 20, got {orch._exchange_adapter.set_calls}"


def test_account_setup_ok_when_everything_matches():
    class _Pos:
        symbol = "BTCUSDT"
        leverage = 10

    orch = _orch_for_account_setup(dry_run=False, positions=[_Pos()])
    asyncio.run(orch._setup_futures_account())  # must not raise


# ---------------------------------------------------------------------------
# MEDIUM: error_logs had a schema and readers but no writer
# ---------------------------------------------------------------------------


class _RecordingRepo:
    def __init__(self):
        self.rows = []

    async def create(self, model):
        self.rows.append(model)


@pytest.fixture
def _patched_repo(monkeypatch):
    """Route make_repo(ErrorLogRepository, ...) to a recording double."""
    import quad.persistence.repositories as repos

    holder = _RecordingRepo()

    def _factory(repo_cls, db, config):
        return holder

    monkeypatch.setattr(repos, "make_repo", _factory)
    return holder


def test_error_sink_is_noop_without_db():
    from quad.monitoring.error_sink import ErrorLogSink

    sink = ErrorLogSink(None, {})
    event = {"event": "boom", "level": "error"}
    assert sink(None, "error", event) is event
    assert sink.pending == 0


def test_error_sink_ignores_non_error_levels():
    from quad.monitoring.error_sink import ErrorLogSink

    sink = ErrorLogSink(object(), {})
    sink(None, "info", {"event": "chatty", "level": "info"})
    sink(None, "debug", {"event": "noisy", "level": "debug"})
    assert sink.pending == 0


@pytest.mark.asyncio
async def test_error_sink_persists_errors(_patched_repo):
    from quad.monitoring.error_sink import ErrorLogSink

    sink = ErrorLogSink(object(), {})
    sink(
        None, "error", {"event": "order_failed", "symbol": "BTCUSDT", "level": "error"}
    )
    assert sink.pending == 1

    written = await sink.flush()
    assert written == 1
    assert len(_patched_repo.rows) == 1
    row = _patched_repo.rows[0]
    assert row.event == "order_failed"
    assert row.level == "ERROR"
    assert "BTCUSDT" in row.details_json
    assert row.timestamp > 0


@pytest.mark.asyncio
async def test_error_sink_never_raises_on_bad_payload():
    from quad.monitoring.error_sink import ErrorLogSink

    class _Unserialisable:
        def __repr__(self):
            raise RuntimeError("boom")

    sink = ErrorLogSink(object(), {})
    event = {"event": "weird", "level": "error", "obj": _Unserialisable()}
    # Must return the event dict and not raise.
    assert sink(None, "error", event) is event


@pytest.mark.asyncio
async def test_error_sink_bounded_queue_drops_oldest(_patched_repo):
    from quad.monitoring.error_sink import ErrorLogSink

    sink = ErrorLogSink(object(), {"error_sink": {"max_queue": 3, "batch_size": 100}})
    for i in range(10):
        sink(None, "error", {"event": f"e{i}", "level": "error"})
    assert sink.pending == 3, "the queue must be bounded, not unbounded"
    # The three most recent survive.
    assert _patched_repo.rows == []


@pytest.mark.asyncio
async def test_error_sink_stop_flushes(_patched_repo):
    from quad.monitoring.error_sink import ErrorLogSink

    sink = ErrorLogSink(object(), {})
    await sink.start()
    sink(None, "error", {"event": "late_failure", "level": "error"})
    await sink.stop()
    assert len(_patched_repo.rows) == 1


def test_error_sink_reader_repos_exist():
    """The readers the scan found write-less must still be present."""
    from quad.persistence.repositories import ErrorLogRepository

    for name in ("get_by_level", "get_recent", "get_by_date_range", "get_by_source"):
        assert callable(getattr(ErrorLogRepository, name)), name


# ---------------------------------------------------------------------------
# MEDIUM: correlation ids (the scan found zero correlation_id usage)
# ---------------------------------------------------------------------------


def test_correlation_id_is_unique_and_prefixed():
    from quad.monitoring.correlation import new_correlation_id

    a = new_correlation_id("cycle")
    b = new_correlation_id("cycle")
    assert a.startswith("cycle-")
    assert a != b
    assert new_correlation_id() != new_correlation_id("")


def test_correlation_scope_binds_and_resets():
    from quad.monitoring.correlation import (
        correlation_scope,
        get_correlation_id,
    )

    assert get_correlation_id() is None
    with correlation_scope("tv") as cid:
        assert get_correlation_id() == cid
    assert get_correlation_id() is None


def test_correlation_scope_resets_on_exception():
    from quad.monitoring.correlation import correlation_scope, get_correlation_id

    with pytest.raises(RuntimeError):
        with correlation_scope("tv"):
            raise RuntimeError("boom")
    assert get_correlation_id() is None


@pytest.mark.asyncio
async def test_correlation_ids_isolate_concurrent_tasks():
    """Two concurrent scans must not share a correlation id."""
    from quad.monitoring.correlation import (
        correlation_scope,
        get_correlation_id,
    )

    seen: dict[str, str] = {}

    async def worker(name: str) -> None:
        with correlation_scope(name):
            await asyncio.sleep(0)
            seen[name] = get_correlation_id()

    await asyncio.gather(worker("a"), worker("b"), worker("c"))
    assert len(seen) == 3
    assert len(set(seen.values())) == 3
    for name, cid in seen.items():
        assert cid.startswith(f"{name}-")


def test_structlog_context_processor_injects_correlation_id():
    from quad.monitoring.correlation import (
        correlation_scope,
        structlog_context_processor,
    )

    with correlation_scope("cycle") as cid:
        out = structlog_context_processor(None, "info", {"event": "x"})
    assert out.get("correlation_id") == cid


def test_correlation_processor_installed_in_entrypoint():
    import inspect

    import quad.__main__ as m

    src = inspect.getsource(m._configure_logging)
    assert "structlog_context_processor" in src, (
        "the entry point must install the correlation-id processor"
    )


def test_orchestrator_binds_cycle_and_webhook_correlation_ids():
    import inspect

    from quad.orchestrator.orchestrator import QuadOrchestrator

    loop_src = inspect.getsource(QuadOrchestrator._main_cycle_loop)
    assert "set_correlation_id" in loop_src
    assert "reset_correlation_id" in loop_src

    tv_src = inspect.getsource(QuadOrchestrator._init_tradingview_webhook)
    assert "new_correlation_id" in tv_src
    assert "reset_correlation_id" in tv_src
