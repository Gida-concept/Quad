"""Config endpoints: per-tenant trading parameters (linear-only v1)."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends

from .deps import ApiState, current_tenant, envelope_error, get_state, log_audit
from .schemas import ConfigBody, ConfigResponse

router = APIRouter(prefix="/v1/config", tags=["config"])


def _to_response(cfg: Any, state: ApiState) -> ConfigResponse:
    symbols = list(getattr(state, "server_symbols", []) or [])
    return ConfigResponse(
        market=cfg.market,
        capital_pct_per_trade=float(cfg.capital_pct_per_trade),
        leverage=int(cfg.leverage),
        take_profit_pct=float(cfg.take_profit_pct),
        stop_loss_pct=float(cfg.stop_loss_pct),
        strategy=cfg.strategy,
        symbols=[str(s) for s in symbols],
        max_positions=int(cfg.max_positions),
        ai_tier=str(getattr(cfg, "ai_tier", "cheap") or "cheap"),
        ai_max_calls_per_day=int(getattr(cfg, "ai_max_calls_per_day", 24) or 24),
        strategy_mode=str(getattr(cfg, "strategy_mode", "trend") or "trend"),
        scalp_tp_pct=float(getattr(cfg, "scalp_tp_pct", 15.0) or 15.0),
        scalp_sl_pct=float(getattr(cfg, "scalp_sl_pct", 8.0) or 8.0),
        scalp_max_calls_per_day=int(
            getattr(cfg, "scalp_max_calls_per_day", 120) or 120
        ),
        updated_at=int(cfg.updated_at or 0),
    )


@router.get("", response_model=ConfigResponse)
async def get_config(
    ctx: dict[str, Any] = Depends(current_tenant),
    state: ApiState = Depends(get_state),
):
    cfg = await state.configs.get_or_default(ctx["tenant"].tenant_uuid)
    return _to_response(cfg, state)


@router.put("", response_model=ConfigResponse)
async def put_config(
    body: ConfigBody,
    ctx: dict[str, Any] = Depends(current_tenant),
    state: ApiState = Depends(get_state),
):
    if body.strategy_mode == "scalp" and body.leverage > 10:
        raise envelope_error(
            "invalid_config",
            "scalp mode caps leverage at 10x (spread-noise liquidates higher)",
            400,
        )
    cfg = await state.configs.get_or_default(ctx["tenant"].tenant_uuid)
    await state.configs.update(
        cfg.id,
        market=body.market,
        capital_pct_per_trade=body.capital_pct_per_trade,
        leverage=body.leverage,
        take_profit_pct=body.take_profit_pct,
        stop_loss_pct=body.stop_loss_pct,
        strategy=body.strategy,
        max_positions=body.max_positions,
        ai_tier=body.ai_tier,
        ai_max_calls_per_day=body.ai_max_calls_per_day,
        strategy_mode=body.strategy_mode,
        scalp_tp_pct=body.scalp_tp_pct,
        scalp_sl_pct=body.scalp_sl_pct,
        scalp_max_calls_per_day=body.scalp_max_calls_per_day,
        updated_at=int(time.time() * 1000),
    )
    updated = await state.configs.get_by_tenant(ctx["tenant"].tenant_uuid)
    if updated is None:
        raise envelope_error("internal_error", "config write failed", 500)
    await log_audit(
        state,
        ctx["tenant"].tenant_uuid,
        "config.update",
        "",
        f"{body.strategy_mode}/{body.strategy}/{body.leverage}x/{body.capital_pct_per_trade}%",
        "api",
    )
    return _to_response(updated, state)
