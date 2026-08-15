"""Regression tests for restart flatten + stale-position guard.

Covers the behavior requested by the user (2026-08-09): on restart the bot
closes the previous trade and opens a fresh one instead of holding a stale
position for hours (see NOTE-48 / ADR-095).
"""

import time
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from quad.config.schema import AiRotationConfig
from quad.orchestrator.orchestrator import QuadOrchestrator
from quad.types.domain import Order, Position, PositionSide, PositionStatus


def _make_orchestrator(**overrides) -> QuadOrchestrator:
    orch = QuadOrchestrator.__new__(QuadOrchestrator)
    cfg = {
        "ai": {
            "pairs": ["BTCUSDT", "ETHUSDT"],
            "rotation": {
                "enabled": True,
                "close_positions_on_start": True,
                "close_open_position_each_cycle": False,
                "max_hold_seconds": 0.0,
                "price_bracket_check": True,
                "price_bracket_tolerance_pct": 0.5,
            },
        }
    }
    cfg["ai"]["rotation"].update(overrides)
    orch._config_dict = cfg
    orch._log = MagicMock()
    orch._rotation_hold_since = {}
    orch._rotation_index = 0
    orch._telegram_bot = None
    orch._telegram_chat_id = 0
    return orch


def _open_position(symbol: str = "BTCUSDT") -> Position:
    return Position(
        symbol=symbol,
        side=PositionSide.LONG,
        quantity=Decimal("0.01"),
        status=PositionStatus.OPEN,
    )


def _short_position(symbol: str = "BTCUSDT") -> Position:
    return Position(
        symbol=symbol,
        side=PositionSide.SHORT,
        quantity=Decimal("0.01"),
        status=PositionStatus.OPEN,
    )


def _bracket_order(
    order_type: str, stop_price: str, symbol: str = "BTCUSDT"
) -> Order:
    return Order(
        symbol=symbol,
        order_type=order_type,
        stop_price=Decimal(stop_price),
        status="NEW",
    )


# ---------------------------------------------------------------------------
# 1. Config schema
# ---------------------------------------------------------------------------


def test_rotation_config_new_fields_defaults():
    cfg = AiRotationConfig()
    assert cfg.close_positions_on_start is True
    assert cfg.max_hold_seconds == 0.0
    assert cfg.price_bracket_check is True
    assert cfg.price_bracket_tolerance_pct == 0.5


def test_rotation_config_price_fields_parse():
    cfg = AiRotationConfig(price_bracket_check=False, price_bracket_tolerance_pct=1.0)
    assert cfg.price_bracket_check is False
    assert cfg.price_bracket_tolerance_pct == 1.0


def test_rotation_config_new_fields_parse():
    cfg = AiRotationConfig(close_positions_on_start=False, max_hold_seconds=7200)
    assert cfg.close_positions_on_start is False
    assert cfg.max_hold_seconds == 7200


# ---------------------------------------------------------------------------
# 2. Startup flatten of orphan positions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_startup_flattens_previous_positions_when_enabled():
    orch = _make_orchestrator()
    orch._exchange_adapter = AsyncMock()
    orch._exchange_adapter.get_positions = AsyncMock(
        return_value=[_open_position("BTCUSDT")]
    )
    orch._close_all_positions = AsyncMock(return_value=True)

    await orch._close_orphan_positions_on_start()

    orch._close_all_positions.assert_awaited_once()
    assert any(
        "startup_flattening_previous_positions"
        in str(call.args[0])
        for call in orch._log.info.call_args_list
    )


