"""Regression tests for the exchange/market-data/TradingView fix batch.

Covers: V5 tick-size + lot-filter parsing, order-status realtime-first/None,
place_order payload rules (priceProtect, positionIdx, tpslMode, timeInForce),
retry/backoff classification, recv_window + clock offset, position side
mapping, historical bar/timestamps, engine cache rules, and TradingView
signal validation.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from quad.exchange.base import ExchangeRateLimitError  # noqa: E402
from quad.exchange.bybit import BybitFuturesAdapter  # noqa: E402
from quad.tradingview.signals import (  # noqa: E402
    convert_to_action,
    normalize_symbol,
    verify_signature,
)
from quad.types.domain import OrderRequest  # noqa: E402


def _adapter(**kw) -> BybitFuturesAdapter:
    kw.setdefault("testnet", True)
    return BybitFuturesAdapter(**kw)


def _filtered(symbol="BTCUSDT", step="0.001", qty="0.001", notion="0"):
    a = _adapter()
    a._client = object()
    a.get_exchange_info = AsyncMock(return_value={})
    a._exchange_info_cache[symbol] = (
        time.monotonic(),
        (Decimal(step), Decimal(qty), Decimal(notion)),
    )
    return a


# --- (1) base get_tick_size supports both layouts ---


class TestTickSizeLayouts:
    @pytest.mark.asyncio
    async def test_spot_style_filters(self):
        a = _adapter()
        a.get_exchange_info = AsyncMock(
            return_value={
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.1"}],
                    }
                ]
            }
        )
        assert await a.get_tick_size("BTCUSDT") == Decimal("0.1")

    @pytest.mark.asyncio
    async def test_bybit_v5_bare_list(self):
        a = _adapter()
        a.get_exchange_info = AsyncMock(
            return_value={
                "list": [
                    {
                        "symbol": "BTCUSDT",
                        "priceFilter": {"tickSize": "0.1"},
                    }
                ]
            }
        )
        assert await a.get_tick_size("BTCUSDT") == Decimal("0.1")

    @pytest.mark.asyncio
    async def test_bybit_v5_envelope(self):
        a = _adapter()
        a.get_exchange_info = AsyncMock(
            return_value={
                "result": {
                    "list": [
                        {
                            "symbol": "BTCUSDT",
                            "priceFilter": {"tickSize": "0.5"},
                        }
                    ]
                }
            }
        )
        assert await a.get_tick_size("BTCUSDT") == Decimal("0.5")


# --- (2) min_notional reads lotSizeFilter fields ---


class TestMinNotional:
    @pytest.mark.asyncio
    async def test_reads_min_notional_value(self):
        a = _adapter()
        a.get_exchange_info = AsyncMock(
            return_value={
                "list": [
                    {
                        "symbol": "BTCUSDT",
                        "lotSizeFilter": {
                            "qtyStep": "0.001",
                            "minOrderQty": "0.001",
                            "minNotionalValue": "5",
                        },
                    }
                ]
            }
        )
        step, qty, notion = await a._get_lot_filters("BTCUSDT")
        assert step == Decimal("0.001")
        assert qty == Decimal("0.001")
        assert notion == Decimal("5")

    @pytest.mark.asyncio
    async def test_reads_min_order_value(self):
        a = _adapter()
        a.get_exchange_info = AsyncMock(
            return_value={
                "list": [
                    {
                        "symbol": "BTCUSDT",
                        "lotSizeFilter": {
                            "qtyStep": "0.01",
                            "minOrderQty": "0.01",
                            "minOrderValue": "7",
                        },
                    }
                ]
            }
        )
        _, _, notion = await a._get_lot_filters("BTCUSDT")
        assert notion == Decimal("7")


# --- (3) position side mapping ---


class TestParsePosition:
    def test_side_field_wins_over_sign(self):
        a = _adapter()
        pos = a._parse_position(
            {
                "symbol": "BTCUSDT",
                "size": "1.5",
                "side": "Sell",
                "positionIdx": 0,
                "avgPrice": "60000",
                "markPrice": "61000",
            }
        )
        assert pos is not None
        assert pos.side.value == "SHORT"
        assert pos.quantity == Decimal("1.5")

    def test_zero_size_skipped(self):
        a = _adapter()
        assert (
            a._parse_position({"symbol": "BTCUSDT", "size": "0", "side": "None"})
            is None
        )

    def test_hedge_legs_map(self):
        a = _adapter()
        base = {
            "symbol": "BTCUSDT",
            "size": "2",
            "side": "Buy",
            "avgPrice": "1",
            "markPrice": "1",
        }
        from quad.types.domain import FuturesPositionSide

        assert (
            a._parse_position({**base, "positionIdx": 1}).position_side
            == FuturesPositionSide.LONG
        )
        assert (
            a._parse_position({**base, "positionIdx": 2}).position_side
            == FuturesPositionSide.SHORT
        )
        assert (
            a._parse_position({**base, "positionIdx": 0}).position_side
            == FuturesPositionSide.BOTH
        )
        # string positionIdx from JSON is tolerated
        assert (
            a._parse_position({**base, "positionIdx": "1"}).position_side
            == FuturesPositionSide.LONG
        )


# --- (4) get_order_status realtime-first, None when absent ---


class TestOrderStatus:
    @pytest.mark.asyncio
    async def test_realtime_hit(self):
        a = _filtered()
        calls = []

        async def fake_get(endpoint, params=None):
            calls.append((endpoint, dict(params or {})))
            return {
                "list": [
                    {
                        "orderId": "x1",
                        "orderStatus": "New",
                        "symbol": "BTCUSDT",
                        "side": "Buy",
                        "orderType": "Market",
                        "qty": "0.01",
                    }
                ]
            }

        a._get = fake_get
        order = await a.get_order_status("x1", symbol="BTCUSDT")
        assert order is not None and order.id == "x1"
        assert order.status == "NEW"
        assert calls[0][0] == "/v5/order/realtime"
        assert calls[0][1]["category"] == "linear"
        assert calls[0][1]["symbol"] == "BTCUSDT"
        assert calls[0][1]["orderId"] == "x1"

    @pytest.mark.asyncio
    async def test_falls_back_to_history_then_none(self):
        a = _filtered()
        endpoints = []

        async def fake_get(endpoint, params=None):
            endpoints.append((endpoint, dict(params or {})))
            if endpoint == "/v5/order/realtime":
                return {"list": []}
            return {
                "list": [
                    {
                        "orderId": "h9",
                        "orderStatus": "Filled",
                        "symbol": "BTCUSDT",
                        "side": "Sell",
                        "orderType": "Market",
                        "qty": "0.02",
                    }
                ]
            }

        a._get = fake_get
        order = await a.get_order_status("h9", symbol="BTCUSDT")
        assert order is not None and order.status == "FILLED"
        assert endpoints[0][0] == "/v5/order/realtime"
        assert endpoints[0][1]["category"] == "linear"
        assert endpoints[0][1]["symbol"] == "BTCUSDT"

        async def fake_empty(endpoint, params=None):
            return {"list": []}

        a._get = fake_empty
        assert await a.get_order_status("nope", symbol="BTCUSDT") is None


# --- (5)-(8) place_order payload rules ---


async def _place(req: OrderRequest):
    a = _filtered()
    captured = {}

    async def fake_post(endpoint, params=None):
        captured.update(params or {})
        return {"orderId": "abc", "orderStatus": "New"}

    a._post = fake_post
    await a.place_order(req)
    return captured


class TestPlaceOrderPayload:
    @pytest.mark.asyncio
    async def test_price_protect_forwarded(self):
        req = OrderRequest(
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            quantity=Decimal("0.01"),
            price=Decimal("60000"),
        )
        req.price_protect = True
        captured = await _place(req)
        assert captured["priceProtect"] is True

    @pytest.mark.asyncio
    async def test_price_protect_absent_when_unset(self):
        req = OrderRequest(
            symbol="BTCUSDT", side="BUY", order_type="MARKET", quantity=Decimal("0.01")
        )
        captured = await _place(req)
        assert "priceProtect" not in captured

    @pytest.mark.asyncio
    async def test_position_idx_defaults_zero(self):
        req = OrderRequest(
            symbol="BTCUSDT", side="BUY", order_type="MARKET", quantity=Decimal("0.01")
        )
        captured = await _place(req)
        assert captured["positionIdx"] == 0

    @pytest.mark.asyncio
    async def test_position_idx_hedge_legs(self):
        req = OrderRequest(
            symbol="BTCUSDT", side="BUY", order_type="MARKET", quantity=Decimal("0.01")
        )
        req.position_side = "LONG"
        assert (await _place(req))["positionIdx"] == 1
        req.position_side = "SHORT"
        assert (await _place(req))["positionIdx"] == 2

    @pytest.mark.asyncio
    async def test_no_tpsl_mode_on_create(self):
        req = OrderRequest(
            symbol="BTCUSDT",
            side="SELL",
            order_type="STOP_MARKET",
            quantity=Decimal("0.01"),
            stop_price=Decimal("60000"),
        )
        captured = await _place(req)
        assert "tpslMode" not in captured
        assert captured["triggerPrice"] == "60000"

    @pytest.mark.asyncio
    async def test_time_in_force_stripped_for_market(self):
        req = OrderRequest(
            symbol="BTCUSDT", side="BUY", order_type="MARKET", quantity=Decimal("0.01")
        )
        req.time_in_force = "GTC"
        captured = await _place(req)
        assert "timeInForce" not in captured

    @pytest.mark.asyncio
    async def test_time_in_force_kept_for_limit(self):
        req = OrderRequest(
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            quantity=Decimal("0.01"),
            price=Decimal("60000"),
        )
        req.time_in_force = "IOC"
        captured = await _place(req)
        assert captured["timeInForce"] == "IOC"


# --- (9) retry classification ---


class TestRetry:
    def test_rate_limit_retryable(self):
        a = _adapter()
        assert a._is_retryable(ExchangeRateLimitError("429 too many")) is True
        assert a._is_retryable(ValueError("order rejected")) is False

    def test_retry_delay_uses_retry_after(self):
        a = _adapter()
        d = a._retry_delay(0, ValueError("429 Retry-After: 2 slow down"))
        assert 2.0 <= d <= 2.6

    @pytest.mark.asyncio
    async def test_request_retries_then_succeeds(self):
        a = _adapter()
        a._client = type("C", (), {"endpoint": "https://x"})()
        calls = {"n": 0}

        def flaky(**kw):
            calls["n"] += 1
            if calls["n"] < 3:
                raise Exception("10006 Too many visits")
            return {"retCode": 0, "retMsg": "OK", "result": {"ok": True}}

        a._client._submit_request = flaky
        a._retry_delay = staticmethod(lambda attempt, exc: 0)
        out = await a._get("/v5/market/time")
        assert out == {"ok": True}
        assert calls["n"] == 3


# --- (10) recv_window + clock offset ---


class TestClock:
    def test_recv_window_clamped(self):
        a = _adapter(recv_window=999999)
        assert a._recv_window == 60000

    @pytest.mark.asyncio
    async def test_server_time_measures_offset(self):
        a = _adapter()
        server_ms = int(time.time() * 1000) + 1500
        a._get = AsyncMock(return_value={"timeSecond": server_ms / 1000})
        out = await a.get_server_time()
        assert out == server_ms
        assert 1000 <= a.clock_offset_ms <= 2000
        assert a._now_ms() >= int(time.time() * 1000)


# --- (14) historical bar + timestamps ---


class TestHistorical:
    @pytest.mark.asyncio
    async def test_bar_forwarded_and_seconds_handled(self):
        from quad.market_data.historical import HistoricalDataProvider

        seen = {}
        now_s = datetime.now().timestamp()

        class FakeEx:
            async def get_klines(self, symbol, interval, limit=500):
                seen["interval"] = interval
                return [(now_s, "1", "2", "0.5", "1.5", "10")]

        prov = HistoricalDataProvider(
            db_manager=AsyncMock(dsn="x"), exchange_adapter=FakeEx()
        )
        start = datetime.now() - timedelta(hours=2)
        end = datetime.now() + timedelta(hours=2)
        candles = await prov.get_candles("BTCUSDT", start, end, bar="15m")
        assert seen["interval"] == "15m"
        assert len(candles) == 1
        assert candles[0].timestamp == int(now_s)

    @pytest.mark.asyncio
    async def test_ms_stamps_tolerated(self):
        from quad.market_data.historical import HistoricalDataProvider

        now_ms = int(datetime.now().timestamp() * 1000)

        class FakeEx:
            async def get_klines(self, symbol, interval, limit=500):
                return [(now_ms, "1", "2", "0.5", "1.5", "10")]

        prov = HistoricalDataProvider(
            db_manager=AsyncMock(dsn="x"), exchange_adapter=FakeEx()
        )
        start = datetime.now() - timedelta(hours=2)
        end = datetime.now() + timedelta(hours=2)
        candles = await prov.get_candles("BTCUSDT", start, end)
        assert len(candles) == 1
        assert candles[0].timestamp == now_ms // 1000


# --- (16) engine cache rules ---


def _engine():
    from quad.market_data.engine import MarketDataEngine

    a = _adapter()
    cfg = {
        "market_data": {
            "symbols": ["BTCUSDT"],
            "buffer_sizes": {"ticks": 10},
            "cache_ttl": 60,
            "engine": {"shutdown_timeout_seconds": 1},
            "websocket": {
                "url": "wss://x",
                "backoff": {
                    "base_seconds": 0.01,
                    "max_seconds": 0.05,
                    "multiplier": 2.0,
                    "jitter_fraction": 0.1,
                },
            },
        }
    }
    eng = MarketDataEngine(a, cfg, db_manager=None)
    eng._symbols = ["BTCUSDT"]
    return eng


class TestEngineCaches:
    @pytest.mark.asyncio
    async def test_unconfirmed_kline_skipped(self):
        eng = _engine()
        from quad.market_data.buffers import PriceBuffer

        eng._buffer = PriceBuffer(
            max_ticks_per_symbol=10,
            config={"market_data": {"buffer_sizes": {"ticks": 10}}},
        )
        await eng._handle_kline_update(
            {
                "arg": {"symbol": "BTCUSDT"},
                "data": [{"symbol": "BTCUSDT", "close": "99", "confirm": False}],
            }
        )
        assert await eng.get_latest_price("BTCUSDT") is None
        await eng._handle_kline_update(
            {
                "arg": {"symbol": "BTCUSDT"},
                "data": [{"symbol": "BTCUSDT", "close": "99", "confirm": True}],
            }
        )
        assert await eng.get_latest_price("BTCUSDT") == Decimal("99")

    @pytest.mark.asyncio
    async def test_stale_book_ignored(self):
        eng = _engine()

        def msg(seq, u):
            return {
                "ts": 1,
                "data": [
                    {
                        "s": "BTCUSDT",
                        "b": [["1", "2"]],
                        "a": [["3", "4"]],
                        "seq": seq,
                        "u": u,
                    }
                ],
            }

        await eng._handle_book_ticker(msg(10, 100))
        await eng._handle_book_ticker(msg(9, 99))
        book = await eng.get_order_book("BTCUSDT")
        assert book["seq"] == 10
        await eng._handle_book_ticker(msg(11, 101))
        assert (await eng.get_order_book("BTCUSDT"))["seq"] == 11

    @pytest.mark.asyncio
    async def test_max_age_and_is_stale(self):
        eng = _engine()
        await eng._handle_ticker(
            {"ts": 1, "data": [{"symbol": "BTCUSDT", "lastPrice": "100"}]}
        )
        assert await eng.get_ticker("BTCUSDT") is not None
        assert await eng.get_ticker("BTCUSDT", max_age=60) is not None
        assert eng.is_stale("ticker", "BTCUSDT", 60) is False
        assert eng.is_stale("ticker", "ETHUSDT", 60) is True
        eng._cache_ts["ticker"]["BTCUSDT"] -= 120
        assert await eng.get_ticker("BTCUSDT", max_age=60) is None
        assert eng.is_stale("ticker", "BTCUSDT", 60) is True


# --- (17) TradingView signals ---


class TestSignals:
    def test_unknown_and_missing_action_rejected(self):
        assert convert_to_action({"ticker": "BTCUSDT", "action": "moon"}) is None
        assert convert_to_action({"ticker": "BTCUSDT"}) is None

    def test_symbol_normalisation(self):
        assert normalize_symbol("BINANCE:BTC-USDT") == "BTCUSDT"
        assert normalize_symbol("btc/usdt") == "BTCUSDT"
        sig = convert_to_action(
            {"ticker": "bybit:eth-usdt", "action": "buy", "quantity": "2"}
        )
        assert sig is not None and sig.symbol == "ETHUSDT" and sig.side == "BUY"

    def test_close_maps_reduce_only(self):
        sig = convert_to_action({"ticker": "BTCUSDT", "action": "close"})
        assert sig is not None
        assert sig.side == "CLOSE" and sig.signal_type == "exit"
        assert sig.reduce_only is True
        entry = convert_to_action({"ticker": "BTCUSDT", "action": "buy"})
        assert entry is not None and entry.reduce_only is False

    def test_qty_must_be_positive(self):
        assert (
            convert_to_action({"ticker": "BTCUSDT", "action": "buy", "quantity": "0"})
            is None
        )
        assert (
            convert_to_action({"ticker": "BTCUSDT", "action": "buy", "quantity": "-3"})
            is None
        )
        assert (
            convert_to_action(
                {"ticker": "BTCUSDT", "action": "buy", "quantity": "nope"}
            )
            is None
        )

    def test_hmac_verify(self):
        assert verify_signature("hello", "bad", secret="s3cr3t") is False
        import hashlib
        import hmac as _hmac

        good = _hmac.new(b"s3cr3t", b"hello", hashlib.sha256).hexdigest()
        assert verify_signature(b"hello", good, secret="s3cr3t") is True
        assert verify_signature(b"hello", good, secret="") is False

    def test_wrong_in_alert_secret_rejected(self):
        ok = convert_to_action(
            {"ticker": "BTCUSDT", "action": "buy", "secret": "s3cr3t"},
            expected_secret="s3cr3t",
        )
        assert ok is not None
        bad = convert_to_action(
            {"ticker": "BTCUSDT", "action": "buy", "secret": "nope"},
            expected_secret="s3cr3t",
        )
        assert bad is None
