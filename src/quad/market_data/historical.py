"""Historical market data provider for Quad futures trading bot.

Provides queries against the persistence layer for backtesting and analysis
use by strategies.  Some methods are stubs that will be fully implemented
when the backtesting engine (Phase 9) adds the required tables.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from quad.exchange.base import ExchangeAdapter
    from quad.persistence.database import DatabaseManager
    from quad.types.domain import Trade
    from quad.types.market import Candle

logger = structlog.get_logger(__name__)

_TRADE_COLUMNS = [
    "id",
    "position_id",
    "order_id",
    "symbol",
    "side",
    "quantity",
    "price",
    "fee",
    "pnl",
    "timestamp",
]


class HistoricalDataProvider:
    """Provides historical market data from the database and MCP server.

    Implements queries against the persistence layer for backtesting and
    strategy analysis.  When an MCP client is provided, historical candle
    data is fetched directly from the MCP server (fixing the previous
    stub limitation).
    """

    def __init__(
        self,
        db_manager: DatabaseManager,
        exchange_adapter: ExchangeAdapter | None = None,
        mcp_client: Any | None = None,
    ) -> None:
        """Initialize the provider.

        Parameters
        ----------
        db_manager:
            The ``DatabaseManager`` instance to query.
        exchange_adapter:
            Optional exchange adapter for REST-based history queries.
        mcp_client:
            Optional ``OkxMcpClient`` for MCP-powered candle fetching.
        """
        self._db = db_manager
        self._exchange = exchange_adapter
        self._mcp = mcp_client
        self._log = logger.bind(dsn=str(db_manager.dsn))

    # ------------------------------------------------------------------
    # Candle data (stub)
    # ------------------------------------------------------------------

    async def get_candles(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> list[Candle]:
        """Return OHLCV candle data for *symbol* over the date range.

        When an MCP client is available, fetches candles directly from
        the OKX MCP server.  Falls back to the exchange adapter if MCP
        is unavailable.

        Parameters
        ----------
        symbol:
            The trading pair symbol (e.g. ``"BTCUSDT"``).
        start:
            Inclusive start of the query window.
        end:
            Inclusive end of the query window.
        bar:
            Candle interval (default ``"1H"``).
        """
        from decimal import Decimal

        from quad.types.market import Candle

        # MCP path: fetch candles directly from the MCP server
        if self._mcp is not None and hasattr(self._mcp, "call_tool"):
            try:
                inst_id = symbol
                if "-" not in symbol and "SWAP" not in symbol:
                    # Convert BTCUSDT → BTC-USDT-SWAP
                    inst_id = symbol.replace("USDT", "-USDT-SWAP") if "USDT" in symbol else f"{symbol}-USDT-SWAP"

                result = await self._mcp.call_tool(
                    "market_get_candles",
                    {"instId": inst_id, "bar": bar, "limit": "300"},
                )
                data = result.get("data", []) if isinstance(result, dict) else []

                candles: list[Candle] = []
                for row in data:
                    if not isinstance(row, (list, tuple)) or len(row) < 6:
                        continue
                    ts_ms = int(row[0])
                    candle_ts = datetime.fromtimestamp(ts_ms / 1000)
                    if start <= candle_ts <= end:
                        candles.append(
                            Candle(
                                symbol=symbol,
                                open=Decimal(str(row[1])),
                                high=Decimal(str(row[2])),
                                low=Decimal(str(row[3])),
                                close=Decimal(str(row[4])),
                                volume=Decimal(str(row[5])),
                                timestamp=ts_ms,
                            )
                        )
                self._log.debug(
                    "mcp_candles_fetched",
                    symbol=symbol,
                    bar=bar,
                    count=len(candles),
                )
                return candles
            except Exception as exc:
                self._log.warning(
                    "mcp_candles_fetch_failed",
                    symbol=symbol,
                    bar=bar,
                    error=str(exc),
                )

        # Fallback: exchange adapter
        if self._exchange is not None and hasattr(self._exchange, "get_klines"):
            try:
                interval_map = {"1H": "1H", "4H": "4H", "1D": "1D", "15m": "15m"}
                interval = interval_map.get(bar, bar)
                raw_klines = await self._exchange.get_klines(symbol, interval, 300)
                if raw_klines:
                    candles = []
                    for k in raw_klines:
                        ts_ms = int(k[0])
                        candle_ts = datetime.fromtimestamp(ts_ms / 1000)
                        if start <= candle_ts <= end:
                            candles.append(
                                Candle(
                                    symbol=symbol,
                                    open=Decimal(str(k[1])),
                                    high=Decimal(str(k[2])),
                                    low=Decimal(str(k[3])),
                                    close=Decimal(str(k[4])),
                                    volume=Decimal(str(k[5])),
                                    timestamp=ts_ms,
                                )
                            )
                    return candles
            except Exception as exc:
                self._log.warning(
                    "exchange_candles_fetch_failed",
                    symbol=symbol,
                    error=str(exc),
                )

        self._log.warning(
            "get_candles_no_data_source",
            symbol=symbol,
            start=start.isoformat(),
            end=end.isoformat(),
        )
        return []

    # ------------------------------------------------------------------
    # Funding rate history
    # ------------------------------------------------------------------

    async def get_funding_rate_history(
        self,
        symbol: str,
        start_time: int | None = None,
        end_time: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return historical funding rate data for *symbol*.

        Delegates to the exchange adapter's ``get_funding_rate_history``
        if available.  Returns an empty list if the adapter does not
        support historical queries.

        Parameters
        ----------
        symbol:
            The trading pair symbol (e.g. ``"BTCUSDT"``).
        start_time:
            Optional start time in unix milliseconds.
        end_time:
            Optional end time in unix milliseconds.
        limit:
            Maximum number of records to return (default 100).

        Returns
        -------
        list[dict]
            Each dict contains ``symbol``, ``funding_rate``, ``mark_price``,
            and ``funding_time`` keys.
        """
        if self._exchange is None or not hasattr(
            self._exchange, "get_funding_rate_history"
        ):
            self._log.warning(
                "get_funding_rate_history_not_available",
                symbol=symbol,
            )
            return []

        try:
            result = await self._exchange.get_funding_rate_history(
                symbol=symbol,
                start_time=start_time,
                end_time=end_time,
                limit=limit,
            )
            self._log.debug(
                "funding_rate_history_fetched",
                symbol=symbol,
                count=len(result),
            )
            return result
        except Exception:
            self._log.exception(
                "funding_rate_history_failed",
                symbol=symbol,
            )
            return []

    # ------------------------------------------------------------------
    # Open interest history
    # ------------------------------------------------------------------

    async def get_open_interest_history(
        self,
        symbol: str,
        period: str = "5m",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return historical open interest data for *symbol*.

        Delegates to the exchange adapter's ``get_open_interest_history``
        if available.  Returns an empty list if the adapter does not
        support historical queries.

        Parameters
        ----------
        symbol:
            The trading pair symbol (e.g. ``"BTCUSDT"``).
        period:
            Data granularity (e.g. ``"5m"``, ``"15m"``, ``"30m"``,
            ``"1h"``, ``"2h"``, ``"4h"``, ``"6h"``, ``"12h"``, ``"1d"``).
        limit:
            Maximum number of records to return (default 100).

        Returns
        -------
        list[dict]
            Each dict contains ``symbol``, ``open_interest``, ``timestamp``,
            and ``open_interest_value`` keys.
        """
        if self._exchange is None or not hasattr(
            self._exchange, "get_open_interest_history"
        ):
            self._log.warning(
                "get_open_interest_history_not_available",
                symbol=symbol,
            )
            return []

        try:
            result = await self._exchange.get_open_interest_history(
                symbol=symbol,
                period=period,
                limit=limit,
            )
            self._log.debug(
                "open_interest_history_fetched",
                symbol=symbol,
                count=len(result),
            )
            return result
        except Exception:
            self._log.exception(
                "open_interest_history_failed",
                symbol=symbol,
            )
            return []

    # ------------------------------------------------------------------
    # Top trader long/short ratio
    # ------------------------------------------------------------------

    async def get_top_trader_long_short_ratio(
        self,
        symbol: str,
        period: str = "5m",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return top trader long/short ratio for *symbol*.

        Delegates to the exchange adapter's
        ``get_top_trader_long_short_ratio`` if available.  Returns an
        empty list if the adapter does not support this query.

        .. note::
            This endpoint may require special API key permissions.

        Parameters
        ----------
        symbol:
            The trading pair symbol (e.g. ``"BTCUSDT"``).
        period:
            Data granularity (e.g. ``"5m"``, ``"15m"``, ``"30m"``,
            ``"1h"``, ``"2h"``, ``"4h"``, ``"6h"``, ``"12h"``, ``"1d"``).
        limit:
            Maximum number of records to return (default 100).

        Returns
        -------
        list[dict]
            Each dict contains ``symbol``, ``long_short_ratio``,
            ``long_account``, ``short_account``, and ``timestamp`` keys.
        """
        if self._exchange is None or not hasattr(
            self._exchange, "get_top_trader_long_short_ratio"
        ):
            self._log.warning(
                "get_top_trader_ls_ratio_not_available",
                symbol=symbol,
            )
            return []

        try:
            result = await self._exchange.get_top_trader_long_short_ratio(
                symbol=symbol,
                period=period,
                limit=limit,
            )
            self._log.debug(
                "top_trader_ls_ratio_fetched",
                symbol=symbol,
                count=len(result),
            )
            return result
        except Exception:
            self._log.exception(
                "top_trader_ls_ratio_failed",
                symbol=symbol,
            )
            return []

    # ------------------------------------------------------------------
    # Trade history
    # ------------------------------------------------------------------

    async def get_trade_history(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> list[Trade]:
        """Return trade history for *symbol* over the date range.

        Parameters
        ----------
        symbol:
            The trading pair symbol (e.g. ``"BTCUSDT"``).
        start:
            Inclusive start of the query window.
        end:
            Inclusive end of the query window.
        """
        from decimal import Decimal

        from quad.types.domain import Trade

        try:
            start_ms = int(start.timestamp() * 1000)
            end_ms = int(end.timestamp() * 1000)

            columns = ", ".join(_TRADE_COLUMNS)
            query = (
                f"SELECT {columns} FROM trades "
                f"WHERE symbol = $1 AND timestamp >= $2 AND timestamp <= $3 "
                f"ORDER BY timestamp DESC"
            )

            async with self._db.pool.acquire() as conn:
                rows = await conn.fetch(query, symbol, start_ms, end_ms)

            trades: list[Trade] = []
            for row in rows:
                trades.append(
                    Trade(
                        id=row[0],
                        position_id=row[1],
                        order_id=row[2],
                        symbol=row[3],
                        side=row[4],
                        quantity=Decimal(str(row[5])),
                        price=Decimal(str(row[6])),
                        fee=Decimal(str(row[7])),
                        pnl=Decimal(str(row[8])),
                        timestamp=row[9],
                    )
                )

            self._log.debug(
                "trade_history_fetched",
                symbol=symbol,
                count=len(trades),
            )
            return trades

        except Exception:
            self._log.exception(
                "trade_history_failed",
                symbol=symbol,
                start=start.isoformat(),
                end=end.isoformat(),
            )
            return []
