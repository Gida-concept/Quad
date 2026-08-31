"""Exchange adapter factory.

Creates the correct ``ExchangeAdapter`` implementation based on the
application configuration.

When ``config.mcp.enabled`` is ``True``, the orchestrator calls MCP
tools directly (no adapter wrapper needed).  This factory only creates
``OkxFuturesAdapter`` for the non-MCP fallback path.
"""

from __future__ import annotations

import os
from typing import Any

import structlog

from quad.exchange.base import ExchangeAdapter

logger = structlog.get_logger(__name__)


def create_exchange(
    config: dict | None = None,
) -> ExchangeAdapter:
    """Create an exchange adapter based on the provided configuration.

    OKX USDT perpetual (instType=SWAP) is the only supported exchange.
    Testnet is the default safety environment and live is opt-in via
    ``exchange.testnet: false``.

    When MCP is enabled, the orchestrator calls MCP tools directly
    instead of going through this adapter.  This factory is only
    used for the non-MCP fallback path.
    """
    from quad.exchange.okx import OkxFuturesAdapter

    cfg = config or {}

    exchange_cfg = cfg.get("exchange", {})
    api_key = exchange_cfg.get("api_key") or os.environ.get("OKX_API_KEY", "")
    api_secret = exchange_cfg.get("api_secret") or os.environ.get(
        "OKX_API_SECRET", ""
    )
    passphrase = exchange_cfg.get("passphrase") or os.environ.get(
        "OKX_PASSPHRASE", ""
    )
    testnet = _coerce_bool(
        exchange_cfg.get("testnet") or os.environ.get("OKX_TESTNET", "")
    )

    logger.info("create_exchange", mode="okx", testnet=testnet)

    return OkxFuturesAdapter(
        api_key=api_key,
        api_secret=api_secret,
        passphrase=passphrase,
        testnet=testnet,
        config=cfg,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _coerce_bool(value: object) -> bool:
    """Coerce a value to bool, handling string representations."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return bool(value)
