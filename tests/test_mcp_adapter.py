"""Smoke tests for MCP integration (SDK-based)."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from quad.exchange.factory import create_exchange


def test_factory_creates_okx_adapter():
    """Factory always creates OkxFuturesAdapter (MCP is called directly)."""
    adapter = create_exchange(config={"exchange": {"testnet": True}})
    assert type(adapter).__name__ == "OkxFuturesAdapter"


def test_factory_default_config():
    """Factory works with empty config (defaults to OKX testnet)."""
    adapter = create_exchange()
    assert type(adapter).__name__ == "OkxFuturesAdapter"
