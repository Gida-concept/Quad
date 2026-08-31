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
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1H",
    "4h": "4H",
    "1d": "1D",
    "1w": "1W",
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
    try:
        return await exchange_adapter.get_klines(pair, interval, limit)
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
        List of (open_time_s, open, high, low, close, volume) tuples.

    Returns
    -------
    list of ``Candle`` objects, oldest first.
    """
    candles: list[Candle] = []
    for k in klines:
        candles.append(
            Candle(
                symbol=pair,
                open=Decimal(str(k[1])),
                high=Decimal(str(k[2])),
                low=Decimal(str(k[3])),
                close=Decimal(str(k[4])),
                volume=Decimal(str(k[5])),
                timestamp=int(k[0]),  # OKX timestamps are already in ms
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
    mcp_client: Any | None = None,
) -> MarketContext:
    """Collect a complete market snapshot for AI trading decisions.

    When ``mcp_client`` is provided, all data is fetched via the OKX MCP
    server (no WebSocket needed).  Falls back to the exchange adapter +
    market data engine path when MCP is unavailable.

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
    mcp_client:
        Optional ``OkxMcpClient`` for MCP-powered data collection.
        When provided, bypasses exchange adapter and market data engine
        for all data fetching.
    """
    cfg = config or {}
    ai_cfg = AiConfig.model_validate(cfg.get("ai", {}))

    pairs: list[str] = list(ai_cfg.pairs)
    timeframes: list[str] = list(ai_cfg.timeframes)
    candle_count: int = ai_cfg.candle_count

    context = MarketContext(timestamp=time.time())

    # ------------------------------------------------------------------
    # MCP-powered data collection (when available)
    # ------------------------------------------------------------------
    if mcp_client is not None and hasattr(mcp_client, "call_tool"):
        try:
            await _collect_context_via_mcp(context, mcp_client, pairs, timeframes, candle_count)
            logger.info(
                "market_context_collected_via_mcp",
                pairs=len(pairs),
                timeframes=len(timeframes),
            )
            return context
        except Exception as exc:
            context.errors["mcp_fallback"] = str(exc)
            logger.warning(
                "mcp_context_collection_failed_falling_back",
                error=str(exc),
            )
            # Fall through to exchange adapter path

    # ------------------------------------------------------------------
    # Exchange adapter path (default)
    # ------------------------------------------------------------------
    await _collect_context_via_adapter(context, exchange_adapter, market_data_engine, pairs, timeframes, candle_count)

    return context


# ============================================================================
# MCP-powered context collection
# ============================================================================


async def _collect_context_via_mcp(
    context: MarketContext,
    mcp_client: Any,
    pairs: list[str],
    timeframes: list[str],
    candle_count: int,
) -> None:
    """Collect market context entirely via MCP tools."""

    # 1. Fetch candles for all pairs × timeframes (parallelized)
    async def _fetch_candles(pair: str, tf: str) -> tuple[str, list] | None:
        try:
            inst_id = pair.replace("USDT", "-USDT-SWAP") if "USDT" in pair and "-" not in pair else pair
            raw_candles = await mcp_client.get_candles(inst_id, tf.upper(), candle_count)
            return (f"{pair}_{tf}", raw_candles)
        except Exception as exc:
            context.errors[f"mcp_candles_{pair}_{tf}"] = str(exc)
            return None

    candle_tasks = [_fetch_candles(p, tf) for p in pairs for tf in timeframes]
    candle_results = await asyncio.gather(*candle_tasks, return_exceptions=True)

    for result in candle_results:
        if isinstance(result, Exception) or result is None:
            continue
        key, raw_candles = result
        if not raw_candles:
            context.errors[f"candles_{key}"] = "empty_response"
            continue
        pair = key.split("_")[0]
        candles = _klines_to_candles(pair, raw_candles)
        context.candles[key] = candles
        logger.debug("mcp_candles_fetched", key=key, count=len(candles))

    # 2. Fetch positions via MCP
    try:
        from quad.types.domain import (
            Position, PositionSide, PositionStatus,
            FuturesPositionSide, MarginType, Account, Balance,
        )
        from decimal import Decimal

        raw_positions = await mcp_client.get_positions("SWAP")
        positions: list[Position] = []
        for pos in raw_positions:
            pos_val = pos.get("pos", "0")
            if pos_val == "0" or pos_val == "" or pos_val is None:
                continue
            side_str = pos.get("posSide", "net")
            qty = abs(Decimal(str(pos_val)))
            if side_str == "long" or (side_str == "net" and qty > 0):
                position_side = PositionSide.LONG
                fut_side = FuturesPositionSide.LONG
            else:
                position_side = PositionSide.SHORT
                fut_side = FuturesPositionSide.SHORT
            positions.append(Position(
                symbol=pos.get("instId", ""),
                side=position_side,
                quantity=qty,
                entry_price=Decimal(str(pos.get("avgPx", "0") or "0")),
                current_price=Decimal(str(pos.get("markPx", pos.get("last", "0")) or "0")),
                unrealized_pnl=Decimal(str(pos.get("upl", "0") or "0")),
                leverage=int(float(pos.get("lever", "1") or "1")),
                margin_type=MarginType.ISOLATED if pos.get("mgnMode") == "isolated" else MarginType.CROSS,
                position_side=fut_side,
                liquidation_price=Decimal(str(pos.get("liqPx", "0") or "0")),
                status=PositionStatus.OPEN,
                opened_at=int(pos.get("cTime", "0") or "0"),
                updated_at=int(pos.get("uTime", "0") or "0"),
            ))
        context.positions = positions
    except Exception as exc:
        context.errors["mcp_positions"] = str(exc)

    # 3. Fetch account via MCP
    try:
        from quad.types.domain import Account, Balance
        from decimal import Decimal

        result = await mcp_client.get_account_balance_all()
        balances: dict[str, Balance] = {}
        total_usdt = Decimal(0)
        details = result.get("details", []) if isinstance(result, dict) else []
        for detail in details:
            ccy = detail.get("ccy", "")
            avail_eq = Decimal(str(detail.get("availEq", detail.get("eq", "0"))))
            frozen_bal = Decimal(str(detail.get("frozenBal", "0")))
            bal = Balance(asset=ccy, free=avail_eq, locked=frozen_bal)
            balances[ccy] = bal
            if ccy == "USDT":
                total_usdt = bal.total

        import time as _time
        context.account = Account(
            id="mcp",
            exchange="okx",
            balances=balances,
            total_usdt=total_usdt,
            timestamp=int(_time.time() * 1000),
            total_wallet_balance=total_usdt,
            total_margin_balance=total_usdt,
            available_balance=total_usdt,
        )
    except Exception as exc:
        context.errors["mcp_account"] = str(exc)

    # 4. Fetch futures market data for all pairs (parallelized)
    async def _fetch_one_mcp_data(pair: str) -> dict[str, Any]:
        out: dict[str, Any] = {"pair": pair}
        inst_id = pair.replace("USDT", "-USDT-SWAP") if "USDT" in pair and "-" not in pair else pair

        try:
            fr_raw = await mcp_client.get_funding_rate(inst_id)
            fr_data = fr_raw.get("data", [{}]) if isinstance(fr_raw, dict) else [{}]
            fr_item = fr_data[0] if fr_data else {}
            out["funding_rate"] = FundingRate(
                symbol=pair,
                funding_rate=Decimal(str(fr_item.get("fundingRate", "0") or "0")),
                next_funding_time=int(fr_item.get("fundingTime", "0") or "0"),
                mark_price=Decimal(str(fr_item.get("markPx", "0") or "0")),
            )
        except Exception as exc:
            context.errors[f"mcp_funding_{pair}"] = str(exc)

        try:
            ticker_raw = await mcp_client.get_ticker(inst_id)
            ticker_data = ticker_raw.get("data", [{}]) if isinstance(ticker_raw, dict) else [{}]
            ticker_item = ticker_data[0] if ticker_data else {}
            if ticker_item:
                last_px = Decimal(str(ticker_item.get("last", "0") or "0"))
                out["mark_price"] = float(last_px)
                out["ticker"] = ticker_item
                contract = FuturesContract(
                    symbol=pair,
                    mark_price=last_px,
                    last_price=last_px,
                    volume_24h=Decimal(str(ticker_item.get("vol24h", "0") or "0")),
                    price_change_24h=Decimal(str(ticker_item.get("sodUtc8", "0") or "0")),
                    high_24h=Decimal(str(ticker_item.get("high24h", "0") or "0")),
                    low_24h=Decimal(str(ticker_item.get("low24h", "0") or "0")),
                    last_update=int(time.time()),
                )
                context.futures_contracts[pair] = contract
        except Exception as exc:
            context.errors[f"mcp_ticker_{pair}"] = str(exc)

        try:
            ob_raw = await mcp_client.get_orderbook(inst_id, 20)
            if isinstance(ob_raw, dict):
                ob_data = ob_raw.get("data", [{}]) if "data" in ob_raw else ob_raw
                if isinstance(ob_data, list) and ob_data:
                    out["order_book"] = ob_data[0] if isinstance(ob_data[0], dict) else ob_data
                elif isinstance(ob_data, dict):
                    out["order_book"] = ob_data
        except Exception as exc:
            context.errors[f"mcp_orderbook_{pair}"] = str(exc)

        return out

    data_results = await asyncio.gather(
        *[_fetch_one_mcp_data(p) for p in pairs], return_exceptions=True
    )
    for result in data_results:
        if isinstance(result, Exception):
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


# ============================================================================
# Exchange adapter context collection (default path)
# ============================================================================


async def _collect_context_via_adapter(
    context: MarketContext,
    exchange_adapter: Any,
    market_data_engine: Any,
    pairs: list[str],
    timeframes: list[str],
    candle_count: int,
) -> None:
    """Collect market context via exchange adapter + market data engine."""
    try:
        tasks: list[Any] = []
        for pair in pairs:
            for tf in timeframes:
                interval = _TIMEFRAME_MAP.get(tf, tf)
                tasks.append(
                    _fetch_klines(exchange_adapter, pair, interval, candle_count)
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

                assert isinstance(result, list)
                klines: list[tuple[float, ...]] = list(result)
                context.candles[key] = _klines_to_candles(pair, klines)
                logger.debug(
                    "candles_fetched",
                    pair=pair,
                    timeframe=tf,
                    count=len(klines),
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
            total_usdt=float(context.account.total_usdt) if context.account else 0,
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
                ticker_dict = dict(ticker)
                contract = FuturesContract(
                    symbol=pair,
                    mark_price=Decimal(str(mp)) if mp else Decimal(0),
                    last_price=Decimal(str(ticker_dict.get("lastPrice", 0))),
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

    # 5. Fetch smart money signals + sentiment for each pair (MCP-only features)
    async def _fetch_smart_money(pair: str) -> tuple[str, dict, dict] | None:
        try:
            coin = pair.replace("USDT", "") if "USDT" in pair else pair
            sm_raw = await mcp_client.get_smart_money_signals(coin, "7D")
            sm_data = sm_raw.get("data", {}) if isinstance(sm_raw, dict) else {}

            sent_raw = await mcp_client.get_coin_sentiment(coin)
            sent_data = sent_raw.get("data", {}) if isinstance(sent_raw, dict) else {}

            return (pair, sm_data, sent_data)
        except Exception as exc:
            context.errors[f"mcp_smart_money_{pair}"] = str(exc)
            return None

    sm_tasks = [_fetch_smart_money(p) for p in pairs]
    sm_results = await asyncio.gather(*sm_tasks, return_exceptions=True)
    for result in sm_results:
        if isinstance(result, Exception) or result is None:
            continue
        pair, sm_data, sent_data = result
        if sm_data:
            context.smart_money[pair] = sm_data
        if sent_data:
            context.sentiment[pair] = sent_data

    # 6. Fetch latest news (global, not per-pair)
    try:
        news_raw = await mcp_client.get_latest_news(20)
        news_data = news_raw.get("data", []) if isinstance(news_raw, dict) else []
        if isinstance(news_data, list):
            context.news = news_data[:20]
    except Exception as exc:
        context.errors["mcp_news"] = str(exc)

    return context
