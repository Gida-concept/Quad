"""Schema tests for the Bybit-only exchange configuration."""

import pytest

from quad.config.schema import BybitConfig, ExchangeConfig, QuadConfig, validate_config


def test_bybit_config_defaults():
    c = BybitConfig()
    assert c.base_url == "https://api.bybit.com"
    assert c.testnet_base_url == "https://api-testnet.bybit.com"
    assert c.ws_public_url == "wss://stream.bybit.com/v5/public"
    assert c.recv_window == 5000


def test_exchange_name_bybit_only():
    assert ExchangeConfig().name == "bybit"
    assert ExchangeConfig(name="bybit").bybit.base_url.startswith("https://")
    with pytest.raises(ValueError):
        ExchangeConfig(name="okx")


def test_quad_config_has_bybit_no_mcp():
    q = QuadConfig()
    assert q.exchange.name == "bybit"
    assert not hasattr(q, "mcp")


def test_minimal_bybit_config_validates():
    ok, errors = validate_config({"exchange": {"name": "bybit", "testnet": True}})
    assert ok, errors