@pytest.mark.asyncio
async def test_startup_skips_flatten_when_rotation_disabled():
    orch = _make_orchestrator()
    orch._config_dict["ai"]["rotation"]["enabled"] = False
    orch._exchange_adapter = AsyncMock()
    orch._close_all_positions = AsyncMock(return_value=True)

    await orch._close_orphan_positions_on_start()

    orch._close_all_positions.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_skips_flatten_when_config_off():
    orch = _make_orchestrator(close_positions_on_start=False)
    orch._exchange_adapter = AsyncMock()
    orch._close_all_positions = AsyncMock(return_value=True)

    await orch._close_orphan_positions_on_start()

    orch._close_all_positions.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_skips_flatten_when_flat():
    orch = _make_orchestrator()
    orch._exchange_adapter = AsyncMock()
    orch._exchange_adapter.get_positions = AsyncMock(return_value=[])
    orch._close_all_positions = AsyncMock(return_value=True)

    await orch._close_orphan_positions_on_start()

    orch._close_all_positions.assert_not_awaited()
    assert any(
        "startup_no_orphan_positions" in str(call.args[0])
        for call in orch._log.info.call_args_list
    )


# ---------------------------------------------------------------------------
# 3. Stale-position guard (max_hold_seconds)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_hold_force_closes_stale_position():
    orch = _make_orchestrator(max_hold_seconds=3600.0)
    groq = MagicMock()
    groq.is_available = MagicMock(return_value=True)
    orch._groq_client = groq
    orch._exchange_adapter = AsyncMock()
    # Held far longer than the 1h cap.
    orch._rotation_hold_since = {"BTCUSDT": time.monotonic() - 7200.0}
    orch._close_all_positions = AsyncMock(return_value=True)

    from quad.types.strategy import StrategyContext

    result = await orch._run_ai_rotation(
        account=None,
        positions=[_open_position("BTCUSDT")],
        open_orders=[],
        context=StrategyContext(config=orch._config_dict),
    )

    orch._scan_pair = AsyncMock(
        return_value={"action": "HOLD", "confidence": 0.5, "indicators": {}}
    )

    assert result is True
    orch._close_all_positions.assert_awaited_once()
    orch._scan_pair.assert_not_awaited()
    assert orch._rotation_hold_since == {}
    assert any(
        "rotation_max_hold_reached" in str(call.args[0])
        for call in orch._log.info.call_args_list
    )


@pytest.mark.asyncio
async def test_max_hold_does_not_close_fresh_position():
    orch = _make_orchestrator(max_hold_seconds=3600.0)
    groq = MagicMock()
    groq.is_available = MagicMock(return_value=True)
    orch._groq_client = groq
    orch._exchange_adapter = AsyncMock()
    orch._rotation_hold_since = {"BTCUSDT": time.monotonic() - 60.0}
    orch._close_all_positions = AsyncMock(return_value=True)
    orch._scan_pair = AsyncMock(
        return_value={"action": "HOLD", "confidence": 0.5, "indicators": {}}
    )

    from quad.types.strategy import StrategyContext

    result = await orch._run_ai_rotation(
        account=None,
        positions=[_open_position("BTCUSDT")],
        open_orders=[],
        context=StrategyContext(config=orch._config_dict),
    )

    assert result is True
    orch._close_all_positions.assert_not_awaited()
    orch._scan_pair.assert_awaited_once()
    assert "BTCUSDT" in orch._rotation_hold_since




# ---------------------------------------------------------------------------
# 4. Price-bracket guard (mark price clearly beyond TP/SL trigger)
# ---------------------------------------------------------------------------


async def _run_case_a(orch, positions, open_orders):
    from quad.types.strategy import StrategyContext

    return await orch._run_ai_rotation(
        account=None,
        positions=positions,
        open_orders=open_orders,
        context=StrategyContext(config=orch._config_dict),
    )


def _ready_orch(orch):
    groq = MagicMock()
    groq.is_available = MagicMock(return_value=True)
    orch._groq_client = groq
    orch._exchange_adapter = AsyncMock()
    orch._close_all_positions = AsyncMock(return_value=True)
    orch._scan_pair = AsyncMock(
        return_value={"action": "HOLD", "confidence": 0.5, "indicators": {}}
    )
    orch._telegram_bot = None
    orch._telegram_chat_id = 0
    return orch


