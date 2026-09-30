"""Bybit USDT perpetual futures exchange adapter.

Implements :class:`quad.exchange.base.ExchangeAdapter` for Bybit's V5 unified
API, targeting **USDT perpetual** contracts exclusively.  Perpetual is selected
by the ``category="linear"`` parameter and is hard-coded as a class constant
(``CATEGORY``) so there is no separate "futures vs perpetual" toggle to
misconfigure.

This adapter uses the official ``pybit`` SDK for both REST and WebSocket
transport, which handles V5 request signing, receive-window, and WebSocket
subscription/auto-reconnect.  Order/position/account JSON is translated into
the shared domain dataclasses defined in ``quad.types.domain``; filter
normalization reuses the ABC's ``normalize_quantity`` / ``normalize_price`` /
``get_tick_size`` helpers (with a Bybit-specific ``_get_lot_filters`` override).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import queue
import random
import re
import time
from collections.abc import AsyncGenerator
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

import structlog

from quad.common.retry import exponential_backoff, retry_async

try:  # pragma: no cover - import guard for environments without the SDK
    from pybit.unified_trading import HTTP, WebSocket
except Exception:  # pragma: no cover
    HTTP = None
    WebSocket = None

if TYPE_CHECKING:  # pragma: no cover - typing only, never executed at runtime
    # The guard above leaves HTTP/WebSocket as ``None`` when the SDK is absent,
    # so annotating the client attributes as ``object`` silenced mypy at the
    # cost of losing every attribute check on them.  Importing the real types
    # under TYPE_CHECKING restores precise checking without making the SDK a
    # hard runtime dependency.
    from pybit.unified_trading import HTTP as PybitHTTP
    from pybit.unified_trading import WebSocket as PybitWebSocket

from quad.exchange.base import (
    ExchangeAdapter,
    ExchangeAuthError,
    ExchangeConnectionError,
    ExchangeError,
    ExchangeOrderError,
    ExchangeRateLimitError,
    _ttl_fresh,
    _ttl_store,
)
from quad.types.domain import (
    Account,
    Balance,
    FuturesPositionSide,
    MarginType,
    Order,
    OrderRequest,
    OrderResult,
    Position,
    PositionSide,
    Trade,
)
from quad.types.exchange import AccountUpdate
from quad.types.market import FundingRate

log = structlog.get_logger(__name__)

# Bybit error codes used by the generic error helpers below.
_MARGIN_MODE_ALREADY_SET_CODE = "110043"  # "Margin mode is not modified"
_ORDER_NOT_FOUND_CODES = ("20001", "30003")  # "Order does not exist" / not found
_ORDER_NOT_FOUND_TEXT = "order does not exist"

#: Total REST attempts (initial call + retries) for a single Bybit request.
_MAX_REST_ATTEMPTS = 3


def _normalize_order_status(raw: object) -> str:
    """Map Bybit's mixed-case ``orderStatus`` values to the canonical
    UPPERCASE literals used by execution/gateway.py, reconciler.py, and
    engine.py (``FILLED`` / ``CANCELLED`` / ``REJECTED`` / ``PARTIALLY_FILLED``
    / ``NEW`` / ...).  Without this, terminal statuses never match, filled
    orders are polled forever as ghosts, and trade persistence never runs.
    """
    mapping = {
        "CREATED": "NEW",
        "NEW": "NEW",
        "PARTIALLYFILLED": "PARTIALLY_FILLED",
        "PARTIALLY_FILLED": "PARTIALLY_FILLED",
        "FILLED": "FILLED",
        "CANCELLED": "CANCELLED",
        "REJECTED": "REJECTED",
        "EXPIRED": "EXPIRED",
        "UNTRIGGERED": "NEW",
        "TRIGGERED": "NEW",
        "DEACTIVATED": "CANCELLED",
    }
    key = str(raw or "").replace("_", "").replace(" ", "").upper()
    return mapping.get(key, str(raw or "").upper())


class BybitFuturesAdapter(ExchangeAdapter):
    """Full-featured Bybit USDT-perpetual exchange adapter (V5 API).

    Args:
        api_key: Bybit API key.  May also be set via the ``BYBIT_API_KEY``
            environment variable.
        api_secret: Bybit API secret.  May also be set via the
            ``BYBIT_API_SECRET`` environment variable.
        testnet: If ``True``, use the Bybit testnet
            (``https://api-testnet.bybit.com``).
        rate_limit: Optional dict with ``max_weight`` and ``max_orders`` keys
            to configure rate-limit tracking (kept for interface parity;
            ``pybit`` performs its own internal throttling).
        recv_window: Request validity window in milliseconds (default 5000).
        config: Optional raw config dict so the top-level ``_dry_run``
            flag and per-exchange URL overrides can be read.
    """

    # USDT perpetual.  Every market call passes this constant, so the bot can
    # only ever trade linear perpetuals (never inverse/spot/options).
    CATEGORY = "linear"

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        testnet: bool = False,
        rate_limit: dict | None = None,
        recv_window: int | None = None,
        config: dict | None = None,
    ) -> None:
        self._log = log.bind(adapter="bybit_futures")
        self._api_key: str = api_key or os.environ.get("BYBIT_API_KEY", "")
        self._api_secret: str = api_secret or os.environ.get("BYBIT_API_SECRET", "")
        self._testnet: bool = testnet
        self._config = config or {}
        self._exchange_config = self._config.get("exchange", {}) or {}
        self._bybit_config = self._exchange_config.get("bybit", {}) or {}
        self._recv_window: int = recv_window or int(
            self._bybit_config.get("recv_window", 5000)
        )
        if not 0 < self._recv_window <= 60000:
            # Bybit accepts 0 < recv_window <= 60000; clamp + warn so a
            # misconfigured value cannot silently break request signing.
            self._log.warning(
                "recv_window_out_of_range",
                recv_window=self._recv_window,
            )
            self._recv_window = max(1, min(int(self._recv_window), 60000))
        # Millisecond offset between exchange server time and local wall
        # clock (server - local), measured by get_server_time().  Applied
        # to locally-stamped timestamps via _now_ms().
        self.clock_offset_ms: int = 0
        # Optional HTTP proxy for REST requests.  Reads from the Bybit config
        # section (``exchange.bybit.proxy``) with a fallback to the standard
        # ``HTTP_PROXY`` / ``HTTPS_PROXY`` environment variables.
        self._proxy: str | None = (
            self._bybit_config.get("http_proxy")
            or self._bybit_config.get("proxy")
            or os.environ.get("HTTP_PROXY")
            or os.environ.get("HTTPS_PROXY")
            or None
        )
        # Dry-run hard guard (top-level ``_dry_run`` config key).  When set AND
        # the exchange is live (``testnet=False``), place_order() refuses every
        # order to protect real funds.
        self._dry_run: bool = bool(self._config.get("_dry_run", False))
        # TTL for the exchange-info filter cache (used by normalize_quantity).
        self._exchange_info_ttl: float = float(
            self._bybit_config.get("exchange_info_ttl_seconds", 60)
        )
        # Resolve base URLs (pybit accepts testnet=bool directly, but we keep
        # the resolved values for diagnostics / logging).
        self._rest_base: str = (
            self._bybit_config.get("testnet_base_url", "https://api-testnet.bybit.com")
            if testnet
            else self._bybit_config.get("base_url", "https://api.bybit.com")
        )
        # pybit 5.17's HTTP client takes no endpoint/base_url parameter — it
        # builds its URL from testnet/domain/tld/demo kwargs — so a custom
        # base_url cannot be passed through and is ignored (warn, don't fail).
        _custom_base = self._bybit_config.get(
            "testnet_base_url" if testnet else "base_url"
        )
        if _custom_base:
            self._log.warning(
                "custom_base_url_ignored_by_pybit",
                base_url=_custom_base,
                reason="pybit HTTP supports testnet/domain/tld only",
            )
        rl = rate_limit or {}
        self._max_weight: int = int(rl.get("max_weight") or 0)
        self._max_orders: int = int(rl.get("max_orders") or 0)
        self._client: PybitHTTP | None = None
        self._ws: PybitWebSocket | None = None
        self._ws_task: asyncio.Task[None] | None = None
        # Subscription state for account-update generator.
        self._account_queue: asyncio.Queue = asyncio.Queue()
        # Thread-safe inbox for raw private-WS payloads.  pybit invokes its
        # WS callbacks on its own IO thread, so the callback only ever does
        # a lock-free put here; the event loop drains it and performs the
        # REST refresh (see subscribe_account_updates).
        self._ws_inbox: queue.Queue = queue.Queue()
        self._stop_event: asyncio.Event = asyncio.Event()
        self._connected: bool = False
        # Per-symbol exchange-info cache (overrides ABC _get_lot_filters).
        self._exchange_info_cache: dict = {}
        # Process-local REST self-throttle.  Bybit enforces an IP-wide
        # 600 req/5s budget shared by every worker on this host; per-UID
        # write limits are ~10/s.  Worker cadence (hourly AI cycles, 60s
        # reconcile) is far below that, but a shared floor keeps bursts
        # (reconnect storms, bracket pairs) from stacking across tasks.
        self._max_rps: float = float(self._bybit_config.get("max_rps", 5.0))
        self._rl_lock: asyncio.Lock = asyncio.Lock()
        self._rl_last: float = 0.0

    # ======================================================================
    # Lifecycle
    # ======================================================================
    async def connect(self) -> None:
        """Create the pybit HTTP (and lazy WebSocket) clients and verify auth."""
        if self._connected:
            return
        self._stop_event.clear()
        if HTTP is None:
            raise ExchangeConnectionError("pybit SDK is not installed")
        self._client = HTTP(
            testnet=self._testnet,
            api_key=self._api_key,
            api_secret=self._api_secret,
            recv_window=self._recv_window,
        )
        # Route REST requests through a proxy if configured.  pybit stores the
        # underlying ``requests.Session()`` as ``self.client`` (not ``session``),
        # so we set its proxies dict after construction.
        if self._proxy and HTTP is not None:
            try:
                self._client.client.proxies = {
                    "http": self._proxy,
                    "https": self._proxy,
                }
                self._log.debug("proxy_configured_http", proxy=self._proxy)
            except AttributeError:
                # pybit internal layout changed — fail open (no proxy)
                self._log.warning("proxy_config_failed", proxy=self._proxy)
        # Verify connectivity / credentials by hitting the public server time.
        try:
            await self.get_server_time()
        except Exception as exc:  # noqa: BLE001 broad guard on connect
            self._client = None
            raise ExchangeConnectionError(f"Failed to connect to Bybit: {exc}") from exc
        # Fail fast on bad credentials with a cheap authenticated call.
        # get_server_time() is public and cannot detect a wrong key/secret.
        try:
            client = self._require_client()
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: client.get_api_key_information(),
            )
        except Exception as exc:  # noqa: BLE001 broad guard on connect
            norm = self._normalize_error(exc)
            if isinstance(norm, ExchangeAuthError):
                self._client = None
                raise norm from exc
            # Non-auth failure (hiccup after a good time check): warn and
            # continue; the time check already proved connectivity.
            self._log.warning("api_key_check_failed", error=str(exc)[:200])
        try:
            self._log.info(
                "bybit_futures_connected",
                testnet=self._testnet,
                rest_base=self._rest_base,
            )
        except Exception:  # noqa: BLE001, S110 logging must not break connect
            pass
        self._connected = True

    async def disconnect(self) -> None:
        """Close the WebSocket (if any) and drop client references."""
        self._stop_event.set()
        if self._ws_task is not None:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):  # noqa: S110 best-effort
                pass
            self._ws_task = None
        self._ws = None
        self._client = None
        self._connected = False
        self._log.info("bybit_futures_disconnected")

    @property
    def is_connected(self) -> bool:
        """Whether the pybit HTTP client is initialised."""
        return self._connected and self._client is not None

    @property
    def is_testnet(self) -> bool:
        """Whether this adapter targets the Bybit testnet."""
        return self._testnet

    @property
    def public_ws_url(self) -> str:
        """Public WebSocket URL (testnet-aware)."""
        if self._testnet:
            return "wss://stream-testnet.bybit.com/v5/public"
        return "wss://stream.bybit.com/v5/public"

    @property
    def private_ws_url(self) -> str:
        """Private WebSocket URL (testnet-aware)."""
        if self._testnet:
            return "wss://stream-testnet.bybit.com/v5/private"
        return "wss://stream.bybit.com/v5/private"

    # ======================================================================
    # Internal — REST helpers
    # ======================================================================
    def _require_client(self):
        if self._client is None:
            raise ExchangeConnectionError("Bybit HTTP client is not initialised")
        return self._client

    def _now_ms(self) -> int:
        """Local wall-clock in ms, corrected by the measured server offset."""
        return int(time.time() * 1000) + int(self.clock_offset_ms)

    def _account_fingerprint(self) -> str:
        """Return a stable, non-reversible id for the configured account.

        Derived from a salted digest of the API key: distinct accounts get
        distinct ids, but the id reveals nothing about the key.  (A raw
        key prefix previously leaked 8 characters of the live secret into
        every log line, status message and persisted row.)
        """
        if not self._api_key:
            return "bybit"
        digest = hashlib.sha256(f"quad-account:{self._api_key}".encode()).hexdigest()
        return f"bybit-{digest[:12]}"

    def _normalize_error(self, exc: Exception) -> Exception:
        """Map a pybit error into a domain ExchangeError where possible."""
        text = str(exc)
        lower = text.lower()
        # Rate limit: Bybit retCode 10006 "Too many visits" (+ HTTP 429/403
        # wording from proxies).  Classified as retryable: _get/_post back
        # off with jitter (honouring any Retry-After) and retry up to 3 tries.
        if (
            "10006" in text
            or "too many visits" in lower
            or "too many requests" in lower
            or "rate limit" in lower
            or "retry-after" in lower
            or re.search(r"\b429\b", text) is not None
        ):
            return ExchangeRateLimitError(text)
        # Auth: bad key/secret, timestamp, or permissions.
        if any(c in text for c in ("10003", "10004", "33004", "34071")):
            return ExchangeAuthError(text)
        if "invalid api" in lower or "api key" in lower and "invalid" in lower:
            return ExchangeAuthError(text)
        if _ORDER_NOT_FOUND_CODES and any(c in text for c in _ORDER_NOT_FOUND_CODES):
            return ExchangeOrderError(text)
        if _ORDER_NOT_FOUND_TEXT in lower:
            return ExchangeOrderError(text)
        return ExchangeError(text)

    async def _throttle(self) -> None:
        """Space REST calls to at most ``max_rps`` per second."""
        if self._max_rps <= 0:
            return
        async with self._rl_lock:
            now = time.monotonic()
            wait = (1.0 / self._max_rps) - (now - self._rl_last)
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._rl_last = now

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        """Whether a (normalized) error is worth retrying with backoff."""
        if isinstance(
            exc,
            (
                ExchangeRateLimitError,
                asyncio.TimeoutError,
                TimeoutError,
                ConnectionError,
            ),
        ):
            return True
        text = str(exc).lower()
        return (
            "429" in text
            or "retry-after" in text
            or "rate limit" in text
            or "too many" in text
            or "timeout" in text
            or "temporarily" in text
        )

    @staticmethod
    def _retry_delay(attempt: int, exc: Exception) -> float:
        """Jittered backoff for attempt N (1-based), honouring Retry-After."""
        match = re.search(r"retry-after[:\s]+(\d+)", str(exc), flags=re.IGNORECASE)
        if match:
            try:
                # The server told us how long to wait; trust it (capped).
                return min(float(match.group(1)), 60.0) + random.uniform(0, 0.5)
            except ValueError:
                pass
        return exponential_backoff(attempt, 0.5, jitter=0.5)

    async def _request(self, method: str, endpoint: str, params: dict | None) -> dict:
        """Raw REST call with throttle + Retry-After/429-aware retries (max 3)."""
        client = self._require_client()

        async def _call(_attempt: int) -> dict:
            try:
                # pybit v5 uses _submit_request() for raw API calls.
                # The path must be fully qualified with the base endpoint.
                full_path = f"{client.endpoint}{endpoint}"
                resp = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: client._submit_request(
                        method=method,
                        path=full_path,
                        query=params or {},
                        auth=True,
                    ),
                )
                return self._unwrap(resp)
            except Exception as exc:  # noqa: BLE001
                # Normalise here so the retry policy below sees the mapped
                # error type (auth/rate-limit/connection/...), not the raw one.
                raise self._normalize_error(exc) from exc

        def _log_retry(attempt: int, exc: Exception, delay: float) -> None:
            self._log.warning(
                "bybit_rest_retry",
                method=method,
                endpoint=endpoint,
                attempt=attempt,
                delay_s=round(delay, 2),
                error=str(exc)[:200],
            )

        return await retry_async(
            _call,
            attempts=_MAX_REST_ATTEMPTS,
            is_retryable=self._is_retryable,
            delay_for=self._retry_delay,
            before_attempt=lambda _attempt: self._throttle(),
            on_retry=_log_retry,
        )

    async def _get(self, endpoint: str, params: dict | None = None) -> dict:
        return await self._request("GET", endpoint, params)

    async def _post(self, endpoint: str, params: dict | None = None) -> dict:
        return await self._request("POST", endpoint, params)

    @staticmethod
    def _unwrap(resp: dict) -> dict:
        """Extract the ``result`` payload from a Bybit V5 envelope.

        Bybit wraps every response as ``{"retCode": 0, "retMsg": "OK",
        "result": {...}, "time": ...}``.  A non-zero ``retCode`` is an error.
        """
        if not isinstance(resp, dict):
            return resp
        ret_code = resp.get("retCode")
        if ret_code not in (0, None):
            msg = resp.get("retMsg", "Bybit error")
            # 10006 "Too many visits" is a quota error: raise the retryable
            # type so _request() backs off (Retry-After-aware) and retries.
            if str(ret_code) == "10006" or "too many" in str(msg).lower():
                raise ExchangeRateLimitError(f"Bybit {ret_code}: {msg}")
            raise ExchangeOrderError(f"Bybit {ret_code}: {msg}")
        result = resp.get("result")
        return result if isinstance(result, dict) else resp

    # ======================================================================
    # REST — Account & Positions
    # ======================================================================
    async def get_account(self) -> Account:
        """Fetch the unified trading account and map it to ``Account``."""
        data = await self._get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        if data is None:
            data = {}
        lists = data.get("list", [{}])
        acct = lists[0] if lists else {}
        total_equity = Decimal(str(acct.get("totalEquity", "0") or "0"))
        total_wallet = Decimal(str(acct.get("totalWalletBalance", "0") or "0"))
        total_margin = Decimal(str(acct.get("totalMarginBalance", "0") or "0"))
        available = Decimal(str(acct.get("totalAvailableBalance", "0") or "0"))
        balances: dict[str, Balance] = {}
        for coin in acct.get("coin", []) or []:
            asset = coin.get("coin", "")
            if not asset:
                continue
            free = Decimal(str(coin.get("availableToWithdraw", "0") or "0"))
            locked = Decimal(str(coin.get("locked", "0") or "0"))
            balances[asset] = Balance(asset=asset, free=free, locked=locked)
        return Account(
            # Non-secret fingerprint of the key.  Never embed key material
            # (or a key prefix) in an identifier: Account.id flows into
            # structlog fields, /status, /balance output and persisted rows.
            id=self._account_fingerprint(),
            exchange="bybit",
            balances=balances,
            total_usdt=total_equity.quantize(Decimal("0.01")),
            timestamp=self._now_ms(),
            max_leverage=1,
            total_wallet_balance=total_wallet,
            total_margin_balance=total_margin,
            available_balance=available,
        )

    async def get_positions(self) -> list[Position]:
        """Fetch all open USDT-perpetual positions."""
        data = await self._get(
            "/v5/position/list",
            {"category": self.CATEGORY, "settleCoin": "USDT"},
        )
        positions: list[Position] = []
        for entry in (data or {}).get("list", []) if isinstance(data, dict) else []:
            pos = self._parse_position(entry)
            if pos is not None:
                positions.append(pos)
        return positions

    def _parse_position(self, entry: dict) -> Position | None:
        symbol = entry.get("symbol", "")
        if not symbol:
            return None
        size = Decimal(str(entry.get("size", "0") or "0"))
        # The entry's own side field is authoritative (Bybit sends
        # side=Buy/Sell per leg); fall back to the size sign only when it
        # is absent.  Zero-size legs carry no exposure — skip them.
        side_raw = str(entry.get("side", "") or "")
        if size == Decimal(0):
            return None
        if side_raw == "Buy":
            side = PositionSide.LONG
        elif side_raw == "Sell":
            side = PositionSide.SHORT
        else:
            side = PositionSide.LONG if size > 0 else PositionSide.SHORT
        pos_side_raw = entry.get("positionIdx", 0)
        try:
            pos_idx = int(pos_side_raw)
        except (ValueError, TypeError):
            pos_idx = 0
        # Hedge-mode legs: 1 = LONG leg, 2 = SHORT leg; 0 = one-way (BOTH).
        fut_side = (
            FuturesPositionSide.LONG
            if pos_idx == 1
            else FuturesPositionSide.SHORT
            if pos_idx == 2
            else FuturesPositionSide.BOTH
        )
        try:
            leverage = int(float(entry.get("leverage", 1) or 1))
        except (ValueError, TypeError, InvalidOperation):
            leverage = 1
        margin_type = (
            MarginType.ISOLATED
            if str(entry.get("isIsolated", "false")).lower() == "true"
            else MarginType.CROSS
        )
        return Position(
            symbol=symbol,
            side=side,
            quantity=abs(size),
            entry_price=Decimal(str(entry.get("avgPrice", "0") or "0")),
            current_price=Decimal(str(entry.get("markPrice", "0") or "0")),
            unrealized_pnl=Decimal(str(entry.get("unrealisedPnl", "0") or "0")),
            realized_pnl=Decimal(str(entry.get("realisedPnl", "0") or "0")),
            leverage=leverage,
            margin_type=margin_type,
            position_side=fut_side,
            liquidation_price=Decimal(str(entry.get("liqPrice", "0") or "0")),
            updated_at=int(entry.get("updatedTime", 0) or 0),
        )

    async def get_user_trades(
        self, symbol: str | None = None, limit: int = 500
    ) -> list[Trade]:
        """Fetch executed fills from ``/v5/execution/list``."""
        symbols = (
            [symbol]
            if symbol
            else [getattr(p, "symbol", "") for p in (await self.get_positions() or [])]
        )
        symbols = [s for s in symbols if s]
        if not symbols:
            return []
        trades: list[Trade] = []
        for sym in symbols:
            try:
                data = await self._get(
                    "/v5/execution/list",
                    {
                        "category": self.CATEGORY,
                        "symbol": sym,
                        "limit": int(limit),
                    },
                )
            except ExchangeError:
                continue
            for entry in (data or {}).get("list", []) if isinstance(data, dict) else []:
                try:
                    trades.append(
                        Trade(
                            order_id=entry.get("orderId", 0) or 0,
                            symbol=sym,
                            side=str(entry.get("side", "")).upper(),
                            quantity=Decimal(str(entry.get("execQty", "0") or "0")),
                            price=Decimal(str(entry.get("execPrice", "0") or "0")),
                            fee=Decimal(str(entry.get("execFee", "0") or "0")),
                            # Bybit's /v5/execution/list returns ``realizedPnl``
                            # per fill — the exchange's own realized PnL for
                            # that leg.  We take it directly from the trade so
                            # the journal and Telegram alerts never compute a
                            # stale/mock PnL from mark-price fallbacks.
                            pnl=Decimal(str(entry.get("realizedPnl", "0") or "0")),
                            timestamp=int(entry.get("execTime", 0) or 0),
                        )
                    )
                except (TypeError, ValueError, InvalidOperation):
                    continue
        return trades

    # ======================================================================
    # REST — Futures Market Data
    # ======================================================================
    async def get_funding_rate(self, symbol: str) -> FundingRate:
        """Fetch the latest funding rate / mark / index prices for a symbol."""
        data = await self._get(
            "/v5/market/tickers",
            {"category": self.CATEGORY, "symbol": symbol},
        )
        entry = ((data or {}).get("list", [{}]) or [{}])[0]
        return FundingRate(
            symbol=entry.get("symbol", symbol),
            funding_rate=Decimal(str(entry.get("fundingRate", "0") or "0")),
            next_funding_time=int(entry.get("nextFundingTime", 0) or 0),
            mark_price=Decimal(str(entry.get("markPrice", "0") or "0")),
            index_price=Decimal(str(entry.get("indexPrice", "0") or "0")),
        )

    async def get_mark_price(self, symbol: str) -> Decimal:
        """Fetch the current mark price for a symbol."""
        data = await self._get(
            "/v5/market/tickers",
            {"category": self.CATEGORY, "symbol": symbol},
        )
        entry = ((data or {}).get("list", [{}]) or [{}])[0]
        return Decimal(str(entry.get("markPrice", "0") or "0"))

    async def get_klines(
        self, symbol: str, interval: str, limit: int = 500
    ) -> list[tuple[float, ...]]:
        """Fetch kline/candlestick data (open_time_s, o, h, l, c, v)."""
        data = await self._get(
            "/v5/market/kline",
            {
                "category": self.CATEGORY,
                "symbol": symbol,
                "interval": interval,
                "limit": int(limit),
            },
        )
        results: list[tuple[float, ...]] = []
        for k in (data or {}).get("list", []) if isinstance(data, dict) else []:
            # Bybit kline tuple: [startTime, open, high, low, close, volume, ...]
            try:
                results.append(
                    (
                        float(k[0]) / 1000.0,
                        float(k[1]),
                        float(k[2]),
                        float(k[3]),
                        float(k[4]),
                        float(k[5]),
                    )
                )
            except (IndexError, TypeError, ValueError):
                continue
        # Bybit /v5/market/kline returns rows NEWEST-first; every consumer
        # (ta.py indicators, strategies) assumes oldest-first.  Sort by
        # open_time ascending so closes[-1] is the most recent candle.
        results.sort(key=lambda t: t[0])
        return results

    # ======================================================================
    # REST — Order Management
    # ======================================================================
    async def place_order(self, request: OrderRequest) -> OrderResult:
        """Place an order on Bybit USDT perpetual.

        Calls ``POST /v5/order/create`` with ``category=linear``.

        Raises:
            RuntimeError: If the hard dry-run guard blocks a live order, or the
                quantity fails exchange filter validation.
            ExchangeOrderError: If the order is rejected by Bybit.
        """
        # 0. Hard dry-run guard — refuse every order when dry-run mode is
        #    enabled but the exchange is LIVE.
        if self._dry_run and not self._testnet:
            self._log.critical(
                "dry_run_guard_blocked_order",
                symbol=request.symbol,
                side=request.side,
                order_type=request.order_type,
                qty=str(request.quantity),
                dry_run=self._dry_run,
                testnet=self._testnet,
            )
            raise RuntimeError(
                "DRY_RUN_GUARD: dry-run mode is enabled but the exchange is "
                "LIVE (testnet=False). Refusing to place the order to protect "
                "real funds."
            )
        # 0b. Normalize quantity to the exchange's LOT_SIZE / MIN_NOTIONAL.
        quantity = await self.normalize_quantity(request.symbol, request.quantity)
        # Map engine order types to Bybit V5.  The engine only ever sends
        # MARKET (entries/exits) and STOP_MARKET / TAKE_PROFIT_MARKET
        # (TP/SL brackets with stop_price + working_type=MARK_PRICE).
        # Bybit has no STOP_MARKET type: conditionals are Market orders
        # with triggerPrice + triggerBy.
        order_kind = (request.order_type or "MARKET").upper()
        is_conditional = order_kind in ("STOP_MARKET", "TAKE_PROFIT_MARKET")
        # Bybit enums are case-sensitive: side ∈ {Buy, Sell}, orderType ∈ {Market,
        # Limit}.
        side = {"BUY": "Buy", "LONG": "Buy", "SELL": "Sell", "SHORT": "Sell"}.get(
            request.side.upper(), request.side
        )
        order_type = (
            "Market" if (order_kind == "MARKET" or is_conditional) else order_kind
        )
        params: dict[str, object] = {
            "category": self.CATEGORY,
            "symbol": request.symbol,
            "side": side,
            "orderType": order_type,
            "qty": str(quantity),
        }
        # positionIdx selects the position leg: 0 = one-way mode (the
        # default position_mode, both sides share one position).  A hedge
        # leg is selected only when the caller marks the request via
        # position_side (LONG/BUY -> 1, SHORT/SELL -> 2, generic HEDGE ->
        # derived from the order side); otherwise keep 0.
        hedge_side = (request.position_side or "").strip().upper()
        if hedge_side in ("LONG", "BUY"):
            params["positionIdx"] = 1  # hedge-mode long leg
        elif hedge_side in ("SHORT", "SELL"):
            params["positionIdx"] = 2  # hedge-mode short leg
        elif hedge_side in ("HEDGE", "BOTH"):
            params["positionIdx"] = 1 if side == "Buy" else 2
        else:
            params["positionIdx"] = 0  # one-way mode (default position_mode)
        if request.price is not None:
            params["price"] = str(
                await self.normalize_price(request.symbol, request.price)
            )
        if request.stop_price is not None:
            trigger = await self.normalize_price(request.symbol, request.stop_price)
            params["triggerPrice"] = str(trigger)
            working = (request.working_type or "MARK_PRICE").upper()
            params["triggerBy"] = (
                "MarkPrice" if working == "MARK_PRICE" else "LastPrice"
            )
            # NOTE: no tpslMode here — that field belongs to the
            # POST /v5/order/set-trading-stop path, not standalone
            # POST /v5/order/create.
        if request.price_protect:
            params["priceProtect"] = True
        if request.time_in_force and order_type != "Market":
            # Bybit rejects/forces timeInForce on Market (and conditional-
            # market) orders — only send it for Limit orders.
            params["timeInForce"] = request.time_in_force.upper()
        if request.reduce_only:
            params["reduceOnly"] = True
        if request.client_order_id:
            # Bybit orderLinkId: ≤36 chars.  Gateway sends uuid.hex (32).
            params["orderLinkId"] = request.client_order_id[:36]
        # Attached TP/SL (entry orders): position-level protection on the
        # create call itself — 1 REST request instead of 3.  Full-position
        # mode auto-tracks the filled qty, so partial fills stay covered.
        if not is_conditional:
            attached = False
            if request.take_profit_price is not None:
                params["takeProfit"] = str(
                    await self.normalize_price(
                        request.symbol, request.take_profit_price
                    )
                )
                params["tpTriggerBy"] = "MarkPrice"
                attached = True
            if request.stop_loss_price is not None:
                params["stopLoss"] = str(
                    await self.normalize_price(request.symbol, request.stop_loss_price)
                )
                params["slTriggerBy"] = "MarkPrice"
                attached = True
            if attached:
                params["tpslMode"] = "Full"
        data = await self._post("/v5/order/create", params)
        # Bybit V5 returns orderId as a UUID string (e.g. "0f4a5a75-..."),
        # not an integer.  Keep it as-is to avoid int() conversion errors.
        order_id = data.get("orderId", 0) or 0
        status = _normalize_order_status(data.get("orderStatus", "Created"))
        return OrderResult(
            order_id=order_id,
            client_order_id=data.get("orderLinkId", request.client_order_id),
            symbol=data.get("symbol", request.symbol),
            side=data.get("side", request.side),
            order_type=data.get("orderType", request.order_type),
            quantity=Decimal(str(data.get("qty", str(quantity)))),
            filled_qty=Decimal(str(data.get("cumExecQty", "0") or "0")),
            price=(
                Decimal(str(data.get("price", "0")))
                if data.get("price") not in (None, "", "0")
                else request.price
            ),
            status=status,
        )

    async def cancel_order(self, order_id: int | str, symbol: str = "") -> bool:
        """Cancel an order by Bybit order ID."""
        params: dict[str, object] = {
            "category": self.CATEGORY,
            "orderId": str(order_id),
        }
        if symbol:
            params["symbol"] = symbol
        try:
            await self._post("/v5/order/cancel", params)
            return True
        except ExchangeOrderError:
            return False
        except ExchangeError:
            return False

    @staticmethod
    def _order_from_entry(entry: dict, order_id: int | str) -> Order:
        """Map a Bybit order-history/realtime entry to an ``Order``."""
        return Order(
            id=entry.get("orderId", order_id) or order_id,
            client_order_id=entry.get("orderLinkId", ""),
            symbol=entry.get("symbol", ""),
            side=entry.get("side", ""),
            order_type=entry.get("orderType", ""),
            quantity=Decimal(str(entry.get("qty", "0") or "0")),
            filled_qty=Decimal(str(entry.get("cumExecQty", "0") or "0")),
            price=(
                Decimal(str(entry.get("price", "0")))
                if entry.get("price") not in (None, "", "0")
                else None
            ),
            stop_price=(
                Decimal(str(entry.get("triggerPrice", "0")))
                if entry.get("triggerPrice") not in (None, "", "0")
                else None
            ),
            status=_normalize_order_status(entry.get("orderStatus", "")),
            time_in_force=entry.get("timeInForce", "GTC"),
            created_at=int(entry.get("createdTime", 0) or 0),
            updated_at=int(entry.get("updatedTime", 0) or 0),
        )

    async def get_order_status(
        self, order_id: int | str, symbol: str = ""
    ) -> Order | None:
        """Query a single order's status: realtime first, history fallback.

        Returns ``None`` when the order is absent on the exchange (never a
        fabricated empty ``Order``).  ``category`` is always sent, plus
        ``symbol`` when known (Bybit V5 ``GET /v5/order/history`` defaults
        to the USDT settle scope when no symbol is given).
        """
        params: dict[str, object] = {
            "category": self.CATEGORY,
            "orderId": str(order_id),
        }
        if symbol:
            params["symbol"] = symbol
        for endpoint in ("/v5/order/realtime", "/v5/order/history"):
            try:
                data = await self._get(endpoint, params)
            except ExchangeError:
                continue
            entries = (data or {}).get("list", []) if isinstance(data, dict) else []
            if entries:
                return self._order_from_entry(entries[0], order_id)
        return None

    async def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        """Query all open orders for the given symbol (or all symbols)."""
        params: dict[str, object] = {"category": self.CATEGORY, "openOnly": 1}
        # Bybit requires settleCoin or symbol for open-order queries; fall back
        # to settleCoin=USDT when no symbol is given.
        if symbol:
            params["symbol"] = symbol
        else:
            params["settleCoin"] = "USDT"
        data = await self._get("/v5/order/realtime", params)
        orders: list[Order] = []
        for entry in (data or {}).get("list", []) if isinstance(data, dict) else []:
            orders.append(
                Order(
                    id=entry.get("orderId", 0) or 0,
                    client_order_id=entry.get("orderLinkId", ""),
                    symbol=entry.get("symbol", ""),
                    side=entry.get("side", ""),
                    order_type=entry.get("orderType", ""),
                    quantity=Decimal(str(entry.get("qty", "0") or "0")),
                    filled_qty=Decimal(str(entry.get("cumExecQty", "0") or "0")),
                    price=(
                        Decimal(str(entry.get("price", "0")))
                        if entry.get("price") not in (None, "", "0")
                        else None
                    ),
                    stop_price=(
                        Decimal(str(entry.get("triggerPrice", "0")))
                        if entry.get("triggerPrice") not in (None, "", "0")
                        else None
                    ),
                    status=_normalize_order_status(entry.get("orderStatus", "")),
                    time_in_force=entry.get("timeInForce", "GTC"),
                    created_at=int(entry.get("createdTime", 0) or 0),
                    updated_at=int(entry.get("updatedTime", 0) or 0),
                )
            )
        return orders

    async def get_order_realized_pnl(
        self, order_id: int | str, symbol: str = ""
    ) -> Decimal:
        """Fetch the realized PnL for a single Bybit order via
        ``GET /v5/order/history``.

        This is the **primary** PnL source for EXIT notifications: it queries
        a single close order's ``realizedPnl`` directly, eliminating the
        stale-window race of scanning ``/v5/execution/list`` (which returns
        up to 500 fills across all time for a symbol).

        Bybit V5 ``/v5/order/history`` returns order history including
        ``realizedPnl`` for filled/closed orders.

        Args:
            order_id: The Bybit order ID of the closing order.
            symbol: Contract symbol (e.g. ``BTCUSDT``).

        Returns:
            The exchange's realized PnL for this order as a ``Decimal``.
            ``Decimal(0)`` when the exchange doesn't report a value or the
            query fails — callers must treat 0 as "no data" and fall back to
            a computed PnL rather than trusting it as a real figure.
        """
        if not order_id:
            return Decimal(0)
        params: dict[str, object] = {
            "category": self.CATEGORY,
            "orderId": str(order_id),
        }
        if symbol:
            params["symbol"] = symbol
        try:
            data = await self._get("/v5/order/history", params)
        except ExchangeError:
            return Decimal(0)
        entries = (data or {}).get("list", []) if isinstance(data, dict) else []
        entry = entries[0] if entries else {}
        return Decimal(str(entry.get("realizedPnl", "0") or "0"))

    # ======================================================================
    # REST — Futures Configuration
    # ======================================================================
    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        """Set leverage for a symbol."""
        return await self._post(
            "/v5/position/set-leverage",
            {
                "category": self.CATEGORY,
                "symbol": symbol,
                "buyLeverage": str(leverage),
                "sellLeverage": str(leverage),
            },
        )

    async def set_margin_mode(
        self, symbol: str, margin_type: str, leverage: int = 1
    ) -> dict:
        """Set margin mode (isolated/cross) for a symbol."""
        trade_mode = 1 if margin_type.lower() == "isolated" else 0
        return await self._post(
            "/v5/position/switch-isolated",
            {
                "category": self.CATEGORY,
                "symbol": symbol,
                "tradeMode": trade_mode,
                "buyLeverage": str(leverage),
                "sellLeverage": str(leverage),
            },
        )

    async def set_position_mode(self, mode: str) -> dict:
        """Set position mode (one_way/hedge)."""
        dual = mode.lower() == "hedge"
        return await self._post(
            "/v5/position/switch-mode",
            {"category": self.CATEGORY, "mode": 3 if dual else 0},
        )

    async def get_position_mode(self) -> str:
        """Get the current position mode."""
        data = await self._get(
            "/v5/position/info", {"category": self.CATEGORY, "symbol": "BTCUSDT"}
        )
        # ``positionIdx`` 0 = one-way (both sides share), 1/2 = hedge.
        entries = (data or {}).get("list", []) if isinstance(data, dict) else []
        hedge = any(str(e.get("positionIdx", 0)) in ("1", "2") for e in entries)
        return "hedge" if hedge else "one_way"

    # ======================================================================
    # WebSocket — User Data Streams
    # ======================================================================
    async def subscribe_account_updates(
        self,
    ) -> AsyncGenerator[AccountUpdate, None]:
        """Subscribe to account/position updates via pybit's private WS.

        Yields ``AccountUpdate`` objects as Bybit pushes them.  The WebSocket
        is opened lazily here; ``disconnect()`` tears it down.
        """
        if WebSocket is None:
            raise ExchangeConnectionError("pybit SDK is not installed")

        def _on_message(_ws_msg):  # pybit passes raw WS frames here
            # Runs on pybit's WS IO thread: never block it and never touch
            # the event loop here — just enqueue the raw payload.  The
            # generator loop below drains the inbox and performs the REST
            # refresh (get_account) on the event loop.
            try:
                topic = _ws_msg.get("topic", "") if isinstance(_ws_msg, dict) else ""
                if "wallet" in topic or "position" in topic:
                    self._ws_inbox.put_nowait(
                        {"topic": topic, "ts": int(time.time() * 1000)}
                    )
            except Exception:  # noqa: BLE001, S110 best-effort push
                pass

        self._ws = WebSocket(
            testnet=self._testnet,
            api_key=self._api_key,
            api_secret=self._api_secret,
            channel_type="private",
        )
        try:
            # pybit v5 subscribe() takes a single topic string, not a list.
            # Subscribe to each private topic separately.
            self._ws.subscribe(topic="wallet", callback=_on_message)
            self._ws.subscribe(topic="position", callback=_on_message)
        except Exception as exc:  # noqa: BLE001
            raise ExchangeConnectionError(f"Bybit WS subscribe failed: {exc}") from exc
        while not self._stop_event.is_set():
            # Drain raw WS payloads enqueued by the IO thread and refresh
            # account state via REST here on the event loop.
            drained: list[dict] = []
            while True:
                try:
                    drained.append(self._ws_inbox.get_nowait())
                except queue.Empty:
                    break
            if drained:
                try:
                    account = await self.get_account()
                except Exception:  # noqa: BLE001, S110 best-effort refresh
                    account = None
                if account is not None:
                    for raw in drained:
                        await self._account_queue.put(
                            AccountUpdate(
                                account=account,
                                event_type=raw.get("topic", ""),
                                timestamp=self._now_ms(),
                            )
                        )
            try:
                update = await asyncio.wait_for(self._account_queue.get(), timeout=1.0)
                yield update
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    # ======================================================================
    # Utility
    # ======================================================================
    async def get_exchange_info(self) -> dict:
        """Fetch instrument info for USDT-perpetual symbols.

        Bybit returns ``{"result": {"list": [ {symbol, lotSizeFilter,
        priceFilter}, ... ]}}``.  The ABC's ``_get_lot_filters`` expects a
        spot-style ``symbols`` list, so this adapter overrides
        ``_get_lot_filters`` to read Bybit's layout directly.
        """
        data = await self._get(
            "/v5/market/instruments-info",
            {"category": self.CATEGORY, "limit": 1000},
        )
        return data if isinstance(data, dict) else {}

    async def get_server_time(self) -> int:
        """Fetch the current Bybit server time (unix ms).

        Measures the server-minus-local clock offset, stores it as
        ``clock_offset_ms`` (applied to locally-stamped timestamps via
        ``_now_ms()``), and warns when the skew exceeds ``recv_window``
        (signed requests would be rejected).
        """
        before = int(time.time() * 1000)
        data = await self._get("/v5/market/time")
        after = int(time.time() * 1000)
        raw = (data or {}).get("timeSecond", 0) or 0
        try:
            server_ms = int(float(str(raw)) * 1000)
        except (TypeError, ValueError):
            server_ms = 0
        if server_ms > 10**13:  # already milliseconds, not seconds
            server_ms = int(float(str(raw)))
        if server_ms:
            self.clock_offset_ms = server_ms - (before + after) // 2
            if abs(self.clock_offset_ms) > self._recv_window:
                self._log.warning(
                    "clock_skew_exceeds_recv_window",
                    clock_offset_ms=self.clock_offset_ms,
                    recv_window=self._recv_window,
                )
        return server_ms

    # ---- Bybit-specific filter parsing (overrides ABC default) ------------
    async def _get_lot_filters(self, symbol: str) -> tuple[Decimal, Decimal, Decimal]:
        """Return (step_size, min_qty, min_notional) from Bybit instruments-info."""
        cache = self._exchange_info_cache
        ttl = float(getattr(self, "_exchange_info_ttl", 60))
        cached = _ttl_fresh(cache, symbol, ttl)
        if cached is not None:
            return cached
        info = await self.get_exchange_info()
        step = min_qty = min_notional = Decimal(0)
        found = False
        for s in info.get("list", []) if isinstance(info, dict) else []:
            if s.get("symbol") != symbol:
                continue
            lot = s.get("lotSizeFilter") or {}
            step = Decimal(str(lot.get("qtyStep", "0") or "0"))
            min_qty = Decimal(str(lot.get("minOrderQty", "0") or "0"))
            raw_notional = (
                lot.get("minNotionalValue")
                or lot.get("minOrderValue")
                or lot.get("minNotional")
            )
            if raw_notional not in (None, ""):
                try:
                    min_notional = Decimal(str(raw_notional))
                except InvalidOperation:
                    min_notional = Decimal(0)
            else:
                # Last resort only: Bybit exposes no notional filter, so
                # approximate it from minOrderQty x current mark price.
                try:
                    mark = await self.get_mark_price(symbol)
                    min_notional = min_qty * mark if mark > 0 else min_qty
                except Exception:
                    min_notional = min_qty
            found = True
            break
        if not found:
            raise RuntimeError(f"no instrument info found for {symbol} on Bybit")
        result = (step, min_qty, min_notional)
        _ttl_store(cache, symbol, result)
        return result

    # ---- Exchange-specific error semantics (overrides ABC defaults) -------
    def is_margin_mode_already_set(self, exc: Exception) -> bool:
        """Bybit returns 110043 "Margin mode is not modified" — benign no-op."""
        text = str(exc)
        return _MARGIN_MODE_ALREADY_SET_CODE in text

    def is_order_not_found(self, exc: Exception) -> bool:
        """Bybit returns 20001/30003 "Order does not exist" — resolve locally."""
        text = str(exc).lower()
        if _ORDER_NOT_FOUND_TEXT in text:
            return True
        return any(c in str(exc) for c in _ORDER_NOT_FOUND_CODES)
