"""Worker lifecycle endpoints: per-tenant kill-switch and resume."""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, Request

from .deps import ApiState, current_tenant, envelope_error, get_state, log_audit

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1/worker", tags=["worker"])


def _supervisor(request: Request):
    return getattr(request.app.state, "supervisor", None)


def _require_supervisor(request: Request):
    sup = _supervisor(request)
    if sup is None:
        raise envelope_error(
            "supervisor_disabled", "worker control is disabled on this instance", 503
        )
    return sup


@router.post("/kill")
async def worker_kill(
    request: Request,
    flatten: bool = True,
    ctx: dict[str, Any] = Depends(current_tenant),
    state: ApiState = Depends(get_state),
):
    """Emergency stop: halt the worker, suspend the tenant.

    With flatten=true (default) all open orders are cancelled
    exchange-side before the worker stops.  The tenant is suspended so a
    crashed-loop restart or stray ensure cannot revive trading until
    an explicit /resume.
    """
    tenant = ctx["tenant"]
    # Kill-switch works even when the supervisor is disabled (dev/single
    # process): flatten + halt always apply; only the process stop is skipped.
    sup = _supervisor(request)
    cancelled: list = []
    warnings: list = []
    if flatten:
        try:
            from .flatten import flatten_account

            cancelled, warnings = await flatten_account(state, tenant.tenant_uuid)
        except Exception as exc:
            warnings.append(f"flatten unavailable: {exc}")
    if sup is not None:
        await sup.stop(tenant.tenant_uuid)
    await state.tenants.update(tenant.id, status="halted")
    await log_audit(
        state, tenant.tenant_uuid, "worker.kill", "active", "halted", "api:worker/kill"
    )
    return {
        "ok": True,
        "data": {
            "state": "halted",
            "tenant": "halted",
            "worker_state": "stopped",
            "orders_cancelled": cancelled,
            "warnings": warnings,
        },
    }


@router.post("/resume")
async def worker_resume(
    request: Request,
    ctx: dict[str, Any] = Depends(current_tenant),
    state: ApiState = Depends(get_state),
):
    """Re-activate a halted tenant (worker restarts when credentials exist)."""
    tenant = ctx["tenant"]
    fresh = await state.tenants.get(tenant.id)
    if fresh is None:
        raise envelope_error("unknown_tenant", "tenant no longer exists", 404)
    if fresh.status == "halted":
        await state.tenants.update(tenant.id, status="active")
    # Resume is an un-halt: the worker (re)starts on next connect/restart when
    # credentials and a supervisor exist. Never refuse the un-halt itself.
    creds = await state.credentials.get_active(tenant.tenant_uuid)
    sup = _supervisor(request)
    worker_state, pid = "disabled", None
    if sup is not None and creds is not None:
        info = await sup.ensure(tenant.tenant_uuid)
        if info.state == "failed":
            logger.warning("worker_resume_failed", error=(info.last_error or "")[:200])
            raise envelope_error("worker_failed", "worker failed to start", 500)
        worker_state, pid = info.state, info.pid
    await log_audit(
        state,
        tenant.tenant_uuid,
        "worker.resume",
        tenant.status,
        "active",
        "api:worker/resume",
    )
    return {"ok": True, "data": {"worker_state": worker_state, "pid": pid}}


@router.post("/restart")
async def worker_restart(
    request: Request,
    ctx: dict[str, Any] = Depends(current_tenant),
    state: ApiState = Depends(get_state),
):
    """Restart the worker (picks up config/credential changes)."""
    tenant = ctx["tenant"]
    sup = _require_supervisor(request)
    await sup.stop(tenant.tenant_uuid)
    info = await sup.ensure(tenant.tenant_uuid)
    if info.state == "failed":
        logger.warning("worker_restart_failed", error=(info.last_error or "")[:200])
        raise envelope_error("worker_failed", "worker failed to start", 500)
    await log_audit(
        state, tenant.tenant_uuid, "worker.restart", "", "", "api:worker/restart"
    )
    return {"ok": True, "data": {"worker_state": info.state, "pid": info.pid}}


@router.get("/status")
async def worker_status(
    request: Request,
    ctx: dict[str, Any] = Depends(current_tenant),
):
    sup = _supervisor(request)
    if sup is None:
        return {"ok": True, "data": {"worker_state": "disabled"}}
    return {"ok": True, "data": sup.status(ctx["tenant"].tenant_uuid)}
