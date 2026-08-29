"""Regression tests for the 2026-08-16 one-trade-per-cycle fixes.

Covers:
- Order status/cancel calls now include the required ``symbol``.
- ENTER is blocked unless the account is flat (serial mode).
- ENTER is blocked when SL/TP brackets cannot be computed.
- ENTER / EXIT Telegram alerts include SL/TP and PnL.
- RiskManager.get_status() computes real daily PnL from persisted trades.
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from quad.exchange.base import ExchangeAdapter
from quad.exchange.okx import OkxFuturesAdapter
from quad.orchestrator.orchestrator import QuadOrchestrator
from quad.risk.sizing import PositionSizer
from quad.types.risk import Action
from quad.types.domain import Order, Position, PositionSide, PositionStatus
from quad.types.strategy import StrategyContext


def _orch(**overrides) -> QuadOrchestrator:
    orch = QuadOrchestrator.__new__(QuadOrchestrator)
    cfg = {
        "trading": {"serial_trade_mode": True, "leverage": 50},
        "ai": {
            "pairs": ["BTCUSDT", "ETHUSDT"],
            "rotation": {"enabled": True, "close_open_position_each_cycle": True},
        },
        "risk": {
            "per_position_sl": {"enabled": True, "capital_pct": 30.0},
            "per_position_tp": {"enabled": True, "capital_pct": 50.0},
        },
    }
    cfg["trading"].update(overrides.pop("trading", {}))
    cfg["risk"].update(overrides.pop("risk", {}))
    cfg["ai"].update(overrides.pop("ai", {}))
    orch._config_dict = cfg
    orch._log = MagicMock()
    orch._rotation_hold_since = {}
    orch._rotation_index = 0
    orch._telegram_bot = None
    orch._telegram_chat_id = 0
    orch._current_symbol = None
    orch._metrics_cycle_count = 0
    orch._num_cycles = 0
    return orch


def _open_position(symbol: str = "BTCUSDT", side: PositionSide = PositionSide.LONG) -> Position:
    return Position(
        symbol=symbol,
        side=side,
        quantity=Decimal("0.01"),
        status=PositionStatus.OPEN,
        entry_price=Decimal("60000"),
        current_price=Decimal("61000"),
    )


# ---------------------------------------------------------------------------
# 1. Symbol parameter propagation (fixes -1102 flood)
# ---------------------------------------------------------------------------


def test_base_adapter_signatures_accept_symbol():
    import inspect

    sig = inspect.signature(ExchangeAdapter.cancel_order)
    assert "symbol" in sig.parameters
    sig = inspect.signature(ExchangeAdapter.get_order_status)
    assert "symbol" in sig.parameters
    sig = inspect.signature(OkxFuturesAdapter.cancel_order)
    assert "symbol" in sig.parameters
    sig = inspect.signature(OkxFuturesAdapter.get_order_status)
    assert "symbol" in sig.parameters


@pytest.mark.asyncio
async def test_gateway_passes_symbol_to_get_order_status():
    from quad.execution.gateway import OrderGateway

    exchange = AsyncMock(spec=OkxFuturesAdapter)
    exchange.get_order_status = AsyncMock(
        return_value=Order(id=42, symbol="BTCUSDT", status="FILLED")
    )
    gateway = OrderGateway(
        exchange,
        config={
            "exchange": {
                "gateway": {
                    "completed_ids_maxlen": 100,
                    "confirmation_timeout_seconds": 5,
                    "max_retries": 1,
                    "backoff_base_seconds": 0.1,
                }
            }
        },
    )
    gateway._active_orders["c1"] = Order(
        id=42, symbol="BTCUSDT", status="NEW"
    )

    order = await gateway.get_status("c1")

    assert order is not None
    exchange.get_order_status.assert_awaited_once_with(42, "BTCUSDT")


@pytest.mark.asyncio
async def test_gateway_passes_symbol_to_cancel():
    from quad.execution.gateway import OrderGateway

    exchange = AsyncMock(spec=OkxFuturesAdapter)
    exchange.cancel_order = AsyncMock(return_value=True)
    gateway = OrderGateway(
        exchange,
        config={
            "exchange": {
                "gateway": {
                    "completed_ids_maxlen": 100,
                    "confirmation_timeout_seconds": 5,
                    "max_retries": 1,
                    "backoff_base_seconds": 0.1,
                }
            }
        },
    )
    gateway._active_orders["c1"] = Order(
        id=42, symbol="BTCUSDT", status="NEW"
    )

    result = await gateway.cancel("c1")

    assert result is True
    exchange.cancel_order.assert_awaited_once_with(42, "BTCUSDT")


@pytest.mark.asyncio
async def test_reconciler_passes_symbol():
    from quad.execution.reconciler import FillReconciler

    exchange = AsyncMock(spec=OkxFuturesAdapter)
    exchange.get_order_status = AsyncMock(
        return_value=Order(id=42, symbol="BTCUSDT", status="FILLED")
    )
    reconciler = FillReconciler(
        exchange,
        config={
            "exchange": {
                "reconciler": {
                    "max_discrepancy_history": 100,
                    "stale_order_hours": 24,
                }
            }
        },
    )
    calls = []

    def fake_record_discrepancy(*a, **k):
        calls.append(a)
        return {
            "type": a[0],
            "client_order_id": "",
            "exchange_order_id": 42,
            "symbol": "BTCUSDT",
            "local_status": a[1].status,
            "exchange_status": a[2],
            "timestamp": 0,
            "details": {},
        }

    reconciler._record_discrepancy = fake_record_discrepancy
    order = Order(id=42, symbol="BTCUSDT", status="NEW")

    disc = await reconciler.reconcile_pending_orders([order])

    exchange.get_order_status.assert_awaited_once_with(42, "BTCUSDT")
    assert calls
    assert disc and disc[0]["type"] == "MISSED_FILL"


# ---------------------------------------------------------------------------
# 2. One-trade-per-cycle: flatten before ENTER
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enter_blocked_when_close_not_flat():
    orch = _orch()
    orch._exchange_adapter = AsyncMock()
    orch._exchange_adapter.get_positions = AsyncMock(return_value=[])
    orch._close_all_positions = AsyncMock(return_value=False)
    orch._execution_engine = AsyncMock()
    orch._risk_manager = AsyncMock()
    orch._db_manager = None

    decision = {
        "action": "ENTER",
        "contract": "BTCUSDT",
        "quantity": 0.01,
        "side": "BUY",
        "direction": "LONG",
        "strategy": "ai_default",
        "reasoning": "test",
        "confidence": 0.9,
    }
    from quad.ai.validator import canonical_direction, derive_side
    from quad.types.domain import PositionSide as PS

    result = await orch._execute_ai_action(
        decision, StrategyContext(config=orch._config_dict)
    )

    assert result is False
    orch._close_all_positions.assert_awaited_once()
    orch._execution_engine.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_enter_blocked_when_positions_open_without_serial():
    orch = _orch()
    orch._config_dict["trading"]["serial_trade_mode"] = False
    orch._exchange_adapter = AsyncMock()
    orch._exchange_adapter.get_positions = AsyncMock(
        return_value=[_open_position("BTCUSDT")]
    )
    orch._execution_engine = AsyncMock()
    orch._risk_manager = AsyncMock()
    orch._db_manager = None

    decision = {
        "action": "ENTER",
        "contract": "ETHUSDT",
        "quantity": 0.01,
        "side": "BUY",
        "direction": "LONG",
        "strategy": "ai_default",
        "reasoning": "test",
        "confidence": 0.9,
    }
    result = await orch._execute_ai_action(
        decision, StrategyContext(config=orch._config_dict)
    )

    assert result is False
    orch._execution_engine.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_enter_blocked_when_brackets_missing():
    orch = _orch()
    orch._exchange_adapter = AsyncMock()
    orch._exchange_adapter.get_positions = AsyncMock(return_value=[])
    orch._exchange_adapter.get_mark_price = AsyncMock(return_value=None)
    orch._close_all_positions = AsyncMock(return_value=True)
    orch._execution_engine = AsyncMock()
    orch._risk_manager = AsyncMock()
    orch._db_manager = None

    decision = {
        "action": "ENTER",
        "contract": "BTCUSDT",
        "quantity": 0.01,
        "side": "BUY",
        "direction": "LONG",
        "strategy": "ai_default",
        "reasoning": "test",
        "confidence": 0.9,
    }
    result = await orch._execute_ai_action(
        decision, StrategyContext(config=orch._config_dict)
    )

    assert result is False
    orch._execution_engine.execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# 3. SL/TP + PnL appear in alerts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_trade_includes_sl_tp_on_enter():
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    orch = _orch()
    orch._telegram_bot = bot
    orch._telegram_chat_id = 12345

    await orch._notify_trade(
        action_type="ENTER",
        strategy="ai_default",
        contract="BTCUSDT",
        side="BUY",
        quantity="0.01",
        price="65000",
        reason="test",
        stop_loss=Decimal("64610"),
        take_profit=Decimal("65650"),
    )

    text = bot.send_message.await_args.kwargs["text"]
    assert "SL:" in text
    assert "64610" in text
    assert "TP:" in text
    assert "65650" in text


@pytest.mark.asyncio
async def test_notify_trade_includes_pnl_on_exit():
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    orch = _orch()
    orch._telegram_bot = bot
    orch._telegram_chat_id = 12345

    await orch._notify_trade(
        action_type="EXIT",
        strategy="rotation_roll",
        contract="BTCUSDT",
        side="LONG",
        quantity="0.01",
        price=None,
        reason="Rotation cycle",
        pnl="$10.00 (+1.67%)",
    )

    text = bot.send_message.await_args.kwargs["text"]
    assert "PnL:" in text
    assert "$10.00" in text
    assert "+1.67%" in text


def test_side_label_normalizes_enum():
    assert QuadOrchestrator._side_label(PositionSide.LONG) == "LONG"
    assert QuadOrchestrator._side_label("PositionSide.SHORT") == "SHORT"
    assert QuadOrchestrator._side_label("BUY") == "BUY"


def test_compute_position_pnl_long_and_short():
    orch = _orch()
    long_pnl = orch._compute_position_pnl(
        entry_price=Decimal("60000"),
        exit_price=Decimal("61000"),
        quantity=Decimal("0.01"),
        side="LONG",
    )
    assert long_pnl == Decimal("10")
    short_pnl = orch._compute_position_pnl(
        entry_price=Decimal("60000"),
        exit_price=Decimal("59000"),
        quantity=Decimal("0.01"),
        side="SHORT",
    )
    assert short_pnl == Decimal("10")


# ---------------------------------------------------------------------------
# 4. Daily PnL from persisted trades
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_risk_status_computes_daily_pnl():
    from quad.risk.manager import RiskManager

    db = MagicMock()
    db.is_connected = True
    db.pool = AsyncMock()

    class FakeTrade:
        def __init__(self, pnl: str):
            self.pnl = pnl

    async def fake_range(self, start, end):
        return [FakeTrade("10.5"), FakeTrade("-3.25"), FakeTrade("0")]

    repo_cls_patch = "quad.persistence.repositories.TradeRepository.get_by_date_range"
    from unittest.mock import patch

    rm = RiskManager.__new__(RiskManager)
    rm._db = db
    rm._config = {"risk": {"max_daily_loss_usd": 500.0}, "max_daily_loss_usd": 500.0}
    rm._log = MagicMock()
    rm._breakers = MagicMock()
    rm._breakers.status = MagicMock(return_value={})
    rm._gates = MagicMock()
    rm._gates.get_gate_status = MagicMock(return_value={})
    with patch(repo_cls_patch, new=fake_range):
        status = await rm.get_status()

    assert status.daily_pnl == Decimal("7.25")
    assert status.daily_loss_limit == Decimal("500")


@pytest.mark.asyncio
async def test_engine_persists_trade_after_fill():
    from quad.execution.engine import ExecutionEngine
    from quad.types.risk import Action
    from quad.types.domain import OrderResult

    db = MagicMock()
    db.is_connected = True

    created = []

    class FakeTradeRepo:
        def __init__(self, dbm):
            self._db = dbm

        async def create(self, model):
            created.append(model)
            return 1

    from quad.persistence.repositories import TradeRepository

    original = TradeRepository.create
    TradeRepository.create = FakeTradeRepo.create  # type: ignore[assignment]
    try:
        gateway_cfg = {
            "exchange": {
                "gateway": {
                    "completed_ids_maxlen": 100,
                    "confirmation_timeout_seconds": 5,
                    "max_retries": 1,
                    "backoff_base_seconds": 0.1,
                },
                "reconciler": {
                    "max_discrepancy_history": 100,
                    "stale_order_hours": 24,
                },
            },
            "execution": {"default_order_type": "MARKET"},
        }
        engine = ExecutionEngine(
            exchange_adapter=AsyncMock(spec=OkxFuturesAdapter),
            risk_manager=MagicMock(),
            db_manager=db,
            config=gateway_cfg,
        )
        # risk evaluate returns a passing result with a sized action
        risk = MagicMock()
        risk.evaluate = AsyncMock(
            return_value=MagicMock(passed=True, details={"action": None})
        )
        engine._risk_manager = risk
        exchange = AsyncMock()
        exchange.normalize_quantity = AsyncMock(return_value=Decimal("0.01"))
        exchange.is_testnet = True
        exchange.place_order = AsyncMock(
            return_value=OrderResult(
                order_id=1,
                symbol="BTCUSDT",
                side="SELL",
                order_type="MARKET",
                quantity=Decimal("0.01"),
                price=Decimal("61000"),
                status="FILLED",
                fills=[{"price": "61000", "qty": "0.01"}],
            )
        )
        gateway = MagicMock()
        gateway.submit = AsyncMock(
            return_value=OrderResult(
                order_id=1,
                symbol="BTCUSDT",
                side="SELL",
                order_type="MARKET",
                quantity=Decimal("0.01"),
                price=Decimal("61000"),
                status="FILLED",
                fills=[{"price": "61000", "qty": "0.01"}],
            )
        )
        engine._gateway = gateway
        engine._exchange_adapter = exchange
        engine._config = gateway_cfg
        engine._config["_dry_run"] = False

        action = Action(
            type="EXIT",
            strategy="serial_close",
            contract="BTCUSDT",
            side="SELL",
            quantity=Decimal("0.01"),
            order_type="MARKET",
            metadata={
                "serial_close": True,
                "entry_price": "60000",
                "position_side": "LONG",
            },
            risk_checked=True,
        )
        await engine.execute(action, StrategyContext(config={}))

        assert created, "expected a trade to be persisted"
        trade = created[0]
        assert trade.symbol == "BTCUSDT"
        assert Decimal(trade.pnl) == Decimal("10")  # (61000 - 60000) * 0.01
    finally:
        TradeRepository.create = original


# ---------------------------------------------------------------------------
# 5. Serial-close quantity is preserved through risk sizing (2026-08-17)
# ---------------------------------------------------------------------------


def test_sizer_preserves_serial_close_quantity():
    """A serial-close EXIT must keep its exact held quantity.

    Regression for 2026-08-17 runtime logs: every hourly rotation failed with
    ``order quantity is zero/negative after sizing (0.00)`` because the
    PositionSizer replaced the close quantity with a Kelly/default notional
    derived from the portfolio value.  With no trade history the default
    fraction of a $10k portfolio is far larger than the held BTC qty, and
    the resulting "sized" quantity was zeroed by the engine's normalize step
    for a symbol with a large minQty, leaving both positions open forever.
    """
    sizer = PositionSizer.__new__(PositionSizer)
    sizer._log = MagicMock()
    sizer._default_fraction = 0.02
    sizer._max_pos_usd = Decimal("1000")
    sizer._min_pos_usd = Decimal("5")
    sizer._kelly_multiplier = 0.25
    sizer._trade_capital_usd = Decimal("5")
    sizer._max_leverage = 50
    sizer._sl_enabled = True
    sizer._cfg = {"per_position_sl": {"enabled": True}}

    from quad.types.strategy import StrategyContext
    from quad.types.domain import Account

    context = StrategyContext(
        config={},
        account=Account(id="x", exchange="okx", total_usdt=Decimal("10000")),
    )
    close = Action(
        type="EXIT",
        strategy="serial_close",
        contract="BTCUSDT",
        side="SELL",
        quantity=Decimal("0.3"),
        order_type="MARKET",
        metadata={"serial_close": True, "entry_price": "62000"},
    )

    sized = __import__("asyncio").run(sizer.compute_size(close, context))

    assert sized.quantity == Decimal("0.3")  # exact held quantity preserved


# ---------------------------------------------------------------------------
# 5. Bug fixes: REJECTED order result + executed flag + stale PnL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_ai_action_returns_false_on_rejected_status():
    """Bug 1: a REJECTED OrderResult must return False so the rotation loop
    does not advance as if a position opened."""
    from quad.types.domain import OrderResult

    orch = _orch()
    orch._exchange_adapter = AsyncMock()
    orch._exchange_adapter.get_positions = AsyncMock(return_value=[])
    orch._close_all_positions = AsyncMock(return_value=True)
    orch._risk_manager = AsyncMock()
    orch._risk_manager.evaluate = AsyncMock(
        return_value=MagicMock(passed=True, reason="", gate="", details={})
    )
    orch._execution_engine = AsyncMock()
    orch._execution_engine.execute = AsyncMock(
        return_value=OrderResult(
            order_id=0,
            symbol="BTCUSDT",
            side="BUY",
            status="REJECTED",
            fills=[],
        )
    )
    orch._db_manager = None
    orch._compute_bracket_prices = AsyncMock(
        return_value=(Decimal("64610"), Decimal("65650"))
    )
    orch._notify_trade = AsyncMock()

    decision = {
        "action": "ENTER",
        "contract": "BTCUSDT",
        "quantity": 0.01,
        "side": "BUY",
        "direction": "LONG",
        "strategy": "ai_default",
        "reasoning": "test",
        "confidence": 0.9,
    }
    result = await orch._execute_ai_action(
        decision, StrategyContext(config=orch._config_dict)
    )

    assert result is False
    orch._notify_trade.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_ai_action_updates_executed_flag_on_fill():
    """Bug 2: on a FILLED order the decision row must be marked executed=1."""
    from quad.types.domain import OrderResult
    from quad.types.risk import Action

    orch = _orch()
    # Wire a mock DB manager with a DecisionRepository that records update() calls.
    mock_db = MagicMock()
    orch._db_manager = mock_db

    repo_calls: list = []

    class FakeRepo:
        def __init__(self, *a, **kw):
            pass

        def update(self, id, **updates):
            repo_calls.append((id, updates))

    import quad.persistence.repositories as repos_mod

    orig_repo = getattr(repos_mod, "DecisionRepository", None)
    orch._exchange_adapter = AsyncMock()
    orch._exchange_adapter.get_positions = AsyncMock(return_value=[])
    orch._close_all_positions = AsyncMock(return_value=True)
    orch._risk_manager = AsyncMock()
    orch._risk_manager.evaluate = AsyncMock(
        return_value=MagicMock(passed=True, reason="", gate="", details={})
    )
    orch._execution_engine = AsyncMock()
    orch._execution_engine.execute = AsyncMock(
        return_value=OrderResult(
            order_id=1,
            symbol="BTCUSDT",
            side="BUY",
            status="FILLED",
            fills=[{"price": "65000", "qty": "0.01"}],
        )
    )
    orch._compute_bracket_prices = AsyncMock(
        return_value=(Decimal("64610"), Decimal("65650"))
    )
    orch._notify_trade = AsyncMock()

    decision = {
        "action": "ENTER",
        "contract": "BTCUSDT",
        "quantity": 0.01,
        "side": "BUY",
        "direction": "LONG",
        "strategy": "ai_default",
        "reasoning": "test",
        "confidence": 0.9,
        "db_id": 42,
    }

    # Monkeypatch DecisionRepository inside the method's local import.
    import src.quad.orchestrator.orchestrator as orch_mod_mod

    orig = orch_mod_mod.DecisionRepository if hasattr(orch_mod_mod, "DecisionRepository") else None

    # The method does `from quad.persistence.repositories import DecisionRepository`
    # at call time, so patch the module attribute.
    import sys

    quad_repos = sys.modules.get("quad.persistence.repositories")
    if quad_repos:
        orig_repo_obj = quad_repos.DecisionRepository
        quad_repos.DecisionRepository = FakeRepo
    try:
        result = await orch._execute_ai_action(
            decision, StrategyContext(config=orch._config_dict)
        )
    finally:
        if quad_repos:
            quad_repos.DecisionRepository = orig_repo_obj

    assert result is True
    assert repo_calls == [(42, {"executed": 1})]


@pytest.mark.asyncio
async def test_build_exit_pnl_uses_entry_price_hint_when_position_stale():
    """Bug 2 (PnL): when the live position book is stale (held=None), the
    entry price from the action metadata must be used instead of returning None."""
    from quad.types.domain import OrderResult

    orch = _orch()
    orch._exchange_adapter = AsyncMock()
    orch._exchange_adapter.get_positions = AsyncMock(return_value=[])
    orch._exchange_adapter.get_mark_price = AsyncMock(return_value="66000")
    orch._exchange_adapter.get_order_realized_pnl = AsyncMock(return_value=Decimal(0))

    order_result = OrderResult(
        order_id=1,
        symbol="BTCUSDT",
        side="SELL",
        status="FILLED",
        fills=[{"price": "66000", "qty": "0.01"}],
    )

    pnl = await orch._build_exit_pnl_text(
        "BTCUSDT",
        "LONG",
        Decimal("0.01"),
        order_result,
        entry_price_hint=Decimal("60000"),
    )
    # (66000 - 60000) * 0.01 = 60.00
    assert pnl is not None
    assert "$60.00" in pnl


@pytest.mark.asyncio
async def test_build_exit_pnl_returns_none_when_no_entry_or_exit_price():
    """Bug 2 (PnL): when neither the live position nor the hint provides an
    entry price AND no exit price can be derived, return None."""
    from quad.types.domain import OrderResult

    orch = _orch()
    orch._exchange_adapter = AsyncMock()
    orch._exchange_adapter.get_positions = AsyncMock(return_value=[])
    orch._exchange_adapter.get_mark_price = AsyncMock(return_value="0")
    orch._exchange_adapter.get_order_realized_pnl = AsyncMock(return_value=Decimal(0))

    order_result = OrderResult(
        order_id=1,
        symbol="BTCUSDT",
        side="SELL",
        status="FILLED",
        fills=[{"price": "0", "qty": "0.01"}],
    )

    pnl = await orch._build_exit_pnl_text(
        "BTCUSDT",
        "LONG",
        Decimal("0.01"),
        order_result,
        entry_price_hint=None,
    )
    assert pnl is None


@pytest.mark.asyncio
async def test_build_exit_pnl_prefers_exchange_order_realized_pnl():
    """Adjustment: PnL for a closed trade must come directly from the exchange
    via GET /v5/order/history's realizedPnl for the specific closing order, NOT
    from mark-price fallbacks or FIFO computation on stale execution-list scans.
    """
    from quad.types.domain import OrderResult

    orch = _orch()
    orch._exchange_adapter = AsyncMock()
    orch._exchange_adapter.get_positions = AsyncMock(return_value=[])
    orch._exchange_adapter.get_mark_price = AsyncMock(
        return_value="999999"  # deliberately wrong — must NOT be used
    )

    # The exchange returns realizedPnl=50.00 for order_id=42 via /v5/order/info
    orch._exchange_adapter.get_order_realized_pnl = AsyncMock(
        return_value=Decimal("50.00")
    )

    order_result = OrderResult(
        order_id=42,
        symbol="BTCUSDT",
        side="SELL",
        status="FILLED",
        fills=[{"price": "65000", "qty": "0.01"}],
    )

    pnl = await orch._build_exit_pnl_text(
        "BTCUSDT",
        "LONG",
        Decimal("0.01"),
        order_result,
        entry_price_hint=Decimal("60000"),
    )
    # Must use the exchange's 50.00 via get_order_realized_pnl, not the
    # mark-price fallback (999999) or the computed (65000-60000)*0.01 = 50.00.
    assert pnl is not None
    assert "50.00" in pnl
    # Must have called the order-level PnL endpoint, not get_user_trades
    orch._exchange_adapter.get_order_realized_pnl.assert_awaited_once_with(42, "BTCUSDT")
    orch._exchange_adapter.get_mark_price.assert_not_awaited()


@pytest.mark.asyncio
async def test_build_exit_pnl_falls_back_to_computed_when_exchange_pnl_zero():
    """When the exchange returns realizedPnl=0 (no close fill yet visible),
    the computed PnL fallback is used (not the mark price if fills are present)."""
    from quad.types.domain import OrderResult

    orch = _orch()
    orch._exchange_adapter = AsyncMock()
    orch._exchange_adapter.get_positions = AsyncMock(return_value=[])
    orch._exchange_adapter.get_mark_price = AsyncMock(
        return_value="999999"  # must NOT be used since fills provide exit price
    )
    orch._exchange_adapter.get_order_realized_pnl = AsyncMock(
        return_value=Decimal("0")  # exchange has no realized PnL yet
    )

    order_result = OrderResult(
        order_id=42,
        symbol="BTCUSDT",
        side="SELL",
        status="FILLED",
        fills=[{"price": "65000", "qty": "0.01"}],
    )

    pnl = await orch._build_exit_pnl_text(
        "BTCUSDT",
        "LONG",
        Decimal("0.01"),
        order_result,
        entry_price_hint=Decimal("60000"),
    )
    # Falls back to computed: (65000 - 60000) * 0.01 = 50.00
    assert pnl is not None
    assert "50.00" in pnl
    # mark_price must NOT be consulted since fills provide the exit price
    orch._exchange_adapter.get_mark_price.assert_not_awaited()