@pytest.mark.asyncio
async def test_price_beyond_sl_forces_close_long():
    orch = _ready_orch(_make_orchestrator())
    orch._exchange_adapter.get_mark_price = AsyncMock(return_value=Decimal("59000"))
    pos = _open_position()  # LONG
    orders = [_bracket_order("STOP_MARKET", "60000"), _bracket_order("TAKE_PROFIT_MARKET", "70000")]

    result = await _run_case_a(orch, [pos], orders)

    assert result is True
    orch._close_all_positions.assert_awaited_once()
    orch._scan_pair.assert_not_awaited()
    assert any(
        "rotation_price_beyond_bracket" in str(call.args[0])
        for call in orch._log.info.call_args_list
    )


@pytest.mark.asyncio
async def test_price_beyond_tp_forces_close_long():
    orch = _ready_orch(_make_orchestrator())
    orch._exchange_adapter.get_mark_price = AsyncMock(return_value=Decimal("71000"))
    pos = _open_position()  # LONG
    orders = [_bracket_order("STOP_MARKET", "60000"), _bracket_order("TAKE_PROFIT_MARKET", "70000")]

    result = await _run_case_a(orch, [pos], orders)

    assert result is True
    orch._close_all_positions.assert_awaited_once()
    orch._scan_pair.assert_not_awaited()


@pytest.mark.asyncio
async def test_price_beyond_sl_forces_close_short():
    orch = _ready_orch(_make_orchestrator())
    orch._exchange_adapter.get_mark_price = AsyncMock(return_value=Decimal("71000"))
    pos = _short_position()  # SHORT: SL above entry
    orders = [_bracket_order("STOP_MARKET", "70000"), _bracket_order("TAKE_PROFIT_MARKET", "60000")]

    result = await _run_case_a(orch, [pos], orders)

    assert result is True
    orch._close_all_positions.assert_awaited_once()
    orch._scan_pair.assert_not_awaited()


@pytest.mark.asyncio
async def test_price_inside_bracket_no_close():
    orch = _ready_orch(_make_orchestrator())
    orch._exchange_adapter.get_mark_price = AsyncMock(return_value=Decimal("65000"))
    pos = _open_position()  # LONG
    orders = [_bracket_order("STOP_MARKET", "60000"), _bracket_order("TAKE_PROFIT_MARKET", "70000")]

    result = await _run_case_a(orch, [pos], orders)

    assert result is True
    orch._close_all_positions.assert_not_awaited()
    orch._scan_pair.assert_awaited_once()


@pytest.mark.asyncio
async def test_price_beyond_trigger_within_tolerance_no_close():
    orch = _ready_orch(_make_orchestrator(price_bracket_tolerance_pct=2.0))
    # 60800 is only ~1.3% below the 60000 SL -> inside the 2% tolerance.
    orch._exchange_adapter.get_mark_price = AsyncMock(return_value=Decimal("60800"))
    pos = _open_position()
    orders = [_bracket_order("STOP_MARKET", "60000")]

    result = await _run_case_a(orch, [pos], orders)

    assert result is True
    orch._close_all_positions.assert_not_awaited()
    orch._scan_pair.assert_awaited_once()


@pytest.mark.asyncio
async def test_price_bracket_check_disabled():
    orch = _ready_orch(_make_orchestrator(price_bracket_check=False))
    orch._exchange_adapter.get_mark_price = AsyncMock(return_value=Decimal("59000"))
    pos = _open_position()
    orders = [_bracket_order("STOP_MARKET", "60000")]

    result = await _run_case_a(orch, [pos], orders)

    assert result is True
    orch._close_all_positions.assert_not_awaited()
    orch._scan_pair.assert_awaited_once()


