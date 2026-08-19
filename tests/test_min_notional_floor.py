r"""Tests for the minNotional-aware quantity floor (execution engine).

Regression: the previous floor logic only raised a sub-minimum quantity up to
``minQty``, which for ETHUSDT turned a 0.01 (notional 19.1 < 20) into an even
smaller 0.001 order (notional 1.91) that Binance still rejected with -4164.
The new helper raises the quantity so it clears minNotional too, or (when the
pre-cap cannot reach it) cleanly rejects.
"""

import asyncio
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from quad.execution.engine import ExecutionEngine, _floor_to_compliant  # noqa: E402
from quad.types.risk import Action  # noqa: E402


# ---------------------------------------------------------------------------
# Pure helper
# ---------------------------------------------------------------------------


def test_floor_clears_min_notional():
    # needed = 20 / 1912.93 = 0.010455 -> ceil to step 0.001 = 0.011
    result = _floor_to_compliant(
        Decimal("0.01"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("20"),
        step_size=Decimal("0.001"),
        mark_price=Decimal("1912.93"),
    )
    assert result == Decimal("0.011")
    assert result * Decimal("1912.93") >= Decimal("20")


def test_floor_respects_existing_min_qty():
    result = _floor_to_compliant(
        Decimal("0.0001"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("0"),
        step_size=Decimal("0.001"),
        mark_price=None,
    )
    assert result == Decimal("0.001")


def test_floor_noop_when_already_compliant():
    result = _floor_to_compliant(
        Decimal("0.02"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("20"),
        step_size=Decimal("0.001"),
        mark_price=Decimal("1912.93"),
    )
    # 0.02 * 1912.93 = 38.26 >= 20 and > needed; unchanged.
    assert result == Decimal("0.02")


def test_floor_handles_no_mark_price():
    # No mark price -> cannot compute notional; just minQty is applied.
    result = _floor_to_compliant(
        Decimal("0.005"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("20"),
        step_size=Decimal("0.001"),
        mark_price=None,
    )
    assert result == Decimal("0.005")


# ---------------------------------------------------------------------------
# Engine path (_prepare_quantity)
# ---------------------------------------------------------------------------


def _make_engine(adapter):
    config = {
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
    return ExecutionEngine(
        exchange_adapter=adapter,
        risk_manager=MagicMock(),
        db_manager=None,
        config=config,
    )


def _min_notional_adapter(normalize_error, filters, mark_price):
    adapter = MagicMock()
    adapter.normalize_quantity = AsyncMock(side_effect=normalize_error)
    adapter.get_symbol_filters = AsyncMock(return_value=filters)
    adapter.get_mark_price = AsyncMock(return_value=mark_price)
    return adapter


def test_prepare_quantity_raises_up_to_min_notional_within_pre_cap():
    adapter = _min_notional_adapter(
        normalize_error=RuntimeError(
            "notional 19.1 (qty 0.01 x mark 1912.93) below minNotional 20 "
            "for ETHUSDT (exchange would reject; Binance -4164)"
        ),
        filters={
            "step_size": Decimal("0.001"),
            "min_qty": Decimal("0.001"),
            "min_notional": Decimal("20"),
        },
        mark_price=Decimal("1912.93"),
    )
    engine = _make_engine(adapter)
    action = Action(type="ENTER", contract="ETHUSDT", quantity=Decimal("0.01"))
    result = asyncio.run(engine._prepare_quantity(action, pre_cap=Decimal("0.02")))
    # sized up from 0.01 to a minNotional-compliant 0.011, within pre-cap 0.02
    assert result == Decimal("0.011")


def test_prepare_quantity_cleanly_rejects_when_pre_cap_too_small():
    # Same ETH case but pre-cap 0.01 is too small to reach minNotional 20.
    adapter = _min_notional_adapter(
        normalize_error=RuntimeError(
            "notional 19.1 (qty 0.01 x mark 1912.93) below minNotional 20 "
            "for ETHUSDT (exchange would reject; Binance -4164)"
        ),
        filters={
            "step_size": Decimal("0.001"),
            "min_qty": Decimal("0.001"),
            "min_notional": Decimal("20"),
        },
        mark_price=Decimal("1912.93"),
    )
    engine = _make_engine(adapter)
    action = Action(type="ENTER", contract="ETHUSDT", quantity=Decimal("0.01"))
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(engine._prepare_quantity(action, pre_cap=Decimal("0.01")))
    assert "minNotional" in str(ei.value)


def test_prepare_quantity_cleanly_rejects_when_no_pre_cap():
    # No pre-cap -> original rejection is re-raised unchanged.
    adapter = _min_notional_adapter(
        normalize_error=RuntimeError("below minQty for ETHUSDT (-1113)"),
        filters={
            "step_size": Decimal("0.001"),
            "min_qty": Decimal("0.001"),
            "min_notional": Decimal("20"),
        },
        mark_price=Decimal("1912.93"),
    )
    engine = _make_engine(adapter)
    action = Action(type="ENTER", contract="ETHUSDT", quantity=Decimal("0.0005"))
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(engine._prepare_quantity(action, pre_cap=None))
    assert "below minQty" in str(ei.value)
