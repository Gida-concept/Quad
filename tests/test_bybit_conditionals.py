"""Bybit conditional-order mapping: engine brackets -> Bybit V5 params."""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from quad.exchange.bybit import BybitFuturesAdapter
from quad.types.domain import OrderRequest


def _adapter_with_filters():
    adapter = BybitFuturesAdapter(testnet=True)
    adapter._client = object()
    adapter._connected = True
    adapter.get_exchange_info = AsyncMock(return_value={})
    adapter._exchange_info_cache["BTCUSDT"] = (
        __import__("time").monotonic(),
        (Decimal("0.001"), Decimal("0.001"), Decimal("0")),
    )
    return adapter


@pytest.mark.asyncio
async def test_stop_market_maps_to_bybit_conditional():
    adapter = _adapter_with_filters()
    captured = {}

    async def fake_post(endpoint, params=None):
        captured.update(params or {})
        return {"orderId": "abc", "orderStatus": "New"}

    adapter._post = fake_post
    req = OrderRequest(
        symbol="BTCUSDT",
        side="SELL",
        order_type="STOP_MARKET",
        quantity=Decimal("0.01"),
        stop_price=Decimal("60000"),
    )
    req.working_type = "MARK_PRICE"
    req.reduce_only = True
    await adapter.place_order(req)

    assert captured["orderType"] == "Market"
    assert captured["triggerPrice"] == "60000"
    assert captured["triggerBy"] == "MarkPrice"
    # tpslMode belongs to the set-trading-stop path, never standalone create.
    assert "tpslMode" not in captured
    assert captured["reduceOnly"] is True
    assert captured["side"] == "Sell"


@pytest.mark.asyncio
async def test_market_entry_has_no_trigger_fields():
    adapter = _adapter_with_filters()
    captured = {}

    async def fake_post(endpoint, params=None):
        captured.update(params or {})
        return {"orderId": "abc", "orderStatus": "New"}

    adapter._post = fake_post
    req = OrderRequest(
        symbol="BTCUSDT", side="BUY", order_type="MARKET", quantity=Decimal("0.01")
    )
    await adapter.place_order(req)

    assert captured["orderType"] == "Market"
    assert "triggerPrice" not in captured
    assert "triggerBy" not in captured
    assert captured["side"] == "Buy"
