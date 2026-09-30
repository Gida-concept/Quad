"""Smoke tests for the Bybit-only exchange factory."""

from quad.exchange.factory import create_exchange


def test_factory_creates_bybit_adapter():
    """Factory always creates BybitFuturesAdapter (Bybit is the only exchange)."""
    adapter = create_exchange(config={"exchange": {"testnet": True}})
    assert type(adapter).__name__ == "BybitFuturesAdapter"
    assert adapter.is_testnet is True


def test_factory_default_config():
    """Factory works with empty config (defaults to Bybit testnet)."""
    adapter = create_exchange()
    assert type(adapter).__name__ == "BybitFuturesAdapter"
    assert adapter.is_testnet is True


def test_factory_live_opt_in():
    adapter = create_exchange(config={"exchange": {"testnet": False}})
    assert type(adapter).__name__ == "BybitFuturesAdapter"
    assert adapter.is_testnet is False
