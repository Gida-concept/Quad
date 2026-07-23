"""Centralized WebSocket connection manager for market data streams.

Provides ``WebSocketManager`` that manages subscriptions to named streams,
handles automatic reconnection with exponential backoff, and routes incoming
messages to registered callbacks.

Uses ``aiohttp`` for WebSocket connections.
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

import structlog

if TYPE_CHECKING:
    import aiohttp
    from quad.exchange.base import ExchangeAdapter

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default combined-stream WebSocket URL for Binance Options
_DEFAULT_WS_COMBINED_URL = "wss://fstream.binance.com/market/stream"

# Reconnection backoff parameters
_BASE_BACKOFF_S = 1.0
_MAX_BACKOFF_S = 30.0
_BACKOFF_MULTIPLIER = 2.0
_JITTER_FRACTION = 0.1

# How long to wait between health-check pings on idle connections
_HEARTBEAT_INTERVAL_S = 30.0


# ---------------------------------------------------------------------------
# Subscription dataclass
# ---------------------------------------------------------------------------


@dataclass
class _Subscription:
    """Internal record for a single stream subscription."""

    id: str
    """Unique subscription identifier (uuid4)."""

    stream_name: str
    """Name of the stream (e.g. ``"BTCUSDT@optionMarkPrice"``)."""

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
    """Manages multiple WebSocket subscriptions to market data streams.

    * Accepts a list of stream names to subscribe to.
    * Handles reconnection with exponential backoff + jitter.
    * Routes received messages to registered handlers by stream name.
    * Supports combined streams (multiple streams in one connection).

    Usage::

        mgr = WebSocketManager(exchange_adapter)
        await mgr.start()
        sub_id = await mgr.subscribe("BTCUSDT@optionMarkPrice", my_handler)
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

            * ``ws_combined_url`` — Override the combined-stream WebSocket URL.
              Defaults to ``wss://fstream.binance.com/market/stream``.
            * ``ws_heartbeat_interval`` — Seconds between keepalive pings.
        """
        self._exchange = exchange_adapter
        self._config = config or {}

        # Combined-stream WebSocket endpoint
        self._ws_url = self._config.get(
            "ws_combined_url",
            _DEFAULT_WS_COMBINED_URL,
        )

        self._log = logger.bind(ws_url=self._ws_url)

        # stream_name -> list of _Subscription
        self._stream_handlers: dict[str, list[_Subscription]] = {}

        # stream_name -> asyncio.Task for the connection runner
        self._tasks: dict[str, asyncio.Task[None]] = {}

        # stream_name -> aiohttp.ClientWebSocketResponse
        self._connections: dict[str, aiohttp.ClientWebSocketResponse] = {}

        # Shared aiohttp session (created once in start())
        self._session: aiohttp.ClientSession | None = None

        self._running = False
        self._lock = asyncio.Lock()
        self._sub_id_map: dict[str, str] = {}  # subscription_id -> stream_name

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Begin processing all active subscriptions.

        Creates the shared HTTP session and starts a connection task for
        every currently registered stream.
        """
        if self._running:
            self._log.warning("already_running")
            return

        import aiohttp

        self._running = True
        self._session = aiohttp.ClientSession()
        self._log.info("ws_manager_started")

        # Start background tasks for existing subscriptions
        async with self._lock:
            for stream_name in list(self._stream_handlers.keys()):
                self._tasks[stream_name] = asyncio.create_task(
                    self._run_stream_connection(stream_name),
                )

    async def stop(self) -> None:
        """Gracefully stop all connections and cancel background tasks.

        Closes all WebSocket connections, cancels connection tasks, and
        closes the shared HTTP session.
        """
        if not self._running:
            return

        self._log.info("ws_manager_stopping")
        self._running = False

        async with self._lock:
            # Close all WebSocket connections
            for stream_name, ws in list(self._connections.items()):
                try:
                    await ws.close()
                except Exception:
                    self._log.exception(
                        "ws_close_error",
                        stream=stream_name,
                    )
            self._connections.clear()

            # Cancel all background tasks
            for stream_name, task in list(self._tasks.items()):
                task.cancel()
            self._tasks.clear()

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
        stream_name: str,
        handler: Callable[[dict], Awaitable[None]],
    ) -> str:
        """Subscribe to a stream and register a callback.

        Parameters
        ----------
        stream_name:
            The stream to subscribe to (e.g. ``"BTCUSDT@optionMarkPrice"``).
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
            stream_name=stream_name,
            handler=handler,
        )

        async with self._lock:
            if stream_name not in self._stream_handlers:
                self._stream_handlers[stream_name] = []

            self._stream_handlers[stream_name].append(sub)
            self._sub_id_map[sub_id] = stream_name

            # If the manager is already running and this is the first
            # subscription for this stream, start a connection task.
            if (
                self._running
                and stream_name not in self._tasks
            ):
                self._tasks[stream_name] = asyncio.create_task(
                    self._run_stream_connection(stream_name),
                )
            # If a connection already exists, send a SUBSCRIBE message
            elif self._running and stream_name in self._connections:
                await self._send_subscribe(stream_name)

        self._log.debug(
            "subscribed",
            stream=stream_name,
            sub_id=sub_id,
            total_subs=len(self._stream_handlers[stream_name]),
        )
        return sub_id

    async def unsubscribe(self, subscription_id: str) -> bool:
        """Unsubscribe from a stream by subscription ID.

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
            stream_name = self._sub_id_map.pop(subscription_id, None)
            if stream_name is None:
                return False

            subs = self._stream_handlers.get(stream_name, [])
            before = len(subs)
            self._stream_handlers[stream_name] = [
                s for s in subs if s.id != subscription_id
            ]
            removed = before - len(self._stream_handlers[stream_name])

            # If no more handlers remain for this stream, tear down
            if len(self._stream_handlers[stream_name]) == 0:
                del self._stream_handlers[stream_name]
                await self._stop_stream_connection(stream_name)

        self._log.debug(
            "unsubscribed",
            sub_id=subscription_id,
            stream=stream_name,
        )
        return removed > 0

    async def resubscribe_all(self) -> None:
        """Reconnect all active subscriptions.

        Closes all existing connections and re-establishes them.  Useful
        after a complete connection loss.
        """
        async with self._lock:
            # Close existing connections
            for stream_name in list(self._connections.keys()):
                ws = self._connections.get(stream_name)
                if ws is not None:
                    try:
                        await ws.close()
                    except Exception:
                        pass

            # Cancel existing tasks
            for stream_name in list(self._tasks.keys()):
                self._tasks[stream_name].cancel()

            self._connections.clear()
            self._tasks.clear()

            # Restart connection tasks
            for stream_name in list(self._stream_handlers.keys()):
                # Reset reconnect counts
                for sub in self._stream_handlers[stream_name]:
                    sub.reconnect_count = 0

                self._tasks[stream_name] = asyncio.create_task(
                    self._run_stream_connection(stream_name),
                )

        self._log.info("resubscribed_all")

    # ------------------------------------------------------------------
    # Health / status
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Return current connection status for all streams.

        Returns
        -------
        dict
            Keys:
            * ``active_subscriptions`` — total number of subscription slots.
            * ``streams_active`` — number of distinct streams.
            * ``reconnect_counts`` — mapping of stream_name -> total reconnects.
            * ``last_message_times`` — mapping of stream_name -> last message
              timestamp (epoch seconds, or 0 if no message yet).
        """
        reconnect_counts: dict[str, int] = {}
        last_message_times: dict[str, float] = {}
        active_count = 0

        for stream_name, subs in self._stream_handlers.items():
            reconnect_counts[stream_name] = sum(
                s.reconnect_count for s in subs
            )
            last_message_times[stream_name] = max(
                s.last_message_at for s in subs
            ) if subs else 0.0
            active_count += len(subs)

        return {
            "active_subscriptions": active_count,
            "streams_active": len(self._stream_handlers),
            "reconnect_counts": reconnect_counts,
            "last_message_times": last_message_times,
        }

    # ------------------------------------------------------------------
    # Internal: connection runner
    # ------------------------------------------------------------------

    async def _run_stream_connection(self, stream_name: str) -> None:
        """Background task that maintains one WebSocket connection.

        Connects to the combined-stream endpoint, subscribes to
        *stream_name*, reads messages, and dispatches them to registered
        handlers.  Reconnects automatically on failure with exponential
        backoff, unless the subscription has been removed.
        """
        backoff = _BASE_BACKOFF_S

        while self._running:
            # Check whether the subscription still exists
            async with self._lock:
                if stream_name not in self._stream_handlers:
                    self._log.debug(
                        "stream_no_longer_subscribed",
                        stream=stream_name,
                    )
                    self._tasks.pop(stream_name, None)
                    return

            try:
                await self._connect_and_read(stream_name)
                # Connection closed cleanly --- reset backoff
                backoff = _BASE_BACKOFF_S
            except asyncio.CancelledError:
                self._log.debug(
                    "ws_task_cancelled",
                    stream=stream_name,
                )
                raise
            except Exception:
                self._log.exception(
                    "ws_connection_error",
                    stream=stream_name,
                    backoff_s=round(backoff, 2),
                )

            if not self._running:
                break

            # Update reconnect counts
            async with self._lock:
                subs = self._stream_handlers.get(stream_name, [])
                for sub in subs:
                    sub.reconnect_count += 1

            # Exponential backoff with jitter
            jitter = random.uniform(0, backoff * _JITTER_FRACTION)
            await asyncio.sleep(backoff + jitter)
            backoff = min(
                backoff * _BACKOFF_MULTIPLIER,
                _MAX_BACKOFF_S,
            )

        self._tasks.pop(stream_name, None)

    async def _connect_and_read(self, stream_name: str) -> None:
        """Connect to the combined stream and read messages.

        Opens a WebSocket to ``self._ws_url``, subscribes to
        *stream_name*, and forwards every incoming message to registered
        handlers until the connection is closed or cancelled.
        """
        session = self._session
        if session is None:
            raise RuntimeError("WebSocketManager not started")

        async with session.ws_connect(
            self._ws_url,
            heartbeat=_HEARTBEAT_INTERVAL_S,
        ) as ws:
            # Store the connection so we can close it later
            async with self._lock:
                self._connections[stream_name] = ws

            self._log.info(
                "ws_connected",
                stream=stream_name,
            )

            # Subscribe to the stream
            await self._send_subscribe(stream_name)

            try:
                async for msg in ws:
                    if not self._running:
                        break

                    if msg.type == 0x1:  # aiohttp.WSMsgType.TEXT
                        await self._handle_message(stream_name, msg.data)
                    elif msg.type == 0x8:  # Close
                        self._log.info(
                            "ws_closed",
                            stream=stream_name,
                            code=ws.close_code,
                        )
                        break
                    elif msg.type == 0x9:  # Ping
                        await ws.pong()
                    elif msg.type == 0xA:  # Pong
                        pass
                    elif msg.type == 0x2:  # Binary (unexpected)
                        self._log.warning(
                            "ws_unexpected_binary",
                            stream=stream_name,
                        )

            finally:
                async with self._lock:
                    if self._connections.get(stream_name) is ws:
                        del self._connections[stream_name]

    async def _send_subscribe(self, stream_name: str) -> None:
        """Send a SUBSCRIBE message on the connection for *stream_name*."""
        ws = self._connections.get(stream_name)
        if ws is None:
            self._log.warning(
                "cannot_subscribe_no_connection",
                stream=stream_name,
            )
            return

        payload = json.dumps({
            "method": "SUBSCRIBE",
            "params": [stream_name],
            "id": str(uuid.uuid4()),
        })
        try:
            await ws.send_str(payload)
            self._log.debug(
                "subscribe_sent",
                stream=stream_name,
            )
        except Exception:
            self._log.exception(
                "subscribe_send_failed",
                stream=stream_name,
            )

    async def _send_unsubscribe(self, stream_name: str) -> None:
        """Send an UNSUBSCRIBE message on the connection for *stream_name*."""
        ws = self._connections.get(stream_name)
        if ws is None:
            return

        payload = json.dumps({
            "method": "UNSUBSCRIBE",
            "params": [stream_name],
            "id": str(uuid.uuid4()),
        })
        try:
            await ws.send_str(payload)
        except Exception:
            # Best-effort; the connection may already be gone.
            pass

    async def _stop_stream_connection(self, stream_name: str) -> None:
        """Teardown a stream connection when no more subscriptions exist."""
        # Send unsubscribe
        await self._send_unsubscribe(stream_name)

        # Close WebSocket
        ws = self._connections.pop(stream_name, None)
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

        # Cancel background task
        task = self._tasks.pop(stream_name, None)
        if task is not None and not task.done():
            task.cancel()

        self._log.debug(
            "stream_connection_stopped",
            stream=stream_name,
        )

    async def _handle_message(
        self,
        stream_name: str,
        raw: str,
    ) -> None:
        """Parse a JSON message and dispatch to registered handlers.

        Combined stream responses are wrapped:
        ``{"stream": "...", "data": {...}}``

        This method extracts the data payload and routes it based on the
        ``stream`` field.  If the message lacks a ``stream`` wrapper, it
        is dispatched to all handlers for *stream_name*.
        """
        try:
            parsed: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError:
            self._log.warning(
                "ws_invalid_json",
                stream=stream_name,
                raw_preview=raw[:200],
            )
            return

        # Determine the actual stream origin
        actual_stream: str | None = parsed.get("stream")
        data: dict[str, Any] | None = parsed.get("data")

        if actual_stream and data is not None:
            # Combined stream wrapper
            message = data
            origin_stream = actual_stream
        else:
            # Raw / unwrapped message
            message = parsed
            origin_stream = stream_name

        # Dispatch to all handlers registered for the origin stream
        async with self._lock:
            subs = self._stream_handlers.get(origin_stream, [])
            # Snapshot the handler list to avoid iteration issues
            handlers = [(s.id, s.handler) for s in subs if s.status == "active"]

        now = time.time()
        for sub_id, handler in handlers:
            try:
                await handler(message)
                # Update last_message_at
                async with self._lock:
                    for s in self._stream_handlers.get(origin_stream, []):
                        if s.id == sub_id:
                            s.last_message_at = now
                            break
            except Exception:
                self._log.exception(
                    "handler_error",
                    stream=origin_stream,
                    sub_id=sub_id,
                )
