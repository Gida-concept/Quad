"""Thin wrapper around the official ``mcp`` Python SDK for OKX MCP server.

Replaces the previous 800-line custom JSON-RPC client with ~80 lines
that delegate to ``mcp.ClientSession``.  Provides the same interface
that the orchestrator, context collector, and TA module expect.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import structlog
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = structlog.get_logger(__name__)


class McpError(Exception):
    """Base exception for MCP errors."""


class McpConnectionError(McpError):
    """Raised when the MCP server cannot be started."""


class OkxMcpClient:
    """Async client for the OKX MCP server using the official ``mcp`` SDK.

    Wraps ``ClientSession`` to provide a simple ``call_tool()`` interface
    plus typed convenience methods for common OKX operations.

    Parameters
    ----------
    command:
        MCP server binary name or path.
    modules:
        MCP modules to enable (default ``"all"``).
    profile:
        OKX API profile name.
    request_timeout:
        Seconds per tool call.
    startup_timeout:
        Seconds for MCP handshake.
    """

    def __init__(
        self,
        command: str = "okx-trade-mcp",
        modules: str = "all",
        profile: str = "default",
        request_timeout: float = 30.0,
        startup_timeout: float = 15.0,
    ) -> None:
        self._command = command
        self._modules = modules
        self._profile = profile
        self._request_timeout = request_timeout
        self._startup_timeout = startup_timeout

        self._session: ClientSession | None = None
        self._read_stream: Any = None
        self._write_stream: Any = None
        self._started_at: float = 0.0
        self._last_call_at: float = 0.0
        self._total_calls = 0
        self._total_errors = 0
        self._tools: dict[str, Any] = {}
        self._log = logger.bind()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the MCP server and complete the handshake."""
        if self._session is not None:
            return

        params = StdioServerParameters(
            command=self._command,
            args=["--modules", self._modules, "--profile", self._profile],
        )

        self._log.info("mcp_starting", command=self._command)

        # Open stdio transport
        read, write = await asyncio.wait_for(
            stdio_client(params).__aenter__(),
            timeout=self._startup_timeout,
        )
        self._read_stream = read
        self._write_stream = write

        # Create and initialize session
        self._session = ClientSession(read, write)
        await asyncio.wait_for(
            self._session.__aenter__(),
            timeout=self._startup_timeout,
        )
        await asyncio.wait_for(
            self._session.initialize(),
            timeout=self._startup_timeout,
        )

        # Discover tools
        tools_result = await asyncio.wait_for(
            self._session.list_tools(),
            timeout=self._startup_timeout,
        )
        self._tools = {t.name: t for t in tools_result.tools}

        self._started_at = time.monotonic()
        self._log.info(
            "mcp_started",
            tools=len(self._tools),
            profile=self._profile,
        )

    async def stop(self) -> None:
        """Shut down the MCP server."""
        if self._session is None:
            return

        self._log.info("mcp_stopping")
        try:
            await self._session.__aexit__(None, None, None)
        except Exception:
            pass
        try:
            if self._read_stream:
                await self._read_stream.aclose()
        except Exception:
            pass
        try:
            if self._write_stream:
                await self._write_stream.aclose()
        except Exception:
            pass

        self._session = None
        self._read_stream = None
        self._write_stream = None
        self._log.info(
            "mcp_stopped",
            total_calls=self._total_calls,
            total_errors=self._total_errors,
        )

    @property
    def is_running(self) -> bool:
        return self._session is not None

    @property
    def uptime(self) -> float:
        return time.monotonic() - self._started_at if self._started_at else 0.0

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "running": self.is_running,
            "uptime_s": round(self.uptime, 1),
            "tools_discovered": len(self._tools),
            "total_calls": self._total_calls,
            "total_errors": self._total_errors,
            "last_call_at": self._last_call_at,
            "profile": self._profile,
            "modules": self._modules,
        }

    # ------------------------------------------------------------------
    # Core tool call
    # ------------------------------------------------------------------

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Call an MCP tool and return the parsed result.

        Parameters
        ----------
        name:
            Tool name (e.g. ``"market_get_ticker"``).
        arguments:
            Tool arguments.

        Returns
        -------
        Parsed JSON response from the tool.
        """
        if self._session is None:
            raise McpConnectionError("MCP server not running")

        arguments = arguments or {}
        self._total_calls += 1
        self._last_call_at = time.monotonic()

        try:
            result = await asyncio.wait_for(
                self._session.call_tool(name, arguments),
                timeout=self._request_timeout,
            )
        except asyncio.TimeoutError:
            self._total_errors += 1
            raise McpError(f"Tool '{name}' timed out after {self._request_timeout}s")
        except Exception as exc:
            self._total_errors += 1
            raise McpError(f"Tool '{name}' failed: {exc}") from exc

        # Extract text content from MCP response
        if hasattr(result, "content") and result.content:
            for item in result.content:
                if hasattr(item, "text"):
                    try:
                        return json.loads(item.text)
                    except (json.JSONDecodeError, TypeError):
                        return {"raw": item.text}

        # Fallback: return result as-is
        if hasattr(result, "model_dump"):
            return result.model_dump()
        return result

    # ------------------------------------------------------------------
    # Convenience methods — Market Data
    # ------------------------------------------------------------------

    async def get_ticker(self, inst_id: str) -> dict[str, Any]:
        return await self.call_tool("market_get_ticker", {"instId": inst_id})

    async def get_candles(self, inst_id: str, bar: str = "1H", limit: int = 150) -> list:
        result = await self.call_tool("market_get_candles", {"instId": inst_id, "bar": bar, "limit": str(limit)})
        return result.get("data", []) if isinstance(result, dict) else []

    async def get_orderbook(self, inst_id: str, sz: int = 20) -> dict[str, Any]:
        return await self.call_tool("market_get_orderbook", {"instId": inst_id, "sz": str(sz)})

    async def get_funding_rate(self, inst_id: str) -> dict[str, Any]:
        return await self.call_tool("market_get_funding_rate", {"instId": inst_id})

    async def get_open_interest(self, inst_id: str) -> dict[str, Any]:
        return await self.call_tool("market_get_open_interest", {"instId": inst_id})

    async def get_mark_price(self, inst_id: str) -> dict[str, Any]:
        return await self.call_tool("market_get_mark_price", {"instId": inst_id})

    async def get_instruments(self, inst_type: str = "SWAP") -> list:
        result = await self.call_tool("market_get_instruments", {"instType": inst_type})
        return result.get("data", []) if isinstance(result, dict) else []

    # ------------------------------------------------------------------
    # Convenience methods — Indicators
    # ------------------------------------------------------------------

    async def get_indicators(self, inst_id: str, bar: str = "1H", indicators: list[str] | None = None) -> dict[str, Any]:
        if indicators is None:
            indicators = ["ema-9", "ema-21", "rsi-14", "macd", "adx", "bb-20", "atr"]
        return await self.call_tool("market_get_indicator", {"instId": inst_id, "bar": bar, "indicators": indicators})

    # ------------------------------------------------------------------
    # Convenience methods — Account
    # ------------------------------------------------------------------

    async def get_account_balance_all(self) -> dict[str, Any]:
        return await self.call_tool("account_get_balance_all", {})

    async def get_positions(self, inst_type: str = "SWAP") -> list:
        result = await self.call_tool("account_get_positions", {"instType": inst_type})
        return result.get("data", []) if isinstance(result, dict) else []

    async def get_account_config(self) -> dict[str, Any]:
        return await self.call_tool("account_get_config", {})

    # ------------------------------------------------------------------
    # Convenience methods — Orders
    # ------------------------------------------------------------------

    async def place_swap_order(self, inst_id: str, side: str, ord_type: str, sz: str, td_mode: str = "isolated", px: str | None = None, reduce_only: bool = False) -> Any:
        order: dict[str, Any] = {"instId": inst_id, "tdMode": td_mode, "side": side, "ordType": ord_type, "sz": sz}
        if px:
            order["px"] = px
        if reduce_only:
            order["reduceOnly"] = True
        return await self.call_tool("swap_batch_orders", {"action": "place", "orders": [order]})

    async def place_algo_order(self, inst_id: str, side: str, ord_type: str, sz: str, td_mode: str = "isolated", pos_side: str = "net", **kwargs: Any) -> Any:
        args: dict[str, Any] = {"instId": inst_id, "tdMode": td_mode, "side": side, "posSide": pos_side, "ordType": ord_type, "sz": sz}
        args.update(kwargs)
        return await self.call_tool("swap_place_algo_order", args)

    # ------------------------------------------------------------------
    # Convenience methods — Smart Money & News (MCP-only)
    # ------------------------------------------------------------------

    async def get_smart_money_signals(self, coin: str = "BTC", time_type: str = "7D") -> dict[str, Any]:
        return await self.call_tool("smartmoney_get_signal_overview_by_filter", {"coin": coin, "timeType": time_type})

    async def get_coin_sentiment(self, coin: str = "BTC") -> dict[str, Any]:
        return await self.call_tool("news_get_coin_sentiment", {"coin": coin})

    async def get_latest_news(self, limit: int = 20) -> dict[str, Any]:
        return await self.call_tool("news_get_latest", {"limit": str(limit)})

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> OkxMcpClient:
        await self.start()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.stop()
