r"""Tests for price tick-size normalization (exchange adapter).

Regression: TP/SL bracket orders were rejected by Binance with -1111
("Precision is over the maximum defined for this asset") because the
``triggerPrice`` computed from the mark price carried excess decimals.  The
adapter now quantizes stop/trigger prices to the symbol's PRICE_FILTER
``tickSize`` (the authoritative last line of defense, mirroring quantity).
"""

import asyncio
import sys
from decimal import Decimal
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from quad.exchange.mock import MockAdapter  # noqa: E402

# BTCUSDT with a 0.1 price tick (as on Binance) and quantity filters.
EXCHANGE_INFO = {
    "timezone": "UTC",
    "serverTime": 0,
    "rateLimits": [],
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "filters": [
                {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                {"filterType": "MIN_NOTIONAL", "notional": "100"},
                {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
            ],
        }
    ],
}


def _adapter() -> MockAdapter:
    return MockAdapter(exchange_info=EXCHANGE_INFO)


def test_normalize_price_quantizes_excess_precision_to_tick():
    a = _adapter()
    out = asyncio.run(a.normalize_price("BTCUSDT", "60867.12345"))
    assert out == Decimal("60867.1")


def test_normalize_price_rounds_half_up_on_the_tick_boundary():
    a = _adapter()
    # 60867.25 / 0.1 = 608672.5 -> ROUND_HALF_UP -> 608673 -> *0.1 = 60867.3
    out = asyncio.run(a.normalize_price("BTCUSDT", "60867.25"))
    assert out == Decimal("60867.3")


def test_normalize_price_returns_none_and_keeps_tick_aligned():
    a = _adapter()
    assert asyncio.run(a.normalize_price("BTCUSDT", None)) is None
    assert asyncio.run(a.normalize_price("BTCUSDT", "60867.1")) == Decimal("60867.1")


def test_get_symbol_filters_includes_tick_size():
    a = _adapter()
    f = asyncio.run(a.get_symbol_filters("BTCUSDT"))
    assert f["tick_size"] == Decimal("0.1")
    assert f["min_notional"] == Decimal("100")
    assert f["min_qty"] == Decimal("0.001")
