"""Market context collector for AI trading decisions.

Gathers all market data needed for AI-driven futures trading: candles (from
the exchange adapter's klines API), current positions and account state from the
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
from decimal import Decimal
from typing import Any

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

# Mapping from our timeframe strings to exchange interval strings
_TIMEFRAME_MAP: dict[str, str] = {
    # Bybit V5 kline intervals (minutes as plain numbers, daily/weekly/monthly).
    "1m": "1",
    "3m": "3",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "1h": "60",
    "2h": "120",
    "4h": "240",
    "6h": "360",
    "12h": "720",
    "1d": "D",
    "1w": "W",
    "1M": "M",
}


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
    smart_money: dict[str, dict] = field(default_factory=dict)
    sentiment: dict[str, dict] = field(default_factory=dict)
    news: list[dict] = field(default_factory=list)
    timestamp: float = 0.0
    errors: dict[str, str] = field(default_factory=dict)


# ============================================================================
# Candle fetching
# ============================================================================


async def close_http_session() -> None:
    """No-op kept for backward compatibility."""


async def _fetch_klines(
    exchange_adapter: Any,
    pair: str,
    interval: str,
    limit: int,
) -> list[tuple[float, ...]] | None:
    """Fetch klines via the exchange adapter.

    Routes through the adapter's ``get_klines()`` method, which handles
    URL resolution (production vs. testnet), rate-limit tracking, retries,
    and server time offset.

    Parameters
    ----------
    exchange_adapter:
        Exchange adapter with a ``get_klines`` method.
    pair:
        Trading pair, e.g. ``"BTCUSDT"``.
    interval:
        Exchange interval string, e.g. ``"15m"``, ``"1h"``.
    limit:
        Number of candles to fetch (max 1000).

    Returns
    -------
    list of tuples or None on failure.
        Each tuple: (open_time, open, high, low, close, volume, ...).
        Timestamps are in seconds.
    """
    # Convert friendly intervals to Bybit format (e.g. "1h" -> "60")
    bybit_interval = _TIMEFRAME_MAP.get(interval.lower(), interval)

    try:
        return await exchange_adapter.get_klines(pair, bybit_interval, limit)
    except Exception as exc:
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
    """Convert raw exchange kline tuples to ``Candle`` dataclasses.

    Parameters
    ----------
    pair:
        Trading pair symbol.
    klines:
        List of (open_time, open, high, low, close, volume) tuples where
        open_time is epoch seconds or milliseconds (normalized to ms).

    Returns
    -------
    list of ``Candle`` objects, oldest first.
    """
    candles: list[Candle] = []
    for k in klines:
        # Normalize to epoch MILLISECONDS: the Bybit adapter returns epoch
        # seconds (Bybit V5 ms converted at the boundary), while other
        # adapters may return ms. Values below 1e12 are seconds → ×1000.
        ts_raw = int(k[0])
        ts_ms = ts_raw * 1000 if ts_raw < 1_000_000_000_000 else ts_raw
        candles.append(
            Candle(
                symbol=pair,
                open=Decimal(str(k[1])),
                high=Decimal(str(k[2])),
                low=Decimal(str(k[3])),
                close=Decimal(str(k[4])),
                volume=Decimal(str(k[5])),
                timestamp=ts_ms,  # epoch milliseconds
            )
        )
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

    All data is fetched via the exchange adapter and market data engine.

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
        Optional configuration dict.
    """
    cfg = config or {}
    ai_cfg = AiConfig.model_validate(cfg.get("ai", {}))

    pairs: list[str] = list(ai_cfg.pairs)
    timeframes: list[str] = list(ai_cfg.timeframes)
    candle_count: int = ai_cfg.candle_count

    context = MarketContext(timestamp=time.time())

    # ------------------------------------------------------------------
    # Exchange adapter path (default)
    # ------------------------------------------------------------------
    await _collect_context_via_adapter(
        context, exchange_adapter, market_data_engine, pairs, timeframes, candle_count
    )

    return context


# ============================================================================
# Exchange adapter context collection
# ============================================================================


async def _collect_context_via_adapter(
    context: MarketContext,
    exchange_adapter: Any,
    market_data_engine: Any,
    pairs: list[str],
    timeframes: list[str],
    candle_count: int,
) -> None:
    """Collect market context via the exchange adapter and market data engine.

    Fetches candles, positions, account, funding rates, tickers, and order
    books for all configured pairs.
    """
    # 1. Fetch candles for all pairs × timeframes (parallelized)
    sem = asyncio.Semaphore(10)

    async def _fetch_candles(pair: str, tf: str) -> tuple[str, list] | None:
        try:
            async with sem:
                raw = await _fetch_klines(exchange_adapter, pair, tf, candle_count)
            if raw:
                return (f"{pair}_{tf}", raw)
            return None
        except Exception as exc:
            context.errors[f"candles_{pair}_{tf}"] = str(exc)
            return None

    candle_tasks = [_fetch_candles(p, tf) for p in pairs for tf in timeframes]
    candle_results = await asyncio.gather(*candle_tasks, return_exceptions=True)

    for result in candle_results:
        # ``return_exceptions=True`` widens the result to include BaseException
        # (CancelledError, KeyboardInterrupt), not just Exception.
        if isinstance(result, BaseException) or result is None:
            continue
        key, raw_klines = result
        pair = key.split("_")[0]
        candles = _klines_to_candles(pair, raw_klines)
        context.candles[key] = candles

    # 2. Fetch positions from exchange adapter
    try:
        positions = await exchange_adapter.get_positions()
        context.positions = positions or []
    except Exception as exc:
        context.errors["positions"] = str(exc)

    # 3. Fetch account balance
    try:
        account = await exchange_adapter.get_account()
        context.account = account
    except Exception as exc:
        context.errors["account"] = str(exc)

    # 4. Fetch funding rates, tickers, order books for each pair
    # (Bybit symbols are used as-is, e.g. "BTCUSDT".)
    for pair in pairs:
        inst_id = pair

        # Funding rate
        try:
            fr = await market_data_engine.get_funding_rate(inst_id)
            if fr:
                context.funding_rates[pair] = fr
        except Exception as exc:
            context.errors[f"funding_{pair}"] = str(exc)

        # Mark price / ticker
        try:
            ticker = await market_data_engine.get_ticker(inst_id)
            if ticker:
                mark = float(getattr(ticker, "mark_price", 0) or 0)
                if mark > 0:
                    context.mark_prices[pair] = mark
                context.futures_contracts[pair] = ticker
        except Exception as exc:
            context.errors[f"ticker_{pair}"] = str(exc)

        # Order book
        try:
            ob = await market_data_engine.get_order_book(inst_id)
            if ob:
                context.order_books[pair] = ob
        except Exception as exc:
            context.errors[f"orderbook_{pair}"] = str(exc)

    # 5. Fetch smart money data via exchange adapter (if available)
    if hasattr(exchange_adapter, "get_smart_money_summary"):
        for pair in pairs:
            coin = pair.replace("USDT", "")
            try:
                sm = await exchange_adapter.get_smart_money_summary(coin)
                if sm:
                    context.smart_money[pair] = sm
            except Exception as exc:
                context.errors[f"smart_money_{pair}"] = str(exc)