@pytest.mark.asyncio
async def test_price_bracket_check_no_triggers_no_close():
    orch = _ready_orch(_make_orchestrator())
    pos = _open_position()
    orders: list[Order] = []

    result = await _run_case_a(orch, [pos], orders)

    assert result is True
    orch._close_all_positions.assert_not_awaited()
    orch._scan_pair.assert_awaited_once()


@pytest.mark.asyncio
async def test_price_bracket_check_mark_unavailable_no_close():
    orch = _ready_orch(_make_orchestrator())
    orch._exchange_adapter.get_mark_price = AsyncMock(return_value=None)
    pos = _open_position()
    orders = [_bracket_order("STOP_MARKET", "60000")]

    result = await _run_case_a(orch, [pos], orders)

    assert result is True
    orch._close_all_positions.assert_not_awaited()
    orch._scan_pair.assert_awaited_once()
@pytest.mark.asyncio
async def test_max_hold_disabled_by_zero():
    orch = _make_orchestrator(max_hold_seconds=0.0)
    groq = MagicMock()
    groq.is_available = MagicMock(return_value=True)
    orch._groq_client = groq
    orch._exchange_adapter = AsyncMock()
    # Even an old timer must not trigger a close when the guard is off.
    orch._rotation_hold_since = {"BTCUSDT": time.monotonic() - 99999.0}
    orch._close_all_positions = AsyncMock(return_value=True)
    orch._scan_pair = AsyncMock(
        return_value={"action": "HOLD", "confidence": 0.5, "indicators": {}}
    )

    from quad.types.strategy import StrategyContext

    result = await orch._run_ai_rotation(
        account=None,
        positions=[_open_position("BTCUSDT")],
        open_orders=[],
        context=StrategyContext(config=orch._config_dict),
    )

    assert result is True
    orch._close_all_positions.assert_not_awaited()


# ---------------------------------------------------------------------------
# 5. Roll every cycle: close old position + open fresh trade (1/cycle)
# ---------------------------------------------------------------------------


def test_rotation_config_close_each_cycle_default():
    cfg = AiRotationConfig()
    assert cfg.close_open_position_each_cycle is True


def test_rotation_config_close_each_cycle_parse():
    cfg = AiRotationConfig(close_open_position_each_cycle=False)
    assert cfg.close_open_position_each_cycle is False


def _roll_orch(**overrides):
    """Orchestrator with close_open_position_each_cycle enabled (new default)."""
    orch = _make_orchestrator(close_open_position_each_cycle=True, **overrides)
    return orch


@pytest.mark.asyncio
async def test_roll_cycle_closes_open_position_and_scans_next_pair():
    orch = _roll_orch()
    groq = MagicMock()
    groq.is_available = MagicMock(return_value=True)
    orch._groq_client = groq
    orch._exchange_adapter = AsyncMock()
    orch._exchange_adapter.get_positions = AsyncMock(return_value=[])
    orch._close_all_positions = AsyncMock(return_value=True)
    orch._scan_pair = AsyncMock(
        return_value={"action": "HOLD", "confidence": 0.5, "indicators": {}}
    )

    from quad.types.strategy import StrategyContext

    result = await orch._run_ai_rotation(
        account=None,
        positions=[_open_position("BTCUSDT")],
        open_orders=[],
        context=StrategyContext(config=orch._config_dict),
    )

    assert result is True
    # Old position closed, then flat scan ran.
    orch._close_all_positions.assert_awaited_once()
    orch._scan_pair.assert_awaited()
    # Rotation advanced past the held pair (BTCUSDT -> ETHUSDT at index 1).
    assert orch._rotation_index == 1
    assert any(
        "rotation_cycle_rolling_position" in str(call.args[0])
        for call in orch._log.info.call_args_list
    )


