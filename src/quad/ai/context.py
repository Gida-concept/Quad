"""Market context collector for AI trading decisions.

Gathers all market data needed for AI-driven futures trading: candles (from
Binance Futures klines API), current positions and account state from the
exchange adapter, and futures market data (funding rates, mark prices, order
books, ticker info).

Usage::

    from quad.ai.context import collect_market_context, MarketContext

    context = await collect_market_context(
        exchange_adapter=adapter,
        market_data_engine=engine,
        db_manager=db,
        config=config_dict,
    )
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import aiohttp
import structlog

from quad.config.schema import AiConfig
from quad.types.domain import Account, Position
from quad.types.market import Candle, FundingRate, FuturesContract

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BINANCE_FUTURES_KLINE_URL = "https://fapi.binance.com/fapi/v1/klines"

# Shared aiohttp session (created on first use, closed via close_http_session())
_shared_session: aiohttp.ClientSession | None = None

# Mapping from our timeframe strings to Binance interval strings
_TIMEFRAME_MAP: dict[str, str] = {
    "15m": "15m",
    "1h": "1h",
}

# aiohttp timeout for klines requests
_KLINE_TIMEOUT_S = 15


# ============================================================================
# MarketContext dataclass
# ============================================================================


@dataclass
class MarketContext:
    """Aggregated market snapshot for AI decision-making.

    Attributes
    ----------
    candles:
        Dict keyed by ``"{pair}_{timeframe}"`` (e.g. ``"BTCUSDT_15m"``),
        each value being a list of ``Candle`` objects (oldest first).
    positions:
        Current open positions fetched from the exchange adapter.
    account:
        Current account state (balances, total USDT).
    funding_rates:
        Dict keyed by pair symbol (e.g. ``"BTCUSDT"``), each value being
        a ``FundingRate`` dataclass.
    futures_contracts:
        Dict keyed by pair symbol, each value being a ``FuturesContract``
        dataclass with ticker info (volume, OI, 24h change, etc.).
    order_books:
        Dict keyed by pair symbol, each value being a raw order book dict
        with ``bids`` and ``asks`` lists.
    mark_prices:
        Dict keyed by pair symbol, each value being the mark price as a
        float.
    timestamp:
        Unix timestamp (seconds) when this context was collected.
    errors:
        Dict of non-fatal errors keyed by step name for diagnostic logging.
    """

    candles: dict[str, list[Candle]] = field(default_factory=dict)
    positions: list[Position] = field(default_factory=list)
    account: Account | None = None
    funding_rates: dict[str, FundingRate] = field(default_factory=dict)
    futures_contracts: dict[str, FuturesContract] = field(default_factory=dict)
    order_books: dict[str, dict] = field(default_factory=dict)
    mark_prices: dict[str, float] = field(default_factory=dict)
    timestamp: float = 0.0
    errors: dict[str, str] = field(default_factory=dict)


# ============================================================================
# Candle fetching
# ============================================================================


def _get_http_session() -> aiohttp.ClientSession:
    """Return the shared aiohttp session, creating it on first access."""
    global _shared_session
    if _shared_session is None or _shared_session.closed:
        _shared_session = aiohttp.ClientSession()
    return _shared_session


async def close_http_session() -> None:
    """Close the shared aiohttp session (call during application shutdown)."""
    global _shared_session
    if _shared_session is not None and not _shared_session.closed:
        await _shared_session.close()
        _shared_session = None


async def _fetch_klines(
    session: aiohttp.ClientSession,
    pair: str,
    interval: str,
    limit: int,
) -> list[tuple[float, ...]] | None:
    """Fetch klines from the Binance Spot public API.

    Parameters
    ----------
    session:
        Reusable aiohttp session.
    pair:
        Trading pair, e.g. ``"BTCUSDT"``.
    interval:
        Binance interval string, e.g. ``"15m"``, ``"1h"``.
    limit:
        Number of candles to fetch (max 1000).

    Returns
    -------
    list of tuples or None on failure.
        Each tuple: (open_time, open, high, low, close, volume, ...).
        Timestamps are in seconds.
    """
    params: dict[str, Any] = {
        "symbol": pair,
        "interval": interval,
        "limit": limit,
    }

    try:
        async with session.get(
            _BINANCE_FUTURES_KLINE_URL,
            params=params,
            timeout=aiohttp.ClientTimeout(total=_KLINE_TIMEOUT_S),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.warning(
                    "kline_fetch_failed",
                    pair=pair,
                    interval=interval,
                    status=resp.status,
                    body=body[:200],
                )
                return None

            data = await resp.json()
            # Binance kline format (index -> field):
            # 0 -> open time (ms), 1 -> open, 2 -> high, 3 -> low,
            # 4 -> close, 5 -> volume, 6 -> close time (ms), ...
            results: list[tuple[float, ...]] = []
            for k in data:
                results.append((
                    k[0] / 1000.0,  # open time in seconds
                    float(k[1]),     # open
                    float(k[2]),     # high
                    float(k[3]),     # low
                    float(k[4]),     # close
                    float(k[5]),     # volume
                ))
            return results

    except (asyncio.TimeoutError, aiohttp.ClientError, ValueError, TypeError) as exc:
        logger.warning(
            "kline_request_error",
            pair=pair,
            interval=interval,
            error=str(exc),
        )
        return None


def _klines_to_candles(
    pair: str,
    klines: list[tuple[float, ...]],
) -> list[Candle]:
    """Convert raw Binance kline tuples to ``Candle`` dataclasses.

    Parameters
    ----------
    pair:
        Trading pair symbol.
    klines:
        List of (open_time_s, open, high, low, close, volume) tuples.

    Returns
    -------
    list of ``Candle`` objects, oldest first.
    """
    candles: list[Candle] = []
    for k in klines:
        candles.append(Candle(
            symbol=pair,
            open=Decimal(str(k[1])),
            high=Decimal(str(k[2])),
            low=Decimal(str(k[3])),
            close=Decimal(str(k[4])),
            volume=Decimal(str(k[5])),
            timestamp=int(k[0] * 1000),  # store in ms for consistency
        ))
    return candles


# ============================================================================
# Public API
# ============================================================================


async def collect_market_context(
    exchange_adapter: Any,
    market_data_engine: Any,
    db_manager: Any | None = None,
    config: dict[str, Any] | None = None,
) -> MarketContext:
    """Collect a complete market snapshot for AI trading decisions.

    Fetches candles (from Binance Futures klines), current positions and
    account state (from the exchange adapter), and futures market data
    (funding rates, mark prices, order books, ticker info) from the
    market data engine.

    Parameters
    ----------
    exchange_adapter:
        The exchange adapter (must have ``get_account`` and
        ``get_positions`` methods).
    market_data_engine:
        The market data engine (must have ``get_funding_rate``,
        ``get_mark_price``, ``get_order_book``, and ``get_ticker``
        methods).
    db_manager:
        Optional database manager (currently unused; reserved for future
        historical queries).
    config:
        Optional configuration dict.  Recognised keys:

        * ``ai.pairs`` — list of pair symbols (default
          ``["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]``).
        * ``ai.timeframes`` — list of timeframe strings (default
          ``["15m", "1h"]``).
        * ``ai.candle_count`` — number of candles per pair per timeframe
          (default 300).

    Returns
    -------
    MarketContext
        A snapshot dataclass with all collected data.
    """
    cfg = config or {}
    ai_cfg = AiConfig.model_validate(cfg.get("ai"))

    pairs: list[str] = list(ai_cfg.pairs)
    timeframes: list[str] = list(ai_cfg.timeframes)
    candle_count: int = ai_cfg.candle_count

    context = MarketContext(timestamp=time.time())

    # ------------------------------------------------------------------
    # 1. Fetch candles via Binance Futures klines API (reused session)
    # ------------------------------------------------------------------
    try:
        session = _get_http_session()
        tasks = []
        for pair in pairs:
            for tf in timeframes:
                interval = _TIMEFRAME_MAP.get(tf, tf)
                tasks.append(
                    _fetch_klines(session, pair, interval, candle_count)
                )

        results = await asyncio.gather(*tasks, return_exceptions=True)

        idx = 0
        for pair in pairs:
            for tf in timeframes:
                key = f"{pair}_{tf}"
                result = results[idx]
                idx += 1

                if isinstance(result, Exception):
                    context.errors[f"candles_{key}"] = str(result)
                    logger.warning(
                        "candle_fetch_failed",
                        pair=pair,
                        timeframe=tf,
                        error=str(result),
                    )
                    continue

                if result is None:
                    context.errors[f"candles_{key}"] = "empty_response"
                    logger.warning(
                        "candle_fetch_empty",
                        pair=pair,
                        timeframe=tf,
                    )
                    continue

                context.candles[key] = _klines_to_candles(pair, result)
                logger.debug(
                    "candles_fetched",
                    pair=pair,
                    timeframe=tf,
                    count=len(result),
                )

    except Exception as exc:
        context.errors["candle_collection"] = str(exc)
        logger.exception("candle_collection_error", error=str(exc))

    # ------------------------------------------------------------------
    # 2. Fetch positions from exchange adapter
    # ------------------------------------------------------------------
    try:
        positions = await exchange_adapter.get_positions()
        context.positions = list(positions)
        logger.debug("positions_fetched", count=len(positions))
    except Exception as exc:
        context.errors["positions"] = str(exc)
        logger.warning("positions_fetch_failed", error=str(exc))

    # ------------------------------------------------------------------
    # 3. Fetch account state from exchange adapter
    # ------------------------------------------------------------------
    try:
        context.account = await exchange_adapter.get_account()
        logger.debug(
            "account_fetched",
            total_usdt=float(context.account.total_usdt)
            if context.account
            else 0,
        )
    except Exception as exc:
        context.errors["account"] = str(exc)
        logger.warning("account_fetch_failed", error=str(exc))

    # ------------------------------------------------------------------
    # 4. Fetch futures market data for all pairs (parallelized)
    # ------------------------------------------------------------------
    async def _fetch_one_futures_data(p: str) -> dict[str, Any]:
        out: dict[str, Any] = {"pair": p}
        try:
            fr = await market_data_engine.get_funding_rate(p)
            out["funding_rate"] = fr
        except Exception as exc:
            context.errors[f"funding_rate_{p}"] = str(exc)
            logger.warning("funding_rate_fetch_failed", pair=p, error=str(exc))

        try:
            mp = await market_data_engine.get_mark_price(p)
            out["mark_price"] = float(mp) if mp is not None else None
        except Exception as exc:
            context.errors[f"mark_price_{p}"] = str(exc)
            logger.warning("mark_price_fetch_failed", pair=p, error=str(exc))

        try:
            ob = await market_data_engine.get_order_book(p)
            if ob is not None:
                out["order_book"] = ob
        except Exception as exc:
            context.errors[f"order_book_{p}"] = str(exc)
            logger.warning("order_book_fetch_failed", pair=p, error=str(exc))

        try:
            ticker = await market_data_engine.get_ticker(p)
            if ticker is not None:
                out["ticker"] = ticker
        except Exception as exc:
            context.errors[f"ticker_{p}"] = str(exc)
            logger.warning("ticker_fetch_failed", pair=p, error=str(exc))

        return out

    results = await asyncio.gather(
        *[_fetch_one_futures_data(p) for p in pairs], return_exceptions=True
    )
    for result in results:
        if isinstance(result, Exception):
            logger.warning("futures_data_gather_error", error=str(result))
            continue
        pair = result["pair"]
        fr = result.get("funding_rate")
        if fr is not None:
            context.funding_rates[pair] = fr
        mp = result.get("mark_price")
        if mp is not None:
            context.mark_prices[pair] = mp
        ob = result.get("order_book")
        if ob is not None:
            context.order_books[pair] = ob
        ticker = result.get("ticker")
        if ticker is not None:
            # Build a FuturesContract from ticker data if available
            try:
                contract = FuturesContract(
                    symbol=pair,
                    mark_price=Decimal(str(mp)) if mp else Decimal("0"),
                    last_price=Decimal(str(ticker.get("lastPrice", 0))),
                    volume_24h=Decimal(str(ticker.get("volume", 0))),
                    price_change_24h=Decimal(str(ticker.get("priceChange", 0))),
                    high_24h=Decimal(str(ticker.get("highPrice", 0))),
                    low_24h=Decimal(str(ticker.get("lowPrice", 0))),
                    last_update=int(time.time()),
                )
                context.futures_contracts[pair] = contract
            except Exception as exc:
                context.errors[f"contract_{pair}"] = str(exc)
                logger.warning("contract_build_failed", pair=pair, error=str(exc))

    return context
