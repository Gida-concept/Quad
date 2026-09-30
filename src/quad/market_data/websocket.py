"""Centralized WebSocket connection manager for Bybit V5 market data streams.

Provides ``WebSocketManager`` that manages subscriptions to Bybit V5 public
topics, handles automatic reconnection with exponential backoff, and routes
incoming messages to registered callbacks.

Logical channel names (stable interface for :class:`MarketDataEngine`) map
to Bybit V5 topics as follows::

    tickers            -> tickers.{symbol}        (last/mark/funding)
    mark-price         -> tickers.{symbol}        (same feed: markPrice + fundingRate)
    books5             -> orderbook.25.{symbol}   (top-25 bids/asks snapshot+delta)
    candle{interval}   -> kline.{bybit}.{symbol}  (1m->1, 5m->5, 15m->15, 1H->60, 4H->240, 1D->D)
    liquidation-orders -> liquidation.{symbol}
    trades             -> publicTrade.{symbol}

Bybit V5 messages have the format::

    {"topic": "tickers.BTCUSDT", "type": "snapshot", "data": {...}, "ts": ...}

Uses ``aiohttp`` for the connection (single multiplexed connection, up to
10 topics per subscribe message).  Application-level ``{"op": "ping"}``
keepalives are sent every ``heartbeat_interval`` seconds (default 20).
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
# Logical channel constants (stable interface — Bybit topics derived below)
# ---------------------------------------------------------------------------

CHANNEL_TICKERS = "tickers"
CHANNEL_MARK_PRICE = "mark-price"
CHANNEL_BOOKS5 = "books5"
CHANNEL_BOOKS = "books"
CHANNEL_CANDLE = "candle"
CHANNEL_LIQUIDATION_ORDERS = "liquidation-orders"
CHANNEL_TRADES = "trades"

# Bybit caps public subscribe messages at 10 topics each.
_MAX_TOPICS_PER_MESSAGE = 10

# Default application-level ping interval (Bybit drops idle connections).
DEFAULT_HEARTBEAT_INTERVAL = 20.0

# Default public-stream endpoint (Bybit V5 linear).
DEFAULT_WS_URL = "wss://stream.bybit.com/v5/public"

# Friendly candle suffix -> Bybit kline interval.
_CANDLE_INTERVAL_MAP = {
    "1m": "1",
    "3m": "3",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "1H": "60",
    "2H": "120",
    "4H": "240",
    "6H": "360",
    "12H": "720",
    "1D": "D",
    "1W": "W",
    "1M": "M",
}


def channel_to_topic(channel: str, symbol: str) -> str:
    """Translate a logical (channel, symbol) pair to a Bybit V5 topic."""
    if channel in (CHANNEL_TICKERS, CHANNEL_MARK_PRICE):
        return f"tickers.{symbol}"
    if channel in (CHANNEL_BOOKS5, CHANNEL_BOOKS):
        return f"orderbook.25.{symbol}"
    if channel.startswith(CHANNEL_CANDLE):
        suffix = channel[len(CHANNEL_CANDLE) :] or "1m"
        interval = _CANDLE_INTERVAL_MAP.get(
            suffix, _CANDLE_INTERVAL_MAP.get(suffix.upper(), "1")
        )
        return f"kline.{interval}.{symbol}"
    if channel == CHANNEL_LIQUIDATION_ORDERS:
        return f"liquidation.{symbol}"
    if channel == CHANNEL_TRADES:
        return f"publicTrade.{symbol}"
    # Unknown channel: pass through as a raw topic prefix.
    return f"{channel}.{symbol}"


def topic_to_channel_symbol(topic: str) -> tuple[str, str]:
    """Best-effort inverse of :func:`channel_to_topic` for dispatch."""
    parts = topic.split(".")
    symbol = parts[-1] if parts else ""
    prefix = ".".join(parts[:-1])
    if prefix == "tickers":
        return CHANNEL_TICKERS, symbol
    if prefix.startswith("orderbook"):
        return CHANNEL_BOOKS5, symbol
    if prefix.startswith("kline"):
        return CHANNEL_CANDLE, symbol
    if prefix == "liquidation":
        return CHANNEL_LIQUIDATION_ORDERS, symbol
    if prefix == "publicTrade":
        return CHANNEL_TRADES, symbol
    return prefix, symbol


# ---------------------------------------------------------------------------
# Subscription dataclass
# ---------------------------------------------------------------------------


@dataclass
class _Subscription:
    """Internal record for a single channel subscription."""

    id: str
    """Unique subscription identifier (uuid4)."""

    channel: str
    """Logical channel name (e.g. ``"tickers"``)."""

    inst_id: str
    """Bybit symbol (e.g. ``"BTCUSDT"``)."""

    handler: Callable[[dict], Awaitable[None]]
    """Async callback invoked with each normalized message."""

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
    """Manages WebSocket subscriptions to Bybit V5 market data topics.

    * Accepts logical channel subscriptions with Bybit symbols.
    * Handles reconnection with exponential backoff + jitter.
    * Routes received messages to registered handlers by topic.
    * Single multiplexed public connection (private account streams live
      in the exchange adapter, not here).

    Usage::

        mgr = WebSocketManager(exchange_adapter)
        await mgr.start()
        sub_id = await mgr.subscribe("tickers", "BTCUSDT", my_handler)
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
              Defaults to ``wss://stream.bybit.com/v5/public``.
            * ``ws_heartbeat_interval`` — Seconds between keepalive pings.
        """
        self._exchange = exchange_adapter
        self._config = config or {}
        # Fail-soft config access: a partial config (or a test double) must
        # not raise KeyError at construction time.  Every lookup below has a
        # working default.
        self._market_data_config = self._config.get("market_data") or {}
        self._ws_config = self._market_data_config.get("websocket") or {}

        # WebSocket endpoint
        self._ws_url = self._ws_config.get("url") or DEFAULT_WS_URL
        self._heartbeat_interval = float(
            self._config.get("ws_heartbeat_interval")
            or self._ws_config.get(
                "heartbeat_interval_seconds", DEFAULT_HEARTBEAT_INTERVAL
            )
        )

        self._log = logger.bind(ws_url=self._ws_url)

        # Subscription management
        self._subscriptions: dict[str, _Subscription] = {}
        # subscription_id -> subscription

        # Connection management (multiplexed: one connection for all topics)
        self._connection: aiohttp.ClientWebSocketResponse[bool] | None = None
        self._connection_task: asyncio.Task[None] | None = None

        # Shared aiohttp session (created once in start())
        self._session: aiohttp.ClientSession | None = None

        self._running = False
        self._lock = asyncio.Lock()
        self._last_pong: float = 0

        # Reconnect limit
        self._reconnect_count = 0
        self._max_reconnects = int(
            self._config.get("market_data", {}).get("ws_max_reconnects", 100)
        )

        # Pending subscribe/unsubscribe operations (Bybit topic strings)
        self._pending_ops: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Begin processing all active subscriptions.

        Creates the shared HTTP session and starts a single multiplexed
        connection task for all topics.
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

    async def _cancel_connection_task(self, timeout: float = 5.0) -> None:
        """Cancel and **await** the connection task.

        A bare ``task.cancel()`` leaves the task pending: shutdown logs
        "Task was destroyed but it is pending", the shared ``ClientSession``
        can be closed underneath an in-flight ``ws_connect``, and
        :meth:`resubscribe_all` can start a second ``_run_connection`` while
        the first is still unwinding (duplicate subscriptions, double
        dispatch).  Awaiting the cancellation makes termination explicit.
        """
        task = self._connection_task
        self._connection_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.CancelledError:
            # The task itself was cancelled — this is the expected outcome.
            if not task.cancelled() and task.exception() is not None:
                self._log.debug(
                    "ws_connection_task_cancelled_with_error",
                    error=str(task.exception()),
                )
        except (TimeoutError, asyncio.TimeoutError):
            self._log.warning(
                "ws_connection_task_cancel_timeout",
                timeout=timeout,
            )
        except Exception as exc:
            self._log.debug("ws_connection_task_cancel_error", error=str(exc))

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

        # Cancel the connection task (and wait for it to actually finish)
        await self._cancel_connection_task()

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
        """Subscribe to a logical channel and register a callback.

        Parameters
        ----------
        channel:
            Logical channel name (``"tickers"``, ``"mark-price"``,
            ``"books5"``, ``"candle1m"``, ...).
        inst_id:
            Bybit symbol (e.g. ``"BTCUSDT"``).
        handler:
            Async callback invoked with each normalized message
            (``{"arg", "action", "data", "ts"}`` with Bybit field names
            inside ``data`` items).

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

        # Queue a subscribe operation (Bybit topic string)
        await self._pending_ops.put(
            {
                "op": "subscribe",
                "args": [channel_to_topic(channel, inst_id)],
            }
        )

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
        await self._pending_ops.put(
            {
                "op": "unsubscribe",
                "args": [channel_to_topic(sub.channel, sub.inst_id)],
            }
        )

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

            # Cancel existing task and WAIT for it to finish, so the old
            # _run_connection cannot overlap with the new one.
            await self._cancel_connection_task()

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
            # With no subscriptions yet there is nothing to read, but the
            # task must stay alive: future subscribe() calls queue pending
            # ops that only this loop drains.  Never return early here.
            async with self._lock:
                has_subs = bool(self._subscriptions)
            if not has_subs:
                self._log.debug("no_subscriptions_waiting")
                await asyncio.sleep(1.0)
                continue

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
            self._reconnect_count += 1
            if self._reconnect_count > self._max_reconnects:
                self._log.critical(
                    "ws_max_reconnects_exceeded", count=self._reconnect_count
                )
                self._running = False
                break

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
        """Connect to Bybit V5 WebSocket and read messages.

        Opens a WebSocket connection, subscribes to all active topics,
        and forwards incoming messages to registered handlers until
        the connection is closed or cancelled.
        """
        session = self._session
        if session is None:
            raise RuntimeError("WebSocketManager not started")

        async with session.ws_connect(self._ws_url) as ws:
            self._connection = ws
            self._log.info("ws_connected", url=self._ws_url)

            # Subscribe to all active topics
            await self._subscribe_all_topics(ws)

            last_ping = time.monotonic()
            last_pong = time.monotonic()

            # Process pending operations and read messages
            try:
                while self._running:
                    # Application-level ping (Bybit drops idle connections)
                    now = time.monotonic()
                    if now - last_ping >= self._heartbeat_interval:
                        try:
                            await ws.send_str(json.dumps({"op": "ping"}))
                        except Exception:
                            self._log.exception("ws_ping_failed")
                            break
                        last_ping = now

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
                    elif msg.type == 0xA:  # Pong
                        last_pong = time.monotonic()
                        self._reconnect_count = 0
                    elif msg.type == 0x2:  # Binary (unexpected)
                        self._log.warning("ws_unexpected_binary")

                    # Dead connection check: no pong received after 3x heartbeat interval
                    if now - last_pong > self._heartbeat_interval * 3:
                        self._log.warning(
                            "ws_dead_connection",
                            since_last_pong=round(now - last_pong, 2),
                        )
                        break

            finally:
                self._connection = None

    async def _send_topic_ops(
        self, ws: aiohttp.ClientWebSocketResponse[bool], op: str, topics: list[str]
    ) -> None:
        """Send (un)subscribe ops, batched to Bybit's per-message topic cap."""
        for i in range(0, len(topics), _MAX_TOPICS_PER_MESSAGE):
            batch = topics[i : i + _MAX_TOPICS_PER_MESSAGE]
            payload = json.dumps({"op": op, "args": batch})
            try:
                await ws.send_str(payload)
                self._log.debug("topic_op_sent", op=op, count=len(batch))
            except Exception:
                self._log.exception("topic_op_failed", op=op)

    async def _subscribe_all_topics(
        self, ws: aiohttp.ClientWebSocketResponse[bool]
    ) -> None:
        """Subscribe to all active topics on the given connection."""
        async with self._lock:
            # Deduplicate topics (mark-price shares the tickers feed)
            topics = sorted(
                {
                    channel_to_topic(sub.channel, sub.inst_id)
                    for sub in self._subscriptions.values()
                }
            )

        if not topics:
            return
        await self._send_topic_ops(ws, "subscribe", topics)
        # The full snapshot above already covers every active topic, so
        # queued subscribe ops for those topics are redundant — drop them
        # (keeping unsubscribes and topics not in the snapshot).
        sent = set(topics)
        kept: list[dict[str, Any]] = []
        while not self._pending_ops.empty():
            try:
                op = self._pending_ops.get_nowait()
            except asyncio.QueueEmpty:
                break
            if op.get("op") == "subscribe":
                remaining = [t for t in op.get("args", []) if t not in sent]
                if remaining:
                    kept.append({"op": "subscribe", "args": remaining})
            else:
                kept.append(op)
        for op in kept:
            self._pending_ops.put_nowait(op)

    async def _process_pending_ops(
        self, ws: aiohttp.ClientWebSocketResponse[bool]
    ) -> None:
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

        if subscribes:
            await self._send_topic_ops(ws, "subscribe", subscribes)
        if unsubscribes:
            await self._send_topic_ops(ws, "unsubscribe", unsubscribes)

    async def _handle_message(self, raw: str) -> None:
        """Parse a Bybit V5 message and dispatch to registered handlers.

        Bybit V5 data messages have the format::

            {"topic": "tickers.BTCUSDT", "type": "snapshot",
             "data": {...} | [...], "ts": ...}

        Control messages (``{"success": ..., "op": "subscribe"}``,
        ``{"op": "pong"}``) are logged and dropped.  Data payloads are
        normalized to ``{"arg", "action", "data", "ts"}`` with ``data``
        always a list of Bybit-field dicts.
        """
        try:
            parsed: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError:
            self._log.warning(
                "ws_invalid_json",
                raw_preview=raw[:200],
            )
            return

        # Control messages
        if parsed.get("op") in ("pong", "ping"):
            return
        if "success" in parsed:
            if not parsed.get("success"):
                self._log.error(
                    "ws_op_failed",
                    op=parsed.get("op"),
                    ret_msg=parsed.get("ret_msg", ""),
                )
            else:
                self._log.debug("ws_op_confirmed", op=parsed.get("op"))
            return

        topic = parsed.get("topic", "")
        if not topic:
            self._log.debug("ws_no_topic", raw_preview=raw[:200])
            return

        channel, symbol = topic_to_channel_symbol(topic)
        data = parsed.get("data", [])
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            return

        # Find matching subscriptions
        async with self._lock:
            matching_subs = [
                sub
                for sub in self._subscriptions.values()
                if sub.inst_id == symbol
                and (
                    sub.channel == channel
                    # mark-price shares the tickers feed
                    or (
                        sub.channel == CHANNEL_MARK_PRICE and channel == CHANNEL_TICKERS
                    )
                )
                and sub.status == "active"
            ]

        now = time.time()
        for sub in matching_subs:
            try:
                message = {
                    "arg": {"channel": sub.channel, "symbol": symbol, "topic": topic},
                    "action": parsed.get("type", "snapshot"),
                    "data": data,
                    "ts": parsed.get("ts", ""),
                }
                await sub.handler(message)
                sub.last_message_at = now
            except Exception:
                self._log.exception(
                    "handler_error",
                    channel=sub.channel,
                    inst_id=symbol,
                    sub_id=sub.id,
                )
