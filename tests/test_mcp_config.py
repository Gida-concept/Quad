"""Quick smoke test for McpConfig schema."""
from quad.config.schema import McpConfig, QuadConfig


def test_mcp_config_defaults():
    c = McpConfig()
    assert c.enabled is True  # MCP is now the default
    assert c.command == "okx-trade-mcp"
    assert c.modules == "all"
    assert c.profile == "default"
    assert c.request_timeout == 30.0
    assert c.startup_timeout == 15.0


def test_quad_config_has_mcp():
    q = QuadConfig()
    assert hasattr(q, "mcp")
    assert isinstance(q.mcp, McpConfig)
    assert q.mcp.enabled is True  # MCP is now the default


def test_mcp_config_from_dict():
    c = McpConfig(**{
        "enabled": True,
        "command": "/usr/local/bin/okx-trade-mcp",
        "modules": "market,swap,account",
        "profile": "live",
    })
    assert c.enabled is True
    assert c.profile == "live"
