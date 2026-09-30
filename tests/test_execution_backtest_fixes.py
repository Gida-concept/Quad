"""Regression tests for execution + backtest bug fixes (minimal-diff batch).

Covers: _persist_trade fee-aware PnL, ingest close-vs-open pairing,
gateway terminal-state eviction, documented backoff, confirmation ack,
reconciler CANCELLED detection + side/symbol dedup key, TWAP window /
urgency / step alignment / flag preservation, backtest margin cash model,
side-aware EXIT PnL, round-trip metrics, max_trades_per_day.
"""

import asyncio
import sys
import time
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from quad.backtesting.engine import BacktestEngine  # noqa: E402
from quad.execution.engine import ExecutionEngine  # noqa: E402
from quad.execution.gateway import OrderGateway  # noqa: E402
from quad.execution.reconciler import FillReconciler  # noqa: E402
from quad.execution.twap import TwapSlicer  # noqa: E402
from quad.types.domain import Order, OrderRequest, OrderResult, Trade  # noqa: E402
from quad.types.risk import Action  # noqa: E402

GW_CFG = {
    "exchange": {
        "gateway": {
            "completed_ids_maxlen": 100,
            "confirmation_timeout_seconds": 5,
            "max_retries": 3,
            "backoff_base_seconds": 2.0,
        },
        "reconciler": {
            "max_discrepancy_history": 50,
            "stale_order_hours": 24,
        },
    }
}

BT_CFG = {
    "starting_capital": Decimal(1000),
    "commission_pct": Decimal("0.001"),
    "slippage_pct": Decimal(0),
    "max_trades_per_day": 10,
}


def _filled_result(cid="cid-1"):
    return OrderResult(
        order_id=111,
        client_order_id=cid,
        symbol="BTCUSDT",
        side="BUY",
        order_type="MARKET",
        quantity=Decimal(1),
        filled_qty=Decimal(1),
        price=Decimal(100),
        status="FILLED",
        fills=[{"price": "100", "qty": "1", "commission": "0.05"}],
    )


# ---------------------------------------------------------------------------
# (1) _persist_trade: fee-aware PnL, real fee stored
# ---------------------------------------------------------------------------


def test_persist_trade_subtracts_fee_and_stores_it(monkeypatch):
    captured = {}

    class FakeRepo:
        async def create(self, model):
            captured["model"] = model

    import quad.persistence.repositories as repos

    monkeypatch.setattr(repos, "make_repo", lambda *a, **k: FakeRepo())
    eng = ExecutionEngine(
        MagicMock(), MagicMock(), db_manager=None, config=dict(GW_CFG)
    )
    eng._db_manager = SimpleNamespace(is_connected=True)
    action = Action(
        type="EXIT",
        contract="BTCUSDT",
        side="SELL",
        quantity=Decimal(1),
        metadata={"entry_price": "100", "position_side": "LONG"},
    )
    result = _filled_result()
    asyncio.run(eng._persist_trade(action, result, fee=Decimal("0.05")))
    model = captured["model"]
    # fill price is 100 (fills[-1] price), entry 100 -> pnl = -fee
    assert Decimal(str(model.pnl)) == Decimal("-0.05")
    assert Decimal(str(model.fee)) == Decimal("0.05")


def test_fill_fee_sums_commission_and_funding():
    eng = ExecutionEngine(
        MagicMock(), MagicMock(), db_manager=None, config=dict(GW_CFG)
    )
    result = _filled_result()
    action = Action(type="EXIT", metadata={"funding_paid": "0.02"})
    assert eng._fill_fee(result, action) == Decimal("0.07")


# ---------------------------------------------------------------------------
# (2) ingest: closes must not join open queues; None-check for exchange pnl
# ---------------------------------------------------------------------------


