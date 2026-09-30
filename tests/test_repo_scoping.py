"""Tests for Phase-4 tenant scoping: writes stamped, reads filtered,
cross-tenant get/update/delete blocked, custom queries scoped."""

import asyncio
import sys
import time
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from quad.persistence import DatabaseManager, make_repo  # noqa: E402
from quad.persistence.models import (  # noqa: E402
    DecisionModel,
    PositionModel,
    TradeModel,
)
from quad.persistence.repositories import (  # noqa: E402
    AccountRepository,
    DecisionRepository,
    PositionRepository,
    TradeRepository,
)


def _run(coro):
    return asyncio.run(coro)


def _mem():
    return DatabaseManager(":memory:")


NOW = int(time.time() * 1000)


def test_make_repo_unscoped_by_default():
    async def go():
        async with _mem() as db:
            repo = make_repo(TradeRepository, db, {})
            assert repo._tenant_scoped is False
            repo2 = make_repo(TradeRepository, db, {"_tenant_id": "t1"})
            assert repo2._tenant_scoped is True

    _run(go())


def test_create_stamps_and_reads_filter():
    async def go():
        async with _mem() as db:
            a = make_repo(PositionRepository, db, {"_tenant_id": "aaa"})
            b = make_repo(PositionRepository, db, {"_tenant_id": "bbb"})

            def mk(sym):
                return PositionModel(
                    id=0,
                    strategy="s",
                    symbol=sym,
                    side="BUY",
                    quantity="1",
                    entry_price="10",
                    current_price="10",
                    unrealized_pnl="0",
                    realized_pnl="0",
                    status="OPEN",
                    opened_at=NOW,
                    updated_at=NOW,
                )

            ida = await a.create(mk("BTCUSDT"))
            idb = await b.create(mk("ETHUSDT"))

            assert (await a.get(ida)).symbol == "BTCUSDT"
            assert await a.get(idb) is None  # cross-tenant get blocked
            assert [p.symbol for p in await a.list()] == ["BTCUSDT"]
            assert await a.count() == 1
            assert [p.symbol for p in await a.list(status="OPEN")] == ["BTCUSDT"]

            # unscoped sees everything (back-compat)
            plain = PositionRepository(db)
            assert await plain.count() == 2

            # cross-tenant update/delete are no-ops
            await a.update(idb, status="CLOSED")
            assert (await b.get(idb)).status == "OPEN"
            await a.delete(idb)
            assert await b.get(idb) is not None
            # own update/delete work
            await a.update(ida, status="CLOSED")
            assert (await a.get(ida)).status == "CLOSED"

    _run(go())


def test_custom_queries_scoped():
    async def go():
        async with _mem() as db:
            a = make_repo(TradeRepository, db, {"_tenant_id": "aaa"})
            b = make_repo(TradeRepository, db, {"_tenant_id": "bbb"})
            # trades FK-reference positions + orders — seed both rows first
            pos_repo = PositionRepository(db)
            pos_id = await pos_repo.create(
                PositionModel(
                    id=0,
                    strategy="s",
                    symbol="BTCUSDT",
                    side="BUY",
                    quantity="1",
                    entry_price="10",
                    current_price="10",
                    unrealized_pnl="0",
                    realized_pnl="0",
                    status="OPEN",
                    opened_at=NOW,
                    updated_at=NOW,
                )
            )
            from quad.persistence.models import OrderModel
            from quad.persistence.repositories import OrderRepository

            ord_id = await OrderRepository(db).create(
                OrderModel(
                    id=0,
                    client_order_id="seed-1",
                    position_id=pos_id,
                    symbol="BTCUSDT",
                    side="BUY",
                    type="MARKET",
                    quantity="1",
                    filled_qty="1",
                    price="10",
                    status="FILLED",
                    time_in_force="",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            for repo, sym in ((a, "BTCUSDT"), (b, "ETHUSDT")):
                await repo.create(
                    TradeModel(
                        id=0,
                        position_id=pos_id,
                        order_id=ord_id,
                        symbol=sym,
                        side="BUY",
                        quantity="1",
                        price="10",
                        fee="0",
                        pnl="5",
                        timestamp=NOW,
                    )
                )
            assert [t.symbol for t in await a.get_by_date_range(0, NOW + 1)] == [
                "BTCUSDT"
            ]
            assert await a.exists_for_order(ord_id, "BUY") is True
            # same order leg exists for bbb too, but a's scope sees only its own
            assert [t.symbol for t in await a.get_recent()] == ["BTCUSDT"]

            da = make_repo(DecisionRepository, db, {"_tenant_id": "aaa"})
            db_ = make_repo(DecisionRepository, db, {"_tenant_id": "bbb"})
            for repo in (da, db_):
                await repo.create(
                    DecisionModel(
                        id=0,
                        timestamp=NOW,
                        strategy="s",
                        action="ENTER",
                        symbol="X",
                        reason="r",
                        risk_passed=1,
                        executed=1,
                        cycle_time_ms=1,
                    )
                )
            assert len(await da.get_unresolved()) == 1
            assert len(await da.get_resolved(limit=10)) == 0
            assert len(await da.get_by_date_range(0, NOW + 1)) == 1
            assert len(await da.get_recent()) == 1

            pa = make_repo(PositionRepository, db, {"_tenant_id": "aaa"})
            assert [p.symbol for p in await pa.get_open()] == []
            await pa.create(
                PositionModel(
                    id=0,
                    strategy="s",
                    symbol="BTCUSDT",
                    side="BUY",
                    quantity="1",
                    entry_price="10",
                    current_price="10",
                    unrealized_pnl="0",
                    realized_pnl="0",
                    status="OPEN",
                    opened_at=NOW,
                    updated_at=NOW,
                )
            )
            assert [p.symbol for p in await pa.get_open()] == ["BTCUSDT"]

    _run(go())


def test_account_scoped():
    async def go():
        async with _mem() as db:
            from quad.persistence.models import AccountModel

            a = make_repo(AccountRepository, db, {"_tenant_id": "aaa"})
            b = make_repo(AccountRepository, db, {"_tenant_id": "bbb"})
            await a.upsert_account(
                AccountModel(
                    id=0,
                    exchange="bybit",
                    balances_json="{}",
                    total_usdt="100",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            assert (await a.get_by_exchange("bybit")).total_usdt == "100"
            assert await b.get_by_exchange("bybit") is None

    _run(go())
