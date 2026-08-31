"""Verify MCP SDK imports and API."""
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import Tool, CallToolResult


def test_mcp_sdk_imports():
    """Verify all required MCP SDK classes are importable."""
    assert ClientSession is not None
    assert StdioServerParameters is not None
    assert stdio_client is not None
    assert Tool is not None
    assert CallToolResult is not None


def test_mcp_sdk_server_params():
    """Verify StdioServerParameters can be created."""
    params = StdioServerParameters(
        command="okx-trade-mcp",
        args=["--modules", "all", "--profile", "default"],
    )
    assert params.command == "okx-trade-mcp"
    assert params.args == ["--modules", "all", "--profile", "default"]

