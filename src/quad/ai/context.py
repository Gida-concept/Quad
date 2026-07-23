"""Market context collector for AI trading decisions.

Gathers all market data needed for AI-driven trading: candles (from Binance
Spot klines API since the Options API does not serve klines), current positions
and account state from the exchange adapter, and option chains.

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
from quad.types.market import Candle, OptionContract

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BINANCE_SPOT_KLINE_URL = "https://api.binance.com/api/v3/klines"

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
    option_chains:
        Dict keyed by pair symbol (e.g. ``"BTCUSDT"``), each value being
        a list of ``OptionContract`` objects.
    timestamp:
        Unix timestamp (seconds) when this context was collected.
    errors:
        Dict of non-fatal errors keyed by step name for diagnostic logging.
    """

    candles: dict[str, list[Candle]] = field(default_factory=dict)
    positions: list[Position] = field(default_factory=list)
    account: Account | None = None
    option_chains: dict[str, list[OptionContract]] = field(default_factory=dict)
    timestamp: float = 0.0
    errors: dict[str, str] = field(default_factory=dict)


# ============================================================================
# Candle fetching
# ============================================================================


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
            _BINANCE_SPOT_KLINE_URL,
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

    Fetches candles (from Binance Spot klines), current positions and
    account state (from the exchange adapter), and option chains (from
    the market data engine).

    Parameters
    ----------
    exchange_adapter:
        The exchange adapter (must have ``get_account`` and
        ``get_positions`` methods).
    market_data_engine:
        The market data engine (must have ``get_option_chain`` method).
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
    ai_cfg = AiConfig.model_validate(cfg.get("ai", {}))

    pairs: list[str] = list(ai_cfg.pairs)
    timeframes: list[str] = list(ai_cfg.timeframes)
    candle_count: int = ai_cfg.candle_count

    context = MarketContext(timestamp=time.time())

    # ------------------------------------------------------------------
    # 1. Fetch candles via Binance Spot klines API
    # ------------------------------------------------------------------
    try:
        async with aiohttp.ClientSession() as session:
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
                    logger.info(
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
        logger.info("positions_fetched", count=len(positions))
    except Exception as exc:
        context.errors["positions"] = str(exc)
        logger.warning("positions_fetch_failed", error=str(exc))

    # ------------------------------------------------------------------
    # 3. Fetch account state from exchange adapter
    # ------------------------------------------------------------------
    try:
        context.account = await exchange_adapter.get_account()
        logger.info(
            "account_fetched",
            total_usdt=float(context.account.total_usdt)
            if context.account
            else 0,
        )
    except Exception as exc:
        context.errors["account"] = str(exc)
        logger.warning("account_fetch_failed", error=str(exc))

    # ------------------------------------------------------------------
    # 4. Fetch option chains for all pairs
    # ------------------------------------------------------------------
    for pair in pairs:
        try:
            chain = await market_data_engine.get_option_chain(pair)
            context.option_chains[pair] = list(chain)
            logger.info(
                "option_chain_fetched",
                pair=pair,
                count=len(chain),
            )
        except Exception as exc:
            context.errors[f"option_chain_{pair}"] = str(exc)
            logger.warning(
                "option_chain_fetch_failed",
                pair=pair,
                error=str(exc),
            )

    return context
