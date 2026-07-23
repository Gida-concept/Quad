"""Historical market data provider for Quad options trading bot.

Provides queries against the persistence layer for backtesting and analysis
use by strategies.  Some methods are stubs that will be fully implemented
when the backtesting engine (Phase 9) adds the required tables.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from quad.persistence.database import DatabaseManager
    from quad.types.domain import Trade
    from quad.types.market import Candle, OptionContract, OptionPriceTick

logger = structlog.get_logger(__name__)

# Column names for the option_contracts table (from models.py)
_OPTION_CONTRACT_COLUMNS = [
    "symbol",
    "underlying",
    "strike",
    "expiry",
    "option_type",
    "mark_price",
    "bid",
    "ask",
    "volume",
    "open_interest",
    "iv",
    "delta",
    "gamma",
    "theta",
    "vega",
]

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
    """Provides historical market data from the database.

    Implements queries against the persistence layer for backtesting and
    strategy analysis.
    """

    def __init__(self, db_manager: DatabaseManager) -> None:
        """Initialize the provider.

        Parameters
        ----------
        db_manager:
            The ``DatabaseManager`` instance to query.
        """
        self._db = db_manager
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

        .. note::
            This is a **stub** that returns an empty list.  Candles will
            be persisted and queryable once the backtesting engine (Phase 9)
            implements candle storage.

        Parameters
        ----------
        symbol:
            The trading pair symbol (e.g. ``"BTCUSDT"``).
        start:
            Inclusive start of the query window.
        end:
            Inclusive end of the query window.
        """
        self._log.warning(
            "get_candles_not_implemented",
            symbol=symbol,
            start=start.isoformat(),
            end=end.isoformat(),
        )
        return []

    # ------------------------------------------------------------------
    # Option chain snapshots
    # ------------------------------------------------------------------

    async def get_option_chain_snapshot(
        self,
        symbol: str,
        timestamp: datetime,
    ) -> list[OptionContract]:
        """Return the most recent option chain snapshot for *symbol*.

        Queries the ``option_contracts`` table for records that match the
        given underlying symbol and were updated at or before *timestamp*.

        Parameters
        ----------
        symbol:
            The underlying asset symbol (e.g. ``"BTCUSDT"``).
        timestamp:
            The snapshot cutoff time.
        """
        from decimal import Decimal

        from quad.types.market import OptionContract

        try:
            ts_ms = int(timestamp.timestamp() * 1000)

            columns = ", ".join(_OPTION_CONTRACT_COLUMNS)
            query = (
                f"SELECT {columns} FROM option_contracts "
                f"WHERE underlying = $1 AND updated_at <= $2 "
                f"ORDER BY expiry ASC, strike ASC"
            )

            async with self._db.pool.acquire() as conn:
                rows = await conn.fetch(query, symbol, ts_ms)

            contracts: list[OptionContract] = []
            for row in rows:
                contracts.append(
                    OptionContract(
                        symbol=row[0],
                        underlying=row[1],
                        strike=Decimal(str(row[2])),
                        expiry=row[3],
                        option_type=row[4],
                        mark_price=Decimal(str(row[5])),
                        bid=Decimal(str(row[6])) if row[6] is not None else None,
                        ask=Decimal(str(row[7])) if row[7] is not None else None,
                        volume=Decimal(str(row[8])) if row[8] is not None else Decimal("0"),
                        open_interest=int(row[9]) if row[9] is not None else 0,
                        implied_volatility=Decimal(str(row[10])) if row[10] is not None else Decimal("0"),
                        delta=Decimal(str(row[11])) if row[11] is not None else Decimal("0"),
                        gamma=Decimal(str(row[12])) if row[12] is not None else Decimal("0"),
                        theta=Decimal(str(row[13])) if row[13] is not None else Decimal("0"),
                        vega=Decimal(str(row[14])) if row[14] is not None else Decimal("0"),
                    )
                )

            self._log.debug(
                "chain_snapshot_fetched",
                symbol=symbol,
                count=len(contracts),
            )
            return contracts

        except Exception:
            self._log.exception(
                "chain_snapshot_failed",
                symbol=symbol,
                timestamp=timestamp.isoformat(),
            )
            return []

    # ------------------------------------------------------------------
    # Price history (stub)
    # ------------------------------------------------------------------

    async def get_price_history(
        self,
        symbol: str,
        limit: int = 100,
    ) -> list[OptionPriceTick]:
        """Return recent price history for *symbol*.

        .. note::
            This is a **stub** that returns an empty list.  Price history
            will be available once the backtesting component records tick
            data.

        Parameters
        ----------
        symbol:
            The option symbol (e.g. ``"BTC-220930-20000-C"``).
        limit:
            Maximum number of ticks to return.
        """
        self._log.warning(
            "get_price_history_not_implemented",
            symbol=symbol,
            limit=limit,
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
