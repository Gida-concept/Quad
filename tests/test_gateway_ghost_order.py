r"""Tests for gateway ghost-order resolution (-2013 "Order does not exist")."""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from quad.execution.gateway import OrderGateway  # noqa: E402
from quad.types.domain import Order  # noqa: E402

GATEWAY_CFG = {
    "exchange": {
        "gateway": {
            "completed_ids_maxlen": 100,
            "confirmation_timeout_seconds": 5,
            "max_retries": 1,
            "backoff_base_seconds": 0.1,
        },
    }
}

_NOT_FOUND_ERR = RuntimeError(
    'Order error (400): {"code":-2013,"msg":"Order does not exist."}'
)


def _gateway(adapter) -> OrderGateway:
    return OrderGateway(adapter, config=GATEWAY_CFG)


def test_refresh_state_resolves_ghost_order_not_polled_again():
    """A tracked order the exchange says doesn't exist is removed from active once."""
    adapter = MagicMock()
    adapter.get_open_orders = AsyncMock(return_value=[])  # not an open order
    adapter.get_order_status = AsyncMock(side_effect=_NOT_FOUND_ERR)
    adapter.is_order_not_found = MagicMock(return_value=True)

    gw = _gateway(adapter)
    ghost = Order(
        id=1000000172889943,
        client_order_id="fancy-bracket-id",
        symbol="BTCUSDT",
        side="SELL",
        order_type="STOP_MARKET",
        status="NEW",
    )
    gw._active_orders["fancy-bracket-id"] = ghost

    asyncio.run(gw.refresh_state())

    # The ghost must be resolved: out of active tracking, into completed.
    assert "fancy-bracket-id" not in gw._active_orders
    assert "fancy-bracket-id" in gw._completed_ids
    # And it must never be queried again on the next refresh cycle.
    asyncio.run(gw.refresh_state())
    assert adapter.get_order_status.call_count == 1


def test_refresh_state_keeps_order_on_transient_error():
    """A transient (non -2013) query failure must NOT resolve the order."""
    adapter = MagicMock()
    adapter.get_open_orders = AsyncMock(return_value=[])
    adapter.get_order_status = AsyncMock(
        side_effect=RuntimeError("Connection reset by peer")
    )
    adapter.is_order_not_found = MagicMock(return_value=False)
    gw = _gateway(adapter)
    order = Order(
        id=42,
        client_order_id="keep-me",
        symbol="BTCUSDT",
        side="BUY",
        status="NEW",
    )
    gw._active_orders["keep-me"] = order
    asyncio.run(gw.refresh_state())
    assert "keep-me" in gw._active_orders
    assert "keep-me" not in gw._completed_ids