def _leg(**kw):
    base = dict(
        symbol="BTCUSDT",
        order_id=kw.pop("order_id", 1),
        timestamp=kw.pop("timestamp", 1),
        quantity=Decimal(1),
        fee=Decimal(0),
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_ingest_close_does_not_requeue_and_fifo_used(monkeypatch):
    created = []

    class FakeRepo:
        async def exists_for_order(self, *a):
            return False

        async def create(self, model):
            created.append(model)

    import quad.persistence.repositories as repos

    monkeypatch.setattr(repos, "make_repo", lambda *a, **k: FakeRepo())
    adapter = MagicMock()
    adapter.get_user_trades = AsyncMock(
        return_value=[
            _leg(
                side="SELL", price=Decimal(100), order_id="o1", timestamp=1
            ),  # open short
            _leg(
                side="BUY", price=Decimal(90), order_id="o2", timestamp=2
            ),  # close short, fifo pnl=10
            _leg(
                side="SELL", price=Decimal(95), order_id="o3", timestamp=3
            ),  # new open (BUY was a close)
        ]
    )
    eng = ExecutionEngine(
        adapter,
        MagicMock(),
        db_manager=SimpleNamespace(is_connected=True),
        config=dict(GW_CFG),
    )
    # legs carry no pnl attr -> getattr default must trigger FIFO, not exchange value
    n = asyncio.run(eng._ingest_exchange_trades())
    assert n == 3
    assert Decimal(str(created[1].pnl)) == Decimal(10)
    # If the closing BUY had been wrongly queued as a long, the final SELL
    # would pop it as (95-90)=5. Correct: it opens a short with pnl 0.
    assert Decimal(str(created[2].pnl)) == Decimal(0)


# ---------------------------------------------------------------------------
# (3) gateway: terminal states move to completed on submit + refresh eviction
# ---------------------------------------------------------------------------


def test_submit_filled_goes_straight_to_completed():
    adapter = MagicMock()
    adapter.place_order = AsyncMock(return_value=_filled_result("c-term"))
    gw = OrderGateway(adapter, config=dict(GW_CFG))
    res = asyncio.run(
        gw.submit(
            OrderRequest(
                symbol="BTCUSDT",
                side="BUY",
                order_type="MARKET",
                quantity=Decimal(1),
                client_order_id="c-term",
            )
        )
    )
    assert res.status == "FILLED"
    assert "c-term" not in gw._active_orders
    assert "c-term" in gw._completed_ids
    assert "c-term" not in gw._pending_confirmations


def test_refresh_state_evicts_local_terminal_without_exchange_call():
    adapter = MagicMock()
    adapter.get_open_orders = AsyncMock(return_value=[])
    adapter.get_order_status = AsyncMock()
    gw = OrderGateway(adapter, config=dict(GW_CFG))
    gw._active_orders["done"] = Order(
        id=7, client_order_id="done", symbol="BTCUSDT", side="SELL", status="FILLED"
    )
    asyncio.run(gw.refresh_state())
    assert "done" not in gw._active_orders
    assert "done" in gw._completed_ids
    adapter.get_order_status.assert_not_called()


# ---------------------------------------------------------------------------
# (5) gateway: documented 1s/2s/4s backoff; ack-driven confirmation
# ---------------------------------------------------------------------------


def test_backoff_follows_documented_schedule(monkeypatch):
    delays = []

    async def fake_sleep(d):
        delays.append(d)

    monkeypatch.setattr("asyncio.sleep", fake_sleep)
    adapter = MagicMock()
    adapter.place_order = AsyncMock(
        side_effect=[TimeoutError("t1"), TimeoutError("t2"), _filled_result("c-bo")]
    )
    gw = OrderGateway(adapter, config=dict(GW_CFG))
    asyncio.run(
        gw.submit(
            OrderRequest(
                symbol="BTCUSDT",
                side="BUY",
                order_type="MARKET",
                quantity=Decimal(1),
                client_order_id="c-bo",
            )
        )
    )
    assert delays == [1.0, 2.0]


def test_submit_cleans_pending_confirmation():
    adapter = MagicMock()
    adapter.place_order = AsyncMock(return_value=_filled_result("c-cf"))
    gw = OrderGateway(adapter, config=dict(GW_CFG))
    asyncio.run(
        gw.submit(
            OrderRequest(
                symbol="BTCUSDT",
                side="BUY",
                order_type="MARKET",
                quantity=Decimal(1),
                client_order_id="c-cf",
            )
        )
    )
    assert gw._pending_confirmations == {}


# ---------------------------------------------------------------------------
# (4) reconciler: CANCELLED detection; side+symbol dedup key
# ---------------------------------------------------------------------------


def test_reconciler_detects_missed_cancellation():
    adapter = MagicMock()
    now_ms = int(time.time() * 1000)
    adapter.get_order_status = AsyncMock(
        return_value=Order(
            id=9,
            client_order_id="cx",
            symbol="BTCUSDT",
            side="BUY",
            status="CANCELLED",
            created_at=now_ms,
        )
    )
    rec = FillReconciler(adapter, db_manager=None, config=dict(GW_CFG))
    local = Order(
        id=9,
        client_order_id="cx",
        symbol="BTCUSDT",
        side="BUY",
        status="NEW",
        created_at=now_ms,
    )
    discs = asyncio.run(rec.reconcile_pending_orders([local]))
    assert any(
        d["type"] == "MISSED_CANCELLATION" and d["exchange_status"] == "CANCELLED"
        for d in discs
    )


def test_detect_missed_fills_distinguishes_side():
    rec = FillReconciler(MagicMock(), db_manager=None, config=dict(GW_CFG))

    def _t(side):
        return Trade(
            id=0,
            order_id=1,
            symbol="BTCUSDT",
            side=side,
            quantity=Decimal(1),
            price=Decimal(100),
            fee=Decimal(0),
            pnl=Decimal(0),
            timestamp=5,
        )

    missed = asyncio.run(rec.detect_missed_fills([_t("BUY")], [_t("BUY"), _t("SELL")]))
    assert [t.side for t in missed] == ["SELL"]


# ---------------------------------------------------------------------------
# (6) twap
# ---------------------------------------------------------------------------


def _twap():
    return TwapSlicer(
        {
            "min_slices": 1,
            "max_slices": 10,
            "default_window_seconds": 300,
            "jitter_seconds": 0.0,
            "min_slice_quantity": Decimal("1"),
            "fill_urgency_threshold": 0.0,
        }
    )


def test_twap_execute_accepts_window_and_preserves_flags():
    seen = []

    class FakeGw:
        async def submit(self, req):
            seen.append(req)
            return OrderResult(
                order_id=len(seen),
                client_order_id=req.client_order_id,
                symbol=req.symbol,
                side=req.side,
                order_type=req.order_type,
                quantity=req.quantity,
                filled_qty=Decimal(0),
                price=req.price,
                status="NEW",
            )

    parent = OrderRequest(
        symbol="BTCUSDT",
        side="BUY",
        order_type="LIMIT",
        quantity=Decimal(2),
        price=Decimal(100),
        stop_price=Decimal(90),
        client_order_id="p",
        reduce_only=True,
        post_only=True,
        working_type="MARK_PRICE",
        position_side="BOTH",
        price_protect=True,
    )
    results = asyncio.run(_twap().execute(parent, FakeGw(), window=0))
    assert len(results) == 2  # first slice + urgent remainder
    urgent = seen[-1]
    assert urgent.quantity == Decimal(2)
    assert urgent.reduce_only is True and urgent.post_only is True
    assert urgent.stop_price == Decimal(90)
    assert urgent.working_type == "MARK_PRICE" and urgent.position_side == "BOTH"
    assert urgent.price_protect is True


def test_twap_plan_step_size_and_min_notional():
    slicer = TwapSlicer(
        {"min_slices": 1, "max_slices": 10, "min_slice_quantity": Decimal("0.1")}
    )
    parent = OrderRequest(
        symbol="BTCUSDT",
        side="BUY",
        order_type="LIMIT",
        quantity=Decimal("3.1"),
        price=Decimal(100),
        client_order_id="p",
    )
    slices = slicer.plan(parent, 60, step_size=Decimal("0.3"))
    assert sum((s.quantity for s in slices), Decimal(0)) == Decimal("3.1")
    assert all((s.quantity % Decimal("0.3")) == 0 for s in slices[:-1])
    small = OrderRequest(
        symbol="BTCUSDT",
        side="BUY",
        order_type="LIMIT",
        quantity=Decimal("1.0"),
        price=Decimal(100),
        client_order_id="p",
    )
    few = slicer.plan(small, 60, min_notional=Decimal(60))
    assert len(few) == 1 and few[0].quantity == Decimal("1.0")


def test_twap_default_urgency_threshold_is_documented_80pct():
    assert TwapSlicer({})._urgency_threshold == 0.8


# ---------------------------------------------------------------------------
# (7)+(8)+(9) backtesting
# ---------------------------------------------------------------------------


class _ScriptStrategy:
    def __init__(self, script):
        self._script = list(script)
        self._n = 0

    def get_name(self):
        return "script"

    async def evaluate(self, context):
        self._n += 1
        if self._n <= len(self._script):
            return self._script[self._n - 1]
        return []


def _run(strategy, cfg=None, days=1, steps_per_day=2):
    start = datetime(2024, 1, 1)
    end = start + timedelta(days=days)
    interval = 24 // steps_per_day
    return asyncio.run(
        BacktestEngine(strategy, db_manager=None, config=dict(cfg or BT_CFG)).run(
            "BTCUSDT", start, end, interval_hours=interval
        )
    )


def test_backtest_long_roundtrip_margin_and_metrics():
    strat = _ScriptStrategy(
        [
            [
                Action(
                    type="ENTER",
                    contract="BTCUSDT",
                    side="BUY",
                    quantity=Decimal(1),
                    price=Decimal(100),
                )
            ],
            [
                Action(
                    type="EXIT",
                    contract="BTCUSDT",
                    side="SELL",
                    quantity=Decimal(1),
                    price=Decimal(110),
                )
            ],
        ]
    )
    res = _run(strat)
    assert res.total_trades == 1
    assert res.winning_trades == 1 and res.losing_trades == 0
    assert res.total_pnl == Decimal(10)
    assert len(res.trades) == 2  # ENTER + EXIT rows retained


def test_backtest_short_pnl_inverted_and_exit_fee_on_exit_price():
    strat = _ScriptStrategy(
        [
            [
                Action(
                    type="ENTER",
                    contract="BTCUSDT",
                    side="SELL",
                    quantity=Decimal(1),
                    price=Decimal(100),
                )
            ],
            [
                Action(
                    type="EXIT",
                    contract="BTCUSDT",
                    side="BUY",
                    quantity=Decimal(1),
                    price=Decimal(90),
                )
            ],
        ]
    )
    res = _run(strat)
    assert res.total_pnl == Decimal(10)
    exit_trade = [t for t in res.trades if t.pnl != 0][0]
    assert exit_trade.fee == Decimal(90) * Decimal(1) * Decimal("0.001")


def test_backtest_open_only_counts_no_trades():
    strat = _ScriptStrategy(
        [
            [
                Action(
                    type="ENTER",
                    contract="BTCUSDT",
                    side="BUY",
                    quantity=Decimal(1),
                    price=Decimal(100),
                )
            ],
        ]
    )
    res = _run(strat)
    assert res.total_trades == 0
    assert res.winning_trades == 0 and res.losing_trades == 0


def test_backtest_max_trades_per_day_caps_enters():
    cfg = dict(BT_CFG)
    cfg["max_trades_per_day"] = 1
    per_step = [
        [
            Action(
                type="ENTER",
                contract=f"C{i}",
                side="BUY",
                quantity=Decimal(1),
                price=Decimal(100),
            )
        ]
        for i in range(4)
    ]
    res = _run(_ScriptStrategy(per_step), cfg=cfg, days=1, steps_per_day=4)
    enters = [t for t in res.trades if t.pnl == 0]
    assert len(enters) == 1
