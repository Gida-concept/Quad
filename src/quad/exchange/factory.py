"""Exchange adapter factory.

Creates the Bybit USDT-perpetual adapter — the only supported exchange.
Testnet (``https://api-testnet.bybit.com``) is the default safety
environment; live is opt-in via ``exchange.testnet: false``.
"""

from __future__ import annotations

import os
from typing import Any

import structlog

from quad.exchange.base import ExchangeAdapter

logger = structlog.get_logger(__name__)


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return True  # testnet-safe default
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def create_exchange(
    config: dict | None = None,
) -> ExchangeAdapter:
    """Create the Bybit USDT-perpetual adapter from configuration.

    Credentials resolve from ``exchange.api_key`` / ``exchange.api_secret``
    first, then ``BYBIT_API_KEY`` / ``BYBIT_API_SECRET``.
    """
    from quad.exchange.bybit import BybitFuturesAdapter

    cfg = config or {}

    exchange_cfg = cfg.get("exchange", {})
    api_key = exchange_cfg.get("api_key") or os.environ.get("BYBIT_API_KEY", "")
    api_secret = exchange_cfg.get("api_secret") or os.environ.get(
        "BYBIT_API_SECRET", ""
    )
    # Testnet-safe default: absent/empty resolves to True (never live by accident).
    raw_testnet = exchange_cfg.get("testnet", None)
    if raw_testnet is None:
        raw_testnet = os.environ.get("BYBIT_TESTNET", None)
    testnet = True if raw_testnet in (None, "") else _coerce_bool(raw_testnet)

    logger.info("create_exchange", mode="bybit", testnet=testnet)

    return BybitFuturesAdapter(
        api_key=api_key,
        api_secret=api_secret,
        testnet=testnet,
        config=cfg,
    )
