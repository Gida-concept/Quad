"""Centralized WebSocket connection manager for OKX V5 futures market data streams.

Provides ``WebSocketManager`` that manages subscriptions to OKX V5 channels,
handles automatic reconnection with exponential backoff, and routes incoming
messages to registered callbacks.

Supports OKX V5 public/private WebSocket streams including:
  - ``tickers`` — 24h ticker data for all symbols
  - ``mark-price`` — mark price + funding rate updates
  - ``books5`` — top 5 order book levels (best bid/ask)
  - ``candle{interval}`` — kline/candlestick updates
  - ``liquidation-orders`` — forced/liquidation order events
  - ``trades`` — public trade feed

Uses ``aiohttp`` for WebSocket connections. Supports multiplexed subscriptions
(multiple channels per connection) as recommended by OKX V5 API.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import aiohttp
import structlog

if TYPE_CHECKING:
    from quad.exchange.base import ExchangeAdapter

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# OKX V5 channel constants
# ---------------------------------------------------------------------------

# Channel names used by OKX V5 WebSocket API
CHANNEL_TICKERS = "tickers"
CHANNEL_MARK_PRICE = "mark-price"
CHANNEL_BOOKS5 = "books5"
CHANNEL_BOOKS = "books"
CHANNEL_CANDLE = "candle"
CHANNEL_LIQUIDATION_ORDERS = "liquidation-orders"
CHANNEL_TRADES = "trades"

# Default heartbeat interval (OKX requires ping every 30 seconds)
DEFAULT_HEARTBEAT_INTERVAL = 30.0


# ---------------------------------------------------------------------------
# Subscription dataclass
# ---------------------------------------------------------------------------


@dataclass
class _Subscription:
    """Internal record for a single channel subscription."""

    id: str
    """Unique subscription identifier (uuid4)."""

    channel: str
    """OKX V5 channel name (e.g. ``"tickers"``)."""

    inst_id: str
    """OKX V5 instrument ID (e.g. ``"BTC-USDT-SWAP"`` or ``"*"``)."""

    handler: Callable[[dict], Awaitable[None]]
    """Async callback invoked with each parsed JSON message."""

    status: Literal["active", "paused", "error"] = "active"
    """Current subscription status."""

    created_at: float = field(default_factory=time.time)
    """Wall-clock timestamp when this subscription was created."""

    last_message_at: float = field(default_factory=time.time)
    """Wall-clock timestamp of the last received message."""

    reconnect_count: int = 0
    """Number of times the underlying connection has been reconnected."""


# ---------------------------------------------------------------------------
# WebSocketManager
# ---------------------------------------------------------------------------


class WebSocketManager:
    """Manages WebSocket subscriptions to OKX V5 market data channels.

    * Accepts channel subscriptions with instrument IDs.
    * Handles reconnection with exponential backoff + jitter.
    * Routes received messages to registered handlers by channel.
    * Supports multiplexed subscriptions (multiple channels per connection).

    Usage::

        mgr = WebSocketManager(exchange_adapter)
        await mgr.start()
        sub_id = await mgr.subscribe("tickers", "BTC-USDT-SWAP", my_handler)
        ...
        await mgr.unsubscribe(sub_id)
        await mgr.stop()
    """

    def __init__(
        self,
        exchange_adapter: ExchangeAdapter,
        config: dict | None = None,
    ) -> None:
        """Initialize the WebSocket manager.

        Parameters
        ----------
        exchange_adapter:
            The exchange adapter (used for stream URL configuration).
        config:
            Optional configuration dict.  Recognised keys:

            * ``ws_url`` — Override the WebSocket URL.
              Defaults to ``wss://ws.okx.com:8443/ws/v5/public``.
            * ``ws_heartbeat_interval`` — Seconds between keepalive pings.
        """
        self._exchange = exchange_adapter
        self._config = config or {}
        self._market_data_config = self._config["market_data"]
        self._ws_config = self._market_data_config["websocket"]

        # WebSocket endpoint
        self._ws_url = self._ws_config["url"]

        self._log = logger.bind(ws_url=self._ws_url)

        # Subscription management
        self._subscriptions: dict[str, _Subscription] = {}
        # subscription_id -> subscription

        # Connection management (multiplexed: one connection for all channels)
        self._connection: aiohttp.ClientWebSocketResponse[bool] | None = None
        self._connection_task: asyncio.Task[None] | None = None

        # Shared aiohttp session (created once in start())
        self._session: aiohttp.ClientSession | None = None

        self._running = False
        self._lock = asyncio.Lock()

        # Pending subscribe/unsubscribe operations
        self._pending_ops: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Begin processing all active subscriptions.

        Creates the shared HTTP session and starts a single multiplexed
        connection task for all channels.
        """
        if self._running:
            self._log.warning("already_running")
            return

        self._running = True
        self._session = aiohttp.ClientSession()
        self._log.info("ws_manager_started")

        # Start the single connection task
        self._connection_task = asyncio.create_task(
            self._run_connection(),
        )

    async def stop(self) -> None:
        """Gracefully stop all connections and cancel background tasks.

        Closes the WebSocket connection, cancels connection tasks, and
        closes the shared HTTP session.
        """
        if not self._running:
            return

        self._log.info("ws_manager_stopping")
        self._running = False

        # Close the connection
        if self._connection is not None:
            try:
                await self._connection.close()
            except Exception:
                self._log.exception("ws_close_error")
            self._connection = None

        # Cancel the connection task
        if self._connection_task is not None:
            self._connection_task.cancel()
            self._connection_task = None

        # Close shared HTTP session
        if self._session is not None:
            await self._session.close()
            self._session = None

        self._log.info("ws_manager_stopped")

    # ------------------------------------------------------------------
    # Subscription management
    # ------------------------------------------------------------------

    async def subscribe(
        self,
        channel: str,
        inst_id: str,
        handler: Callable[[dict], Awaitable[None]],
    ) -> str:
        """Subscribe to an OKX V5 channel and register a callback.

        Parameters
        ----------
        channel:
            OKX V5 channel name (e.g. ``"tickers"``).
        inst_id:
            OKX V5 instrument ID (e.g. ``"BTC-USDT-SWAP"`` or ``"*"``).
        handler:
            Async callback invoked with each decoded JSON message.

        Returns
        -------
        str
            A unique subscription ID that can be passed to
            :meth:`unsubscribe`.
        """
        sub_id = str(uuid.uuid4())
        sub = _Subscription(
            id=sub_id,
            channel=channel,
            inst_id=inst_id,
            handler=handler,
        )

        async with self._lock:
            self._subscriptions[sub_id] = sub

        # Queue a subscribe operation
        await self._pending_ops.put({
            "op": "subscribe",
            "args": [{"channel": channel, "instId": inst_id}],
        })

        self._log.debug(
            "subscribed",
            channel=channel,
            inst_id=inst_id,
            sub_id=sub_id,
            total_subs=len(self._subscriptions),
        )
        return sub_id

    async def unsubscribe(self, subscription_id: str) -> bool:
        """Unsubscribe from a channel by subscription ID.

        Parameters
        ----------
        subscription_id:
            The subscription ID returned by :meth:`subscribe`.

        Returns
        -------
        bool
            ``True`` if the subscription was found and removed.
        """
        async with self._lock:
            sub = self._subscriptions.pop(subscription_id, None)
            if sub is None:
                return False

        # Queue an unsubscribe operation
        await self._pending_ops.put({
            "op": "unsubscribe",
            "args": [{"channel": sub.channel, "instId": sub.inst_id}],
        })

        self._log.debug(
            "unsubscribed",
            sub_id=subscription_id,
            channel=sub.channel,
            inst_id=sub.inst_id,
        )
        return True

    async def resubscribe_all(self) -> None:
        """Reconnect and re-subscribe all active subscriptions.

        Closes the existing connection and re-establishes it.  Useful
        after a complete connection loss.
        """
        async with self._lock:
            # Close existing connection
            if self._connection is not None:
                try:
                    await self._connection.close()
                except Exception:  # noqa: S110  best-effort close
                    pass
                self._connection = None

            # Cancel existing task
            if self._connection_task is not None:
                self._connection_task.cancel()
                self._connection_task = None

            # Reset reconnect counts
            for sub in self._subscriptions.values():
                sub.reconnect_count = 0

        # Restart connection task
        self._connection_task = asyncio.create_task(
            self._run_connection(),
        )

        self._log.info("resubscribed_all")

    # ------------------------------------------------------------------
    # Health / status
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Return current connection status for all subscriptions.

        Returns
        -------
        dict
            Keys:
            * ``active_subscriptions`` — total number of active subscriptions.
            * ``channels_active`` — number of distinct channels.
            * ``reconnect_count`` — total reconnects.
            * ``last_message_times`` — mapping of channel -> last message
              timestamp (epoch seconds, or 0 if no message yet).
        """
        reconnect_count = 0
        last_message_times: dict[str, float] = {}
        channels_active = set()

        for sub in self._subscriptions.values():
            reconnect_count += sub.reconnect_count
            channels_active.add(sub.channel)
            key = f"{sub.channel}:{sub.inst_id}"
            last_message_times[key] = sub.last_message_at

        return {
            "active_subscriptions": len(self._subscriptions),
            "channels_active": len(channels_active),
            "reconnect_count": reconnect_count,
            "last_message_times": last_message_times,
        }

    # ------------------------------------------------------------------
    # Internal: connection runner
    # ------------------------------------------------------------------

    async def _run_connection(self) -> None:
        """Background task that maintains a single multiplexed WebSocket connection.

        Connects to the WebSocket endpoint, processes pending subscribe/unsubscribe
        operations, reads messages, and dispatches them to registered handlers.
        Reconnects automatically on failure with exponential backoff.
        """
        ws_backoff_cfg = self._ws_config["backoff"]
        ws_base_backoff = float(ws_backoff_cfg["base_seconds"])
        ws_max_backoff = float(ws_backoff_cfg["max_seconds"])
        ws_backoff_mult = float(ws_backoff_cfg["multiplier"])
        ws_jitter = float(ws_backoff_cfg["jitter_fraction"])

        backoff = ws_base_backoff

        while self._running:
            # Check whether there are any subscriptions
            async with self._lock:
                if not self._subscriptions:
                    self._log.debug("no_subscriptions")
                    return

            try:
                await self._connect_and_read()
                # Connection closed cleanly --- reset backoff
                backoff = ws_base_backoff
            except asyncio.CancelledError:
                self._log.debug("ws_task_cancelled")
                raise
            except Exception:
                self._log.exception(
                    "ws_connection_error",
                    backoff_s=round(backoff, 2),
                )

            if not self._running:
                break

            # Update reconnect counts
            async with self._lock:
                for sub in self._subscriptions.values():
                    sub.reconnect_count += 1

            # Exponential backoff with jitter
            jitter = random.uniform(0, backoff * ws_jitter)
            await asyncio.sleep(backoff + jitter)
            backoff = min(
                backoff * ws_backoff_mult,
                ws_max_backoff,
            )

    async def _connect_and_read(self) -> None:
        """Connect to OKX V5 WebSocket and read messages.

        Opens a WebSocket connection, subscribes to all active channels,
        and forwards incoming messages to registered handlers until
        the connection is closed or cancelled.
        """
        session = self._session
        if session is None:
            raise RuntimeError("WebSocketManager not started")

        async with session.ws_connect(
            self._ws_url,
            heartbeat=DEFAULT_HEARTBEAT_INTERVAL,
        ) as ws:
            self._connection = ws
            self._log.info("ws_connected", url=self._ws_url)

            # Subscribe to all active channels
            await self._subscribe_all_channels(ws)

            # Process pending operations and read messages
            try:
                while self._running:
                    # Process pending subscribe/unsubscribe operations
                    await self._process_pending_ops(ws)

                    # Check for messages with a short timeout
                    try:
                        msg = await asyncio.wait_for(ws.receive(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue

                    if msg.type == 0x1:  # TEXT
                        await self._handle_message(msg.data)
                    elif msg.type == 0x8:  # Close
                        self._log.info("ws_closed", code=ws.close_code)
                        break
                    elif msg.type == 0x9:  # Ping
                        # OKX sends "ping" text, respond with "pong"
                        if msg.data == "ping":
                            await ws.send_str("pong")
                    elif msg.type == 0xA:  # Pong
                        pass
                    elif msg.type == 0x2:  # Binary (unexpected)
                        self._log.warning("ws_unexpected_binary")

            finally:
                self._connection = None

    async def _subscribe_all_channels(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Subscribe to all active channels on the given connection."""
        async with self._lock:
            # Group subscriptions by channel+instId to avoid duplicates
            args = []
            seen = set()
            for sub in self._subscriptions.values():
                key = (sub.channel, sub.inst_id)
                if key not in seen:
                    seen.add(key)
                    args.append({"channel": sub.channel, "instId": sub.inst_id})

        if not args:
            return

        # OKX allows up to 300 args per subscribe message
        for i in range(0, len(args), 300):
            batch = args[i:i + 300]
            payload = json.dumps({
                "op": "subscribe",
                "args": batch,
                "id": str(uuid.uuid4()),
            })
            try:
                await ws.send_str(payload)
                self._log.debug("subscribe_batch_sent", count=len(batch))
            except Exception:
                self._log.exception("subscribe_batch_failed")

    async def _process_pending_ops(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Process pending subscribe/unsubscribe operations."""
        ops = []
        while not self._pending_ops.empty():
            try:
                ops.append(self._pending_ops.get_nowait())
            except asyncio.QueueEmpty:
                break

        if not ops:
            return

        # Group by operation type
        subscribes = []
        unsubscribes = []
        for op in ops:
            if op["op"] == "subscribe":
                subscribes.extend(op["args"])
            elif op["op"] == "unsubscribe":
                unsubscribes.extend(op["args"])

        # Send subscribe batch
        if subscribes:
            # OKX allows up to 300 args per message
            for i in range(0, len(subscribes), 300):
                batch = subscribes[i:i + 300]
                payload = json.dumps({
                    "op": "subscribe",
                    "args": batch,
                    "id": str(uuid.uuid4()),
                })
                try:
                    await ws.send_str(payload)
                    self._log.debug("subscribe_sent", count=len(batch))
                except Exception:
                    self._log.exception("subscribe_send_failed")

        # Send unsubscribe batch
        if unsubscribes:
            for i in range(0, len(unsubscribes), 300):
                batch = unsubscribes[i:i + 300]
                payload = json.dumps({
                    "op": "unsubscribe",
                    "args": batch,
                    "id": str(uuid.uuid4()),
                })
                try:
                    await ws.send_str(payload)
                    self._log.debug("unsubscribe_sent", count=len(batch))
                except Exception:
                    self._log.exception("unsubscribe_send_failed")

    async def _handle_message(self, raw: str) -> None:
        """Parse a JSON message and dispatch to registered handlers.

        OKX V5 messages have the format:
        {
            "arg": {"channel": "...", "instId": "..."},
            "action": "subscribe"|"unsubscribe"|"update",
            "data": [...],
            "ts": "..."
        }
        """
        try:
            parsed: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError:
            self._log.warning(
                "ws_invalid_json",
                raw_preview=raw[:200],
            )
            return

        # Handle pong response
        if raw == "pong":
            return

        # Handle subscribe/unsubscribe confirmation
        action = parsed.get("action")
        if action in ("subscribe", "unsubscribe"):
            arg = parsed.get("arg", {})
            self._log.debug(
                "ws_action_confirmed",
                action=action,
                channel=arg.get("channel"),
                inst_id=arg.get("instId"),
            )
            return

        # Handle error responses
        if "errorCode" in parsed:
            self._log.error(
                "ws_error",
                error_code=parsed["errorCode"],
                error_msg=parsed.get("errorMsg", ""),
            )
            return

        # Handle data messages
        arg = parsed.get("arg", {})
        channel = arg.get("channel", "")
        inst_id = arg.get("instId", "")
        data = parsed.get("data", [])

        if not channel:
            self._log.debug("ws_no_channel", raw_preview=raw[:200])
            return

        # Find matching subscriptions
        async with self._lock:
            matching_subs = [
                sub for sub in self._subscriptions.values()
                if sub.channel == channel
                and (sub.inst_id == "*" or sub.inst_id == inst_id)
                and sub.status == "active"
            ]

        now = time.time()
        for sub in matching_subs:
            try:
                # Create a message dict with the standard OKX V5 format
                message = {
                    "arg": arg,
                    "action": action or "update",
                    "data": data,
                    "ts": parsed.get("ts", ""),
                }
                await sub.handler(message)
                sub.last_message_at = now
            except Exception:
                self._log.exception(
                    "handler_error",
                    channel=channel,
                    inst_id=inst_id,
                    sub_id=sub.id,
                )
