"""Tenant-scoped trading reads: positions, orders, decisions, PnL."""

from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, Query

from .deps import ApiState, current_tenant, get_state
from .schemas import OrderOut, PnlSummary, PositionOut

router = APIRouter(prefix="/v1", tags=["trading"])


def _as_dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(0)


@router.get("/positions", response_model=list[PositionOut])
async def list_positions(
    status: str = Query(default="OPEN"),
    ctx: dict[str, Any] = Depends(current_tenant),
    state: ApiState = Depends(get_state),
):
    rows = await state.positions.list(
        tenant_id=ctx["tenant"].tenant_uuid, status=status
    )
    return [
        PositionOut(
            symbol=r.symbol,
            side=r.side,
            quantity=r.quantity,
            entry_price=r.entry_price,
            current_price=r.current_price,
            unrealized_pnl=r.unrealized_pnl,
            leverage=int(r.leverage),
            status=r.status,
        )
        for r in rows
    ]


@router.get("/orders", response_model=list[OrderOut])
async def list_orders(
    limit: int = Query(default=20, ge=1, le=100),
    ctx: dict[str, Any] = Depends(current_tenant),
    state: ApiState = Depends(get_state),
):
    rows = await state.orders.list(tenant_id=ctx["tenant"].tenant_uuid)
    rows = sorted(rows, key=lambda r: r.created_at, reverse=True)[:limit]
    return [
        OrderOut(
            client_order_id=r.client_order_id,
            symbol=r.symbol,
            side=r.side,
            type=r.type,
            quantity=r.quantity,
            price=r.price,
            status=r.status,
        )
        for r in rows
    ]


@router.get("/pnl", response_model=PnlSummary)
async def pnl_summary(
    ctx: dict[str, Any] = Depends(current_tenant),
    state: ApiState = Depends(get_state),
):
    tuuid = ctx["tenant"].tenant_uuid
    day_start = int(time.time() * 1000) - 24 * 3600 * 1000
    realized = await state.trades.sum_pnl_since(tenant_id=tuuid, since_ms=day_start)
    open_pos = await state.positions.list(tenant_id=tuuid, status="OPEN")
    unrealized = sum((_as_dec(p.unrealized_pnl) for p in open_pos), Decimal(0))
    return PnlSummary(realized_today=str(realized), open_unrealized=str(unrealized))


@router.get("/status")
async def tenant_status(
    ctx: dict[str, Any] = Depends(current_tenant),
    state: ApiState = Depends(get_state),
):
    """Combined snapshot for dashboards: config + creds + counts."""
    tenant = ctx["tenant"]
    cfg = await state.configs.get_or_default(tenant.tenant_uuid)
    creds = await state.credentials.get_active(tenant.tenant_uuid)
    binding = await state.bindings.get_by_tenant(tenant.tenant_uuid)
    open_count = await state.positions.count(
        tenant_id=tenant.tenant_uuid, status="OPEN"
    )
    return {
        "ok": True,
        "data": {
            "tenant_uuid": tenant.tenant_uuid,
            "trading_halted": tenant.status != "active",
            "market": cfg.market,
            "strategy": cfg.strategy,
            "exchange_connected": creds is not None,
            "exchange_testnet": bool(creds.testnet) if creds else True,
            "telegram_bound": binding is not None,
            "open_positions": open_count,
        },
    }


@router.get("/audit")
async def audit_log(
    limit: int = Query(default=50, ge=1, le=200),
    ctx: dict[str, Any] = Depends(current_tenant),
    state: ApiState = Depends(get_state),
):
    """Tenant-scoped audit trail (credential + config + halt events)."""
    rows = await state.config_audit.list(tenant_id=ctx["tenant"].tenant_uuid)
    rows = sorted(rows, key=lambda r: r.timestamp, reverse=True)[:limit]
    return {
        "ok": True,
        "data": [
            {
                "timestamp": r.timestamp,
                "key": r.key,
                "old": r.old_value,
                "new": r.new_value,
                "source": r.source,
            }
            for r in rows
        ],
    }
