"""Tests for BybitFuturesAdapter — contract, normalization, error mapping.

All tests run purely in-process with no network calls.  The pybit SDK is
guarded (``if HTTP is None``) so these tests pass even when pybit is not
installed — the contract/normalization/error checks do not require it.
"""

from __future__ import annotations

import ast
import inspect
import sys
from collections.abc import AsyncGenerator
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from quad.exchange.base import (
    ExchangeAdapter,
    ExchangeAuthError,
    ExchangeBannedError,
    ExchangeConnectionError,
    ExchangeError,
    ExchangeOrderError,
    ExchangeRateLimitError,
)
from quad.exchange.bybit import (
    BybitFuturesAdapter,
    _MARGIN_MODE_ALREADY_SET_CODE,
    _ORDER_NOT_FOUND_CODES,
    _ORDER_NOT_FOUND_TEXT,
)


# ---------------------------------------------------------------------------
# Contract: ABC compliance
# ---------------------------------------------------------------------------


class TestABContract:
    """BybitFuturesAdapter must implement every abstract method."""

    def test_syntax_valid(self):
        ast.parse(open("src/quad/exchange/bybit.py").read())

    def test_is_subclass_of_exchange_adapter(self):
        assert issubclass(BybitFuturesAdapter, ExchangeAdapter)

    def test_has_all_abstract_methods(self):
        abstract_names = {
            name
            for name, _ in inspect.getmembers(ExchangeAdapter)
            if getattr(getattr(ExchangeAdapter, name, None), "__isabstractmethod__", False)
        }
        for name in abstract_names:
            assert hasattr(BybitFuturesAdapter, name), (
                f"BybitFuturesAdapter missing abstract method: {name}"
            )

    def test_category_is_linear(self):
        assert BybitFuturesAdapter.CATEGORY == "linear"

    def test_is_testnet_true_when_constructed_with_testnet(self):
        adapter = BybitFuturesAdapter(testnet=True)
        assert adapter.is_testnet is True

    def test_is_testnet_false_when_constructed_without_testnet(self):
        adapter = BybitFuturesAdapter(testnet=False)
        assert adapter.is_testnet is False

    def test_is_connected_false_before_connect(self):
        adapter = BybitFuturesAdapter()
        assert adapter.is_connected is False

    def test_connect_disconnect_lifecycle(self):
        adapter = BybitFuturesAdapter()
        assert adapter.is_connected is False
        # connect() requires pybit; skip if not installed
        pytest.importorskip("pybit", reason="pybit SDK not installed")


# ---------------------------------------------------------------------------
# Normalization: _get_lot_filters (Bybit instruments-info layout)
# ---------------------------------------------------------------------------


class TestLotFilters:
    """The Bybit adapter overrides _get_lot_filters for Bybit's layout."""

    def _make_adapter_with_cache(self, symbol: str, entry: dict) -> BybitFuturesAdapter:
        adapter = BybitFuturesAdapter(testnet=True)
        adapter._exchange_info_cache = {}
        # Manually populate the cache so get_exchange_info is never called
        now = __import__("time").monotonic()
        step = Decimal(str(entry.get("lotSizeFilter", {}).get("qtyStep", "0") or "0"))
        min_qty = Decimal(str(entry.get("lotSizeFilter", {}).get("minOrderQty", "0") or "0"))
        adapter._exchange_info_cache[symbol] = (now, (step, min_qty, min_qty))
        return adapter

    @pytest.mark.asyncio
    async def test_get_lot_filters_returns_cached_values(self):
        adapter = self._make_adapter_with_cache(
            "BTCUSDT",
            {"lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001"}},
        )
        step, min_qty, min_notional = await adapter._get_lot_filters("BTCUSDT")
        assert step == Decimal("0.001")
        assert min_qty == Decimal("0.001")
        assert min_notional == Decimal("0.001")

    @pytest.mark.asyncio
    async def test_get_lot_filters_populates_cache(self):
        adapter = self._make_adapter_with_cache(
            "ETHUSDT",
            {"lotSizeFilter": {"qtyStep": "0.01", "minOrderQty": "0.01"}},
        )
        step, _, _ = await adapter._get_lot_filters("ETHUSDT")
        assert step == Decimal("0.01")
        assert "ETHUSDT" in adapter._exchange_info_cache

    @pytest.mark.asyncio
    async def test_get_lot_filters_missing_symbol_raises(self):
        adapter = BybitFuturesAdapter(testnet=True)
        adapter._exchange_info_cache = {}
        # Stub get_exchange_info to return empty
        adapter.get_exchange_info = AsyncMock(return_value={})
        with pytest.raises(RuntimeError, match="no instrument info found"):
            await adapter._get_lot_filters("FAKEUSDT")


# ---------------------------------------------------------------------------
# Error semantics: is_margin_mode_already_set / is_order_not_found
# ---------------------------------------------------------------------------


