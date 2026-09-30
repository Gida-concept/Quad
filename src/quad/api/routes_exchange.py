"""Exchange endpoints: verify-then-store Bybit keys, connection status."""

from __future__ import annotations

import os
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Request

from quad.security import encrypt_secret

from .bybit_verify import BybitVerifyError, verify_bybit_credentials
from .deps import ApiState, current_tenant, envelope_error, get_state, log_audit
from .schemas import (
    ExchangeConnectBody,
    ExchangeConnectResponse,
    ExchangeStatusResponse,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1/exchange", tags=["exchange"])


def _supervisor(request: Request):
    """Return the worker supervisor, or None when disabled (dev/test)."""
    return getattr(request.app.state, "supervisor", None)


@router.post("/connect", response_model=ExchangeConnectResponse)
async def exchange_connect(
    body: ExchangeConnectBody,
    request: Request,
    ctx: dict[str, Any] = Depends(current_tenant),
    state: ApiState = Depends(get_state),
):
    tenant = ctx["tenant"]
    if tenant.status != "active":
        raise envelope_error(
            "trading_halted", "trading is halted for this tenant; resume first", 409
        )
    # Live gate: real funds require explicit server opt-in + user confirmation.
    # QUAD_ALLOW_LIVE=true (operator) AND confirm_live=true (user) or no live.
    if not body.testnet:
        allow_live = os.environ.get("QUAD_ALLOW_LIVE", "false").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        if not allow_live:
            raise envelope_error(
                "live_trading_disabled",
                "live trading is disabled on this server; testnet only",
                403,
            )
        if not body.confirm_live:
            raise envelope_error(
                "live_confirmation_required",
                "live trading requires explicit confirmation "
                "(resend with confirm_live=true)",
                400,
            )
    try:
        meta = await verify_bybit_credentials(
            body.api_key, body.api_secret, testnet=body.testnet
        )
    except BybitVerifyError as exc:
        # Generic client message; detail stays in the server log.
        logger.warning("exchange_verify_failed", error=str(exc)[:120])
        raise envelope_error(
            "invalid_credentials", "Bybit rejected the credentials", 400
        ) from exc
    except Exception as exc:
        logger.warning("exchange_verify_unreachable", error=str(exc)[:120])
        raise envelope_error(
            "invalid_credentials",
            "could not verify credentials with Bybit (retryable)",
            400,
        ) from exc
    try:
        await state.credentials.upsert_encrypted(
            tenant.tenant_uuid,
            encrypt_secret(body.api_key),
            encrypt_secret(body.api_secret),
            testnet=body.testnet,
            bybit_uid=meta["bybit_uid"],
            permissions=meta["permissions"],
        )
    except Exception as exc:
        logger.warning("exchange_store_failed", error=str(exc)[:120])
        raise envelope_error(
            "storage_failed", "could not store credentials", 500
        ) from exc
    await log_audit(
        state,
        tenant.tenant_uuid,
        "exchange.connect",
        "",
        f"bybit:{'testnet' if body.testnet else 'live'}:{meta['bybit_uid']}",
        "api",
    )
    supervisor = _supervisor(request)
    if supervisor is not None:
        info = await supervisor.ensure(tenant.tenant_uuid)
        if info.state == "failed":
            logger.warning(
                "exchange_worker_failed", error=(info.last_error or "")[:200]
            )
            raise envelope_error(
                "worker_failed", "credentials stored but worker failed to start", 500
            )
    return ExchangeConnectResponse(
        verified=True,
        testnet=body.testnet,
        bybit_uid=meta["bybit_uid"],
        permissions=meta["permissions"],
    )


@router.get("/status", response_model=ExchangeStatusResponse)
async def exchange_status(
    request: Request,
    ctx: dict[str, Any] = Depends(current_tenant),
    state: ApiState = Depends(get_state),
):
    tenant = ctx["tenant"]
    creds = await state.credentials.get_active(tenant.tenant_uuid)
    if creds is None:
        return ExchangeStatusResponse(connected=False)
    supervisor = _supervisor(request)
    worker = (
        supervisor.status(tenant.tenant_uuid) if supervisor else {"state": "disabled"}
    )
    # The supervisor reports a pid, but it can arrive as a string (it is read
    # back from a status file written by another process).  Coerce rather than
    # hand the raw value to Pydantic, so a malformed pid degrades to "unknown"
    # instead of failing the whole status response.
    raw_pid = worker.get("pid")
    worker_pid: int | None
    try:
        worker_pid = int(raw_pid) if raw_pid is not None else None
    except (TypeError, ValueError):
        worker_pid = None
    return ExchangeStatusResponse(
        connected=True,
        testnet=bool(creds.testnet),
        bybit_uid=creds.bybit_uid,
        last_verified_at=creds.last_verified_at,
        worker_state=worker.get("state"),
        worker_pid=worker_pid,
    )


@router.delete("/disconnect")
async def exchange_disconnect(
    request: Request,
    ctx: dict[str, Any] = Depends(current_tenant),
    state: ApiState = Depends(get_state),
):
    """Remove stored Bybit credentials and stop the tenant's worker."""
    tenant = ctx["tenant"]
    creds = await state.credentials.get_active(tenant.tenant_uuid)
    if creds is not None:
        await state.credentials.delete(creds.id)
    await log_audit(
        state,
        tenant.tenant_uuid,
        "exchange.disconnect",
        "connected",
        "disconnected",
        "api",
    )
    supervisor = _supervisor(request)
    if supervisor is not None:
        await supervisor.stop(tenant.tenant_uuid)
    return {"ok": True, "data": {"connected": False}}
