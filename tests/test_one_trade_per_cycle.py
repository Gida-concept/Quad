"""Regression tests for the 2026-08-16 one-trade-per-cycle fixes.

Covers:
- Binance order status/cancel calls now include the required ``symbol``.
- ENTER is blocked unless the account is flat (serial mode).
- ENTER is blocked when SL/TP brackets cannot be computed.
- ENTER / EXIT Telegram alerts include SL/TP and PnL.
- RiskManager.get_status() computes real daily PnL from persisted trades.
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from quad.exchange.base import ExchangeAdapter
from quad.exchange.binance import BinanceFuturesAdapter
from quad.exchange.mock import MockAdapter
from quad.orchestrator.orchestrator import QuadOrchestrator
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
    sig = inspect.signature(BinanceFuturesAdapter.cancel_order)
    assert "symbol" in sig.parameters
    sig = inspect.signature(BinanceFuturesAdapter.get_order_status)
    assert "symbol" in sig.parameters
    sig = inspect.signature(MockAdapter.get_order_status)
    assert "symbol" in sig.parameters


@pytest.mark.asyncio
async def test_gateway_passes_symbol_to_get_order_status():
    from quad.execution.gateway import OrderGateway

    exchange = AsyncMock(spec=MockAdapter)
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

    exchange = AsyncMock(spec=MockAdapter)
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

    exchange = AsyncMock(spec=MockAdapter)
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
            exchange_adapter=AsyncMock(spec=MockAdapter),
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