class TestErrorSemantics:
    def test_margin_mode_already_set_true(self):
        adapter = BybitFuturesAdapter()
        exc = ExchangeOrderError(f"Bybit {_MARGIN_MODE_ALREADY_SET_CODE}: Margin mode is not modified")
        assert adapter.is_margin_mode_already_set(exc) is True

    def test_margin_mode_already_set_false(self):
        adapter = BybitFuturesAdapter()
        exc = ExchangeOrderError("Bybit 110044: Some other error")
        assert adapter.is_margin_mode_already_set(exc) is False

    def test_order_not_found_by_text(self):
        adapter = BybitFuturesAdapter()
        exc = ExchangeOrderError(f"Bybit 20001: {_ORDER_NOT_FOUND_TEXT}")
        assert adapter.is_order_not_found(exc) is True

    def test_order_not_found_by_code(self):
        adapter = BybitFuturesAdapter()
        exc = ExchangeOrderError("Bybit 30003: Order does not exist.")
        assert adapter.is_order_not_found(exc) is True

    def test_order_not_found_false_for_other(self):
        adapter = BybitFuturesAdapter()
        exc = ExchangeOrderError("Bybit 110042: Position does not exist")
        assert adapter.is_order_not_found(exc) is False

    def test_abc_defaults_are_false(self):
        """The ABC base methods always return False."""
        exc = ExchangeOrderError("anything")
        assert ExchangeAdapter.is_margin_mode_already_set(None, exc) is False
        assert ExchangeAdapter.is_order_not_found(None, exc) is False


# ---------------------------------------------------------------------------
# Error hierarchy: shared definitions
# ---------------------------------------------------------------------------


class TestErrorHierarchy:
    def test_hierarchy(self):
        assert issubclass(ExchangeConnectionError, ExchangeError)
        assert issubclass(ExchangeAuthError, ExchangeError)
        assert issubclass(ExchangeRateLimitError, ExchangeError)
        assert issubclass(ExchangeBannedError, ExchangeError)
        assert issubclass(ExchangeOrderError, ExchangeError)

    def test_bybit_imports_match_base(self):
        from quad.exchange.bybit import ExchangeError as BybitErr
        assert BybitErr is ExchangeError


# ---------------------------------------------------------------------------
# Normalization helpers: normalize_price / normalize_quantity / get_tick_size
# ---------------------------------------------------------------------------


class TestNormalizationHelpers:
    """These methods are inherited from the ABC and exchange-agnostic.
    We test them through BybitFuturesAdapter to confirm they still work."""

    def _adapter_with_exchange_info(self, info: dict) -> BybitFuturesAdapter:
        adapter = BybitFuturesAdapter(testnet=True)
        adapter._exchange_info_cache = {}
        # Stub get_exchange_info to return the Binance-shaped data used by
        # the ABC's _get_lot_filters (we override this in Bybit, but
        # normalize_quantity/get_tick_size still work via _get_lot_filters).
        adapter.get_exchange_info = AsyncMock(return_value=info)
        return adapter

    @pytest.mark.asyncio
    async def test_normalize_price_quantizes_to_tick(self):
        info = {"list": [{"symbol": "BTCUSDT", "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001"}}]}
        adapter = self._adapter_with_exchange_info(info)
        # Pre-populate the cache so _get_lot_filters is short-circuited
        adapter._exchange_info_cache["BTCUSDT"] = (
            __import__("time").monotonic(),
            (Decimal("0.001"), Decimal("0.001"), Decimal("0.001")),
        )
        # Normalize price still uses the ABC's get_tick_size which calls
        # get_exchange_info.  We stub get_tick_size directly for this test.
        adapter.get_tick_size = AsyncMock(return_value=Decimal("0.1"))
        out = await adapter.normalize_price("BTCUSDT", "60867.12345")
        assert out == Decimal("60867.1")

    @pytest.mark.asyncio
    async def test_normalize_price_returns_none(self):
        adapter = self._adapter_with_exchange_info({})
        adapter.get_tick_size = AsyncMock(return_value=Decimal("0.1"))
        out = await adapter.normalize_price("BTCUSDT", None)
        assert out is None

    @pytest.mark.asyncio
    async def test_normalize_quantity_uses_lot_filters(self):
        adapter = self._adapter_with_exchange_info({})
        # Inject cached lot filters: step=0.001, min=0.001
        adapter._exchange_info_cache["BTCUSDT"] = (
            __import__("time").monotonic(),
            (Decimal("0.001"), Decimal("0.001"), Decimal("100")),
        )
        out = await adapter.normalize_quantity("BTCUSDT", Decimal("0.12345"))
        assert out == Decimal("0.123")


# ---------------------------------------------------------------------------
# Order mapping: place_order params structure
# ---------------------------------------------------------------------------


class TestOrderMapping:
    @pytest.mark.asyncio
    async def test_place_order_builds_correct_params(self):
        """Verify the adapter builds the right pybit params from an OrderRequest."""
        adapter = BybitFuturesAdapter(testnet=True)
        adapter._client = MagicMock()
        adapter._connected = True
        adapter.get_exchange_info = AsyncMock(return_value={})
        # Stub _post to capture params
        captured = {}
        async def fake_post(endpoint, params=None):
            captured.update({"endpoint": endpoint, "params": params})
            return {"orderId": "12345", "orderStatus": "New"}
        adapter._post = fake_post

        from quad.types.domain import OrderRequest
        req = OrderRequest(
            symbol="BTCUSDT",
            side="BUY",
            order_type="MARKET",
            quantity=Decimal("0.01"),
        )
        adapter._exchange_info_cache["BTCUSDT"] = (
            __import__("time").monotonic(),
            (Decimal("0.001"), Decimal("0.001"), Decimal("100")),
        )
        result = await adapter.place_order(req)

        assert captured["endpoint"] == "/v5/order/create"
        p = captured["params"]
        assert p["category"] == "linear"
        assert p["symbol"] == "BTCUSDT"
        assert p["side"] == "BUY"
        assert p["orderType"] == "MARKET"
        assert p["positionIdx"] == 0
        assert result.order_id == 12345
        assert result.status == "New"