@pytest.mark.asyncio
async def test_roll_cycle_notifies_exit_then_scan():
    orch = _roll_orch()
    groq = MagicMock()
    groq.is_available = MagicMock(return_value=True)
    orch._groq_client = groq
    orch._exchange_adapter = AsyncMock()
    orch._exchange_adapter.get_positions = AsyncMock(return_value=[])
    orch._close_all_positions = AsyncMock(return_value=True)
    orch._notify_trade = AsyncMock()
    orch._scan_pair = AsyncMock(
        return_value={"action": "HOLD", "confidence": 0.5, "indicators": {}}
    )

    from quad.types.strategy import StrategyContext

    result = await orch._run_ai_rotation(
        account=None,
        positions=[_open_position("BTCUSDT")],
        open_orders=[],
        context=StrategyContext(config=orch._config_dict),
    )

    assert result is True
    orch._notify_trade.assert_awaited_once()
    call = orch._notify_trade.await_args
    assert call.kwargs["action_type"] == "EXIT"
    assert call.kwargs["contract"] == "BTCUSDT"
    assert "Rotation cycle" in call.kwargs["reason"]


@pytest.mark.asyncio
async def test_roll_cycle_close_failure_does_not_scan():
    orch = _roll_orch()
    groq = MagicMock()
    groq.is_available = MagicMock(return_value=True)
    orch._groq_client = groq
    orch._exchange_adapter = AsyncMock()
    orch._close_all_positions = AsyncMock(return_value=False)
    orch._scan_pair = AsyncMock(
        return_value={"action": "ENTER", "confidence": 0.9, "indicators": {}}
    )

    from quad.types.strategy import StrategyContext

    result = await orch._run_ai_rotation(
        account=None,
        positions=[_open_position("BTCUSDT")],
        open_orders=[],
        context=StrategyContext(config=orch._config_dict),
    )

    assert result is True
    orch._close_all_positions.assert_awaited_once()
    # A failed close must NOT open a second trade this cycle.
    orch._scan_pair.assert_not_awaited()


@pytest.mark.asyncio
async def test_roll_cycle_refreshes_positions_for_status():
    orch = _roll_orch()
    groq = MagicMock()
    groq.is_available = MagicMock(return_value=True)
    orch._groq_client = groq
    orch._exchange_adapter = AsyncMock()
    orch._exchange_adapter.get_positions = AsyncMock(return_value=[])
    orch._close_all_positions = AsyncMock(return_value=True)
    orch._scan_pair = AsyncMock(
        return_value={"action": "HOLD", "confidence": 0.5, "indicators": {}}
    )

    from quad.types.strategy import StrategyContext

    positions = [_open_position("BTCUSDT")]
    result = await orch._run_ai_rotation(
        account=None,
        positions=positions,
        open_orders=[],
        context=StrategyContext(config=orch._config_dict),
    )

    assert result is True
    # The caller's list is refreshed in place so cycle_status is accurate.
    assert positions == []


@pytest.mark.asyncio
async def test_legacy_hold_mode_keeps_position_when_flag_off():
    orch = _make_orchestrator(close_open_position_each_cycle=False)
    groq = MagicMock()
    groq.is_available = MagicMock(return_value=True)
    orch._groq_client = groq
    orch._exchange_adapter = AsyncMock()
    orch._exchange_adapter.get_mark_price = AsyncMock(return_value=Decimal("65000"))
    orch._close_all_positions = AsyncMock(return_value=True)
    orch._scan_pair = AsyncMock(
        return_value={"action": "HOLD", "confidence": 0.5, "indicators": {}}
    )

    from quad.types.strategy import StrategyContext

    pos = _open_position()
    orders = [_bracket_order("STOP_MARKET", "60000"), _bracket_order("TAKE_PROFIT_MARKET", "70000")]

    result = await orch._run_ai_rotation(
        account=None,
        positions=[pos],
        open_orders=orders,
        context=StrategyContext(config=orch._config_dict),
    )

    assert result is True
    # Legacy mode: no roll close when the flag is off and price is inside bracket.
    orch._close_all_positions.assert_not_awaited()
    orch._scan_pair.assert_awaited_once()
